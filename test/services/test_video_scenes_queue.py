"""Video scenes for a second video wait their turn instead of being refused.

In the user's words: "while the scenes from task 1 are being generated, we go
to task 2 and hit generate video scenes there too ... it will go to the scenes
generation queue and will generate the scenes for task 2 just after finishing
the task 1 scenes."

One video at a time has the graphics card. A batch asked for meanwhile is
queued and starts by itself, whichever way the one ahead of it ended.
"""

from pathlib import Path

import pytest

from app.services import scene_editor, scene_manifest, scene_video

from test.services.test_scene_video import (  # noqa: F401 - fixtures
    _reviewed_task,
    idle_task,
    scenes_of,
    studio,
    task_root,
    wait_until,
)

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture
def two(studio, task_root):  # noqa: F811
    """The studio's video with a clip that holds until the gate opens, and a
    second video with one scene marked."""
    with scene_video._LOCK:
        scene_video._QUEUE.clear()
        scene_video._ENDED.clear()
    scenes = scenes_of(studio.task_id)
    scenes[0]["keyword"] = "HOLD here"
    scene_manifest.save_scenes(studio.task_id, scenes)
    scene_video.mark(studio.task_id, 0, "text")

    other, _ = _reviewed_task(task_root, "task-two")
    scenes = scenes_of(other)
    for scene in scenes:
        scene["duration"] = 4.0
    scene_manifest.save_scenes(other, scenes)
    scene_video.mark(other, 0, "text")

    studio.other = other
    yield studio
    studio.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="every batch to end")
    with scene_video._LOCK:
        scene_video._QUEUE.clear()
        scene_video._ENDED.clear()


def running_task():
    shot = scene_video.snapshot()
    return shot.get("task_id") if shot["running"] else None


def test_a_second_video_is_queued_and_starts_when_the_first_ends(two):
    scene_video.start(two.task_id)
    shot = scene_video.start(two.other)

    # Accepted, not refused; the first still has the card.
    assert shot["running"] and shot["task_id"] == two.task_id
    (waiting,) = shot["queue"]
    assert (waiting["task_id"], waiting["state"], waiting["position"]) == (
        two.other, "queued", 1)
    assert waiting["total"] == 1 and waiting["seconds"] > 0
    assert scenes_of(two.other)[0]["kind"] == "image"

    two.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="both batches to end")

    assert scenes_of(two.task_id)[0]["kind"] == "video"
    assert scenes_of(two.other)[0]["kind"] == "video"
    shot = scene_video.snapshot()
    assert shot["task_id"] == two.other and shot["state"] == "done"
    assert shot["queue"] == []
    # How the first one went is still there for its own editor to tell.
    (ended,) = shot["ended"]
    assert (ended["task_id"], ended["state"], ended["done"], ended["ended"]) == (
        two.task_id, "done", 1, True)


def test_no_picture_can_be_drawn_between_the_two(two):
    """The card changes hands in one step: there is no moment in which the
    first has ended and the second has not begun."""
    seen = []
    ended = scene_video._remember_ended

    def watch(batch):
        # Called with the lock held, at the handover.
        seen.append(scene_video._BATCH is batch)
        ended(batch)

    scene_video._remember_ended = watch
    try:
        scene_video.start(two.task_id)
        scene_video.start(two.other)
        two.gate.write_text("open")
        wait_until(lambda: not scene_video.running(), what="both batches to end")
    finally:
        scene_video._remember_ended = ended
    assert seen == [True]
    assert scene_video.snapshot()["task_id"] == two.other


def test_the_same_video_is_not_queued_twice(two):
    scene_video.start(two.task_id)
    with pytest.raises(scene_editor.SceneBusyError, match="already being made"):
        scene_video.start(two.task_id)
    scene_video.start(two.other)
    with pytest.raises(scene_editor.SceneBusyError, match="already waiting"):
        scene_video.start(two.other)
    assert len(scene_video.snapshot()["queue"]) == 1


def test_stopping_the_one_running_lets_the_next_begin(two):
    scenes = scenes_of(two.other)
    scenes[0]["keyword"] = "HOLD too"
    scene_manifest.save_scenes(two.other, scenes)
    scene_video.start(two.task_id)
    scene_video.start(two.other)

    scene_video.stop()
    wait_until(lambda: running_task() == two.other, what="the second to begin")

    (ended,) = scene_video.snapshot()["ended"]
    assert ended["task_id"] == two.task_id and ended["state"] == "stopped"
    assert scenes_of(two.task_id)[0]["animate"] == "text"


def test_a_video_can_leave_the_queue_without_touching_the_one_running(two):
    scene_video.start(two.task_id)
    scene_video.start(two.other)

    shot = scene_video.forget(two.other)

    assert shot["queue"] == [] and shot["task_id"] == two.task_id and shot["running"]
    two.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="the batch to end")
    # Never made, and still marked for another time.
    other = scenes_of(two.other)[0]
    assert other["kind"] == "image" and other["animate"] == "text"


def test_what_is_made_is_what_is_marked_when_the_turn_comes(two):
    scene_video.start(two.task_id)
    scene_video.start(two.other)
    # While waiting, the user changes their mind about which scene moves.
    scene_video.mark(two.other, 0, "")
    scene_video.mark(two.other, 1, "text")

    two.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="both batches to end")

    first, second = scenes_of(two.other)
    assert first["kind"] == "image" and second["kind"] == "video"


def test_nothing_left_marked_is_said_and_does_not_hold_up_the_rest(two):
    scene_video.start(two.task_id)
    scene_video.start(two.other)
    scene_video.mark(two.other, 0, "")

    two.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="both batches to end")

    shot = scene_video.snapshot()
    assert shot["task_id"] == two.other and shot["state"] == "failed"
    assert "still marked" in shot["error"]


def test_a_video_waiting_its_turn_cannot_be_deleted_from_under_the_queue(two):
    scene_video.start(two.task_id)
    scene_video.start(two.other)
    with pytest.raises(scene_editor.SceneBusyError, match="waiting its turn"):
        scene_video.refuse_for_task(two.other)
    scene_video.forget(two.other)
    scene_video.refuse_for_task(two.other)


def test_closing_a_result_forgets_it(two):
    scene_video.start(two.task_id)
    scene_video.start(two.other)
    two.gate.write_text("open")
    wait_until(lambda: not scene_video.running(), what="both batches to end")

    assert scene_video.forget(two.task_id)["ended"] == []


class TestThePage:
    def test_the_button_queues_when_another_video_has_the_card(self):
        assert '`${elsewhere ? "Queue" : "Make"} ${marked} `' in JS
        assert "const toQueue = videoElsewhere(video) && !videoQueued(video)" in JS

    def test_a_waiting_video_says_so_and_can_leave(self):
        assert "Waiting its turn · " in JS
        assert 'ui.videoStop.textContent = "Leave the queue";' in JS
        stop = JS.split('ui.videoStop.addEventListener("click"')[1].split("\n});")[0]
        leave = stop.index("/video-scenes`);")
        assert stop.index("if (videoQueued())") < leave < stop.index(
            'call("DELETE", "/video-scenes")')

    def test_closing_a_result_never_stops_another_videos_batch(self):
        done = JS.split('ui.videoDone.addEventListener("click"')[1].split("\n});")[0]
        assert done.index("/tasks/${encodeURIComponent(state.taskId)}/video-scenes") < (
            done.index('call("DELETE", "/video-scenes")'))
        assert "!video.running && video.state && video.task_id === state.taskId" in done
