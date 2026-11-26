"""A scene can be longer than the clip it shows: the last frame is held.

In the user's words: "when managing the timeline, the user must be able to
increase or decrease the scene length as much he wants". A picture was already
free (test_hold_a_picture_longer.py). Footage was capped at its own length, in
the browser and again on the server, because there are no more frames to show.
There are not, so the last one stays: what a video editor does when a clip is
dragged past its end.

These run the real ffmpeg, on a clip a second long.
"""

import os
import subprocess

import pytest

from app.services import timeline
from app.utils import utils


@pytest.fixture
def clip(tmp_path):
    """A one-second clip whose picture changes all the way through."""
    path = tmp_path / "footage.mp4"
    made = subprocess.run(
        [utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=160x90:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True,
    )
    if made.returncode != 0 or not path.is_file():
        pytest.skip("ffmpeg could not make the test clip")
    return str(path)


def frame_at(path: str, seconds: float) -> bytes:
    shot = subprocess.run(
        [utils.get_ffmpeg_binary(), "-v", "error", "-ss", f"{seconds:.3f}", "-i", path,
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    return shot.stdout


def test_the_held_copy_is_long_enough_and_ends_on_the_last_frame(clip):
    held = timeline.ensure_video_length("t", clip, 3.0)

    assert held != clip
    assert timeline.probe_duration(held) >= 3.0
    # Past the end of the original the picture no longer changes...
    late, later = frame_at(held, 1.6), frame_at(held, 2.8)
    assert late and late == later
    # ...and before it, it still does: the clip plays, then stops.
    assert frame_at(held, 0.1) != frame_at(held, 0.8)


def test_the_original_is_left_as_it_was_and_the_copy_is_out_of_the_way(clip):
    before = os.path.getsize(clip)
    held = timeline.ensure_video_length("t", clip, 3.0)

    assert os.path.getsize(clip) == before
    # In a folder of its own, so the video's list of earlier media, which reads
    # the files beside the manifest, never offers a held copy as a clip.
    assert os.path.dirname(held) == os.path.join(os.path.dirname(clip), timeline.HELD_DIR)


def test_asking_again_for_the_same_length_encodes_nothing(clip):
    first = timeline.ensure_video_length("t", clip, 3.0)
    stamp = os.stat(first).st_mtime_ns

    assert timeline.ensure_video_length("t", clip, 3.0) == first
    assert os.stat(first).st_mtime_ns == stamp
    # Another length is another file.
    assert timeline.ensure_video_length("t", clip, 5.0) != first


def test_a_clip_that_is_already_long_enough_is_used_as_it_is(clip):
    assert timeline.ensure_video_length("t", clip, 0.5) == clip
    assert not os.path.isdir(os.path.join(os.path.dirname(clip), timeline.HELD_DIR))


def test_a_file_ffmpeg_cannot_read_is_handed_back_not_raised(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    from unittest.mock import patch

    with patch.object(timeline, "probe_duration", return_value=1.0):
        assert timeline.ensure_video_length("t", str(broken), 3.0) == str(broken)
    assert not any(name.endswith(".part.mp4")
                   for _, _, names in os.walk(tmp_path) for name in names)


@pytest.mark.parametrize("source, start, needed, held", [
    (5.0, 0.0, 9.0, 4.0),      # four seconds past the end
    (5.0, 2.0, 9.0, 6.0),      # an in-point leaves less footage to show
    (5.0, 0.0, 5.0, 0.0),      # exactly the clip
    (5.0, 0.0, 3.0, 0.0),      # shorter than the clip: nothing held
    (5.0, 0.0, None, 0.0),     # no length of its own
])
def test_how_much_of_a_scene_is_the_held_frame(source, start, needed, held):
    assert timeline.held_seconds(source, start, needed) == pytest.approx(held)


def test_the_render_list_shows_the_scene_at_the_length_it_was_set(clip, tmp_path, monkeypatch):
    """The whole way: a three-second scene on a one-second clip is three
    seconds of the render, cut from the held copy."""
    import shutil

    from app.services import scene_manifest

    folder = tmp_path / "t1"
    folder.mkdir()
    shutil.copy(clip, folder / "footage.mp4")

    def task_dir(sub_dir: str = ""):
        target = tmp_path / sub_dir if sub_dir else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(scene_manifest.utils, "task_dir", task_dir)

    edit_list = timeline.build_edit_list(
        "t1", [{"index": 0, "material": "footage.mp4", "duration": 3.0}])

    assert edit_list[0]["start"] == 0.0 and edit_list[0]["end"] == 3.0
    assert os.path.dirname(edit_list[0]["path"]).endswith(timeline.HELD_DIR)
    assert timeline.timeline_total(edit_list) == pytest.approx(3.0)

    # A scene no longer than its clip is cut from the clip itself, untouched.
    short = timeline.build_edit_list(
        "t1", [{"index": 0, "material": "footage.mp4", "duration": 0.5}])
    assert os.path.basename(short[0]["path"]) == "footage.mp4"
    assert short[0]["end"] == 0.5


def test_the_stored_render_list_can_find_the_held_copy_again(clip, tmp_path, monkeypatch):
    """The render list is stored by name and resolved when the render starts.
    A held copy lives in ``held/``: stored by its bare name it was "missing or
    outside the task directory", and a video with one could not be rendered."""
    import shutil

    from app.services import scene_editor, scene_manifest

    folder = tmp_path / "t1"
    folder.mkdir()
    shutil.copy(clip, folder / "footage.mp4")

    def task_dir(sub_dir: str = ""):
        target = tmp_path / sub_dir if sub_dir else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(scene_manifest.utils, "task_dir", task_dir)
    held, plain = (
        timeline.build_edit_list(
            "t1", [{"index": 0, "material": "footage.mp4", "duration": seconds}])[0]["path"]
        for seconds in (3.0, 0.5))

    stored = scene_editor._edit_list_material("t1", held)
    assert stored == f"{timeline.HELD_DIR}/{os.path.basename(held)}"
    assert os.path.samefile(scene_manifest.resolve_material("t1", stored), held)
    assert scene_editor._edit_list_material("t1", plain) == "footage.mp4"
    # A file from outside the video's folder keeps going by its name alone.
    assert scene_editor._edit_list_material(
        "t1", str(tmp_path / "uploads" / "mine.mp4")) == "mine.mp4"
