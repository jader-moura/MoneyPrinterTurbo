"""Stats and the title on black: the two things a cold open is built from.

What is pinned:

* a scene's ``stat`` is a figure and a caption, in either spelling, with a
  length limit on each; it cannot share a scene with the title;
* the title takes ``"on": "black"``, and nothing else there;
* a stat is up while its line is spoken, at least 2.2 s and at most 5, and
  leaves before the next one;
* a title on black takes the silence before its scene when there is enough,
  and the first seconds of the scene when there is not;
* the subtitle file draws both, the timeline strip steps aside for a stat,
  and the editor is handed the same things with the same numbers.
"""

import json
from pathlib import Path

import pytest

from app.models.schema import VideoParams
from app.services import finishing, scene_plan
from app.services.utils import ass_subtitles

PAGE = Path(__file__).resolve().parents[2] / "resource" / "public" / "editor"
HTML = (PAGE / "index.html").read_text(encoding="utf-8")
CSS = (PAGE / "editor.css").read_text(encoding="utf-8")
JS = (PAGE / "editor.js").read_text(encoding="utf-8")

LINES = ["Seven bodies come out of her garden.", "About eighty seven thousand dollars.",
         "This is how it stayed hidden.", "Sacramento, in the autumn.",
         "She ran a boarding house."]


def _plan(stats=(), title=None, pause=None):
    scenes = [{"narration": line, "prompt": "A picture."} for line in LINES]
    for scene, stat in zip(scenes, stats):
        if stat:
            scene["stat"] = stat
    if pause:
        scenes[2]["pause_after"] = pause
    plan = {"scenes": scenes}
    if title:
        plan["title"] = title
    return plan


def _scenes(**how):
    return scene_plan.stored(scene_plan.parse(json.dumps(_plan(**how))))


def _direction(scenes, spoken, duration=60.0):
    words = ass_subtitles.words_from_cues(
        [(span, line) for span, line in zip(spoken, LINES)])
    return finishing.direct(scenes, words, duration)


# The third line ends at 9.0; the fourth starts at 12.5 or, with no pause, 9.3.
PAUSED = [(0.0, 3.0), (3.4, 6.0), (6.5, 9.0), (12.5, 15.0), (15.5, 18.0)]
STRAIGHT = [(0.0, 3.0), (3.4, 6.0), (6.5, 9.0), (9.3, 12.0), (12.5, 15.0)]


class TestThePlan:
    def test_a_stat_in_either_spelling(self):
        scenes = _scenes(stats=[{"figure": "7 bodies", "caption": "Found in her garden"},
                                "$87,000 | Approximate total"])
        assert scenes[0]["direction"]["stat"] == {
            "figure": "7 bodies", "caption": "Found in her garden"}
        assert scenes[1]["direction"]["stat"] == {
            "figure": "$87,000", "caption": "Approximate total"}

    def test_a_stat_needs_a_figure_and_keeps_to_its_lengths(self):
        with pytest.raises(scene_plan.ScenePlanError, match="scene 1.*no figure"):
            _scenes(stats=[{"caption": "Found in her garden"}])
        with pytest.raises(scene_plan.ScenePlanError, match="figure is 23 characters"):
            _scenes(stats=["eighty seven thousand $"])
        with pytest.raises(scene_plan.ScenePlanError, match="caption is 5. characters"):
            _scenes(stats=["7 | " + "x" * 50])

    def test_the_title_can_be_on_black_and_nothing_else(self):
        scenes = _scenes(title={"text": "Dorothea Puente", "scene": 4, "on": "black"})
        assert scenes[3]["direction"]["title"] == {
            "text": "Dorothea Puente", "tagline": "", "on": "black"}
        assert "on" not in _scenes(
            title={"text": "Dorothea Puente", "on": "picture"})[0]["direction"]["title"]
        with pytest.raises(scene_plan.ScenePlanError, match='"on" is \'white\''):
            _scenes(title={"text": "Dorothea Puente", "on": "white"})

    def test_the_title_scene_cannot_carry_a_stat(self):
        with pytest.raises(scene_plan.ScenePlanError, match="both the title and a stat"):
            _scenes(stats=["7 bodies"], title={"text": "Dorothea Puente", "scene": 1})

    def test_a_direction_file_can_add_them(self):
        directed = scene_plan.redirected(_scenes(), json.dumps({
            "title": {"text": "Dorothea Puente", "scene": 4, "on": "black"},
            "scenes": {"1": {"stat": "7 bodies | Found in her garden"}}}))
        assert directed[0]["direction"]["stat"]["figure"] == "7 bodies"
        assert directed[3]["direction"]["title"]["on"] == "black"

    def test_the_report_and_the_outline_name_them(self):
        plan = scene_plan.parse(json.dumps(_plan(
            stats=["7 bodies", "$87,000"],
            title={"text": "Dorothea Puente", "scene": 4, "on": "black"})))
        said = scene_plan.report(plan)
        assert "title over scene 4 (on black)" in said and "2 stats (scenes 1-2)" in said
        assert "stat: 7 bodies" in scene_plan.outline(scene_plan.stored(plan))


