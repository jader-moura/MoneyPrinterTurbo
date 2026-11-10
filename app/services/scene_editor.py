"""审片期间对单个场景的编辑操作：重新生成、替换、改台词、重排、删除。

Why this is not in ``scene_manifest``
------------------------------------
``scene_manifest`` 被 ``task`` 导入,所以它不能反过来导入 ``task`` 或 ``material``。
编辑操作需要真正去调供应商和读任务参数,因此单独成模块：依赖方向保持
``task -> scene_manifest`` 和 ``scene_editor -> {task, material, scene_manifest}``,
不出现环。

``scene_manifest`` is imported by ``task``, so it cannot import ``task`` or
``material`` back. These operations do call providers and read task params, so
they live here instead: the dependency direction stays
``task -> scene_manifest`` and ``scene_editor -> {task, material, scene_manifest}``.

Why regeneration goes through ``material.download_videos``
----------------------------------------------------------
每个素材源的差异已经在 ``download_videos`` 里收敛过一次了。重新生成一个场景
只要把 ``search_terms`` 收窄成一个关键词、把目标时长收窄成一段片长,就能对
所有素材源复用同一条路径,而不是为 openai_image 单独写一份、再为 pexels 写
一份。``get_video_materials`` 则是整批规划,不能用来做单条重生成。

Every source's differences are already reconciled inside ``download_videos``.
Regenerating one scene narrows ``search_terms`` to a single keyword and the
target duration to one clip, which reuses that single path for every source
instead of special-casing each one. ``get_video_materials`` plans the whole set
at once and is the wrong entry point for a single scene.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from loguru import logger

from app.models import image_style as image_style_model
from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import material, scene_manifest, task_artifacts, timeline
from app.services import bgm, transition_sound
from app.utils import subtitle_reader, subtitle_writer
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

SceneError = scene_manifest.SceneError


class SceneBusyError(SceneError):
    """任务正在渲染或发布，此时编辑场景会和渲染读到的文件打架。"""


# 这些源不是按关键词取素材的，重新生成一个场景没有意义。
# These sources are not keyword-driven, so regenerating one scene is meaningless.
_UNSUPPORTED_REGEN_SOURCES = ("local", "loomloom")


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _editing(task_id: str) -> Iterator[None]:
    """把"读清单 → 改 → 写回"圈成一个不可分割的动作。

    为什么现在需要：重生成排了队之后，写清单的不只有正在处理的那个 HTTP
    请求了——后台那个工作线程出完一张图就会写一次。两边都是"先读整份清单，
    改掉一段，再整份写回去",交错起来后写的那次会把先写的那次完全盖掉:
    用户在等图的时候改了一句台词,图出来的瞬间那句台词就没了。

    锁按任务分：两个任务互不相干，没有理由互相等。

    注意**不要**把出图圈进来。出图要几分钟，拿着锁出图等于审片界面在这几
    分钟里全部卡住,而排队的全部意义正是让用户能接着干别的。所以慢的那一段
    在锁外面跑,跑完重新读一遍清单再写——这也是 ``_locate_scene`` 存在的原因。

    Both the request thread and the regeneration worker read the whole
    manifest, change one scene and write it all back, so without this the
    later write silently discards the earlier one -- a narration edited while
    a picture was being generated would vanish the moment it finished. The
    lock is per task, and deliberately does not cover generation itself:
    holding it for minutes would freeze the editor, which is exactly what the
    queue is meant to prevent.
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.get(task_id)
        if lock is None:
            # 可重入：下面好几个操作会互相调用（比如重排之后重新读一遍），
            # 普通锁会把自己锁死。
            lock = threading.RLock()
            _LOCKS[task_id] = lock
    with lock:
        yield


def _require_idle(task_id: str) -> None:
    task = sm.state.get_task(task_id)
    if task and tm.is_task_busy(task):
        raise SceneBusyError(
            "this task is still running; wait for it to finish before editing scenes"
        )


def load_params(task_id: str) -> VideoParams:
    """从任务清单读回参数。

    清单是审片唯一可靠的参数来源：任务状态里不保存 params,而 WebUI 的
    session 在浏览器刷新后就没了。未知字段一律丢弃,因为清单可能是旧版本
    写的,不能让一个历史字段让整个审片界面打不开。
    """
    payload = scene_manifest.load_raw(task_id)
    raw = payload.get("params")
    if not isinstance(raw, dict):
        raise SceneError(
            "this task has no saved parameters; it cannot be re-rendered"
        )
    allowed = set(VideoParams.model_fields)
    cleaned = {key: value for key, value in raw.items() if key in allowed}
    try:
        return VideoParams(**cleaned)
    except Exception as exc:
        raise SceneError(f"saved parameters are not usable: {exc}") from exc


