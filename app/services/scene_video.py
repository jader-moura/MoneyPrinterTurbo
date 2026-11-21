"""Video scenes: chosen scenes become generated clips, all in one batch.

Why a batch, and why nothing else may run beside it
---------------------------------------------------
A clip takes about six minutes on a 16 GB card (Wan 2.2 A14B, 4-step, 832x480)
and needs the whole card plus about 20 GB of host memory. The image server
cannot draw while that is going on, and neither can the language model. So
video is not queued a scene at a time like pictures are: the user marks every
scene that should move, then starts one batch. While it runs, every scene edit
is refused (``refuse_while_running``) and the editor shows one blocking
progress window.

Two ways to make a clip, chosen per scene:

``image``  animate the scene's own picture (image-to-video). The clip starts on
           that picture, so the look the user already approved is kept.
``text``   draw a clip from the scene's prompt alone (text-to-video).

How the card changes hands
--------------------------
The models do not live in this process or in the image server. A worker
process is started for a few clips at a time
(``local_services/wanvideo/worker.py``), and its exit is what returns the
memory. Before the first one starts, the image server is asked to drop its
model and Ollama to drop its own; the image server loads its model again by
itself the next time a picture is asked for.

A few clips per process because the worker holds one of the model's two halves
at a time: it draws the first half of every clip it was given, swaps, then
draws the second half. Nothing is finished until the swap, so a small group
keeps the first result, and what a stop throws away, to a few clips.

How a clip fits its scene
-------------------------
The model draws at most 81 frames at 16 fps, a little over five seconds, and
most narrated scenes are longer. A clip is therefore slowed to cover its scene
(up to ``MAX_STRETCH``) and brought to the renderer's 30 fps with motion
interpolation, so slow does not mean juddering. A scene too long even for that
is refused when it is marked, with the limit named.
"""

from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from loguru import logger

from app.config import config
from app.services import llm, material, scene_editor, scene_manifest, timeline
from app.services.scene_manifest import SceneError
from app.utils import utils

MODES = scene_manifest.VIDEO_MODES

#: What the model draws: frames per second, and the most and fewest frames.
FPS = 16
MAX_FRAMES = 81
MIN_FRAMES = 17
#: How far a clip may be slowed to cover its scene. At 2x a five-second clip
#: covers ten; slower than that reads as slow motion rather than as footage.
MAX_STRETCH = 2.0
#: The frame rate clips are delivered at, which is the renderer's own.
OUT_FPS = 30
#: Close to 480p in each shape, in multiples of 16 as the model requires.
SIZES = {"16:9": (832, 480), "9:16": (480, 832), "1:1": (624, 624)}
#: Measured on an RTX 5060 Ti for a full 81-frame clip, loading aside.
TYPICAL_CLIP_SECONDS = 360
#: What one worker process costs before and between drawing: the text encoder
#: and each half of the model are read from disk once.
TYPICAL_GROUP_SECONDS = 150
#: How many clips one worker process is given.
CLIPS_PER_GROUP = 4
#: Denoising steps a clip reports. Decoding follows them and takes about as
#: long as one, so progress within a clip is counted out of one more.
STEPS = 4

WORK_DIR = "wan-work"
WORKER = os.path.join(
    utils.root_dir(), "local_services", "wanvideo", "worker.py"
)

WAITING, RUNNING, DONE, FAILED, STOPPED = "waiting", "running", "done", "failed", "stopped"


#: Added to what a scene needs when its clip is fitted. The timeline clamps a
#: scene to the length of its material, so a clip a frame short would shorten
#: the scene; a few frames over are simply not shown.
SPARE_SECONDS = 0.15


def _motion_seconds(frames: int) -> float:
    """How much footage a clip of this many frames really delivers.

    Two frames fewer than drawn. N frames span N-1 intervals, and motion
    interpolation needs a frame after the one it is filling towards, so it
    stops one interval early: measured, an 81-frame clip slowed to cover
    7.30s came out 7.13s long.
    """
    return max(1, frames - 2) / FPS