class TestWhenTheyAreShown:
    def test_a_stat_is_up_while_its_line_is_spoken(self):
        found = _direction(_scenes(stats=["7 bodies", "$87,000"]), STRAIGHT)
        first, second = found.stats
        assert (first.start, first.end) == (0.0, 3.25)
        # 2.6 s of speech and a quarter second after it.
        assert (second.start, second.end) == (3.4, 6.25)

    def test_a_short_line_still_gets_time_to_be_read_and_a_long_one_is_cut(self):
        quick = [(0.0, 1.0), (10.0, 19.0), (20.0, 21.0), (22.0, 23.0), (24.0, 25.0)]
        first, second = _direction(_scenes(stats=["7 bodies", "$87,000"]), quick).stats
        assert first.end == pytest.approx(2.2)
        assert second.end == pytest.approx(15.0)

    def test_one_stat_leaves_before_the_next_comes(self):
        close = [(0.0, 1.0), (1.2, 4.0), (5.0, 6.0), (7.0, 8.0), (9.0, 10.0)]
        first, second = _direction(_scenes(stats=["7 bodies", "$87,000"]), close).stats
        assert first.end == pytest.approx(second.start - 0.15)

    def test_a_title_on_black_takes_the_silence_before_its_scene(self):
        title = _direction(_scenes(
            title={"text": "Dorothea Puente", "scene": 4, "on": "black"}, pause=3.5),
            PAUSED).title
        assert title.on_black
        assert (title.start, title.end) == pytest.approx((9.15, 12.4))

    def test_without_a_silence_it_takes_the_start_of_its_scene(self):
        title = _direction(_scenes(
            title={"text": "Dorothea Puente", "scene": 4, "on": "black"}),
            STRAIGHT).title
        assert title.on_black
        assert (title.start, title.end) == pytest.approx((9.15, 12.55))

    def test_the_usual_title_is_where_it_always_was(self):
        title = _direction(_scenes(title={"text": "Dorothea Puente", "scene": 4}),
                           PAUSED).title
        assert not title.on_black
        assert (title.start, title.end) == pytest.approx((12.35, 15.55))

    def test_the_impact_lands_with_the_black_not_with_the_scene(self):
        found = _direction(_scenes(
            title={"text": "Dorothea Puente", "scene": 4, "on": "black"}, pause=3.5),
            PAUSED)
        assert found.hits == pytest.approx([9.15])