def _scenes_or_fail(task_id: str) -> list[dict]:
    scenes = scene_manifest.load(task_id)["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )
    return scenes


def _scene_at(scenes: list[dict], index: Any) -> int:
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise SceneError(f"scene index must be a whole number, got {index!r}") from exc
    if not 0 <= position < len(scenes):
        raise SceneError(
            f"scene {position} does not exist; this task has {len(scenes)} scenes"
        )
    return position


def _locate_scene(scenes: list[dict], position: int, anchor: str) -> int:
    """出图期间时间线可能变了，把用户当时点的那一段找回来。

    按素材文件认，不按下标认：排队的那几分钟里用户可能删掉、插入或者重排
    了场景，下标就指向另一段画面了。把新图写到一段用户没动过的画面上，比
    这次重生成直接失败要糟得多——他会以为那段画面自己坏了。

    那一段真的不在了（被删了）就点名说清楚。生成出来的文件留在磁盘上没人
    用，这不好看，但比悄悄塞给另一段画面好。

    Scene indices shift when the timeline is edited while a job waits, so the
    scene is found again by the material file it had. Writing the new picture
    onto a scene the user never touched would be worse than failing.
    """
    if not anchor:
        return _scene_at(scenes, position)
    if (
        0 <= position < len(scenes)
        and str(scenes[position].get("material") or "") == anchor
    ):
        return position
    for candidate, scene in enumerate(scenes):
        if str(scene.get("material") or "") == anchor:
            return candidate
    raise SceneError(
        "the scene this picture was generated for is no longer in the timeline; "
        "ask for it again if you still want it"
    )


def check_regenerate(
    task_id: str,
    index: Any,
    *,
    keyword: str | None = None,
    image_style: str | None = None,
) -> dict:
    """能不能重生成这一段；能的话返回执行它需要的东西。

    为排队而拆出来的：这些失败改请求才能解决（任务在渲染中、下标越界、这个
    素材源不按关键词出图、风格 id 不认识），所以必须在**排队的那一刻**就说，
    而不是等队列轮到它、几分钟之后再冒出来——那时候用户只会以为是出图失败。

    返回的 ``material`` 是这一段现在用的素材文件，排队期间时间线变了就靠它
    把这一段找回来（见 ``_locate_scene``）。

    Split out for the queue: these failures are fixed by a different request,
    so they must be reported when the job is accepted rather than minutes
    later, where they would look like a generator failure.
    """
    _require_idle(task_id)
    params = load_params(task_id)
    source = str(getattr(params, "video_source", "") or "")
    if source in _UNSUPPORTED_REGEN_SOURCES:
        raise SceneError(
            f"video source {source!r} does not generate materials from keywords; "
            "replace this scene with your own file instead"
        )
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = dict(scenes[position])
    term = str(keyword if keyword is not None else scene.get("keyword") or "").strip()
    if not term:
        term = suggested_keyword(task_id, position)
    if not term:
        raise SceneError("no keyword to generate from; provide one with the request")
    # 风格 id 认不认识现在就说。不传就按这一段记着的那个风格验——真正出图时
    # 用的也是它。
    effective_style = (
        image_style if image_style is not None else str(scene.get("image_style") or "")
    )
    _image_prompt_template(params, effective_style)
    return {
        "index": position,
        "keyword": term,
        "material": str(scene.get("material") or ""),
    }


def regenerate_material(
    task_id: str,
    index: Any,
    *,
    keyword: str | None = None,
    image_style: str | None = None,
    expect_material: str | None = None,
) -> dict:
    """重新取这一个场景的素材。

    ``keyword`` 给了就同时改写场景关键词——用户看到画面不对时,改的往往正是
    关键词,所以这两件事应该一次做完,而不是让用户先改关键词再点一次重生成。

    ``image_style`` 给了就只给这一张换个画风;不给就用任务自己记下的那个,
    也就是出片时真正用的那个。换过的风格记在场景上,界面下次打开还显示它,
    用户不用每次重新挑。

    对库存素材源（Pexels 等）有一个已知限制：搜索结果有缓存,同一个关键词
    重新生成很可能拿回同一个文件。本地出图的源每次都是新的采样,所以重生成
    确实会得到不同的画面。这里不绕过缓存——绕过等于让用户在不知情的情况下
    多花一次第三方额度。

    ``expect_material`` 给的是排队时这一段用的素材文件。队列里排着的活执行
    时下标可能已经指向别的画面了（用户删了、插了、重排了），给了它就按文件
    把那一段找回来。见 ``_locate_scene``。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]
        term = str(
            keyword if keyword is not None else scene.get("keyword") or ""
        ).strip()
        # 三种情况要分开，否则界面上那个下拉框会说谎。
        #
        # 不传（``None``）：沿用这一段上次挑过的风格。不这样的话，在编辑器里
        # 把某一段改成漫画风，下一次重生成又会悄悄变回去。老版客户端和不画
        # 下拉框的界面走的都是这条路。
        # 传空字符串：用户在下拉框里选了"和这条片子一样"，那就回到任务记下
        # 的风格，并且把这一段上记着的那个选择清掉——留着它，下次打开界面
        # 显示的还是旧选择，而画出来的已经不是它了。
        # 传风格 id：只给这一张换画风，并记住它。
        if image_style is None:
            style = str(scene.get("image_style") or "").strip()
        else:
            style = str(image_style).strip()
        anchor = str(expect_material or scene.get("material") or "")

    # 出图在锁外面。本机一张图要几分钟，拿着锁出图等于这几分钟里这个任务
    # 什么都改不了。
    new_path, source = _produce_material(
        task_id, term, what=f"scene {position}", image_style=style
    )

    # 重新读一遍清单：刚才那几分钟里用户完全可能改过别的场景，拿出图之前
    # 那份内存里的旧清单写回去会把那些改动全盖掉。
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _locate_scene(scenes, position, anchor)
        scene = scenes[position]
        if image_style is not None:
            if style:
                scene["image_style"] = style
            else:
                scene.pop("image_style", None)
        scene["material"] = new_path
        scene["kind"] = scene_manifest.material_kind(new_path)
        scene["source"] = source
        scene["keyword"] = term
        # 用户亲手重生成过的结果，不应该被后续批量操作再覆盖掉。
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        return scene


def _image_prompt_template(params: VideoParams, image_style: str | None) -> str:
    """这一次出图要用的画风模板。

    不指定风格就用任务自己记下的那个——出片时真正用的那个,而不是
    config.toml "现在"的值。审片的编辑器跑在 API 进程里,那份配置和 WebUI
    的不是同一份内存,也不会跟着 WebUI 的改动更新,漫画风的片子因此补出过
    一张油画。

    未知的风格 id 直接报错,不静默回退成别的画风：悄悄换一种风格正是这里
    要修的毛病。
    """
    requested = str(image_style or "").strip()
    if not requested:
        return str(getattr(params, "image_prompt_template", "") or "")
    template = image_style_model.template_for_style(requested)
    if not template:
        raise SceneError(
            f"unknown image style {requested!r}; choose one of: "
            + ", ".join(image_style_model.image_style_ids())
        )
    return template


def _produce_material(
    task_id: str,
    term: str,
    *,
    what: str,
    advice: str = "replace this scene with your own file instead",
    image_style: str | None = None,
) -> tuple[str, str]:
    """按关键词取一份素材，返回 ``(文件路径, 素材源)``。

    重生成一段画面和新增一段画面要走完全相同的取材路径，否则两者会在
    画幅、片长、图片动效上慢慢长出差异,最后拼进同一条片子里对不上。
    """
    params = load_params(task_id)
    source = str(getattr(params, "video_source", "") or "")
    if source in _UNSUPPORTED_REGEN_SOURCES:
        raise SceneError(
            f"video source {source!r} does not generate materials from keywords; "
            f"{advice}"
        )
    if not term:
        raise SceneError(
            "no keyword to generate from; provide one with the request"
        )

    clip_duration = int(getattr(params, "video_clip_duration", 5) or 5)
    prompt_template = _image_prompt_template(params, image_style)
    logger.info(
        f"generating {what} of task {task_id} "
        f"from source {source!r} with keyword {term!r}"
    )
    try:
        produced = material.download_videos(
            task_id=task_id,
            search_terms=[term],
            source=source,
            video_aspect=VideoAspect(
                getattr(params, "video_aspect", None) or VideoAspect.portrait
            ),
            # 顺序模式让它按关键词顺序逐个取，单关键词时就是"取一个"。
            video_concat_mode=VideoConcatMode.sequential,
            # 只要覆盖一段片长，避免为一个场景取回一整批素材。
            audio_duration=float(clip_duration),
            max_clip_duration=clip_duration,
            match_script_order=True,
            image_motion=getattr(params, "image_motion", None) or "zoom",
            # 画风跟着任务走（或者跟着用户这次挑的走），不跟着全局配置走。
            image_prompt_template=prompt_template,
        )
    except Exception as exc:
        # 供应商失败必须点名是谁失败了，不能静默换源，也不能留下半条清单。
        raise SceneError(f"{source} failed to produce a material: {exc}") from exc

    if not produced:
        raise SceneError(f"{source} returned no material for {term!r}")
    return str(produced[0]), source


def add_scene(
    task_id: str,
    *,
    keyword: str,
    narration: str = "",
    after: Any = None,
    image_style: str | None = None,
) -> dict:
    """按关键词生成一段新画面，插进时间线。

    为什么需要它：素材张数由关键词个数决定，不由旁白长度决定。画面比旁白短时
    "把每段停得更久"(fit)只是答案之一,另一个答案是"多补几张画面"——而在这之前
    时间线只能改已有的那些,补不了新的。

    ``after`` 不给就追加到末尾,这也是界面上那个加号的行为。新场景的台词默认
    为空:这里只补画面,不动旁白,否则会悄悄让已经录好的配音失效。

    ``image_style`` 不给就跟着任务记下的风格,补出来的画面和片子里原有的是
    同一种风格——补画面的人想要的本来就是"再来一张一样的",而不是一张漫画
    片子里突然插进来的油画。给了就按它来,并记在这一段上,之后重生成这一段
    还是这个风格。
    """
    _require_idle(task_id)
    term = str(keyword or "").strip()
    style = str(image_style or "").strip()
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        # 插在哪一段后面，同样按素材文件认：出图这几分钟里时间线可能变了，
        # 光记一个下标会把新画面插到另一个位置去。
        after_position = None if after is None else _scene_at(scenes, after)
        anchor = (
            ""
            if after_position is None
            else str(scenes[after_position].get("material") or "")
        )

    # 和重生成一样，慢的那一段不拿锁。
    new_path, source = _produce_material(
        task_id,
        term,
        what="a new scene",
        advice="add your own file as a scene instead",
        image_style=style,
    )

    scene = {
        "index": 0,  # save_scenes 会按位置重排
        "keyword": term,
        "narration": str(narration or ""),
        "material": new_path,
        "kind": scene_manifest.material_kind(new_path),
        "source": source,
        "locked": True,
    }
    if style:
        scene["image_style"] = style
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        position = (
            len(scenes)
            if after_position is None
            else _locate_scene(scenes, after_position, anchor) + 1
        )
        scenes.insert(position, scene)
        scene_manifest.save_scenes(task_id, scenes)
        logger.success(
            f"added scene at position {position} of task {task_id} "
            f"with keyword {term!r}"
        )
        return scene_manifest.load(task_id)["scenes"][position]


def replace_material(task_id: str, index: Any, material_path: str) -> dict:
    """用一个已经通过上传校验的文件替换场景素材。"""
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)

        # 上传目录和任务目录之外的路径一律拒绝，哪怕它确实存在。
        resolved = scene_manifest.resolve_material(task_id, material_path)
        resolved = _adopt_into_task(task_id, resolved)
        scene = scenes[position]
        scene["material"] = resolved
        scene["kind"] = scene_manifest.material_kind(resolved)
        scene["source"] = "local"
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        return scene


def _adopt_into_task(task_id: str, source: str) -> str:
    """把任务目录之外的替换素材复制进任务目录。

    两个理由。一是 ``/stream`` 只服务任务目录，留在公共上传目录里的文件无法
    通过接口播放,审片界面上就会显示成"素材缺失"。二是任务从此自给自足：上传
    目录被清理或那个文件被别的任务替换掉,都不会让这条已经审过的片子渲染失败。

    代价是一次文件复制。重名时加序号,不覆盖任何已有文件。
    """
    task_directory = os.path.realpath(utils.task_dir(task_id))
    if os.path.dirname(os.path.realpath(source)) == task_directory:
        return source

    stem, suffix = os.path.splitext(os.path.basename(source))
    for attempt in range(1000):
        name = f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt}{suffix}"
        target = os.path.join(task_directory, name)
        if not os.path.exists(target):
            try:
                shutil.copyfile(source, target)
            except OSError as exc:
                raise SceneError(f"failed to copy the replacement material: {exc}") from exc
            logger.info(f"adopted replacement material into task {task_id}: {name}")
            return target
    raise SceneError("too many replacement materials with the same name")


def update_scene(
    task_id: str,
    index: Any,
    *,
    narration: str | None = None,
    keyword: str | None = None,
    locked: bool | None = None,
) -> dict:
    """改台词、关键词或锁定状态。

    改台词会让配音和字幕同时失效：配音是按整段脚本合成的,改一句话之后旧
    配音的时间轴对不上任何字幕。这里只打标记,不自动重新配音——重新配音会
    重跑一次 TTS 并作废全部字幕时间,那应该是用户按下按钮的结果,不是改一个
    文本框的副作用。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]

        narration_changed = False
        if narration is not None:
            text = str(narration).strip()
            # 和界面上现在显示的那段比,而不是和存着的比。界面显示的是按时间
            # 算出来的台词,存着的往往是空的——照存着的比,用户只要按一下"保存
            # 文字"而什么都没改,也会被当成改了台词,配音白白作废一次。
            shown = narration_for_scenes(task_id)
            current = (
                shown[position]
                if position < len(shown)
                else str(scene.get("narration") or "")
            )
            narration_changed = text != current
            if narration_changed:
                scene["narration"] = text
                # 标成手改过,重算时不要盖掉他写的字。清空也算改过——那是"重新
                # 配音时别念这几句"的意思,不是"交回去重新算一遍",否则刚删掉
                # 的句子下一次读清单又回来了。
                scene["narration_edited"] = True
        if keyword is not None:
            scene["keyword"] = str(keyword).strip()
        if locked is not None:
            scene["locked"] = bool(locked)

        extra: dict[str, Any] = {}
        if narration_changed:
            extra["audio_stale"] = True
        scene_manifest.save_scenes(task_id, scenes, **extra)
        return scene


