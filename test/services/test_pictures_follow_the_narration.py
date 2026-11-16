"""每张画面停多久，跟着它那句旁白走。

提示词是一句旁白写一条的，可每一段默认都停同样长，于是时间线上所有画面
一样宽、越往后越和旁白对不上。钉住的事：

* 每一段从它那句话开始，到下一段的话开始为止；加起来正好是整条旁白；
* 新片子第一次打开时自动对一次，不算一步编辑；
* 拖过的、出过片的、对过一次的，不再自动动它；
* 按钮点的那一次可以撤销；
* 对不上就点名是哪一段，不猜。

Each picture is held for as long as its own narration line is spoken. A new
video is timed this way once, on first open, without becoming an undo step;
videos that were trimmed, rendered or already timed are left alone; the button
is undoable; and a scene whose words are not in the recording is named rather
than guessed at.
"""

from pathlib import Path

import pytest

from app.services import scene_editor, scene_manifest, task_artifacts

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"

SRT = """1
00:00:00,000 --> 00:00:01,500
One

2
00:00:01,500 --> 00:00:06,000
Two

3
00:00:06,000 --> 00:00:07,000
Three
"""


def _spoken_task(task_root, lines=("One", "Two", "Three"), audio=8.0):
    task_id, _ = _reviewed_task(task_root, scenes=len(lines))
    (task_root / task_id / "subtitle.srt").write_text(SRT, encoding="utf-8")
    task_artifacts.patch_script_data(
        task_id, subtitle_path="subtitle.srt", audio_duration=audio
    )
    scenes = scene_manifest.load(task_id)["scenes"]
    for scene, line in zip(scenes, lines):
        scene["narration"] = line
    from app.services import edit_history

    with edit_history.paused():
        scene_manifest.save_scenes(task_id, scenes)
    return task_id


def _durations(task_id):
    return [s.get("duration") for s in scene_manifest.load(task_id)["scenes"]]


class TestHowLongEachPictureStays:
    def test_each_scene_lasts_as_long_as_its_line(self, task_root):
        task_id = _spoken_task(task_root)
        # 最后一段停到配音结束（8 秒），不是最后一条字幕结束（7 秒）。
        assert scene_editor.narration_paced_durations(task_id) == [1.5, 4.5, 2.0]

    def test_together_they_cover_the_whole_narration(self, task_root):
        task_id = _spoken_task(task_root)
        assert sum(scene_editor.narration_paced_durations(task_id)) == 8.0

    def test_a_scene_can_carry_several_lines(self, task_root):
        task_id = _spoken_task(task_root, lines=("One\nTwo", "Three"))
        assert scene_editor.narration_paced_durations(task_id) == [6.0, 2.0]

    def test_a_line_nobody_has_stays_on_the_picture_before_it(self, task_root):
        """删掉了"Two"那一段：念 Two 的时候，One 的图接着停。"""
        task_id = _spoken_task(task_root, lines=("One", "Three"))
        assert scene_editor.narration_paced_durations(task_id) == [6.0, 2.0]

    def test_punctuation_and_case_do_not_matter(self, task_root):
        task_id = _spoken_task(task_root, lines=("one.", "TWO!", " three "))
        assert scene_editor.narration_paced_durations(task_id) == [1.5, 4.5, 2.0]

    def test_words_that_are_not_in_the_recording_are_named(self, task_root):
        task_id = _spoken_task(task_root, lines=("One", "Something else", "Three"))
        with pytest.raises(scene_editor.SceneError, match="scene 2"):
            scene_editor.narration_paced_durations(task_id)

    def test_scenes_out_of_order_are_not_guessed_at(self, task_root):
        task_id = _spoken_task(task_root, lines=("Two", "One", "Three"))
        with pytest.raises(scene_editor.SceneError, match="scene 2"):
            scene_editor.narration_paced_durations(task_id)

    def test_changed_words_that_were_not_recorded_have_no_timing(self, task_root):
        task_id = _spoken_task(task_root)
        task_artifacts.patch_script_data(task_id, audio_stale=True)
        with pytest.raises(scene_editor.SceneError, match="re-record"):
            scene_editor.narration_paced_durations(task_id)


