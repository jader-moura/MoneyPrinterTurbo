"""A score for the cold open, asked for on the plan's title.

* the title takes ``"score": "cold open"``, and nothing else there;
* the score runs from the first frame to where the title lands, on the cuts
  before it, and has the impacts of that stretch to itself;
* a title too early in the video gets its usual impact and no score;
* in the mix the score gives way under the narration, and the music starts
  after the title instead of at the first frame;
* the sound itself: the right length, the same every time, quiet before the
  title and hard on it.
"""

import json

import numpy as np
import pytest

from app.services import cold_open_score, finishing, scene_plan
from app.services.sound_design import SAMPLE_RATE
from app.services.utils import ass_subtitles

LINES = ["Seven bodies come out of her garden.", "About eighty seven thousand dollars.",
         "This is how it stayed hidden.", "Sacramento, in the autumn.",
         "She ran a boarding house."]
# The third line ends at 19.0 and the title scene starts at 23.0.
SPOKEN = [(0.5, 6.0), (6.5, 12.0), (12.5, 19.0), (23.0, 26.0), (26.5, 30.0)]
EARLY = [(0.0, 3.0), (3.4, 6.0), (6.5, 9.0), (12.5, 15.0), (15.5, 18.0)]


def _scenes(title, impact_on_first=False):
    scenes = [{"narration": line, "prompt": "A picture."} for line in LINES]
    scenes[2]["pause_after"] = 3.5
    if impact_on_first:
        scenes[0]["impact"] = "start"
        scenes[4]["impact"] = "start"
    return scene_plan.stored(scene_plan.parse(json.dumps(
        {"scenes": scenes, "title": {"text": "Dorothea Puente", "scene": 4, **title}})))


def _direction(scenes, spoken=SPOKEN, duration=60.0):
    words = ass_subtitles.words_from_cues(
        [(span, line) for span, line in zip(spoken, LINES)])
    return finishing.direct(scenes, words, duration)


class TestThePlan:
    def test_the_title_can_ask_for_a_cold_open_score(self):
        for spelling in ("cold open", "cold-open", "Cold Open", True):
            title = _scenes({"score": spelling})[3]["direction"]["title"]
            assert title["score"] == "cold open"
        assert "score" not in _scenes({})[3]["direction"]["title"]
        assert "score" not in _scenes({"score": False})[3]["direction"]["title"]

    def test_and_nothing_else_there(self):
        with pytest.raises(scene_plan.ScenePlanError, match='"score" is \'piano\''):
            _scenes({"score": "piano"})

    def test_the_report_says_so(self):
        said = scene_plan.report(scene_plan.parse(json.dumps({
            "scenes": [{"narration": line, "prompt": "A picture."} for line in LINES],
            "title": {"text": "Dorothea Puente", "scene": 4, "on": "black",
                      "score": "cold open"}})))
        assert "title over scene 4 (on black), a cold-open score before it" in said


class TestWhereItGoes:
    def test_it_runs_up_to_the_title_on_the_cuts_before_it(self):
        found = _direction(_scenes({"on": "black", "score": "cold open"}))
        landing, cuts = found.score
        assert landing == pytest.approx(found.title.start) == pytest.approx(19.15)
        assert cuts == pytest.approx([0.5, 6.5, 12.5])
        assert found.sounds

    def test_it_has_the_impacts_of_that_stretch_to_itself(self):
        found = _direction(
            _scenes({"on": "black", "score": "cold open"}, impact_on_first=True))
        # Not the title's, not the first scene's; the one after the title stays.
        assert found.hits == pytest.approx([26.4])

    def test_without_a_score_the_title_has_its_impact(self):
        found = _direction(_scenes({"on": "black"}))
        assert found.score is None
        assert found.hits == pytest.approx([19.15])

    def test_a_title_too_early_gets_its_impact_and_no_score(self):
        found = _direction(_scenes({"on": "black", "score": "cold open"}), EARLY)
        assert found.score is None
        assert found.hits == pytest.approx([9.15])

    def test_a_title_over_its_picture_can_have_one_too(self):
        assert _direction(_scenes({"score": "cold open"})).score[0] == pytest.approx(22.9)