def delete_scene(task_id: str, index: Any) -> list[dict]:
    """删除一个场景。素材文件留在磁盘上，不删。

    不删文件是有意的：用户可能只是想先去掉一段看看效果,而磁盘上的那张图
    是上一次已经花掉的算力。清理任务目录本来就由删除任务负责。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        if len(scenes) == 1:
            raise SceneError("a video needs at least one scene")
        removed = scenes.pop(position)
        logger.info(
            f"removed scene {position} from task {task_id}: {removed.get('material')}"
        )
        scene_manifest.save_scenes(task_id, scenes)
        return scene_manifest.load(task_id)["scenes"]


def reorder_scenes(task_id: str, order: Sequence[Any]) -> list[dict]:
    """按给定的原始下标顺序重排场景。

    要求是一个完整排列,不接受"只给前几个"：部分顺序会让剩下的场景去哪里
    变成一个需要猜的问题,而猜错的结果是成片里少了一段。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        try:
            positions = [int(value) for value in order]
        except (TypeError, ValueError) as exc:
            raise SceneError("scene order must be a list of whole numbers") from exc

        if sorted(positions) != list(range(len(scenes))):
            raise SceneError(
                f"scene order must be a permutation of 0..{len(scenes) - 1}, "
                f"got {positions}"
            )
        # 画面换位置，台词留在原处。
        #
        # 台词属于时间,不属于图：配音是一整条,顺序没变；字幕按那条配音的时间
        # 轴生成,也没变。把图拖到前面只是让它早点出现,第 30 秒念的还是第 30
        # 秒那句话。文字跟着图走的话,界面会把另一段的台词显示在这张图下面,而
        # "重新配音"读的就是这份列表——按一下就把整条旁白按画面顺序重念了。
        #
        # Reordering pictures must not reorder the words: the voice-over is one
        # take and the subtitles are timed against it, so what is heard at a
        # given second does not change when a picture moves to another slot.
        words = [
            (
                str(scene.get("narration") or ""),
                bool(scene.get("narration_edited")),
            )
            for scene in scenes
        ]
        reordered = [scenes[position] for position in positions]
        for scene, (text, edited) in zip(reordered, words):
            scene["narration"] = text
            if edited:
                scene["narration_edited"] = True
            else:
                scene.pop("narration_edited", None)
        scene_manifest.save_scenes(task_id, reordered)
        return scene_manifest.load(task_id)["scenes"]


def set_locked(task_id: str, index: Any, locked: bool) -> dict:
    return update_scene(task_id, index, locked=locked)


def media_url(task_id: str, path: Any, endpoint: str = "") -> str:
    """返回场景素材的可播放地址，无法解析时返回空字符串。"""
    relative = scene_manifest.relative_media_path(task_id, path)
    if not relative:
        return ""
    base = (endpoint or "").rstrip("/")
    return f"{base}/api/v1/stream/{relative}" if base else f"/api/v1/stream/{relative}"


def _enum_value(value: Any) -> str:
    """把 ``(str, Enum)`` 成员和裸字符串统一成界面能直接显示的字符串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def planned_windows(
    task_id: str, manifest: dict, params=None, *, probe: bool = True
) -> list[dict]:
    """场景在成片里各自占的时间段，渲染器用的就是这组数字。

    ``probe`` 关掉时按请求的单段时长估算，不启动解码器——审片列表每次页面
    重跑都要算一遍，不该为此多付十几次解码。
    """
    scenes = manifest.get("scenes") or []
    if not scenes:
        return []
    if params is None:
        params = load_params(task_id)
    return timeline.plan_windows(
        task_id,
        scenes,
        audio_duration=manifest.get("audio_duration") or 0.0,
        clip_speed=getattr(params, "video_clip_speed", 1.0) or 1.0,
        clip_duration=getattr(params, "video_clip_duration", None),
        probe=probe,
    )


def _narration_by_time(
    task_id: str,
    manifest: dict,
    windows: list[dict] | None = None,
) -> list[str]:
    """按时间把旁白分到每一段画面上：这段画面在屏幕上时真正会念到的那几句。

    画面铺不满旁白时整组会轮播,所以要按轮播后的真实时间线分配,再把同一个
    场景的几次出现合回一条。只按第一轮分配的话,第一轮之后的每一句都会堆到
    最后一个场景上。

    改过脚本还没重新配音时不看字幕：那份字幕还是旧词的时间轴,照它分配会把
    用户刚写下的句子显示成改之前的样子。
    """
    scenes = manifest["scenes"]
    if not scenes:
        return []
    if windows is None:
        windows = planned_windows(task_id, manifest, probe=False)
    if len(windows) != len(scenes):
        return ["" for _ in scenes]
    lines = scene_manifest.split_narration(manifest.get("script"))
    segments = (
        []
        if manifest.get("audio_stale")
        else scene_manifest.narration_segments(task_id)
    )
    if not lines and not segments:
        return ["" for _ in scenes]
    cycled = timeline.cycle_windows(
        windows, float(manifest.get("audio_duration") or 0.0)
    )
    blocks = scene_manifest.distribute_narration(
        [{"start": item["start"], "end": item["end"]} for item in cycled],
        segments=segments,
        lines=lines,
    )
    texts = ["" for _ in scenes]
    for item, block in zip(cycled, blocks):
        if not block:
            continue
        index = item["scene"]
        texts[index] = "\n".join(filter(None, (texts[index], block))).strip()
    return texts


def narration_for_scenes(
    task_id: str,
    manifest: dict | None = None,
    windows: list[dict] | None = None,
) -> list[str]:
    """每一段画面该显示的台词。

    台词不属于某一张图,属于某一段时间：配音是一整条,字幕是按那条配音的时
    间轴生成的,所以"这段画面上会念什么"只能由它在成片里的起止时间决定。把
    文字存在场景上只是个缓存——画面一重排、一删、一改时长,缓存就过期了,而
    过期的缓存会让界面显示另一段的台词。

    Narration belongs to a stretch of time, not to a picture: the voice-over
    is one take and the subtitles are timed against it, so what is said over
    a picture follows from where that picture sits in the finished video.
    Recomputing it on every read is what keeps the editor honest after a
    reorder, a delete or a timing change.

    用户亲手改过的那一段例外,用他写的字：那不是缓存,那是他的输入。
    """
    manifest = manifest if manifest is not None else scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    stored = [str(scene.get("narration") or "") for scene in scenes]
    try:
        derived = _narration_by_time(task_id, manifest, windows)
    except scene_manifest.SceneError as exc:
        # 排不出时间线（参数读不回来之类）时宁可显示存着的旧文字，也不要让
        # 审片界面打不开。
        logger.warning(f"cannot pair narration by time for {task_id}: {exc}")
        return stored
    if len(derived) != len(stored):
        return stored
    return [
        stored[position]
        if scenes[position].get("narration_edited") or not derived[position]
        else derived[position]
        for position in range(len(scenes))
    ]


def narration_script(task_id: str) -> str:
    """重新配音要念的脚本,顺序与画面顺序无关。

    不把场景台词按场景顺序拼起来：场景顺序是画面顺序。用户把第 7 张图拖到
    最前面,是想让那张图先出现,不是想让第 7 句话先被念出来——而按场景顺序拼
    接恰恰会那样,再按一下"重新配音"整条旁白就被打乱了,而且没有任何提示。

    按时间分配出来的块本身就是脚本顺序(场景顺序即时间顺序),重排画面动不了
    它。一句都不丢,所以拼回去仍然是完整的脚本。
    """
    manifest = scene_manifest.load(task_id)
    blocks = narration_for_scenes(task_id, manifest)
    script = "\n".join(block.strip() for block in blocks if block.strip())
    # 一句都没排出来（既没有脚本也没有字幕）时退回旧的拼接，总比交一段空
    # 脚本给 TTS 好。
    return script or scene_manifest.narration_script(task_id)


def rewrite_script(task_id: str, script: str) -> str:
    """整条旁白的文字,一次改完。

    台词按时间归到画面上,所以"改第 3 段的词"其实是在改脚本的某一块；想调整
    句子的先后、把一句拆成两句、或者整段重写,都没有哪一段画面是它的归属。
    这里是改词的地方,场景上的那个输入框只是它的局部视图。

    改完配音就不作数了,但不在这里自动重配：重配要跑一次 TTS 并作废全部字幕
    时间,那应该是用户按下按钮的结果,不是存一次文字的副作用。
    """
    _require_idle(task_id)
    text = str(script or "").strip()
    if not text:
        raise SceneError("the narration script cannot be empty")
    with _editing(task_id):
        manifest = scene_manifest.load(task_id)
        if not manifest["scenes"]:
            raise SceneError(f"task {task_id} has no scenes to narrate")
        if text == str(manifest.get("script") or "").strip():
            return text
        if not scene_manifest.patch_script(task_id, text):
            raise SceneError("failed to save the narration script")
        # 脚本就是台词本身了，场景上那些"手改过"的标记跟着作废：留着它们会
        # 让刚写下的句子被旧的局部编辑挡住。
        scenes = manifest["scenes"]
        for scene in scenes:
            scene.pop("narration_edited", None)
            scene["narration"] = ""
        scene_manifest.save_scenes(task_id, scenes, audio_stale=True)
    return text


def realign_narration(task_id: str) -> bool:
    """把场景列表里漏掉的旁白句子按时间补回去，返回是否改动过。

    关键词个数决定画面张数，旁白句数由脚本决定，两者几乎从不相等。按下标
    配对时多出来的句子在界面上直接消失(12 张图配 28 句旁白会丢 16 句),
    而"改台词后重新配音"读的就是这份列表——脚本会被悄悄砍掉一多半。

    这里是幂等的：补过一次之后每条场景台词都是若干句的合并，不再等于脚本
    里的任何单句，``narration_pairing_is_stale`` 随即返回 False。用户手改过
    台词的任务同样返回 False，不会被盖掉。
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    lines = scene_manifest.split_narration(manifest.get("script"))
    if not scene_manifest.narration_pairing_is_stale(scenes, lines):
        return False
    try:
        windows = planned_windows(task_id, manifest, probe=False)
    except scene_manifest.SceneError as exc:
        # 参数读不回来时宁可继续显示旧配对，也不要让审片界面打不开。
        logger.warning(f"cannot re-pair narration for {task_id}: {exc}")
        return False

    # 画面铺不满旁白时整组会轮播，所以要按轮播后的真实时间线分配，再把
    # 同一个场景的几次出现合回一条。只按第一轮分配的话，第一轮之后的每一
    # 句都会堆到最后一个场景上。
    cycled = timeline.cycle_windows(
        windows, float(manifest.get("audio_duration") or 0.0)
    )
    blocks = scene_manifest.distribute_narration(
        [{"start": item["start"], "end": item["end"]} for item in cycled],
        segments=scene_manifest.narration_segments(task_id),
        lines=lines,
    )
    texts = ["" for _ in windows]
    for item, block in zip(cycled, blocks):
        if not block:
            continue
        index = item["scene"]
        texts[index] = "\n".join(filter(None, (texts[index], block))).strip()
    scene_manifest.apply_narration_pairing(task_id, texts)
    logger.info(
        f"re-paired {len(lines)} narration lines across {len(scenes)} scenes "
        f"by on-screen time, task_id: {task_id}"
    )
    return True


