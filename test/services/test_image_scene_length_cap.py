"""一张图最多停多久。

Why these tests exist
---------------------
画面是按它配的那句话有多长就停多长的，这正是同步的来源。可实测用户那条 499
秒的片子里，最长的一句要念 18.6 秒——一张静图就在屏幕上干停 18.6 秒，而默认的
``video_clip_duration`` 是 3 秒，根本不是这么回事。

所以给"拉长"加一个上限：超过上限的句子拆回它的分句，每段各配一张图。同步不
变（每张图还是对着自己那几条字幕），但没有那么长的定格了。实测 8 秒上限把
96 句变成 112 句，最长定格从 18.6 秒降到 13 秒。

这里钉住三件事：

1. **条数总和不许变。** 时间线是拿"每张图占几条字幕"的和去对字幕总数的，
   和一旦不等就整条退回平均铺——不报错，只是悄悄不同步了。
2. **不设上限时，结果和改之前一个字不差。** 这是回归闸门。
3. **出图的分组和算时间的分组必须是同一套。** 以前时间线是从脚本重算一遍
   的，带了参数之后那就是另一套分组，于是同步白做。
"""

import json
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import scene_manifest, timeline
from app.utils import utils


LONG = (
    "One two three four five, six seven eight nine ten eleven twelve. "
    "Short line."
)


class TestEstimatingHowLongALineTakes:
    def test_the_rate_is_the_measured_one(self):
        """2.31 词/秒是从用户那条 499 秒、1151 词的片子里量出来的。"""
        assert utils.DEFAULT_WORDS_PER_SECOND == pytest.approx(2.31)
        assert utils.estimate_spoken_seconds("a b c d e f") == pytest.approx(
            6 / 2.31, abs=0.01
        )

    def test_chinese_is_counted_by_character(self):
        """中日韩没有空格，按词数算会把一整句算成一个词。"""
        assert utils.estimate_spoken_seconds("你好世界") > 0.5

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_nothing_takes_no_time(self, text):
        assert utils.estimate_spoken_seconds(text) == 0.0


class TestTheCapSplitsLongLines:
    def test_a_long_sentence_becomes_more_than_one_picture(self):
        uncapped = utils.split_narration_into_sentences(LONG)
        capped = utils.split_narration_into_sentences(LONG, max_seconds=3.0)
        assert len(uncapped) == 2
        assert len(capped) > len(uncapped)

    def test_the_clause_counts_still_add_up(self):
        """和不等于字幕条数时，时间线会整条退回平均铺——同步就白做了。"""
        clauses = len(utils.split_string_by_punctuations(LONG))
        for cap in (None, 0, 1.0, 3.0, 8.0, 999.0):
            groups = utils.split_narration_into_sentences(LONG, max_seconds=cap)
            assert sum(count for _, count in groups) == clauses, cap

    def test_no_cap_is_byte_identical_to_before(self):
        """回归闸门：不设上限时一个字都不许变。"""
        for script in [
            LONG,
            "A, b. C. D, e, f.",
            "U.S. spying is real, and it spreads.",
            "No final punctuation",
            "",
        ]:
            plain = utils.split_narration_into_sentences(script)
            for off in (None, 0, 0.0, ""):
                assert utils.split_narration_into_sentences(
                    script, max_seconds=off
                ) == plain, (script, off)

    def test_a_clause_too_long_to_split_is_left_alone(self):
        """一个分句自己就超上限时再拆不下去——指望不上把一句话从中间切开。

        实测那条片子里还剩 4 句超过 8 秒，最长 13 秒，都是中间没有任何标点
        的长分句。这是上限的已知边界，不是没算到。
        """
        one = "word " * 40
        groups = utils.split_narration_into_sentences(one.strip(), max_seconds=2.0)
        assert len(groups) == 1
        assert utils.estimate_spoken_seconds(groups[0][0]) > 2.0

    def test_a_bad_cap_is_ignored_rather_than_crashing(self):
        plain = utils.split_narration_into_sentences(LONG)
        for junk in ("abc", object(), float("nan")):
            assert utils.split_narration_into_sentences(LONG, max_seconds=junk) == plain

    def test_the_split_pieces_still_read_as_text(self):
        """送给模型的还是话，不是用空格拼起来的碎片。"""
        groups = utils.split_narration_into_sentences(LONG, max_seconds=3.0)
        for text, _ in groups:
            assert text == text.strip()
            assert "  " not in text


class TestTheParamCarriesTheCap:
    def test_the_default_is_eight_seconds(self):
        assert VideoParams(video_subject="x").max_image_scene_duration == 8.0

    def test_it_can_be_turned_off(self):
        params = VideoParams(video_subject="x", max_image_scene_duration=0)
        assert not params.max_image_scene_duration


