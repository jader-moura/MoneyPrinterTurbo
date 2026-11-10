"""发到 YouTube 之前要准备的那三样：标题、简介、封面。

Why this is a separate module
-----------------------------
``scene_editor`` 管的是"这条片子剪成什么样"：画面、台词、字幕、声音——每一项
都会改变成片本身。这里管的是"这条片子怎么发出去"：标题、简介和封面一个字节
都不进成片，渲染器根本不读它们。两者放在一起，``scene_editor`` 就会开始同时
扮演剪辑台和发布台。

Everything here is additive: render and publish never read it, so a task that
never opens this panel renders byte-for-byte as before.

Why the aspect ratio comes first
--------------------------------
封面的形状由画幅决定，而不是反过来。9:16 的片子进 Shorts，封面必须是竖图，
上下还要给播放器的按钮让位；16:9 的片子进普通视频页，封面是横图。拿一张横图
配一条竖片，YouTube 会把它两边裁掉——而主体通常正在那里。

所以这个模块的每一个入口都先读出这条片子真正的画幅（从任务清单里存着的那份
参数，不是"现在"的全局配置），再决定写什么、出多大的图。界面也拿得到它，因此
用户在按下"生成封面"之前就看得见会得到一张什么形状的图。

Why it is allowed while the task is busy
----------------------------------------
``scene_editor`` 的编辑操作在任务运行期间一律拒绝，因为渲染器正在读那些素材
文件。这里不拒绝：写文案恰恰是等渲染的那几分钟里最自然的事，而封面和文案都
不是渲染器的输入。唯一共用的是 ``script.json``，而那份文件的写入本来就是带锁
的原子替换，这里只往里添一个 ``publish`` 字段。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any

from loguru import logger

from app.models.schema import VideoAspect
from app.services import llm, material, scene_editor, scene_manifest, task_artifacts
from app.services import thumbnail_text
from app.utils import utils

SceneError = scene_manifest.SceneError

#: 清单里存发布信息的那个键。
_PUBLISH_KEY = "publish"

#: 封面的文件名。固定名字而不是每次一个新名字：一条片子只有一张封面，
#: 重新生成就是替换它——攒下一堆 ``thumbnail-a3f9.png`` 没有任何好处，而且
#: 界面还得去猜哪一张是最新的。
THUMBNAIL_FILENAME = "youtube-thumbnail.png"

#: 压字之前那张图。成品是"图 + 字"，而这两样的代价差了几个数量级：换图要
#: 重新出图或重新取帧，改字只是重画一次文字。分开存着，改字就不用碰图，
#: 换图也不会把调好的字弄丢。
BASE_FILENAME = "youtube-thumbnail-base.png"

#: 标签的总长度上限。YouTube 自己的限制是全部标签合起来 500 个字符。
MAX_TAGS_LENGTH = 500
MAX_TAG_LENGTH = 100


def _record(task_id: str) -> dict:
    """这条片子已经存下来的发布信息。没有就是空的。"""
    payload = scene_manifest.load_raw(task_id)
    stored = payload.get(_PUBLISH_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def _save(task_id: str, record: dict) -> dict:
    if not task_artifacts.patch_script_data(task_id, **{_PUBLISH_KEY: record}):
        raise SceneError("failed to save the publishing details")
    return record


def aspect_of(task_id: str) -> str:
    """这条片子的画幅，按出片时存下来的参数算。

    读的是任务清单里的 ``params``，不是 config.toml 现在的值：编辑器跑在 API
    进程里，那份配置可能早就不是生成这条片子时的那一份了。认不出来时按竖版
    算，和渲染器自己的回落一致。
    """
    try:
        params = scene_editor.load_params(task_id)
    except SceneError:
        return llm.DEFAULT_YOUTUBE_ASPECT
    raw = getattr(params, "video_aspect", None)
    value = str(getattr(raw, "value", raw) or "").strip()
    try:
        return VideoAspect(value).value
    except ValueError:
        return llm.DEFAULT_YOUTUBE_ASPECT


def _subject_and_script(task_id: str) -> tuple[str, str]:
    """生成文案要用的素材：主题和旁白全文。"""
    manifest = scene_manifest.load(task_id)
    script = str(manifest.get("script") or "").strip()
    subject = ""
    try:
        params = scene_editor.load_params(task_id)
        subject = str(getattr(params, "video_subject", "") or "").strip()
    except SceneError as exc:
        logger.warning(f"cannot read the subject of {task_id}: {exc}")
    return subject, script


def thumbnail_file(task_id: str, *, require_file: bool = True) -> str:
    """封面的绝对路径。不存在时报错点名没有封面。"""
    record = _record(task_id)
    name = str(record.get("thumbnail") or "")
    if not name:
        raise SceneError("this video has no thumbnail yet")
    try:
        return scene_manifest.resolve_task_file(
            task_id, name, require_file=require_file
        )
    except SceneError as exc:
        raise SceneError(f"the thumbnail file is gone: {exc}") from exc


def describe(task_id: str, endpoint: str = "") -> dict:
    """界面要显示的全部内容：画幅、文案、封面，以及各自的上限。

    画幅无条件带上，而且连成品封面的像素尺寸一起给——用户要在按下"生成封面"
    之前就知道会得到一张什么形状、多大的图，而不是出完才发现是竖的。
    """
    aspect = aspect_of(task_id)
    spec = llm.youtube_format(aspect)
    record = _record(task_id)
    width, height = material.YOUTUBE_THUMBNAIL_PIXELS[VideoAspect(aspect)]

    thumbnail_url = ""
    thumbnail_name = str(record.get("thumbnail") or "")
    if thumbnail_name:
        thumbnail_url = scene_editor.media_url(task_id, thumbnail_name, endpoint)
        if thumbnail_url:
            # 封面是固定文件名，重新生成会原地替换它。不带一个会变的参数，
            # 浏览器会把上一张接着显示出来，用户以为封面没重出。
            try:
                stamp = int(os.path.getmtime(thumbnail_file(task_id)))
                thumbnail_url = f"{thumbnail_url}?v={stamp}"
            except (SceneError, OSError):
                thumbnail_url = ""

    subject, script = _subject_and_script(task_id)
    return {
        "task_id": task_id,
        # 这条片子是竖的还是横的，以及它在 YouTube 上是哪一种东西。
        "aspect": aspect,
        "kind": spec["kind"],
        "format_label": spec["label"],
        "title": str(record.get("title") or ""),
        "description": str(record.get("description") or ""),
        "tags": [str(tag) for tag in record.get("tags") or []],
        "thumbnail_prompt": str(record.get("thumbnail_prompt") or ""),
        "thumbnail": thumbnail_name,
        "thumbnail_url": thumbnail_url,
        "thumbnail_width": width,
        "thumbnail_height": height,
        "generated_at": record.get("generated_at"),
        # 当前这张封面是从哪儿来的：``scene`` 是从片子里挑的，``drawn`` 是
        # 重新画的。界面要说出来——"这张图是片子里的第几段"是用户下一步
        # 想换的时候唯一需要知道的事。
        "thumbnail_from": str(record.get("thumbnail_from") or ""),
        "thumbnail_scene": record.get("thumbnail_scene"),
        # 可以拿来当封面的画面。封面本来就该是片子里的一个画面，所以这份
        # 清单是这个面板的主路径，重新画一张是备选。
        "scenes": _choosable_scenes(task_id, endpoint),
        # 压在封面上的那行字，以及它能用的字体和位置。界面不写死这两张表：
        # 装着哪些字体只有服务端知道。
        "text": text_style(task_id),
        "text_fonts": scene_editor.available_fonts(),
        "text_positions": list(thumbnail_text.TEXT_POSITIONS),
        "text_max": thumbnail_text.MAX_TEXT_LENGTH,
        "has_picture": _has_picture(task_id),
        # ``fallback`` 表示这份文案不是模型写的。界面据此说出来，而不是让
        # 用户以为模型就这个水平。
        "text_source": str(record.get("text_source") or ""),
        "text_error": str(record.get("text_error") or ""),
        "thumbnail_generated_at": record.get("thumbnail_generated_at"),
        "title_max": spec["title_max"],
        "description_max": spec["description_max"],
        "tag_count": spec["tag_count"],
        "prompt_max": llm.MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH,
        # 文案是从这两样写出来的。界面用它说清楚"没有脚本时文案会很单薄"。
        "has_subject": bool(subject),
        "has_script": bool(script),
        # 这台机器上配了文生图没有。没配就别让用户按下那个按钮再看报错。
        "can_draw_thumbnail": material.is_openai_image_enabled(),
    }


def _base_file(task_id: str) -> str:
    return os.path.join(utils.task_dir(task_id), BASE_FILENAME)


def _has_picture(task_id: str) -> bool:
    """有没有一张可以压字的图。

    成品也算：压字是后加的功能，早先的成品上没有字，``_compose`` 会把它收养
    成 base。只看 base 的话，那些封面会被报成"还没有图"，而它明明就在面板上
    显示着。
    """
    if os.path.isfile(_base_file(task_id)):
        return True
    return os.path.isfile(os.path.join(utils.task_dir(task_id), THUMBNAIL_FILENAME))


def _default_font(task_id: str) -> str:
    """封面文字默认用这条片子字幕的那一款字。

    封面和片子出自同一条片子，用同一款字看起来才像一套；而且那一款用户已经
    为这条片子挑过了，不用再挑一次。读不出来时让界面自己挑一个。
    """
    try:
        params = scene_editor.load_params(task_id)
    except SceneError:
        return ""
    return str(getattr(params, "font_name", "") or "")


def text_style(task_id: str) -> dict:
    """封面文字的样式。没设置过就给一份默认的，文字预填这条片子的标题。

    预填标题是因为用户要的几乎总是它——"加点字"这件事，九成就是把标题压上
    去。预填而不是直接画上去：封面上的字往往要比标题更短更狠,所以它只是个
    起点。
    """
    record = _record(task_id)
    stored = record.get("thumbnail_text")
    style = thumbnail_text.normalise(
        stored if isinstance(stored, dict) else None,
        font_name=_default_font(task_id),
        known_fonts=scene_editor.available_fonts(),
    )
    if not isinstance(stored, dict):
        style["text"] = str(record.get("title") or "")
    return style


def _compose(task_id: str, record: dict) -> dict:
    """从 base 重新合成成品。base 不在就什么都不做。

    合成失败不能把已经存下来的选择回滚掉——那张 base 是真的在磁盘上,用户
    挑的那一段画面也确实生效了,错的只是那几个字。所以这里把错误原样抛出去
    让调用方点名,而不是假装没挑过。
    """
    base = _base_file(task_id)
    if not os.path.isfile(base):
        # 压字这件事是后加的，所以磁盘上存在"有成品、没有 base"的封面。那种
        # 成品上本来就没有字（那时还不能加字），所以它本身就是一张合格的
        # base——收养它，而不是让用户重新挑一次图。
        existing = os.path.join(utils.task_dir(task_id), THUMBNAIL_FILENAME)
        if os.path.isfile(existing):
            shutil.copyfile(existing, base)
            logger.info(f"adopted the existing thumbnail of {task_id} as its picture")
        else:
            raise SceneError("there is no thumbnail picture yet; choose one first")
    target = os.path.join(utils.task_dir(task_id), THUMBNAIL_FILENAME)
    style = thumbnail_text.normalise(
        record.get("thumbnail_text"),
        font_name=_default_font(task_id),
        known_fonts=scene_editor.available_fonts(),
    )
    try:
        return thumbnail_text.render(base, target, style)
    except thumbnail_text.ThumbnailTextError as exc:
        raise SceneError(str(exc)) from exc


def set_text(task_id: str, wanted: Any, *, endpoint: str = "") -> dict:
    """改封面上的那行字，并立刻重新合成成品。

    只重画文字，不碰那张图：改一个词不该重跑一次文生图，也不该重新取一次帧。
    """
    record = _record(task_id)
    try:
        style = thumbnail_text.normalise(
            wanted,
            font_name=_default_font(task_id),
            known_fonts=scene_editor.available_fonts(),
        )
    except thumbnail_text.ThumbnailTextError as exc:
        raise SceneError(str(exc)) from exc
    record["thumbnail_text"] = style
    _save(task_id, record)
    # 还没挑过图就只存样式，不报错：用户完全可能先写字再挑图。
    #
    # 这里必须问 ``_has_picture`` 而不是只看 base 在不在：压字之前就存在的
    # 那些封面只有成品、没有 base，而收养成品那一步在 ``_compose`` 里面。
    # 只看 base 的话，这个判断对那些封面永远是假，字就被存下来却从来没画上去。
    if _has_picture(task_id):
        _compose(task_id, record)
        record["thumbnail"] = THUMBNAIL_FILENAME
        record["thumbnail_generated_at"] = time.time()
        _save(task_id, record)
    return describe(task_id, endpoint)


def _choosable_scenes(task_id: str, endpoint: str = "") -> list[dict]:
    """能当封面用的那些画面。

    只给下标、地址和关键词：面板上要画的是一排可点的小图，不需要时间线那
    一整套（``scene_editor.describe`` 会重新配对旁白、规划每段的起止，
    为了一排缩略图付那个代价不值得）。

    取不到地址的那些直接不列出来——列一个点下去必然失败的格子，比不列更糟。
    """
    try:
        scenes = scene_manifest.load(task_id)["scenes"]
    except SceneError as exc:
        logger.warning(f"cannot list the scenes of {task_id}: {exc}")
        return []

    choosable = []
    for scene in scenes:
        url = scene_editor.media_url(task_id, scene.get("material"), endpoint)
        if not url:
            continue
        choosable.append(
            {
                "index": scene["index"],
                "url": url,
                "keyword": str(scene.get("keyword") or ""),
                "kind": str(scene.get("kind") or ""),
            }
        )
    return choosable


def use_scene(
    task_id: str, index: Any, *, at: float | None = None, endpoint: str = ""
) -> dict:
    """拿这一段画面当封面。

    这是这个面板的主路径。封面本该是片子里的一个画面：那些图已经在磁盘上
    了，不用再跑一次文生图，不用等本机那几分钟，也不会出现一张和成片对不上
    的封面——而重新画一张最容易出的毛病恰恰是这个。

    画幅照旧由这条片子决定：素材本来就是按成片画幅出的，所以 9:16 的片子
    裁出来的是一张竖封面，16:9 的是横的。
    """
    aspect = aspect_of(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    if not scenes:
        raise SceneError("this task has no scenes to take a picture from")
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise SceneError(f"{index!r} is not a scene number") from exc
    if position < 0 or position >= len(scenes):
        raise SceneError(
            f"there is no scene {position}; this video has {len(scenes)} scenes"
        )

    scene = scenes[position]
    source = scene_manifest.resolve_material(task_id, scene.get("material"))
    try:
        width, height = material.thumbnail_from_material(
            source, VideoAspect(aspect), _base_file(task_id), at=float(at or 0.0)
        )
    except material.ThumbnailImageError as exc:
        raise SceneError(str(exc)) from exc

    record = _record(task_id)
    # 换图保留那几个字：调好的文字不该因为换了一张底图就丢掉。
    _compose(task_id, record)
    record["thumbnail"] = THUMBNAIL_FILENAME
    record["thumbnail_width"] = width
    record["thumbnail_height"] = height
    record["thumbnail_generated_at"] = time.time()
    record["thumbnail_from"] = "scene"
    record["thumbnail_scene"] = position
    record["aspect"] = aspect
    _save(task_id, record)
    logger.success(
        f"thumbnail for task {task_id} taken from scene {position}: "
        f"{width}x{height} ({aspect})"
    )
    return describe(task_id, endpoint)


def generate_text(task_id: str, *, language: str = "auto", endpoint: str = "") -> dict:
    """让模型按这条片子的画幅写一份标题、简介、标签和封面说明。

    覆盖已经存下来的那一份。生成是用户按按钮的结果，而"再写一版"正是那个
    按钮的用途；改过的内容想留住就按保存，和脚本面板一样。
    """
    aspect = aspect_of(task_id)
    subject, script = _subject_and_script(task_id)
    if not subject and not script:
        raise SceneError(
            "this task has neither a subject nor a script, so there is nothing "
            "to write a title from"
        )
    metadata = llm.generate_youtube_metadata(
        video_subject=subject,
        video_script=script,
        aspect=aspect,
        language=language,
    )
    record = _record(task_id)
    record.update(
        {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "thumbnail_prompt": metadata["thumbnail_prompt"],
            "aspect": aspect,
            "language": language,
            "generated_at": time.time(),
            # 这一份是模型写的还是降级拼的，连同降级的原因一起存下来。存而
            # 不只是返回：这份草稿在磁盘上会一直是这一份，明天重新打开面板
            # 时它仍然很差，那时也该说清楚为什么。
            "text_source": str(metadata.get("source") or "model"),
            "text_error": str(metadata.get("error") or ""),
        }
    )
    _save(task_id, record)
    return describe(task_id, endpoint)


def _clean_tags(raw: Any, limit: int) -> list[str]:
    """标签：去掉 ``#``、去重、按 YouTube 的总长度截断。"""
    if isinstance(raw, str):
        candidates = [part for part in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(part) for part in raw]
    else:
        raise SceneError("tags must be a list of words or a comma separated line")

    tags: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in candidates:
        tag = " ".join(str(item).replace("#", "").split())[:MAX_TAG_LENGTH]
        if not tag or tag.lower() in seen:
            continue
        # 总长度到了就停下，不留半个词。
        if total + len(tag) > MAX_TAGS_LENGTH:
            break
        seen.add(tag.lower())
        tags.append(tag)
        total += len(tag) + 1
        if limit and len(tags) >= limit:
            break
    return tags


