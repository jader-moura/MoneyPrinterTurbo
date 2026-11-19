"""在编辑器的"Sound"面板里往曲库加一首歌。

钉住的事：

* 传上来的文件走的还是 ``bgm.save_bgm_upload``（验格式、验大小、存成 UUID）；
  这里多做的只是记下它原来叫什么，给下拉框显示；
* 名字只是显示用的：记不上、读不出都不挡上传，也不影响别的曲子；
* 被拒的文件原样把原因说出来；服务端自己的毛病是 500，不赖用户的文件；
* 面板把名字连同曲库一起给页面，页面传完就选中那一首，但要点保存才用上。

A song can be added to the music library from the editor's Sound panel. It is
stored the way every upload is; the only addition is remembering its name.
"""

import io
import json
from pathlib import Path

import pytest

from app.services import bgm, scene_editor

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (PAGE / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture
def library(tmp_path, monkeypatch):
    folder = tmp_path / "bgm"
    folder.mkdir()
    monkeypatch.setattr(bgm, "uploaded_bgm_dir", lambda create=True: str(folder))
    return folder


class TestTheNameOfAnUpload:
    def test_it_is_remembered_beside_the_file(self, library):
        (library / "aaa.mp3").write_bytes(b"x")
        assert bgm.remember_uploaded_bgm_name("aaa.mp3", "Dark Ambient.mp3") == "Dark Ambient.mp3"
        assert bgm.uploaded_bgm_names() == {"aaa.mp3": "Dark Ambient.mp3"}

    def test_only_the_file_name_is_kept_not_a_path(self, library):
        (library / "aaa.mp3").write_bytes(b"x")
        bgm.remember_uploaded_bgm_name("aaa.mp3", "C:\\Users\\me\\Music\\Slow Piano.mp3")
        assert bgm.uploaded_bgm_names()["aaa.mp3"] == "Slow Piano.mp3"

    def test_a_song_without_a_name_shows_as_its_stored_name(self, library):
        (library / "aaa.mp3").write_bytes(b"x")
        assert bgm.remember_uploaded_bgm_name("aaa.mp3", "") == "aaa.mp3"

    def test_songs_deleted_from_disk_lose_their_name(self, library):
        (library / "aaa.mp3").write_bytes(b"x")
        (library / "bbb.mp3").write_bytes(b"x")
        bgm.remember_uploaded_bgm_name("aaa.mp3", "One.mp3")
        (library / "aaa.mp3").unlink()
        bgm.remember_uploaded_bgm_name("bbb.mp3", "Two.mp3")
        assert bgm.uploaded_bgm_names() == {"bbb.mp3": "Two.mp3"}

    def test_an_unreadable_record_is_no_names_not_an_error(self, library):
        (library / "names.json").write_text("{broken", encoding="utf-8")
        assert bgm.uploaded_bgm_names() == {}
        (library / "names.json").write_text(json.dumps(["a"]), encoding="utf-8")
        assert bgm.uploaded_bgm_names() == {}

    def test_the_record_is_not_listed_as_a_song(self, library):
        (library / "aaa.mp3").write_bytes(b"x")
        bgm.remember_uploaded_bgm_name("aaa.mp3", "One.mp3")
        assert "names.json" not in [
            Path(path).name for path in bgm._list_bgm_files((str(library),))]


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_a_song_is_stored_and_its_name_comes_back(self, client, library, monkeypatch):
        def stored(filename, source, **limits):
            seen.update(limits)
            (library / "f00d.mp3").write_bytes(source.read())
            return "f00d.mp3"

        monkeypatch.setattr(bgm, "save_bgm_upload", stored)
        seen = {}
        got = client.post("/api/v1/music", files={
            "file": ("Dark Ambient.mp3", io.BytesIO(b"song"), "audio/mpeg")})
        assert got.status_code == 200, got.text
        assert got.json()["data"] == {"file": "f00d.mp3", "name": "Dark Ambient.mp3"}
        assert bgm.uploaded_bgm_names() == {"f00d.mp3": "Dark Ambient.mp3"}
        # 编辑器里放宽到 200 MB；WebUI 那条路的 30 MB 不动。
        assert seen["max_bytes"] == 200 * 1024 * 1024
        assert bgm.MAX_BGM_UPLOAD_BYTES == 30 * 1024 * 1024


    def test_a_refused_file_says_why(self, client, library, monkeypatch):
        def refused(filename, source, **limits):
            raise bgm.BgmUploadError("unsupported background music format")

        monkeypatch.setattr(bgm, "save_bgm_upload", refused)
        got = client.post("/api/v1/music", files={
            "file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
        assert got.status_code == 400
        assert "unsupported background music format" in got.text
        assert bgm.uploaded_bgm_names() == {}

    def test_a_server_fault_is_not_blamed_on_the_file(self, client, library, monkeypatch):
        def broken(filename, source, **limits):
            raise bgm.BgmServiceError("ffmpeg at C:/secret/path is missing")

        monkeypatch.setattr(bgm, "save_bgm_upload", broken)
        got = client.post("/api/v1/music", files={
            "file": ("a.mp3", io.BytesIO(b"song"), "audio/mpeg")})
        assert got.status_code == 500
        assert "secret" not in got.text


class TestThePanel:
    def test_the_names_go_to_the_page_with_the_library(self):
        source = (ROOT / "app/services/scene_editor.py").read_text(encoding="utf-8")
        body = source.split("def background_music(")[1].split("\ndef ")[0]
        assert '"names": bgm.uploaded_bgm_names()' in body
        assert scene_editor.background_music.__doc__

    def test_the_sound_panel_has_a_place_to_add_a_song(self):
        panel = HTML.split('id="music-panel"')[1].split("</section>")[0]
        assert 'id="music-upload" type="file" accept="audio/*"' in panel
        assert panel.index('id="music-track"') < panel.index('id="music-upload"')

    def test_the_page_uploads_it_selects_it_and_waits_for_save(self):
        handler = JS.split('ui.musicUpload.addEventListener("change"')[1].split("\n});")[0]
        assert 'upload("/music", file)' in handler
        assert "track: data.file" in handler
        assert "Save sound to use it" in handler
        # 传完不自己存：用不用到这条片子上是"Save sound"的事。
        assert 'call("PUT"' not in handler

    def test_an_uploaded_song_shows_under_its_own_name(self):
        body = JS.split("\nfunction drawMusic(")[1].split("\n}\n")[0]
        assert "names[name] || name" in body

    def test_it_cannot_be_used_when_a_service_composes_the_music(self):
        body = JS.split("\nfunction drawMusic(")[1].split("\n}\n")[0]
        assert "ui.musicUpload.disabled = !!music.external" in body


class TestTheSizeLimit:
    def _song(self, megabytes):
        return io.BytesIO(b"x" * (megabytes * 1024 * 1024))

    def test_the_editor_takes_a_song_the_webui_would_refuse(self, library):
        name, staged, size = bgm._stage_bgm_upload(
            "long.mp3", self._song(31), max_bytes=bgm.MAX_EDITOR_BGM_UPLOAD_BYTES)
        assert size == 31 * 1024 * 1024
        bgm._remove_staged_file(staged)

    def test_without_a_larger_limit_thirty_megabytes_still_holds(self, library):
        with pytest.raises(bgm.BgmUploadError, match="exceeds the 30 MB limit"):
            bgm._stage_bgm_upload("long.mp3", self._song(31))

    def test_past_the_editors_limit_the_message_names_that_limit(self, library):
        with pytest.raises(bgm.BgmUploadError, match="exceeds the 2 MB limit"):
            bgm._stage_bgm_upload(
                "long.mp3", self._song(3), max_bytes=2 * 1024 * 1024)
