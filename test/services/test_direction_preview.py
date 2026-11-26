"""编辑器里预览成片的导演项：片名、标题卡、重音、环境声、配乐让开。

钉住的事：

* 位置是出片那一套算的（同一份分镜、同一个 ``direct``），编辑器不另算一遍；
* 接口从不在请求里去听配音：没听过就答 ``listening``、放到后台听，同一条
  任务只听一个；听不了就退回字幕估的时间，并且说出来，不反复重试；
* 没选成片观感的片子，分镜里写了导演项也要说"出片时不会做"；
* 没挑过标题卡的片子，不为了预览去问模型；
* 那几种声音合成一次、存成文件；认不出的名字是 404；
* 页面那一头：每一帧都画、都放，停下来声音也停；配乐让开用的是和 ffmpeg
  同一个式子；画面上的字号、位置是 ``ass_subtitles`` 里的那几个数。

The editor's preview of what the finishing pass adds, and where it lands.
"""

import json
import threading
import wave
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.models.schema import VideoParams
from app.services import direction_preview, finishing, llm, scene_editor, scene_manifest
from app.services import sound_design, task_artifacts
from app.services import state as sm
from app.services.utils import ass_subtitles
from app.utils import utils

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (PAGE / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (PAGE / "editor.css").read_text(encoding="utf-8").replace("\r\n", "\n")

LINES = [
    "The tent was still standing,",
    "led by Igor Dyatlov.",
    "He starts to dig.",
    "It is human remains.",
]
SRT = """1
00:00:00,000 --> 00:00:02,000
The tent was still standing,

2
00:00:02,400 --> 00:00:04,000
led by Igor Dyatlov.

3
00:00:06,000 --> 00:00:07,000
He starts to dig.

4
00:00:09,000 --> 00:00:12,000
It is human remains.
"""
DIRECTED = [
    {"ambience": "wind", "title": {"text": "Dyatlov", "tagline": "Nine hikers"}},
    {"card": {"title": "Igor Dyatlov", "subtitle": "Leader"}},
    {"music": "off", "impact": "start"},
    {"music": "off", "ambience": "room"},
]


def _function(name):
    return JS.split(f"\nfunction {name}(")[1].split("\n}\n")[0]


@pytest.fixture(autouse=True)
def forgotten():
    """每个测试从"谁也没在听、谁也没失败过"开始。"""
    direction_preview._listening.clear()
    direction_preview._failed.clear()
    # 测试里的"配音"只是几个字节：量它多长会让解码器白启动一次。
    with patch.object(
            direction_preview.voice_service, "get_audio_duration", return_value=30.0):
        yield
    for worker in list(direction_preview._listening.values()):
        worker.join(timeout=5)
    direction_preview._listening.clear()
    direction_preview._failed.clear()


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


def _task(task_root, *, look="clean", directions=DIRECTED, task_id="directed"):
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.png").write_bytes(b"x")
    (directory / "audio.mp3").write_bytes(b"not really audio")
    (directory / "subtitle.srt").write_text(SRT, encoding="utf-8")
    plan = [
        {"narration": line, "photo": None, **({"direction": wanted} if wanted else {})}
        for line, wanted in zip(LINES, directions or [None] * len(LINES))
    ]
    params = VideoParams(
        video_subject="pass", video_source="openai_image", finish_look=look,
        scene_plan=plan, subtitle_enabled=False,
    )
    script = " ".join(LINES)
    task_artifacts.write_script_data(
        task_id, {"script": script, "search_terms": ["a"], "params": params.model_dump()})
    scene_manifest.record_prepared_artifacts(
        task_id, video_script=script, video_terms=["a"],
        materials=[str(directory / "1.png")],
        audio_file=str(directory / "audio.mp3"), audio_duration=30.0,
        subtitle_path=str(directory / "subtitle.srt"),
        video_source="openai_image", clip_duration=5,
    )
    return task_id


def _from_the_subtitles():
    """词的时间照字幕摊，不听配音。"""
    return patch.object(finishing.config, "finish", {"word_timing": "cues"}, create=True)


class TestWhatTheEditorIsTold:
    def test_a_directed_plan_comes_back_placed_in_seconds(self, task_root):
        with _from_the_subtitles():
            found = direction_preview.describe(_task(task_root))
        assert found["state"] == "ready" and found["planned"]
        assert found["title"] == {
            "start": 0.0, "end": 2.55, "text": "Dyatlov", "tagline": "Nine hikers"}
        assert found["cards"] == [
            {"start": 2.4, "end": 8.4, "title": "Igor Dyatlov", "subtitle": "Leader"}]
        # 片名自带一记，"这一句开头落一记"是另一记。
        assert found["hits"] == pytest.approx([0.0, 5.9])
        assert found["music_off"] == [[6.0, 12.0]]
        assert [bed["kind"] for bed in found["beds"]] == ["room", "wind"]
        assert found["beds"][1]["windows"] == [[0.0, 2.0]]

    def test_it_is_the_placement_the_render_makes(self, task_root):
        task_id = _task(task_root)
        with _from_the_subtitles():
            found = direction_preview.describe(task_id)
            task_dir = utils.task_dir(task_id)
            params = scene_editor.load_params(task_id)
            words = ass_subtitles.words_from_cues(
                finishing.moviepy_subtitles(str(task_root / task_id / "subtitle.srt")))
            rendered = finishing.direct(
                finishing._plan_scenes(task_dir, params), words, found["duration"])
        assert found["hits"] == pytest.approx(rendered.hits)
        assert found["music_off"] == [list(window) for window in rendered.music_off]
        assert found["title"]["start"] == pytest.approx(rendered.title.start)

    def test_the_envelopes_are_the_ones_ffmpeg_is_given(self, task_root):
        with _from_the_subtitles():
            found = direction_preview.describe(_task(task_root))
        music = found["music"]
        assert music["ramps"] == [[3.5, 12.8]]
        assert (music["rise"], music["fall"], music["floor_db"]) == (2.5, 4.0, -70.0)
        expression = sound_design.gate([(6.0, 12.0)], **sound_design.MUSIC_GATE)
        assert expression == "clip((t-3.500)/2.500,0,1)-clip((t-12.800)/4.000,0,1)"
        wind = found["beds"][1]
        assert wind["ramps"] == [[0.0, 3.5]] and (wind["rise"], wind["fall"]) == (2.0, 2.0)

    def test_estimated_timing_is_said_out_loud(self, task_root):
        with _from_the_subtitles():
            found = direction_preview.describe(_task(task_root))
        assert found["timing"] == "estimate"
        assert "subtitle timings" in found["note"]

    def test_a_plan_without_direction_has_nothing_to_show(self, task_root):
        with _from_the_subtitles():
            found = direction_preview.describe(_task(task_root, directions=None))
        assert found == {"state": "none", "note": ""}

    def test_without_a_finish_look_the_direction_is_reported_as_not_rendered(self, task_root):
        found = direction_preview.describe(_task(task_root, look=""))
        assert found["state"] == "off" and "Finish look" in found["note"]
        plain = direction_preview.describe(
            _task(task_root, look="", directions=None, task_id="plain"))
        assert plain == {"state": "none", "note": ""}

    def test_direction_that_cannot_be_placed_says_so(self, task_root):
        task_id = _task(task_root)
        (task_root / task_id / "subtitle.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nSomething else entirely.\n", encoding="utf-8")
        with _from_the_subtitles():
            found = direction_preview.describe(task_id)
        assert found["state"] == "off" and "not in the recording" in found["note"]

    def test_cards_already_chosen_are_shown_and_the_model_is_not_asked(self, task_root):
        task_id = _task(task_root, directions=None)
        (task_root / task_id / "title-cards.json").write_text(json.dumps(
            [{"phrase": "led by Igor Dyatlov", "title": "Igor Dyatlov", "subtitle": ""}]),
            encoding="utf-8")
        with _from_the_subtitles(), patch.object(
                llm, "generate_title_cards", side_effect=AssertionError("asked")):
            found = direction_preview.describe(task_id)
            (task_root / task_id / "title-cards.json").unlink()
            nothing = direction_preview.describe(task_id)
        assert [card["title"] for card in found["cards"]] == ["Igor Dyatlov"]
        assert not found["planned"] and found["note"] == ""
        assert nothing["state"] == "none"


class TestListeningToTheNarration:
    def test_the_request_does_not_wait_for_it(self, task_root):
        task_id = _task(task_root)
        started, release = threading.Event(), threading.Event()

        def slow(audio, task_dir):
            started.set()
            release.wait(5)

        with patch.object(finishing, "listen", side_effect=slow) as listen:
            first = direction_preview.describe(task_id)
            assert started.wait(5)
            second = direction_preview.describe(task_id)
            release.set()
            direction_preview._listening[task_id].join(5)
        assert first["state"] == second["state"] == "listening"
        # 问了两次，只听一个。
        assert listen.call_count == 1

    def test_once_heard_the_direction_follows_the_recording(self, task_root):
        task_id = _task(task_root)
        spoken = [
            (0.5, 0.7, "The"), (0.7, 1.0, "tent"), (1.0, 1.2, "was"), (1.2, 1.5, "still"),
            (1.5, 2.1, "standing"), (3.0, 3.2, "led"), (3.2, 3.4, "by"), (3.4, 3.8, "Igor"),
            (3.8, 4.4, "Dyatlov"), (6.5, 6.7, "He"), (6.7, 7.0, "starts"), (7.0, 7.1, "to"),
            (7.1, 7.5, "dig"), (9.2, 9.4, "It"), (9.4, 9.6, "is"), (9.6, 10.0, "human"),
            (10.0, 10.8, "remains"),
        ]
        with patch.object(finishing, "_spoken_words", return_value=spoken) as heard:
            assert direction_preview.describe(task_id)["state"] == "listening"
            direction_preview._listening[task_id].join(5)
            found = direction_preview.describe(task_id)
        assert heard.call_count == 1
        assert found["state"] == "ready" and found["timing"] == "recording"
        assert found["note"] == ""
        assert found["cards"][0]["start"] == pytest.approx(3.0)

    def test_a_narration_that_cannot_be_heard_falls_back_and_is_not_retried(self, task_root):
        task_id = _task(task_root)
        failure = finishing.FinishError("the whisper model cannot be loaded")
        with patch.object(finishing, "_spoken_words", side_effect=failure) as heard:
            assert direction_preview.describe(task_id)["state"] == "listening"
            direction_preview._listening[task_id].join(5)
            found = direction_preview.describe(task_id)
            again = direction_preview.describe(task_id)
        assert heard.call_count == 1
        assert found["state"] == again["state"] == "ready"
        assert found["timing"] == "estimate" and "cannot be loaded" in found["note"]
        assert found["cards"][0]["start"] == pytest.approx(2.4)

    def test_placing_the_direction_never_listens_by_itself(self, task_root):
        task_id = _task(task_root)
        directory = str(task_root / task_id)
        params = scene_editor.load_params(task_id)
        with patch.object(finishing, "_spoken_words", side_effect=AssertionError("listened")):
            found = finishing.editor_direction(
                params, f"{directory}/subtitle.srt", f"{directory}/audio.mp3", directory, 30.0)
        assert found["timing"] == "estimate" and found["cards"]

    def test_a_video_with_nothing_to_place_is_not_listened_to(self, task_root):
        task_id = _task(task_root, directions=None)
        with patch.object(finishing, "listen", side_effect=AssertionError("listened")):
            assert direction_preview.describe(task_id)["state"] == "none"


class TestAnotherVideoBeingTimed:
    def test_a_video_already_heard_does_not_wait_for_it(self, task_root, tmp_path):
        """别的片子正在听（锁被占着），这一条有现成的结果：马上答。"""
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"heard before")
        (tmp_path / finishing._SPOKEN_WORDS_FILE).write_text(json.dumps({
            "audio": finishing._audio_stamp(str(audio)), "words": [[0.0, 0.5, "tent"]],
        }), encoding="utf-8")
        answered = []
        assert finishing._LISTENING.acquire(timeout=1)
        try:
            worker = threading.Thread(target=lambda: answered.append(
                finishing._spoken_words_once(str(audio), str(tmp_path))))
            worker.start()
            worker.join(timeout=5)
        finally:
            finishing._LISTENING.release()
        worker.join(timeout=5)
        assert [list(word) for word in answered[0]] == [[0.0, 0.5, "tent"]]
        assert not worker.is_alive()