class TestANewVideoStartsThisWay:
    def test_opening_it_times_the_pictures(self, task_root):
        task_id = _spoken_task(task_root)
        assert _durations(task_id) == [None, None, None]
        scene_editor.describe(task_id)
        assert _durations(task_id) == [1.5, 4.5, 2.0]

    def test_that_is_not_a_step_to_undo(self, task_root):
        task_id = _spoken_task(task_root)
        scene_editor.describe(task_id)
        assert scene_editor.history_state(task_id)["undo"] == 0

    def test_it_happens_once(self, task_root):
        """对过之后用户把一段的时长撤掉了：下次打开不该又给他对回去。"""
        task_id = _spoken_task(task_root)
        scene_editor.describe(task_id)
        for index in range(3):
            scene_manifest.clear_scene_timing(task_id, index)
        scene_editor.describe(task_id)
        assert _durations(task_id) == [None, None, None]

    def test_a_video_somebody_trimmed_is_left_alone(self, task_root):
        task_id = _spoken_task(task_root)
        scene_manifest.patch_scene_timing(task_id, 1, duration=2.5)
        scene_editor.describe(task_id)
        assert _durations(task_id) == [None, 2.5, None]

    def test_a_video_already_rendered_is_left_alone(self, task_root):
        task_id = _spoken_task(task_root)
        (task_root / task_id / "final-1.mp4").write_bytes(b"x")
        scene_editor.describe(task_id)
        assert _durations(task_id) == [None, None, None]

    def test_words_that_do_not_match_do_not_stop_it_opening(self, task_root):
        # 两张图、三条字幕，而且台词也认不出来：哪一种账都对不上。
        task_id = _spoken_task(task_root, lines=("Nope", "Neither"))
        assert len(scene_editor.describe(task_id)["scenes"]) == 2
        assert _durations(task_id) == [None, None]

    def test_it_is_what_the_render_would_have_done_anyway(self, task_root):
        """自动那一次写下的就是渲染器本来的安排：只是让时间线如实画出来，
        不改成片。"""
        from app.services import timeline

        task_id = _spoken_task(task_root)
        manifest = scene_manifest.load(task_id)
        planned = timeline.plan_windows(
            task_id, manifest["scenes"], audio_duration=8.0, probe=False
        )
        scene_editor.describe(task_id)
        assert _durations(task_id) == [
            round(w["end"] - w["start"], 3) for w in planned
        ]


class TestTheButton:
    def test_it_can_be_undone(self, task_root):
        task_id = _spoken_task(task_root)
        scene_editor.pace_to_narration(task_id)
        assert _durations(task_id) == [1.5, 4.5, 2.0]
        state = scene_editor.history_state(task_id)
        assert state["undo_label"] == "Match narration"
        scene_editor.undo(task_id)
        assert _durations(task_id) == [None, None, None]

    def test_it_works_again_after_scenes_were_trimmed(self, task_root):
        task_id = _spoken_task(task_root)
        scene_manifest.patch_scene_timing(task_id, 0, duration=7.0)
        scene_editor.pace_to_narration(task_id)
        assert _durations(task_id) == [1.5, 4.5, 2.0]

    def test_over_http(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi

        task_id = _spoken_task(task_root)
        response = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/timeline/pace"
        )
        assert response.status_code == 200, response.text
        assert _durations(task_id) == [1.5, 4.5, 2.0]

    def test_it_is_on_the_page(self):
        html = (EDITOR / "index.html").read_text(encoding="utf-8")
        js = (EDITOR / "editor.js").read_text(encoding="utf-8")
        assert 'id="pace"' in html and 'el("pace")' in js
        assert "/timeline/pace`" in js

    def test_stretching_to_fit_is_still_there(self):
        """加，不替换：按比例铺满旁白的那个按钮和接口原样留着。"""
        html = (EDITOR / "index.html").read_text(encoding="utf-8")
        assert 'id="fit"' in html
        assert callable(scene_editor.fit_to_narration)