def describe(task_id: str, endpoint: str = "", *, with_timing: bool = False) -> dict:
    """审片界面的读模型：场景、可播放地址、以及配音是否已失效。

    ``with_timing`` 打开时额外探测每段素材的真实时长。时间线界面必须知道
    源长度才能画出拖拽边界,但探测要逐个打开解码器,所以默认不做——Phase 1
    的审片列表并不需要它。探测结果按"路径+修改时间+大小"缓存。
    """
    # 先把漏掉的旁白句子按时间补回场景，再读：界面显示的台词必须是这段画面
    # 播放时真正会念的那几句，否则用户会以为渲染器换掉了图片。
    try:
        realign_narration(task_id)
    except Exception as exc:  # pragma: no cover - 补配对失败不该挡住审片
        logger.warning(f"cannot re-pair narration for {task_id}: {exc}")
    manifest = scene_manifest.load(task_id)
    params = None
    try:
        params = load_params(task_id)
    except scene_manifest.SceneError as exc:
        logger.warning(f"cannot read render settings for {task_id}: {exc}")
    windows = []
    if params is not None:
        try:
            windows = planned_windows(
                task_id, manifest, params, probe=with_timing
            )
        except scene_manifest.SceneError as exc:
            logger.warning(f"cannot plan scene timing for {task_id}: {exc}")
    # 台词按时间算,不直接用存着的那一份：存着的只是缓存,画面一重排、一删、
    # 一改时长就过期了。理由见 ``narration_for_scenes``。
    spoken = narration_for_scenes(task_id, manifest, windows or None)
    scenes = []
    for scene in manifest["scenes"]:
        record = dict(scene)
        if scene["index"] < len(spoken):
            record["narration"] = spoken[scene["index"]]
        record["narration_edited"] = bool(scene.get("narration_edited"))
        # 这一段自己挑的转场和运镜。空字符串一律是"跟整条片子一样"，界面
        # 据此把下拉框停在第一项上。
        record["transition"] = transition_id(scene.get("transition"))
        record["image_motion"] = str(scene.get("image_motion") or "")
        record["cut_sound"] = str(scene.get("cut_sound") or "")
        # 关键词是空的那几段：告诉界面"不填的话会拿什么去画"，让它当占位文字
        # 显示出来。空着的关键词原本在重生成那里直接被拒，用户只能逐段手敲。
        if not str(scene.get("keyword") or "").strip():
            line = " ".join(
                str(spoken[scene["index"]] if scene["index"] < len(spoken) else "").split()
            )
            subject = str(getattr(params, "video_subject", "") or "").strip()
            record["keyword_suggestion"] = ". ".join(
                part for part in (subject, line) if part
            )[:400]
        else:
            record["keyword_suggestion"] = ""
        window = windows[scene["index"]] if scene["index"] < len(windows) else None
        # 成片里的起止时间。界面靠它把"第 7 段画面"和"第 30 秒的旁白"对上。
        record["starts_at"] = window["start"] if window else None
        record["ends_at"] = window["end"] if window else None
        record["url"] = media_url(task_id, scene["material"], endpoint)
        record["exists"] = bool(record["url"])
        timing = scene_manifest.scene_timing(scene)
        record["trim_start"] = timing["trim_start"]
        record["duration"] = timing["duration"]
        record["source_duration"] = None
        if with_timing and record["exists"]:
            try:
                record["source_duration"] = timeline.probe_duration(
                    scene_manifest.resolve_material(task_id, scene["material"])
                )
            except scene_manifest.SceneError as exc:
                # 探测不出来不该让整个时间线打不开，这一段按"长度未知"显示。
                logger.warning(
                    f"cannot probe scene duration, task_id: {task_id}, "
                    f"scene: {scene.get('index')}, error: {exc}"
                )
        record["material"] = os.path.basename(scene["material"])
        scenes.append(record)
    payload = {
        "task_id": task_id,
        "scenes": scenes,
        "audio_stale": manifest["audio_stale"],
        "audio_url": media_url(task_id, manifest.get("audio_file"), endpoint),
        "subtitle_path": os.path.basename(str(manifest.get("subtitle_path") or "")),
        "script": manifest["script"],
    }
    # 画风：可选项，以及这条片子出片时用的那个。界面要默认选中后者——
    # 编辑器以前根本不知道风格这件事，重生成只能用 API 进程"现在"的配置。
    #
    # 只有按关键词作图的来源才有画风可挑，别的来源这里是空列表，界面据此
    # 不画那个下拉框。
    supports_style = (
        str(getattr(params, "video_source", "") or "")
        in image_style_model.IMAGE_STYLE_SOURCES
    )
    payload["image_styles"] = (
        list(image_style_model.image_style_ids()) if supports_style else []
    )
    # 这条片子出片前的任务没有记过画风（这个字段比它们新），那时唯一能说的
    # 就是"按设置来"。界面要分得清这两种情况，否则对一个老任务说"和这条片子
    # 一样"等于撒谎——它用的其实是设置里现在那个风格。
    pinned = str(getattr(params, "image_prompt_template", "") or "").strip()
    payload["image_style_pinned"] = bool(pinned)
    task_style = image_style_model.style_for_template(
        tm.resolve_image_prompt_template(params) if params is not None else ""
    )
    # 自定义模板配不上任何预设名,这时不谎报一个预设,界面只说"和这条片子
    # 一样"就够了。
    payload["image_style"] = (
        "" if task_style == image_style_model.IMAGE_STYLE_CUSTOM else task_style
    )
    # 可挑的转场与运镜。界面不写死这两张表：渲染器认的名字只有服务端知道，
    # 写死一份迟早和它对不上。
    payload["transitions"] = list(SCENE_TRANSITIONS)
    payload["image_motions"] = list(SCENE_MOTIONS)
    # 这台机器上装着的转场音效。界面不写死：音效库是本机目录加用户自己丢进
    # 去的文件，只有服务端知道里面有什么。
    try:
        payload["cut_sounds"] = transition_sound.list_transition_sounds()
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the transition sounds: {exc}")
        payload["cut_sounds"] = []
    payload["planned_total"] = round(windows[-1]["end"], 3) if windows else 0.0
    # 旁白总长无条件带上：审片列表要靠它说清楚"画面铺不满旁白,所以会轮播"。
    # 这只是读一次清单，不探测素材，所以不必等 with_timing。
    payload["narration_total"] = round(
        float(manifest.get("audio_duration") or 0.0), 3
    )
    if with_timing:
        if params is None:
            params = load_params(task_id)
        # 成片的像素尺寸，用来把字号换算到预览框里。比例认不出来时按竖版
        # 算：那是这个项目的默认，猜错也只是预览里的字略大略小。
        try:
            frame_width, frame_height = VideoAspect(
                getattr(params, "video_aspect", None) or VideoAspect.portrait
            ).to_resolution()
        except (ValueError, TypeError):
            frame_width, frame_height = VideoAspect.portrait.to_resolution()
        payload["clip_speed"] = float(getattr(params, "video_clip_speed", 1.0) or 1.0)
        payload["audio_duration"] = float(manifest.get("audio_duration") or 0.0)
        payload["has_timing_edits"] = scene_manifest.has_timing_edits(
            manifest["scenes"]
        )
        # 旁白是逐句合成的，字幕里的每一条正好对应一句。时间线按句画出来，
        # 用户才看得出哪句话落在哪一段画面上。
        payload["narration_segments"] = scene_manifest.narration_segments(task_id)
        # 渲染设置要一并交给界面。否则用户看不到画面在动、看不到转场时，
        # 无法区分"编辑器没显示"和"这次生成本来就没配"——而后者才是常见原因：
        # image_motion=static 的素材在生成时就烘成了静止片段,
        # transition=None 则是渲染阶段压根不加转场。
        payload["render_settings"] = {
            "aspect": _enum_value(getattr(params, "video_aspect", "")),
            "image_motion": _enum_value(getattr(params, "image_motion", "")),
            "transition": _enum_value(getattr(params, "video_transition_mode", "")),
            # 整条片子的那一声，以及音量。界面要靠它写出"跟片子一样（whoosh）"，
            # 而音量是整条片子一个值——每一段挑的是"响哪一声"，不是"响多大"。
            "transition_sound": str(getattr(params, "transition_sound", "") or ""),
            "transition_sound_volume": float(
                getattr(params, "transition_sound_volume", 0.0) or 0.0
            ),
            "concat_mode": _enum_value(getattr(params, "video_concat_mode", "")),
            "clip_duration": getattr(params, "video_clip_duration", None),
            # 预览要照搬渲染器的适配方式，否则这里看到的构图和成片不一致：
            # cover 会裁掉多余边缘，contain 会留黑边。
            "fit": _enum_value(getattr(params, "video_fit_mode", "")),
            # 成片的像素尺寸。编辑器要按它把字号换算到预览框的大小上——
            # 60 号字在 1920 高的画面里和在 400 高的预览框里完全是两回事。
            "frame_width": frame_width,
            "frame_height": frame_height,
            # 字幕的样子。预览里要照着画，否则"字会不会挡住画面、够不够大"
            # 这件事只能等渲完才知道。
            "subtitle": {
                "enabled": bool(getattr(params, "subtitle_enabled", True)),
                "position": str(getattr(params, "subtitle_position", "") or "bottom"),
                "custom_position": float(
                    getattr(params, "custom_position", 70.0) or 0.0
                ),
                "font_size": int(getattr(params, "font_size", 60) or 60),
                "color": str(getattr(params, "text_fore_color", "") or "#FFFFFF"),
                "stroke_color": str(getattr(params, "stroke_color", "") or "#000000"),
                "stroke_width": float(getattr(params, "stroke_width", 0) or 0.0),
                # False 表示不要底色；字符串是颜色。两者都原样交给界面。
                "background": getattr(params, "text_background_color", False),
                "animation": str(getattr(params, "subtitle_animation", "") or "none"),
            },
        }
    return payload