def set_metadata(
    task_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: Any = None,
    thumbnail_prompt: str | None = None,
    endpoint: str = "",
) -> dict:
    """存下用户编辑过的文案。只提交想改的字段。

    超长一律报错，不悄悄截断：标题被砍掉半句而界面不说，用户会把它当成自己
    写的那一句发出去。
    """
    aspect = aspect_of(task_id)
    spec = llm.youtube_format(aspect)
    record = _record(task_id)

    if title is not None:
        text = str(title).strip()
        if len(text) > spec["title_max"]:
            raise SceneError(
                f"the title is {len(text)} characters; YouTube allows "
                f"{spec['title_max']}"
            )
        record["title"] = text
    if description is not None:
        text = str(description)
        if len(text) > spec["description_max"]:
            raise SceneError(
                f"the description is {len(text)} characters; YouTube allows "
                f"{spec['description_max']}"
            )
        record["description"] = text
    if tags is not None:
        record["tags"] = _clean_tags(tags, spec["tag_count"])
    if thumbnail_prompt is not None:
        text = str(thumbnail_prompt).strip()
        if len(text) > llm.MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH:
            raise SceneError(
                f"the thumbnail description is {len(text)} characters; keep it "
                f"under {llm.MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH}"
            )
        record["thumbnail_prompt"] = text

    record["aspect"] = aspect
    _save(task_id, record)
    return describe(task_id, endpoint)


