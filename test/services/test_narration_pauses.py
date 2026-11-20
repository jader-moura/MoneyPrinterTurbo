"""给已经录好的旁白加停顿：插进去的是真的静音，跟着声音走的东西一起挪。

为什么要这些测试
----------------
停顿本来只在配音那一刻管用。片子做完之后想让某一句"停一下"，以前只能把整
条旁白重录。``narration_pauses`` 改成往录好的声音里插静音——这件事一旦做错，
错得很难看：刀落在一个词中间、字幕从那里开始全部错位、画面比声音早换走。

所以这里钉住：

* 刀落在两句之间最安静的地方，插进去的正好是要的那么长；
* 字幕、每个词的时间、时间线上那一段画面，都往后挪同样的秒数；
* 每一次都从原样重做，所以改一处、去掉一处不会越加越长；
* 配音时录进去的停顿去不掉；重新配过音就把新的当原样。

Pauses spliced into an already recorded narration: the cut lands in the gap,
everything timed to the voice moves with it, and every run rebuilds from the
untouched originals.
"""

import json

import numpy as np
import pytest

from app.models.schema import VideoParams
from app.services import direction_preview, finishing, narration_pauses, scene_manifest
from app.services import scene_plan, task_artifacts
from app.services.utils import ass_subtitles
from app.utils import utils

RATE = 24000
LINES = [
    "The tent was still standing.",
    "It was led by Igor Dyatlov.",
    "He starts to dig.",
    "It is human remains.",
]
# 每一句从第几秒念到第几秒；句与句之间是静音。
SPOKEN = [(0.0, 2.0), (2.4, 4.0), (6.0, 7.0), (9.0, 12.0)]
LENGTH = 13.0


def _stamp(seconds):
    whole = int(round(seconds * 1000))
    return f"00:00:{whole // 1000:02d},{whole % 1000:03d}"


SRT = "".join(
    f"{at}\n{_stamp(start)} --> {_stamp(end)}\n{line.rstrip('.')}\n\n"
    for at, ((start, end), line) in enumerate(zip(SPOKEN, LINES), start=1)
)


def _voice():
    """假的旁白：每一句是一段正弦波，句与句之间是静音。"""
    samples = np.zeros((int(LENGTH * RATE), 1), dtype=np.int16)
    for start, end in SPOKEN:
        first, last = int(start * RATE), int(end * RATE)
        time = np.arange(last - first) / RATE
        samples[first:last, 0] = (9000 * np.sin(2 * np.pi * 220 * time)).astype(np.int16)
    return samples


def _loud(path):
    """这份声音里哪些地方有声：``[(起, 止)]``，秒，四舍五入到 0.05。"""
    samples = narration_pauses._decode(str(path), RATE, 1)[:, 0]
    frame = RATE // 20
    active = [
        bool(np.abs(samples[at:at + frame].astype(np.int32)).max() > 1500)
        for at in range(0, len(samples) - frame + 1, frame)
    ]
    spans, start = [], None
    for at, on in enumerate(active + [False]):
        if on and start is None:
            start = at
        elif not on and start is not None:
            spans.append((round(start * 0.05, 2), round(at * 0.05, 2)))
            start = None
    return spans


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


def _task(task_root, *, pauses=None, task_id="paused", scenes=None):
    """一条录好了旁白、听过了词的片子。``pauses`` 是配音时就录进去的。"""
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.png").write_bytes(b"x")
    audio = directory / "audio.mp3"
    narration_pauses._encode(_voice(), RATE, 128, str(audio))
    (directory / "subtitle.srt").write_text(SRT, encoding="utf-8")
    plan = [
        {"narration": line, "photo": None,
         **({"direction": {"pause_after": pauses[at]}} if pauses and at in pauses else {})}
        for at, line in enumerate(LINES, start=1)
    ]
    params = VideoParams(
        video_subject="pass", video_source="openai_image", finish_look="clean",
        scene_plan=plan, subtitle_enabled=False,
    )
    script = scene_plan.script_of({"scenes": plan})
    task_artifacts.write_script_data(task_id, {
        "script": script, "search_terms": ["a"], "params": params.model_dump(),
        "audio_file": str(audio), "audio_duration": 13,
        "subtitle_path": str(directory / "subtitle.srt"),
        "scenes": scenes or [
            {"id": f"scene{at}", "index": at - 1, "material": str(directory / "1.png"),
             "narration": line, "duration": length, "kind": "image"}
            for at, (line, length) in enumerate(zip(LINES, (2.4, 3.6, 3.0, 4.0)), start=1)
        ],
    })
    cues = finishing.moviepy_subtitles(str(directory / "subtitle.srt"))
    words = [[word.start, word.end, word.text] for word in ass_subtitles.words_from_cues(cues)]
    (directory / finishing._SPOKEN_WORDS_FILE).write_text(json.dumps({
        "audio": finishing._audio_stamp(str(audio)), "words": words,
    }), encoding="utf-8")
    return task_id


