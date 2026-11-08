"""时间线：时长换算、剪辑表生成，以及"没编辑过就不走剪辑表"这条开关。"""

from unittest.mock import patch

import pytest

from app.services import scene_manifest, timeline


class TestNormaliseTiming:
    def test_an_unedited_scene_uses_the_whole_material(self):
        timing = timeline.normalise_timing(source_duration=5.0)
        assert timing["trim_start"] == 0.0
        assert timing["trim_end"] == 5.0
        assert timing["duration"] == 5.0
        assert timing["clamped"] is False

    def test_an_in_point_shortens_what_is_left(self):
        timing = timeline.normalise_timing(source_duration=5.0, trim_start=2.0)
        assert timing["trim_start"] == 2.0
        assert timing["duration"] == 3.0

    def test_a_duration_past_the_material_is_clamped_not_rejected(self):
        """拖过素材末尾是常事，为此报错一次毫无意义。"""
        timing = timeline.normalise_timing(source_duration=5.0, duration=99.0)
        assert timing["duration"] == 5.0
        assert timing["clamped"] is True

    def test_an_in_point_past_the_material_is_pulled_back(self):
        timing = timeline.normalise_timing(source_duration=5.0, trim_start=9.0)
        assert timing["trim_start"] == pytest.approx(4.8)
        assert timing["clamped"] is True

    def test_slow_motion_needs_less_source_for_the_same_screen_time(self):
        """0.5 倍速播 1 秒源画面，在成片里停 2 秒。换算必须在服务端做一次。"""
        timing = timeline.normalise_timing(
            source_duration=6.0, duration=2.0, clip_speed=0.5
        )
        assert timing["trim_end"] == 1.0
        assert timing["duration"] == 2.0

    def test_fast_motion_needs_more_source_for_the_same_screen_time(self):
        timing = timeline.normalise_timing(
            source_duration=6.0, duration=2.0, clip_speed=2.0
        )
        assert timing["trim_end"] == 4.0
        assert timing["duration"] == 2.0

    def test_a_duration_below_the_floor_is_refused(self):
        with pytest.raises(timeline.TimelineError, match="at least"):
            timeline.normalise_timing(source_duration=5.0, duration=0.01)

    def test_a_negative_in_point_is_refused(self):
        with pytest.raises(timeline.TimelineError, match="cannot be negative"):
            timeline.normalise_timing(source_duration=5.0, trim_start=-1)

    def test_a_non_numeric_duration_is_refused_by_name(self):
        with pytest.raises(timeline.TimelineError, match="duration must be a number"):
            timeline.normalise_timing(source_duration=5.0, duration="soon")

    def test_a_zero_length_material_is_refused(self):
        with pytest.raises(timeline.TimelineError, match="no usable duration"):
            timeline.normalise_timing(source_duration=0.0)

    def test_an_in_point_leaving_nothing_usable_is_refused(self):
        with pytest.raises(timeline.TimelineError, match="is left after the in-point"):
            timeline.normalise_timing(source_duration=5.0, trim_start=4.95, duration=1.0)


class TestTimingSwitch:
    """``has_timing_edits`` 决定走不走剪辑表，所以它的边界要钉住。"""

    def test_an_untouched_task_has_no_timing_edits(self):
        scenes = [{"index": 0, "material": "1.mp4"}, {"index": 1, "material": "2.mp4"}]
        assert scene_manifest.has_timing_edits(scenes) is False

    def test_one_trimmed_scene_turns_the_whole_task_into_a_timeline(self):
        scenes = [{"index": 0, "material": "1.mp4", "duration": 2.0}]
        assert scene_manifest.has_timing_edits(scenes) is True

    def test_an_in_point_alone_counts_as_an_edit(self):
        scenes = [{"index": 0, "material": "1.mp4", "trim_start": 1.5}]
        assert scene_manifest.has_timing_edits(scenes) is True

    def test_an_explicit_zero_in_point_is_not_an_edit(self):
        """清掉设置后留下的 0 不该把任务永久锁在剪辑表模式。"""
        scenes = [{"index": 0, "material": "1.mp4", "trim_start": 0}]
        assert scene_manifest.has_timing_edits(scenes) is False

    def test_unreadable_timing_values_are_treated_as_unedited(self):
        scenes = [{"index": 0, "material": "1.mp4", "duration": "soon"}]
        assert scene_manifest.has_timing_edits(scenes) is False

    def test_scene_timing_defaults_for_a_manifest_written_before_this_feature(self):
        timing = scene_manifest.scene_timing({"index": 0, "material": "1.mp4"})
        assert timing == {"trim_start": 0.0, "duration": None}