class TestThePlacement:
    def test_a_title_and_an_impact_on_the_same_line_strike_once(self):
        cues = [((0.0, 2.0), LINES[0]), ((2.4, 4.0), LINES[1])]
        found = finishing.direct([
            {"narration": LINES[0], "direction": {
                "impact": "start", "title": {"text": "Dyatlov", "tagline": ""}}},
            {"narration": LINES[1], "direction": {"impact": "end"}},
        ], ass_subtitles.words_from_cues(cues), 60.0)
        assert found.hits == pytest.approx([0.0, 4.05])

    def test_the_ramps_are_what_the_gate_is_built_from(self):
        windows = [(10.0, 12.0), (14.0, 20.0), (60.0, 61.0)]
        assert sound_design.ramps(windows, fall=2.0, lead=0.5, hold=1.5) == [
            (9.5, 21.5), (59.5, 62.5)]
        assert sound_design.gate(windows, **sound_design.BED_GATE).count("clip") == 4


class TestTheSounds:
    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        def storage(sub_dir: str = "", create: bool = False):
            target = tmp_path / "storage" / sub_dir
            if create:
                target.mkdir(parents=True, exist_ok=True)
            return str(target)

        monkeypatch.setattr(utils, "storage_dir", storage)
        return tmp_path / "storage"

    def test_the_editor_can_play_every_sound_the_plan_can_ask_for(self):
        assert set(direction_preview.SOUNDS) == {"hit", *sound_design.BED_RMS_DB}

    def test_a_sound_is_synthesised_once_and_kept(self, cache):
        with patch.object(sound_design, "hit", wraps=sound_design.hit) as made:
            first = direction_preview.sound_file("hit")
            second = direction_preview.sound_file("hit")
        assert first == second and made.call_count == 1
        with wave.open(first, "rb") as handle:
            assert handle.getframerate() == sound_design.SAMPLE_RATE
            assert handle.getnframes() == int(
                sound_design.HIT_SECONDS * sound_design.SAMPLE_RATE)

    def test_a_changed_level_is_a_different_file(self, cache):
        first = direction_preview.sound_file("hit")
        with patch.object(sound_design, "HIT_PEAK_DB", -12.0):
            assert direction_preview.sound_file("hit") != first

    def test_an_unknown_name_is_refused(self, cache):
        with pytest.raises(ValueError):
            direction_preview.sound_file("../config")


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        return TestClient(asgi.app)

    def test_the_direction_of_a_task(self, client, task_root):
        task_id = _task(task_root)
        with patch.object(sm.state, "get_task", return_value={"task_id": task_id}):
            with _from_the_subtitles():
                response = client.get(f"/api/v1/tasks/{task_id}/direction")
        assert response.status_code == 200
        assert response.json()["data"]["title"]["text"] == "Dyatlov"

    def test_an_unknown_task_is_not_found(self, client, task_root):
        assert client.get("/api/v1/tasks/nobody/direction").status_code == 404

    def test_a_sound_is_served_as_audio(self, client, tmp_path):
        target = tmp_path / "hit.wav"
        sound_design.write_wav(str(target), sound_design.hit(0.2))
        with patch.object(direction_preview, "sound_file", return_value=str(target)):
            response = client.get("/api/v1/direction/sounds/hit")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"

    def test_an_unknown_sound_is_not_found(self, client):
        assert client.get("/api/v1/direction/sounds/nope").status_code == 404