def _lengths(task_id):
    return [scene["duration"] for scene in scene_manifest.load(task_id)["scenes"]]


def _cue_starts(task_root, task_id):
    cues = finishing.moviepy_subtitles(str(task_root / task_id / "subtitle.srt"))
    return [round(cue[0][0], 2) for cue in cues]


class TestTheCut:
    def test_it_lands_where_the_gap_is_quietest(self):
        samples = _voice()
        at = narration_pauses.quietest(samples, RATE, 3.9, 6.1) / RATE
        assert 4.0 < at < 6.0

    def test_a_gap_too_narrow_to_search_is_cut_in_the_middle(self):
        samples = _voice()
        assert narration_pauses.quietest(samples, RATE, 5.0, 5.01) == int(5.005 * RATE)

    def test_exactly_the_silence_asked_for_goes_in(self):
        samples = _voice()
        cut = int(5.0 * RATE)
        made = narration_pauses.spliced(samples, RATE, [(cut, 1.5)])
        assert len(made) == len(samples) + int(1.5 * RATE)
        assert not made[cut:cut + int(1.5 * RATE)].any()
        # 刀口前后的声音原样照搬。
        assert (made[:int(1.9 * RATE)] == samples[:int(1.9 * RATE)]).all()
        assert (made[int(7.6 * RATE):] == samples[int(6.1 * RATE):]).all()

    def test_a_cut_inside_sound_fades_out_and_in_so_it_does_not_click(self):
        samples = np.full((RATE, 1), 8000, dtype=np.int16)
        made = narration_pauses.spliced(samples, RATE, [(RATE // 2, 0.1)])
        before, after = RATE // 2 - 1, RATE // 2 + int(0.1 * RATE)
        assert abs(int(made[before, 0])) < 100 and abs(int(made[after, 0])) < 100
        assert made[before - RATE // 10, 0] == 8000 and made[after + RATE // 10, 0] == 8000


class TestTheSubtitles:
    def test_only_the_times_move(self):
        moved = narration_pauses.shifted_subtitles(SRT, lambda cue, start, end: cue * 0.5)
        assert "00:00:00,000 --> 00:00:02,000" in moved
        assert "00:00:02,900 --> 00:00:04,500" in moved
        assert "00:00:10,500 --> 00:00:13,500" in moved
        strip = lambda text: [line for line in text.split("\n") if "-->" not in line]  # noqa: E731
        assert strip(moved) == strip(SRT)


class TestAPauseAfterALine:
    def test_everything_timed_to_the_voice_moves_with_it(self, task_root):
        task_id = _task(task_root)
        directory = task_root / task_id
        found = narration_pauses.apply(task_id, {2: 1.0})
        assert found["inserted"] == {2: 1.0} and found["added"] == 1.0

        # 声音：第二句和第三句之间多了一秒，别的缝没变。
        spans = _loud(directory / "audio.mp3")
        assert [round(end - start, 1) for start, end in spans] == [2.0, 1.6, 1.0, 3.0]
        assert spans[2][0] == pytest.approx(7.0, abs=0.06)
        assert spans[1][0] == pytest.approx(2.4, abs=0.06)
        # 字幕和词：第三句起往后一秒。
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 7.0, 10.0]
        words = json.loads((directory / finishing._SPOKEN_WORDS_FILE).read_text("utf-8"))
        assert words["audio"] == finishing._audio_stamp(str(directory / "audio.mp3"))
        starts = [word[0] for word in words["words"]]
        assert min(start for start in starts if start > 5) == pytest.approx(7.0, abs=0.01)
        # 画面：念第二句的那一段多停一秒，别的不动。
        assert _lengths(task_id) == [2.4, 4.6, 3.0, 4.0]
        payload = task_artifacts.read_script_data(task_id)
        assert payload["audio_duration"] == 14
        # 分镜和脚本里记下来：以后重新配音，这一处就是录进去的。
        assert payload["params"]["scene_plan"][1]["direction"] == {"pause_after": 1.0}
        assert "It was led by Igor Dyatlov. [pause: 1s]" in payload["script"]

    def test_the_editor_needs_no_second_listen(self, task_root):
        task_id = _task(task_root)
        narration_pauses.apply(task_id, {2: 1.0})
        directory = task_root / task_id
        assert finishing._saved_spoken_words(str(directory / "audio.mp3"), str(directory))

    def test_the_untouched_originals_are_kept_beside_the_video(self, task_root):
        task_id = _task(task_root)
        directory = task_root / task_id
        before = (directory / "audio.mp3").read_bytes()
        narration_pauses.apply(task_id, {2: 1.0})
        assert (directory / "narration.before-pauses.mp3").read_bytes() == before
        assert (directory / narration_pauses.ORIGINAL_SUBTITLE).read_text("utf-8") == SRT

    def test_nothing_asked_for_touches_nothing(self, task_root):
        task_id = _task(task_root)
        directory = task_root / task_id
        before = (directory / "audio.mp3").read_bytes()
        assert narration_pauses.apply(task_id, {})["inserted"] == {}
        assert (directory / "audio.mp3").read_bytes() == before
        assert not (directory / narration_pauses.SIDECAR).exists()


class TestEveryRunStartsFromTheOriginal:
    def test_changing_the_pauses_does_not_pile_them_up(self, task_root):
        task_id = _task(task_root)
        narration_pauses.apply(task_id, {2: 1.0})
        narration_pauses.apply(task_id, {2: 0.5, 3: 2.0})
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 6.5, 11.5]
        assert _lengths(task_id) == [2.4, 4.1, 5.0, 4.0]
        spans = _loud(task_root / task_id / "audio.mp3")
        assert spans[2][0] == pytest.approx(6.5, abs=0.06)
        assert spans[3][0] == pytest.approx(11.5, abs=0.06)

    def test_taking_them_all_away_gives_the_timeline_back(self, task_root):
        task_id = _task(task_root)
        narration_pauses.apply(task_id, {1: 1.0, 3: 1.5})
        found = narration_pauses.apply(task_id, {})
        assert found["inserted"] == {} and found["added"] == 0.0
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 6.0, 9.0]
        assert _lengths(task_id) == [2.4, 3.6, 3.0, 4.0]
        payload = task_artifacts.read_script_data(task_id)
        assert payload["audio_duration"] == 13 and "[pause" not in payload["script"]
        assert all("direction" not in scene for scene in payload["params"]["scene_plan"])

    def test_a_narration_recorded_again_becomes_the_new_original(self, task_root):
        task_id = _task(task_root)
        directory = task_root / task_id
        narration_pauses.apply(task_id, {2: 1.0})
        # 重新配音：旁白换了一份，词也重新听过。
        _task(task_root)
        assert narration_pauses.describe(task_id)["inserted"] == {}
        narration_pauses.apply(task_id, {3: 1.0})
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 6.0, 10.0]
        spans = _loud(directory / "audio.mp3")
        assert [round(end - start, 1) for start, end in spans] == [2.0, 1.6, 1.0, 3.0]


