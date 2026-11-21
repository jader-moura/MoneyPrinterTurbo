"""Video scenes: marked one by one, made in one batch, nothing else beside it.

What these hold
---------------
A clip takes minutes and the whole graphics card, so the feature is built on
three promises: a scene that cannot become a clip is refused when it is
marked, not forty minutes into a batch; while a batch runs no scene can be
edited, on any task; and a clip that fails or a batch that is stopped loses
nothing - finished clips stay, the rest stay marked, and the picture a clip
replaced can be put back.

The worker here is a stand-in script. It speaks the real worker's protocol
(see ``local_services/wanvideo/worker.py``) and writes a few bytes where the
real one writes a clip, so the batch logic runs for real: a process is
started, its stdout is read, and stopping it ends that process.
"""

import os
import shutil
import sys
import textwrap
import time

import pytest

from app.services import scene_editor, scene_manifest, scene_regen_queue, scene_video

from test.services.test_image_style_pinning import (  # noqa: F401 - fixtures
    _reviewed_task,
    idle_task,
    task_root,
)

FAKE_WORKER = textwrap.dedent(
    """
    import json, os, sys, time

    def say(event, **fields):
        print(json.dumps({"event": event, **fields}), flush=True)

    job = json.load(open(sys.argv[1], encoding="utf-8"))
    gate = os.environ.get("FAKE_GATE", "")
    print("a line a library printed")
    say("loading", what="loading the video model")
    say("ready", seconds=0.1)
    for clip in job["clips"]:
        say("clip", id=clip["id"])
        if "CRASH" in clip["prompt"]:
            os._exit(3)
        if "HOLD" in clip["prompt"]:
            # Until the test opens the gate, or the app ends this process.
            while gate and not os.path.exists(gate):
                time.sleep(0.02)
        for n in range(4):
            say("step", id=clip["id"], n=n + 1, of=4)
        if "FAIL" in clip["prompt"]:
            say("clip_failed", id=clip["id"], error="OutOfMemoryError: no room")
            continue
        with open(clip["out"], "wb") as handle:
            handle.write(json.dumps({k: clip.get(k) for k in ("frames", "image", "prompt")}).encode())
        say("clip_done", id=clip["id"], file=clip["out"], seconds=0.1)
    say("done")
    """
)


