"""画面与旁白的对齐：场景在成片里占哪一段，以及那一段念的是哪几句。

Why these tests exist
---------------------
关键词个数决定画面张数，旁白句数由脚本决定，两者几乎从不相等——12 个关键词
配 28 句旁白是常态。原来的实现在两处都按下标配对：

* 场景列表把第 i 段画面配第 i 句旁白，于是后 16 句在界面上直接消失，
  "改台词后重新配音"还会把脚本从 28 句砍成 12 句；
* 渲染器把 12 段 5 秒画面拼成 60 秒，再把第 1 段接到 64 秒旁白尾巴上，
  于是列表里数到 12 段、成片里有 13 段,而且第一张图出现两次。

两者叠起来的表现就是"列表里看到的图和成片里的图对不上"。这些测试钉住的是
修好之后的两条性质：列表上写的时间段就是渲染器真正用的时间段，而每一句旁白
都恰好落在一段画面下。
"""

from unittest.mock import patch

import pytest

from app.services import scene_manifest, timeline


@pytest.fixture
def twelve_scenes(tmp_path, monkeypatch):
    """12 段素材的任务目录，对应 12 个关键词那种典型生成结果。"""
    directory = tmp_path / "t1"
    directory.mkdir()
    for index in range(12):
        (directory / f"{index}.mp4").write_bytes(b"x")

    def fake_task_dir(sub_dir: str = ""):
        target = tmp_path / sub_dir if sub_dir else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(scene_manifest.utils, "task_dir", fake_task_dir)
    return "t1"


def scenes_of(count: int, **extra) -> list[dict]:
    return [
        {"index": index, "material": f"{index}.mp4", **extra} for index in range(count)
    ]


class TestPlanWindows:
    """每段画面在成片里的起止时间。"""

    def test_scenes_are_spread_across_the_whole_narration(self, twelve_scenes):
        """12 段 5 秒画面配 64 秒旁白时每段 5.33 秒，而不是 5 秒加一段重复。"""
        with patch.object(timeline, "probe_duration", return_value=5.0):
            windows = timeline.plan_windows(
                twelve_scenes,
                scenes_of(12),
                audio_duration=64.0,
                clip_duration=5,
            )
        assert len(windows) == 12
        assert windows[0]["start"] == 0.0
        assert windows[-1]["end"] == pytest.approx(64.0, abs=0.01)
        for window in windows:
            assert window["end"] - window["start"] == pytest.approx(64 / 12, abs=0.01)

    def test_the_windows_never_leave_a_gap_or_overlap(self, twelve_scenes):
        with patch.object(timeline, "probe_duration", return_value=5.0):
            windows = timeline.plan_windows(
                twelve_scenes, scenes_of(12), audio_duration=64.0, clip_duration=5
            )
        for earlier, later in zip(windows, windows[1:]):
            assert later["start"] == pytest.approx(earlier["end"], abs=0.01)

    def test_a_longer_scene_keeps_a_longer_share(self, twelve_scenes):
        """按比例缩放,不平均分配：用户手动调过的长短关系必须留住。"""
        scenes = scenes_of(3)
        scenes[1]["duration"] = 10.0
        with patch.object(timeline, "probe_duration", return_value=5.0):
            windows = timeline.plan_windows(
                twelve_scenes, scenes, audio_duration=40.0, clip_duration=5
            )
        lengths = [window["end"] - window["start"] for window in windows]
        assert lengths[1] > lengths[0]

    def test_an_edited_timeline_is_not_rescaled(self, twelve_scenes):
        """拖过时间线之后按拖出来的长度算，不替用户铺满旁白。"""
        scenes = scenes_of(2, duration=3.0)
        with patch.object(timeline, "probe_duration", return_value=5.0):
            windows = timeline.plan_windows(
                twelve_scenes, scenes, audio_duration=64.0, clip_duration=5
            )
        assert windows[-1]["end"] == pytest.approx(6.0)

    def test_without_a_narration_length_the_natural_lengths_stand(self, twelve_scenes):
        with patch.object(timeline, "probe_duration", return_value=4.0):
            windows = timeline.plan_windows(
                twelve_scenes, scenes_of(3), audio_duration=0, clip_duration=5
            )
        assert windows[-1]["end"] == pytest.approx(12.0)

    def test_no_scenes_plan_to_no_windows(self, twelve_scenes):
        assert timeline.plan_windows(twelve_scenes, [], audio_duration=64.0) == []

    def test_planning_without_probing_starts_no_decoders(self, twelve_scenes):
        """审片列表每次页面重跑都要算一遍，不该为此多付十几次解码器启动。"""
        with patch.object(timeline, "probe_duration") as probe:
            windows = timeline.plan_windows(
                twelve_scenes,
                scenes_of(12),
                audio_duration=64.0,
                clip_duration=5,
                probe=False,
            )
        assert probe.call_count == 0
        assert windows[-1]["end"] == pytest.approx(64.0, abs=0.01)

    def test_an_unreadable_material_does_not_break_the_plan(self, twelve_scenes):
        """一段素材读不出来时按请求时长估算，而不是让整个界面打不开。"""
        with patch.object(
            timeline, "probe_duration", side_effect=timeline.TimelineError("gone")
        ):
            windows = timeline.plan_windows(
                twelve_scenes, scenes_of(2), audio_duration=10.0, clip_duration=5
            )
        assert windows[-1]["end"] == pytest.approx(10.0, abs=0.01)


