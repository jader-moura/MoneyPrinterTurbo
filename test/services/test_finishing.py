"""成片精修：ffmpeg 那一遍、ASS 字幕、叠化和新加的几种运镜。

钉住的事：

* ``finish_look`` 为空时成片照旧由原来的渲染器出，不为空才走精修；
* 混音图里旁白的处理、配乐让位、转场音效各在各的位置上，开关关掉就没有；
* 字幕：词的时间从字幕文件里摊出来，拼写是脚本的；一组几个词、念到哪个
  哪个变色；标题卡要在旁白里找到它那句话；
* 叠化从"下一段本来该开始的那一刻"开始，成片总长不变；
* 新运镜进了文件名、时间线认得、画面确实在动。

The finishing pass, its subtitles, crossfades and the added camera moves.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.models.schema import ImageMotion, VideoParams, VideoTransitionMode
from app.services import finishing, llm, new_video, timeline, video
from app.services.utils import ass_subtitles, video_effects
from app.utils import utils

ROOT = Path(__file__).resolve().parents[2]
CUES = [
    ((0.0, 2.0), "The tent was still standing,"),
    ((2.4, 4.0), "led by Igor Dyatlov"),
]


class TestWhenItRuns:
    def test_a_task_without_a_look_keeps_the_classic_renderer(self):
        params = VideoParams(video_subject="x")
        assert params.finish_look == ""
        assert not finishing.wanted(params)

    def test_a_look_turns_it_on(self):
        assert finishing.wanted(VideoParams(video_subject="x", finish_look="clean"))

    def test_an_unknown_look_is_named_in_the_error(self):
        with pytest.raises(finishing.FinishError, match="sepia"):
            finishing.look_of(VideoParams(video_subject="x", finish_look="sepia"))

    def test_the_task_chooses_between_the_two(self):
        source = (ROOT / "app/services/task.py").read_text(encoding="utf-8")
        assert (
            "finishing.render if finishing.wanted(params) else video.generate_video"
            in source
        )

    def test_settings_fall_back_to_the_defaults(self):
        with patch.object(finishing.config, "finish", {"loudness": "loud", "grain": 0},
                          create=True):
            assert finishing.setting("loudness") == -14.0
            assert finishing.setting("grain") == 0
            assert finishing.setting("vignette") is True


class TestTheMix:
    def _graph(self, **more):
        options = dict(duration=30.0, voice_gain=5.0, music_gain=-19.0)
        options.update(more)
        return finishing.mix_graph(**options)

    def test_voice_is_processed_and_music_ducks_under_it(self):
        graph = self._graph()
        assert "highpass=f=70" in graph and "acompressor=" in graph
        assert "[bed][key]sidechaincompress=" in graph
        assert "amix=inputs=2:normalize=0" in graph
        assert "atrim=duration=30.000" in graph

    def test_without_music_there_is_nothing_to_duck(self):
        graph = self._graph(music_gain=None)
        assert "sidechaincompress" not in graph and "amix=inputs=1" in graph

    def test_the_switches_take_the_steps_out(self):
        graph = self._graph(voice_processing=False, ducking=False)
        assert "highpass" not in graph and "sidechaincompress" not in graph

    def test_cut_sounds_land_on_their_cuts(self):
        graph = self._graph(cut_sounds=[(2, [1.5, 4.25])], cut_volume=0.5)
        assert "adelay=1500:all=1" in graph and "adelay=4250:all=1" in graph
        assert "amix=inputs=4" in graph


class TestThePicture:
    def test_clean_has_no_grade_and_no_texture(self):
        chain = finishing.picture_filters(look="clean", duration=60.0)
        assert "eq=" not in chain and "noise=" not in chain and "vignette" not in chain
        assert "fade=t=in" in chain and "fade=t=out:st=58.000" in chain

    def test_cold_documentary_grades_and_adds_texture(self):
        chain = finishing.picture_filters(look="cold_documentary", duration=60.0)
        assert "saturation=0.82" in chain and "vignette=angle=PI/7" in chain
        assert "noise=c0s=5" in chain

    def test_subtitles_are_burned_before_the_fades(self):
        chain = finishing.picture_filters(
            look="clean", duration=60.0, subtitle_file="final-1.ass",
            fonts_dir="C:/fonts")
        assert "ass='final-1.ass':fontsdir='C\\:/fonts'" in chain
        assert chain.index("ass=") < chain.index("fade=t=in")

    def test_a_very_short_video_is_not_faded_to_nothing(self):
        assert "fade=" not in finishing.picture_filters(look="clean", duration=1.5)
        only_in = finishing.picture_filters(look="clean", duration=3.0)
        assert "fade=t=in" in only_in and "fade=t=out" not in only_in


class TestWords:
    def test_times_stay_inside_their_cue_and_spelling_is_the_scripts(self):
        words = ass_subtitles.words_from_cues(CUES)
        assert [word.text for word in words[:3]] == ["The", "tent", "was"]
        assert words[0].start == 0.0 and words[4].end == 2.0 and words[4].ends_cue
        assert words[5].start == 2.4 and words[-1].end == 4.0

    def test_groups_break_at_punctuation_cue_ends_and_size(self):
        groups = ass_subtitles.group_words(ass_subtitles.words_from_cues(CUES))
        assert [[word.text for word in group] for group in groups] == [
            ["The", "tent", "was", "still"], ["standing,"],
            ["led", "by", "Igor", "Dyatlov"],
        ]

    def test_recognised_times_are_taken_with_the_scripts_spelling(self):
        words = ass_subtitles.words_from_cues([((0.0, 3.0), "led by Igor Dyatlov")])
        spoken = [(0.5, 0.7, "led"), (0.7, 0.9, "by"), (0.9, 1.3, "Eager"),
                  (1.3, 2.0, "Dyathov")]
        timed = ass_subtitles.align_spoken_words(words, spoken)
        assert [word.text for word in timed] == ["led", "by", "Igor", "Dyatlov"]
        assert timed[0].start == 0.5 and timed[1].start == 0.7
        assert timed[2].start == 0.9 and timed[3].end == 2.0

    def test_words_the_recogniser_skipped_sit_between_their_neighbours(self):
        # 估出来的时间差了好几秒：漏听的词不能留在那儿。
        words = ass_subtitles.words_from_cues([((10.0, 14.0), "led by Igor Dyatlov")])
        spoken = [(0.5, 0.7, "led"), (1.5, 2.0, "Dyatlov")]
        timed = ass_subtitles.align_spoken_words(words, spoken)
        assert timed[0].start == 0.5 and timed[3].start == 1.5
        assert timed[1].start == 0.7 and timed[2].end == 1.5
        assert timed[1].end == timed[2].start

    def test_a_word_stretched_over_skipped_ones_gives_the_time_back(self):
        words = ass_subtitles.words_from_cues([((0.0, 4.0), "led by Igor Dyatlov")])
        spoken = [(1.0, 3.0, "led"), (3.0, 3.5, "Dyatlov")]
        timed = ass_subtitles.align_spoken_words(words, spoken)
        assert timed[0].start == 1.0 and timed[0].end < 2.0
        assert timed[1].start == timed[0].end and timed[2].end == 3.0

    def test_the_stretched_word_may_sit_a_word_or_two_before_the_gap(self):
        words = ass_subtitles.words_from_cues(
            [((0.0, 9.0), "came from the Green River south was found")])
        spoken = [(0.0, 0.4, "came"), (0.4, 4.4, "from"), (4.4, 4.5, "the"),
                  (4.5, 4.9, "was"), (4.9, 5.3, "found")]
        timed = ass_subtitles.align_spoken_words(words, spoken)
        assert timed[1].end < 1.2                      # "from" is cut back
        assert timed[2].start == timed[1].end          # "the" follows it
        assert 1.5 < timed[4].start < 4.0              # "River" is inside the gap
        assert timed[6].start == 4.5 and timed[5].end == 4.5

    def test_skipped_words_at_either_end_keep_their_length(self):
        words = ass_subtitles.words_from_cues([((10.0, 12.0), "So led by Igor")])
        spoken = [(3.0, 3.2, "led"), (3.2, 3.4, "by")]
        timed = ass_subtitles.align_spoken_words(words, spoken)
        assert timed[0].end == 3.0 and 2.0 < timed[0].start < 3.0
        assert timed[3].start == 3.4 and timed[3].end > 3.4

    def test_a_phrase_is_found_whatever_its_punctuation(self):
        words = ass_subtitles.words_from_cues(CUES)
        assert ass_subtitles.find_phrase(words, "Led by Igor Dyatlov.") == 2.4
        assert ass_subtitles.find_phrase(words, "led by Yuri") is None

    def test_time_never_reads_sixty_seconds(self):
        assert ass_subtitles.ass_time(59.999) == "0:01:00.00"
        assert ass_subtitles.ass_time(3725.5) == "1:02:05.50"

    def test_colours_are_blue_green_red(self):
        assert ass_subtitles.ass_colour("#FFD73F") == "&H003FD7FF"


class TestTheSubtitleFile:
    def _build(self, mode, **more):
        style = ass_subtitles.Style(font="Inter", uppercase=mode == "highlight")
        return ass_subtitles.build(CUES, width=1920, height=1080, style=style,
                                   mode=mode, **more)

    def test_the_spoken_word_is_lit(self):
        text = self._build("highlight")
        assert "{\\c&H3FD7FF&}THE{\\c&HFFFFFF&} TENT WAS STILL" in text
        assert "THE {\\c&H3FD7FF&}TENT{\\c&HFFFFFF&} WAS STILL" in text
        assert "PlayResX: 1920" in text

    def test_a_sentence_at_a_time_is_one_event_per_cue(self):
        text = self._build("sentence")
        assert text.count("Dialogue:") == 2
        assert "The tent was still standing," in text

    def test_a_long_cue_is_cut_to_the_lines_allowed(self):
        cues = [((0.0, 10.0), " ".join(["mountain"] * 40))]
        style = ass_subtitles.Style(font="Inter", size=76)
        text = ass_subtitles.build(cues, width=1920, height=1080, style=style,
                                   mode="sentence", max_lines=1)
        assert text.count("Dialogue:") > 4

    def test_a_word_at_a_time(self):
        assert self._build("word_by_word").count("Dialogue:") == 9

    def test_title_cards_are_drawn_without_captions(self):
        card = ass_subtitles.TitleCard(2.4, 8.4, "Igor Dyatlov", "Group leader")
        text = ass_subtitles.build([], width=1920, height=1080,
                                   style=ass_subtitles.Style(font="Inter"),
                                   title_cards=[card])
        assert "IGOR DYATLOV" in text and "Group leader" in text
        assert text.count("Dialogue:") == 3


class TestTitleCards:
    def test_cards_need_their_phrase_in_the_narration(self):
        answers = ["led by Igor Dyatlov | IGOR DYATLOV | Group leader",
                   "in the year of the comet | 1961 | Somewhere else",
                   "no bars here"]
        with patch.object(llm, "_request_terms", return_value=answers):
            cards = llm.generate_title_cards(
                "Dyatlov", "The group was led by Igor Dyatlov, a student.")
        assert cards == [{"phrase": "led by Igor Dyatlov", "title": "IGOR DYATLOV",
                          "subtitle": "Group leader"}]

    def test_the_saved_choice_is_used_and_timed_from_the_words(self, tmp_path):
        (tmp_path / "title-cards.json").write_text(
            '[{"phrase": "led by Igor", "title": "Igor Dyatlov", "subtitle": "Leader"},'
            ' {"phrase": "not said", "title": "Nobody", "subtitle": ""}]',
            encoding="utf-8")
        words = ass_subtitles.words_from_cues(CUES)
        with patch.object(llm, "generate_title_cards") as asked:
            cards = finishing.title_cards(
                str(tmp_path), VideoParams(video_subject="x"), words, 120.0)
        asked.assert_not_called()
        assert [(card.title, card.start) for card in cards] == [("Igor Dyatlov", 2.4)]

    def test_a_model_that_fails_costs_the_cards_not_the_video(self, tmp_path):
        (tmp_path / "script.json").write_text('{"script": "led by Igor Dyatlov"}',
                                              encoding="utf-8")
        words = ass_subtitles.words_from_cues(CUES)
        with patch.object(llm, "generate_title_cards", side_effect=RuntimeError("down")):
            assert finishing.title_cards(
                str(tmp_path), VideoParams(video_subject="x"), words, 120.0) == []
        assert not (tmp_path / "title-cards.json").exists()


class TestCrossfades:
    def test_the_dissolve_starts_where_the_cut_was(self):
        graph, label = video._crossfade_graph(
            [("a.mp4", 3.0, 0.5), ("b.mp4", 2.0, 0.0), ("c.mp4", 4.0, 0.0)],
            keep_tail=False)
        assert "xfade=transition=fade:duration=0.5000:offset=3.0000" in graph
        # 第二刀是硬切。
        assert "[v1][c2]concat=n=2:v=1:a=0[v2]" in graph and label == "[v2]"
        assert "[0:v]" in graph and "trim=duration=3.5000" in graph
        assert "trim=duration=4.0000" in graph

    def test_a_short_clip_gets_a_shorter_dissolve(self):
        assert video.crossfade_seconds(5.0, 5.0) == 0.5
        assert video.crossfade_seconds(0.6, 5.0) == pytest.approx(0.27)

    def test_it_is_a_transition_a_scene_or_a_video_can_choose(self):
        from app.services import scene_editor

        assert VideoTransitionMode.cross_fade.value == "CrossFade"
        assert scene_editor.SCENE_TRANSITIONS["cross_fade"] == "CrossFade"

    def test_the_joined_video_is_as_long_as_its_clips(self, tmp_path):
        ffmpeg = utils.get_ffmpeg_binary()
        pieces = []
        for index, (colour, seconds) in enumerate(
                [("red", 1.0), ("green", 1.2), ("blue", 0.8)] * 5):
            clip = tmp_path / f"clip{index}.mp4"
            subprocess.run(
                [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                 f"color={colour}:s=160x90:r=30:d={seconds}", "-pix_fmt", "yuv420p",
                 str(clip)], check=True)
            pieces.append((str(clip), seconds, 0.4))
        pieces[-1] = (pieces[-1][0], pieces[-1][1], 0.0)
        output = tmp_path / "joined.mp4"
        # 十五段、一次最多接十二段：要先接成两块再接起来。
        video.crossfade_video_clips_with_ffmpeg(pieces, str(output), 2, str(tmp_path))
        assert timeline.probe_duration(str(output)) == pytest.approx(15.0, abs=0.08)
        assert sorted(os.listdir(tmp_path))[-1] == "joined.mp4"
        assert not [name for name in os.listdir(tmp_path) if name.startswith(".ffmpeg")]


class TestCameraMoves:
    NEW = (ImageMotion.varied, ImageMotion.zoom_out, ImageMotion.pan_right,
           ImageMotion.pan_left, ImageMotion.zoom_top)

    def test_each_move_has_its_own_clip_name(self):
        suffixes = {video.image_clip_suffix(3, motion) for motion in ImageMotion}
        assert len(suffixes) == len(ImageMotion)
        for motion in self.NEW:
            name = "a" * 64 + video.image_clip_suffix(3, motion)
            assert timeline.motion_of(name) == motion.value

    def test_the_old_names_did_not_change(self):
        assert video.image_clip_suffix(3, ImageMotion.zoom).startswith(".zoom-")
        assert video.image_clip_suffix(3, ImageMotion.static).startswith(".static-")

    def test_a_pan_moves_the_window_sideways(self):
        frame = np.zeros((90, 160, 3), dtype=np.uint8)
        frame[:, :80] = 255
        left = video_effects.move_still_frame(frame, 1.12, 0.0, 0.5)
        right = video_effects.move_still_frame(frame, 1.12, 1.0, 0.5)
        assert left.shape == frame.shape
        assert left.mean() > right.mean()

    def test_the_scene_editor_offers_them(self):
        from app.services import scene_editor

        for motion in self.NEW:
            assert motion.value in scene_editor.SCENE_MOTIONS


class TestTheNewVideoForm:
    def test_a_new_video_is_finished_unless_that_was_turned_off(self):
        assert new_video.saved_settings({}, {})["finish_look"] == "cold_documentary"
        assert new_video.saved_settings({"finish_look": ""}, {})["finish_look"] == ""
        assert new_video.saved_settings({"finish_look": "warm"}, {})["finish_look"] == "warm"

    def test_the_first_finished_video_starts_from_the_new_subtitles_and_moves(self):
        before = {"subtitle_display_mode": "sentence", "image_motion": "zoom",
                  "video_transition_mode": "None"}
        first = new_video.saved_settings(before, {})
        assert first["subtitle_display_mode"] == "highlight"
        assert first["image_motion"] == "varied"
        assert first["video_transition_mode"] == "CrossFade"

    def test_after_that_what_was_chosen_is_remembered(self):
        chosen = {"finish_look": "warm", "subtitle_display_mode": "sentence",
                  "image_motion": "zoom"}
        later = new_video.saved_settings(chosen, {})
        assert later["subtitle_display_mode"] == "sentence"
        assert later["image_motion"] == "zoom"

    def test_a_look_that_no_longer_exists_goes_back_to_the_default(self):
        assert (new_video.saved_settings({"finish_look": "sepia"}, {})["finish_look"]
                == "cold_documentary")