class TestPausesRecordedWithTheVoice:
    def test_they_cannot_be_taken_out_or_shortened(self, task_root):
        task_id = _task(task_root, pauses={1: 1.0})
        assert narration_pauses.recorded(task_id) == {1: 1.0}
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        new = scene_plan.redirected(
            plan, json.dumps({"scenes": {"1": {"pause_after": 0.4}, "2": {"impact": "end"}}}),
            recorded={1: 1.0})
        assert new[0]["direction"] == {"pause_after": 1.0}

    def test_only_what_goes_beyond_them_is_spliced_in(self, task_root):
        task_id = _task(task_root, pauses={2: 0.5})
        found = narration_pauses.apply(task_id, {2: 1.5})
        assert found["inserted"] == {2: 1.0}
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        assert plan[1]["direction"]["pause_after"] == 1.5
        # 后来不要多出来的那一截了：录进去的半秒还在。
        narration_pauses.apply(task_id, {2: 0.5})
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        assert plan[1]["direction"]["pause_after"] == 0.5
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 6.0, 9.0]


class TestWhichPictureHolds:
    def test_the_one_showing_that_line_even_when_the_times_disagree(self, task_root):
        """时间线照字幕排，字幕的时间是估的：按秒数去对会落到隔壁那一段上。"""
        directory = task_root / "paused"
        scenes = [
            {"id": f"scene{at}", "index": at - 1, "material": str(directory / "1.png"),
             "narration": line, "duration": length, "kind": "image"}
            # 第一段画面排得很长：第二句念完的那一秒，屏幕上还是第一段。
            for at, (line, length) in enumerate(zip(LINES, (6.0, 1.0, 2.0, 4.0)), start=1)
        ]
        task_id = _task(task_root, scenes=scenes)
        narration_pauses.apply(task_id, {2: 1.0})
        assert _lengths(task_id) == [6.0, 2.0, 2.0, 4.0]

    def test_a_line_whose_picture_was_removed_holds_what_is_on_screen_then(self, task_root):
        directory = task_root / "paused"
        scenes = [
            {"id": "wide", "index": 0, "material": str(directory / "1.png"),
             "narration": "Something else entirely.", "duration": 5.0, "kind": "image"},
            {"id": "close", "index": 1, "material": str(directory / "1.png"),
             "narration": "And another thing.", "duration": 8.0, "kind": "image"},
        ]
        task_id = _task(task_root, scenes=scenes)
        # 第二句在字幕里第 4 秒结束：那时屏幕上是第一段。
        narration_pauses.apply(task_id, {2: 1.0})
        assert _lengths(task_id) == [6.0, 8.0]


