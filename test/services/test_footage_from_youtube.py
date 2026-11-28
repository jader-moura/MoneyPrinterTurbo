"""A few seconds of a YouTube video on a scene, without its sound.

The user watches the video in their own browser and says which part they want;
only that part is fetched and it is placed like a clip they uploaded.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import footage, scene_editor, scene_manifest
from test.services.test_scene_editor import (  # noqa: F401
    task_root,
)
from test.services.test_choosing_a_photo_on_google import _reviewed_task

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource" / "public" / "editor"
JS = (EDITOR / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (EDITOR / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
VIDEO = "dQw4w9WgXcQ"


class TestTheTimes:
    @pytest.mark.parametrize("written, seconds", [
        ("2:34", 154.0), ("154", 154.0), ("1:02:34", 3754.0), ("2:34.5", 154.5),
        (" 0:05 ", 5.0), (12.5, 12.5),
    ])
    def test_a_time_is_read_the_way_it_is_written(self, written, seconds):
        assert footage.seconds_of(written) == seconds

    @pytest.mark.parametrize("written", ["", "abc", "2:75", "1:2:3:4", "-5", "2:"])
    def test_what_is_not_a_time_is_refused(self, written):
        with pytest.raises(footage.FootageError):
            footage.seconds_of(written)

    def test_the_end_has_to_be_after_the_start(self):
        with pytest.raises(footage.FootageError, match="after the start"):
            footage.checked_part("2:38", "2:34")

    def test_a_part_cannot_be_longer_than_the_limit(self):
        with pytest.raises(footage.FootageError, match="at most"):
            footage.checked_part("0:00", "5:00")
        assert footage.checked_part("2:34", "2:38") == (154.0, 158.0)


class TestTheAddress:
    @pytest.mark.parametrize("address", [
        f"https://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}&t=154s",
        f"https://youtu.be/{VIDEO}?t=154",
        f"https://m.youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        VIDEO,
    ])
    def test_a_youtube_video_is_recognised(self, address):
        assert footage.video_id(address) == VIDEO

    @pytest.mark.parametrize("address", [
        "", "https://example.com/watch?v=" + VIDEO, "file:///etc/passwd",
        "https://www.youtube.com/results?search_query=cats",
        "https://notyoutube.com/watch?v=" + VIDEO,
    ])
    def test_anything_else_is_refused(self, address):
        with pytest.raises(footage.FootageError):
            footage.video_id(address)


class TestFetching:
    def test_only_the_part_is_fetched_and_it_is_cut_without_sound(self, tmp_path):
        seen = {}

        def download(address, begin, finish, folder, ffmpeg):
            seen["download"] = (address, begin, finish)
            part = os.path.join(folder, "part.mp4")
            Path(part).write_bytes(b"video")
            return part

        def run(command, **_):
            seen["command"] = command
            Path(command[-1]).write_bytes(b"cut")
            return type("Done", (), {"returncode": 0, "stderr": b""})()

        with patch.object(footage, "_download", side_effect=download), \
                patch.object(footage.subprocess, "run", side_effect=run), \
                patch.object(footage.utils, "get_ffmpeg_binary", return_value="ffmpeg"):
            clip = footage.fetch(f"https://youtu.be/{VIDEO}", "2:34", "2:38",
                                 str(tmp_path))
            again = footage.fetch(VIDEO, "154", "158", str(tmp_path))
        assert seen["download"] == (
            f"https://www.youtube.com/watch?v={VIDEO}", 154.0, 158.0)
        assert "-an" in seen["command"]
        assert seen["command"][seen["command"].index("-t") + 1] == "4.000"
        assert os.path.dirname(clip) == str(tmp_path) and clip.endswith(".mp4")
        # The same part of the same video is fetched once.
        assert again == clip

    def test_without_the_downloader_it_says_what_to_install(self, tmp_path):
        import builtins

        real = builtins.__import__

        def no_downloader(name, *args, **kwargs):
            if name.startswith("yt_dlp"):
                raise ImportError(name)
            return real(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=no_downloader):
            with pytest.raises(footage.FootageError, match="yt-dlp"):
                footage._download("https://youtu.be/x", 0, 4, str(tmp_path), "ffmpeg")


class TestOnAScene:
    def _client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_clip_is_placed_like_an_uploaded_one(self, task_root):
        from app.services import scene_video
        from app.utils import utils

        task_id, _ = _reviewed_task(task_root)
        clip = os.path.join(utils.task_dir(task_id), "footage-x.mp4")
        Path(clip).write_bytes(b"clip")
        with patch.object(footage, "fetch", return_value=clip) as fetch, \
                patch.object(scene_video, "fit_own_clip",
                             return_value={"fit": None}) as fit:
            answer = self._client().post(
                f"/api/v1/tasks/{task_id}/scenes/0/footage",
                json={"url": f"https://youtu.be/{VIDEO}", "start": "2:34",
                      "end": "2:38"},
            )
        assert answer.status_code == 200, answer.text
        assert fetch.call_args.args[:3] == (f"https://youtu.be/{VIDEO}", "2:34", "2:38")
        assert fit.called
        scene = scene_manifest.load(task_id)["scenes"][0]
        assert os.path.basename(scene["material"]) == "footage-x.mp4"
        assert scene["kind"] == "video" and scene["locked"] is True
        # What the scene showed before can be put back.
        assert scene.get("history")

    def test_a_wrong_address_says_so(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        answer = self._client().post(
            f"/api/v1/tasks/{task_id}/scenes/0/footage",
            json={"url": "https://example.com/video", "start": "0:01", "end": "0:05"},
        )
        assert answer.status_code != 200
        assert "YouTube" in answer.text

    def test_a_part_marked_in_the_browser_reaches_the_editor_once(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        client = self._client()
        path = f"/api/v1/tasks/{task_id}/footage/picked"
        sent = client.post(
            path, headers={"Origin": "chrome-extension://abcdefgh"},
            json={"session": "s1", "url": f"https://www.youtube.com/watch?v={VIDEO}",
                  "start": 154.0, "end": 158.2},
        )
        assert sent.status_code == 200, sent.text
        assert client.get(path, params={"session": "old"}).json()["data"] == {
            "state": "waiting"}
        got = client.get(path, params={"session": "s1"}).json()["data"]
        assert got == {"state": "picked", "start": 154.0, "end": 158.2,
                       "url": f"https://www.youtube.com/watch?v={VIDEO}"}
        assert client.get(path, params={"session": "s1"}).json()["data"][
            "state"] == "waiting"

    def test_a_web_page_cannot_send_a_part(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        sent = self._client().post(
            f"/api/v1/tasks/{task_id}/footage/picked",
            headers={"Origin": "https://evil.example"},
            json={"session": "s1", "url": VIDEO, "start": 1, "end": 5},
        )
        assert sent.status_code == 403


class TestThePage:
    def test_every_element_the_script_looks_up_is_on_the_page(self):
        for dom_id in ("footage-modal", "footage-title", "footage-line",
                       "footage-search", "footage-browse", "footage-how",
                       "footage-link", "footage-from", "footage-to",
                       "footage-note", "footage-add", "footage-close"):
            assert f'id="{dom_id}"' in HTML
            assert f'el("{dom_id}")' in JS

    def test_the_scene_has_a_button_for_it(self):
        assert 'button("Get footage…", "small", () => openFootage(scene));' in JS

    def test_opening_the_window_searches_and_fetches_nothing(self):
        opened = JS.split("function openFootage(scene) {")[1].split("\n}\n")[0]
        assert "window.open(" not in opened and "call(" not in opened

    def test_youtube_opens_in_the_users_own_browser(self):
        body = JS.split("function browseYouTube() {")[1].split("\n}\n")[0]
        assert "window.open(" in body and "#mpt-pick=${marker}" in body
        assert 'kind: "footage"' in body

    def test_the_part_is_asked_for_with_the_link_and_the_two_times(self):
        body = JS.split("function addFootage() {")[1].split("\n}\n")[0]
        assert 'scenePath(at) + "/footage", { url, start, end }' in body

    def test_the_extension_marks_the_part_on_the_video(self):
        folder = ROOT / "browser-extension" / "photo-picker"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert tuple(int(n) for n in manifest["version"].split(".")[:2]) >= (1, 1)
        content = (folder / "content.js").read_text(encoding="utf-8")
        background = (folder / "background.js").read_text(encoding="utf-8")
        assert 'if (to.kind === "footage") { footageBar(); return; }' in content
        assert "video.currentTime" in content
        assert "/footage/picked`" in background