class TestAutoFitEditList:
    """没人拖过时间线时，渲染器照场景列表铺满旁白。"""

    def test_one_clip_per_scene_with_nothing_repeated(self, twelve_scenes):
        with patch.object(timeline, "probe_duration", return_value=5.0):
            with patch.object(timeline, "ensure_image_length") as extend:
                extend.side_effect = lambda task_id, material, needed: material
                edit_list = timeline.auto_fit_edit_list(
                    twelve_scenes,
                    scenes_of(12),
                    audio_duration=64.0,
                    clip_duration=5,
                )
        assert len(edit_list) == 12
        assert len({item["path"] for item in edit_list}) == 12

    def test_the_total_covers_the_narration(self, twelve_scenes):
        """素材能拉长时总长必须等于旁白长，否则最后半句会被切掉。"""
        lengths = {}

        def fake_probe(path):
            return lengths.get(path, 5.0)

        def fake_extend(task_id, material, needed):
            # 真实实现会按新时长重渲出另一个文件，所以这里也要换一个路径：
            # 返回同一个路径时 build_edit_list 不会重新探测，测试就测不到
            # "拉长之后真的变长了"。
            rendered = f"{material}@{needed:.2f}"
            lengths[rendered] = needed
            return rendered

        # 图片素材在生成时就烘成了 mp4，所以"能不能拉长"取决于原图还在不在，
        # 而不是扩展名——这里把原图装回去，走的就是真实任务那条路。
        with patch.object(timeline, "probe_duration", side_effect=fake_probe):
            with patch.object(timeline, "source_image_for", return_value="a.png"):
                with patch.object(
                    timeline, "ensure_image_length", side_effect=fake_extend
                ):
                    edit_list = timeline.auto_fit_edit_list(
                        twelve_scenes,
                        scenes_of(12),
                        audio_duration=64.0,
                        clip_duration=5,
                    )
        assert timeline.timeline_total(edit_list) == pytest.approx(64.0, abs=0.05)

    def test_material_that_cannot_be_held_longer_is_not_looped(self, twelve_scenes):
        """视频素材拉不长时就是短，但也不会被循环补成 13 段。"""
        with patch.object(timeline, "probe_duration", return_value=5.0):
            with patch.object(timeline, "ensure_image_length") as extend:
                extend.side_effect = lambda task_id, material, needed: material
                edit_list = timeline.auto_fit_edit_list(
                    twelve_scenes, scenes_of(12), audio_duration=64.0, clip_duration=5
                )
        assert len(edit_list) == 12
        assert timeline.timeline_total(edit_list) == pytest.approx(60.0, abs=0.05)