class TestBuildEditList:
    @pytest.fixture
    def two_scenes(self, tmp_path, monkeypatch):
        directory = tmp_path / "t1"
        directory.mkdir()
        for name in ("1.mp4", "2.mp4"):
            (directory / name).write_bytes(b"x")

        def fake_task_dir(sub_dir: str = ""):
            target = tmp_path / sub_dir if sub_dir else tmp_path
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        monkeypatch.setattr(scene_manifest.utils, "task_dir", fake_task_dir)
        return "t1"

    def test_every_scene_enters_the_edit_list_even_when_only_one_was_trimmed(
        self, two_scenes
    ):
        """只拖了第二段时，第一段也要进剪辑表。

        否则没拖过的片段会回到自动切片逻辑、长度全变一遍，用户只动了一段
        却看到整条片子都不一样了。
        """
        scenes = [
            {"index": 0, "material": "1.mp4"},
            {"index": 1, "material": "2.mp4", "duration": 2.0},
        ]
        with patch.object(timeline, "probe_duration", return_value=5.0):
            edit_list = timeline.build_edit_list(two_scenes, scenes)
        assert len(edit_list) == 2
        assert edit_list[0]["end"] == 5.0
        assert edit_list[1]["end"] == 2.0

    def test_the_edit_list_follows_scene_order(self, two_scenes):
        scenes = [
            {"index": 0, "material": "2.mp4", "duration": 1.0},
            {"index": 1, "material": "1.mp4", "duration": 1.0},
        ]
        with patch.object(timeline, "probe_duration", return_value=5.0):
            edit_list = timeline.build_edit_list(two_scenes, scenes)
        assert [item["path"].split("\\")[-1].split("/")[-1] for item in edit_list] == [
            "2.mp4",
            "1.mp4",
        ]

    def test_an_empty_timeline_is_refused(self, two_scenes):
        with pytest.raises(timeline.TimelineError, match="no scenes to render"):
            timeline.build_edit_list(two_scenes, [])

    def test_a_missing_material_is_named(self, two_scenes):
        scenes = [{"index": 0, "material": "gone.mp4", "duration": 1.0}]
        with pytest.raises(scene_manifest.SceneError):
            timeline.build_edit_list(two_scenes, scenes)

    def test_an_image_held_longer_is_re_rendered_rather_than_stretched(self, two_scenes):
        """图片片段是按固定时长烘好的，拉长必须重渲，不能拉伸那段 mp4。"""
        scenes = [{"index": 0, "material": "1.mp4", "duration": 9.0}]
        with patch.object(scene_manifest, "material_kind", return_value="image"):
            with patch.object(timeline, "ensure_image_length") as extend:
                extend.return_value = str(
                    scene_manifest.utils.task_dir("t1") + "/1.mp4"
                )
                with patch.object(timeline, "probe_duration", return_value=5.0):
                    timeline.build_edit_list(two_scenes, scenes)
        assert extend.call_count == 1

    def test_a_video_held_longer_is_clamped_instead_of_re_rendered(self, two_scenes):
        scenes = [{"index": 0, "material": "1.mp4", "duration": 9.0}]
        with patch.object(scene_manifest, "material_kind", return_value="video"):
            with patch.object(timeline, "ensure_image_length") as extend:
                with patch.object(timeline, "probe_duration", return_value=5.0):
                    edit_list = timeline.build_edit_list(two_scenes, scenes)
        assert extend.call_count == 0
        assert edit_list[0]["end"] == 5.0


class TestTimelineTotal:
    def test_the_total_folds_in_the_clip_speed(self):
        edit_list = [{"start": 0, "end": 4}, {"start": 0, "end": 2}]
        assert timeline.timeline_total(edit_list) == 6
        assert timeline.timeline_total(edit_list, clip_speed=2.0) == 3


class TestNarrationSegments:
    """旁白按句切分：配音是逐句合成的，字幕里的每条正好对应一句。"""

    @pytest.fixture
    def task(self, tmp_path, monkeypatch):
        def fake_task_dir(sub_dir: str = ""):
            target = tmp_path / sub_dir if sub_dir else tmp_path
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        monkeypatch.setattr(scene_manifest.utils, "task_dir", fake_task_dir)
        return "narr"

    def _write(self, task, body: str) -> None:
        from app.services import task_artifacts

        directory = scene_manifest.utils.task_dir(task)
        with open(f"{directory}/subtitle.srt", "w", encoding="utf-8") as handle:
            handle.write(body)
        task_artifacts.write_script_data(
            task, {"script": "x", "subtitle_path": "subtitle.srt"}
        )

    def test_each_cue_becomes_a_segment(self, task):
        self._write(task, (
            "1\n00:00:00,000 --> 00:00:01,930\nBread is older than farming\n\n"
            "2\n00:00:01,930 --> 00:00:06,076\nAnd that changes everything\n\n"
        ))
        segments = scene_manifest.narration_segments(task)
        assert len(segments) == 2
        assert segments[0] == {
            "start": 0.0, "end": 1.93, "text": "Bread is older than farming",
        }
        assert segments[1]["start"] == 1.93

    def test_hours_and_minutes_are_converted(self, task):
        self._write(task, "1\n01:02:03,500 --> 01:02:04,000\nlate line\n\n")
        segments = scene_manifest.narration_segments(task)
        assert segments[0]["start"] == pytest.approx(3723.5)

    def test_a_cue_with_no_duration_is_dropped(self, task):
        """零长度的字幕块在时间线上画不出来，留着只会变成一条看不见的缝。"""
        self._write(task, (
            "1\n00:00:02,000 --> 00:00:02,000\nempty\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nreal\n\n"
        ))
        segments = scene_manifest.narration_segments(task)
        assert [s["text"] for s in segments] == ["real"]

    def test_a_task_without_subtitles_returns_nothing(self, task):
        from app.services import task_artifacts

        task_artifacts.write_script_data(task, {"script": "x", "subtitle_path": ""})
        assert scene_manifest.narration_segments(task) == []

    def test_a_missing_subtitle_file_returns_nothing(self, task):
        from app.services import task_artifacts

        task_artifacts.write_script_data(
            task, {"script": "x", "subtitle_path": "gone.srt"}
        )
        assert scene_manifest.narration_segments(task) == []

    def test_a_corrupt_subtitle_file_does_not_raise(self, task):
        """字幕坏了不该让整个时间线打不开。"""
        self._write(task, "not an srt at all\n")
        assert scene_manifest.narration_segments(task) == []