class TestTheBurnedInWords:
    STYLE = ass_subtitles.Style(font="Inter", serif_font="Palatino Linotype",
                                card_font="Bahnschrift")

    def _build(self, cues=(), style=None, **more):
        return ass_subtitles.build(list(cues), width=1920, height=1080,
                                   style=style or self.STYLE, **more)

    def test_a_stat_is_a_rule_a_figure_and_a_caption(self):
        text = self._build(stats=[ass_subtitles.Stat(
            5.0, 8.0, "7 bodies", "Found in her garden")])
        rows = [row for row in text.splitlines() if row.startswith("Dialogue")]
        figure = next(row for row in rows if "}7 BODIES" in row)
        caption = next(row for row in rows if "}FOUND IN HER GARDEN" in row)
        rule = next(row for row in rows if "\\1c&H231AD9&" in row)
        # The figure rises 14 units under the rule, 320 above the bottom.
        assert ",Serif," in figure and "\\move(150,788,150,774,0,470)" in figure
        assert "\\pos(150,760)" in rule and "\\t(0,470,\\fscx100)" in rule
        # The caption comes a moment later and leaves with the figure.
        assert ",0:00:05.00,0:00:08.00," in figure
        assert ",0:00:05.17,0:00:08.00," in caption and ",SmallCaps," in caption
        assert "Style: Serif,Palatino Linotype,128," in text

    def test_captions_at_the_bottom_lift_it(self):
        stat = ass_subtitles.Stat(5.0, 8.0, "7 bodies")
        lifted = self._build([((0.0, 2.0), "A caption.")], stats=[stat])
        assert "\\pos(150,590)" in lifted
        top = ass_subtitles.Style(font="Inter", position="top")
        assert "\\pos(150,760)" in self._build(
            [((0.0, 2.0), "A caption.")], style=top, stats=[stat])

    def test_a_stat_too_short_to_read_is_not_drawn(self):
        assert "7 BODIES" not in self._build(
            stats=[ass_subtitles.Stat(5.0, 5.5, "7 bodies")])

    def test_the_timeline_strip_steps_aside_for_a_stat(self):
        event = ass_subtitles.TimelineEvent
        line = ass_subtitles.Timeline(
            start=0.0, end=30.0, first="1929", last="1988",
            events=[event(0.3, "1929", "", 0.0), event(6.0, "1988", "", 1.0)])
        text = self._build(timelines=[line],
                           stats=[ass_subtitles.Stat(2.0, 5.0, "7 bodies")])
        plates = [row for row in text.splitlines() if "\\blur50" in row]
        assert [row.split(",")[1:3] for row in plates] == [
            ["0:00:00.00", "0:00:02.00"], ["0:00:05.00", "0:00:14.00"]]

    def test_a_title_on_black_is_a_black_frame_and_a_serif(self):
        text = self._build(title=ass_subtitles.Title(
            9.15, 12.4, "Dorothea Puente", "The landlady of F Street", on_black=True))
        rows = [row for row in text.splitlines() if row.startswith("Dialogue")]
        black = next(row for row in rows if "\\1a&H00&" in row)
        assert "m 0 0 l 1920 0 1920 1080 0 1080" in black
        names = [row for row in rows if "}DOROTHEA PUENTE" in row]
        # The glow, and the letters over it.
        assert len(names) == 2 and all(",Serif," in row for row in names)
        assert all("\\an5\\pos(960,514)" in row for row in names)
        assert any("}THE LANDLADY OF F STREET" in row for row in rows)
        # Nothing of the usual title: no veil, no heavy sans.
        assert "\\1a&H5C&" not in text and ",Title," not in "\n".join(rows)

    def test_the_usual_title_is_unchanged(self):
        text = self._build(title=ass_subtitles.Title(9.0, 12.0, "Dorothea Puente"))
        assert "\\1a&H5C&" in text and ",Serif," not in text.split("[Events]")[1]


class TestWhatTheEditorIsTold:
    def test_the_stats_the_black_title_and_the_renders_numbers(self, tmp_path):
        srt = tmp_path / "subtitle.srt"
        rows = []
        for number, ((start, end), line) in enumerate(zip(PAUSED, LINES), start=1):
            stamp = lambda t: f"00:00:{int(t):02d},{int(round(t % 1 * 1000)):03d}"  # noqa: E731
            rows.append(f"{number}\n{stamp(start)} --> {stamp(end)}\n{line}\n")
        srt.write_text("\n".join(rows), encoding="utf-8")
        (tmp_path / "script.json").write_text(json.dumps({"params": {
            "scene_plan": _scenes(
                stats=["7 bodies | Found in her garden"], pause=3.5,
                title={"text": "Dorothea Puente", "scene": 4, "on": "black"})}}),
            encoding="utf-8")
        params = VideoParams(video_subject="x", subtitle_enabled=False)
        found = finishing.editor_direction(
            params, str(srt), str(tmp_path / "audio.mp3"), str(tmp_path), 60.0)
        assert found["stats"] == [{
            "start": 0.0, "end": 3.25, "figure": "7 bodies",
            "caption": "Found in her garden"}]
        assert found["title"]["on_black"] is True
        assert (found["title"]["start"], found["title"]["end"]) == (9.15, 12.4)
        assert found["stat"] == {
            "edge": 150, "rule_from_bottom": 320, "in": 0.33, "out": 0.23,
            "rule": 0.47, "caption_delay": 0.17, "rise": 14}

    def test_the_page_has_the_pieces(self):
        for piece in ('id="dir-stat"', 'id="dir-stat-figure"', 'id="dir-stat-caption"'):
            assert piece in HTML
        assert ".dir-stat-figure" in CSS and ".direction.black-title .dir-title-text" in CSS
        assert "function drawDirectionStat(found, at, unitPx)" in JS
        assert 'classList.toggle("black-title", onBlack)' in JS
        assert 'span("stat", "row-a"' in JS
