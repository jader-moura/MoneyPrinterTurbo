"""Title cards made and changed by hand in the editor.

Pinned here:

* a card is kept as the words said when it appears, in the file the finishing
  pass already reads, so the render shows what the editor shows;
* a video whose cards came from its scene plan keeps them when the first card
  is changed by hand;
* a card that would not be shown (overlapping another, no title, too long) is
  refused on the click and says why;
* the page: a card on the Direction track opens a panel, and a narration line
  can be given one.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.services import direction_preview, finishing, title_cards
from app.services import state as sm
from app.services.utils import ass_subtitles

from test.services.test_direction_preview import (  # noqa: F401 - fixtures
    DIRECTED,
    _from_the_subtitles,
    _task,
    forgotten,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")

FILE = "title-cards.json"


@pytest.fixture(autouse=True)
def timed_from_the_subtitles():
    with _from_the_subtitles():
        yield


def _saved(task_root, task_id):
    return json.loads((task_root / task_id / FILE).read_text(encoding="utf-8"))


def _cards(found):
    return [(card["start"], card["title"], card["subtitle"]) for card in found["cards"]]


class TestANewCard:
    def test_it_starts_on_the_word_being_said_and_is_shown(self, task_root):
        task_id = _task(task_root, directions=None)
        found = title_cards.add(task_id, 6.1, "February 1959", "Northern Urals")

        assert found["state"] == "ready"
        assert _cards(found) == [(6.0, "February 1959", "Northern Urals")]

    def test_it_is_kept_as_the_words_it_sits_on(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 6.1, "February 1959")

        (entry,) = _saved(task_root, task_id)
        assert entry["title"] == "February 1959" and entry["subtitle"] == ""
        assert entry["phrase"].startswith("He starts to dig")

    def test_the_render_places_it_where_the_editor_shows_it(self, task_root):
        task_id = _task(task_root, directions=None)
        found = title_cards.add(task_id, 6.1, "February 1959")

        cues = finishing.moviepy_subtitles(str(task_root / task_id / "subtitle.srt"))
        words = ass_subtitles.words_from_cues(cues)
        rendered = finishing.title_cards(
            str(task_root / task_id), None, words, found["duration"])
        assert [(card.start, card.title) for card in rendered] == [(6.0, "February 1959")]

    def test_one_that_would_overlap_another_is_refused_and_says_which(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 2.4, "Igor Dyatlov")
        with pytest.raises(title_cards.CardError, match="overlap the card 'Igor Dyatlov'"):
            title_cards.add(task_id, 6.0, "February 1959")
        assert len(_saved(task_root, task_id)) == 1

    def test_it_needs_a_title_that_fits(self, task_root):
        task_id = _task(task_root, directions=None)
        with pytest.raises(title_cards.CardError, match="needs a title"):
            title_cards.add(task_id, 6.0, "   ")
        with pytest.raises(title_cards.CardError, match="the most is 40"):
            title_cards.add(task_id, 6.0, "x" * 41)
        with pytest.raises(title_cards.CardError, match="the most is 60"):
            title_cards.add(task_id, 6.0, "Fine", "y" * 61)
        assert not (task_root / task_id / FILE).exists()

    def test_after_the_last_word_there_is_nothing_to_tie_it_to(self, task_root):
        task_id = _task(task_root, directions=None)
        with pytest.raises(title_cards.CardError, match="narration has ended"):
            title_cards.add(task_id, 20.0, "Too late")

    def test_a_video_without_a_finish_look_is_told_so(self, task_root):
        task_id = _task(task_root, look="", directions=None)
        with pytest.raises(title_cards.CardError, match="no Finish look"):
            title_cards.add(task_id, 6.0, "February 1959")


class TestACardFromTheScenePlan:
    def test_changing_it_keeps_it_and_the_rest_of_the_direction(self, task_root):
        task_id = _task(task_root)
        before = direction_preview.describe(task_id)
        (start, _, _), = _cards(before)

        found = title_cards.change(task_id, start, title="The leader", subtitle="")

        assert _cards(found) == [(start, "The leader", "")]
        assert found["title"] == before["title"] and found["hits"] == before["hits"]
        (entry,) = _saved(task_root, task_id)
        assert entry["phrase"].startswith("led by Igor Dyatlov")

    def test_adding_one_keeps_the_plan_s_own(self, task_root):
        directions = [DIRECTED[0], DIRECTED[1], None, None]
        task_id = _task(task_root, directions=directions)
        found = title_cards.add(task_id, 9.6, "The find")

        assert [card["title"] for card in found["cards"]] == ["Igor Dyatlov", "The find"]
        assert len(_saved(task_root, task_id)) == 2

    def test_deleting_it_does_not_bring_it_back_from_the_plan(self, task_root):
        task_id = _task(task_root)
        (start, _, _), = _cards(direction_preview.describe(task_id))
        found = title_cards.remove(task_id, start)

        assert found["cards"] == [] and found["title"]
        assert _saved(task_root, task_id) == []
        assert direction_preview.describe(task_id)["cards"] == []


class TestChangingACard:
    def test_only_what_is_given_changes(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 6.0, "February 1959", "Northern Urals")
        found = title_cards.change(task_id, 6.0, subtitle="Kholat Syakhl")
        assert _cards(found) == [(6.0, "February 1959", "Kholat Syakhl")]

    def test_it_can_be_moved_onto_other_words(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 6.0, "February 1959")
        found = title_cards.change(task_id, 6.0, time=0.0)

        assert _cards(found) == [(0.0, "February 1959", "")]
        assert _saved(task_root, task_id)[0]["phrase"].startswith("The tent was still")

    def test_moving_it_a_little_is_not_overlapping_itself(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 2.4, "Igor Dyatlov")
        found = title_cards.change(task_id, 2.4, time=6.0)
        assert _cards(found) == [(6.0, "Igor Dyatlov", "")]

    def test_it_is_named_by_when_it_starts(self, task_root):
        task_id = _task(task_root, directions=None)
        title_cards.add(task_id, 6.0, "February 1959")
        with pytest.raises(title_cards.CardError, match="no title card at 0:15"):
            title_cards.change(task_id, 15.0, title="Nobody")
        with pytest.raises(title_cards.CardError, match="no title card at 0:15"):
            title_cards.remove(task_id, 15.0)

    def test_a_card_whose_words_are_gone_stays_in_the_file_untouched(self, task_root):
        task_id = _task(task_root, directions=None)
        lost = {"phrase": "words nobody says", "title": "Lost", "subtitle": ""}
        (task_root / task_id / FILE).write_text(json.dumps([lost]), encoding="utf-8")
        title_cards.add(task_id, 6.0, "February 1959")
        title_cards.remove(task_id, 6.0)
        assert _saved(task_root, task_id) == [lost]


class TestThePhrase:
    def _video(self, said):
        video = object.__new__(title_cards._Video)
        video.words = [ass_subtitles.Word(float(at), at + 0.9, word)
                       for at, word in enumerate(said.split())]
        return video

    def test_it_grows_until_it_is_found_nowhere_earlier(self):
        video = self._video("he went up the hill and then he went up the hill again today")
        phrase = video.phrase_at(7)
        assert phrase == "he went up the hill again"
        assert ass_subtitles.find_phrase(video.words, phrase) == 7.0

    def test_words_said_the_same_way_earlier_cannot_hold_a_card(self):
        video = self._video("no no no no no no no no no no no no no no no no no no no no")
        with pytest.raises(title_cards.CardError, match="said the same way earlier"):
            video.phrase_at(3)


class TestTheRoutes:
    def test_add_change_and_delete(self, task_root):
        task_id = _task(task_root, directions=None)
        client = TestClient(asgi.app)
        cards = f"/api/v1/tasks/{task_id}/direction/cards"
        # The task is known from its files on disk; nothing is running.
        with patch.object(sm.state, "get_task", return_value=None):
            added = client.post(cards, json={"time": 6.1, "title": "February 1959"})
            assert added.status_code == 200
            assert added.json()["data"]["cards"][0]["start"] == 6.0

            changed = client.put(cards, json={"start": 6.0, "title": "1959"})
            assert changed.json()["data"]["cards"][0]["title"] == "1959"

            refused = client.post(cards, json={"time": 6.1, "title": ""})
            assert refused.status_code >= 400 and "needs a title" in refused.text

            gone = client.delete(cards, params={"start": 6.0})
            assert gone.status_code == 200 and not gone.json()["data"].get("cards")


class TestThePage:
    def test_a_card_on_the_track_opens_its_panel(self):
        track = JS.split("function drawDirectionTrack() {")[1].split("\n}\n")[0]
        assert "() => openCard(card)" in track
        panel = JS.split("function drawInspector() {")[1].split("const scene =")[0]
        assert "if (state.card) { drawCardPanel(state.card); return; }" in panel

    def test_the_panel_can_reword_move_and_delete(self):
        panel = JS.split("function drawCardPanel(card) {")[1].split("\n}\n")[0]
        for label in ('"Title"', '"Put it at the playhead"', '"Delete the card"',
                      '"Add the card"'):
            assert label in panel
        save = JS.split("async function saveCard(card) {")[1].split("\n}\n")[0]
        assert 'fresh ? "POST" : "PUT"' in save and "showDirection(found);" in save

    def test_a_narration_line_can_be_given_a_card(self):
        panel = JS.split("function drawLinePanel(line) {")[1].split("\n}\n")[0]
        assert '"Add a title card here"' in panel and "openCard(null," in panel

    def test_clicking_a_picture_or_a_line_closes_the_card(self):
        drag = JS.split("function startDrag(event) {")[1].split("drawInspector();")[0]
        assert "state.card = null;" in drag
        click = JS.split('bar.addEventListener("pointerdown"')[1].split("});")[0]
        assert "state.card = null;" in click