def prepare_audio_rerun(task_id: str) -> VideoParams:
    """准备"用编辑后的台词重新配音"所需的参数，由调用方提交任务执行。

    真正的重配音仍然走流水线自己的 audio 与 subtitle 阶段
    (``start_from="audio"``, ``stop_at="subtitle"``),而不是在这里直接调
    TTS：那两个阶段里有音量处理、自定义音频、字幕提供方差异等一串既有行为,
    在这里复制一遍必然漏掉其中几条。

    台词先落盘,因为 ``start_from="audio"`` 时脚本是从清单读回来的,不是从
    params 里读的。
    """
    _require_idle(task_id)
    params = load_params(task_id)
    # 按时间排好的台词，而不是按场景顺序拼起来的：重排画面不该改变念词的
    # 顺序。见 ``narration_script``。
    script = narration_script(task_id)
    if not script:
        raise SceneError("every scene's narration is empty; nothing to narrate")

    if not scene_manifest.patch_script(task_id, script):
        raise SceneError("failed to save the edited narration")
    # 让 params 自身保持一致，避免任何入口再去问一次 LLM 要脚本。
    params.video_script = script
    return params


# 一段画面可以自己挑的转场。键是接口和界面用的 id，值是渲染器认的那个字符
# 串（``VideoTransitionMode`` 的 value）。"none" 是显式的"这一刀不要转场"，
# 和"没挑过"不是一回事：前者压过整条片子的设置，后者跟着它走。
SCENE_TRANSITIONS: dict[str, str] = {
    "none": "none",
    "fade_in": VideoTransitionMode.fade_in.value,
    "fade_out": VideoTransitionMode.fade_out.value,
    "slide_in": VideoTransitionMode.slide_in.value,
    "slide_out": VideoTransitionMode.slide_out.value,
    "zoom_in": VideoTransitionMode.zoom_in.value,
    "zoom_out": VideoTransitionMode.zoom_out.value,
    "shuffle": VideoTransitionMode.shuffle.value,
}

SCENE_MOTIONS: tuple[str, ...] = ("zoom", "static")


def transition_id(stored: Any) -> str:
    """把存着的渲染器值翻回界面用的 id，认不出来就当"没挑过"。"""
    value = str(stored or "").strip()
    for key, mapped in SCENE_TRANSITIONS.items():
        if mapped == value:
            return key
    return ""


