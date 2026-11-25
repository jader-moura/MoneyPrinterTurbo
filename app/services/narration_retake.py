"""One line of a recorded narration, recorded again and put back in its place.

Why
---
The voice sometimes gets a single line wrong: a garbled word, an odd stress, a
breath in the middle of a name. The only way to fix it used to be recording
the whole narration again, which takes minutes and changes every other line
too.

This records just that line, with the same voice and speed, and splices it
into the recording where the old one was. A new take is never exactly as long
as the old one, so everything timed to the voice moves with it: the subtitle
times, the time of every word, the picture that was on screen, and the length
of the whole narration.

The rules
---------
* **Same words.** The line is spoken as the script has it. Changing what is
  said is a script edit, and that still needs the whole narration recorded.
* **Cut in the silence.** The cuts go at the quietest points before and after
  the line, found in the real waveform, with a few milliseconds of fade so
  nothing clicks. The silence the old line had before and after it is kept.
* **The picture follows.** The scene on screen during the line gets longer or
  shorter by the same amount, so everything after it stays in step.
"""

from __future__ import annotations

import math
import os
import re
import threading
from types import SimpleNamespace
from typing import Callable, List, Optional, Tuple

import numpy as np
from loguru import logger

from app.services import (
    edit_history, finishing, narration_pauses as pauses, scene_editor,
    scene_manifest, task_artifacts, timeline, voice,
)
from app.services.utils import ass_subtitles
from app.utils import utils

#: How far a word boundary can be off (seconds) when nobody has listened to
#: the narration and the times are only estimated from the subtitles.
_ESTIMATED_EDGE = 0.35
#: Kept on each side of the speech when a take is trimmed (seconds).
_MARGIN = 0.02
#: One slice of the waveform when looking for where speech starts (seconds).
_SLICE = 0.02
#: One slice of the waveform when looking for the silence between two lines.
_STEP = 0.01
#: A new take is brought to the loudness of the lines around it, within this.
_GAIN = (0.5, 2.0)
#: How much of the narration around the line sets that loudness (seconds).
_AROUND = 10.0

_ONE_AT_A_TIME = threading.Lock()


class RetakeError(scene_editor.SceneError):
    """This line cannot be recorded again. Says which line and why."""


# ---------------------------------------------------------------------------
# The sound
# ---------------------------------------------------------------------------
def _levels(samples: np.ndarray, rate: int) -> np.ndarray:
    """How loud each short slice is."""
    size = max(1, int(_SLICE * rate))
    count = len(samples) // size
    if not count:
        return np.zeros(0)
    mono = np.abs(samples[: count * size].astype(np.int32)).mean(axis=1)
    return mono.reshape(count, size).mean(axis=1)


def loud_span(samples: np.ndarray, rate: int) -> Optional[Tuple[int, int]]:
    """Where the speech is: ``(first sample, one past the last)``; ``None`` for silence."""
    levels = _levels(samples, rate)
    if not len(levels):
        return None
    top = float(np.percentile(levels, 95))
    if top < 40:
        return None
    loud = np.nonzero(levels > max(top * 0.08, 30))[0]
    if not len(loud):
        return None
    size = max(1, int(_SLICE * rate))
    return int(loud[0]) * size, int(loud[-1] + 1) * size


def _loudness(samples: np.ndarray, rate: int) -> float:
    """The level of the spoken parts, ignoring the silence between them."""
    levels = _levels(samples, rate)
    if not len(levels):
        return 0.0
    top = float(np.percentile(levels, 95))
    spoken = levels[levels > max(top * 0.08, 30)]
    return float(spoken.mean()) if len(spoken) else 0.0


def _faded(samples: np.ndarray, rate: int, *, start: bool, end: bool) -> np.ndarray:
    piece = samples.astype(np.float32)
    ramp = min(max(1, int(pauses._FADE * rate)), len(piece))
    if ramp and start:
        piece[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float32)[:, None]
    if ramp and end:
        piece[-ramp:] *= np.linspace(1.0, 0.0, ramp, dtype=np.float32)[:, None]
    return piece


def _silence_around(old: np.ndarray, rate: int) -> Tuple[int, int]:
    """How much silence the old take had before and after its speech, in
    samples. Kept as it was, however long: a pause that was recorded on
    purpose stays the length it was."""
    floor = int(_MARGIN * 2 * rate)
    span = loud_span(old, rate)
    if span is None:
        return floor * 2, floor * 2
    return max(floor, span[0]), max(floor, len(old) - span[1])