class TestTheMix:
    def test_the_score_gives_way_under_the_narration(self):
        graph = finishing.mix_graph(
            duration=60.0, voice_gain=0.0, music_gain=None, score=(1, -4.0, 19.15))
        assert "asplit=2[voice][scorekey]" in graph
        assert "[scoreraw][scorekey]sidechaincompress" in graph
        assert "[voice][score]amix=inputs=2" in graph

    def test_the_music_starts_after_the_title(self):
        graph = finishing.mix_graph(
            duration=60.0, voice_gain=0.0, music_gain=-14.0, score=(2, -4.0, 19.15))
        assert "asplit=3[voice][key][scorekey]" in graph
        assert "adelay=20650:all=1,afade=t=in:st=20.650:d=4.0" in graph
        assert "[voice][score][music]amix=inputs=3" in graph

    def test_without_a_score_the_graph_is_what_it_was(self):
        plain = finishing.mix_graph(duration=60.0, voice_gain=0.0, music_gain=-14.0)
        assert "asplit=2[voice][key]" in plain
        assert "score" not in plain and "adelay" not in plain
        assert "[0:a]" in finishing.mix_graph(
            duration=60.0, voice_gain=0.0, music_gain=None).split("[voice]")[0]


@pytest.fixture(scope="module")
def sound():
    return cold_open_score.score(TestTheSound.TITLE, TestTheSound.CUTS)


class TestTheSound:
    TITLE = 14.0
    CUTS = [3.0, 5.5, 8.2, 11.0]

    def test_it_is_stereo_and_rings_past_the_title(self, sound):
        assert sound.shape == (
            int((self.TITLE + cold_open_score.TAIL_SECONDS) * SAMPLE_RATE), 2)
        assert np.abs(sound).max() < 1.0
        assert np.abs(sound[-100:]).max() < 1e-3

    def test_the_title_lands_hard_after_a_quiet_start(self, sound):
        peak = np.abs(sound).max(axis=1)
        at = int(self.TITLE * SAMPLE_RATE)
        assert peak[at:at + SAMPLE_RATE // 2].max() > 0.6
        assert peak[SAMPLE_RATE:2 * SAMPLE_RATE].max() < 0.2

    def test_the_same_cuts_give_the_same_sound(self, sound):
        assert np.array_equal(sound, cold_open_score.score(self.TITLE, self.CUTS))

    def test_the_chords_and_the_piano_follow_the_cuts(self):
        made = cold_open_score._Score(34.53, [
            4.0, 6.5, 9.767, 12.767, 15.167, 17.333, 19.233, 21.5, 23.233, 25.633,
            26.833, 29.167, 30.867])
        assert made.opening == 4.0
        assert made.changes == [9.767, 19.233, 26.833]
        notes = made.piano_notes()
        assert [at for at, _ in notes][:3] == [4.0, 6.5, 9.767]
        # 25.633 is too close to the chord change at 26.833 and gives way.
        assert 26.833 in [at for at, _ in notes] and 25.633 not in [at for at, _ in notes]
        assert [at for at, *_ in made.boom_times()] == [0.12, 4.0, 17.333, 30.867]

    def test_with_no_cuts_it_still_has_its_shape(self):
        made = cold_open_score._Score(20.0, [])
        assert made.opening == pytest.approx(20.0 / 6)
        assert made.changes == pytest.approx([5.6, 11.2, 15.6])
        assert len(made.boom_times()) >= 3

    def test_a_title_too_early_cannot_have_one(self):
        assert not cold_open_score.possible(11.0) and not cold_open_score.possible(None)
        with pytest.raises(ValueError, match="at least 12 seconds"):
            cold_open_score.score(8.0)