def wait_until(predicate, timeout=20.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def studio(task_root, tmp_path, monkeypatch):
    """A reviewed two-scene task, and a machine that can make video scenes."""
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    monkeypatch.setattr(scene_video, "WORKER", str(worker))
    monkeypatch.setattr(scene_video, "interpreter", lambda: sys.executable)
    monkeypatch.setattr(scene_video, "_log_path", lambda: str(tmp_path / "wanvideo.log"))
    freed = []
    monkeypatch.setattr(scene_video, "_free_the_card", lambda: freed.append(True))
    monkeypatch.setattr(scene_video.llm, "unload_ollama_model", lambda *a, **k: False)
    # Fitting is ffmpeg's job; here the drawn file is simply delivered.
    monkeypatch.setattr(
        scene_video, "_fit", lambda raw, target, needed, frames: shutil.copyfile(raw, target))
    scene_regen_queue._reset_for_tests()

    task_id, _ = _reviewed_task(task_root, "task-video")
    scenes = scene_manifest.load(task_id)["scenes"]
    for scene in scenes:
        scene["duration"] = 4.0
    scene_manifest.save_scenes(task_id, scenes)

    class Studio:
        pass

    made = Studio()
    made.task_id, made.gate, made.freed = task_id, gate, freed
    yield made
    gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    scene_video.dismiss()


def scenes_of(task_id):
    return scene_manifest.load(task_id)["scenes"]


def run_to_the_end(task_id):
    scene_video.start(task_id)
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    return scene_video.snapshot()


def test_frames_follow_the_scene_and_stay_what_the_model_accepts():
    assert scene_video.frames_for(3.0) == 53
    assert scene_video.frames_for(0.3) == scene_video.MIN_FRAMES
    # Longer than one clip: the most the model draws, slowed afterwards.
    assert scene_video.frames_for(8.0) == scene_video.MAX_FRAMES
    assert all((scene_video.frames_for(s / 10) - 1) % 4 == 0 for s in range(1, 120))


def test_a_fitted_clip_is_never_shorter_than_its_scene(tmp_path):
    """Run through the real ffmpeg: the scene would be clamped to a short clip."""
    import subprocess

    from app.services import timeline
    from app.utils import utils

    for needed in (1.8, 4.0, 7.146, scene_video.max_scene_seconds()):
        frames = scene_video.frames_for(needed)
        raw, fitted = str(tmp_path / f"raw-{needed}.mp4"), str(tmp_path / f"fit-{needed}.mp4")
        subprocess.run(
            [utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
             f"testsrc2=size=160x96:rate={scene_video.FPS}", "-frames:v", str(frames),
             "-pix_fmt", "yuv420p", raw], check=True)
        scene_video._fit(raw, fitted, needed, frames)
        assert timeline.probe_duration(fitted) >= needed, (needed, frames)


def test_a_marked_scene_becomes_a_clip_and_keeps_its_picture(studio):
    picture = scenes_of(studio.task_id)[0]["material"]
    scene_video.mark(studio.task_id, 0, "image")
    assert scenes_of(studio.task_id)[0]["animate"] == "image"

    result = run_to_the_end(studio.task_id)

    assert (result["state"], result["done"], result["failed"]) == ("done", 1, 0)
    assert studio.freed, "the image server was never asked to give up the card"
    first, second = scenes_of(studio.task_id)
    assert first["kind"] == "video" and os.path.isfile(first["material"])
    assert first["video"] == {
        "mode": "image",
        "file": os.path.basename(first["material"]),
        "still": os.path.basename(picture),
    }
    assert "animate" not in first and first["locked"]
    # The picture is one click away in "Earlier media".
    assert [entry["material"] for entry in first["history"]] == [os.path.basename(picture)]
    # The worker was handed that picture, and a 4s scene's worth of frames.
    assert '"frames": 69' in open(first["material"], encoding="utf-8").read()
    assert "video" not in second and second["kind"] == "image"
    # Nothing of the work folder is left to show up among the pictures.
    assert not os.path.exists(os.path.join(os.path.dirname(picture), scene_video.WORK_DIR))


def test_a_clip_from_the_prompt_needs_no_picture(studio):
    scene_video.mark(studio.task_id, 1, "text")
    run_to_the_end(studio.task_id)
    scene = scenes_of(studio.task_id)[1]
    assert scene["video"]["mode"] == "text" and "still" not in scene["video"]


def test_a_scene_too_long_for_one_clip_is_refused_when_marked(studio):
    scenes = scenes_of(studio.task_id)
    scenes[0]["duration"] = scene_video.max_scene_seconds() + 1
    scene_manifest.save_scenes(studio.task_id, scenes)
    with pytest.raises(scene_manifest.SceneError, match="at most 9.7s"):
        scene_video.mark(studio.task_id, 0, "text")
    assert "animate" not in scenes_of(studio.task_id)[0]


def test_footage_has_no_picture_to_animate(studio):
    scenes = scenes_of(studio.task_id)
    footage = os.path.join(os.path.dirname(scenes[0]["material"]), "stock.mp4")
    open(footage, "wb").write(b"x")
    scenes[0]["material"], scenes[0]["kind"] = footage, "video"
    scene_manifest.save_scenes(studio.task_id, scenes)
    with pytest.raises(scene_manifest.SceneError, match="no picture to animate"):
        scene_video.mark(studio.task_id, 0, "image")
    # From the prompt is still open to it.
    scene_video.mark(studio.task_id, 0, "text")


def test_a_mark_can_be_taken_off_and_nothing_marked_starts_nothing(studio):
    scene_video.mark(studio.task_id, 0, "image")
    scene_video.mark(studio.task_id, 0, "")
    assert "animate" not in scenes_of(studio.task_id)[0]
    with pytest.raises(scene_manifest.SceneError, match="no scene is marked"):
        scene_video.start(studio.task_id)


def test_every_picture_is_animated_in_one_go(studio):
    scenes = scenes_of(studio.task_id)
    scenes[0]["duration"] = scene_video.max_scene_seconds() + 1
    scene_manifest.save_scenes(studio.task_id, scenes)

    # Asking what would happen changes nothing.
    plan = scene_video.pictures(studio.task_id)
    assert plan["scenes"] == [scene["index"] for scene in scenes[1:]]
    assert [one["scene"] for one in plan["skipped"]] == [0]
    assert "at most 9.7s" in plan["skipped"][0]["reason"]
    assert not any("animate" in scene for scene in scenes_of(studio.task_id))

    studio.gate.write_text("open")
    scene_video.start(studio.task_id, everything=True)
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    after = scenes_of(studio.task_id)
    # The scene too long for a clip stays a picture; every other one moves.
    assert "video" not in after[0]
    assert all(scene["video"]["mode"] == "image" for scene in after[1:])
    # Nothing is left to animate, and the finished ones are not done twice.
    assert scene_video.pictures(studio.task_id)["scenes"] == []


def test_animating_everything_keeps_a_choice_made_by_hand(studio):
    scene_video.mark(studio.task_id, 0, "text")
    assert 0 not in scene_video.pictures(studio.task_id)["scenes"]
    studio.gate.write_text("open")
    scene_video.start(studio.task_id, everything=True)
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    assert scenes_of(studio.task_id)[0]["video"]["mode"] == "text"


def test_nothing_can_be_edited_while_a_batch_runs(studio):
    scenes = scenes_of(studio.task_id)
    scenes[0]["keyword"] = "HOLD the door"
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_video.mark(studio.task_id, 0, "text")
    scene_video.start(studio.task_id)
    wait_until(lambda: scene_video.snapshot().get("current"), what="the clip to start")

    for edit in (
        lambda: scene_editor.update_scene(studio.task_id, 1, keyword="other"),
        lambda: scene_editor.check_regenerate(studio.task_id, 1),
        lambda: scene_regen_queue.enqueue_prompt(studio.task_id, 1),
        lambda: scene_video.mark(studio.task_id, 1, "text"),
        lambda: scene_video.start(studio.task_id),
        # Another video altogether: the reason is the card, and there is one.
        lambda: scene_editor._require_idle("some-other-task"),
    ):
        with pytest.raises(scene_editor.SceneBusyError):
            edit()

    studio.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    scene_editor.update_scene(studio.task_id, 1, keyword="other")


def test_one_failed_clip_does_not_take_the_others_with_it(studio):
    scenes = scenes_of(studio.task_id)
    scenes[0]["keyword"] = "FAIL this one"
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_video.mark(studio.task_id, 0, "text")
    scene_video.mark(studio.task_id, 1, "text")

    result = run_to_the_end(studio.task_id)

    assert (result["state"], result["done"], result["failed"]) == ("done", 1, 1)
    assert "no room" in result["clips"][0]["error"]
    first, second = scenes_of(studio.task_id)
    # Still marked, so the next batch tries it again; its picture is untouched.
    assert first["animate"] == "text" and first["kind"] == "image"
    assert second["kind"] == "video"


def test_a_worker_that_dies_is_reported_and_does_not_hang_the_editor(studio):
    scenes = scenes_of(studio.task_id)
    scenes[0]["keyword"] = "CRASH"
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_video.mark(studio.task_id, 0, "text")

    result = run_to_the_end(studio.task_id)

    assert result["state"] == "done" and result["failed"] == 1
    assert "exit code 3" in result["clips"][0]["error"]
    scene_editor.update_scene(studio.task_id, 0, keyword="fine again")


def test_stopping_keeps_what_is_made_and_leaves_the_rest_marked(studio):
    scenes = scenes_of(studio.task_id)
    scenes[1]["keyword"] = "HOLD here"
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_video.mark(studio.task_id, 0, "text")
    scene_video.mark(studio.task_id, 1, "text")
    scene_video.start(studio.task_id)
    wait_until(lambda: scene_video.snapshot()["done"] == 1, what="the first clip")

    scene_video.stop()
    wait_until(lambda: not scene_video.running(), what="the batch to stop")

    result = scene_video.snapshot()
    assert result["state"] == "stopped"
    first, second = scenes_of(studio.task_id)
    assert first["kind"] == "video"
    assert second["animate"] == "text" and second["kind"] == "image"


def test_pictures_still_being_drawn_hold_the_batch_back(studio, monkeypatch):
    scene_video.mark(studio.task_id, 0, "image")
    monkeypatch.setattr(scene_regen_queue, "snapshot", lambda task_id="": {"busy": True})
    with pytest.raises(scene_editor.SceneBusyError, match="still being drawn"):
        scene_video.start(studio.task_id)


def test_the_record_does_not_outlive_the_clip_it_describes(studio):
    scene_video.mark(studio.task_id, 0, "image")
    run_to_the_end(studio.task_id)
    scenes = scenes_of(studio.task_id)
    picture = os.path.join(os.path.dirname(scenes[0]["material"]), "1.png")
    # Any other way of changing the picture just replaces the material.
    scenes[0]["material"] = picture
    scene_manifest.save_scenes(studio.task_id, scenes)
    assert "video" not in scenes_of(studio.task_id)[0]


def test_animating_again_starts_from_the_picture_not_from_the_clip(studio):
    picture = scenes_of(studio.task_id)[0]["material"]
    scene_video.mark(studio.task_id, 0, "image")
    run_to_the_end(studio.task_id)
    scene_video.dismiss()
    scene_video.mark(studio.task_id, 0, "image")
    run_to_the_end(studio.task_id)
    scene = scenes_of(studio.task_id)[0]
    assert scene["video"]["still"] == os.path.basename(picture)
    handed = open(scene["material"], encoding="utf-8").read()
    assert os.path.basename(picture) in handed


def test_earlier_media_tells_a_video_from_a_picture_and_puts_either_back(studio):
    from app.services import picture_shelf

    picture = os.path.basename(scenes_of(studio.task_id)[0]["material"])
    scene_video.mark(studio.task_id, 0, "image")
    run_to_the_end(studio.task_id)
    clip = os.path.basename(scenes_of(studio.task_id)[0]["material"])

    tiles = {tile["material"]: tile for tile in picture_shelf.shelf(studio.task_id)}
    assert (tiles[clip]["media"], tiles[clip]["video_mode"]) == ("video", "image")
    assert (tiles[picture]["media"], tiles[picture]["video_mode"]) == ("picture", "")

    # Back to the picture: the scene is no longer a video scene...
    scene_editor.restore_material(studio.task_id, 0, picture)
    scene = scenes_of(studio.task_id)[0]
    assert "video" not in scene and os.path.basename(scene["material"]) == picture
    # ...and back to the clip: it is one again, and still knows its picture.
    scene_editor.restore_material(studio.task_id, 0, clip)
    scene = scenes_of(studio.task_id)[0]
    assert scene["video"] == {"mode": "image", "file": clip, "still": picture}

    # A clip made before that was recorded is known by its name.
    scene_editor.restore_material(studio.task_id, 0, picture)
    scenes = scenes_of(studio.task_id)
    for entry in scenes[0]["history"]:
        entry.pop("video", None)
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_editor.restore_material(studio.task_id, 0, clip)
    assert scenes_of(studio.task_id)[0]["video"] == {"mode": "image", "file": clip}
