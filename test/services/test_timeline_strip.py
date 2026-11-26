"""The timeline strip: dates that stay on screen while a story moves through them.

What is pinned:

* a scene's ``timeline`` is a date with a year in figures and a short label,
  or ``"end"``; anything else is refused by scene number;
* a date becomes a place on an axis: a month, a day, "late" or a season move
  it inside its year, a decade sits in its middle;
* the strip opens just before its first date is spoken, stays through the
  undated scenes after it, and leaves after the scene marked ``"end"`` (or
  after its last date); dates after an ``"end"`` make a new strip;
* the finished video's subtitle file draws it, gives way to the title, and
  moves to the top only when captions have the bottom;
* the editor is handed the same strips and the same numbers to draw from.
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

LINES = ["She was born in nineteen twenty nine.", "Both parents drank.",
         "Her father died in nineteen thirty seven.", "She married at sixteen.",
         "Years later she was arrested."]
SPOKEN = [((0.0, 3.0), LINES[0]), ((3.0, 5.0), LINES[1]), ((5.0, 8.0), LINES[2]),
          ((8.0, 10.0), LINES[3]), ((20.0, 23.0), LINES[4])]


def _plan(*timelines):
    return {"scenes": [
        {"narration": line, "prompt": "A picture.",
         **({"timeline": timeline} if timeline else {})}
        for line, timeline in zip(LINES, timelines)]}


def _scenes(*timelines):
    return scene_plan.stored(scene_plan.parse(json.dumps(_plan(*timelines))))


class TestThePlan:
    def test_a_date_and_a_label_in_either_spelling(self):
        scenes = _scenes({"date": "Jan 9, 1929", "label": "Born in Redlands"},
                         None, "1937 | Her father dies", None, "end")
        assert scenes[0]["direction"]["timeline"] == {
            "date": "Jan 9, 1929", "label": "Born in Redlands"}
        assert scenes[2]["direction"]["timeline"] == {
            "date": "1937", "label": "Her father dies"}
        assert scenes[4]["direction"]["timeline"] == {"end": True}
        assert "direction" not in scenes[1]

    def test_the_last_date_can_also_end_the_strip(self):
        scenes = _scenes("1929", None, {"date": "1937", "end": True}, None, None)
        assert scenes[2]["direction"]["timeline"] == {
            "date": "1937", "label": "", "end": True}

    @pytest.mark.parametrize("wrong, said", [
        ("Her childhood", "needs a year in figures"),
        ({"label": "Born"}, "needs a year in figures"),
        ("The ninth of January 1929", "the most is 14"),
        (7, "timeline is an object"),
    ])
    def test_what_is_wrong_is_said_by_scene(self, wrong, said):
        with pytest.raises(scene_plan.ScenePlanError, match="scene 3") as error:
            scene_plan.parse(json.dumps(_plan("1929", None, wrong, None, None)))
        assert said in str(error.value)

    def test_a_long_label_is_cut_not_refused(self):
        scenes = _scenes({"date": "1929", "label": "x" * 80}, None, "1937", None, None)
        assert len(scenes[0]["direction"]["timeline"]["label"]) == 36

    @pytest.mark.parametrize("date, year", [
        ("1978", 1978.0), ("Dec 21, 1978", 1978.97), ("November 1988", 1988.875),
        ("Late 1960s", 1968.5), ("1970s", 1975.0), ("Fall 1986", 1986.8),
        ("early 1982", 1982.15), ("May 1982", 1982.375),
    ])
    def test_a_date_is_a_place_in_time(self, date, year):
        assert scene_plan.timeline_when(date) == pytest.approx(year, abs=0.01)

    def test_no_year_is_no_place(self):
        assert scene_plan.timeline_when("That spring") is None
        assert scene_plan.timeline_when("") is None

    def test_a_video_that_exists_can_be_given_one(self):
        new = scene_plan.redirected(_scenes(None, None, None, None, None), json.dumps({
            "scenes": {"1": {"timeline": "1929 | Born"}, "3": {"timeline": "1937"},
                       "4": {"timeline": "end"}}}))
        assert new[0]["direction"] == {"timeline": {"date": "1929", "label": "Born"}}
        assert new[3]["direction"] == {"timeline": {"end": True}}
        assert "timeline: 1929" in scene_plan.outline(new)
        assert "2 timeline dates (scenes 1 to 3)" in scene_plan.report({"scenes": [
            {**scene, "prompt": "x"} for scene in new]})

    def test_the_checker_remarks_on_a_strip_with_nothing_to_move_to(self):
        notes = scene_plan.direction_notes(_scenes("1929", None, None, None, None))
        assert any("one date" in note for note in notes)
        notes = scene_plan.direction_notes(_scenes("end", None, "1929", None, "1937"))
        assert any("scene 1 ends a timeline" in note for note in notes)
        assert not any("one date" in note for note in notes)


class TestWhenItShows:
    def _direct(self, *timelines, duration=120.0):
        words = ass_subtitles.words_from_cues(SPOKEN)
        return finishing.direct(_scenes(*timelines), words, duration)

    def test_it_stays_from_the_first_date_to_the_scene_marked_end(self):
        found = self._direct("1929 | Born", None, "1937 | Her father dies", "end", None)
        assert found.planned and len(found.timelines) == 1
        line = found.timelines[0]
        assert line.start == pytest.approx(0.0) and line.end == pytest.approx(10.4)
        assert [(event.at, event.date, event.place) for event in line.events] == [
            (0.0, "1929", 0.0), (5.0, "1937", 1.0)]
        assert (line.first, line.last) == ("1929", "1937")

    def test_without_an_end_it_leaves_after_its_last_date(self):
        line = self._direct(None, None, "1937", None, "1945").timelines[0]
        assert line.start == pytest.approx(4.7) and line.end == pytest.approx(24.0)

    def test_dates_after_an_end_are_a_strip_of_their_own(self):
        found = self._direct("1929", {"date": "1937", "end": True}, None, "1945", "1960")
        assert [(line.first, line.last) for line in found.timelines] == [
            ("1929", "1937"), ("1945", "1960")]

    def test_a_date_sits_where_it_falls_between_the_ends(self):
        line = self._direct("1929", None, "1937", "1945", "1969").timelines[0]
        assert [event.place for event in line.events] == pytest.approx([0, 0.2, 0.4, 1.0])

    def test_a_scene_no_longer_spoken_drops_only_its_own_date(self):
        scenes = _scenes("1929", None, "1937", None, "1945")
        scenes[2]["narration"] = "This line was rewritten."
        found = finishing.direct(scenes, ass_subtitles.words_from_cues(SPOKEN), 120.0)
        assert [event.date for event in found.timelines[0].events] == ["1929", "1945"]


class TestTheBurnedInStrip:
    LINE = ass_subtitles.Timeline(10.0, 40.0, [
        ass_subtitles.TimelineEvent(10.3, "1929", "Born in Redlands", 0.0),
        ass_subtitles.TimelineEvent(20.0, "Nov 12, 1988", "Second day of digging", 0.75),
    ], "1929", "2011")

    def _build(self, cues=(), style=None, **more):
        return ass_subtitles.build(
            list(cues), width=1920, height=1080,
            style=style or ass_subtitles.Style(font="Inter"), timelines=[self.LINE],
            **more)

    def test_the_axis_the_marker_and_the_words_are_drawn(self):
        text = self._build()
        # The axis runs between the two edges, 112 above the bottom.
        assert "m 150 966 l 1770 966 1770 970 150 970" in text
        assert "}1929" in text and "}2011" in text
        assert "}NOV 12, 1988" in text and "}Second day of digging" in text
        # The first event is simply there; the second slides in from the first.
        assert "\\pos(138,956)" in text
        assert "\\move(138,956,1353,956,0,600)" in text
        assert "\\fscx0.10\\t(0,600,\\fscx75.00)" in text

    def test_past_the_middle_the_words_hang_left_of_the_marker(self):
        text = self._build()
        assert "\\an1\\pos(138,946)" in text          # 1929, reading rightwards
        assert "\\an3\\move(162,946,1377,946,0,600)" in text  # 1988, reading leftwards

    def test_the_strip_leaves_eight_seconds_after_the_last_change(self):
        events = [line for line in self._build().splitlines() if "Dialogue" in line]
        label = next(line for line in events if "Second day of digging" in line)
        date = next(line for line in events if "NOV 12, 1988" in line)
        plates = [line for line in events if "\\blur50" in line]
        assert ",0:00:20.00,0:00:28.00," in label
        assert ",0:00:20.00,0:00:28.00," in date
        # Two events ten seconds apart: too short a gap to go away in.
        assert [line.split(",")[1:3] for line in plates] == [["0:00:10.00", "0:00:28.00"]]

    def test_between_events_far_apart_it_is_away(self):
        event = ass_subtitles.TimelineEvent
        line = ass_subtitles.Timeline(
            start=9.7, end=120.0, first="1929", last="2011",
            events=[event(10.0, "1929", "Born", 0.0), event(60.0, "1988", "", 0.75),
                    event(65.0, "2011", "", 1.0)])
        # Up for eight seconds, back 0.6 s before the marker moves again.
        assert ass_subtitles.timeline_rests([line]) == [(17.7, 59.4), (73.0, 120.0)]
        text = ass_subtitles.build([], width=1920, height=1080,
                                   style=ass_subtitles.Style(font="Inter"), timelines=[line])
        plates = [row for row in text.splitlines() if "\\blur50" in row]
        assert [row.split(",")[1:3] for row in plates] == [
            ["0:00:09.70", "0:00:17.70"], ["0:00:59.40", "0:01:13.00"]]
        # It comes back on the old date, then the marker slides to the new one.
        old = [row for row in text.splitlines() if "}1929" in row and "CardTitle" in row]
        assert [row.split(",")[1:3] for row in old] == [
            ["0:00:09.70", "0:00:17.70"], ["0:00:59.40", "0:01:00.00"]]

    def test_it_gives_way_to_the_title(self):
        text = self._build(title=ass_subtitles.Title(14.0, 18.0, "Dorothea Puente"))
        plates = [line for line in text.splitlines() if "\\blur50" in line]
        assert [line.split(",")[1:3] for line in plates] == [
            ["0:00:10.00", "0:00:14.00"], ["0:00:18.00", "0:00:28.00"]]

    def test_captions_at_the_bottom_send_it_to_the_top(self):
        cues = [((0.0, 2.0), "A caption.")]
        assert "m 150 116 l 1770 116" in self._build(cues)
        top = ass_subtitles.Style(font="Inter", position="top")
        assert "m 150 966 l 1770 966" in self._build(cues, style=top)

    def test_at_the_top_it_also_gives_way_to_cards(self):
        style = ass_subtitles.Style(font="Inter")
        card = ass_subtitles.TitleCard(12.0, 18.0, "Sacramento")
        assert ass_subtitles.timeline_holes(None, [card], top=True) == [(12.0, 18.0)]
        assert ass_subtitles.timeline_holes(None, [card], top=False) == []
        assert ass_subtitles.timeline_on_top(style, captions=False) is False

    def test_nothing_is_drawn_without_one(self):
        plain = ass_subtitles.build([], width=1920, height=1080,
                                    style=ass_subtitles.Style(font="Inter"))
        assert "Dialogue" not in plain


class TestTheEditor:
    def test_the_strip_is_in_the_frame_and_drawn_from_the_direction(self):
        frame = HTML.split('<div id="direction"')[1].split('id="yt"')[0]
        for name in ("dir-timeline", "dir-tl-ticks", "dir-tl-fill", "dir-tl-marker",
                     "dir-tl-first", "dir-tl-last", "dir-tl-date", "dir-tl-label"):
            assert f'id="{name}"' in frame
        assert "function drawDirectionTimeline(found, at, unitPx)" in JS
        assert "drawDirectionTimeline(found, at, unitPx);" in JS
        for name in (".dir-tl-axis", ".dir-tl-fill", ".dir-tl-marker", ".dir-run"):
            assert f"\n{name} {{" in CSS

    def test_a_video_with_only_a_timeline_still_shows_its_overlay(self):
        body = JS.split("function drawDirection() {")[1].split("\nfunction ")[0]
        assert "!title && !cards.length && !lines.length" in body

    def test_the_track_shows_the_run_and_its_dates(self):
        body = JS.split("function drawDirectionTrack() {")[1].split("\nfunction ")[0]
        assert 'run.className = "dir-run";' in body
        assert 'date.className = "dir-run-date";' in body
        # A mark is put on the track before it is handed back.
        assert body.index("lane.appendChild(mark);") < body.index("return mark;")


class TestWhatTheEditorIsTold:
    def test_the_strips_and_the_renders_numbers(self, tmp_path):
        srt = tmp_path / "subtitle.srt"
        rows = []
        for number, ((start, end), text) in enumerate(SPOKEN, start=1):
            stamp = lambda t: f"00:00:{int(t):02d},{int(round(t % 1 * 1000)):03d}"  # noqa: E731
            rows.append(f"{number}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
        srt.write_text("\n".join(rows), encoding="utf-8")
        (tmp_path / "script.json").write_text(json.dumps({"params": {
            "scene_plan": _scenes("1929 | Born", None, "1937", "end", None)}}),
            encoding="utf-8")
        params = VideoParams(video_subject="x", subtitle_enabled=False)
        found = finishing.editor_direction(
            params, str(srt), str(tmp_path / "audio.mp3"), str(tmp_path), 60.0)
        assert found["timelines"] == [{
            "start": 0.0, "end": 10.4, "first": "1929", "last": "1937",
            "events": [
                {"at": 0.0, "date": "1929", "label": "Born", "place": 0.0},
                {"at": 5.0, "date": "1937", "label": "", "place": 1.0},
            ]}]
        assert found["timeline"] == {
            "top": False, "holes": [], "edge": 150, "from_edge": 112,
            "move": 0.6, "label_seconds": 8.0, "fade": 0.5}