def set_effects(
    task_id: str,
    index: Any,
    *,
    transition: str | None = None,
    image_motion: str | None = None,
    cut_sound: str | None = None,
) -> dict:
    """设置这一段画面的转场和运镜。

    两者在片子里的位置完全不同，但在界面上是同一件事（"这一段怎么出场"），
    所以放在一个入口里：

    * 转场是渲染期的效果，存下来就行，渲染时按剪辑表逐段应用；
    * 运镜在出图那一刻就烘进 mp4 了,所以改它要从原图重烘一段——几秒钟的
      本机编码,而且 zoom/static 来回切换时第二次起直接命中磁盘上的旧文件。

    两者都用空字符串表示"跟整条片子一样"。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]

        if transition is not None:
            chosen = str(transition).strip()
            if chosen and chosen not in SCENE_TRANSITIONS:
                raise SceneError(
                    f"unknown transition {chosen!r}; pick one of: "
                    f"{', '.join(sorted(SCENE_TRANSITIONS))} (or empty to "
                    "follow the video)"
                )
            if chosen:
                scene["transition"] = SCENE_TRANSITIONS[chosen]
            else:
                scene.pop("transition", None)

        if cut_sound is not None:
            sound = str(cut_sound).strip()
            if sound and sound != "none":
                # 名字必须当场校验。等到渲染那一刻才发现"这台机器上没有这个
                # 音效"，用户已经等完了一整次渲染,而片子里就是没响。
                try:
                    transition_sound.resolve_transition_sound(sound)
                except ValueError as exc:
                    raise SceneError(
                        f"unknown transition sound {sound!r}: {exc}"
                    ) from exc
            if sound:
                scene["cut_sound"] = sound
            else:
                scene.pop("cut_sound", None)

        if image_motion is not None:
            motion = str(image_motion).strip()
            if motion and motion not in SCENE_MOTIONS:
                raise SceneError(
                    f"unknown picture motion {motion!r}; pick one of: "
                    f"{', '.join(SCENE_MOTIONS)} (or empty to follow the video)"
                )
            _apply_scene_motion(task_id, scene, motion)

        scene_manifest.save_scenes(task_id, scenes)
        return scenes[position]


def edit_subtitle(task_id: str, index: Any, text: str) -> dict:
    """改一条字幕的文字，时间轴和配音都不动。

    这和改脚本是两件事，不能混在一起：

    * 改脚本是改**要念的词**，必须重新配音才算数，整条字幕的时间点也会跟着
      重来；
    * 改字幕只是改**屏幕上那行字**。听写把 "paddy" 认成 "padding" 的时候，
      要改的就是这一条——为一个字重跑一遍 TTS 并作废全部时间点，代价完全不
      成比例。

    所以这里只重写字幕文件里那一条的正文，时间戳按原样抄回去（不重新格式
    化，免得在浮点换算上丢精度），配音文件一个字节都不动。

    代价要说在前面：之后真去重新配音时，字幕是按新配音重新生成的，这里的
    改动会被盖掉。界面上写明了这一点。
    """
    _require_idle(task_id)
    with _editing(task_id):
        path = _subtitle_file(task_id)
        cues = subtitle_reader.file_to_subtitles(path)
        if not cues:
            raise SceneError("this task has no subtitles to edit")
        try:
            position = int(index)
        except (TypeError, ValueError) as exc:
            raise SceneError(f"subtitle index must be a whole number, got {index!r}") from exc
        if not 0 <= position < len(cues):
            raise SceneError(
                f"there is no subtitle {position}; this task has {len(cues)}"
            )

        wanted = str(text or "").strip()
        if not wanted:
            raise SceneError(
                "a caption cannot be empty; put a space-free dash in it if you "
                "want it blank on screen"
            )

        blocks = []
        for order, (_number, times, body) in enumerate(cues):
            blocks.append(
                f"{order + 1}\n{times}\n{wanted if order == position else body}\n"
            )
        if not subtitle_writer.write_subtitle_file(path, "\n".join(blocks)):
            raise SceneError("failed to save the caption")
        logger.info(f"edited subtitle {position} of task {task_id}")
    return {"index": position, "text": wanted}


def _subtitle_file(task_id: str) -> str:
    """这条任务的字幕文件，没有就报错点名。"""
    payload = scene_manifest.load_raw(task_id)
    stored = str(payload.get("subtitle_path") or "").strip()
    if not stored:
        raise SceneError(
            "this task has no subtitle file; it was generated with subtitles "
            "turned off"
        )
    path = scene_manifest.resolve_task_file(task_id, stored)
    if not os.path.isfile(path):
        raise SceneError(f"the subtitle file is missing: {os.path.basename(path)}")
    return path


def library(endpoint: str = "", *, page: int = 1, page_size: int = 24) -> dict:
    """磁盘上所有做过的片子，最近动过的排在前面。

    为什么不用 ``GET /tasks``：那一份是进程内存里的任务表，API 一重启就空了。
    而"我做过哪些片子"这件事的真正记录在磁盘上——每个任务目录里的
    ``script.json``。用户隔天回来想接着改前天那条片子，内存里什么都没有。

    先按修改时间排序、再只读这一页的清单：任务目录会一直攒下去（这台机器上
    已经有一百多个），每次请求都把每一份清单读一遍是没必要的。
    """
    root = utils.task_dir()
    try:
        names = os.listdir(root)
    except OSError as exc:
        logger.warning(f"cannot list the task folder: {exc}")
        return {"tasks": [], "total": 0, "page": page, "page_size": page_size}

    found: list[tuple[float, str]] = []
    for name in names:
        manifest = os.path.join(root, name, "script.json")
        try:
            found.append((os.path.getmtime(manifest), name))
        except OSError:
            # 没有清单的目录不是一条做过的片子（半路失败、或别的东西留下的）。
            continue
    found.sort(reverse=True)

    total = len(found)
    start = max(0, (int(page) - 1) * int(page_size))
    window = found[start:start + int(page_size)]

    tasks = []
    for changed_at, task_id in window:
        try:
            tasks.append(_library_entry(task_id, changed_at, endpoint))
        except Exception as exc:  # pragma: no cover - 一条读不出来不该毁掉整页
            logger.warning(f"skipping {task_id} in the library: {exc}")
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


def _library_entry(task_id: str, changed_at: float, endpoint: str) -> dict:
    """列表里的一条：够用户认出这是哪条片子，不多读一个字节。"""
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    raw = scene_manifest.load_raw(task_id)
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}

    subject = str(params.get("video_subject") or "").strip()
    if not subject:
        # 没有主题就用脚本开头，总比一串 uuid 强。
        subject = " ".join(str(manifest.get("script") or "").split())[:80]

    # 出过片的任务要能一眼看出来，并且能直接点开看。
    directory = utils.task_dir(task_id)
    finals = sorted(
        name for name in os.listdir(directory)
        if name.startswith("final-") and name.endswith(".mp4")
    ) if os.path.isdir(directory) else []

    return {
        "task_id": task_id,
        "subject": subject,
        "scenes": len(scenes),
        "narration": round(float(manifest.get("audio_duration") or 0.0), 1),
        "audio_stale": bool(manifest.get("audio_stale")),
        "source": str(params.get("video_source") or ""),
        "updated_at": round(float(changed_at), 3),
        "rendered": bool(finals),
        "video_url": media_url(task_id, os.path.join(directory, finals[-1]), endpoint)
        if finals else "",
        # 第一段画面当封面。读不出来就空着，界面画一个占位块。
        "thumb_url": media_url(task_id, scenes[0]["material"], endpoint)
        if scenes else "",
    }


def suggested_keyword(task_id: str, index: Any) -> str:
    """这一段没有关键词时，拿什么去出图。

    一条任务常常关键词比场景少——这条片子 25 段画面只拿到 12 个关键词，后面
    13 段的关键词是空的。空关键词在重生成那里直接被拒，于是用户要么逐段手敲
    一句，要么那几段永远换不了画面。

    退回"片子主题 + 这一段念的那句话"。它不如模型逐句写出来的画面描述，但
    它至少说的是这一刻的事，而且画风模板还会接在后面。界面也把这句话填进
    关键词框当占位文字，所以用户看得见它要拿什么去画，改一笔就是自己的。

    Keyword lists are routinely shorter than the scene list, and an empty
    keyword is refused outright, which leaves those scenes unregenerable. The
    subject plus the line spoken over this scene is a weaker prompt than a
    written-out visual description, but it is about this moment rather than
    about nothing.
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    try:
        position = _scene_at(scenes, index)
    except SceneError:
        return ""
    spoken = narration_for_scenes(task_id, manifest)
    line = " ".join(
        str(spoken[position] if position < len(spoken) else "").split()
    )
    try:
        subject = str(
            getattr(load_params(task_id), "video_subject", "") or ""
        ).strip()
    except SceneError:
        subject = ""
    parts = [part for part in (subject, line) if part]
    return ". ".join(parts)[:400]


def _patch_params(task_id: str, **fields: Any) -> dict:
    """改这条任务自己存着的渲染参数。

    审片之后的渲染读的是清单里的 ``params``，不是 WebUI 的 session，也不是全局
    配置。所以在编辑器里改的每一项都只影响这一条片子——这正是用户要的：为了
    眼前这条片子调一下字幕颜色,不该把下一条也改了。
    """
    payload = scene_manifest.load_raw(task_id)
    stored = payload.get("params")
    if not isinstance(stored, dict):
        raise SceneError("this task has no saved parameters; it cannot be changed")
    stored.update(fields)
    if not task_artifacts.patch_script_data(task_id, params=stored):
        raise SceneError("failed to save the render settings")
    return stored


SUBTITLE_POSITIONS: tuple[str, ...] = (
    "top",
    "center",
    "bottom",
    "two_thirds_bottom",
    "custom",
)

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def available_fonts() -> list[str]:
    """字体目录里的字体文件名，含子目录。"""
    directory = utils.font_dir()
    found: list[str] = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith((".ttf", ".ttc", ".otf")):
                relative = os.path.relpath(os.path.join(root, name), directory)
                found.append(relative.replace(os.sep, "/"))
    return sorted(found)


def subtitle_style(task_id: str) -> dict:
    """字幕的样子，以及这台机器上有哪些字体可选。"""
    params = load_params(task_id)
    background = getattr(params, "text_background_color", False)
    return {
        "enabled": bool(getattr(params, "subtitle_enabled", True)),
        "font_name": str(getattr(params, "font_name", "") or ""),
        "font_size": int(getattr(params, "font_size", 60) or 60),
        "color": str(getattr(params, "text_fore_color", "") or "#FFFFFF"),
        "stroke_color": str(getattr(params, "stroke_color", "") or "#000000"),
        "stroke_width": float(getattr(params, "stroke_width", 0) or 0.0),
        "background": background if isinstance(background, str) else bool(background),
        "position": str(getattr(params, "subtitle_position", "") or "bottom"),
        "custom_position": float(getattr(params, "custom_position", 70.0) or 0.0),
        "positions": list(SUBTITLE_POSITIONS),
        "fonts": available_fonts(),
    }


