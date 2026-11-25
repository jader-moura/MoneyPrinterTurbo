"""One narration line recorded again and put back where the old take was.

In the user's words: "at the moment we can only regenerate the images, would
be good if we could select a specific narration item and ask to regenerate it
(using the right sidebar)".

What is pinned here:

* only that line changes: the lines before it are where they were, the lines
  after it are whole and moved by exactly the difference in length;
* the subtitle times, the word times, the picture on screen and the length of
  the narration move with the sound;
* the line is spoken as the script has it, punctuation included;
* a take that fails or is silent leaves the narration untouched.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.services import finishing, narration_pauses, narration_retake, scene_editor
from app.services import scene_manifest, scene_video, task_artifacts

from test.services.test_narration_pauses import (  # noqa: F401 - fixtures
    LINES,
    RATE,
    _cue_starts,
    _lengths,
    _loud,
    _task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")


def _recorder(seconds, *, level=9000, said=None, silence=0.5):
    """A voice that says every line as a tone ``seconds`` long, with some
    silence before and after it the way a real one does."""
    def record(text, target):
        if said is not None:
            said.append(text)
        samples = np.zeros((int((seconds + 2 * silence) * RATE), 1), dtype=np.int16)
        first = int(silence * RATE)
        time = np.arange(int(seconds * RATE)) / RATE
        samples[first:first + len(time), 0] = (
            level * np.sin(2 * np.pi * 330 * time)).astype(np.int16)
        narration_pauses._encode(samples, RATE, 128, target)
        return True
    return record


@pytest.fixture
def video(task_root, monkeypatch):  # noqa: F811
    # The take is not listened to in these tests: its words are estimated.
    def deaf(path):
        raise finishing.FinishError("no whisper here")

    monkeypatch.setattr(finishing, "_spoken_words", deaf)
    task_id = _task(task_root)
    return task_root, task_id


def _near(spans, wanted, by=0.11):
    assert len(spans) == len(wanted), spans
    for (start, end), (want_start, want_end) in zip(spans, wanted):
        assert abs(start - want_start) <= by and abs(end - want_end) <= by, (spans, wanted)


class TestTheSound:
    def test_a_longer_take_pushes_everything_after_it_later(self, video):
        root, task_id = video
        made = narration_retake.retake(task_id, 1, _recorder(3.0))

        # The line was 1.6s of speech and is 3.0s now.
        assert made["index"] == 1 and abs(made["added"] - 1.4) < 0.1
        by = made["added"]
        _near(_loud(root / task_id / "audio.mp3"), [
            (0.0, 2.0), (2.4, 5.4), (6.0 + by, 7.0 + by), (9.0 + by, 12.0 + by)])
        assert abs(made["duration"] - (13.0 + by)) < 0.1

    def test_a_shorter_take_pulls_everything_after_it_earlier(self, video):
        root, task_id = video
        made = narration_retake.retake(task_id, 1, _recorder(0.8))

        by = made["added"]
        assert abs(by + 0.8) < 0.1
        _near(_loud(root / task_id / "audio.mp3"), [
            (0.0, 2.0), (2.4, 3.2), (6.0 + by, 7.0 + by), (9.0 + by, 12.0 + by)])

    def test_the_first_and_the_last_line_can_be_recorded_again(self, video):
        root, task_id = video
        first = narration_retake.retake(task_id, 0, _recorder(1.0))
        last = narration_retake.retake(task_id, 3, _recorder(1.0))

        a, b = first["added"], last["added"]
        assert abs(a + 1.0) < 0.15 and abs(b + 2.0) < 0.15
        _near(_loud(root / task_id / "audio.mp3"), [
            (0.0, 1.0), (2.4 + a, 4.0 + a), (6.0 + a, 7.0 + a), (9.0 + a, 10.0 + a)], by=0.16)

    def test_a_quiet_take_is_brought_to_the_level_of_the_lines_around_it(self, video):
        root, task_id = video
        narration_retake.retake(task_id, 1, _recorder(1.6, level=2500))
        samples = narration_pauses._decode(str(root / task_id / "audio.mp3"), RATE, 1)[:, 0]
        line = np.abs(samples[int(2.8 * RATE):int(3.6 * RATE)].astype(np.int32)).max()
        assert line > 4500

    def test_the_cut_is_after_the_last_sound_of_a_word_not_inside_it(self):
        # "checks": the word, a hush of 60ms, its last sound, then the real
        # pause. A cut in the hush would leave the last sound in the recording.
        def tone(seconds):
            t = np.arange(int(seconds * RATE)) / RATE
            return (9000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)

        def hush(seconds):
            return np.zeros(int(seconds * RATE), np.int16)

        samples = np.concatenate(
            [tone(1.0), hush(0.06), tone(0.1), hush(0.3), tone(1.0)])[:, None]
        # The word was heard from 0.6s to 1.0s, the next from 1.0s to 1.9s.
        cut = narration_retake.gap(samples, RATE, 0.8, 1.45) / RATE
        assert 1.18 < cut < 1.44

    def test_lines_that_run_together_are_cut_at_the_quietest_instant(self):
        t = np.arange(2 * RATE) / RATE
        samples = (9000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)[:, None]
        assert 0.9 <= narration_retake.gap(samples, RATE, 0.9, 1.1) / RATE <= 1.1

    def test_the_take_itself_is_not_left_in_the_folder(self, video):
        root, task_id = video
        narration_retake.retake(task_id, 1, _recorder(1.0))
        assert not [p.name for p in (root / task_id).iterdir() if "take" in p.name]


class TestWhatFollowsTheVoice:
    def test_the_subtitles_move_with_it(self, video):
        root, task_id = video
        by = narration_retake.retake(task_id, 1, _recorder(3.0))["added"]

        starts = _cue_starts(root, task_id)
        assert starts[:2] == [0.0, 2.4]
        assert abs(starts[2] - (6.0 + by)) < 0.01 and abs(starts[3] - (9.0 + by)) < 0.01
        cues = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        assert abs(cues[1][0][1] - (4.0 + by)) < 0.01
        # Not a word of the captions changes.
        assert [cue[1] for cue in cues] == [line.rstrip(".") for line in LINES]

    def test_the_picture_on_screen_lasts_as_much_longer(self, video):
        _, task_id = video
        before = _lengths(task_id)
        by = narration_retake.retake(task_id, 1, _recorder(3.0))["added"]

        after = _lengths(task_id)
        assert abs(after[1] - (before[1] + by)) < 0.01
        assert [after[0], after[2], after[3]] == [before[0], before[2], before[3]]

    def test_the_narration_is_as_much_longer_in_the_manifest(self, video):
        _, task_id = video
        by = narration_retake.retake(task_id, 1, _recorder(3.0))["added"]
        assert scene_manifest.load_raw(task_id)["audio_duration"] in (
            int(np.ceil(13.0 + by)), int(np.ceil(13.0 + by)) + 1)

    def test_the_word_times_are_kept_and_moved_not_listened_for_again(self, video):
        root, task_id = video
        directory = root / task_id
        before = json.loads((directory / finishing._SPOKEN_WORDS_FILE).read_text("utf-8"))
        by = narration_retake.retake(task_id, 1, _recorder(3.0))["added"]

        saved = finishing._saved_spoken_words(str(directory / "audio.mp3"), str(directory))
        assert saved is not None, "the saved word times no longer belong to this audio"
        assert [word[2] for word in saved] == [word[2] for word in before["words"]]
        was = {word[2]: word for word in before["words"]}
        now = {word[2]: word for word in saved}
        assert now["tent"][0] == was["tent"][0]
        assert abs(now["dig"][0] - (was["dig"][0] + by)) < 0.01
        # The line's own words sit inside the new take.
        assert 2.3 < now["Igor"][0] < 5.5

    def test_a_take_that_was_listened_to_uses_what_was_heard(self, video, monkeypatch):
        root, task_id = video
        monkeypatch.setattr(finishing, "_spoken_words", lambda path: [
            (0.5, 0.9, "Alpha"), (0.9, 1.4, "Beta"), (1.4, 3.5, "Gamma")])
        narration_retake.retake(task_id, 1, _recorder(3.0))

        directory = root / task_id
        saved = finishing._saved_spoken_words(str(directory / "audio.mp3"), str(directory))
        heard = [word for word in saved if word[2] in ("Alpha", "Beta", "Gamma")]
        # 0.5s into the take's file is where its speech starts: the line's start.
        assert abs(heard[0][0] - 2.4) < 0.15 and abs(heard[2][1] - 5.4) < 0.15


class TestWhatIsSaid:
    def test_the_line_is_spoken_as_the_script_has_it(self, video):
        _, task_id = video
        said = []
        made = narration_retake.retake(task_id, 1, _recorder(1.0, said=said))
        assert said == ["It was led by Igor Dyatlov."] and made["text"] == said[0]

    def test_punctuation_comes_from_the_script(self):
        script = "Would you pay? In May of 1925, in a hotel room in Paris, they sat."
        lines = ["Would you pay", "In May of 1925", "in a hotel room in Paris", "they sat"]
        assert narration_retake.as_written(script, lines, 0) == "Would you pay?"
        assert narration_retake.as_written(script, lines, 2) == "in a hotel room in Paris,"

    def test_pause_marks_are_never_read_out(self):
        script = "He stops. [pause: 2s] He starts to dig."
        assert narration_retake.as_written(
            script, ["He stops", "He starts to dig"], 1) == "He starts to dig."

    def test_a_repeated_line_finds_its_own_place(self):
        script = "Run! They said. Run."
        assert narration_retake.as_written(script, ["Run", "They said", "Run"], 2) == "Run."

    def test_a_line_the_script_no_longer_has_is_spoken_as_the_caption_has_it(self):
        assert narration_retake.as_written(
            "Something else entirely.", ["The tent was standing"], 0) == "The tent was standing"


class TestWhenItCannotBeDone:
    def _untouched(self, root, task_id, attempt, match):
        audio = root / task_id / "audio.mp3"
        before = audio.read_bytes()
        subtitle = (root / task_id / "subtitle.srt").read_bytes()
        lengths = _lengths(task_id)
        with pytest.raises(scene_editor.SceneError, match=match):
            attempt()
        assert audio.read_bytes() == before
        assert (root / task_id / "subtitle.srt").read_bytes() == subtitle
        assert _lengths(task_id) == lengths

    def test_a_voice_that_fails_leaves_the_narration_as_it_was(self, video):
        root, task_id = video
        self._untouched(root, task_id, lambda: narration_retake.retake(
            task_id, 1, lambda text, target: False), "did not record")

    def test_a_silent_take_leaves_the_narration_as_it_was(self, video):
        root, task_id = video
        self._untouched(root, task_id, lambda: narration_retake.retake(
            task_id, 1, _recorder(1.0, level=0)), "silence")

    def test_a_line_that_is_not_there(self, video):
        root, task_id = video
        self._untouched(root, task_id, lambda: narration_retake.retake(
            task_id, 9, _recorder(1.0)), "no narration line 10")

    def test_a_script_changed_since_the_recording(self, video):
        root, task_id = video
        task_artifacts.patch_script_data(task_id, audio_stale=True)
        self._untouched(root, task_id, lambda: narration_retake.retake(
            task_id, 1, _recorder(1.0)), "re-record the whole narration")

    def test_not_while_video_scenes_have_the_card(self, video, monkeypatch):
        root, task_id = video

        def busy():
            raise scene_editor.SceneBusyError("video scenes are being made")

        monkeypatch.setattr(scene_video, "refuse_while_running", busy)
        self._untouched(root, task_id, lambda: narration_retake.retake(
            task_id, 1, _recorder(1.0)), "video scenes")

    def test_one_line_at_a_time(self, video):
        _, task_id = video
        assert narration_retake._ONE_AT_A_TIME.acquire(blocking=False)
        try:
            with pytest.raises(scene_editor.SceneBusyError, match="another narration line"):
                narration_retake.retake(task_id, 1, _recorder(1.0))
        finally:
            narration_retake._ONE_AT_A_TIME.release()


class TestThePage:
    def test_clicking_a_line_puts_it_in_the_right_hand_panel(self):
        click = JS.split('bar.addEventListener("pointerdown"')[1].split("});")[0]
        assert "state.line = index;" in click and "drawInspector();" in click
        panel = JS.split("function drawInspector() {")[1].split("const scene =")[0]
        assert "if (line) { drawLinePanel(line); return; }" in panel

    def test_the_panel_can_play_the_line_and_record_it_again(self):
        panel = JS.split("function drawLinePanel(line) {")[1].split("\n}\n")[0]
        assert '"Play this line"' in panel and '"Record this line again"' in panel
        assert "/narration/${line.index}/retake" in JS

    def test_the_new_take_is_what_plays_afterwards(self):
        # The retake is queued, so the new sound is fetched when its job ends.
        watch = JS.split("async function watchQueue() {")[1].split("\n}\n")[0]
        assert 'job.kind === "narration"' in watch
        assert '"take=" + Date.now()' in watch and "ui.narration.src =" in watch

    def test_the_retake_is_queued_and_the_queue_names_the_line(self):
        retake = JS.split("async function retakeLine(line) {")[1].split("\n}\n")[0]
        assert "watchQueue();" in retake and "run(" not in retake
        queue = JS.split("function drawQueue() {")[1].split("\nfunction ")[0]
        assert "`Narration line ${job.line + 1}`" in queue
        assert "waiting to be recorded" in queue

    def test_clicking_a_picture_takes_the_panel_back(self):
        drag = JS.split("function startDrag(event) {")[1].split("drawInspector();")[0]
        assert "state.line = -1;" in drag


class TestTheLineAsRecorded:
    def test_it_says_what_the_voice_is_given_and_when_it_is_heard(self, video):
        _, task_id = video
        told = narration_retake.line(task_id, 2)
        assert told == {"index": 2, "text": "He starts to dig.",
                        "start": 6.0, "end": 7.0, "heard": True}

    def test_heard_times_win_over_the_caption_times(self, video):
        root, task_id = video
        directory = root / task_id
        saved = json.loads((directory / finishing._SPOKEN_WORDS_FILE).read_text("utf-8"))
        saved["words"] = [[start + 0.7, end + 0.7, word] for start, end, word in saved["words"]]
        (directory / finishing._SPOKEN_WORDS_FILE).write_text(json.dumps(saved), "utf-8")
        told = narration_retake.line(task_id, 2)
        assert (told["start"], told["end"]) == (6.7, 7.7)

    def test_a_narration_nobody_listened_to_gives_the_caption_times(self, video):
        root, task_id = video
        (root / task_id / finishing._SPOKEN_WORDS_FILE).unlink()
        told = narration_retake.line(task_id, 2)
        assert (told["start"], told["end"], told["heard"]) == (6.0, 7.0, False)

    def test_the_page_plays_the_line_where_it_was_heard(self):
        play = JS.split("async function playLine(line) {")[1].split("\n}\n")[0]
        assert "const heard = await heardLine(line);" in play
        assert "/narration/${line.index}`" in JS


class TestTakingALineOut:
    def test_the_line_is_gone_from_the_sound_and_the_rest_comes_earlier(self, video):
        root, task_id = video
        made = narration_retake.remove(task_id, 1)

        # Cut in the middle of the pause before it and of the pause after it.
        assert abs(made["removed"] - 2.8) < 0.15
        by = made["removed"]
        _near(_loud(root / task_id / "audio.mp3"), [
            (0.0, 2.0), (6.0 - by, 7.0 - by), (9.0 - by, 12.0 - by)], by=0.16)
        assert abs(made["duration"] - (13.0 - by)) < 0.1
        assert scene_manifest.load_raw(task_id)["audio_duration"] == 11

    def test_its_caption_is_gone_and_the_later_ones_move(self, video):
        root, task_id = video
        before = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        made = narration_retake.remove(task_id, 1)
        after = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))

        assert [cue[1] for cue in after] == [before[0][1], before[2][1], before[3][1]]
        assert after[0][0] == before[0][0]
        assert abs(after[1][0][0] - (before[2][0][0] - made["removed"])) < 0.01
        assert abs(after[2][0][1] - (before[3][0][1] - made["removed"])) < 0.01

    def test_its_words_are_gone_and_the_stamp_matches_the_new_sound(self, video):
        root, task_id = video
        words = root / task_id / finishing._SPOKEN_WORDS_FILE
        before = [word[2] for word in json.loads(words.read_text("utf-8"))["words"]]
        cues = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        gone = len(cues[1][1].split())
        skip = len(cues[0][1].split())
        narration_retake.remove(task_id, 1)
        saved = json.loads(words.read_text("utf-8"))

        assert saved["audio"] == finishing._audio_stamp(str(root / task_id / "audio.mp3"))
        assert [word[2] for word in saved["words"]] == before[:skip] + before[skip + gone:]
        starts = [word[0] for word in saved["words"]]
        assert starts == sorted(starts)

    def test_the_picture_on_screen_gets_shorter_by_the_same(self, video):
        _, task_id = video
        before = _lengths(task_id)
        made = narration_retake.remove(task_id, 1)
        after = _lengths(task_id)

        assert abs(sum(before) - sum(after) - made["removed"]) < 0.01
        assert after[0] == before[0] and after[2:] == before[2:]

    def test_the_script_and_the_scene_no_longer_have_it(self, video):
        _, task_id = video
        narration_retake.remove(task_id, 1)
        payload = scene_manifest.load_raw(task_id)

        assert LINES[1] not in payload["script"]
        for line in (LINES[0], LINES[2], LINES[3]):
            assert line in payload["script"]
        assert [scene["narration"] for scene in payload["scenes"]] == [
            LINES[0], "", LINES[2], LINES[3]]
        assert not payload.get("audio_stale")

    def test_the_first_and_the_last_line_can_be_taken_out(self, video):
        root, task_id = video
        before = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        narration_retake.remove(task_id, 3)
        narration_retake.remove(task_id, 0)
        cues = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        assert [cue[1] for cue in cues] == [before[1][1], before[2][1]]
        assert len(_loud(root / task_id / "audio.mp3")) == 2

    def test_the_only_line_stays(self, video):
        _, task_id = video
        for _ in range(3):
            narration_retake.remove(task_id, 0)
        with pytest.raises(narration_retake.RetakeError, match="only narration line"):
            narration_retake.remove(task_id, 0)

    def test_not_while_video_scenes_are_made(self, video, monkeypatch):
        _, task_id = video

        def busy():
            raise scene_editor.SceneBusyError("video scenes are being made")

        monkeypatch.setattr(scene_video, "refuse_while_running", busy)
        with pytest.raises(scene_editor.SceneBusyError):
            narration_retake.remove(task_id, 1)

    def test_a_pause_tag_after_the_line_goes_with_it(self):
        script = "Alpha one. [pause: 0.2s]\nBeta two. [pause: 0.3s]\nGamma three."
        lines = ["Alpha one", "Beta two", "Gamma three"]
        assert narration_retake.without(script, lines, 1) == (
            "Alpha one. [pause: 0.2s]\nGamma three.")
        assert narration_retake.without(script, lines, 0) == (
            "Beta two. [pause: 0.3s]\nGamma three.")
        assert narration_retake.without(script, lines, 2) == (
            "Alpha one. [pause: 0.2s]\nBeta two. [pause: 0.3s]\n")
        assert narration_retake.without("Alpha one, beta two, gamma three.",
                                        ["Alpha one", "beta two", "gamma three"], 1) == (
            "Alpha one, gamma three.")

    def test_the_page_asks_before_taking_a_line_out(self):
        assert "/narration/${line.index}`" in JS and '"DELETE"' in JS
        assert "Delete this line" in JS


import time  # noqa: E402

from app.services import scene_regen_queue  # noqa: E402


def _job(task_id, job_id):
    for job in scene_regen_queue.snapshot(task_id)["jobs"]:
        if job["id"] == job_id:
            return job
    return None


def _work_one():
    """What the worker does for one job. Done by hand: other test files
    count on the worker thread not having been started."""
    scene_regen_queue._run(scene_regen_queue._take())


@pytest.fixture
def queued(video, monkeypatch):
    """The video, a voice that says a 1s tone, and a queue worked by hand."""
    root, task_id = video
    scene_regen_queue._reset_for_tests()
    said = []
    monkeypatch.setattr(scene_regen_queue, "_ensure_worker", lambda: None)
    monkeypatch.setattr(narration_retake, "_record",
                        lambda params: _recorder(1.0, said=said))
    monkeypatch.setattr(
        scene_editor, "load_params",
        lambda task: type("Params", (), {"voice_name": "a-voice", "voice_rate": 1.0})())
    yield root, task_id, said
    scene_regen_queue._reset_for_tests()


@pytest.fixture
def picture(monkeypatch):
    """Queues a picture; drawing it is instant and remembered in ``drawn``."""
    drawn = []

    def draw(job):
        drawn.append(job.id)
        job.state = scene_regen_queue.DONE
        job.finished_at = time.time()
        with scene_regen_queue._LOCK:
            scene_regen_queue._RUNNING = None
            scene_regen_queue._remember(job)

    monkeypatch.setattr(scene_regen_queue, "_run_material", draw)

    def queue_one(task_id):
        job = scene_regen_queue._Job(task_id, 0, "a picture", None, "scene1")
        with scene_regen_queue._LOCK:
            scene_regen_queue._QUEUE.append(job)
        return job

    return queue_one, drawn


class TestInTheQueue:
    def test_a_retake_is_queued_and_recorded_when_its_turn_comes(self, queued):
        root, task_id, said = queued
        job = scene_regen_queue.enqueue_retake(task_id, 1)
        assert job["kind"] == "narration" and job["line"] == 1
        assert job["state"] == "queued" and not said

        _work_one()
        done = _job(task_id, job["id"])
        assert done["state"] == "done"
        assert len(said) == 1 and abs(done["added"] + 0.6) < 0.15
        assert len(_loud(root / task_id / "audio.mp3")) == 4
        assert not scene_regen_queue.snapshot()["busy"]

    def test_it_waits_behind_the_picture_queued_before_it(self, queued, picture):
        _, task_id, said = queued
        queue_one, drawn = picture
        first = queue_one(task_id)
        job = scene_regen_queue.enqueue_retake(task_id, 1)
        assert job["position"] == 2

        _work_one()
        assert drawn == [first.id] and not said
        assert _job(task_id, job["id"])["state"] == "queued"
        _work_one()
        assert len(said) == 1 and _job(task_id, job["id"])["state"] == "done"

    def test_while_it_is_recorded_it_is_the_one_thing_running(self, queued, monkeypatch):
        _, task_id, _ = queued
        seen = []

        def record(params):
            def one(text, target):
                seen.append(scene_regen_queue.snapshot(task_id)["running"]["kind"])
                return _recorder(1.0)(text, target)
            return one

        monkeypatch.setattr(narration_retake, "_record", record)
        scene_regen_queue.enqueue_retake(task_id, 1)
        _work_one()
        assert seen == ["narration"]

    def test_a_line_deleted_before_it_does_not_send_it_to_the_wrong_line(self, queued):
        root, task_id, said = queued
        cues = finishing.moviepy_subtitles(str(root / task_id / "subtitle.srt"))
        job = scene_regen_queue.enqueue_retake(task_id, 2)
        narration_retake.remove(task_id, 0)

        _work_one()
        done = _job(task_id, job["id"])
        assert done["state"] == "done" and done["line"] == 1
        assert [words.split()[0] for words in said] == [cues[2][1].split()[0]]

    def test_a_line_deleted_while_it_waited_fails_and_says_so(self, queued):
        _, task_id, said = queued
        job = scene_regen_queue.enqueue_retake(task_id, 2)
        narration_retake.remove(task_id, 2)

        _work_one()
        failed = _job(task_id, job["id"])
        assert failed["state"] == "failed" and not said
        assert "no longer in the narration" in failed["error"]

    def test_the_same_line_is_not_queued_twice(self, queued):
        _, task_id, _ = queued
        scene_regen_queue.enqueue_retake(task_id, 1)
        with pytest.raises(scene_manifest.SceneError, match="already waiting"):
            scene_regen_queue.enqueue_retake(task_id, 1)
        scene_regen_queue.enqueue_retake(task_id, 2)
        assert scene_regen_queue.snapshot(task_id)["waiting"] == 2

    def test_a_waiting_retake_can_be_cancelled(self, queued):
        _, task_id, said = queued
        job = scene_regen_queue.enqueue_retake(task_id, 1)
        scene_regen_queue.cancel(job["id"])
        assert not scene_regen_queue.snapshot()["busy"] and not said

    def test_it_is_refused_on_the_click_while_video_scenes_are_made(
            self, queued, monkeypatch):
        _, task_id, _ = queued

        def busy():
            raise scene_editor.SceneBusyError("video scenes are being made")

        monkeypatch.setattr(scene_video, "refuse_while_running", busy)
        with pytest.raises(scene_editor.SceneBusyError):
            scene_regen_queue.enqueue_retake(task_id, 1)
        assert not scene_regen_queue.snapshot()["busy"]

    def test_a_line_that_cannot_be_recorded_is_refused_on_the_click(self, queued):
        _, task_id, _ = queued
        with pytest.raises(narration_retake.RetakeError, match="no narration line 9"):
            scene_regen_queue.enqueue_retake(task_id, 8)

    def test_nothing_else_that_generates_starts_while_a_line_waits(
            self, queued, picture, monkeypatch):
        _, task_id, _ = queued
        queue_one, _ = picture
        monkeypatch.setattr(scene_video, "availability",
                            lambda: {"available": True, "reason": ""})
        scene_regen_queue.refuse_while_busy("recording the narration")
        scene_regen_queue.enqueue_retake(task_id, 1)
        with pytest.raises(scene_editor.SceneBusyError,
                           match="narration lines are still being recorded"):
            scene_video.start(task_id)

        queue_one(task_id)
        with pytest.raises(scene_editor.SceneBusyError) as refused:
            scene_regen_queue.refuse_while_busy("recording the narration")
        assert "1 picture(s)" in str(refused.value)
        assert "1 narration line(s)" in str(refused.value)
        with pytest.raises(scene_editor.SceneBusyError, match="pictures are still being drawn"):
            scene_video.start(task_id)

    def test_a_failed_retake_says_why_and_the_queue_goes_on(self, queued, monkeypatch):
        _, task_id, said = queued
        monkeypatch.setattr(narration_retake, "_record",
                            lambda params: (lambda text, target: False))
        failed = scene_regen_queue.enqueue_retake(task_id, 1)
        _work_one()
        assert _job(task_id, failed["id"])["state"] == "failed"
        assert "did not record the line" in _job(task_id, failed["id"])["error"]

        monkeypatch.setattr(narration_retake, "_record",
                            lambda params: _recorder(1.0, said=said))
        again = scene_regen_queue.enqueue_retake(task_id, 1)
        _work_one()
        assert _job(task_id, again["id"])["state"] == "done"
