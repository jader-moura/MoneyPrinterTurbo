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

    def test_the_browser_opens_only_when_asked(self):
        assert "browseGoogle(" not in _body("openPhotoPicker")
        assert "browseGoogle(" not in _body("writePhotoWords")
        clicked = JS.split('ui.photoGoogle.addEventListener("click"')[1][:400]
        assert "browseGoogle(words);" in clicked

    def test_google_opens_in_the_users_own_browser(self):
        """服务端另开的那个浏览器过不了 Google 的验证码。窗口开在用户自己
        的浏览器里，服务端什么都不开。"""
        body = _body("browseGoogle")
        assert "window.open(" in body
        assert "/photos/browse" not in JS

    def test_a_blocked_window_is_shown_not_hidden(self):
        body = _body("browseGoogle")
        assert "if (!photoWindow) {" in body
        assert "Allow pop-ups" in body

    def test_closing_the_picker_closes_the_window(self):
        assert "closePhotoBrowser();" in _body("closePhotoPicker")
        assert "opened.close();" in _body("closePhotoBrowser")

    def test_a_dragged_or_copied_address_is_downloaded_by_the_server(self):
        assert "/photos/address`" in _body("takePhotoAddress")
        assert "redrawFromPhoto({ photo_upload: data.file });" in _body(
            "takePhotoAddress")
        # Google 的格子拖过来是一个链接；原图在它的 imgurl 里。
        assert '.get("imgurl")' in _body("photoAddressIn")
        drop = JS.split('ui.photoModal.addEventListener("drop"')[1][:600]
        assert "takePhotoAddress(address);" in drop
        paste = JS.split('document.addEventListener("paste"')[1][:800]
        assert "takePhotoAddress(text);" in paste
        # 往搜索框里贴的普通词不拦。
        assert "if (!isPhotoAddress(text)) return;" in paste


class TestAPictureFromItsAddress:
    def test_it_is_downloaded_and_kept_like_an_upload(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi
        from app.services import reference_image, scene_editor

        task_id, _ = _reviewed_task(task_root)
        photo = reference_image.Reference(
            data=b"png", query="apple", source_url="https://example.com/a.jpg",
            page_url="https://example.com/a.jpg", provider="", width=1200,
            height=800,
        )
        with patch.object(reference_image, "chosen", return_value=photo) as chosen:
            answer = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/photos/address",
                json={"url": " https://example.com/a.jpg ", "search": "apple"},
            )
        assert answer.status_code == 200, answer.text
        assert chosen.call_args.args == ({"url": "https://example.com/a.jpg"}, "apple")
        data = answer.json()["data"]
        assert (data["width"], data["height"]) == (1200, 800)
        assert scene_editor.uploaded_reference_photo(data["file"]) is photo

    def test_one_that_cannot_be_downloaded_says_why(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi
        from app.services import reference_image

        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            reference_image, "chosen",
            side_effect=reference_image.ReferenceImageError("it answered 403"),
        ):
            answer = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/photos/address",
                json={"url": "https://example.com/a.jpg"},
            )
        assert answer.status_code != 200 or answer.json()["status"] != 200
        assert "403" in answer.text

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


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, "PNG")
    return out.getvalue()


class TestAPictureClickedInTheUsersOwnBrowser:
    """装了扩展的话，在自己浏览器的 Google 图片窗口里点大图就行：扩展把图
    交给服务端，编辑器来取。"""

    def _client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_clicked_picture_reaches_the_editor_once(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        client = self._client()
        path = f"/api/v1/tasks/{task_id}/photos/picked"
        assert client.get(path, params={"session": "s1"}).json()["data"] == {
            "state": "waiting"
        }
        sent = client.post(
            path, files={"file": ("picked-photo", _png(1200, 800), "image/png")},
            data={"source": "https://example.com/a.jpg",
                  "page": "https://www.google.com/search?q=apple", "session": "s1"},
        )
        assert sent.status_code == 200, sent.text
        # 别的那一次搜索点的不算。
        assert client.get(path, params={"session": "old"}).json()["data"][
            "state"] == "waiting"
        got = client.get(path, params={"session": "s1"}).json()["data"]
        assert got["state"] == "picked"
        assert (got["width"], got["height"]) == (1200, 800)
        photo = scene_editor.uploaded_reference_photo(got["file"])
        assert photo.source_url == "https://example.com/a.jpg"
        # 只交一次。
        assert client.get(path, params={"session": "s1"}).json()["data"][
            "state"] == "waiting"

    def test_a_picture_too_small_is_refused_with_the_reason(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        sent = self._client().post(
            f"/api/v1/tasks/{task_id}/photos/picked",
            files={"file": ("picked-photo", _png(200, 150), "image/png")},
            data={"session": "s1"},
        )
        assert sent.status_code != 200
        assert "long edge" in sent.text

    def test_the_page_arms_the_window_and_waits_for_the_click(self):
        body = _body("browseGoogle")
        assert "#mpt-pick=${marker}" in body
        assert "watchPhotoPick(photoPick.at, session);" in body
        watch = _body("watchPhotoPick")
        assert "/photos/picked?session=" in watch
        assert "redrawFromPhoto({ photo_upload: data.file });" in watch
        assert "stopWatchingPhotoPick();" in _body("closePhotoBrowser")

    def test_the_extension_is_there_and_only_acts_in_that_window(self):
        import json

        folder = ROOT / "browser-extension" / "photo-picker"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["manifest_version"] == 3
        content = (folder / "content.js").read_text(encoding="utf-8")
        background = (folder / "background.js").read_text(encoding="utf-8")
        assert '"#mpt-pick="' in content
        # 不是编辑器开的窗口，什么都不做。
        assert "if (!to || to.error || window.top !== window) return;" in content
        assert "dataset.mptPhotoPicker" in content
        assert "dataset.mptPhotoPicker" in JS
        assert "/photos/picked`" in background


class TestTheExtensionIsLetIn:
    """扩展的请求带着 ``Origin: chrome-extension://…``。只有交图的那一条路
    对它开；别的路、别的来源照旧被挡。"""

    def _post(self, task_root, path_tail, origin):
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        return TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/{path_tail}",
            files={"file": ("picked-photo", _png(1200, 800), "image/png")},
            data={"session": "s1"}, headers={"Origin": origin},
        )

    def test_the_extension_can_hand_over_a_picture(self, task_root):
        sent = self._post(task_root, "photos/picked", "chrome-extension://abcdefgh")
        assert sent.status_code == 200, sent.text

    def test_a_web_page_cannot(self, task_root):
        sent = self._post(task_root, "photos/picked", "https://evil.example")
        assert sent.status_code == 403

    def test_no_other_route_is_opened_to_extensions(self, task_root):
        sent = self._post(task_root, "photos", "chrome-extension://abcdefgh")
        assert sent.status_code == 403