class TestThePage:
    def test_the_overlay_sits_in_the_frame_under_the_captions(self):
        frame = HTML.split('<div id="frame"')[1].split('<div id="placeholder"')[0]
        assert frame.index('id="subs"') < frame.index('id="direction"')
        for name in ("dir-dim", "dir-title-text", "dir-tagline", "dir-card-title",
                     "dir-card-sub"):
            assert f'id="{name}"' in frame
        layer = CSS.split("\n.direction {")[1].split("}")[0]
        assert "z-index: 2;" in layer and "pointer-events: none;" in layer

    def test_the_lane_and_its_name_are_absent_until_there_is_direction(self):
        assert '<div id="direction-lane" class="track short direction-lane" hidden>' in HTML
        assert 'id="direction-cell" class="gutter-cell short" hidden' in HTML
        body = _function("drawDirectionTrack")
        assert "lane.hidden = !show;" in body and "ui.directionCell.hidden = !show;" in body

    def test_it_is_read_with_the_task_and_again_after_an_edit(self):
        opened = JS.split("\nasync function load(")[1].split("\n}\n")[0]
        assert opened.index("forgetDirection();") < opened.index("loadDirection();")
        assert "loadDirection();" in JS.split("\nasync function refresh(")[1].split("\n}\n")[0]

    def test_a_placement_still_being_timed_is_asked_for_again(self):
        body = JS.split("\nasync function loadDirection(")[1].split("\n}\n")[0]
        assert "showDirection(found);" in body
        # Shown by the function the title card routes' answers go through too.
        shown = _function("showDirection")
        assert 'found.state === "listening"' in shown
        assert "setTimeout(loadDirection, DIRECTION_POLL)" in shown
        # 中途换了任务，迟到的答案不作数。
        assert "if (taskId !== state.taskId) return;" in body

    def test_every_frame_draws_and_sounds_it(self):
        body = _function("movePlayhead")
        assert "drawDirection();" in body and "soundDirection();" in body
        assert "drawDirectionTrack();" in _function("draw")

    def test_stopping_stops_the_sounds_and_playing_picks_an_impact_up_midway(self):
        assert "quietDirection();" in _function("stop")
        assert "startHitsUnder(state.playhead);" in _function("play")
        assert "strike(at - hit)" in _function("startHitsUnder")

    def test_the_music_level_goes_through_the_dropout(self):
        assert "applyMusicLevel();" in _function("syncMusicBed")
        assert "ui.musicBed.volume =" not in _function("syncMusicBed")
        assert "state.musicLevel" in _function("applyMusicLevel")
        dip = _function("musicDip")
        assert "Math.pow(10, (found.music.floor_db / 20) * envelopeAt(found.music, at))" in dip

    def test_the_envelope_is_the_gate_expression(self):
        body = _function("envelopeAt")
        assert "unit((at - up) / Math.max(shape.rise, 0.01))" in body
        assert "- unit((at - down) / Math.max(shape.fall, 0.01))" in body

    def test_the_sizes_are_the_ones_burned_into_the_video(self):
        assert f"font-size: calc({ass_subtitles._CARD_TITLE_SIZE} * var(--u));" in CSS
        assert f"font-size: calc({ass_subtitles._CARD_SUB_SIZE} * var(--u));" in CSS
        assert f"font-size: calc({ass_subtitles._TITLE_SIZE} * var(--u) * var(--shrink, 1));" in CSS
        assert f"font-size: calc({ass_subtitles._TITLE_SUB_SIZE} * var(--u));" in CSS
        # 标题卡的红线：&H003A3AC8 是 BGR。
        assert ass_subtitles._CARD_ACCENT == "&H003A3AC8" and "background: #c83a3a;" in CSS
        body = _function("drawDirection")
        assert f"{ass_subtitles._TITLE_SIZE} * 0.62 * squeeze + 12" in body
        assert "/ 1080" in body


