"""每张图停多久，由它那句旁白念了多久决定。

Why these tests exist
---------------------
``plan_windows`` 原来把画面按比例平分到整条旁白上：29 张图配 95 秒,每张
3.3 秒。可旁白不是平均的——同一条片子里 "in Texas" 只念了 0.7 秒,
"In late two thousand twenty five, Texas sued five of the biggest TV makers"
念了 5 秒多。平分之后第一张之后就开始错位,而且越往后偏得越多,用户看到的
就是"图和声音不同步"。

每句话念了多久，字幕里本来就有（配音是逐句合成的，一条字幕恰好一句）。
这些测试钉住的是：有逐句时间时就按它铺，没有时才退回平分；而且边界要落在
"下一句开始"，中间不留缝。

The even spread drifted further with every line because lines are not equally
long. These tests pin that per-line cues are used when available, that the
windows stay gapless, and that every fallback path behaves as before.
"""

from unittest.mock import patch

import pytest

from app.services import scene_manifest, timeline


# 用户那条真片子的形状：13 句话被逗号切成 29 段，长短差了七倍。
UNEVEN_CUES = [
    {"start": 0.0, "end": 3.2},    # Your TV might be taking two pictures...
    {"start": 3.4, "end": 4.1},    # It is called A C R
    {"start": 4.3, "end": 6.0},    # automatic content recognition
    {"start": 6.2, "end": 11.4},   # In late two thousand twenty five, Texas sued...
    {"start": 11.6, "end": 12.3},  # in Texas
]


@pytest.fixture
def task(tmp_path, monkeypatch):
    directory = tmp_path / "t1"
    directory.mkdir()
    for index in range(40):
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


def _plan(task_id, scenes, cues, **kwargs):
    with (
        patch.object(scene_manifest, "narration_segments", return_value=cues),
        patch.object(timeline, "probe_duration", return_value=5.0),
    ):
        return timeline.plan_windows(task_id, scenes, **kwargs)


class TestAPictureHoldsForItsOwnLine:
    def test_each_window_is_its_own_line_not_the_average(self, task):
        """短句短停、长句长停——平分出来的 2.46 秒一刀切才是错位的来源。"""
        windows = _plan(task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3)

        assert len(windows) == 5
        # 第四句念了 5.2 秒，第五句只念了 0.7 秒。
        assert windows[3]["end"] - windows[3]["start"] == pytest.approx(5.4, abs=0.05)
        assert windows[4]["end"] - windows[4]["start"] == pytest.approx(0.7, abs=0.05)
        # 平分的话每段都会是这个数，谁都对不上。
        even = 12.3 / 5
        assert abs((windows[3]["end"] - windows[3]["start"]) - even) > 1.0

    def test_a_picture_changes_when_the_next_line_starts(self, task):
        """切换点要落在下一句开口的时刻，这才是"同步"的定义。"""
        windows = _plan(task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3)

        for index in range(1, len(UNEVEN_CUES)):
            assert windows[index]["start"] == pytest.approx(
                UNEVEN_CUES[index]["start"], abs=0.001
            ), "画面换的那一刻，就是下一句开口的那一刻"

    def test_the_track_has_no_holes_in_it(self, task):
        """句与句之间的停顿归给前一张图，否则成片里是一帧黑。"""
        windows = _plan(task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3)

        assert windows[0]["start"] == 0.0, "开头的留白也得有画面盖着"
        for earlier, later in zip(windows, windows[1:]):
            assert earlier["end"] == later["start"], "两段画面之间不能有缝"
        assert windows[-1]["end"] >= 12.3, "最后一张要盖到音频结束"

    def test_the_whole_narration_is_covered_exactly_once(self, task):
        windows = _plan(task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3)
        covered = sum(item["end"] - item["start"] for item in windows)
        assert covered == pytest.approx(windows[-1]["end"], abs=0.001)


class TestTheLineWinsOverTheClipCeiling:
    def test_a_long_line_keeps_its_full_time(self, task):
        """按上限截断就是把错位重新造出来，所以这里句子说了算。"""
        windows = _plan(
            task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3, clip_duration=2
        )
        assert windows[3]["end"] - windows[3]["start"] == pytest.approx(5.4, abs=0.05)

    def test_and_it_says_so_in_the_log(self, task):
        from loguru import logger

        said: list[str] = []
        sink = logger.add(said.append, level="INFO")
        try:
            _plan(
                task, scenes_of(5), UNEVEN_CUES, audio_duration=12.3, clip_duration=2
            )
        finally:
            logger.remove(sink)
        text = " ".join(said)
        assert "maximum clip duration" in text
        assert "drift" in text, "要说清为什么不按上限来"


class TestWhenTheCuesCannotBeTrusted:
    """退回原来那套的每条路，行为都要和改动前一致。"""

    def test_no_subtitles_falls_back_to_the_even_spread(self, task):
        windows = _plan(task, scenes_of(5), [], audio_duration=12.3)
        spans = [item["end"] - item["start"] for item in windows]
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_a_different_number_of_lines_than_pictures_is_not_paired(self, task):
        """条数不等说明画面和句子本来就不是一对一，按位置对齐只会对错。"""
        windows = _plan(task, scenes_of(3), UNEVEN_CUES, audio_duration=12.3)
        spans = [item["end"] - item["start"] for item in windows]
        assert len(windows) == 3
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_a_dragged_timeline_is_left_alone(self, task):
        """用户看着波形对齐过了，不该被我们按字幕重算一遍。"""
        dragged = scenes_of(5, duration=4.0)
        windows = _plan(task, dragged, UNEVEN_CUES, audio_duration=12.3)
        for window in windows:
            assert window["end"] - window["start"] == pytest.approx(4.0, abs=0.01)

    def test_overlapping_cues_fall_back_instead_of_making_a_zero_length_clip(
        self, task
    ):
        """时间戳不单调时宁可对不齐，也不能交出一段零长画面让渲染器崩。"""
        broken = [
            {"start": 0.0, "end": 3.0},
            {"start": 5.0, "end": 6.0},
            {"start": 1.0, "end": 2.0},
        ]
        windows = _plan(task, scenes_of(3), broken, audio_duration=10.0)
        assert len(windows) == 3
        for window in windows:
            assert window["end"] > window["start"]
        spans = [item["end"] - item["start"] for item in windows]
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_malformed_cue_values_fall_back(self, task):
        broken = [{"start": 0.0, "end": 3.0}, {"start": "soon", "end": 6.0}]
        windows = _plan(task, scenes_of(2), broken, audio_duration=10.0)
        assert len(windows) == 2
        for window in windows:
            assert window["end"] > window["start"]


class TestTheRenderUsesTheSameWindows:
    """界面上写的时间段必须就是渲染器用的时间段，否则审片没有意义。"""

    def test_the_edit_list_follows_the_spoken_lines(self, task):
        with (
            patch.object(scene_manifest, "narration_segments", return_value=UNEVEN_CUES),
            patch.object(timeline, "probe_duration", return_value=60.0),
        ):
            edit_list = timeline.auto_fit_edit_list(
                task, scenes_of(5), audio_duration=12.3
            )
        spans = [float(item["end"]) - float(item["start"]) for item in edit_list]
        assert len(edit_list) == 5, "列表里五段，成片里就该是五段"
        assert spans[3] == pytest.approx(5.4, abs=0.05)
        assert spans[4] == pytest.approx(0.7, abs=0.05)
