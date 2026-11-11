"""场景清单：把流水线中间产物整理成可逐条检查、替换和重排的场景列表。

Why this exists
---------------
``_run_pipeline`` 产出的是几个平行列表：脚本、关键词、素材文件路径。想在渲染
之前让用户逐个检查并替换某一张图,就需要一个把它们对齐成"场景"的结构,并且
这个结构必须能落盘——审片可能跨越 WebUI 重启,内存状态不可依赖。

The pipeline produces parallel lists: a script, keywords, and material files.
Reviewing a run before it renders means pairing those into *scenes*, and the
pairing has to live on disk: a review can span a WebUI restart, so in-memory
task state is not enough. The scene list is stored in the task's existing
``script.json`` manifest, which is already written atomically under a lock.

What a scene is, and what it is not
-----------------------------------
场景记录的是"这一段用哪个素材、念哪句话",不记录它在成片里的精确时间。真正
的时长由 ``generate_final_videos`` 根据配音总长、拼接模式和转场计算,清单里
写一个"每段目标时长"只会让界面显示一个并不成立的数字。``clip_duration``
因此只是请求值,不是结果。

A scene says which material covers which narration line. It deliberately does
not claim an on-screen duration: the real timing is computed by
``generate_final_videos`` from the narration length, the concat mode and the
transitions, so a per-scene duration stored here would be a number the UI
displays and the render ignores. ``clip_duration`` is the *requested* value
only.

Trust boundary
--------------
清单是磁盘文件,HTTP 调用方也能间接影响它。读取时每个素材路径都必须重新
限定在任务目录内,不能因为"是我们自己写的"就直接拿去渲染。

The manifest is a file on disk and HTTP callers can influence it, so every
material path is re-resolved inside the task directory on the way out. Being
written by us earlier is not evidence it is still safe to open.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from loguru import logger

from app.services import task_artifacts
from app.utils import file_security, subtitle_reader, utils

# 与 ``_run_pipeline`` 的阶段顺序严格一致。``stop_at`` 在哪里停,
# ``start_from`` 就能从哪里继续。
# Exactly the stage order ``_run_pipeline`` runs in: whatever ``stop_at`` can
# stop after, ``start_from`` can resume at.
PIPELINE_STAGES = ("script", "terms", "audio", "subtitle", "materials", "video")

# 审片恢复渲染需要的产物。缺任何一个都不能静默重新生成。
# What resuming a render needs. A missing one is an error, never a quiet re-run.
RESUME_FIELDS = ("audio_file", "audio_duration", "subtitle_path")

_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".flv", ".wmv"}
)

_SCENES_KEY = "scenes"
_STALE_KEY = "audio_stale"


class SceneError(ValueError):
    """清单结构或请求参数不合法。调用方负责翻译成 HTTP 状态码。"""


def stage_index(stage: Any, *, field: str = "stage") -> int:
    """返回阶段序号；未知阶段抛出 ``SceneError`` 而不是静默回退到默认值。"""
    text = str(stage or "").strip().lower()
    if text not in PIPELINE_STAGES:
        raise SceneError(
            f"unknown {field}: {stage!r}; expected one of {', '.join(PIPELINE_STAGES)}"
        )
    return PIPELINE_STAGES.index(text)


def runs_stage(stage: str, start_from: str) -> bool:
    """该阶段是否需要真正执行（而不是从清单里读回上一次的产物）。"""
    return stage_index(stage) >= stage_index(start_from, field="start_from")


def material_kind(path: str) -> str:
    """按扩展名判断素材类型；无法判断时返回 ``unknown``。

    这里不复用 ``material_upload._material_kind``：那个函数对未知格式抛异常,
    适合校验上传,但清单里出现一个无法识别的旧文件不应该让整个审片界面打不开。
    """
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    return "unknown"


def split_narration(video_script: Any) -> list[str]:
    """把脚本切成与字幕一致的句子列表。

    复用字幕阶段用的同一个切分函数,这样编辑界面里显示的句子和观众最终看到
    的字幕是同一套断句,不会出现"界面上第三句"对不上"画面里第三句"。
    """
    text = str(video_script or "").strip()
    if not text:
        return []
    lines = utils.split_string_by_punctuations(text)
    return [line for line in (part.strip() for part in lines) if line]


def normalise_terms(video_terms: Any) -> list[str]:
    """关键词可能是列表,也可能是逗号分隔的字符串，两种都要能读。"""
    if isinstance(video_terms, str):
        candidates: Iterable[Any] = video_terms.split(",")
    elif isinstance(video_terms, (list, tuple)):
        candidates = video_terms
    else:
        return []
    return [term for term in (str(item).strip() for item in candidates) if term]


def build_scenes(
    *,
    video_script: Any,
    video_terms: Any,
    materials: Iterable[Any],
    video_source: str = "",
    clip_duration: Any = None,
) -> list[dict]:
    """把脚本、关键词和素材对齐成场景列表。

    场景数量由素材决定,因为每个素材才真正对应成片里的一段画面；关键词和
    句子数量与它不一致时按位置配对,配不上的留空而不是循环复用——界面上一个
    空关键词是看得见的提示,一个错位的关键词只会让用户以为模型画错了。

    ``match_materials_to_script`` 打开时关键词顺序本身就是脚本叙事顺序
    (见 ``task.generate_terms`` 的注释),所以按位置配对就是正确配对；关闭时
    顺序无意义,配对也就只是展示上的近似,不影响渲染。
    """
    lines = split_narration(video_script)
    terms = normalise_terms(video_terms)
    requested_duration = _coerce_duration(clip_duration)

    scenes: list[dict] = []
    for index, material in enumerate(materials or []):
        path = str(material or "").strip()
        if not path:
            continue
        scenes.append(
            {
                "index": len(scenes),
                "id": new_scene_id(),
                "keyword": terms[index] if index < len(terms) else "",
                "narration": lines[index] if index < len(lines) else "",
                "material": path,
                "kind": material_kind(path),
                "source": str(video_source or ""),
                "clip_duration": requested_duration,
                "locked": False,
            }
        )
    return scenes


def build_planned_scenes(
    *,
    video_script: Any,
    video_terms: Any,
    audio_duration: Any,
    clip_duration: Any,
    video_source: str = "",
) -> list[dict]:
    """还没出图时的场景列表：一段一个位置，写着**将要**画什么。

    为什么要有它：出图是整条流水线里最慢最贵的一步（本机一张几分钟，按张
    计费的源按张烧钱）。在这之前，用户唯一能看到提示词的时机是图都画完
    之后——那时候改一个字就意味着整批重画。先把位置摆出来、提示词填进去，
    用户看过、改过、点了头再开画。

    张数由旁白长度和一张图停多久决定，不由模型吐了几个关键词决定，并且和
    ``material._terms_to_cover`` 用同一个算法：用户在这里看到并批准的那一串
    提示词，就是真开画时逐张送出去的那一串，不能是另一串。

    关键词比张数少时从头轮播（同一个关键词再画一次得到的是另一张图）。
    台词这里不填：它是按时间算出来的，``describe`` 每次都会照当时的时间线
    重算，存一份只会在用户改了时长之后变成错的。

    The slowest, most expensive step is drawing, so the prompts are shown
    before it rather than after, when changing one word would mean redrawing
    everything. How many pictures follows from the narration length and how
    long one picture holds - the same rule ``material._terms_to_cover`` uses,
    so the list approved here is the list that gets drawn.
    """
    terms = normalise_terms(video_terms)
    hold = _coerce_duration(clip_duration) or 1.0
    try:
        required = max(0.0, float(audio_duration or 0.0))
    except (TypeError, ValueError):
        required = 0.0
    needed = max(1, math.ceil(required / hold)) if required > 0 else len(terms)
    needed = max(needed, len(terms))
    if not terms:
        return []

    return [
        {
            "index": position,
            "id": new_scene_id(),
            "keyword": terms[position % len(terms)],
            "narration": "",
            "material": "",
            "kind": "",
            "source": str(video_source or ""),
            "clip_duration": hold,
            "locked": False,
        }
        for position in range(needed)
    ]


def record_planned_scenes(
    task_id: str,
    *,
    video_script: Any,
    video_terms: Any,
    audio_duration: Any,
    clip_duration: Any,
    audio_file: Any,
    subtitle_path: Any,
    video_source: str = "",
) -> bool:
    """把"等着出图"的那份场景清单连同配音、字幕一起落盘。

    和 ``record_prepared_artifacts`` 写在一次 patch 里是同一个理由：不能出现
    "场景已经摆好、配音还是上一轮"的中间状态。
    """
    scenes = build_planned_scenes(
        video_script=video_script,
        video_terms=video_terms,
        audio_duration=audio_duration,
        clip_duration=clip_duration,
        video_source=video_source,
    )
    return task_artifacts.patch_script_data(
        task_id,
        scenes=scenes,
        script=str(video_script or ""),
        search_terms=normalise_terms(video_terms),
        audio_file=str(audio_file or "") or None,
        audio_duration=audio_duration,
        subtitle_path=str(subtitle_path or "") or None,
        **{_STALE_KEY: False},
    )


def pending_scenes(scenes: Iterable[Any]) -> list[dict]:
    """还没有画面的那几段。"""
    return [
        scene for scene in scenes or []
        if not str((scene or {}).get("material") or "").strip()
    ]


def _coerce_duration(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def scene_id(record: Mapping | Any) -> str:
    """这一段画面的身份，跟着它走，不跟着它排第几走。

    下标不是身份。排队出图要几分钟，而用户在这几分钟里干的事恰恰是接着
    审片——拖动、删除、插入。拿下标认人，等图出来时它早就指向别的画面了。

    老任务的清单里没有这个字段，所以认素材文件名：那对一条已经出好图的
    片子来说是稳定的，而且不用改写用户的清单就能用。两样都没有（还没出图
    的占位场景，只可能出自新流程）时交给调用方生成一个。

    An index is not an identity: a queued job waits minutes, and what the
    user does in those minutes is reorder and delete. Older manifests have
    no id, so the material file name serves - stable for a task whose
    pictures exist, and it needs no rewrite of what is already on disk.
    """
    data = record if isinstance(record, Mapping) else {}
    existing = str(data.get("id") or "").strip()
    if existing:
        return existing
    return material_key(str(data.get("material") or ""))


def material_key(path: Any) -> str:
    """素材的身份：只认文件名那一截。

    同一个文件在几处写法不同——清单和队列记的是整条路径，时间线接口发给
    界面的是文件名。要对得上，就都退到文件名为止。
    """
    text = str(path or "")
    cut = max(text.rfind("/"), text.rfind("\\"))
    return text[cut + 1:] if cut >= 0 else text


def new_scene_id() -> str:
    """给一条新场景发一个身份。"""
    return uuid.uuid4().hex[:12]


def _normalise_scene(raw: Any, index: int) -> dict:
    """把清单里的一条记录整理成完整场景；缺字段补默认值,不抛异常。"""
    record = raw if isinstance(raw, Mapping) else {}
    path = str(record.get("material") or "").strip()
    scene = {
        "index": index,
        "id": scene_id(record),
        "keyword": str(record.get("keyword") or ""),
        "narration": str(record.get("narration") or ""),
        "material": path,
        "kind": str(record.get("kind") or "") or material_kind(path),
        "source": str(record.get("source") or ""),
        "clip_duration": _coerce_duration(record.get("clip_duration")),
        "locked": bool(record.get("locked")),
    }
    # 画风同样只在用户显式挑过之后才出现，和下面的时间线字段同一个道理：
    # 无条件补一个默认值会让每条老记录都长出一个新键，而丢掉它会让用户在
    # 编辑器里挑的画风存不下来。
    style = str(record.get("image_style") or "").strip()
    if style:
        scene["image_style"] = style
    # 这一段自己挑过的转场。空（不存在）表示"跟整条片子一样"，这和显式选了
    # "none"（这一刀不要转场）不是一回事，所以用"有才带"而不是补默认值。
    transition = str(record.get("transition") or "").strip()
    if transition:
        scene["transition"] = transition
    # 这一段自己挑的运镜。素材本身已经是按它烘出来的，这里存的是"用户挑过
    # 什么"，界面要靠它把下拉框停在正确的位置上。
    motion = str(record.get("image_motion") or "").strip()
    if motion:
        scene["image_motion"] = motion
    # 切到这一段时响的那一声。文件名由 transition_sound 音效库校验，这里只
    # 原样存；"none" 是"这一刀不要响"，不存在则跟着整条片子走。
    cut_sound = str(record.get("cut_sound") or "").strip()
    if cut_sound:
        scene["cut_sound"] = cut_sound
    # 用户亲手改过这一段的台词。按"有才带"处理，理由同上。
    #
    # 为什么要记：台词平时是按时间算出来的（这段画面播放时真正会念到的那
    # 几句），画面一重排、一删、一改时长就会重新算一遍。算出来的东西不该
    # 盖掉用户自己写的字，所以改过的那一段要标出来，重算时跳过它。
    if record.get("narration_edited"):
        scene["narration_edited"] = True
    # 时间线字段只在用户真的设置过之后才出现，所以这里按"有才带"处理：
    # 这个函数同时跑在读和写两条路上，无条件补默认值会让每条老记录都长出
    # 两个新键,而无条件丢掉则会让拖好的入点存不进去。
    for key in ("trim_start", "duration"):
        value = record.get(key)
        if value is None:
            continue
        try:
            scene[key] = round(float(value), 3)
        except (TypeError, ValueError):
            # 坏值按"没设置过"处理，这与 scene_timing 的容错一致。
            continue
    return scene


def load_raw(task_id: str) -> dict:
    """返回整份清单原文，供需要读 ``params`` 这类非场景字段的调用方使用。"""
    return task_artifacts.read_script_data(task_id)


def load(task_id: str) -> dict:
    """读取场景清单。没有场景时返回 ``{"scenes": [], ...}`` 而不是报错。"""
    payload = task_artifacts.read_script_data(task_id)
    raw_scenes = payload.get(_SCENES_KEY)
    scenes = (
        [_normalise_scene(raw, index) for index, raw in enumerate(raw_scenes)]
        if isinstance(raw_scenes, list)
        else []
    )
    return {
        "scenes": scenes,
        "audio_stale": bool(payload.get(_STALE_KEY)),
        "script": str(payload.get("script") or ""),
        "search_terms": payload.get("search_terms"),
        "audio_file": payload.get("audio_file"),
        "audio_duration": payload.get("audio_duration"),
        "subtitle_path": payload.get("subtitle_path"),
    }


def save_scenes(task_id: str, scenes: list[dict], **extra: Any) -> bool:
    """写回场景列表并重排 ``index``，使其始终等于位置。"""
    ordered = [_normalise_scene(scene, index) for index, scene in enumerate(scenes)]
    return task_artifacts.patch_script_data(task_id, scenes=ordered, **extra)


def record_prepared_artifacts(
    task_id: str,
    *,
    video_script: Any,
    video_terms: Any,
    materials: Iterable[Any],
    audio_file: Any,
    audio_duration: Any,
    subtitle_path: Any,
    video_source: str = "",
    clip_duration: Any = None,
) -> bool:
    """素材阶段结束时落盘一切恢复渲染和审片需要的东西。

    写在一次 patch 里,是为了不出现"场景已经更新、配音还是上一轮"的中间状态。
    失败只记录警告：清单是审片功能的入口,写不成最多让审片不可用,不应该把
    一次已经花掉算力的生成判为失败。
    """
    scenes = build_scenes(
        video_script=video_script,
        video_terms=video_terms,
        materials=materials,
        video_source=video_source,
        clip_duration=clip_duration,
    )
    return task_artifacts.patch_script_data(
        task_id,
        scenes=scenes,
        audio_file=str(audio_file or "") or None,
        audio_duration=audio_duration,
        subtitle_path=str(subtitle_path or "") or None,
        **{_STALE_KEY: False},
    )


def _resolve_within(
    directories: list[str], path: Any, *, require_file: bool, label: str
) -> str:
    candidate = str(path or "").strip()
    if not candidate:
        raise SceneError(f"no {label} recorded")

    for base_dir in directories:
        if not base_dir:
            continue
        try:
            return file_security.resolve_path_within_directory(
                base_dir, candidate, require_file=require_file
            )
        except ValueError:
            continue
    raise SceneError(f"{label} is missing or outside the task directory")


def resolve_task_file(task_id: str, path: Any, *, require_file: bool = True) -> str:
    """把清单里的任务产物路径（配音、字幕）重新限定在任务目录内。"""
    return _resolve_within(
        [utils.task_dir(task_id)], path, require_file=require_file, label="file"
    )


def resolve_material(task_id: str, path: Any, *, require_file: bool = True) -> str:
    """把清单里的素材路径重新限定在任务目录内。

    素材也可能来自公共上传目录（"replace with my own file" 走的就是那里）,
    所以两个白名单目录都试。任何越界路径一律拒绝,不区分它是否存在,避免把
    这个接口变成探测宿主机文件的工具。
    """
    return _resolve_within(
        [utils.task_dir(task_id), _uploaded_material_dir()],
        path,
        require_file=require_file,
        label="material file",
    )


def _uploaded_material_dir() -> str:
    """共享上传目录；导入失败时退化为"只允许任务目录"。"""
    try:
        from app.services import material_upload

        return material_upload.uploaded_material_dir(create=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"uploaded material directory unavailable: {exc}")
        return ""


def materials_for_render(task_id: str) -> list[str]:
    """按场景顺序返回素材绝对路径，供恢复渲染使用。

    渲染必须读这里而不是读 ``materials``：用户重排或替换过的结果只存在于场景
    列表里,从旧列表渲染会悄悄丢掉所有编辑。
    """
    scenes = load(task_id)["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )
    return [resolve_material(task_id, scene["material"]) for scene in scenes]


def edit_list_for_render(task_id: str) -> list[dict]:
    """读回剪辑表并解析成绝对路径，没有剪辑表时返回空列表。

    返回空列表的含义是"这条任务没有时间线编辑",渲染器据此走原本的自动切片
    逻辑。任何一项素材解析不了就报错点名,而不是跳过那一段——时间线上少一段
    画面,用户只会以为渲染器把它吞了。
    """
    payload = load_raw(task_id)
    entries = payload.get("edit_list")
    if not isinstance(entries, list) or not entries:
        return []

    resolved: list[dict] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SceneError(f"edit list entry {position} is not an object")
        path = resolve_material(task_id, entry.get("material"))
        try:
            start = float(entry.get("start") or 0.0)
            end = float(entry.get("end"))
        except (TypeError, ValueError) as exc:
            raise SceneError(
                f"edit list entry {position} has an unreadable time range"
            ) from exc
        if end <= start:
            raise SceneError(
                f"edit list entry {position} has an empty range "
                f"({start:.2f}s to {end:.2f}s)"
            )
        item = {"path": path, "start": start, "end": end}
        # 这一段自己挑的转场，没挑过就不带这个键。渲染器据此决定是用它还是
        # 用整条片子的那一个设置。
        transition = str(entry.get("transition") or "").strip()
        if transition:
            item["transition"] = transition
        cut_sound = str(entry.get("cut_sound") or "").strip()
        if cut_sound:
            item["cut_sound"] = cut_sound
        resolved.append(item)
    return resolved


_SRT_STAMP = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _stamp_seconds(parts: tuple) -> float:
    hours, minutes, seconds, millis = (int(value) for value in parts)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def narration_segments(task_id: str) -> list[dict]:
    """旁白按句切分后的时间点，取自任务的字幕文件。

    配音本身是逐句合成的，字幕里的每一条恰好对应其中一句，所以这就是现成的
    "旁白分块"。时间线把它们逐块画出来，用户才看得出哪句话落在哪一段画面上
    ——只画一整条长条等于把这个信息丢掉。

    没有字幕文件（例如关掉了字幕）时返回空列表，由调用方退回整条显示。
    """
    payload = load_raw(task_id)
    try:
        path = resolve_task_file(task_id, payload.get("subtitle_path"))
    except SceneError:
        return []

    segments: list[dict] = []
    try:
        cues = subtitle_reader.file_to_subtitles(path)
    except Exception as exc:  # pragma: no cover - 字幕损坏不该让时间线打不开
        logger.warning(f"cannot read narration segments for {task_id}: {exc}")
        return []

    for _, times, text in cues:
        stamps = _SRT_STAMP.findall(times or "")
        if len(stamps) != 2:
            continue
        start = _stamp_seconds(stamps[0])
        end = _stamp_seconds(stamps[1])
        if end <= start:
            continue
        segments.append(
            {"start": round(start, 3), "end": round(end, 3), "text": str(text or "")}
        )
    return segments


def scene_timing(scene: Any) -> dict:
    """读出一个场景的时间线设置，缺字段按"没编辑过"处理。

    ``trim_start``/``duration`` 不由 ``build_scenes`` 写入,只有用户真的在
    时间线上拖过之后才出现。因此这里必须按缺省读：老任务的 ``script.json``
    里根本没有这两个键,直接取下标会让整个审片界面打不开。
    """
    record = scene if isinstance(scene, dict) else {}
    trim_start = record.get("trim_start")
    duration = record.get("duration")
    try:
        trim_start = float(trim_start) if trim_start is not None else 0.0
    except (TypeError, ValueError):
        trim_start = 0.0
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {"trim_start": max(0.0, trim_start), "duration": duration}


def has_timing_edits(scenes: Any) -> bool:
    """这条任务上有没有人动过时间线。

    这是"走不走剪辑表"的开关：一次都没拖过就继续用原来的自动切片逻辑,
    于是默认出片结果一个字节都不会变。只要有一段被拖过,就整条按剪辑表渲染
    ——否则没拖过的片段会回到自动切片、长度全变一遍。
    """
    for scene in scenes or []:
        timing = scene_timing(scene)
        if timing["duration"] is not None or timing["trim_start"] > 0:
            return True
    return False


def has_render_overrides(scenes: Any) -> bool:
    """这条任务要不要按剪辑表渲染。

    拖过时间线要；某一段自己挑过转场也要——每段的转场只能通过剪辑表交给
    渲染器(``process_one_clip`` 读的就是剪辑表那一项)。自动切片那条路上
    没有"这一段"的概念,一段源素材会被切成好几段画面。

    一样都没有时返回 False，任务继续走原来的自动切片逻辑，默认出片结果一个
    字节都不会变。
    """
    if has_timing_edits(scenes):
        return True
    return any(
        str(scene.get("transition") or "").strip()
        or str(scene.get("cut_sound") or "").strip()
        for scene in scenes or []
    )


def patch_scene_timing(
    task_id: str,
    index: int,
    *,
    trim_start: Any = None,
    duration: Any = None,
) -> dict:
    """把一个场景的入点与时长写回清单，返回改完的场景。

    ``duration`` 显式传 ``0`` 或负数没有意义,这里不做夹取——调用方
    (``scene_editor.update_timing``)已经用 ``timeline.normalise_timing``
    按素材真实长度算过一遍,到这里只负责落盘。
    """
    scenes = load(task_id)["scenes"]
    if not 0 <= index < len(scenes):
        raise SceneError(
            f"scene {index} does not exist; this task has {len(scenes)} scenes"
        )
    scene = scenes[index]
    if trim_start is not None:
        scene["trim_start"] = round(float(trim_start), 3)
    if duration is not None:
        scene["duration"] = round(float(duration), 3)
    save_scenes(task_id, scenes)
    return scene


def clear_scene_timing(task_id: str, index: int) -> dict:
    """撤掉一个场景的时间线设置，让它回到自动时长。"""
    scenes = load(task_id)["scenes"]
    if not 0 <= index < len(scenes):
        raise SceneError(
            f"scene {index} does not exist; this task has {len(scenes)} scenes"
        )
    scene = scenes[index]
    scene.pop("trim_start", None)
    scene.pop("duration", None)
    save_scenes(task_id, scenes)
    return scene


def record_audio_artifacts(
    task_id: str,
    *,
    audio_file: Any,
    audio_duration: Any,
    subtitle_path: Any,
    script: Any = None,
) -> bool:
    """重新配音后把新的配音、字幕写回清单，并清掉"配音已失效"标记。

    不写回的话，审片界面会继续显示旧配音，渲染也会继续被 ``audio_stale``
    拦住——用户明明刚刚重新配过音。
    """
    updates: dict[str, Any] = {
        "audio_file": str(audio_file or "") or None,
        "audio_duration": audio_duration,
        "subtitle_path": str(subtitle_path or "") or None,
        _STALE_KEY: False,
    }
    if script is not None:
        updates["script"] = str(script)
    return task_artifacts.patch_script_data(task_id, **updates)


def mark_audio_stale(task_id: str, stale: bool = True) -> bool:
    """标记配音与字幕已与脚本不一致。"""
    return task_artifacts.patch_script_data(task_id, **{_STALE_KEY: bool(stale)})


def patch_script(task_id: str, script: str) -> bool:
    """只更新清单里的脚本正文，场景列表保持原样。"""
    return task_artifacts.patch_script_data(task_id, script=str(script or ""))


def narration_script(task_id: str) -> str:
    """把编辑后的场景台词拼回一段脚本，供重新配音使用。"""
    scenes = load(task_id)["scenes"]
    lines = [scene["narration"].strip() for scene in scenes]
    return "\n".join(line for line in lines if line)


def relative_media_path(task_id: str, path: Any) -> str:
    """返回供 ``/stream`` 使用的相对路径；越界或缺失时返回空字符串。

    ``/stream/{file_path:path}`` 自己也会再做一次限定,这里先解析一次是为了
    界面不去请求一个必然 404 的地址。
    """
    try:
        resolved = resolve_material(task_id, path)
    except SceneError:
        return ""
    try:
        return os.path.relpath(resolved, utils.task_dir("")).replace(os.sep, "/")
    except ValueError:
        return ""


def distribute_narration(
    windows: list[dict],
    *,
    segments: list[dict] | None = None,
    lines: list[str] | None = None,
) -> list[str]:
    """把旁白按时间分配到每个场景，一句都不丢。

    Why this exists
    ---------------
    ``build_scenes`` 原来按下标配对：第 i 段画面配第 i 句。关键词 12 个、
    旁白 28 句时，后 16 句在场景列表里根本不出现——界面少显示了一多半台词,
    而"改台词后重新配音"会把脚本从 28 句砍成 12 句。按时间分配之后每句都
    落在某一段画面上，拼回去仍然是完整的脚本。

    Pairing by index dropped every line past the last material: 12 keywords
    against 28 narration lines left 16 lines invisible in the review list, and
    re-recording from that list would have cut the script down to 12 lines.
    Splitting by time puts every line under the picture that is on screen
    while it is spoken, and joining them back yields the whole script again.

    每句归给"重叠最多"的那一段。完全落在时间线之外的句子（旁白比画面长时
    的尾句）归给离它最近的一段，而不是被丢掉。
    """
    if not windows:
        return []

    cues = _narration_cues(windows, segments, lines)
    buckets: list[list[str]] = [[] for _ in windows]
    for cue in cues:
        best_index = 0
        best_overlap = None
        for index, window in enumerate(windows):
            overlap = min(cue["end"], window["end"]) - max(
                cue["start"], window["start"]
            )
            if best_overlap is None or overlap > best_overlap:
                best_index, best_overlap = index, overlap
        buckets[best_index].append(cue["text"])
    # 用换行而不是空格拼：``split_narration`` 按标点和换行切句,所以换行拼出来
    # 的几句重新切一次还是原来那几句——"改台词后重新配音"就不会把三句话读成
    # 一句长句,断句和停顿都留在原处。
    return [
        "\n".join(part for part in bucket if part).strip() for bucket in buckets
    ]


def _narration_cues(
    windows: list[dict],
    segments: list[dict] | None,
    lines: list[str] | None,
) -> list[dict]:
    """返回带时间的旁白句子；没有字幕时按句数在整条时间线上均分。"""
    timed = [
        {
            "start": float(segment.get("start") or 0.0),
            "end": float(segment.get("end") or 0.0),
            "text": str(segment.get("text") or ""),
        }
        for segment in (segments or [])
        if str(segment.get("text") or "").strip()
    ]
    if timed:
        return timed

    texts = [line for line in (lines or []) if str(line).strip()]
    if not texts:
        return []
    # 没有字幕文件（关掉字幕的任务）时退回均分。均分不如字幕准，但仍然比
    # 按下标配对准：至少每句都会落在时间上大致对应的那段画面上。
    span = float(windows[-1]["end"]) - float(windows[0]["start"])
    if span <= 0:
        span = float(len(texts))
    origin = float(windows[0]["start"])
    step = span / len(texts)
    return [
        {
            "start": origin + position * step,
            "end": origin + (position + 1) * step,
            "text": str(text),
        }
        for position, text in enumerate(texts)
    ]


def narration_pairing_is_stale(scenes: list[dict], lines: list[str]) -> bool:
    """场景里的台词是不是漏掉了脚本里的句子。

    只认"每条场景台词都原封不动是脚本里的某一句"这一种情况：用户手改过台词
    之后再重排会把他的改动盖掉,那比少显示几句更糟,所以改过就不动。
    """
    stored = [str(scene.get("narration") or "").strip() for scene in scenes or []]
    available = [str(line).strip() for line in lines or [] if str(line).strip()]
    if not available or not stored:
        return False
    # 有人亲手改过台词，就谁也别动这份列表。
    #
    # 下面那条"每条都原封不动是脚本里的某一句"的判断猜的就是这件事，但猜不
    # 到"清空"：空字符串被当成"这一段还没配到词"，于是整份配对被重算一遍，
    # 刚刚删掉的句子又回来了。改过的那一段现在自己带着标记。
    if any(scene.get("narration_edited") for scene in scenes or []):
        return False
    known = set(available)
    if any(text and text not in known for text in stored):
        # 有人手改过台词（或上一次就是按时间合并的），不碰。
        return False
    return len([text for text in stored if text]) < len(available)


def apply_narration_pairing(task_id: str, texts: list[str]) -> bool:
    """把按时间分好的台词写回场景列表。"""
    scenes = load(task_id)["scenes"]
    if len(texts) != len(scenes):
        raise SceneError(
            f"cannot re-pair narration: {len(texts)} blocks for {len(scenes)} scenes"
        )
    for scene, text in zip(scenes, texts):
        scene["narration"] = text
    return save_scenes(task_id, scenes)
