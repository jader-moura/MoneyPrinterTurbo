"""The Chatterbox container runs for a narration and is stopped after it."""

from unittest.mock import patch

import pytest

from app.config import config
from app.services import chatterbox_server as server
from app.services import voice

# Taken at import, before the suite-wide fixture swaps it for "no container".
REAL_CONTAINER = server._container


@pytest.fixture
def chatterbox_config(monkeypatch):
    def use(**values):
        monkeypatch.setattr(config, "chatterbox", values)
        monkeypatch.setattr(server, "_container", REAL_CONTAINER)
    return use


@pytest.mark.parametrize("values, expected", [
    ({"base_url": "http://127.0.0.1:4123/v1", "container": "box"}, "box"),
    ({"base_url": "http://localhost:4123/v1", "container": " box "}, "box"),
    # Unnamed: a server the user runs their own way is left alone.
    ({"base_url": "http://127.0.0.1:4123/v1"}, ""),
    ({"base_url": "http://127.0.0.1:4123/v1", "container": ""}, ""),
    # Somewhere else: it does not share this card, and is not ours to stop.
    ({"base_url": "http://10.0.0.5:4123/v1", "container": "box"}, ""),
    ({"base_url": "", "container": "box"}, ""),
])
def test_only_a_named_container_on_this_machine_is_managed(chatterbox_config, values, expected):
    chatterbox_config(**values)
    assert server._container() == expected


def test_nothing_is_started_or_stopped_without_a_container(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1")
    with patch.object(server, "_docker") as docker, \
            patch.object(server, "is_ready", return_value=False):
        assert server.ensure_running() is True
        assert server.release() is False
    docker.assert_not_called()


def test_a_ready_server_is_not_started_again(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server, "_docker") as docker, \
            patch.object(server, "is_ready", return_value=True):
        assert server.ensure_running() is True
    docker.assert_not_called()


def test_a_stopped_container_is_started_and_waited_for(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server, "_docker", return_value=True) as docker, \
            patch.object(server, "_status", side_effect=["", "", "initializing", "healthy"]), \
            patch.object(server.time, "sleep"):
        assert server.ensure_running() is True
    docker.assert_called_once_with("start", "box")


def test_a_model_that_fails_to_load_is_reported_at_once(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server, "_docker", return_value=True) as docker, \
            patch.object(server, "_status", side_effect=["", "initializing", "error"]), \
            patch.object(server.time, "sleep"):
        assert server.ensure_running() is False
    # Started, then stopped again: a server that cannot speak holds no card.
    assert [call.args[0] for call in docker.call_args_list] == ["start", "stop"]


def test_a_container_that_never_loads_is_a_failure(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box", start_timeout=0.01)
    with patch.object(server, "_docker", return_value=True), \
            patch.object(server, "_status", return_value="initializing"), \
            patch.object(server, "POLL_SECONDS", 0.001):
        assert server.ensure_running() is False


def test_a_container_docker_cannot_start_is_a_failure(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server, "_docker", return_value=False), \
            patch.object(server, "is_ready", return_value=False):
        assert server.ensure_running() is False


def test_release_stops_the_container(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server, "_docker", return_value=True) as docker:
        assert server.release() is True
    docker.assert_called_once_with("stop", "-t", "5", "box")


def test_missing_docker_is_reported_not_raised(chatterbox_config):
    chatterbox_config(base_url="http://127.0.0.1:4123/v1", container="box")
    with patch.object(server.subprocess, "run", side_effect=FileNotFoundError):
        assert server.release() is False


def _tts(name="chatterbox:narrator-Male"):
    return voice.tts(text="Hello there.", voice_name=name, voice_rate=1.0, voice_file="x.mp3")


def test_narration_starts_the_server_and_always_gives_the_card_back():
    with patch.object(server, "ensure_running", return_value=True) as start, \
            patch.object(server, "release") as release, \
            patch.object(voice, "_single_tts", return_value="made"):
        assert _tts() == "made"
    start.assert_called_once()
    release.assert_called_once()

    with patch.object(server, "ensure_running", return_value=True), \
            patch.object(server, "release") as release, \
            patch.object(voice, "_single_tts", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            _tts()
    release.assert_called_once()


def test_narration_fails_without_recording_when_the_server_will_not_start():
    with patch.object(server, "ensure_running", return_value=False), \
            patch.object(server, "release") as release, \
            patch.object(voice, "_single_tts") as single:
        assert _tts() is None
    single.assert_not_called()
    release.assert_not_called()


def test_other_voices_never_touch_the_container():
    with patch.object(server, "ensure_running") as start, \
            patch.object(server, "release") as release, \
            patch.object(voice, "_single_tts", return_value="made"):
        assert _tts("en-US-AvaNeural") == "made"
    start.assert_not_called()
    release.assert_not_called()


# --- a long narration goes out in pieces --------------------------------------

def test_sentences_are_packed_up_to_the_budget_and_never_cut():
    text = "One two three. Four five six! Seven eight nine? Ten."
    assert voice._split_at_sentences(text, 1000) == [text]
    assert voice._split_at_sentences(text, 30) == [
        "One two three. Four five six!", "Seven eight nine? Ten."]
    # A sentence over the budget stays whole rather than being cut mid phrase.
    assert voice._split_at_sentences("A very long single sentence here. Short.", 10) == [
        "A very long single sentence here.", "Short."]
    assert voice._split_at_sentences("   ", 10) == []


def test_chatterbox_always_records_through_the_piecewise_path():
    for script in ("No pauses at all.", "Part 1. [pause: 2s] Part 2."):
        with patch.object(server, "ensure_running", return_value=True), \
                patch.object(server, "release"), \
                patch.object(voice, "_tts_with_pauses", return_value="pieces") as pieces:
            assert voice.tts(text=script, voice_name="chatterbox:narrator-Male",
                             voice_rate=1.0, voice_file="x.mp3") == "pieces"
        assert pieces.call_args.kwargs["max_speech_chars"] == voice.CHATTERBOX_MAX_REQUEST_CHARS


def test_a_long_narration_is_one_request_per_piece(tmp_path):
    sentence = "This sentence is exactly forty characters."
    script = " ".join([sentence] * 60)  # ~2500 characters
    asked = []

    def record(text, voice_name, voice_rate, voice_file, voice_volume=1.0, **_):
        asked.append(text)
        return None  # stop after the first piece; the split is what is under test

    with patch.object(voice, "_single_tts", side_effect=record):
        assert voice._tts_with_pauses(
            text=script, voice_name="chatterbox:x", voice_rate=1.0,
            voice_file=str(tmp_path / "out.mp3"),
            max_speech_chars=voice.CHATTERBOX_MAX_REQUEST_CHARS) is None
    assert len(asked) == 1
    assert len(asked[0]) <= voice.CHATTERBOX_MAX_REQUEST_CHARS
    assert asked[0].endswith(".")