class TestWhatCannotBeDone:
    def test_a_line_that_is_no_longer_spoken_is_named(self, task_root):
        task_id = _task(task_root)
        payload = task_artifacts.read_script_data(task_id)
        payload["params"]["scene_plan"][2]["narration"] = "He stops digging at dusk."
        task_artifacts.write_script_data(task_id, payload)
        before = (task_root / task_id / "audio.mp3").read_bytes()
        with pytest.raises(narration_pauses.PauseError, match="scene 3"):
            narration_pauses.apply(task_id, {3: 1.0})
        assert (task_root / task_id / "audio.mp3").read_bytes() == before
        assert _lengths(task_id) == [2.4, 3.6, 3.0, 4.0]

    def test_a_narration_waiting_to_be_recorded_again(self, task_root):
        task_id = _task(task_root)
        task_artifacts.patch_script_data(task_id, audio_stale=True)
        with pytest.raises(narration_pauses.PauseError, match="re-record"):
            narration_pauses.apply(task_id, {1: 1.0})


class TestTheDirectionFile:
    def test_a_pause_in_the_file_is_spliced_in_and_reported(self, task_root):
        task_id = _task(task_root)
        said = direction_preview.redirect(task_id, json.dumps({
            "scenes": {"2": {"pause_after": 1.0, "impact": "end"}}}))
        assert "1 pause spliced into the narration (+1.0s); it now runs 0:14" in said
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        assert plan[1]["direction"] == {"impact": "end", "pause_after": 1.0}
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 7.0, 10.0]

    def test_leaving_it_out_of_the_next_file_removes_it(self, task_root):
        task_id = _task(task_root)
        direction_preview.redirect(task_id, json.dumps({"scenes": {"2": {"pause_after": 1.0}}}))
        said = direction_preview.redirect(
            task_id, json.dumps({"scenes": {"2": {"impact": "end"}}}))
        assert "removed" in said
        assert _cue_starts(task_root, task_id) == [0.0, 2.4, 6.0, 9.0]
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        assert plan[1]["direction"] == {"impact": "end"}

    def test_a_file_without_pauses_does_not_touch_the_narration(self, task_root):
        task_id = _task(task_root)
        before = (task_root / task_id / "audio.mp3").read_bytes()
        direction_preview.redirect(task_id, json.dumps({"scenes": {"2": {"impact": "end"}}}))
        assert (task_root / task_id / "audio.mp3").read_bytes() == before
        assert not (task_root / task_id / narration_pauses.SIDECAR).exists()