def set_subtitle_style(task_id: str, **wanted: Any) -> dict:
    """改字幕的样子：字体、字号、颜色、描边、底色、位置。

    只校验、只落盘，不碰字幕文件本身：这些是渲染时才用到的样式，改它们既不
    会动时间轴，也不会让配音失效。

    颜色一律按 ``#RRGGBB`` 收：渲染那边把它交给 Pillow，一个拼错的颜色名会
    在渲染的最后一步才炸，那时用户已经等完了整条片子。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if "enabled" in wanted and wanted["enabled"] is not None:
            fields["subtitle_enabled"] = bool(wanted["enabled"])

        font = wanted.get("font_name")
        if font is not None:
            name = str(font).strip()
            if name and name not in available_fonts():
                raise SceneError(
                    f"unknown font {name!r}; pick one of the fonts in "
                    "resource/fonts"
                )
            fields["font_name"] = name

        size = wanted.get("font_size")
        if size is not None:
            try:
                value = int(size)
            except (TypeError, ValueError) as exc:
                raise SceneError("the font size must be a whole number") from exc
            if not 8 <= value <= 300:
                raise SceneError(f"the font size must be between 8 and 300, got {value}")
            fields["font_size"] = value

        for key, field in (("color", "text_fore_color"), ("stroke_color", "stroke_color")):
            colour = wanted.get(key)
            if colour is None:
                continue
            text = str(colour).strip()
            if not _COLOUR.match(text):
                raise SceneError(
                    f"{key} must look like #RRGGBB, got {text!r}"
                )
            fields[field] = text.upper()

        width = wanted.get("stroke_width")
        if width is not None:
            try:
                value = float(width)
            except (TypeError, ValueError) as exc:
                raise SceneError("the outline width must be a number") from exc
            if not 0 <= value <= 20:
                raise SceneError(
                    f"the outline width must be between 0 and 20, got {value}"
                )
            fields["stroke_width"] = round(value, 2)

        background = wanted.get("background")
        if background is not None:
            # False 是"不要底色"，字符串是颜色。两者都是渲染器认的写法。
            if isinstance(background, bool) or background == "":
                fields["text_background_color"] = False
            else:
                text = str(background).strip()
                if not _COLOUR.match(text):
                    raise SceneError(
                        f"the caption background must look like #RRGGBB or be "
                        f"turned off, got {text!r}"
                    )
                fields["text_background_color"] = text.upper()

        position = wanted.get("position")
        if position is not None:
            text = str(position).strip()
            if text not in SUBTITLE_POSITIONS:
                raise SceneError(
                    f"unknown caption position {text!r}; pick one of: "
                    f"{', '.join(SUBTITLE_POSITIONS)}"
                )
            fields["subtitle_position"] = text

        custom = wanted.get("custom_position")
        if custom is not None:
            try:
                value = float(custom)
            except (TypeError, ValueError) as exc:
                raise SceneError("the custom position must be a number") from exc
            if not 0 <= value <= 100:
                raise SceneError(
                    f"the custom position must be between 0 and 100, got {value}"
                )
            fields["custom_position"] = round(value, 1)

        if fields:
            _patch_params(task_id, **fields)
    return subtitle_style(task_id)


def video_effects(task_id: str) -> dict:
    """整条片子的转场和运镜，以及有几段自己挑过、不跟着它走。"""
    params = load_params(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    return {
        "transition": transition_id(
            _enum_value(getattr(params, "video_transition_mode", "")) or "none"
        ),
        "image_motion": str(_enum_value(getattr(params, "image_motion", "")) or ""),
        "transitions": list(SCENE_TRANSITIONS),
        "image_motions": list(SCENE_MOTIONS),
        # 界面要据此说清楚"改了这个，那几段还是不跟着变"。
        "scenes_with_own_transition": sum(
            1 for scene in scenes if str(scene.get("transition") or "").strip()
        ),
        "scenes": len(scenes),
    }


def set_video_effects(
    task_id: str,
    *,
    transition: str | None = None,
    image_motion: str | None = None,
    apply_to_scenes: bool = False,
) -> dict:
    """一次改掉整条片子的转场（和运镜）。

    ``apply_to_scenes`` 打开时，顺手清掉每一段自己挑过的转场——否则"全部改成
    淡入"之后，之前单独挑过的那几段仍然我行我素,而用户以为自己已经改完了。

    运镜这一项只影响**之后**出的图：运镜是出图那一刻烘进 mp4 的，已经在盘上
    的素材不会因为改了这个设置而变。要改已经出好的那一段,用那一段自己的运镜
    （会从原图重烘一份）。这件事界面上也写明了。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if transition is not None:
            chosen = str(transition).strip()
            if chosen and chosen not in SCENE_TRANSITIONS:
                raise SceneError(
                    f"unknown transition {chosen!r}; pick one of: "
                    f"{', '.join(sorted(SCENE_TRANSITIONS))}"
                )
            # 渲染器那边 "没有转场" 是 None，不是字符串 "none"。
            fields["video_transition_mode"] = (
                None if not chosen or chosen == "none" else SCENE_TRANSITIONS[chosen]
            )

        if image_motion is not None:
            motion = str(image_motion).strip()
            if motion and motion not in SCENE_MOTIONS:
                raise SceneError(
                    f"unknown picture motion {motion!r}; pick one of: "
                    f"{', '.join(SCENE_MOTIONS)}"
                )
            if motion:
                fields["image_motion"] = motion

        if fields:
            _patch_params(task_id, **fields)

        if apply_to_scenes and transition is not None:
            scenes = scene_manifest.load(task_id)["scenes"]
            cleared = 0
            for scene in scenes:
                if scene.pop("transition", None) is not None:
                    cleared += 1
            if cleared:
                scene_manifest.save_scenes(task_id, scenes)
                logger.info(
                    f"cleared {cleared} per-scene transitions of task {task_id}"
                )
    return video_effects(task_id)


def cut_sound_settings(task_id: str) -> dict:
    """整条片子在每一刀上响的那一声，以及可选的音效。"""
    params = load_params(task_id)
    try:
        library = [os.path.basename(p) for p in transition_sound.list_transition_sounds()]
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the transition sounds: {exc}")
        library = []
    return {
        "sound": str(getattr(params, "transition_sound", "") or ""),
        "volume": float(getattr(params, "transition_sound_volume", 0.0) or 0.0),
        "sounds": library,
    }


