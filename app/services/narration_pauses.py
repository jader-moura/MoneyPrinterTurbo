"""给已经录好的旁白加停顿：在两句之间插一段真的静音。

这是什么
--------
分镜里的 ``pause_after`` 本来只在配音那一刻管用：那一行后面带一个
``[pause: 1s]``，配音时插进一段静音。片子做完之后再想让某一句"停一下"，就只
能把整条旁白重录一遍——十几分钟的配音，为了几处一秒钟的空白。

这里不重录：把那几句后面的缝找出来，往录好的声音里插静音，然后把跟着声音走
的每一样东西一起挪——字幕、每个词的时间、时间线上那一段画面停多久。

几条规矩
--------
* **永远从原样重做。** 第一次动手之前，旁白、字幕、词的时间各留一份
  （``*.before-pauses.*``）。以后每一次都是"原样 + 这一次要的全部停顿"，所以
  加、改、去掉一处停顿都是同一个动作，声音也不会一遍遍重新压缩。
* **插在最安静的地方。** 一句的最后一个词和下一句的第一个词之间，取实际
  波形里最安静的那一点下刀；刀口两边各有几毫秒的淡出淡入，不留"咔"的一声。
  这两个词的时间是听出来的（whisper），不是字幕里估的。
* **画面跟着停。** 停顿那一刻在屏幕上的那一段，时长加上同样的秒数——和配音
  时录进去的停顿一样，画面停在那里等。
* **重新配过音就从头算。** 发现旁白不是这里上次做出来的那一份，就把现在这
  份当成新的原样。

Pauses for a narration that is already recorded: real silence spliced in
between two lines, with the subtitles, the word timings and the timeline moved
to match. Every run rebuilds from the untouched originals kept beside the
video, so adding, changing and removing pauses are the same operation.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from app.services import (
    edit_history, finishing, scene_editor, scene_manifest, scene_plan, task_artifacts,
)
from app.services.utils import ass_subtitles
from app.utils import utils

#: 这条片子的停顿是怎么做出来的：原样在哪、每一处插在第几秒。
SIDECAR = "narration-pauses.json"
ORIGINAL_SUBTITLE = "subtitle.before-pauses.srt"
ORIGINAL_WORDS = "spoken-words.before-pauses.json"
#: 不到这么长的不算停顿（也分不清是不是四舍五入出来的）。
SMALLEST = 0.05
#: 刀口两边淡出淡入多久（秒）。
_FADE = 0.008
#: 听出来的词边界能差多少（秒）：找下刀的地方时两头各放宽这么多。
_EDGE = 0.06
#: 在缝里找"最安静的一点"时，一小段有多长（秒）。
_QUIET_WINDOW = 0.03

_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")
_TIMING_LINE = re.compile(
    r"^(\s*)(\d+:\d\d:\d\d[,.]\d{1,3})(\s*-->\s*)(\d+:\d\d:\d\d[,.]\d{1,3})(.*)$")


class PauseError(ValueError):
    """这处停顿加不了。说清楚是哪一段、为什么。"""


def _original_audio_name(audio: str) -> str:
    return f"narration.before-pauses{os.path.splitext(audio)[1] or '.mp3'}"


def _stamp(path: str) -> list:
    return finishing._audio_stamp(path)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data) -> None:
    partial = f"{path}.{os.getpid()}.part"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(partial, path)


# ---------------------------------------------------------------------------
# 声音
# ---------------------------------------------------------------------------
_STREAM = re.compile(r"Audio: .*?(\d+) Hz, ([^,\r\n]+)(?:,[^,\r\n]*)?(?:, (\d+) kb/s)?")
_LAYOUTS = {"mono": 1, "stereo": 2}


def _ffmpeg() -> str:
    return utils.get_ffmpeg_binary()


def _probe(audio: str) -> Tuple[int, int, int]:
    """``(采样率, 声道数, 码率 kbps)``。从 ffmpeg 自己报的那一行里读，不另找 ffprobe。"""
    done = subprocess.run(
        [_ffmpeg(), "-nostdin", "-hide_banner", "-i", audio],
        capture_output=True, timeout=60,
    )
    match = _STREAM.search(done.stderr.decode("utf-8", "replace"))
    if not match:
        raise PauseError("the narration cannot be read: no audio stream in it")
    rate = int(match.group(1))
    channels = _LAYOUTS.get(match.group(2).strip().split("(")[0], 2)
    kbps = int(match.group(3) or 128)
    return rate, channels, max(96, min(320, kbps))


def _decode(audio: str, rate: int, channels: int) -> np.ndarray:
    done = subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-i", audio, "-f", "s16le",
         "-acodec", "pcm_s16le", "-ar", str(rate), "-ac", str(channels), "-"],
        capture_output=True, timeout=600,
    )
    if done.returncode != 0 or not done.stdout:
        raise PauseError(
            "the narration cannot be decoded: "
            + done.stderr.decode("utf-8", "replace").strip()[-300:])
    samples = np.frombuffer(done.stdout, dtype=np.int16)
    return samples[: len(samples) - len(samples) % channels].reshape(-1, channels)


def _encode(samples: np.ndarray, rate: int, kbps: int, target: str) -> None:
    extension = os.path.splitext(target)[1] or ".mp3"
    partial = f"{target}.{os.getpid()}.part{extension}"
    done = subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(rate),
         "-ac", str(samples.shape[1]), "-i", "-", "-b:a", f"{kbps}k", partial],
        input=samples.astype(np.int16).tobytes(), capture_output=True, timeout=600,
    )
    if done.returncode != 0 or not os.path.exists(partial):
        try:
            os.remove(partial)
        except OSError:
            pass
        raise PauseError(
            "the narration with pauses could not be written: "
            + done.stderr.decode("utf-8", "replace").strip()[-300:])
    os.replace(partial, target)


def quietest(samples: np.ndarray, rate: int, start: float, end: float) -> int:
    """``start`` 到 ``end`` 秒之间最安静的那一点，返回它是第几个采样。

    缝太窄（两句几乎连着念）就取正中间。
    """
    first = max(0, int(start * rate))
    last = min(len(samples), int(end * rate))
    window = max(1, int(_QUIET_WINDOW * rate))
    if last - first <= window:
        return max(0, min(len(samples), int((start + end) / 2 * rate)))
    loudness = np.abs(samples[first:last].astype(np.int32)).sum(axis=1)
    sums = np.concatenate(([0], np.cumsum(loudness)))
    energy = sums[window:] - sums[:-window]
    return first + int(np.argmin(energy)) + window // 2


def spliced(samples: np.ndarray, rate: int, cuts: List[Tuple[int, float]]) -> np.ndarray:
    """在每个 ``(第几个采样, 秒)`` 处插一段静音。刀口两边淡出淡入。"""
    fade = max(1, int(_FADE * rate))
    pieces = []
    at = 0
    for index, seconds in sorted(cuts):
        index = max(at, min(len(samples), index))
        before = samples[at:index].astype(np.float32)
        if len(before):
            ramp = min(fade, len(before))
            before[-ramp:] *= np.linspace(1.0, 0.0, ramp, dtype=np.float32)[:, None]
        pieces.append(before)
        pieces.append(np.zeros((int(round(seconds * rate)), samples.shape[1]), np.float32))
        at = index
    rest = samples[at:].astype(np.float32)
    pieces.append(rest)
    # 每一刀后面那一段的开头淡入（第一段不算：它前面没有刀口）。
    for piece_at in range(2, len(pieces), 2):
        piece = pieces[piece_at]
        if len(piece):
            ramp = min(fade, len(piece))
            piece[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float32)[:, None]
    return np.clip(np.concatenate(pieces), -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# 跟着声音走的那几样
# ---------------------------------------------------------------------------
def _seconds(stamp: str) -> float:
    hours, minutes, seconds, thousandths = _TIME.match(stamp).groups()
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(thousandths.ljust(3, "0")) / 1000.0)


def _stamp_of(seconds: float) -> str:
    whole = int(round(seconds * 1000))
    return (f"{whole // 3600000:02d}:{whole // 60000 % 60:02d}:"
            f"{whole // 1000 % 60:02d},{whole % 1000:03d}")


def shifted_subtitles(text: str, shift_of_cue) -> str:
    """字幕文件原样照抄，只把每一条的时间往后挪 ``shift_of_cue(第几条, 起, 止)`` 秒。"""
    lines = text.split("\n")
    cue = 0
    for at, line in enumerate(lines):
        match = _TIMING_LINE.match(line.rstrip("\r"))
        if not match:
            continue
        lead, start, arrow, end, rest = match.groups()
        by = shift_of_cue(cue, _seconds(start), _seconds(end))
        lines[at] = (f"{lead}{_stamp_of(_seconds(start) + by)}{arrow}"
                     f"{_stamp_of(_seconds(end) + by)}{rest}"
                     + ("\r" if line.endswith("\r") else ""))
        cue += 1
    return "\n".join(lines)


def _later(cuts: List[Tuple[float, float]], at: float) -> float:
    """原来第 ``at`` 秒的东西，现在要往后挪几秒。"""
    return sum(seconds for where, seconds in cuts if where <= at)


# ---------------------------------------------------------------------------
# 这条片子的状态
# ---------------------------------------------------------------------------
def _state(task_dir: str, audio: str) -> Optional[dict]:
    """上一次在这里做出来的停顿；旁白不是那一份了（重新配过音）就是 ``None``。"""
    state = _read_json(os.path.join(task_dir, SIDECAR))
    if not isinstance(state, dict) or state.get("built") != _stamp(audio):
        return None
    original = os.path.join(task_dir, _original_audio_name(audio))
    if not os.path.isfile(original):
        return None
    return state


def describe(task_id: str) -> dict:
    """这条片子现在有哪些停顿：``{"recorded": {段号: 秒}, "inserted": {段号: 秒}}``。"""
    payload = scene_manifest.load_raw(task_id)
    try:
        audio = scene_manifest.resolve_task_file(task_id, payload.get("audio_file"))
    except scene_manifest.SceneError:
        return {"recorded": {}, "inserted": {}}
    state = _state(utils.task_dir(task_id), audio)
    if state is None:
        plan = (payload.get("params") or {}).get("scene_plan") or []
        return {"recorded": _plan_pauses(plan), "inserted": {}}
    return {
        "recorded": {int(k): v for k, v in (state.get("recorded") or {}).items()},
        "inserted": {int(item["scene"]): item["seconds"]
                     for item in state.get("inserted") or []},
    }


def _plan_pauses(plan: list) -> Dict[int, float]:
    return {
        number: float(scene["direction"]["pause_after"])
        for number, scene in enumerate(plan, start=1)
        if isinstance(scene, dict) and (scene.get("direction") or {}).get("pause_after")
    }


def recorded(task_id: str) -> Dict[int, float]:
    """配音时就录进去的停顿。这些动不了：它们在原样的声音里。"""
    return describe(task_id)["recorded"]


def apply(task_id: str, wanted: Dict[int, float]) -> dict:
    """让这条片子的停顿正好是 ``wanted``（``{分镜段号: 这一段后面一共停几秒}``）。

    比配音时录进去的短，就照录进去的（去不掉）；多出来的那一截插进去。返回
    ``{"inserted": {段号: 秒}, "added": 总共多了几秒, "duration": 新的总长}``。
    没有可做的事（不要停顿，也从来没加过）什么都不碰。
    """
    payload = scene_manifest.load_raw(task_id)
    if not payload:
        raise PauseError(f"there is no video {task_id!r}")
    if payload.get("audio_stale"):
        raise PauseError(
            "the narration was changed and not re-recorded yet; re-record it "
            "first (the pauses are then recorded with it)")
    plan = [scene for scene in (payload.get("params") or {}).get("scene_plan") or []
            if isinstance(scene, dict)]
    if not plan:
        raise PauseError("this video was not made from a scene plan")
    try:
        audio = scene_manifest.resolve_task_file(task_id, payload.get("audio_file"))
        subtitle = scene_manifest.resolve_task_file(task_id, payload.get("subtitle_path"))
    except scene_manifest.SceneError as exc:
        raise PauseError(f"this video has no narration or subtitle timing yet: {exc}") from exc
    task_dir = utils.task_dir(task_id)
    original_audio = os.path.join(task_dir, _original_audio_name(audio))
    original_subtitle = os.path.join(task_dir, ORIGINAL_SUBTITLE)
    original_words = os.path.join(task_dir, ORIGINAL_WORDS)

    state = _state(task_dir, audio)
    if state is None:
        if not any(seconds >= SMALLEST for seconds in wanted.values()):
            return {"inserted": {}, "added": 0.0, "duration": None}
        # 第一次（或者重新配过音）：现在这一份就是原样。词的时间没听过就先听。
        if finishing._saved_spoken_words(audio, task_dir) is None:
            logger.info("timing the narration before adding pauses (a few minutes)")
            finishing.listen(audio, task_dir)
        shutil.copy2(audio, original_audio)
        shutil.copy2(subtitle, original_subtitle)
        shutil.copy2(os.path.join(task_dir, finishing._SPOKEN_WORDS_FILE), original_words)
        state = {"recorded": _plan_pauses(plan), "scene_added": {}}
    was_recorded = {int(k): float(v) for k, v in (state.get("recorded") or {}).items()}

    add = {
        int(number): round(float(seconds) - was_recorded.get(int(number), 0.0), 3)
        for number, seconds in wanted.items()
    }
    add = {number: seconds for number, seconds in add.items() if seconds >= SMALLEST}
    for number in add:
        if not 1 <= number <= len(plan):
            raise PauseError(f"scene {number}: the video has scenes 1 to {len(plan)}")

    # 每一段是从哪儿念到哪儿：拼写用字幕的，时间用听出来的。
    spoken = (_read_json(original_words) or {}).get("words") or []
    cues = finishing.moviepy_subtitles(original_subtitle)
    words = ass_subtitles.align_spoken_words(
        ass_subtitles.words_from_cues(cues), [tuple(word) for word in spoken])
    places = ass_subtitles.locate_lines(
        words, [str(scene.get("narration") or "") for scene in plan])

    rate, channels, kbps = _probe(original_audio)
    samples = _decode(original_audio, rate, channels)
    length = len(samples) / rate

    cuts: List[Tuple[int, float]] = []
    inserted = []
    for number in sorted(add):
        place = places[number - 1]
        if place is None:
            raise PauseError(
                f"scene {number}: its narration is not in the recording any more, "
                "so there is no place to put the pause")
        following = next((p for p in places[number:] if p is not None), None)
        gap_end = following[0] if following else length
        # 听出来的词边界有几十毫秒的出入，而有的句子之间只隔了四十毫秒：两头
        # 各放宽一点再找最安静的那一点，不然会落在上一个词的尾音里。
        index = quietest(samples, rate, place[1] - _EDGE, max(place[1], gap_end) + _EDGE)
        cuts.append((index, add[number]))
        inserted.append({"scene": number, "at": round(index / rate, 3),
                         "seconds": add[number]})
    cut_times = [(item["at"], item["seconds"]) for item in inserted]

    # 声音。
    _encode(spliced(samples, rate, cuts) if cuts else samples, rate, kbps, audio)

    # 字幕：一段占几条是数出来的（和分镜进来时的算法一样），所以按"第几条"
    # 挪最稳；数不上（台词改过）才退回按时间。
    counts = [len(utils.split_string_by_punctuations(str(scene.get("narration") or "")))
              for scene in plan]
    cue_scene: List[int] = []
    for number, count in enumerate(counts, start=1):
        cue_scene.extend([number] * count)
    by_count = len(cue_scene) == len(cues)

    def shift_of_cue(cue: int, start: float, end: float) -> float:
        if by_count and cue < len(cue_scene):
            return sum(seconds for number, seconds in add.items() if number < cue_scene[cue])
        return _later(cut_times, (start + end) / 2)

    with open(original_subtitle, encoding="utf-8") as handle:
        text = handle.read()
    partial = f"{subtitle}.{os.getpid()}.part"
    with open(partial, "w", encoding="utf-8", newline="") as handle:
        handle.write(shifted_subtitles(text, shift_of_cue))
    os.replace(partial, subtitle)

    # 每个词的时间：听过的结果跟着挪，不用为了几处静音再听四分钟。
    moved = []
    for word in spoken:
        by = _later(cut_times, (float(word[0]) + float(word[1])) / 2)
        moved.append([round(float(word[0]) + by, 3), round(float(word[1]) + by, 3), word[2]])
    _write_json(os.path.join(task_dir, finishing._SPOKEN_WORDS_FILE),
                {"audio": _stamp(audio), "words": moved})

    total_added = round(sum(add.values()), 3)
    # 时间线上的每一段是照字幕的时间排的（字幕的时间是估的，和听出来的能差
    # 一两秒），所以"哪一段画面跟着停"不能拿刀口的秒数去对：按台词认；认不出
    # （那一段被拆过、删过）才按字幕里这一段结束的时间找。
    ends: Dict[int, float] = {}
    if by_count:
        last = 0
        for number, count in enumerate(counts, start=1):
            last += count
            if count:
                ends[number] = float(cues[last - 1][0][1])
    holds = [
        (str(plan[item["scene"] - 1].get("narration") or ""),
         ends.get(item["scene"], item["at"]), item["seconds"])
        for item in inserted
    ]
    scene_added = _hold_pictures(
        task_id, [str(scene.get("narration") or "") for scene in plan], holds,
        {str(k): float(v) for k, v in (state.get("scene_added") or {}).items()})
    _note_in_the_plan(task_id, was_recorded, add)

    whole = length + total_added
    old = payload.get("audio_duration")
    task_artifacts.patch_script_data(
        task_id,
        audio_duration=math.ceil(whole) if isinstance(old, int) else round(whole, 3),
    )
    _write_json(os.path.join(task_dir, SIDECAR), {
        "built": _stamp(audio), "recorded": {str(k): v for k, v in was_recorded.items()},
        "inserted": inserted, "scene_added": scene_added,
    })
    logger.info(
        f"narration pauses for {task_id}: {len(inserted)} inserted, "
        f"{total_added:.2f}s added, now {whole:.2f}s")
    return {"inserted": {item["scene"]: item["seconds"] for item in inserted},
            "added": total_added, "duration": round(whole, 3)}


def _said(text) -> str:
    return " ".join(str(text or "").lower().split())


def _hold_pictures(task_id: str, narrations: List[str],
                   holds: List[Tuple[str, float, float]],
                   added_before: Dict[str, float]) -> Dict[str, float]:
    """停顿前那句话的画面，多停同样的秒数。返回 ``{场景 id: 多停了几秒}``。

    ``holds`` 是 ``(那一段的台词, 字幕里它结束的时间, 秒)``。先把上一次加的减回
    去，再按这一次的加：所以改一处、去掉一处都对。

    哪一段画面：时间线上台词和它一样的那一段（按顺序认，重复的台词各归各
    的）。时间线是人排的，那一段可能被拆过、删过——认不出就退回"字幕里那
    一刻屏幕上是哪一段"。
    """
    with scene_editor._editing(task_id):
        manifest = scene_manifest.load(task_id)
        scenes = manifest.get("scenes") or []
        if not scenes:
            return {}
        windows = scene_editor.planned_windows(task_id, manifest, probe=False)
        lengths = []
        for scene, window in zip(scenes, windows):
            now = float(window["end"]) - float(window["start"])
            lengths.append(max(0.0, now - added_before.get(str(scene.get("id")), 0.0)))

        # 分镜的每一段落在时间线的哪一段上。
        shown = [_said(scene.get("narration")) for scene in scenes]
        placed: Dict[str, int] = {}
        cursor = 0
        for narration in narrations:
            key = _said(narration)
            for at in range(cursor, len(shown)):
                if key and shown[at] == key:
                    placed.setdefault(key, at)
                    cursor = at + 1
                    break
        edges = []
        elapsed = 0.0
        for length in lengths:
            elapsed += length
            edges.append(elapsed)

        added: Dict[str, float] = {}
        for narration, ends_at, seconds in holds:
            at = placed.get(_said(narration))
            if at is None:
                at = next((n for n, edge in enumerate(edges) if ends_at <= edge + 0.05),
                          len(scenes) - 1)
            key = str(scenes[at].get("id"))
            added[key] = round(added.get(key, 0.0) + seconds, 3)

        changed = False
        for scene, length in zip(scenes, lengths):
            key = str(scene.get("id"))
            if key in added or key in added_before:
                scene["duration"] = round(length + added.get(key, 0.0), 3)
                changed = True
        if changed:
            # 不记成一步"撤销"：撤销回去的是时间线，声音回不去，两边就对不上了。
            with edit_history.paused():
                scene_manifest.save_scenes(task_id, scenes)
        return added


def _note_in_the_plan(task_id: str, was_recorded: Dict[int, float],
                      add: Dict[int, float]) -> None:
    """分镜里记下每一段后面一共停几秒；以后重新配音，停顿就是录进去的。"""
    payload = task_artifacts.read_script_data(task_id)
    params = payload.get("params") or {}
    plan = params.get("scene_plan") or []
    script_before = scene_plan.script_of({"scenes": plan}) if plan else ""
    for number, scene in enumerate(plan, start=1):
        if not isinstance(scene, dict):
            continue
        total = round(was_recorded.get(number, 0.0) + add.get(number, 0.0), 2)
        direction = dict(scene.get("direction") or {})
        if total >= SMALLEST:
            direction["pause_after"] = total
        else:
            direction.pop("pause_after", None)
        if direction:
            scene["direction"] = direction
        else:
            scene.pop("direction", None)
    updates = {"params": params}
    # 脚本还是分镜原样的那一份，才跟着改：人改过的脚本不去动它。
    if plan and str(payload.get("script") or "") == script_before:
        updates["script"] = scene_plan.script_of({"scenes": plan})
    task_artifacts.patch_script_data(task_id, **updates)