class TestDistributeNarration:
    """旁白按时间分配到画面上，一句都不丢。"""

    windows = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.0}]

    def test_every_line_lands_under_exactly_one_scene(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "one"},
            {"start": 2.0, "end": 4.0, "text": "two"},
            {"start": 6.0, "end": 8.0, "text": "three"},
        ]
        texts = scene_manifest.distribute_narration(self.windows, segments=segments)
        # 用换行拼，这样这几句重新切一次还是原来那几句（见 split_narration）。
        assert texts == ["one\ntwo", "three"]

    def test_a_line_past_the_last_picture_is_kept_not_dropped(self):
        """旁白比画面长是常态,末尾那句归给最后一段,而不是消失。"""
        segments = [{"start": 20.0, "end": 22.0, "text": "tail"}]
        texts = scene_manifest.distribute_narration(self.windows, segments=segments)
        assert texts[-1] == "tail"

    def test_a_line_straddling_two_scenes_goes_where_most_of_it_is(self):
        segments = [{"start": 4.0, "end": 9.0, "text": "straddles"}]
        texts = scene_manifest.distribute_narration(self.windows, segments=segments)
        assert texts == ["", "straddles"]

    def test_without_subtitles_the_lines_are_split_evenly(self):
        """关掉字幕的任务没有时间戳，按句数均分仍然比按下标配对准。"""
        texts = scene_manifest.distribute_narration(
            self.windows, lines=["a", "b", "c", "d"]
        )
        assert texts == ["a\nb", "c\nd"]

    def test_subtitles_win_over_plain_lines(self):
        texts = scene_manifest.distribute_narration(
            self.windows,
            segments=[{"start": 6.0, "end": 7.0, "text": "timed"}],
            lines=["ignored"],
        )
        assert texts == ["", "timed"]

    def test_no_windows_means_no_blocks(self):
        assert scene_manifest.distribute_narration([], lines=["a"]) == []

    def test_blank_segments_are_skipped(self):
        texts = scene_manifest.distribute_narration(
            self.windows, segments=[{"start": 0.0, "end": 1.0, "text": "  "}]
        )
        assert texts == ["", ""]


class TestNarrationPairingIsStale:
    """什么时候该重排配对——以及什么时候绝对不能动。"""

    def test_fewer_scenes_than_lines_is_stale(self):
        assert (
            scene_manifest.narration_pairing_is_stale(
                [{"narration": "a"}, {"narration": "b"}], ["a", "b", "c"]
            )
            is True
        )

    def test_a_complete_pairing_is_left_alone(self):
        assert (
            scene_manifest.narration_pairing_is_stale(
                [{"narration": "a"}, {"narration": "b"}], ["a", "b"]
            )
            is False
        )

    def test_hand_edited_narration_is_never_overwritten(self):
        """用户手改过的台词比"少显示几句"重要得多,改过就不碰。"""
        assert (
            scene_manifest.narration_pairing_is_stale(
                [{"narration": "my own words"}], ["a", "b", "c"]
            )
            is False
        )

    def test_an_already_merged_pairing_is_not_redone(self):
        """补过一次之后每条台词都是若干句的合并，不能再被当成"没补过"。"""
        assert (
            scene_manifest.narration_pairing_is_stale(
                [{"narration": "a\nb"}, {"narration": "c"}], ["a", "b", "c"]
            )
            is False
        )

    def test_a_script_without_lines_is_not_stale(self):
        assert scene_manifest.narration_pairing_is_stale([{"narration": ""}], []) is False


class TestApplyNarrationPairing:
    def test_a_mismatched_block_count_is_refused(self, twelve_scenes, monkeypatch):
        monkeypatch.setattr(
            scene_manifest,
            "load",
            lambda task_id: {"scenes": scenes_of(2)},
        )
        with pytest.raises(scene_manifest.SceneError, match="for 2 scenes"):
            scene_manifest.apply_narration_pairing(twelve_scenes, ["only one"])