def gap(samples: np.ndarray, rate: int, start: float, end: float) -> int:
    """Where to cut between two lines: the middle of the longest silence that
    begins between ``start`` and ``end`` seconds. Returns a sample number.

    The quietest instant is not good enough. A word like "checks" goes silent
    for a few hundredths of a second before its last sound, and a cut there
    leaves that sound behind, to be heard after the new take. The pause
    between two lines is longer than any silence inside a word, so the longest
    one is taken, and followed to its real end even past ``end``.
    """
    size = max(1, int(_STEP * rate))
    first = max(0, int(start * rate)) // size
    last = min(len(samples), int(end * rate)) // size
    if last <= first:
        return pauses.quietest(samples, rate, start, end)
    near = int(2.0 * rate) // size
    low, high = max(0, first - near), min(len(samples) // size, last + near)
    levels = np.abs(samples[low * size: high * size].astype(np.int32)).mean(axis=1)
    levels = levels.reshape(high - low, size).mean(axis=1)
    quiet = levels < max(30.0, float(np.percentile(levels, 95)) * 0.06)

    best, at = None, first - low
    while at < last - low:
        if not quiet[at]:
            at += 1
            continue
        until = at
        while until < len(quiet) and quiet[until]:
            until += 1
        if best is None or until - at > best[1] - best[0]:
            best = (at, until)
        at = until
    if best is None:  # the lines run together: the least bad instant
        return pauses.quietest(samples, rate, start, end)
    return (low + (best[0] + best[1]) // 2) * size


def replaced(samples: np.ndarray, rate: int, first: int, last: int,
             take: np.ndarray) -> np.ndarray:
    """``samples`` with ``[first, last)`` swapped for ``take``.

    The take is trimmed to its speech and given the silence the old line had
    before and after it. Both joins fade.
    """
    old = samples[first:last]
    channels = samples.shape[1]
    lead, tail = _silence_around(old, rate)

    spoken = loud_span(take, rate)
    if spoken is None:
        raise RetakeError("the voice returned silence for this line; try again")
    margin = int(_MARGIN * rate)
    speech = _faded(
        take[max(0, spoken[0] - margin): min(len(take), spoken[1] + margin)],
        rate, start=True, end=True)

    around = samples[max(0, first - int(_AROUND * rate)): last + int(_AROUND * rate)]
    wanted, got = _loudness(around, rate), _loudness(speech, rate)
    if wanted > 0 and got > 0:
        speech *= max(_GAIN[0], min(_GAIN[1], wanted / got))

    pieces = [
        _faded(samples[:first], rate, start=False, end=first > 0),
        np.zeros((lead, channels), np.float32),
        speech,
        np.zeros((tail, channels), np.float32),
        _faded(samples[last:], rate, start=last < len(samples), end=False),
    ]
    return np.clip(np.concatenate(pieces), -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------
def _cue_words(cues) -> List[Optional[Tuple[int, int]]]:
    """Which words belong to each subtitle line: ``(first, last)``, or ``None``.

    Counted the way :func:`ass_subtitles.words_from_cues` splits them, so the
    numbers index its result.
    """
    spans: List[Optional[Tuple[int, int]]] = []
    at = 0
    for _, text in cues:
        count = len(ass_subtitles._clean(text).split())
        spans.append((at, at + count - 1) if count else None)
        at += count
    return spans


_TOKEN = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")


def as_written(script: str, lines: List[str], index: int) -> str:
    """Line ``index`` the way the script has it, punctuation and all.

    The subtitles drop the punctuation, and the voice reads better with it: a
    question sounds like one. Each line is looked for after the one before it;
    a line the script no longer has (it was edited) is returned as it is.
    """
    span = _span(script, lines, index)
    if span is None:
        return str(lines[index] or "").strip()
    return _untagged(script)[span[0]: span[1]].strip()


def _untagged(script: str) -> str:
    """The script with its ``[pause: …]`` tags blanked out, every other
    character where it was."""
    return re.sub(r"\[[^\]\n]*\]", lambda tag: " " * len(tag.group()), str(script or ""))


def _span(script: str, lines: List[str], index: int) -> Optional[Tuple[int, int]]:
    """Where line ``index`` is in the script: ``(first character, one past the
    last)``, closing punctuation included. ``None`` when it is not there."""
    script = _untagged(script)
    tokens = [(match.start(), match.end(), ass_subtitles._key(match.group()))
              for match in _TOKEN.finditer(script)]
    tokens = [token for token in tokens if token[2]]
    cursor = 0
    for at, line in enumerate(lines[: index + 1]):
        wanted = [key for key in (ass_subtitles._key(m.group())
                                  for m in _TOKEN.finditer(str(line or ""))) if key]
        place = None
        if wanted:
            for start in range(cursor, len(tokens) - len(wanted) + 1):
                if all(tokens[start + n][2] == wanted[n] for n in range(len(wanted))):
                    place = start
                    break
        if place is None:
            if at == index:
                return None
            continue
        cursor = place + len(wanted)
        if at == index:
            first, last = tokens[place][0], tokens[cursor - 1][1]
            closing = re.match(r"[^\s\w]*", script[last:]).group()
            return first, last + len(closing)
    return None


def without(script: str, lines: List[str], index: int) -> str:
    """The script with line ``index`` taken out, and a pause tag that
    followed it on the same row. Unchanged when the line is not in it."""
    script = str(script or "")
    span = _span(script, lines, index)
    return script if span is None else _taken_out(script, *span)


def _taken_out(script: str, first: int, last: int) -> str:
    last += len(re.match(r"(?:[ \t]*\[[^\]\n]*\])*[ \t]*", script[last:]).group())
    head, tail = script[:first].rstrip(" \t"), script[last:]
    if not head or head.endswith("\n"):
        return head + (tail[1:] if tail.startswith("\n") else tail)
    if not tail or tail.startswith("\n"):
        return head + tail
    return head + " " + tail


def _take_words(take_path: str, tokens: List[str], starts_at: float,
                seconds: float, skipped: float) -> list:
    """When each word of the new take is said, on the narration's clock.

    Listened for in the take alone (a few seconds, not the minutes the whole
    narration takes). If that cannot be done the words are spread across the
    line by their length, which is what the subtitles do anyway.
    """
    try:
        heard = finishing._spoken_words(take_path)
    except Exception as exc:  # whisper is optional; the estimate below is not
        logger.warning(f"could not listen to the new take, estimating its words: {exc}")
        heard = []
    if heard:
        ends_at = starts_at + seconds
        return [[round(min(ends_at, max(starts_at, starts_at + start - skipped)), 3),
                 round(min(ends_at, max(starts_at, starts_at + end - skipped)), 3), text]
                for start, end, text in heard]
    weights = [len(re.sub(r"\W", "", token)) + 2 for token in tokens]
    total = float(sum(weights)) or 1.0
    words, at = [], starts_at
    for token, weight in zip(tokens, weights):
        until = at + seconds * weight / total
        words.append([round(at, 3), round(until, 3), token])
        at = until
    return words


# ---------------------------------------------------------------------------
# What follows the voice
# ---------------------------------------------------------------------------
def _moved_subtitles(text: str, index: int, by: float) -> str:
    """The subtitle file as it is, with line ``index`` ending ``by`` seconds
    later and every line after it moved by the same."""
    lines = text.split("\n")
    cue = 0
    for at, line in enumerate(lines):
        match = pauses._TIMING_LINE.match(line.rstrip("\r"))
        if not match:
            continue
        lead, start, arrow, end, rest = match.groups()
        begins, stops = pauses._seconds(start), pauses._seconds(end)
        if cue == index:
            stops = max(begins + 0.05, stops + by)
        elif cue > index:
            begins, stops = begins + by, stops + by
        if cue >= index:
            lines[at] = (f"{lead}{pauses._stamp_of(begins)}{arrow}"
                         f"{pauses._stamp_of(stops)}{rest}"
                         + ("\r" if line.endswith("\r") else ""))
        cue += 1
    return "\n".join(lines)


def _move_pictures(task_id: str, at: float, by: float) -> Optional[int]:
    """The scene on screen at ``at`` seconds lasts ``by`` seconds longer.

    Returns its position, or ``None`` when the video has no scenes.
    """
    with scene_editor._editing(task_id):
        manifest = scene_manifest.load(task_id)
        scenes = manifest.get("scenes") or []
        if not scenes:
            return None
        windows = scene_editor.planned_windows(task_id, manifest, probe=False)
        lengths = [max(0.0, float(w["end"]) - float(w["start"])) for w in windows]
        if len(lengths) != len(scenes):
            return None
        elapsed, place = 0.0, len(scenes) - 1
        for position, length in enumerate(lengths):
            elapsed += length
            if at < elapsed:
                place = position
                break
        # A video nobody has timed by hand is laid out line by line when it is
        # rendered. Giving one scene a length switches that off for all of
        # them, so every scene is given the length it has now.
        untimed = not scene_manifest.has_timing_edits(scenes)
        for position, (scene, length) in enumerate(zip(scenes, lengths)):
            if position == place:
                scene["duration"] = round(
                    max(timeline.MIN_CLIP_DURATION, length + by), 3)
            elif untimed:
                scene["duration"] = round(max(timeline.MIN_CLIP_DURATION, length), 3)
        # Not a step to undo: undo would move the pictures back and leave the
        # sound where it is.
        with edit_history.paused():
            scene_manifest.save_scenes(task_id, scenes)
        return place


# ---------------------------------------------------------------------------
# Recording one line again
# ---------------------------------------------------------------------------
def _record(params) -> Callable[[str, str], bool]:
    def record(text: str, target: str) -> bool:
        made = voice.tts(
            text=text,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_file=target,
        )
        return made is not None and os.path.isfile(target)
    return record


def line(task_id: str, index) -> dict:
    """One narration line as it is in the recording: what the voice says for
    it and when. ``{"index", "text", "start", "end", "heard"}``.

    The subtitle times are estimates and can be a second off; these are the
    times the words were heard at, when the narration has been listened to
    (``heard``). It is never listened to here: that takes minutes.
    """
    payload = scene_manifest.load_raw(task_id)
    try:
        audio = scene_manifest.resolve_task_file(task_id, payload.get("audio_file"))
        subtitle = scene_manifest.resolve_task_file(task_id, payload.get("subtitle_path"))
    except scene_manifest.SceneError as exc:
        raise RetakeError(
            f"this video has no narration or no line timing yet: {exc}") from exc
    cues = finishing.moviepy_subtitles(subtitle)
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise RetakeError(f"the line must be a whole number, got {index!r}") from exc
    if not 0 <= position < len(cues):
        raise RetakeError(
            f"there is no narration line {position + 1}; this video has {len(cues)}")
    (start, end), _ = cues[position]
    heard = None if payload.get("audio_stale") else finishing._saved_spoken_words(
        audio, utils.task_dir(task_id))
    span = _cue_words(cues)[position]
    if heard and span is not None:
        words = ass_subtitles.align_spoken_words(
            ass_subtitles.words_from_cues(cues), heard)
        start, end = words[span[0]].start, words[span[1]].end
    return {
        "index": position,
        "text": as_written(
            str(payload.get("script") or ""), [cue[1] for cue in cues], position),
        "start": round(float(start), 3), "end": round(float(end), 3),
        "heard": bool(heard),
    }


def check(task_id: str, index) -> dict:
    """Whether line ``index`` can be recorded again, asked before it is put in
    the queue so a refusal is said on the click and not minutes later.
    Returns ``{"index", "caption", "text"}``."""
    from app.services import scene_video

    scene_editor._require_idle(task_id)
    scene_video.refuse_while_running()
    payload, _, _, cues, position = _checked(task_id, index, voiced=True)
    if not str(getattr(scene_editor.load_params(task_id), "voice_name", "") or "").strip():
        raise RetakeError("this video has no voice set to record the line with")
    return {
        "index": position,
        "caption": str(cues[position][1] or ""),
        "text": as_written(
            str(payload.get("script") or ""), [cue[1] for cue in cues], position),
    }


def where(task_id: str, index, caption: str) -> int:
    """Which line has this caption now. A line waits in the queue by its
    words, not its number: a line deleted before it moves every number after."""
    _, _, _, cues, _ = _checked(task_id, 0, voiced=False)
    said = [str(cue[1] or "") for cue in cues]
    try:
        position = int(index)
    except (TypeError, ValueError):
        position = -1
    if 0 <= position < len(said) and said[position] == caption:
        return position
    same = [at for at, text in enumerate(said) if text == caption]
    if not same:
        raise RetakeError("this line is no longer in the narration")
    # The nearest one: the same words can be said twice in a video.
    return min(same, key=lambda at: abs(at - position))


def retake(task_id: str, index, record: Optional[Callable[[str, str], bool]] = None,
           *, wait: bool = False) -> dict:
    """Record narration line ``index`` again and put it where the old one was.

    ``record(text, path)`` writes the spoken line to ``path``; left out, it is
    the video's own voice. ``wait`` waits for a line being removed instead of
    refusing. Returns what changed:
    ``{"index", "text", "was", "now", "added", "duration"}`` (seconds).
    """
    from app.services import scene_video

    scene_editor._require_idle(task_id)
    # A batch of video scenes has the whole graphics card, and a local voice
    # needs part of it.
    scene_video.refuse_while_running()
    if not _ONE_AT_A_TIME.acquire(blocking=wait):
        raise scene_editor.SceneBusyError(
            "another narration line is being recorded; wait for it to finish")
    try:
        return _retake(task_id, index, record)
    finally:
        _ONE_AT_A_TIME.release()


def _checked(task_id: str, index, *, voiced: bool):
    """What can be refused without opening the recording. Returns
    ``(payload, audio, subtitle, cues, position)``. ``voiced`` refuses a
    narration no voice recorded."""
    payload = scene_manifest.load_raw(task_id)
    if not payload:
        raise RetakeError(f"there is no video {task_id!r}")
    if payload.get("audio_stale"):
        raise RetakeError(
            "the script was changed and the narration not recorded since, so "
            "the lines no longer match it; re-record the whole narration first")
    if voiced and str((payload.get("params") or {}).get("custom_audio_file") or "").strip():
        raise RetakeError(
            "this narration is an audio file you supplied, so there is no "
            "voice to record one line with")
    try:
        audio = scene_manifest.resolve_task_file(task_id, payload.get("audio_file"))
        subtitle = scene_manifest.resolve_task_file(task_id, payload.get("subtitle_path"))
    except scene_manifest.SceneError as exc:
        raise RetakeError(
            f"this video has no narration or no line timing yet: {exc}") from exc

    cues = finishing.moviepy_subtitles(subtitle)
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise RetakeError(f"the line must be a whole number, got {index!r}") from exc
    if not 0 <= position < len(cues):
        raise RetakeError(
            f"there is no narration line {position + 1}; this video has {len(cues)}")
    if _cue_words(cues)[position] is None:
        raise RetakeError(f"narration line {position + 1} has no words to record")
    return payload, audio, subtitle, cues, position


def _placed(task_id: str, index, *, voiced: bool = True) -> SimpleNamespace:
    """Line ``index`` found in the recording: the sound, the cues, and the two
    samples it is cut at."""
    payload, audio, subtitle, cues, position = _checked(task_id, index, voiced=voiced)
    first, last = _cue_words(cues)[position]
    line = str(cues[position][1] or "")
    text = as_written(str(payload.get("script") or ""), [cue[1] for cue in cues], position)

    # When each word is said: heard in the recording when that has been done
    # (or can be), estimated from the subtitles otherwise.
    task_dir = utils.task_dir(task_id)
    heard = finishing._saved_spoken_words(audio, task_dir)
    if heard is None:
        try:
            logger.info("timing the narration before recording one line again "
                        "(a few minutes, once)")
            finishing.listen(audio, task_dir)
            heard = finishing._saved_spoken_words(audio, task_dir)
        except Exception as exc:
            logger.warning(f"the narration could not be listened to, using the "
                           f"subtitle times: {exc}")
    words = ass_subtitles.words_from_cues(cues)
    if heard:
        words = ass_subtitles.align_spoken_words(words, heard)

    def between(left, right) -> int:
        """The cut between two neighbouring words. Heard times say which word
        is which but not exactly where one ends, so the silence is looked for
        from the middle of one word to the middle of the next."""
        if heard:
            return gap(samples, rate, (float(left.start) + float(left.end)) / 2,
                       (float(right.start) + float(right.end)) / 2)
        edges = float(left.end), float(right.start)
        return gap(samples, rate, min(edges) - _ESTIMATED_EDGE,
                   max(edges) + _ESTIMATED_EDGE)

    rate, channels, kbps = pauses._probe(audio)
    samples = pauses._decode(audio, rate, channels)
    length = len(samples) / rate
    cut_from = between(words[first - 1], words[first]) if first > 0 else 0
    cut_to = (between(words[last], words[last + 1])
              if last < len(words) - 1 else len(samples))
    if cut_to - cut_from < int(0.1 * rate):
        raise RetakeError(
            f"narration line {position + 1} could not be found in the recording")
    return SimpleNamespace(
        payload=payload, audio=audio, subtitle=subtitle, cues=cues,
        position=position, line=line, text=text, task_dir=task_dir, heard=heard,
        rate=rate, channels=channels, kbps=kbps, samples=samples, length=length,
        cut_from=cut_from, cut_to=cut_to)


def _retake(task_id: str, index, record) -> dict:
    at = _placed(task_id, index)
    payload, audio, subtitle, cues = at.payload, at.audio, at.subtitle, at.cues
    position, line, text, task_dir, heard = at.position, at.line, at.text, at.task_dir, at.heard
    rate, channels, kbps, samples, length = at.rate, at.channels, at.kbps, at.samples, at.length
    cut_from, cut_to = at.cut_from, at.cut_to

    if record is None:
        params = scene_editor.load_params(task_id)
        if not str(getattr(params, "voice_name", "") or "").strip():
            raise RetakeError("this video has no voice set to record the line with")
        record = _record(params)

    take_path = os.path.join(task_dir, f"narration-take.{os.getpid()}.mp3")
    try:
        logger.info(f"recording narration line {position + 1} of {task_id} again: {text!r}")
        if not record(text, take_path):
            raise RetakeError(
                "the voice did not record the line; check that it is running "
                "and try again")
        take = pauses._decode(take_path, rate, channels)
        made = replaced(samples, rate, cut_from, cut_to, take)
        pauses._encode(made, rate, kbps, audio)

        added = (len(made) - len(samples)) / rate
        was, now = (cut_to - cut_from) / rate, (cut_to - cut_from) / rate + added

        with open(subtitle, encoding="utf-8") as handle:
            written = handle.read()
        partial = f"{subtitle}.{os.getpid()}.part"
        with open(partial, "w", encoding="utf-8", newline="") as handle:
            handle.write(_moved_subtitles(written, position, added))
        os.replace(partial, subtitle)

        if heard:
            cut_at, cut_until = cut_from / rate, cut_to / rate
            spoken = loud_span(take, rate) or (0, len(take))
            lead = _silence_around(samples[cut_from:cut_to], rate)[0] / rate
            skipped = max(0.0, spoken[0] / rate - _MARGIN)
            speech = (spoken[1] - spoken[0]) / rate + 2 * _MARGIN
            fresh = _take_words(
                take_path, ass_subtitles._clean(line).split(),
                cut_at + lead, speech, skipped)
            moved = [list(word) for word in heard
                     if (float(word[0]) + float(word[1])) / 2 < cut_at]
            moved += fresh
            moved += [[round(float(word[0]) + added, 3), round(float(word[1]) + added, 3),
                       word[2]]
                      for word in heard
                      if (float(word[0]) + float(word[1])) / 2 >= cut_until]
            pauses._write_json(
                os.path.join(task_dir, finishing._SPOKEN_WORDS_FILE),
                {"audio": pauses._stamp(audio), "words": moved})
    finally:
        try:
            os.remove(take_path)
        except OSError:
            pass

    (begins, stops), _ = cues[position]
    _move_pictures(task_id, (float(begins) + float(stops)) / 2, added)

    whole = length + added
    old_duration = payload.get("audio_duration")
    task_artifacts.patch_script_data(
        task_id,
        audio_duration=(math.ceil(whole) if isinstance(old_duration, int)
                        else round(whole, 3)),
    )
    logger.info(
        f"narration line {position + 1} of {task_id} recorded again: "
        f"{was:.2f}s -> {now:.2f}s, the narration is now {whole:.2f}s")
    return {
        "index": position, "text": text, "was": round(was, 3), "now": round(now, 3),
        "added": round(added, 3), "duration": round(whole, 3),
    }


# ---------------------------------------------------------------------------
# Taking one line out
# ---------------------------------------------------------------------------
def _forget(task_id: str, lines: List[str], index: int) -> None:
    """Take the line out of the script and out of the scene that says it, so
    recording the whole narration later does not bring it back."""
    payload = scene_manifest.load_raw(task_id)
    script = str(payload.get("script") or "")
    shorter = without(script, lines, index)
    if shorter != script:
        task_artifacts.patch_script_data(task_id, script=shorter)

    with scene_editor._editing(task_id):
        scenes = scene_manifest.load(task_id).get("scenes") or []
        said = [str(scene.get("narration") or "") for scene in scenes]
        span = _span("\n".join(said), lines, index)
        if span is None:
            return
        starts = 0
        for scene, text in zip(scenes, said):
            if starts <= span[0] and span[1] <= starts + len(text):
                scene["narration"] = _taken_out(
                    text, span[0] - starts, span[1] - starts).strip()
                with edit_history.paused():
                    scene_manifest.save_scenes(task_id, scenes)
                return
            starts += len(text) + 1


def remove(task_id: str, index) -> dict:
    """Take narration line ``index`` out: its sound, its caption and its place
    in the script. Everything after it comes that much earlier. Returns
    ``{"index", "text", "removed", "duration"}`` (seconds).

    Nothing is recorded, so this also works on a narration that was supplied
    as an audio file.
    """
    from app.services import scene_video

    scene_editor._require_idle(task_id)
    scene_video.refuse_while_running()
    if not _ONE_AT_A_TIME.acquire(blocking=False):
        raise scene_editor.SceneBusyError(
            "a narration line is being recorded or removed; wait for it to finish")
    try:
        return _remove(task_id, index)
    finally:
        _ONE_AT_A_TIME.release()


def _remove(task_id: str, index) -> dict:
    at = _placed(task_id, index, voiced=False)
    if len(at.cues) < 2:
        raise RetakeError(
            "this is the only narration line; a video cannot be left without one")
    rate, samples = at.rate, at.samples
    made = np.clip(np.concatenate([
        _faded(samples[: at.cut_from], rate, start=False, end=at.cut_from > 0),
        _faded(samples[at.cut_to:], rate, start=at.cut_to < len(samples), end=False),
    ]), -32768, 32767).astype(np.int16)
    removed = (at.cut_to - at.cut_from) / rate
    pauses._encode(made, rate, at.kbps, at.audio)

    blocks = []
    for (begins, stops), text in at.cues[: at.position] + at.cues[at.position + 1:]:
        by = removed if len(blocks) >= at.position else 0.0
        blocks.append(
            f"{len(blocks) + 1}\n{pauses._stamp_of(max(0.0, float(begins) - by))} --> "
            f"{pauses._stamp_of(max(0.05, float(stops) - by))}\n{text}\n")
    partial = f"{at.subtitle}.{os.getpid()}.part"
    with open(partial, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(blocks))
    os.replace(partial, at.subtitle)

    if at.heard:
        cut_at, cut_until = at.cut_from / rate, at.cut_to / rate
        kept = [list(word) for word in at.heard
                if (float(word[0]) + float(word[1])) / 2 < cut_at]
        kept += [[round(float(word[0]) - removed, 3), round(float(word[1]) - removed, 3),
                  word[2]]
                 for word in at.heard
                 if (float(word[0]) + float(word[1])) / 2 >= cut_until]
        pauses._write_json(
            os.path.join(at.task_dir, finishing._SPOKEN_WORDS_FILE),
            {"audio": pauses._stamp(at.audio), "words": kept})

    (begins, stops), _ = at.cues[at.position]
    _move_pictures(task_id, (float(begins) + float(stops)) / 2, -removed)
    try:
        _forget(task_id, [cue[1] for cue in at.cues], at.position)
    except Exception as exc:  # the sound is already right; the text is a courtesy
        logger.warning(f"line {at.position + 1} is out of the recording but "
                       f"could not be taken out of the script: {exc}")

    whole = at.length - removed
    old_duration = at.payload.get("audio_duration")
    task_artifacts.patch_script_data(
        task_id,
        audio_duration=(math.ceil(whole) if isinstance(old_duration, int)
                        else round(whole, 3)),
    )
    logger.info(f"narration line {at.position + 1} of {task_id} removed: "
                f"{removed:.2f}s shorter, the narration is now {whole:.2f}s")
    return {"index": at.position, "text": at.text,
            "removed": round(removed, 3), "duration": round(whole, 3)}
