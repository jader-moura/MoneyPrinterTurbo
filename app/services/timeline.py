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
        clip = None
        try:
            clip, resolved = video_service._open_image_clip_with_fallback(candidate)
            digest = hashlib.sha256(os.fsencode(os.path.abspath(resolved)))
            frame = clip.get_frame(0)
            digest.update(str(frame.shape).encode())
            digest.update(frame.tobytes())
            if clip.mask is not None:
                digest.update(clip.mask.get_frame(0).tobytes())
            if digest.hexdigest() == stem:
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
        logger.warning(
            "cannot extend an image clip without its source image, "
            f"task_id: {task_id}, material: {os.path.basename(material)}"
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
        if (
            needed_source is not None
            and needed_source > source_duration - float(trim_start) + 1e-6
            and scene_manifest.material_kind(material) != "video"
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