def max_scene_seconds() -> float:
    """The longest scene one clip can cover."""
    return math.floor((_motion_seconds(MAX_FRAMES) * MAX_STRETCH - SPARE_SECONDS) * 10) / 10


def frames_for(source_seconds: float) -> int:
    """How many frames to draw for this much footage. Always 4k+1, as Wan needs."""
    # Two more than the footage is long, for the reason in ``_motion_seconds``.
    wanted = math.ceil((max(0.0, source_seconds) + SPARE_SECONDS) * FPS) + 2
    frames = math.ceil(max(0, wanted - 1) / 4) * 4 + 1
    return max(MIN_FRAMES, min(MAX_FRAMES, frames))


# ---------------------------------------------------------------------------
# Can this machine do it at all
# ---------------------------------------------------------------------------


def interpreter() -> str:
    """The Python that runs the worker, or "" when there is none.

    The image server's interpreter: it is the one with a torch built for this
    card. Named outright in ``wan_video_python`` or ``GEN_PYTHON``, otherwise
    looked for where ``scripts/start-local.sh`` looks for the image server's.
    Never the app's own: its torch, if it has one, is usually CPU-only.
    """
    root = utils.root_dir()
    candidates = [
        str(config.app.get("wan_video_python", "") or "").strip(),
        str(os.environ.get("GEN_PYTHON", "") or "").strip(),
        os.path.join(root, "local_services", "genserver", ".venv", "Scripts", "python.exe"),
        os.path.join(root, "local_services", "genserver", ".venv", "bin", "python"),
        os.path.join(root, "..", "video-generator", "scripts", "genserver", ".venv",
                     "Scripts", "python.exe"),
        os.path.join(root, "..", "video-generator", "scripts", "genserver", ".venv",
                     "bin", "python"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.expanduser(candidate)):
            return os.path.abspath(os.path.expanduser(candidate))
    return ""


def availability() -> dict:
    """Whether video scenes can be made here, and if not, what is missing."""
    if not os.path.isfile(WORKER):
        return {"available": False, "reason": "the video worker is not installed"}
    if not interpreter():
        return {
            "available": False,
            "reason": "no Python with a GPU build of torch was found; set "
                      "wan_video_python in config.toml to the image server's interpreter",
        }
    try:
        utils.get_ffmpeg_binary()
    except Exception as exc:  # noqa: BLE001 - whatever it is, it is the reason
        return {"available": False, "reason": f"ffmpeg is not available: {exc}"}
    return {"available": True, "reason": ""}


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------


class _Batch:
    def __init__(self, task_id: str, clips: list[dict]) -> None:
        self.task_id = task_id
        self.clips = clips
        self.state = RUNNING
        self.error = ""
        self.phase = "preparing"
        self.note = "Freeing the graphics card"
        self.started_at = time.time()
        self.finished_at = 0.0
        self.current: Optional[dict] = None
        self.groups_left = 0
        self.stopping = False
        self.process: Optional[subprocess.Popen] = None


_LOCK = threading.Lock()
_BATCH: Optional[_Batch] = None


def running() -> bool:
    with _LOCK:
        return _BATCH is not None and _BATCH.state == RUNNING


def refuse_while_running() -> None:
    """Raises while a batch runs. Every scene edit goes through this.

    For every task, not only the one being animated: the reason is the
    graphics card, and there is one.
    """
    with _LOCK:
        batch = _BATCH
        if batch is None or batch.state != RUNNING:
            return
        done = sum(1 for clip in batch.clips if clip["state"] in (DONE, FAILED))
        total = len(batch.clips)
    raise scene_editor.SceneBusyError(
        f"video scenes are being made ({done} of {total} finished) and they need "
        "the whole graphics card; wait for them to finish, or stop them"
    )


def snapshot() -> dict:
    """The batch as the editor shows it. Also says whether one can be started."""
    state = availability()
    payload: dict[str, Any] = {
        **state,
        "running": False,
        "typical_clip_seconds": TYPICAL_CLIP_SECONDS,
        "typical_group_seconds": TYPICAL_GROUP_SECONDS,
        "clips_per_group": CLIPS_PER_GROUP,
        "max_scene_seconds": round(max_scene_seconds(), 2),
    }
    with _LOCK:
        batch = _BATCH
        if batch is None:
            return payload
        clips = [dict(clip) for clip in batch.clips]
        current = batch.current["id"] if batch.current else ""
        now = time.time()
        # Work is counted in frames: a clip of fewer frames is that much
        # quicker. A clip part-way through counts for the steps it has had,
        # with decoding as one more.
        def progress(clip: dict) -> float:
            if clip["state"] in (DONE, FAILED, STOPPED):
                return 1.0
            return min(clip.get("step", 0), STEPS) / (STEPS + 1)

        worked = sum(clip["frames"] * progress(clip) for clip in clips if clip["state"] != FAILED)
        left = sum(clip["frames"] * (1 - progress(clip)) for clip in clips)
        elapsed = (batch.finished_at or now) - batch.started_at
        if worked >= MAX_FRAMES:
            # A full clip's worth is behind us: go by what this machine did,
            # loading included.
            remaining = left * elapsed / worked
        else:
            remaining = (left * TYPICAL_CLIP_SECONDS / MAX_FRAMES
                         + batch.groups_left * TYPICAL_GROUP_SECONDS)
        payload.update({
            "running": batch.state == RUNNING,
            "task_id": batch.task_id,
            "state": batch.state,
            "error": batch.error,
            "phase": batch.phase,
            "note": batch.note,
            "stopping": batch.stopping,
            "total": len(clips),
            "done": sum(1 for clip in clips if clip["state"] == DONE),
            "failed": sum(1 for clip in clips if clip["state"] == FAILED),
            "current": current,
            "steps": STEPS,
            "elapsed": round(elapsed, 1),
            "remaining": round(remaining) if batch.state == RUNNING else 0,
            "clips": [
                {
                    "id": clip["id"], "scene": clip["index"], "mode": clip["mode"],
                    "state": clip["state"], "error": clip.get("error", ""),
                    "step": clip.get("step", 0),
                }
                for clip in clips
            ],
        })
    return payload


# ---------------------------------------------------------------------------
# Choosing scenes
# ---------------------------------------------------------------------------


def _still_of(task_id: str, scene: dict) -> str:
    """The picture to animate for this scene, or "" when it has none.

    A scene that is already a video clip is animated again from the picture it
    was made from, not from a frame of the clip.
    """
    name = str((scene.get("video") or {}).get("still") or scene.get("material") or "")
    if not name:
        return ""
    try:
        resolved = scene_manifest.resolve_material(task_id, name)
    except SceneError:
        return ""
    if scene_manifest.material_kind(resolved) == "image":
        return resolved
    return timeline.source_image_for(task_id, resolved) or ""


def file_to_show(task_id: str, index: Any) -> dict:
    """The file a person means by "this scene's picture", to point at in the folder.

    That is the image itself, not the clip the scene plays: a picture is stored
    as a clip named by a digest, and a video scene plays a generated clip, but
    the file to take somewhere else is the image both came from. A scene with
    no image behind it (footage, a clip drawn from the prompt) shows what it
    plays.
    """
    scenes = scene_editor._scenes_or_fail(task_id)
    scene = scenes[scene_editor._scene_at(scenes, index)]
    still = _still_of(task_id, scene)
    if still:
        return {"file": os.path.basename(still), "what": "picture"}
    name = scene_manifest.material_key(scene.get("material"))
    if not name:
        raise SceneError(
            f"scene {int(scene.get('index', 0)) + 1} has no picture yet, so there "
            "is no file to show")
    return {"file": name, "what": "video"}


def _plan_clip(task_id: str, params, scene: dict, mode: str) -> dict:
    """Everything the worker needs for one scene, or a named reason it cannot."""
    number = int(scene.get("index", 0)) + 1
    if mode not in MODES:
        raise SceneError(f"video mode must be one of {', '.join(MODES)}, got {mode!r}")

    speed = float(getattr(params, "video_clip_speed", 1.0) or 1.0)
    if str(scene.get("material") or "").strip():
        on_screen = scene_editor._on_screen_seconds(task_id, scene)
    else:
        on_screen = float(scene.get("duration") or scene.get("clip_duration") or 0) or 5.0
    needed = on_screen * max(speed, 1e-6)
    if needed > max_scene_seconds() + 1e-6:
        raise SceneError(
            f"scene {number} is on screen for {on_screen:.1f}s and a video scene can "
            f"cover at most {max_scene_seconds():.1f}s; shorten or split it first"
        )

    term = str(scene.get("keyword") or "").strip() or scene_editor.suggested_keyword(
        task_id, int(scene.get("index", 0)))
    if not term:
        raise SceneError(f"scene {number} has no prompt to make a video from")

    clip = {
        "id": scene_manifest.scene_id(scene) or scene_manifest.new_scene_id(),
        "index": int(scene.get("index", 0)),
        "mode": mode,
        "needed": round(needed, 3),
        "frames": frames_for(needed),
        "seed": random.randint(1, 2**31 - 1),
        "state": WAITING,
    }
    if mode == "image":
        still = _still_of(task_id, scene)
        if not still:
            raise SceneError(
                f"scene {number} has no picture to animate (stock footage and "
                "uploaded videos have none); make it from the prompt instead"
            )
        clip["image"] = still
        # The picture already carries the style; the prompt only says what is
        # in it, which is what the model needs to move it plausibly.
        clip["prompt"] = term
    else:
        template = scene_editor._image_prompt_template(params, scene.get("image_style"))
        clip["prompt"] = material._openai_image_prompt(term, template)

    aspect = scene_editor._enum_value(getattr(params, "video_aspect", "")) or "9:16"
    clip["width"], clip["height"] = SIZES.get(aspect, SIZES["9:16"])
    return clip


def mark(task_id: str, index: Any, mode: str) -> dict:
    """Mark a scene to become a video clip in the next batch, or unmark it ("").

    Checked now rather than when the batch starts: a scene with no picture to
    animate, or one too long for a clip, should be said on the click and not
    discovered forty minutes into a batch.
    """
    scene_editor._require_idle(task_id)
    wanted = str(mode or "").strip()
    with scene_editor._editing(task_id):
        scenes = scene_editor._scenes_or_fail(task_id)
        position = scene_editor._scene_at(scenes, index)
        candidate = dict(scenes[position])
    if wanted:
        _plan_clip(task_id, scene_editor.load_params(task_id), candidate, wanted)
    with scene_editor._editing(task_id):
        scenes = scene_editor._scenes_or_fail(task_id)
        position = scene_editor._locate_scene(
            scenes, position, scene_manifest.scene_id(candidate))
        scene = scenes[position]
        if wanted:
            scene["animate"] = wanted
        else:
            scene.pop("animate", None)
        scene_manifest.save_scenes(task_id, scenes)
        return scene


def pictures(task_id: str) -> dict:
    """Which scenes "animate all pictures" would animate, and which it cannot.

    Every still picture that is not already a video and not already marked. A
    scene too long for a clip is left out and named, as it would be if it were
    marked by hand. Maps and scenes with nothing drawn yet are not pictures to
    animate and are passed over without a word.
    """
    params = scene_editor.load_params(task_id)
    found, skipped = [], []
    for scene in scene_editor._scenes_or_fail(task_id):
        if scene.get("animate") in MODES or scene.get("video"):
            continue
        if (scene.get("direction") or {}).get("map"):
            continue
        if not str(scene.get("material") or "").strip():
            continue
        if not _still_of(task_id, scene):
            continue
        try:
            _plan_clip(task_id, params, scene, "image")
        except SceneError as exc:
            skipped.append({"scene": int(scene.get("index", 0)), "reason": str(exc)})
            continue
        found.append(int(scene.get("index", 0)))
    return {"scenes": found, "skipped": skipped}


def _mark_pictures(task_id: str) -> int:
    """Mark every picture `pictures` names, exactly as a mark by hand would."""
    wanted = set(pictures(task_id)["scenes"])
    with scene_editor._editing(task_id):
        scenes = scene_editor._scenes_or_fail(task_id)
        for scene in scenes:
            if int(scene.get("index", -1)) in wanted and scene.get("animate") not in MODES:
                scene["animate"] = "image"
        if wanted:
            scene_manifest.save_scenes(task_id, scenes)
    return len(wanted)


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def start(task_id: str, everything: bool = False) -> dict:
    """Start one batch for every marked scene of this task.

    With `everything`, every still picture is marked first, so the batch
    animates the whole video in one go.
    """
    global _BATCH
    scene_editor._require_idle(task_id)
    state = availability()
    if not state["available"]:
        raise SceneError(f"video scenes cannot be made here: {state['reason']}")

    # Pictures still being drawn hold the card, and the batch is about to ask
    # the image server to drop its model.
    from app.services import scene_regen_queue

    if scene_regen_queue.snapshot()["busy"]:
        raise scene_editor.SceneBusyError(
            "pictures are still being drawn; wait for the queue to finish "
            "before making video scenes"
        )

    if everything:
        _mark_pictures(task_id)
    params = scene_editor.load_params(task_id)
    scenes = scene_editor._scenes_or_fail(task_id)
    marked = [scene for scene in scenes if scene.get("animate") in MODES]
    if not marked:
        raise SceneError("no scene is marked to become a video; mark some first")
    clips = [_plan_clip(task_id, params, scene, scene["animate"]) for scene in marked]

    with _LOCK:
        if _BATCH is not None and _BATCH.state == RUNNING:
            raise scene_editor.SceneBusyError("video scenes are already being made")
        batch = _Batch(task_id, clips)
        _BATCH = batch
    threading.Thread(
        target=_run, args=(batch,), name="scene-video-batch", daemon=True
    ).start()
    logger.success(f"started {len(clips)} video scene(s) for task {task_id}")
    return snapshot()


def stop() -> dict:
    """Stop the batch. Clips already finished stay; the rest stay marked."""
    with _LOCK:
        batch = _BATCH
        if batch is None or batch.state != RUNNING:
            raise SceneError("no video scenes are being made")
        batch.stopping = True
        batch.note = "Stopping"
        process = batch.process
    _end_process(process)
    return snapshot()


def dismiss() -> dict:
    """Forget a finished batch, so the editor stops showing its result."""
    global _BATCH
    with _LOCK:
        if _BATCH is not None and _BATCH.state != RUNNING:
            _BATCH = None
    return snapshot()


def _end_process(process: Optional[subprocess.Popen]) -> None:
    """End the worker, and whatever it started.

    The whole tree on Windows: a venv's python.exe there is a launcher, so the
    process holding the model is its child, and ``terminate`` on the launcher
    would leave 10 GB of model running.
    """
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _free_the_card() -> None:
    """Ask the image server to drop its model.

    Only an image server on this machine: one somewhere else does not share
    this card. A server that is not running, or is not ours and has no
    ``/unload``, needs nothing. One that is busy drawing is a reason to stop.
    """
    base = str(config.app.get("openai_image_base_url", "") or "").strip()
    host = (urlparse(base).hostname or "").lower() if base else ""
    if host in ("127.0.0.1", "localhost", "::1"):
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        try:
            answer = requests.post(f"{root}/unload", json={}, timeout=120)
        except requests.RequestException as exc:
            logger.info(f"the image server did not answer an unload ({type(exc).__name__}); "
                        "taking it as not running")
        else:
            if answer.status_code == 503:
                raise SceneError(
                    "the image server is busy and would not give up the graphics "
                    "card; wait for it to finish and start again"
                )
            logger.info(f"asked the image server to unload (status {answer.status_code})")


def _log_path() -> str:
    """Where the worker's own output goes: tracebacks, and what the libraries print."""
    return os.path.join(utils.storage_dir("logs", create=True), "wanvideo.log")


def _work_dir(task_id: str) -> str:
    folder = os.path.join(utils.task_dir(task_id), WORK_DIR)
    os.makedirs(folder, exist_ok=True)
    return folder


def _run(batch: _Batch) -> None:
    try:
        groups = [
            (mode, clips[at:at + CLIPS_PER_GROUP])
            for mode in MODES
            for clips in [[clip for clip in batch.clips if clip["mode"] == mode]]
            for at in range(0, len(clips), CLIPS_PER_GROUP)
        ]
        batch.groups_left = len(groups)
        _free_the_card()
        for number, (mode, clips) in enumerate(groups):
            if batch.stopping:
                break
            # Before every group, not once: Ollama loads its model again the
            # moment anything asks it a question, and a 10 GB language model
            # beside the video model fills the card. Measured: the clip being
            # decoded then took minutes instead of seconds.
            llm.unload_ollama_model()
            _run_worker(batch, mode, clips, number)
            batch.groups_left = len(groups) - number - 1
    except SceneError as exc:
        batch.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - the window must end either way
        batch.error = f"unexpected error: {exc}"
        logger.exception(f"the video scene batch for task {batch.task_id} broke")
    finally:
        with _LOCK:
            for clip in batch.clips:
                if clip["state"] in (WAITING, RUNNING):
                    clip["state"] = STOPPED if batch.stopping else FAILED
                    if not batch.stopping:
                        clip["error"] = batch.error or "the batch ended before this clip"
            made = sum(1 for clip in batch.clips if clip["state"] == DONE)
            batch.state = STOPPED if batch.stopping else (
                FAILED if batch.error and not made else DONE)
            batch.current = None
            batch.process = None
            batch.finished_at = time.time()
        shutil.rmtree(os.path.join(utils.task_dir(batch.task_id), WORK_DIR), ignore_errors=True)
        logger.info(
            f"video scene batch for task {batch.task_id} ended {batch.state}: "
            f"{made} of {len(batch.clips)} made"
        )


def _run_worker(batch: _Batch, mode: str, clips: list[dict], number: int) -> None:
    """One worker process for a few clips of one kind; returns when it has exited."""
    folder = _work_dir(batch.task_id)
    by_id = {clip["id"]: clip for clip in clips}
    for clip in clips:
        clip["raw"] = os.path.join(folder, f"{mode}-{clip['id']}.mp4")
    job = os.path.join(folder, f"job-{number}.json")
    with open(job, "w", encoding="utf-8") as handle:
        json.dump({
            "mode": mode,
            "ffmpeg": utils.get_ffmpeg_binary(),
            "clips": [
                {key: clip[key] for key in
                 ("id", "prompt", "width", "height", "frames", "seed", "image")
                 if key in clip} | {"out": clip["raw"]}
                for clip in clips
            ],
        }, handle)

    log_path = _log_path()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    with _LOCK:
        batch.phase, batch.note = "loading", "Loading the video model"
    fatal = ""
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {mode} x{len(clips)} "
                  f"task {batch.task_id} =====\n")
        log.flush()
        process = subprocess.Popen(
            # It watches this process and ends with it. Its stdin is nothing at
            # all: a worker waiting on a pipe never finishes importing torch.
            [interpreter(), WORKER, job, "--parent-pid", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            env=env, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with _LOCK:
            batch.process = process
        try:
            for line in process.stdout:
                event = _parse(line)
                if not event:
                    continue
                kind = event.get("event")
                clip = by_id.get(str(event.get("id") or ""))
                if kind == "loading":
                    with _LOCK:
                        batch.note = str(event.get("what") or batch.note).capitalize()
                elif kind == "clip" and clip:
                    with _LOCK:
                        clip["state"] = RUNNING
                        batch.current, batch.phase = clip, "drawing"
                        # The last visit to a clip is its decode.
                        doing = "Finishing" if clip.get("step", 0) >= STEPS else "Drawing"
                        batch.note = f"{doing} scene {clip['index'] + 1}"
                elif kind == "step" and clip:
                    with _LOCK:
                        clip["step"] = int(event.get("n") or 0)
                elif kind == "clip_done" and clip:
                    _finish_clip(batch, clip)
                elif kind == "clip_failed" and clip:
                    with _LOCK:
                        clip["state"], clip["error"] = FAILED, str(event.get("error") or "failed")
                    logger.warning(f"video scene {clip['index'] + 1} failed: {clip['error']}")
                elif kind == "fatal":
                    fatal = str(event.get("error") or "the video worker failed")
        finally:
            _end_process(process)
            code = process.wait()
    if batch.stopping:
        return
    unfinished = [clip for clip in clips if clip["state"] in (WAITING, RUNNING)]
    if unfinished:
        reason = fatal or (
            f"the video worker stopped unexpectedly (exit code {code}); "
            f"see {log_path}"
        )
        with _LOCK:
            for clip in unfinished:
                clip["state"], clip["error"] = FAILED, reason
        logger.warning(f"video worker for task {batch.task_id}: {reason}")


def _parse(line: str) -> Optional[dict]:
    """One progress line, or None for anything a library printed instead."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) and event.get("event") else None


def _fit(raw: str, target: str, needed: float, frames: int) -> None:
    """The drawn clip, slowed to cover its scene and brought to 30 fps.

    Motion interpolation rather than repeating frames: 16 fps slowed to cover
    an eight-second scene is 10 fps, which stutters. A build of ffmpeg without
    the filter falls back to repeating frames instead of losing the clip.
    """
    factor = max(1.0, (needed + SPARE_SECONDS) / _motion_seconds(frames))
    slow = f"setpts={factor:.5f}*PTS,"
    smooth = f"minterpolate=fps={OUT_FPS}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    part = target + ".part.mp4"
    last = ""
    for motion in (smooth, f"fps={OUT_FPS}"):
        done = subprocess.run(
            [utils.get_ffmpeg_binary(), "-v", "error", "-y", "-i", raw, "-vf", slow + motion,
             "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
             "-pix_fmt", "yuv420p", part],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode == 0 and os.path.isfile(part):
            os.replace(part, target)
            return
        last = done.stderr.decode("utf-8", "replace")[-300:]
    raise SceneError(f"ffmpeg could not prepare the clip: {last}")


def _finish_clip(batch: _Batch, clip: dict) -> None:
    """Fit the clip to its scene and put it on the timeline."""
    try:
        target = os.path.join(
            utils.task_dir(batch.task_id), f"wan-{clip['mode']}-{clip['id']}-{clip['seed']}.mp4")
        with _LOCK:
            batch.note = f"Fitting scene {clip['index'] + 1} to its length"
        _fit(clip["raw"], target, clip["needed"], clip["frames"])
        _place(batch.task_id, clip, target)
    except Exception as exc:  # noqa: BLE001 - one clip must not end the batch
        with _LOCK:
            clip["state"], clip["error"] = FAILED, str(exc)
        logger.warning(f"video scene {clip['index'] + 1} could not be placed: {exc}")
        return
    with _LOCK:
        clip["state"] = DONE
    logger.success(
        f"scene {clip['index'] + 1} of task {batch.task_id} is now a video clip "
        f"({clip['mode']}, {clip['frames']} frames)")


def _place(task_id: str, clip: dict, target: str) -> None:
    """Point the scene at its clip. The picture it replaces goes into its history,
    so "Earlier pictures" puts it back."""
    with scene_editor._editing(task_id):
        scenes = scene_editor._scenes_or_fail(task_id)
        position = scene_editor._locate_scene(scenes, clip["index"], clip["id"])
        scene = scenes[position]
        still = str((scene.get("video") or {}).get("still") or scene.get("material") or "")
        scene_manifest.remember_material(scene)
        scene["material"] = target
        scene["kind"] = scene_manifest.material_kind(target)
        record = {"mode": clip["mode"], "file": os.path.basename(target)}
        if clip["mode"] == "image" and still:
            record["still"] = scene_manifest.material_key(still)
        scene["video"] = record
        scene.pop("animate", None)
        # The in-point belonged to the picture's clip, not to this one.
        scene.pop("trim_start", None)
        # Chosen by hand, like a redrawn picture: batch redraws leave it alone.
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        clip["index"] = position
