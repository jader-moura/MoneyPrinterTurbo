"""一张图配一整句话，而不是配逗号切出来的半句。

Why these tests exist
---------------------
断句用的 ``split_string_by_punctuations`` 连逗号一起断——配音和字幕要的就是
这么细。可画面不是：用户那条真片子里 19 句话被切成 29 段，其中 16 段是
"in Texas"、"Your laptop" 这样念不到一秒的碎片。为每个碎片单画一张图，是
按半句话去烧几分钟显卡，而且那张图短到根本看不清。

改成按句出图之后，条数就和字幕不一样了（19 对 29）。所以这里钉住两件事：
分句的条数账要算得平（每句占几条字幕，加起来必须等于字幕总数），以及时间线
要能照这个账把字幕合回去——否则画面会对到别人的句子上，那比平均铺更难查，
因为它看起来像是图配错了。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import llm, scene_manifest, task as task_service, timeline, twelvelabs
from app.utils import utils


SCRIPT = (
    "Your TV might be taking two pictures of your screen every second.\n"
    "It is called A C R, automatic content recognition. "
    "Think of it as Shazam for your TV.\n"
    "The goal? Knowing what you watch, so advertisers can target you.\n"
)


class TestSentencesAndTheirClauseCounts:
    def test_the_counts_add_up_to_the_clauses(self):
        """这本账必须是平的，不然时间线合字幕时就会错位。"""
        groups = utils.split_narration_into_sentences(SCRIPT)
        clauses = utils.split_string_by_punctuations(SCRIPT)
        assert sum(count for _, count in groups) == len(clauses)

    def test_a_comma_does_not_end_a_picture_but_a_full_stop_does(self):
        groups = utils.split_narration_into_sentences(
            "It is called A C R, automatic content recognition. Next sentence."
        )
        assert [count for _, count in groups] == [2, 1]
        assert groups[0][0] == "It is called A C R, automatic content recognition."

    @pytest.mark.parametrize(
        "script, counts",
        [
            ("A? B! C.", [1, 1, 1]),
            ("No final punctuation", [1]),
            ("", []),
            ("   ", []),
            ("One, two, three.", [3]),
            ("Semicolons; stay inside. Yes.", [2, 1]),
        ],
    )
    def test_shapes(self, script, counts):
        groups = utils.split_narration_into_sentences(script)
        assert [count for _, count in groups] == counts

    def test_the_sentence_reads_as_a_sentence(self):
        """送给模型的是一句话，不是用空格拼起来的几个碎片。"""
        groups = utils.split_narration_into_sentences("Wait, there is more. Done.")
        assert groups[0][0] == "Wait, there is more."

    def test_the_clause_splitter_itself_did_not_change(self):
        """重构只是把"结尾是哪个标点"也带出来，断句结果一个字都不能变。"""
        for script in [
            SCRIPT,
            "U.S. spying is real, and it spreads.",
            "10:30 is fine, 1,000 too.",
            "No.1 and Vol.2",
            "",
        ]:
            assert utils.split_string_by_punctuations(script) == [
                text for text, _ in utils._split_with_boundaries(script)
            ]


class TestThePromptsFollowSentences:
    """开着的时候要的是句子，关掉之后回到原来的半句。"""

    def _params(self, **extra):
        return VideoParams(
            video_subject="how smart TVs watch you",
            video_terms="",
            align_terms_to_narration=True,
            match_materials_to_script=False,
            video_source="openai_image",
            **extra,
        )

    def _lines_asked_for(self, params):
        with (
            patch.object(
                llm, "generate_terms_per_line", return_value=["p"]
            ) as per_line,
            patch.object(
                twelvelabs,
                "rerank_terms_by_subject",
                side_effect=lambda **kw: kw["search_terms"],
            ),
        ):
            task_service.generate_terms("t", params, SCRIPT)
        return per_line.call_args.kwargs["narration_lines"]

    def test_by_default_one_prompt_per_sentence(self):
        lines = self._lines_asked_for(self._params())
        assert lines == [text for text, _ in utils.split_narration_into_sentences(SCRIPT)]
        assert len(lines) < len(utils.split_string_by_punctuations(SCRIPT)), (
            "按句出图就该比按半句出图少，这正是省下来的显卡时间"
        )

    def test_turning_it_off_goes_back_to_one_prompt_per_clause(self):
        lines = self._lines_asked_for(self._params(one_picture_per_sentence=False))
        assert lines == utils.split_string_by_punctuations(SCRIPT)


class TestTheTimingGroupsTheCuesToMatch:
    """19 张图配 29 条字幕时，每张图占的是它那一句的几条。"""

    @pytest.fixture
    def task(self, tmp_path, monkeypatch):
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

    # "A, b. C. D, e, f." -> 3 句，占 2 + 1 + 3 = 6 条字幕
    SCRIPT = "A, b. C. D, e, f."
    CUES = [
        {"start": 0.0, "end": 1.0},   # A
        {"start": 1.0, "end": 2.0},   # b      -> 第一句 0..2
        {"start": 2.5, "end": 8.0},   # C      -> 第二句 2.5..8
        {"start": 8.5, "end": 9.0},   # D
        {"start": 9.0, "end": 9.5},   # e
        {"start": 9.5, "end": 10.0},  # f      -> 第三句 8.5..10
    ]

    def _plan(self, task, scenes, *, script=None, cues=None, **kwargs):
        with (
            patch.object(
                scene_manifest, "narration_segments", return_value=cues or self.CUES
            ),
            patch.object(
                scene_manifest,
                "load_raw",
                return_value={"script": script if script is not None else self.SCRIPT},
            ),
            patch.object(timeline, "probe_duration", return_value=60.0),
        ):
            return timeline.plan_windows(task, scenes, **kwargs)

    def _scenes(self, count):
        return [{"index": i, "material": f"{i}.mp4"} for i in range(count)]

    def test_each_picture_spans_its_whole_sentence(self, task):
        windows = self._plan(task, self._scenes(3), audio_duration=10.0)

        assert len(windows) == 3
        assert windows[0]["start"] == 0.0
        # 切换点落在下一句开口的时刻。
        assert windows[1]["start"] == pytest.approx(2.5, abs=0.001)
        assert windows[2]["start"] == pytest.approx(8.5, abs=0.001)
        assert windows[-1]["end"] >= 10.0

    def test_the_long_sentence_gets_the_long_hold(self, task):
        windows = self._plan(task, self._scenes(3), audio_duration=10.0)
        spans = [item["end"] - item["start"] for item in windows]
        assert spans[1] > spans[0], "第二句念了 5.5 秒，就该停 5.5 秒"
        assert spans[1] == pytest.approx(6.0, abs=0.05)

    def test_no_gaps_between_pictures(self, task):
        windows = self._plan(task, self._scenes(3), audio_duration=10.0)
        for earlier, later in zip(windows, windows[1:]):
            assert earlier["end"] == later["start"]

    def test_a_script_that_does_not_add_up_falls_back(self, task):
        """账不平时宁可平均铺，也不能把某句的时间安到另一句的画面上。"""
        windows = self._plan(
            task, self._scenes(3), script="Totally different text.", audio_duration=10.0
        )
        spans = [item["end"] - item["start"] for item in windows]
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_a_missing_script_falls_back(self, task):
        windows = self._plan(task, self._scenes(3), script="", audio_duration=10.0)
        spans = [item["end"] - item["start"] for item in windows]
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_a_scene_count_that_is_not_the_sentence_count_falls_back(self, task):
        windows = self._plan(task, self._scenes(4), audio_duration=10.0)
        spans = [item["end"] - item["start"] for item in windows]
        assert len(windows) == 4
        assert all(span == pytest.approx(spans[0], abs=0.01) for span in spans)

    def test_one_picture_per_cue_still_wins_when_the_counts_match(self, task):
        """按半句出图的老任务不受影响：条数相等时还是一对一。"""
        windows = self._plan(task, self._scenes(6), audio_duration=10.0)
        assert len(windows) == 6
        assert windows[1]["start"] == pytest.approx(1.0, abs=0.001)
