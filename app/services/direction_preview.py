"""编辑器里预览成片的"导演项"：片名、标题卡、重音、环境声、配乐让开。

这是什么
--------
这些东西都是精修那一遍（``finishing.render``）出片时才加上去的，编辑器的预览
放的只是每一段的素材和旁白。所以在这之前，一张摆错了位置的标题卡要等整条片子
渲完才看得见。

这个模块把"成片里会加什么、各在第几秒"算出来交给编辑器，编辑器自己在预览上
画、自己放那几种声音。算法不在这里：位置是 ``finishing.editor_direction`` 给
的，和出片走同一套。这里管的是编辑器那一头的三件事：

* 这条任务的文件在哪（旁白、字幕）；
* 位置要靠"把配音听一遍"才准，而听一遍要几分钟——放到后台去听，接口先答
  ``listening``，编辑器过一会儿再问；
* 那几种声音是合成的，没有素材文件：合成一次，存在缓存目录里给浏览器放。

What the finished video will add on top of the editor's preview (title, title
cards, impacts, ambience, music dropouts) and when. The placement itself is
``finishing.editor_direction``; this module finds the task's files, runs the
slow word-timing pass in the background, and keeps the synthesised sounds as
files a browser can play.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Tuple

from loguru import logger

from app.services import finishing, narration_pauses, scene_editor, scene_manifest, scene_plan
from app.services import sound_design, task_artifacts
from app.services import voice as voice_service
from app.utils import utils

#: 编辑器能放的那几种声音。
SOUNDS: Tuple[str, ...] = ("hit",) + tuple(sorted(sound_design.BED_RMS_DB))

_lock = threading.Lock()
#: 正在后台听配音的任务。
_listening: Dict[str, threading.Thread] = {}
#: 听不了的那些：``任务 -> (配音的指纹, 原因)``。同一条配音不再试第二遍。
_failed: Dict[str, Tuple[list, str]] = {}


def _nothing(note: str = "", state: str = "none") -> dict:
    return {"state": state, "note": note}


def _files(task_id: str) -> Tuple[str, str, float]:
    """``(旁白, 字幕, 旁白多长)``。没有旁白时旁白是空串。"""
    payload = scene_manifest.load_raw(task_id)
    try:
        audio = scene_manifest.resolve_task_file(task_id, payload.get("audio_file"))
    except scene_manifest.SceneError:
        return "", "", 0.0
    try:
        subtitle = scene_manifest.resolve_task_file(task_id, payload.get("subtitle_path"))
    except scene_manifest.SceneError:
        subtitle = ""
    try:
        seconds = float(voice_service.get_audio_duration(audio) or 0.0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        try:
            seconds = float(payload.get("audio_duration") or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
    return audio, subtitle, seconds


def _listen(task_id: str, audio: str, task_dir: str) -> None:
    try:
        finishing.listen(audio, task_dir)
    except Exception as exc:  # 听不了不是编辑器的错：记下来，位置退回估的
        logger.warning(f"cannot time the narration of {task_id} for the editor: {exc}")
        try:
            stamp = finishing._audio_stamp(audio)
        except OSError:
            stamp = []
        with _lock:
            _failed[task_id] = (stamp, str(exc))
    finally:
        with _lock:
            _listening.pop(task_id, None)


def _start_listening(task_id: str, audio: str, task_dir: str) -> None:
    with _lock:
        if task_id in _listening:
            return
        worker = threading.Thread(
            target=_listen, args=(task_id, audio, task_dir),
            name=f"direction-{task_id}", daemon=True,
        )
        _listening[task_id] = worker
    worker.start()


def _gave_up(task_id: str, audio: str) -> str:
    """这条配音是不是已经试过、听不了；是的话返回原因。配音换过就再试。"""
    with _lock:
        failed = _failed.get(task_id)
    if not failed:
        return ""
    try:
        if failed[0] == finishing._audio_stamp(audio):
            return failed[1] or "the narration could not be timed"
    except OSError:
        pass
    with _lock:
        _failed.pop(task_id, None)
    return ""


def describe(task_id: str) -> dict:
    """这条片子的导演项，摆好了位置。

    ``state`` 是其中一个：

    * ``none`` —— 没有可预览的（没有分镜的导演项，也没有挑过的标题卡）；
    * ``off`` —— 分镜里写了，但出片时不会做（没选"成片观感"，或者一处也
      摆不上），``note`` 说原因；
    * ``listening`` —— 正在后台听配音，过一会儿再问；
    * ``ready`` —— 其余各项就是成片里的位置。
    """
    params = scene_editor.load_params(task_id)
    task_dir = utils.task_dir(task_id)
    directed = scene_plan.directed(finishing._plan_scenes(task_dir, params))
    try:
        finished = finishing.wanted(params)
    except finishing.FinishError as exc:
        return _nothing(str(exc), "off")
    if not finished:
        if directed:
            return _nothing(
                "The scene plan directs a title, cards or sound, but this video "
                "has no Finish look, so the render will not add them.", "off")
        return _nothing()
    audio, subtitle, seconds = _files(task_id)
    if not audio:
        return _nothing("Record the narration to see the direction." if directed else "")

    reason = _gave_up(task_id, audio)
    if not reason and finishing.waits_for_listening(params, subtitle, audio, task_dir):
        _start_listening(task_id, audio, task_dir)
        return _nothing(
            "Timing the direction against the narration. This takes a few "
            "minutes the first time and is kept for the render.", "listening")

    found = finishing.editor_direction(params, subtitle, audio, task_dir, seconds)
    shown = (
        found["title"] or found["cards"] or found["hits"]
        or found["music_off"] or found["beds"]
    )
    if not shown:
        if directed:
            return _nothing(
                "The scene plan has direction, but none of it could be placed: "
                "its narration lines are not in the recording.", "off")
        return _nothing()
    note = ""
    if found["timing"] == "estimate" and directed:
        note = (
            "Placed from the subtitle timings, which can be up to two seconds "
            "off" + (f" ({reason})." if reason else ".")
        )
    return {"state": "ready", "note": note, **found}


#: 换导演项之前的那份清单，留在任务目录里。只留第一份：没有导演项时的原样。
BACKUP = "script.before-direction.json"


def task_of(text: str) -> str:
    """任务号；给的是编辑器的地址（``...?task=<号>``）也认。"""
    text = str(text or "").strip()
    if "task=" in text:
        text = text.split("task=", 1)[1].split("&", 1)[0].split("#", 1)[0]
    return text


def _stored_plan(task_id: str) -> Tuple[dict, list]:
    payload = scene_manifest.load_raw(task_id)
    if not payload:
        raise scene_plan.ScenePlanError(f"there is no video {task_id!r}")
    params = payload.get("params")
    plan = params.get("scene_plan") if isinstance(params, dict) else None
    return payload, [scene for scene in plan or [] if isinstance(scene, dict)]


def outline(task_id: str) -> str:
    """这条片子的分镜，一段一行、带段号：写导演项之前先看它。"""
    _, plan = _stored_plan(task_id)
    if not plan:
        raise scene_plan.ScenePlanError(
            "this video was not made from a scene plan, so there are no "
            "scenes to direct"
        )
    return scene_plan.outline(plan)


def redirect(task_id: str, text: str) -> str:
    """给一条已经做出来的片子换一份导演项（格式见 ``scene_plan.redirected``）。

    只动清单里分镜的导演项：旁白、画面、时间线都不碰，也不出片。原来的清单
    留一份在旁边（:data:`BACKUP`）。返回写完之后该看一眼的那几行。
    """
    payload, plan = _stored_plan(task_id)
    spoken = narration_pauses.recorded(task_id)
    directed = scene_plan.redirected(plan, text, recorded=spoken)
    look = scene_plan.redirected_look(text)

    task_dir = utils.task_dir(task_id)
    backup = os.path.join(task_dir, BACKUP)
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=4)

    # 停顿是往录好的旁白里插静音：声音、字幕、时间线都跟着动（见
    # narration_pauses）。先做它——做不成就什么都还没改；做成了它会改清单，
    # 所以清单要重新读一遍再写分镜。
    wanted = {
        number: float(scene["direction"]["pause_after"])
        for number, scene in enumerate(directed, start=1)
        if (scene.get("direction") or {}).get("pause_after")
    }
    paused = None
    if any(seconds > spoken.get(number, 0.0) for number, seconds in wanted.items()) or (
            os.path.isfile(os.path.join(task_dir, narration_pauses.SIDECAR))):
        try:
            paused = narration_pauses.apply(task_id, wanted)
        except narration_pauses.PauseError as exc:
            raise scene_plan.ScenePlanError(str(exc)) from exc
        payload = scene_manifest.load_raw(task_id)
    payload["params"]["scene_plan"] = directed
    if look:
        payload["params"]["finish_look"] = look
    task_artifacts.write_script_data(task_id, payload)
    mapped = _place_maps(task_id, plan, directed)

    # 报告里讲画面的那几行（几段照着照片画、哪几段太长）对一条做完的片子没用。
    said = scene_plan.report({"scenes": directed}).splitlines()
    start = next(at for at, line in enumerate(said)
                 if line.startswith(("direction:", "no direction")))
    lines = said[start:]
    if look:
        lines.append(f"look: {look} (this video's Finish is now set to it)")
    if paused is not None:
        if paused["inserted"]:
            whole = paused["duration"] or 0.0
            lines.append(
                f"{len(paused['inserted'])} pause"
                f"{'' if len(paused['inserted']) == 1 else 's'} spliced into the "
                f"narration (+{paused['added']:.1f}s); it now runs "
                f"{int(whole // 60)}:{int(whole % 60):02d}. The pictures on screen "
                "at each pause hold for it"
            )
        else:
            lines.append("the pauses spliced into the narration earlier were removed")
    lines.extend(mapped)
    try:
        finished = bool(finishing.wanted(scene_editor.load_params(task_id)))
    except finishing.FinishError:
        finished = False
    if not finished:
        lines.append(
            "this video has no Finish look, so the render will not add any of "
            "it - choose one in the editor"
        )
    if os.path.isfile(os.path.join(task_dir, finishing._TITLE_CARDS_FILE)):
        lines.append(
            f"{finishing._TITLE_CARDS_FILE} in the video's folder decides the "
            "cards and overrides the ones written here - delete it to use these"
        )
    lines.append(
        "saved. The editor shows it on the Direction track; the finished video "
        "changes when it is rendered again"
    )
    return "\n".join(lines)


def _place_maps(task_id: str, before: list, directed: list) -> list:
    """分镜里写了地图的那几段，把时间线上念那句话的画面换成地图。返回要说的话。

    时间线是人排的，所以按台词找那一段（和停顿一样）。找不到（那一段被删了、
    台词改了）就说一声，不猜。原来有地图、这一次不要了的：画面不自动换回去
    ——原来那张图在这一段的历史里，在编辑器里点一下就回来。
    """
    said = []
    scenes = scene_manifest.load(task_id).get("scenes") or []
    shown = [" ".join(str(scene.get("narration") or "").lower().split()) for scene in scenes]
    cursor = 0
    for number, (old, new) in enumerate(zip(before, directed), start=1):
        key = " ".join(str(new.get("narration") or "").lower().split())
        at = next((n for n in range(cursor, len(shown)) if key and shown[n] == key), None)
        if at is not None:
            cursor = at + 1
        wanted = (new.get("direction") or {}).get("map")
        had = (old.get("direction") or {}).get("map")
        if wanted:
            if at is None:
                said.append(
                    f"scene {number}: its narration is not on the timeline any more, "
                    "so the map was not placed")
                continue
            try:
                scene_editor.place_map(task_id, at, wanted)
                said.append(f"scene {number} now shows a map of {wanted['label']}")
            except Exception as exc:
                said.append(f"scene {number}: the map could not be placed ({exc})")
        elif had:
            said.append(
                f"scene {number} no longer has a map in the file; its picture was "
                "left as it is - put the earlier picture back from the scene's "
                "history in the editor")
    return said


def sound_file(name: str) -> str:
    """一种声音的 WAV 文件，没有就先合成出来。认不出的名字报 ``ValueError``。

    电平写在文件里（和成片里混进去的是同一份波形），浏览器照原样放就行。
    文件名带着合成用的那几个数：数改了，就是另一个文件。
    """
    if name not in SOUNDS:
        raise ValueError(f"no such sound: {name!r}")
    if name == "hit":
        key = f"{sound_design.HIT_PEAK_DB:g}_{sound_design.HIT_SECONDS:g}"
    else:
        key = f"{sound_design.BED_RMS_DB[name]:g}_{sound_design.BED_SECONDS:g}"
    folder = utils.storage_dir("cache/direction-sounds", create=True)
    path = os.path.join(folder, f"{name}_{key}.wav".replace("-", "m"))
    if not os.path.isfile(path):
        sound = sound_design.hit() if name == "hit" else sound_design.bed(name)
        partial = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
        sound_design.write_wav(partial, sound)
        os.replace(partial, path)
    return path


if __name__ == "__main__":  # pragma: no cover
    # python -m app.services.direction_preview <task> [direction.json]
    import sys

    if len(sys.argv) not in (2, 3):
        sys.exit(
            "usage: python -m app.services.direction_preview <task id or editor "
            "address> [direction.json]\n"
            "  without a file: lists the video's scenes by number\n"
            "  with a file: replaces the video's direction with the file's"
        )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if len(sys.argv) == 2:
            print(outline(task_of(sys.argv[1])))
        else:
            with open(sys.argv[2], encoding="utf-8") as handle:
                print(redirect(task_of(sys.argv[1]), handle.read()))
    except scene_plan.ScenePlanError as problem:
        sys.exit(f"cannot direct this video: {problem}")
