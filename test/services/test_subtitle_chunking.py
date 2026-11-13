# -*- coding: utf-8 -*-
"""一次只给看一句话那么多字。

用户的原话："we are showing a big paragraph with a lot of text, we should show
only one sentence/phrase per time"。字幕文件里的一条是旁白的一个分句，念上
六七秒是常事，大字号竖屏里就是四五行一堵字墙。

这里钉的是两件事：切出来的那几条读起来对（时间摊得对、字没丢、每条都放得
下），以及**字幕文件本身不许动**——它同时是每段画面停多久的唯一依据。
"""

import json

import pytest

from app.services.utils import subtitle_chunking


def line_counter(width):
    """假的排版器：一行放得下 ``width`` 个字符，按词贪心折行。

    真正的那个要读字体文件，所以这里用一个说得清的替身——测的是切条的规矩，
    不是 Pillow 怎么量字。
    """

    def count(text):
        lines = 1
        used = 0
        for word in text.split():
            needed = len(word) if used == 0 else len(word) + 1
            if used + needed > width:
                lines += 1
                used = len(word)
            else:
                used += needed
        return lines

    return count


def cjk_line_counter(width):
    def count(text):
        return max(1, -(-len(text) // width))

    return count


# 用户那条片子里第一条字幕，原样照搬：77 个字符、6.59 秒。
LONG = "What if I told you square windows once helped tear airplanes apart in the sky"
LONG_CUE = ((0.0, 6.59), LONG)


class TestWhatFitsIsLeftAlone:
    """绝大多数条本来就放得下，它们一个字都不该变。"""

    def test_a_short_cue_comes_back_untouched(self):
        cue = ((1.0, 2.5), "In nineteen fifty two")
        assert subtitle_chunking.split_long_cues([cue], line_counter(28)) == [cue]

    def test_a_cue_exactly_at_the_limit_is_not_split(self):
        text = "a" * 20 + " " + "b" * 20
        count = line_counter(21)
        assert count(text) == 2
        assert subtitle_chunking.split_long_cues(
            [((0.0, 3.0), text)], count, max_lines=2
        ) == [((0.0, 3.0), text)]

    def test_zero_turns_it_off(self):
        """0 就是这个设置出现之前的样子。"""
        assert subtitle_chunking.split_long_cues(
            [LONG_CUE], line_counter(28), max_lines=0
        ) == [LONG_CUE]

    def test_a_word_too_long_to_break_is_kept(self):
        """一个词自己就超了（没有空格的一长串）。切不动，不如留着。"""
        cue = ((0.0, 2.0), "a" * 60)
        assert subtitle_chunking.split_long_cues([cue], line_counter(10)) == [cue]

    def test_a_measurement_that_blows_up_does_not_lose_the_cue(self):
        """量不出来（字体读不出来之类）时宁可不切，不能少画一条。"""

        def broken(_text):
            raise OSError("cannot open resource")

        assert subtitle_chunking.split_long_cues([LONG_CUE], broken) == [LONG_CUE]


class TestALongCueBecomesSeveral:
    def setup_method(self):
        self.pieces = subtitle_chunking.split_long_cues(
            [LONG_CUE], line_counter(28), max_lines=2
        )

    def test_it_is_actually_split(self):
        assert len(self.pieces) == 2

    def test_every_piece_fits_on_screen(self):
        count = line_counter(28)
        assert all(count(text) <= 2 for _, text in self.pieces)

    def test_not_a_word_is_lost_and_the_order_holds(self):
        assert " ".join(text for _, text in self.pieces) == LONG

    def test_the_pieces_are_about_the_same_length(self):
        """贪心装会剩出"……in the sky"这种一两个词的尾巴，读起来像断了气。"""
        lengths = [len(text) for _, text in self.pieces]
        assert max(lengths) <= 2 * min(lengths)

    def test_the_cue_keeps_its_own_start_and_end(self):
        assert self.pieces[0][0][0] == pytest.approx(0.0)
        assert self.pieces[-1][0][1] == pytest.approx(6.59)

    def test_the_time_is_shared_out_not_invented(self):
        total = sum(end - start for (start, end), _ in self.pieces)
        assert total == pytest.approx(6.59)

    def test_the_pieces_do_not_overlap_or_leave_gaps(self):
        for before, after in zip(self.pieces, self.pieces[1:]):
            assert before[0][1] == pytest.approx(after[0][0])
            assert before[0][1] > before[0][0]

    def test_the_longer_piece_gets_the_longer_time(self):
        """按字数摊：话多的那条停得久一点。"""
        (first, second) = self.pieces
        if len(first[1]) > len(second[1]):
            assert first[0][1] - first[0][0] > second[0][1] - second[0][0]
        else:
            assert second[0][1] - second[0][0] > first[0][1] - first[0][0]


class TestItDoesNotFlashByTooFastToRead:
    def test_a_fast_cue_is_not_cut_into_unreadable_slivers(self):
        """念得特别快的那句切开后每条都看不清，不如留一条长的。"""
        cue = ((0.0, 0.9), "a very long line that would be cut into many little pieces")
        pieces = subtitle_chunking.split_long_cues(
            [cue], line_counter(10), max_lines=2, min_seconds=0.6
        )
        assert len(pieces) == 1

    def test_nothing_on_screen_for_less_than_the_floor(self):
        cue = ((0.0, 3.0), " ".join(f"word{at}" for at in range(24)))
        pieces = subtitle_chunking.split_long_cues(
            [cue], line_counter(20), max_lines=2, min_seconds=0.6
        )
        assert pieces
        assert all(end - start >= 0.6 for (start, end), _ in pieces)


class TestWritingWithoutSpaces:
    """中日韩没有空格，断行只能断在字与字之间，而且不能凭空多出空格。"""

    SENTENCE = "这首诗描绘了诗人在寂静的夜晚看到窗前的明月不禁想起远方的家乡和亲人"

    def test_it_splits_between_characters(self):
        pieces = subtitle_chunking.split_long_cues(
            [((0.0, 5.0), self.SENTENCE)], cjk_line_counter(14), max_lines=2
        )
        assert len(pieces) == 2
        assert "".join(text for _, text in pieces) == self.SENTENCE

    def test_no_space_is_invented(self):
        pieces = subtitle_chunking.split_long_cues(
            [((0.0, 5.0), self.SENTENCE)], cjk_line_counter(14), max_lines=2
        )
        assert all(" " not in text for _, text in pieces)


class TestTheWholeFileGoesThrough:
    def test_cues_that_fit_and_cues_that_do_not_keep_their_order(self):
        cues = [
            ((0.0, 6.59), LONG),
            ((6.59, 8.39), "In nineteen fifty two"),
            ((8.39, 11.72), "Britain launched the de Havilland Comet"),
        ]
        pieces = subtitle_chunking.split_long_cues(cues, line_counter(28), max_lines=2)
        assert len(pieces) > len(cues)
        starts = [start for (start, _), _ in pieces]
        assert starts == sorted(starts)
        assert pieces[-1] == cues[-1]
        assert pieces[0][0][0] == pytest.approx(0.0)
        assert pieces[-1][0][1] == pytest.approx(11.72)


class TestTheSubtitleFileItselfIsNotTouched:
    """那个文件同时是每段画面停多久的唯一依据。

    ``scene_manifest.narration_segments`` 读的就是它，``timeline`` 按"一条字幕
    一段画面"配图（``_windows_from_cues``）。条数一变，画面和话就对不上——所以
    切只能切在渲染那一步，文件一个字节都不许动。
    """

    @staticmethod
    def _video_source(name):
        import inspect

        from app.services import video

        return inspect.getsource(getattr(video, name))

    def test_the_split_happens_after_the_file_is_read_and_before_it_is_drawn(self):
        source = self._video_source("generate_video")
        read = source.index("moviepy_subtitles(subtitle_path)")
        split = source.index("subtitle_chunking.split_long_cues")
        drawn = source.index("SubtitlesClip(")
        assert read < split < drawn

    def test_it_is_measured_with_the_font_that_will_be_drawn(self):
        source = self._video_source("generate_video")
        assert "subtitles, count_subtitle_lines, max_lines=max_lines" in source
        # 量和画必须用同一个宽度，否则量出来两行、画出来三行。
        assert source.count("subtitle_text_max_width(") >= 2

    def test_nothing_in_the_renderer_rewrites_the_subtitle_file(self):
        source = self._video_source("generate_video")
        assert "write_subtitle_file" not in source

    def test_the_splitter_only_returns_data(self):
        import inspect

        source = inspect.getsource(subtitle_chunking)
        for forbidden in ("open(", "write", "os.", "subtitle_path"):
            assert forbidden not in source


class TestTheSettingIsWiredUp:
    def test_video_params_carries_it(self):
        from app.config import config
        from app.models.schema import VideoParams

        expected = int(config.ui.get("subtitle_max_lines", 2))
        assert VideoParams(video_subject="x").subtitle_max_lines == expected

    def test_a_broken_config_value_falls_back_instead_of_crashing(self):
        from app.models import schema

        assert schema._get_valid_ui_int("subtitle_max_lines", 2) >= 0

    def test_the_webui_and_the_cli_both_offer_it(self):
        import inspect
        from pathlib import Path

        import cli

        assert "--subtitle-max-lines" in inspect.getsource(cli)
        root = Path(__file__).parent.parent.parent
        webui = (root / "webui" / "Main.py").read_text(encoding="utf-8")
        assert 'key="subtitle_max_lines_select"' in webui
        assert '_set_runtime_config(\n                "ui", "subtitle_max_lines"' in webui

    def test_every_locale_can_label_it(self):
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        locales = sorted((root / "webui" / "i18n").glob("*.json"))
        assert len(locales) == 14
        for path in locales:
            words = json.loads(path.read_text(encoding="utf-8"))["Translation"]
            for key in ("Lines On Screen", "Lines On Screen Help", "No Limit"):
                assert words.get(key), f"{path.name} is missing {key}"