class TestDirectingAVideoThatExists:
    """``python -m app.services.direction_preview <task> direction.json``。"""

    WANTED = json.dumps({
        "title": {"text": "Dyatlov", "scene": 2},
        "scenes": {"1": {"card": "February 1959 | Northern Urals"},
                   "3-4": {"music": "off", "ambience": "room"}},
    })

    def test_only_the_plans_direction_changes_and_the_old_file_is_kept(self, task_root):
        task_id = _task(task_root)
        before = task_artifacts.read_script_data(task_id)
        with _from_the_subtitles():
            said = direction_preview.redirect(task_id, self.WANTED)
            found = direction_preview.describe(task_id)
        after = task_artifacts.read_script_data(task_id)
        assert [s.get("direction") for s in after["params"]["scene_plan"]] == [
            {"card": {"title": "February 1959", "subtitle": "Northern Urals"}},
            {"title": {"text": "Dyatlov", "tagline": ""}},
            {"music": "off", "ambience": "room"},
            {"music": "off", "ambience": "room"},
        ]
        after["params"]["scene_plan"] = before["params"]["scene_plan"]
        assert after == before
        kept = json.loads((task_root / task_id / direction_preview.BACKUP).read_text("utf-8"))
        assert kept == before
        # 说的是导演项，不是画面；编辑器接着读到的就是新的这一份。
        assert said.startswith("direction: title over scene 2; 1 card (scene 1)")
        assert "drawn from a photo" not in said and said.endswith("rendered again")
        assert found["state"] == "ready" and found["title"]["text"] == "Dyatlov"
        assert [card["title"] for card in found["cards"]] == ["February 1959"]

    def test_the_first_backup_is_the_one_that_stays(self, task_root):
        task_id = _task(task_root)
        before = task_artifacts.read_script_data(task_id)
        direction_preview.redirect(task_id, self.WANTED)
        direction_preview.redirect(task_id, json.dumps({"scenes": {"1": {"impact": "end"}}}))
        kept = json.loads((task_root / task_id / direction_preview.BACKUP).read_text("utf-8"))
        assert kept == before

    def test_a_refused_file_changes_nothing(self, task_root):
        task_id = _task(task_root)
        before = task_artifacts.read_script_data(task_id)
        with pytest.raises(direction_preview.scene_plan.ScenePlanError):
            direction_preview.redirect(task_id, json.dumps({"scenes": {"9": {"music": "off"}}}))
        assert task_artifacts.read_script_data(task_id) == before
        assert not (task_root / task_id / direction_preview.BACKUP).exists()

    def test_what_would_stop_it_showing_is_said(self, task_root):
        task_id = _task(task_root, look="")
        (task_root / task_id / "title-cards.json").write_text("[]", encoding="utf-8")
        said = direction_preview.redirect(task_id, self.WANTED)
        assert "no Finish look" in said and "title-cards.json" in said

    def test_the_scenes_are_listed_by_number(self, task_root):
        task_id = _task(task_root)
        lines = direction_preview.outline(task_id).split("\n")
        assert len(lines) == 4 and "[card: Igor Dyatlov]" in lines[1]
        with pytest.raises(direction_preview.scene_plan.ScenePlanError, match="no video"):
            direction_preview.outline("missing")

    def test_the_editors_address_names_the_task(self):
        assert direction_preview.task_of(
            "http://127.0.0.1:8080/editor/?task=abc-123&x=1") == "abc-123"
        assert direction_preview.task_of(" abc-123 ") == "abc-123"


class TestAFailedRead:
    def test_the_track_says_so_instead_of_showing_nothing(self):
        body = JS.split("\nasync function loadDirection(")[1].split("\n}\n")[0]
        failed = body.split("} catch (error) {")[1].split("if (taskId")[0]
        assert 'state: "off"' in failed and "could not be loaded" in failed
        assert "found = null" not in failed
        # ``off`` 带着说明：轨道照样出现，把这句话写在上面。
        assert "!!found.note" in _function("drawDirectionTrack")