class TestTheTimingUsesTheSameGrouping:
    """出图按 112 组，算时间也必须按那 112 组,不能自己从脚本重算一套。"""

    @pytest.fixture
    def task(self, tmp_path, monkeypatch):
        def fake_task_dir(sub_dir: str = ""):
            target = tmp_path / sub_dir if sub_dir else tmp_path
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        monkeypatch.setattr(scene_manifest.utils, "task_dir", fake_task_dir)
        return "t1"

    def _scenes_and_cues(self, script, cap):
        """照真实流程走一遍：分组出图，每条分句一条字幕。

        字幕长度故意各不相同。每条一样长的话，按分组算和按平均铺算出来的
        结果一模一样,这些测试就什么也证明不了——真实的旁白从来不是匀速的,
        那恰恰是当初不同步的原因。
        """
        groups = utils.split_narration_into_sentences(script, max_seconds=cap)
        scenes = [
            {"index": i, "material": f"{i}.mp4", "narration": text}
            for i, (text, _) in enumerate(groups)
        ]
        clauses = utils.split_string_by_punctuations(script)
        cues = []
        at = 0.0
        for index, clause in enumerate(clauses):
            length = 1.0 + (index % 3) * 1.5
            cues.append({"start": at, "end": at + length, "text": clause})
            at += length
        return scenes, cues, at

    def _plan(self, task, scenes, cues, script, cap, **kwargs):
        raw = {"script": script, "params": {"max_image_scene_duration": cap}}
        with (
            patch.object(scene_manifest, "narration_segments", return_value=cues),
            patch.object(scene_manifest, "load_raw", return_value=raw),
            patch.object(timeline, "probe_duration", return_value=600.0),
        ):
            return timeline.plan_windows(task, scenes, **kwargs)

    def test_each_capped_picture_still_spans_its_own_clauses(self, task):
        script = "One two three four five, six seven eight nine ten eleven. Short."
        cap = 3.0
        scenes, cues, total = self._scenes_and_cues(script, cap)
        assert len(scenes) > 2, "这段脚本在 3 秒上限下必须被拆开，否则没测到东西"

        windows = self._plan(
            task, scenes, cues, script, cap, audio_duration=total
        )
        assert len(windows) == len(scenes)
        # 切换点落在字幕的边界上，而不是平均分出来的位置。
        for earlier, later in zip(windows, windows[1:]):
            assert earlier["end"] == later["start"]
        assert windows[0]["start"] == 0.0

    def test_the_grouping_is_read_from_the_scenes_not_recomputed(self, task):
        """脚本没变但分组参数变了时，照场景里存的那句话数才不会错。"""
        script = "One two three four five, six seven eight nine ten eleven. Short."
        scenes, cues, total = self._scenes_and_cues(script, 3.0)
        # 清单里存的是"没有上限"，和场景的实际分组不一致。照场景数就不受影响。
        windows = self._plan(
            task, scenes, cues, script, 0.0, audio_duration=total
        )
        spans = [round(w["end"] - w["start"], 3) for w in windows]
        assert len(windows) == len(scenes)
        assert len(set(spans)) > 1, "退回平均铺的话每段会一样长，那就是没同步上"

    def test_a_scene_without_stored_narration_falls_back_to_the_script(self, task):
        script = "A, b. C."
        cap = 0.0
        groups = utils.split_narration_into_sentences(script, max_seconds=cap)
        scenes = [{"index": i, "material": f"{i}.mp4"} for i in range(len(groups))]
        cues = [
            {"start": float(i), "end": float(i) + 1.0}
            for i in range(len(utils.split_string_by_punctuations(script)))
        ]
        windows = self._plan(
            task, scenes, cues, script, cap, audio_duration=float(len(cues))
        )
        assert len(windows) == len(scenes)
        assert windows[0]["end"] == pytest.approx(2.0, abs=0.001)


class TestTheRealScriptGetsShorterHolds:
    """用用户那条真脚本量一遍——这是这个功能存在的理由。"""

    SCRIPT_PATH = (
        "storage/tasks/46516451-0eef-4013-b0e7-5677018f04a8/script.json"
    )

    @pytest.fixture
    def script(self):
        try:
            with open(self.SCRIPT_PATH, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            pytest.skip("那条任务只在开发机上")
        return utils.remove_pause_tags(str(raw.get("script") or ""))

    def test_the_longest_hold_comes_down(self, script):
        def longest(cap):
            groups = utils.split_narration_into_sentences(script, max_seconds=cap)
            return max(utils.estimate_spoken_seconds(text) for text, _ in groups)

        assert longest(None) > 18.0, "改之前最长的一句要念 18.6 秒"
        assert longest(8.0) < 14.0

    def test_the_cost_is_about_a_sixth_more_pictures(self, script):
        plain = utils.split_narration_into_sentences(script)
        capped = utils.split_narration_into_sentences(script, max_seconds=8.0)
        assert len(plain) == 96
        assert len(capped) == 112
        assert sum(c for _, c in plain) == sum(c for _, c in capped)