def generate_thumbnail(
    task_id: str, *, prompt: str | None = None, endpoint: str = ""
) -> dict:
    """按这条片子的画幅画一张封面。

    ``prompt`` 不给就用存着的那一段说明；它也没有的话就报错点名——拿主题
    原样去出图得到的是一张"插画化的句子"，不是一张封面，而用户会以为这个
    功能就是这么差。先按"生成文案"，那一步会把封面说明一起写出来。
    """
    aspect = aspect_of(task_id)
    record = _record(task_id)
    text = str(
        prompt if prompt is not None else record.get("thumbnail_prompt") or ""
    ).strip()
    if not text:
        raise SceneError(
            "there is no thumbnail description yet; generate the title and "
            "description first, or write one yourself"
        )
    if len(text) > llm.MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH:
        raise SceneError(
            f"the thumbnail description is {len(text)} characters; keep it "
            f"under {llm.MAX_YOUTUBE_THUMBNAIL_PROMPT_LENGTH}"
        )

    try:
        width, height = material.generate_thumbnail_image(
            text, VideoAspect(aspect), _base_file(task_id)
        )
    except material.ThumbnailImageError as exc:
        # 供应商失败点名是谁失败了，不静默回落到"用片中的一帧"：那是另一个
        # 决定，该由用户做。
        raise SceneError(f"the thumbnail could not be drawn: {exc}") from exc
    except ValueError as exc:
        # ``_openai_image_endpoint`` 在没配端点或模型时抛的就是这个，文案里
        # 已经写明了要去 config.toml 填什么。
        raise SceneError(str(exc)) from exc

    _compose(task_id, record)
    record["thumbnail"] = THUMBNAIL_FILENAME
    record["thumbnail_prompt"] = text
    record["thumbnail_width"] = width
    record["thumbnail_height"] = height
    record["thumbnail_generated_at"] = time.time()
    record["thumbnail_from"] = "drawn"
    record.pop("thumbnail_scene", None)
    record["aspect"] = aspect
    _save(task_id, record)
    logger.success(
        f"thumbnail for task {task_id} drawn at {width}x{height} ({aspect})"
    )
    return describe(task_id, endpoint)
