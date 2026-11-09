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
import shutil
from typing import Any, Sequence

from loguru import logger

from app.models.schema import VideoAspect, VideoConcatMode, VideoParams
from app.services import material, scene_manifest, task_artifacts, timeline
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

SceneError = scene_manifest.SceneError


class SceneBusyError(SceneError):
    """任务正在渲染或发布，此时编辑场景会和渲染读到的文件打架。"""


# 这些源不是按关键词取素材的，重新生成一个场景没有意义。
# These sources are not keyword-driven, so regenerating one scene is meaningless.
_UNSUPPORTED_REGEN_SOURCES = ("local", "loomloom")


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


def regenerate_material(
    task_id: str, index: Any, *, keyword: str | None = None
) -> dict:
    """重新取这一个场景的素材。

    ``keyword`` 给了就同时改写场景关键词——用户看到画面不对时,改的往往正是
    关键词,所以这两件事应该一次做完,而不是让用户先改关键词再点一次重生成。

    对库存素材源（Pexels 等）有一个已知限制：搜索结果有缓存,同一个关键词
    重新生成很可能拿回同一个文件。本地出图的源每次都是新的采样,所以重生成
    确实会得到不同的画面。这里不绕过缓存——绕过等于让用户在不知情的情况下
    多花一次第三方额度。
    """
    _require_idle(task_id)
    scenes = _scenes_or_fail(task_id)
    position = _scene_at(scenes, index)
    scene = scenes[position]

    term = str(keyword if keyword is not None else scene.get("keyword") or "").strip()
    new_path, source = _produce_material(task_id, term, what=f"scene {position}")
    scene["material"] = new_path
    scene["kind"] = scene_manifest.material_kind(new_path)
    scene["source"] = source
    scene["keyword"] = term
    # 用户亲手重生成过的结果，不应该被后续批量操作再覆盖掉。
    scene["locked"] = True
    scene_manifest.save_scenes(task_id, scenes)
    return scene


def _produce_material(
    task_id: str,
    term: str,
    *,
    what: str,
    advice: str = "replace this scene with your own file instead",
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
        )
    except Exception as exc:
        # 供应商失败必须点名是谁失败了，不能静默换源，也不能留下半条清单。
        raise SceneError(f"{source} failed to produce a material: {exc}") from exc

    if not produced:
        raise SceneError(f"{source} returned no material for {term!r}")
    return str(produced[0]), source


def add_scene(
    task_id: str, *, keyword: str, narration: str = "", after: Any = None
) -> dict:
    """按关键词生成一段新画面，插进时间线。

    为什么需要它：素材张数由关键词个数决定，不由旁白长度决定。画面比旁白短时
    "把每段停得更久"(fit)只是答案之一,另一个答案是"多补几张画面"——而在这之前
    时间线只能改已有的那些,补不了新的。

    ``after`` 不给就追加到末尾,这也是界面上那个加号的行为。新场景的台词默认
    为空:这里只补画面,不动旁白,否则会悄悄让已经录好的配音失效。
    """
    _require_idle(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    term = str(keyword or "").strip()
    new_path, source = _produce_material(
        task_id,
        term,
        what="a new scene",
        advice="add your own file as a scene instead",
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
    position = len(scenes) if after is None else _scene_at(scenes, after) + 1
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
    scenes = _scenes_or_fail(task_id)
    position = _scene_at(scenes, index)
    scene = scenes[position]

    narration_changed = False
    if narration is not None:
        text = str(narration).strip()
        narration_changed = text != scene.get("narration", "")
        scene["narration"] = text
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
    scene_manifest.save_scenes(task_id, [scenes[position] for position in positions])
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

    texts = scene_manifest.distribute_narration(
        windows,
        segments=scene_manifest.narration_segments(task_id),
        lines=lines,
    )
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
    scenes = []
    for scene in manifest["scenes"]:
        record = dict(scene)
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
    payload["planned_total"] = round(windows[-1]["end"], 3) if windows else 0.0
    if with_timing:
        if params is None:
            params = load_params(task_id)
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
            "concat_mode": _enum_value(getattr(params, "video_concat_mode", "")),
            "clip_duration": getattr(params, "video_clip_duration", None),
            # 预览要照搬渲染器的适配方式，否则这里看到的构图和成片不一致：
            # cover 会裁掉多余边缘，contain 会留黑边。
            "fit": _enum_value(getattr(params, "video_fit_mode", "")),
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
    script = scene_manifest.narration_script(task_id)
    if not script:
        raise SceneError("every scene's narration is empty; nothing to narrate")

    if not scene_manifest.patch_script(task_id, script):
        raise SceneError("failed to save the edited narration")
    # 让 params 自身保持一致，避免任何入口再去问一次 LLM 要脚本。
    params.video_script = script
    return params


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
    if scene_manifest.has_timing_edits(scenes):
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
                }
                for item in edit_list
            ],
        )
    else:
        task_artifacts.patch_script_data(task_id, edit_list=[])
    return params