def set_cut_sound(
    task_id: str,
    *,
    sound: str | None = None,
    volume: float | None = None,
) -> dict:
    """换整条片子的转场音效，或者把它整个关掉。

    ``sound`` 空字符串就是"一刀都不要响"——这是唯一一个能把它从整条片子上
    拿掉的入口。每一段自己挑的那个"静音"只管自己那一刀。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if sound is not None:
            name = str(sound).strip()
            if name:
                try:
                    transition_sound.resolve_transition_sound(name)
                except ValueError as exc:
                    raise SceneError(f"unknown transition sound {name!r}: {exc}") from exc
            fields["transition_sound"] = name

        if volume is not None:
            try:
                level = float(volume)
            except (TypeError, ValueError) as exc:
                raise SceneError("the sound volume must be a number") from exc
            if not 0.0 <= level <= 1.0:
                raise SceneError(
                    f"the sound volume must be between 0 and 1, got {level}"
                )
            fields["transition_sound_volume"] = round(level, 3)

        if fields:
            _patch_params(task_id, **fields)
    return cut_sound_settings(task_id)


RANDOM_BACKGROUND = "random"


def background_music(task_id: str) -> dict:
    """这条片子的背景音乐：选了什么、多响，以及这台机器上有哪些可选。"""
    params = load_params(task_id)
    chosen = str(getattr(params, "bgm_file", "") or "").strip()
    kind = str(getattr(params, "bgm_type", "") or "").strip()
    try:
        tracks = bgm.list_bgm_files()
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the background music: {exc}")
        tracks = []
    return {
        # 界面只认三种：不要、随机、某一首。bgm_type 里那些外部提供商
        # (sonilo/elevenlabs) 这里原样显示为"由设置决定"，不去改它。
        "track": (
            os.path.basename(chosen)
            if chosen
            else (RANDOM_BACKGROUND if kind == RANDOM_BACKGROUND else "")
        ),
        "kind": kind,
        "volume": float(getattr(params, "bgm_volume", 0.0) or 0.0),
        "tracks": [os.path.basename(path) for path in tracks],
        # 这条片子交给外部服务生成配乐时，界面不该假装能在这里换曲子。
        "external": kind in {"sonilo", "elevenlabs"},
    }


def set_background_music(
    task_id: str,
    *,
    track: str | None = None,
    volume: float | None = None,
) -> dict:
    """换掉这条片子的背景音乐，或者调它的音量。

    ``track`` 空字符串是"不要背景音乐"，``"random"`` 是每次渲染随机挑一首，
    其余按文件名解析。名字当场校验：等渲染完才发现这台机器上没有这首曲子,
    用户已经白等了一整次渲染,而片子里就是没有音乐。

    改的是任务清单里的 ``params``——审片之后的渲染读的就是那一份，不是
    WebUI 的 session，也不是全局配置。所以这里换的曲子只影响这一条片子。
    """
    _require_idle(task_id)
    with _editing(task_id):
        params = load_params(task_id)
        payload = scene_manifest.load_raw(task_id)
        stored = payload.get("params")
        if not isinstance(stored, dict):
            raise SceneError(
                "this task has no saved parameters; its music cannot be changed"
            )

        if track is not None:
            wanted = str(track).strip()
            if not wanted:
                stored["bgm_type"] = ""
                stored["bgm_file"] = ""
            elif wanted == RANDOM_BACKGROUND:
                stored["bgm_type"] = RANDOM_BACKGROUND
                stored["bgm_file"] = ""
            else:
                try:
                    resolved = bgm.resolve_bgm_file(wanted)
                except ValueError as exc:
                    raise SceneError(
                        f"unknown background music {wanted!r}: {exc}"
                    ) from exc
                # 内置歌曲和用户自己丢进去的分别记成 preset/custom，WebUI 的
                # 来源下拉框据此显示，不至于把一首内置曲子说成"自定义"。
                builtin = {
                    os.path.basename(path) for path in bgm.list_builtin_bgm_files()
                }
                stored["bgm_type"] = (
                    "preset" if os.path.basename(resolved) in builtin else "custom"
                )
                stored["bgm_file"] = resolved

        if volume is not None:
            try:
                level = float(volume)
            except (TypeError, ValueError) as exc:
                raise SceneError("the music volume must be a number") from exc
            if not 0.0 <= level <= 1.0:
                raise SceneError(
                    f"the music volume must be between 0 and 1, got {level}"
                )
            stored["bgm_volume"] = round(level, 3)

        if not task_artifacts.patch_script_data(task_id, params=stored):
            raise SceneError("failed to save the background music")
    return background_music(task_id)


def _apply_scene_motion(task_id: str, scene: dict, motion: str) -> None:
    """按新的运镜重烘这一段的素材，并记下用户挑的是哪一种。

    空字符串是"跟整条片子一样"，这时按任务参数里的运镜重烘一次——否则"改回
    默认"会让画面停在上一次挑的那个样子,而界面显示的是"跟片子一样"。
    """
    params = load_params(task_id)
    wanted = motion or str(_enum_value(getattr(params, "image_motion", "")) or "zoom")
    material = scene_manifest.resolve_material(task_id, scene["material"])
    if timeline.motion_of(material) == wanted:
        # 已经是这个运镜了，不必重烘。记号还是要更新：用户可能是从"静止"
        # 切回"跟片子一样"，而片子本来就是静止的。
        pass
    else:
        try:
            rendered = timeline.ensure_image_motion(task_id, material, wanted)
        except timeline.TimelineError as exc:
            raise SceneError(str(exc)) from exc
        scene["material"] = scene_manifest.relative_media_path(task_id, rendered)
        scene["kind"] = scene_manifest.material_kind(rendered)
    if motion:
        scene["image_motion"] = motion
    else:
        scene.pop("image_motion", None)


def update_timing(
    task_id: str,
    index: int,
    *,
    trim_start=None,
    duration=None,
    reset: bool = False,
) -> dict:
    """设置一个场景的入点和在成片里的停留时长。

    先按素材真实长度算一遍再落盘：用户在时间线上完全可能拖过素材末尾，
    那时应该收到"已经按素材长度收住"，而不是渲染到这一段才失败。
    """
    _require_idle(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    if not 0 <= index < len(scenes):
        raise SceneError(
            f"scene {index} does not exist; this task has {len(scenes)} scenes"
        )

    if reset:
        return scene_manifest.clear_scene_timing(task_id, index)

    scene = scenes[index]
    material = scene_manifest.resolve_material(task_id, scene.get("material"))
    params = load_params(task_id)
    speed = getattr(params, "video_clip_speed", 1.0) or 1.0

    source_duration = timeline.probe_duration(material)
    current = scene_manifest.scene_timing(scene)
    requested_start = current["trim_start"] if trim_start is None else trim_start
    requested_duration = current["duration"] if duration is None else duration

    needed_source = None
    if requested_duration is not None:
        try:
            needed_source = float(requested_duration) * max(float(speed), 1e-6)
        except (TypeError, ValueError):
            needed_source = None

    # 图片素材的源片段是生成时按固定时长烘好的，要停更久必须按新时长重渲。
    # 这里就地重渲并把场景指向新文件，用户才能真的把一张图拉长。
    if (
        needed_source is not None
        and scene_manifest.material_kind(material) != "video"
        and needed_source > source_duration - float(requested_start or 0.0) + 1e-6
    ):
        extended = timeline.ensure_image_length(
            task_id, material, float(requested_start or 0.0) + needed_source
        )
        if extended != material:
            _point_scene_at(task_id, index, extended)
            material = extended
            source_duration = timeline.probe_duration(material)

    timing = timeline.normalise_timing(
        source_duration=source_duration,
        trim_start=requested_start,
        duration=requested_duration,
        clip_speed=speed,
    )
    # 只把写清单那一下圈进锁里。上面的 ``ensure_image_length`` 可能要重渲一
    # 段画面，拿着锁去做它没必要。
    with _editing(task_id):
        scene = scene_manifest.patch_scene_timing(
            task_id,
            index,
            trim_start=timing["trim_start"],
            duration=timing["duration"],
        )
    scene["clamped"] = timing["clamped"]
    scene["source_duration"] = timing["source_duration"]
    return scene


def fit_to_narration(task_id: str) -> dict:
    """把现有场景按比例拉长或压缩，铺满整段旁白。

    为什么需要这个：素材的张数由关键词个数决定，不由旁白长度决定。一次
    12 个关键词、每段 2 秒的生成只有 24 秒画面,而旁白可能有 64 秒。自动出片
    那条路会把这 12 段循环播放近三遍来凑够时长;时间线是"所见即所得"的,
    不会替用户循环,于是时间线总长就停在 24 秒。

    这里按比例缩放而不是平均分配：用户可能已经手动调过某几段的长短,平均
    分配会把那些编辑一笔抹掉。全都没调过时,按比例缩放的结果本来就等于平均。

    视频素材拉不过自身长度,会被 ``update_timing`` 收住,所以返回值里带上
    实际达到的总长,让调用方如实告诉用户差了多少,而不是假装铺满了。
    """
    _require_idle(task_id)
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )

    target = float(manifest.get("audio_duration") or 0.0)
    if target <= 0:
        raise SceneError(
            "this task has no narration to fit to; record the narration first"
        )

    params = load_params(task_id)
    speed = float(getattr(params, "video_clip_speed", 1.0) or 1.0) or 1.0

    current: list[float] = []
    for scene in scenes:
        material = scene_manifest.resolve_material(task_id, scene.get("material"))
        source_duration = timeline.probe_duration(material)
        timing = scene_manifest.scene_timing(scene)
        if timing["duration"] is not None:
            held = float(timing["duration"])
        else:
            held = (source_duration - timing["trim_start"]) / speed
        current.append(max(timeline.MIN_CLIP_DURATION, held))

    total = sum(current)
    if total <= 0:
        raise SceneError("these scenes have no usable duration")

    factor = target / total
    for index, held in enumerate(current):
        update_timing(task_id, index, duration=round(held * factor, 3))

    logger.info(
        f"fitted {len(scenes)} scenes to {target:.2f}s of narration "
        f"(scaled by {factor:.2f}x), task_id: {task_id}"
    )
    return describe(task_id, with_timing=True)


def _point_scene_at(task_id: str, index: int, material_path: str) -> None:
    """把某个场景指向一份新素材文件。供图片按新时长重渲后改写清单使用。"""
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        if not 0 <= index < len(scenes):
            return
        scenes[index]["material"] = scene_manifest.relative_media_path(
            task_id, material_path
        )
        scenes[index]["kind"] = scene_manifest.material_kind(material_path)
        scene_manifest.save_scenes(task_id, scenes)


def prepare_render(task_id: str) -> VideoParams:
    """准备恢复渲染所需的参数，并在提交前把能先查的前提都查掉。"""
    _require_idle(task_id)
    params = load_params(task_id)
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )
    if manifest["audio_stale"]:
        raise SceneError(
            "the narration no longer matches the edited script; "
            "regenerate the audio before rendering"
        )
    # 素材文件在审片期间可能被外部删除。这里先全部解析一遍，让错误停在
    # 提交之前，而不是在后台渲染到一半才失败。
    scene_manifest.materials_for_render(task_id)

    # 只有真的拖过时间线才生成剪辑表。一次都没拖过时清掉它，任务就回到
    # 原来的自动切片逻辑——默认出片结果因此一个字节都不会变。
    # 只有真的拖过时间线才写剪辑表。一次都没拖过时清空它，任务就回到原来的
    # 自动切片逻辑——默认出片结果因此一个字节都不会变。
    #
    # 剪辑表落在任务清单里而不是只存内存：渲染任务重启后仍要渲染用户排好的
    # 那条时间线。素材按任务目录相对名存，整个任务目录搬走也还能解析。
    # 挑过转场的任务也要走剪辑表：每段的转场只能从剪辑表交到渲染器手上。
    if scene_manifest.has_render_overrides(scenes):
        speed = getattr(params, "video_clip_speed", 1.0) or 1.0
        edit_list = timeline.build_edit_list(task_id, scenes, clip_speed=speed)
        task_artifacts.patch_script_data(
            task_id,
            edit_list=[
                {
                    # 按文件名存，和 ``scenes[].material`` 一个写法：
                    # ``resolve_material`` 的白名单是"任务目录 + 上传目录",
                    # 存成 ``/stream`` 用的那种 ``<task_id>/<file>`` 反而解析不出来。
                    "material": os.path.basename(item["path"]),
                    "start": round(float(item["start"]), 3),
                    "end": round(float(item["end"]), 3),
                    # 没挑过转场的段不写这个键，渲染器照旧用整条片子的设置。
                    **(
                        {"transition": item["transition"]}
                        if item.get("transition")
                        else {}
                    ),
                    **(
                        {"cut_sound": item["cut_sound"]}
                        if item.get("cut_sound")
                        else {}
                    ),
                }
                for item in edit_list
            ],
        )
    else:
        task_artifacts.patch_script_data(task_id, edit_list=[])
    return params
