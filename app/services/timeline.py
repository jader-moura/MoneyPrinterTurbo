"""时间线编辑：把审片后的场景清单翻译成渲染器要的显式剪辑表。

Why this module exists
----------------------
``combine_videos`` 原本自己决定每段画面多长：按旁白总时长和 ``max_clip_duration``
把素材切成等长片段，不够就循环、多了就丢掉。那套逻辑对"一键出片"是对的,但
时间线上用户明确拖了入点出点,就必须照他拖的渲染。

这里只负责"把场景清单算成一张剪辑表",不碰渲染本身。渲染器拿到剪辑表后
走的还是原来那条路：fit mode、变速、转场、编码、拼接、字幕全部复用。

``combine_videos`` decides clip lengths itself: it slices sources into equal
chunks sized from the narration duration, loops when short and discards the
surplus. That is right for one-click rendering, but once a user has dragged an
in/out point the render must honour exactly that. This module turns a reviewed
scene list into an explicit edit list; the renderer keeps its own path.

Why image clips are special
---------------------------
文生图素材在**生成时**就被烘成了固定时长的 mp4(见
``material._render_openai_image_video``),所以它的源长度就是当初烘的那几秒。
要让一张图在成片里停更久,不能拉伸那段 mp4,必须拿原始 png 按新时长重渲。
原始 png 一直留在任务目录里,而 ``render_image_zoom_video`` 以
``(图片内容, 时长)`` 做内容寻址,所以重渲既可行也自动带缓存。

A generated image is baked into a fixed-length MP4 at generation time, so its
source is only as long as it was baked. Holding it longer means re-rendering
from the original PNG, which is still in the task directory, rather than
stretching the baked MP4.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Iterable

from loguru import logger

from app.services import scene_manifest

# 低于这个长度的片段在成片里基本是一帧闪过，而且 30fps 下不足 6 帧，
# 转场也无从施加。拒绝比渲染出一段看不见的画面更有用。
MIN_CLIP_DURATION = 0.2

# 探测素材时长的结果按"路径 + 修改时间 + 大小"缓存。时间线每次打开都要
# 探测十几个素材，每次探测都要启动一次解码器；用户重生成某一段之后文件
# 变了，这三元组也会变，所以缓存不会把旧时长带到新素材上。
_duration_cache: dict[tuple[str, int, int], float] = {}

# 图片的内容寻址摘要同样按"路径 + 修改时间 + 大小"缓存。一次渲染要为十几
# 个场景各找一次原图，不缓存就是"场景数 × 图片数"次解码：26 张图、12 个
# 场景是 312 次,而其中 286 次算的是同一批摘要。
_image_digest_cache: dict[tuple[str, int, int], str] = {}


class TimelineError(scene_manifest.SceneError):
    """时间线层面的错误。继承 SceneError，沿用既有的 400 映射。"""


def _file_identity(path: str) -> tuple[str, int, int]:
    stat = os.stat(path)
    return (os.path.abspath(path), int(stat.st_mtime_ns), int(stat.st_size))


def probe_duration(path: str) -> float:
    """读取素材时长。

    这里不另起 ffprobe 子进程，而是复用 ``video`` 里那个已经处理过各种坏
    元数据的打开函数：同一个素材在探测和渲染两处必须得到同一个时长，否则
    用户拖到的出点会和渲染结果差一点。
    """
    if not path or not os.path.isfile(path):
        raise TimelineError(f"material is missing from disk: {path}")

    try:
        identity = _file_identity(path)
    except OSError as exc:
        raise TimelineError(f"cannot read material: {path} ({exc})") from exc

    cached = _duration_cache.get(identity)
    if cached is not None:
        return cached

    # 延迟导入：video 会拉起 moviepy 和 numpy，而 timeline 的纯计算函数
    # (normalise_timing 等) 被测试和接口层频繁导入，不该为此付这个代价。
    from app.services import video as video_service

    clip = None
    try:
        clip = video_service._open_video_clip_quietly(path)
        duration = float(clip.duration or 0.0)
    except Exception as exc:
        raise TimelineError(
            f"cannot read the duration of {os.path.basename(path)}: {exc}"
        ) from exc
    finally:
        if clip is not None:
            video_service.close_clip(clip)

    if duration <= 0:
        raise TimelineError(
            f"{os.path.basename(path)} reports a zero duration and cannot be trimmed"
        )

    _duration_cache[identity] = duration
    return duration


def source_image_for(task_id: str, material: str) -> str | None:
    """找回这段图片素材的原始 png，找不到返回 None。

    ``render_image_zoom_video`` 用 ``sha256(绝对路径 + 首帧形状 + 首帧字节)``
    当文件名前缀，所以反过来对任务目录里的每张 png 重算一次这个摘要就能配对。
    代价是打开若干张图片，因此只在用户真的要把某张图拉长时才调用，不在
    列表接口里顺手做。

    配不上就返回 None 而不是抛错：调用方据此把时长夹回源长度并说明原因，
    这比让整次渲染失败有用。
    """
    directory = os.path.dirname(os.path.abspath(material))
    stem = os.path.basename(material).split(".", 1)[0]
    if not stem or len(stem) != 64:
        # 不是内容寻址命名，说明这不是图片烘出来的片段。
        return None

    from app.services import video as video_service

    try:
        candidates = [
            os.path.join(directory, name)
            for name in sorted(os.listdir(directory))
            if os.path.splitext(name)[1].lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
    except OSError:
        return None

    for candidate in candidates:
        try:
            identity = _file_identity(candidate)
        except OSError:
            continue
        cached = _image_digest_cache.get(identity)
        if cached is not None:
            if cached == stem:
                return candidate
            continue

        clip = None
        try:
            clip, resolved = video_service._open_image_clip_with_fallback(candidate)
            digest = hashlib.sha256(os.fsencode(os.path.abspath(resolved)))
            frame = clip.get_frame(0)
            digest.update(str(frame.shape).encode())
            digest.update(frame.tobytes())
            if clip.mask is not None:
                digest.update(clip.mask.get_frame(0).tobytes())
            hexdigest = digest.hexdigest()
            # 只有解析后的路径与候选路径一致时才缓存：回退打开可能换成另一个
            # 文件，那时这个摘要不属于这个缓存键。
            if os.path.abspath(resolved) == os.path.abspath(candidate):
                _image_digest_cache[identity] = hexdigest
            if hexdigest == stem:
                return resolved
        except Exception:
            continue
        finally:
            if clip is not None:
                video_service.close_clip(clip)
    return None


def _coerce_seconds(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineError(f"{field} must be a number, got {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TimelineError(f"{field} must be a finite number, got {value!r}")
    if number < 0:
        raise TimelineError(f"{field} cannot be negative, got {number}")
    return number


def normalise_timing(
    *,
    source_duration: float,
    trim_start: Any = None,
    duration: Any = None,
    clip_speed: float = 1.0,
) -> dict:
    """把用户拖出来的入点和时长夹进这段素材真实存在的范围。

    ``duration`` 是**画面在成片里停留的时长**,而 ``trim_start``/``trim_end``
    是**源素材上的时间**。两者差一个变速倍率：0.5 倍速播 1 秒源画面会在成片
    里停 2 秒。把换算放在这里,界面和渲染器就不必各算一遍、也不会算歪。

    返回值带 ``clamped``,调用方可以据此告诉用户"你拖的比素材本身还长,已经
    按素材长度收住了",而不是悄悄改掉他的输入。
    """
    try:
        speed = float(clip_speed or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if speed <= 0:
        speed = 1.0

    if source_duration <= 0:
        raise TimelineError("this material has no usable duration")

    start = _coerce_seconds(trim_start, field="trim_start") or 0.0
    clamped = False

    if start >= source_duration:
        # 入点拖到素材之外：收回到还能留下最短片段的位置。
        start = max(0.0, source_duration - MIN_CLIP_DURATION)
        clamped = True

    available_source = source_duration - start
    requested_screen = _coerce_seconds(duration, field="duration")
    if requested_screen is None:
        # 没指定就用剩下的全部，这与"未编辑的场景照旧"一致。
        source_span = available_source
    else:
        if requested_screen < MIN_CLIP_DURATION:
            raise TimelineError(
                f"duration must be at least {MIN_CLIP_DURATION} seconds, "
                f"got {requested_screen}"
            )
        source_span = requested_screen * speed
        if source_span > available_source + 1e-6:
            source_span = available_source
            clamped = True

    if source_span < MIN_CLIP_DURATION:
        raise TimelineError(
            f"only {available_source:.2f}s of this material is left after the "
            f"in-point; at least {MIN_CLIP_DURATION}s is needed"
        )

    end = min(source_duration, start + source_span)
    return {
        "trim_start": round(start, 3),
        "trim_end": round(end, 3),
        "duration": round((end - start) / speed, 3),
        "source_duration": round(source_duration, 3),
        "clamped": clamped,
    }


def ensure_image_length(task_id: str, material: str, needed_source: float) -> str:
    """要把一张图停得比烘出来的片段更久时，按新时长重渲一段。

    找不到原始图片就原样返回那段 mp4，由调用方把时长夹回去。这里不抛错：
    一个"只能停 5 秒"的限制不该让整次渲染失败。
    """
    source_image = source_image_for(task_id, material)
    if not source_image:
        # 网上下的视频走到这里是正常的：它本来就只有那么长。调用方会把出点
        # 夹回素材长度,所以这里说清楚"为什么停不了那么久",而不是报错。
        logger.info(
            f"cannot hold {os.path.basename(material)} for "
            f"{needed_source:.2f}s: no source image to re-render it from, "
            f"task_id: {task_id}"
        )
        return material

    from app.services import video as video_service

    # 取两位小数而不是取整：把 12 段画面铺满 64 秒旁白时每段是 5.33 秒，
    # 取整成 5 秒会凭空少掉 4 秒。两位小数同时也让内容寻址的缓存键稳定。
    target = round(max(MIN_CLIP_DURATION, needed_source), 2)
    # render_image_zoom_video 以 (图片, 时长) 内容寻址，重复请求同一时长
    # 直接命中已有文件，不会重复编码。
    rendered = video_service.render_image_zoom_video(source_image, target)
    logger.info(
        f"re-rendered an image clip to {target:.2f}s for the timeline, "
        f"task_id: {task_id}, file: {os.path.basename(rendered)}"
    )
    return rendered


def build_edit_list(
    task_id: str,
    scenes: Iterable[dict],
    *,
    clip_speed: float = 1.0,
) -> list[dict]:
    """按场景顺序算出渲染器要的剪辑表。

    每一项只有 ``path``/``start``/``end``——这正是 ``process_one_clip`` 已经
    会处理的三个字段,所以渲染器那边不需要新的片段语义,只需要"别再自己切了"。

    没编辑过时长的场景同样会进剪辑表(用素材全长),这样"用户只拖了第 3 段"
    也不会让其余片段回到自动切片逻辑、长度全变。
    """
    edit_list: list[dict] = []
    for position, scene in enumerate(scenes):
        material = scene_manifest.resolve_material(task_id, scene.get("material"))
        source_duration = probe_duration(material)

        requested = scene.get("duration")
        trim_start = scene.get("trim_start") or 0.0
        needed_source = None
        if requested is not None:
            try:
                needed_source = float(requested) * max(float(clip_speed or 1.0), 1e-6)
            except (TypeError, ValueError):
                needed_source = None

        # 图片片段要停更久时先重渲，再按新的源长度算入出点。
        #
        # 判断"能不能拉长"不能只看扩展名：图片素材在生成时就烘成了固定长度
        # 的 mp4(见 material._render_openai_image_video),扩展名一律是
        # .mp4。只按扩展名筛会把每一段图片都当成"拉不长的视频",于是 12 段
        # 5 秒画面永远铺不满 64 秒旁白——用户看到的就是"成片比列表多一段、
        # 第一张图出现两次"。
        #
        # 所以条件是"不是视频文件,或者能找回它的原图"。``source_image_for``
        # 认的是内容寻址的文件名,网上下的视频对不上,会立刻返回 None,那一段
        # 仍然按素材长度夹回去。
        #
        # Whether a clip can be held longer is not an extension question:
        # generated images are baked to a fixed-length MP4, so they all end in
        # .mp4. Sniffing the extension alone treats every picture as an
        # unstretchable video, and twelve five-second clips can then never
        # cover a 64s narration. The test is therefore "not a video file, or
        # its source image is still on disk" — a downloaded video matches
        # neither and stays clamped to its own length.
        if needed_source is not None and (
            needed_source > source_duration - float(trim_start) + 1e-6
        ) and (
            scene_manifest.material_kind(material) != "video"
            or source_image_for(task_id, material) is not None
        ):
            extended = ensure_image_length(
                task_id, material, float(trim_start) + needed_source
            )
            if extended != material:
                material = extended
                source_duration = probe_duration(material)

        timing = normalise_timing(
            source_duration=source_duration,
            trim_start=trim_start,
            duration=requested,
            clip_speed=clip_speed,
        )
        if timing["clamped"]:
            logger.warning(
                f"scene {position} timing was clamped to the material length, "
                f"task_id: {task_id}, material: {os.path.basename(material)}"
            )
        edit_list.append(
            {
                "path": material,
                "start": timing["trim_start"],
                "end": timing["trim_end"],
            }
        )

    if not edit_list:
        raise TimelineError("the timeline has no scenes to render")
    return edit_list


def timeline_total(edit_list: Iterable[dict], *, clip_speed: float = 1.0) -> float:
    """剪辑表在成片里的总时长（已折算变速）。"""
    try:
        speed = max(float(clip_speed or 1.0), 1e-6)
    except (TypeError, ValueError):
        speed = 1.0
    return sum(
        max(0.0, float(item.get("end", 0)) - float(item.get("start", 0))) / speed
        for item in edit_list
    )


def _natural_length(
    task_id: str,
    scene: dict,
    *,
    clip_speed: float,
    clip_duration: float | None,
    probe: bool = True,
) -> float:
    """一个场景在"没人拖过时间线"时的自然长度。

    这是渲染器自动切片会给它的长度：素材本身的长度（扣掉入点、折算变速），
    再被 ``max_clip_duration`` 截一刀。读不出素材时退回请求的单段时长，而
    不是抛错——时间线上少一段画面比显示一个近似时长更糟。

    ``probe`` 关掉时一律按请求的单段时长估算，不启动解码器。这只在"某段素材
    本身短于 max_clip_duration"时与真实值不同：图片素材是按这个时长烘出来
    的，网上下的视频通常更长，所以估算值在绝大多数任务里就是真实值。审片
    列表走这条路（它每次页面重跑都要算一遍），时间线编辑器则真探测。
    """
    timing = scene_manifest.scene_timing(scene)
    if timing["duration"] is not None:
        return max(MIN_CLIP_DURATION, float(timing["duration"]))

    if not probe:
        return max(MIN_CLIP_DURATION, float(clip_duration or MIN_CLIP_DURATION))

    try:
        material = scene_manifest.resolve_material(task_id, scene.get("material"))
        source_duration = probe_duration(material)
    except scene_manifest.SceneError as exc:
        logger.warning(
            f"cannot measure scene {scene.get('index')} of {task_id}: {exc}; "
            "assuming the requested clip length"
        )
        return max(MIN_CLIP_DURATION, float(clip_duration or MIN_CLIP_DURATION))

    held = (source_duration - timing["trim_start"]) / clip_speed
    if clip_duration:
        held = min(held, float(clip_duration))
    return max(MIN_CLIP_DURATION, held)


def cycle_windows(windows: list[dict], target: Any = 0.0) -> list[dict]:
    """把一组时间段按顺序重播到铺满旁白，每一项带上它属于哪个场景。

    画面在"一张图最多停多久"的上限之内铺不满旁白时会整组轮播，于是第 i
    段画面在成片里出现不止一次。旁白分配必须按这条真实时间线来做：只看
    第一轮的话，第一轮之后的每一句都没有任何时间段与之重叠，会全部堆到
    最后一个场景上——用户看到的就是最后一段画面挂着半篇旁白。

    When the pictures cycle, scene *i* appears more than once, and narration
    has to be split over that real timeline. Looking only at the first pass
    leaves every later line with no overlapping window, so they all pile onto
    the last scene.
    """
    windows = list(windows or [])
    if not windows:
        return []
    try:
        limit = max(0.0, float(target or 0.0))
    except (TypeError, ValueError):
        limit = 0.0

    cycled: list[dict] = []
    elapsed = float(windows[0]["start"])
    position = 0
    # 每段至少 MIN_CLIP_DURATION，所以轮数有限；这里再加一道硬上限，
    # 防止某段算出 0 长度时转不出去。
    guard = max(len(windows) * 100, len(windows))
    while position < guard:
        source = windows[position % len(windows)]
        length = max(0.0, float(source["end"]) - float(source["start"]))
        cycled.append(
            {
                "start": round(elapsed, 3),
                "end": round(elapsed + length, 3),
                "scene": position % len(windows),
            }
        )
        elapsed += length
        position += 1
        covered = not limit or elapsed >= limit - MIN_CLIP_DURATION
        if position >= len(windows) and covered:
            break
        if length <= 0 and position >= len(windows):
            break
    return cycled


def plan_windows(
    task_id: str,
    scenes: Iterable[dict],
    *,
    audio_duration: Any = 0.0,
    clip_speed: Any = 1.0,
    clip_duration: Any = None,
    probe: bool = True,
) -> list[dict]:
    """每个场景在成片里实际占据的时间段。

    Why this exists
    ---------------
    场景列表原来按下标把第 i 段画面配第 i 句旁白。关键词个数和旁白句数一旦
    不同——12 个关键词配 28 句旁白是常态——这个配对就是错的：第 7 段画面
    在成片里播在 30 秒处，那里念的是第 16 句，界面却写着第 7 句。用户看到
    的"场景"和他听到的声音对不上，于是以为渲染器换掉了图片。

    The scene list used to pair picture *i* with narration line *i*. The term
    count and the line count are rarely equal — 12 keywords against 28 lines
    is typical — and then the pairing is simply wrong: picture 7 plays at
    0:30, where line 16 is spoken, while the list claims line 7. Nothing is
    out of order in the render; the list is lying about it.

    没人拖过时间线时这里按比例铺满旁白，与 ``auto_fit_edit_list`` 用的是同
    一套数字，所以界面上写的时间段就是渲染器真正会用的时间段。拖过之后按
    拖出来的长度算，不再缩放——那才是用户要的结果。
    """
    scenes = list(scenes or [])
    if not scenes:
        return []

    try:
        speed = max(float(clip_speed or 1.0), 1e-6)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        target = max(0.0, float(audio_duration or 0.0))
    except (TypeError, ValueError):
        target = 0.0
    try:
        cap = float(clip_duration) if clip_duration else None
    except (TypeError, ValueError):
        cap = None

    lengths = [
        _natural_length(
            task_id, scene, clip_speed=speed, clip_duration=cap, probe=probe
        )
        for scene in scenes
    ]

    # 拖过时间线的任务按它拖出来的长度渲染，不缩放；没拖过的按比例铺满旁白。
    #
    # 放大时绝不越过 ``max_clip_duration``。那是用户填的"一张图最多停多久"
    # 的上限,不是起点：12 张图配 72 秒旁白时按比例要停 6 秒,而用户明明填的
    # 是 3 秒。铺不满就让它铺不满——缺口由 ``auto_fit_edit_list`` 轮播补齐,
    # 界面也会如实说"每张图播了两遍",这比悄悄把 3 秒变成 6 秒诚实。
    # 缩小不受此限：28 张图配 72 秒旁白时每张 2.57 秒,本来就在上限以内。
    #
    # Scaling up never crosses max_clip_duration. It is the ceiling the user
    # typed for how long one picture may hold, not a starting point: twelve
    # pictures across a 72s narration want six seconds each when the user
    # asked for three. Falling short is the honest outcome — the gap is
    # covered by cycling in ``auto_fit_edit_list`` and stated in the UI.
    # Scaling down is unaffected; it stays under the ceiling by definition.
    total = sum(lengths)
    if not scene_manifest.has_timing_edits(scenes) and target > 0 and total > 0:
        factor = target / total
        lengths = [
            min(length * factor, cap) if cap and factor > 1 else length * factor
            for length in lengths
        ]

    windows: list[dict] = []
    elapsed = 0.0
    for length in lengths:
        windows.append({"start": round(elapsed, 3), "end": round(elapsed + length, 3)})
        elapsed += length
    return windows


def _cycle_to_cover(
    edit_list: list[dict], target: float, speed: float
) -> list[dict]:
    """按顺序重播这组片段，直到铺满旁白；最后一段裁到正好收尾。

    返回的仍然是一张"所见即所得"的剪辑表：界面上有几张图,成片里就是这几
    张图按顺序轮播,而不是渲染器自己随机挑几段补。
    """
    covered = timeline_total(edit_list, clip_speed=speed)
    if covered <= 0:
        return edit_list

    filled = list(edit_list)
    position = 0
    while target - covered > MIN_CLIP_DURATION:
        item = dict(edit_list[position % len(edit_list)])
        length = (float(item["end"]) - float(item["start"])) / speed
        remaining = target - covered
        if length > remaining:
            item["end"] = round(float(item["start"]) + remaining * speed, 3)
            length = remaining
        filled.append(item)
        covered += length
        position += 1

    logger.info(
        f"{len(edit_list)} scenes cover {timeline_total(edit_list, clip_speed=speed):.2f}s "
        f"of a {target:.2f}s narration at the configured clip length; "
        f"cycling them to {len(filled)} clips rather than holding each longer"
    )
    return filled


def auto_fit_edit_list(
    task_id: str,
    scenes: Iterable[dict],
    *,
    audio_duration: Any = 0.0,
    clip_speed: Any = 1.0,
    clip_duration: Any = None,
) -> list[dict]:
    """把场景按 ``plan_windows`` 的时间段铺满旁白，返回剪辑表。

    没有这一步时，渲染器会把 12 段 5 秒画面拼成 60 秒，然后把第 1 段再接到
    64 秒旁白的尾巴上。用户在列表里看到 12 段，成片里却看到 13 段、而且第一
    张图出现两次。按时间段渲染之后，列表有几段成片就有几段。

    Without this the renderer lays 12 five-second clips end to end for 60s and
    then loops clip 1 to cover a 64s narration: the list shows 12 pictures and
    the video shows 13, with the first one twice. Rendering the planned
    windows means the list and the video hold the same scenes.
    """
    scenes = list(scenes or [])
    windows = plan_windows(
        task_id,
        scenes,
        audio_duration=audio_duration,
        clip_speed=clip_speed,
        clip_duration=clip_duration,
    )
    planned = []
    for scene, window in zip(scenes, windows):
        held = round(window["end"] - window["start"], 3)
        planned.append({**scene, "duration": held})

    try:
        speed = max(float(clip_speed or 1.0), 1e-6)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        target = max(0.0, float(audio_duration or 0.0))
    except (TypeError, ValueError):
        target = 0.0
    try:
        cap = float(clip_duration) if clip_duration else None
    except (TypeError, ValueError):
        cap = None

    edit_list = build_edit_list(task_id, planned, clip_speed=speed)
    if not target or not planned:
        return edit_list

    # 上限之内铺不满旁白时轮播，而不是把每张图停得更久。
    #
    # 用户把"一张图最多停多久"填成 3 秒,12 张图就只有 36 秒,旁白却有 72
    # 秒。把每张拉到 6 秒等于无视他填的数字；轮播则是旧渲染器一直以来的做
    # 法,区别只在于现在这件事会被明说出来,而不是悄悄多出一段。
    #
    # When the pictures cannot cover the narration within the ceiling, cycle
    # them rather than holding each one longer. Three seconds times twelve is
    # thirty-six against a seventy-two second narration; stretching to six
    # would ignore the number the user typed.
    if sum(float(item.get("duration") or 0.0) for item in planned) + MIN_CLIP_DURATION < target:
        return _cycle_to_cover(edit_list, target, speed)

    # 把差额补在最后一段上。
    #
    # 重渲一张图得到的时长会被帧率量化：要 5.33 秒，30fps 下拿到的是 5.30
    # 秒。12 段各差 0.03 秒,加起来就是 0.4 秒——旁白最后那半句会被切掉。
    # 这里把累计误差一次性加到最后一段,而不是让它分散在每段上。
    #
    # Re-rendering an image quantises to whole frames, so twelve clips each a
    # frame short add up to a cut-off final line. The accumulated error goes
    # onto the last clip in one go.
    for _ in range(3):
        gap = target - timeline_total(edit_list, clip_speed=speed)
        if abs(gap) <= 0.05:
            break
        held = float(planned[-1].get("duration") or 0.0)
        adjusted = round(max(MIN_CLIP_DURATION, held + gap), 3)
        if cap and adjusted > cap:
            # 补误差也不能越过用户填的上限。
            adjusted = round(float(cap), 3)
        if abs(adjusted - held) < 1e-3:
            break
        planned[-1] = {**planned[-1], "duration": adjusted}
        # 只重算最后一段：重算整张剪辑表会把前面十几段都再探测、再找一次
        # 原图,而它们的时长根本没变。
        retried = edit_list[:-1] + build_edit_list(
            task_id, [planned[-1]], clip_speed=speed
        )
        if abs(target - timeline_total(retried, clip_speed=speed)) >= abs(gap):
            # 没变短也没变长（素材拉不动了），就别再试第三次。
            break
        edit_list = retried
    return edit_list
