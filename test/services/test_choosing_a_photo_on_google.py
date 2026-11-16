"""照着照片重画：在一个小浏览器窗口里的 Google 图片上，点哪张用哪张。

钉住的事：

* 不走任何搜索 API：窗口是这个程序在本机上开的，搜的是框里那几个词；
* 页面里那段脚本只认"原图够大的图"，点中的那张交回来；
* 交回来的图走"传一张自己的"那条路——同一套检查、同一种凭据——只是记得
  自己是哪来的；
* 这张用不了（太小、不是图）时错误回到那个窗口里，让用户接着挑；
* 找不到浏览器、配置写错，都点名报错，不悄悄换成别的；
* 编辑器：点按钮就开，每秒问一次，点了就直接开画，关窗就关浏览器；
* 加，不替换：原来的搜索、"替我挑一张"、上传，都还在。

"Redraw from a photo" opens a small browser window, started by the app on this
computer, on Google Images. The picture that is clicked comes back through the
same checks as an uploaded one. No search API or key is involved.
"""

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app.config import config
from app.services import photo_browser, reference_image, scene_editor

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"
JS = (EDITOR / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (EDITOR / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")


def _body(name: str) -> str:
    start = JS.index(f"function {name}(")
    ends = [at for at in (JS.find("\nfunction ", start + 1),
                          JS.find("\nasync function ", start + 1)) if at > 0]
    return JS[start:min(ends)]


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def no_browser_settings():
    saved = {key: config.app.get(key) for key in
             ("photo_browser_path", "photo_browser_search_url")}
    for key in saved:
        config.app.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            config.app.pop(key, None)
        else:
            config.app[key] = value


class TestWhereItSearches:
    def test_google_images_by_default(self, no_browser_settings):
        assert photo_browser.search_url("  red   apple ") == (
            "https://www.google.com/search?udm=2&q=red+apple"
        )

    def test_the_words_cannot_break_out_of_the_address(self, no_browser_settings):
        assert photo_browser.search_url("a&b=c #d").endswith("q=a%26b%3Dc+%23d")

    def test_another_image_search_can_be_named(self, no_browser_settings):
        config.app["photo_browser_search_url"] = "https://example.test/i?q={words}"
        assert photo_browser.search_url("red apple") == (
            "https://example.test/i?q=red+apple"
        )

    @pytest.mark.parametrize("wrong", ["https://example.test/i", "file:///x?{words}"])
    def test_a_wrong_setting_is_named_not_replaced_by_google(
        self, no_browser_settings, wrong
    ):
        config.app["photo_browser_search_url"] = wrong
        with pytest.raises(photo_browser.PhotoBrowserError,
                           match="photo_browser_search_url"):
            photo_browser.search_url("red apple")


class TestWhichBrowser:
    def test_the_one_in_the_settings_is_used(self, no_browser_settings, tmp_path):
        program = tmp_path / "my-browser.exe"
        program.write_bytes(b"x")
        config.app["photo_browser_path"] = str(program)
        assert photo_browser.find_browser() == str(program)

    def test_a_setting_that_points_nowhere_is_named(self, no_browser_settings, tmp_path):
        config.app["photo_browser_path"] = str(tmp_path / "gone.exe")
        with pytest.raises(photo_browser.PhotoBrowserError,
                           match="photo_browser_path"):
            photo_browser.find_browser()

    def test_no_browser_at_all_says_what_is_needed(self, no_browser_settings):
        with patch.object(photo_browser.os.path, "isfile", return_value=False), \
                patch.object(photo_browser.shutil, "which", return_value=None):
            with pytest.raises(photo_browser.PhotoBrowserError,
                               match="Chrome or Edge"):
                photo_browser.find_browser()


class TestTheScriptInThePage:
    def test_only_a_picture_large_enough_can_be_chosen(self):
        assert "Math.max(img.naturalWidth, img.naturalHeight) >= MIN" in (
            photo_browser.INJECTED
        )

    def test_a_small_picture_keeps_doing_what_the_site_does(self):
        """缩略图不够大：点它不拦，还是网站自己的行为——打开大图。"""
        click = photo_browser.INJECTED.split('addEventListener("click"')[1]
        assert click.index("if (!img) return;") < click.index("event.preventDefault();")

    def test_the_chosen_one_is_handed_over_with_where_it_was(self):
        click = photo_browser.INJECTED.split('addEventListener("click"')[1]
        assert "window.__BINDING__(JSON.stringify({" in click
        assert "src: img.currentSrc || img.src, page: location.href" in click

    def test_it_does_not_depend_on_how_google_builds_its_page(self):
        for word in ("querySelector", "getElementById", "google"):
            assert word not in photo_browser.INJECTED


class TestTheSession:
    def test_nothing_to_search_for_opens_nothing(self):
        with patch.object(photo_browser._Session, "launch") as launch:
            with pytest.raises(photo_browser.PhotoBrowserError, match="no words"):
                photo_browser.start("t", "   ", lambda raw, chosen: {})
        launch.assert_not_called()

    def test_a_video_that_opened_no_window_has_none(self):
        assert photo_browser.status("never-opened")["state"] == "none"
        assert photo_browser.close("never-opened")["state"] == "none"

    def test_a_picture_that_cannot_be_used_is_said_in_the_window(self):
        """收不下就在那个窗口里说为什么，窗口不关，用户接着挑。"""
        def refuse(raw, chosen):
            raise ValueError("is only 200x100")

        session = photo_browser._Session("t", "apple", refuse, 512)
        session.state = "open"
        said = []
        with patch.object(session, "_bytes_of", return_value=b"x"), \
                patch.object(session, "_say",
                             side_effect=lambda text, bad: said.append((text, bad))):
            assert session._picked('{"src": "https://a.test/x.png"}') is False
        assert session.state == "open"
        assert said and said[0][1] is True and "is only 200x100" in said[0][0]

    def test_a_picture_that_can_be_used_ends_it(self):
        session = photo_browser._Session(
            "t", "apple", lambda raw, chosen: {"file": "tok"}, 512
        )
        session.state = "open"
        with patch.object(session, "_bytes_of", return_value=b"x"), \
                patch.object(session, "_say"):
            assert session._picked('{"src": "https://a.test/x.png"}') is True
        assert session.describe()["state"] == "picked"
        assert session.describe()["file"] == "tok"

    def test_a_picture_inlined_in_the_page_needs_no_download(self):
        session = photo_browser._Session("t", "apple", lambda r, c: {}, 512)
        assert session._bytes_of("data:image/png;base64,aGVsbG8=") == b"hello"

    def test_the_bytes_come_from_the_browser_not_from_the_server(self):
        """服务端不替人去访问任意网址：字节是从那个浏览器里取的。"""
        source = (ROOT / "app/services/photo_browser.py").read_text(encoding="utf-8")
        taken = source.split("def _bytes_of(")[1].split("\n    def ")[0]
        assert "Page.getResourceContent" in taken
        for fetcher in ("urlopen", "requests.", "httpx.", "_open_local("):
            assert fetcher not in taken


class TestWhatTheEditorDoesWithIt:
    def _keep(self, task_root, search="red apple"):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(photo_browser, "start",
                          return_value={"state": "open"}) as start:
            scene_editor.browse_for_photo(task_id, 0, search=search)
        return task_id, start

    def test_it_searches_the_words_it_was_given(self, task_root):
        task_id, start = self._keep(task_root, "  red   apple ")
        assert start.call_args.args[:2] == (task_id, "red apple")
        assert start.call_args.kwargs["min_edge"] == reference_image.MIN_LONG_EDGE

    def test_the_clicked_picture_is_kept_like_an_uploaded_one(self, task_root):
        _, start = self._keep(task_root)
        keep = start.call_args.args[2]
        kept = keep(_png(800, 600), {"src": "https://site.test/a.png",
                                     "page": "https://www.google.com/search?q=x"})
        assert (kept["width"], kept["height"]) == (800, 600)
        reference = scene_editor.uploaded_reference_photo(kept["file"])
        assert reference.provider == reference_image.UPLOAD
        # 它记得自己是哪来的，搜的是什么。
        assert reference.source_url == "https://site.test/a.png"
        assert reference.query == "red apple"

    def test_a_thumbnail_is_refused(self, task_root):
        _, start = self._keep(task_root)
        keep = start.call_args.args[2]
        with pytest.raises(reference_image.ReferenceImageError, match="long edge"):
            keep(_png(200, 120), {"src": "data:image/png;base64,xx"})

    def test_a_browser_that_cannot_open_is_named(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            photo_browser, "start",
            side_effect=photo_browser.PhotoBrowserError("needs Chrome or Edge"),
        ):
            with pytest.raises(scene_editor.SceneError, match="Chrome or Edge"):
                scene_editor.browse_for_photo(task_id, 0, search="apple")

    def test_over_http(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        client = TestClient(asgi.app)
        with patch.object(photo_browser, "start",
                          return_value={"state": "open", "session": "s"}):
            opened = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/photos/browse?search=apple"
            )
        assert opened.status_code == 200, opened.text
        assert opened.json()["data"]["state"] == "open"
        seen = client.get(f"/api/v1/tasks/{task_id}/photos/browse")
        assert seen.status_code == 200 and seen.json()["data"]["state"] == "none"
        assert client.delete(
            f"/api/v1/tasks/{task_id}/photos/browse").status_code == 200


class TestThePage:
    def test_every_element_the_script_looks_up_is_on_the_page(self):
        for dom_id in ("photo-google", "photo-google-note"):
            assert f'id="{dom_id}"' in HTML
            assert f'el("{dom_id}")' in JS

    def test_opening_the_picker_opens_the_browser(self):
        assert "if (before) browseGoogle(before);" in _body("openPhotoPicker")
        written = _body("writePhotoWords")
        assert written.index("ui.photoSearch.value = words;") < written.index(
            "browseGoogle(words);"
        )

    def test_the_clicked_picture_is_drawn_from_at_once(self):
        body = _body("watchPhotoBrowser")
        assert 'data.state === "picked" && data.file' in body
        assert "redrawFromPhoto({ photo_upload: data.file });" in body

    def test_an_answer_about_an_older_window_is_ignored(self):
        assert "data.session === browsing.session" in _body("watchPhotoBrowser")

    def test_a_browser_that_cannot_open_is_shown_not_hidden(self):
        body = _body("browseGoogle")
        assert 'googleNote(String(error.message || error), "warn");' in body

    def test_closing_the_picker_closes_the_browser(self):
        assert "closePhotoBrowser();" in _body("closePhotoPicker")
        assert 'call("DELETE", photoBrowserPath())' in _body("closePhotoBrowser")

    def test_a_pasted_or_dropped_picture_is_used_too(self):
        paste = JS.split('document.addEventListener("paste"')[1][:400]
        assert "takeCopiedPhoto(file);" in paste
        assert "/photos`, file)" in _body("takeCopiedPhoto")


class TestNothingWasTakenAway:
    def test_the_library_search_and_the_other_ways_are_still_there(self):
        for dom_id in ("photo-find", "photo-upload", "photo-auto", "photo-grid"):
            assert f'id="{dom_id}"' in HTML
        # 图库的搜索不再自己跑，但按钮还在，点了照旧搜。
        assert "findPhotos(" not in _body("openPhotoPicker")
        assert "findPhotos(" not in _body("writePhotoWords")
        assert 'ui.photoFind.addEventListener("click", searchPhotosFromBox);' in JS
        assert "if (words) findPhotos(words);" in _body("searchPhotosFromBox")
