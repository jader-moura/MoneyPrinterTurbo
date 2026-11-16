"""照着照片重画：先把搜到的摆出来给人挑，也可以自己传一张。

以前这一步是全自动的——搜一遍、拿第一张下得来的、直接开画，三分钟之后才
看到它拿的是哪一张。这里钉住的是：

* 搜索只列结果，不下载、不开画；
* 挑中哪一张就画哪一张，这张下不来就说这张下不来，不换一张；
* 只认刚才搜出来给人看过的地址——否则这就是一个让服务器去访问任意网址的
  接口；
* 传上来的照片和下载来的走同一套检查；
* 原来那条全自动的路还在，一个字没变。

Redraw from a photo: the search results are shown for the user to choose from,
or they upload their own. Searching draws nothing, the chosen photo is the one
used (never silently swapped), only an address that was actually offered can be
fetched, an upload gets the same checks as a download, and the old automatic
path still works unchanged.
"""

import io
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import reference_image, scene_editor, scene_regen_queue

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]

OPENVERSE = [
    {"url": "https://img.example/a.jpg", "foreign_landing_url": "https://site.example/a",
     "thumbnail": "https://api.example/thumb/a", "width": 2000, "height": 1500,
     "license": "by", "license_version": "2.0", "creator": "Ann"},
    {"url": "https://img.example/b.jpg", "foreign_landing_url": "https://site.example/b",
     "width": 1600, "height": 1200, "creator": "Bo"},
    {"url": "https://img.example/tiny.jpg", "width": 200, "height": 150},
]


def _png(width=800, height=600) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def openverse():
    """Openverse 答了这三条。测试不去敲真的 Openverse。"""
    class Reply:
        status_code = 200

        def json(self):
            return {"results": OPENVERSE}

    with patch.object(reference_image, "_setting",
                      side_effect=lambda cfg, key, default="": default), \
            patch.object(reference_image.requests, "get", return_value=Reply()) as get:
        yield get


@pytest.fixture(autouse=True)
def clean_slate():
    scene_editor._OFFERED.clear()
    scene_editor._UPLOADED.clear()
    yield
    scene_editor._OFFERED.clear()
    scene_editor._UPLOADED.clear()


class TestSearchingOnlyLists:
    def test_the_results_are_listed_with_where_they_come_from(self, openverse):
        found = reference_image.candidates("red apple")
        assert found["query"] == "red apple"
        assert found["provider"] == reference_image.OPENVERSE
        first = found["photos"][0]
        assert first["url"] == "https://img.example/a.jpg"
        assert first["thumb"] == "https://api.example/thumb/a"
        assert first["page_url"] == "https://site.example/a"
        assert first["host"] == "site.example"
        assert first["creator"] == "Ann"

    def test_nothing_is_downloaded(self, openverse):
        with patch.object(reference_image, "_fetch") as fetch:
            reference_image.candidates("red apple")
        fetch.assert_not_called()
        # 只有那一次搜索请求。
        assert openverse.call_count == 1

    def test_a_result_without_a_thumbnail_shows_the_picture_itself(self, openverse):
        photos = reference_image.candidates("red apple")["photos"]
        assert photos[1]["thumb"] == photos[1]["url"]

    def test_pictures_too_small_to_draw_from_are_left_out(self, openverse):
        urls = [p["url"] for p in reference_image.candidates("red apple")["photos"]]
        assert "https://img.example/tiny.jpg" not in urls

    def test_one_used_by_another_scene_is_shown_and_marked_not_hidden(
        self, openverse
    ):
        """挑不挑由人决定。自动那条路才是直接跳过。"""
        photos = reference_image.candidates(
            "red apple", avoid=["https://img.example/a.jpg"]
        )["photos"]
        assert [p["used"] for p in photos] == [True, False]

    def test_an_empty_search_is_refused(self):
        with pytest.raises(reference_image.ReferenceImageError, match="empty"):
            reference_image.candidates("   ")

    def test_the_automatic_path_still_takes_the_first_usable_one(self, openverse):
        with patch.object(reference_image, "_fetch",
                          return_value=(b"png", 800, 600)) as fetch:
            got = reference_image.search("red apple")
        assert got.source_url == "https://img.example/a.jpg"
        assert got.creator == "Ann"
        fetch.assert_called_once()


class TestTheChosenOneIsTheOneUsed:
    def test_it_is_downloaded_with_the_same_checks(self, openverse):
        photo = reference_image.candidates("red apple")["photos"][1]
        with patch.object(reference_image, "_fetch",
                          return_value=(b"png", 1600, 1200)) as fetch:
            got = reference_image.chosen({**photo, "query": "red apple"})
        fetch.assert_called_once_with("https://img.example/b.jpg")
        assert got.source_url == "https://img.example/b.jpg"
        assert got.page_url == "https://site.example/b"
        assert (got.width, got.height, got.query) == (1600, 1200, "red apple")

    def test_if_it_cannot_be_used_that_is_said_and_no_other_is_taken(
        self, openverse
    ):
        photo = reference_image.candidates("red apple")["photos"][0]
        refused = reference_image.ReferenceImageError("answered 403")
        with patch.object(reference_image, "_fetch", side_effect=refused) as fetch:
            with pytest.raises(reference_image.ReferenceImageError) as raised:
                reference_image.chosen(photo)
        assert "you chose" in str(raised.value) and "403" in str(raised.value)
        assert fetch.call_count == 1

    def test_only_an_address_that_was_offered_can_be_fetched(
        self, task_root, openverse
    ):
        """否则"照这个地址下一张图"就是一个让服务器访问任意网址的接口。"""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.find_reference_photos(task_id, 0, search="red apple")
        with patch.object(reference_image, "_fetch") as fetch:
            for url in ("http://127.0.0.1:8080/api/v1/settings",
                        "http://169.254.169.254/latest/meta-data",
                        "https://img.example/never-offered.jpg", ""):
                with pytest.raises(scene_editor.SceneError, match="search results"):
                    scene_editor.chosen_reference_photo(task_id, url)
        fetch.assert_not_called()

    def test_what_was_offered_to_one_video_is_not_offered_to_another(
        self, task_root, openverse
    ):
        task_id, _ = _reviewed_task(task_root, task_id="one")
        other, _ = _reviewed_task(task_root, task_id="two")
        scene_editor.find_reference_photos(task_id, 0, search="red apple")
        with pytest.raises(scene_editor.SceneError):
            scene_editor.chosen_reference_photo(other, "https://img.example/a.jpg")

    def test_an_offered_one_is_fetched(self, task_root, openverse):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.find_reference_photos(task_id, 0, search="red apple")
        with patch.object(reference_image, "_fetch",
                          return_value=(b"png", 2000, 1500)):
            got = scene_editor.chosen_reference_photo(
                task_id, "https://img.example/a.jpg"
            )
        assert got.source_url == "https://img.example/a.jpg"
        assert got.query == "red apple"

    def test_searching_with_my_own_words_does_not_ask_the_model(
        self, task_root, openverse
    ):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.llm, "image_search_query") as written:
            scene_editor.find_reference_photos(task_id, 0, search="red apple")
        written.assert_not_called()

    def test_with_no_words_the_model_writes_them(self, task_root, openverse):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.llm, "image_search_query",
                          return_value="apple orchard") as written:
            found = scene_editor.find_reference_photos(task_id, 0)
        written.assert_called_once()
        assert found["query"] == "apple orchard"


class TestTheWordsToSearchWith:
    def test_a_scene_drawn_from_a_photo_before_reuses_those_words(self, task_root):
        from app.services import scene_manifest

        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {
            "query": "de Havilland Comet 1952", "provider": "openverse",
            "source_url": "https://img.example/a.jpg",
        }
        scene_manifest.save_scenes(task_id, scenes)
        with patch.object(scene_editor.llm, "image_search_query") as written:
            got = scene_editor.reference_photo_words(task_id, 0)
        assert got == {"query": "de Havilland Comet 1952", "from": "last"}
        written.assert_not_called()

    def test_the_editor_is_told_those_words_with_the_scene(self, task_root):
        from app.services import scene_manifest

        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {"query": "red apple", "provider": "openverse",
                                  "source_url": "https://img.example/a.jpg"}
        scene_manifest.save_scenes(task_id, scenes)
        with patch.object(scene_editor.timeline, "probe_duration", return_value=5.0):
            shown = scene_editor.describe(task_id, with_timing=True)["scenes"][0]
        assert shown["reference"]["query"] == "red apple"

    def test_otherwise_the_model_writes_them_and_nothing_is_searched(
        self, task_root
    ):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.llm, "image_search_query",
                          return_value="apple orchard"),                 patch.object(reference_image.requests, "get") as get:
            got = scene_editor.reference_photo_words(task_id, 0)
        assert got == {"query": "apple orchard", "from": "model"}
        get.assert_not_called()

    def test_over_http(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.llm, "image_search_query",
                          return_value="apple orchard"):
            got = TestClient(asgi.app).get(
                f"/api/v1/tasks/{task_id}/scenes/0/photos/words"
            )
        assert got.status_code == 200, got.text
        assert got.json()["data"]["query"] == "apple orchard"


class TestMyOwnPhoto:
    def test_it_gets_the_same_treatment_as_a_downloaded_one(self):
        got = reference_image.uploaded(_png(900, 700), "mine.png")
        assert got.provider == reference_image.UPLOAD
        assert (got.width, got.height) == (900, 700)
        assert got.source_url == "" and got.query == ""
        # 重新编码过的 PNG。
        assert got.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_a_file_that_is_not_a_picture_is_refused_by_name(self):
        with pytest.raises(reference_image.ReferenceImageError, match="notes.txt"):
            reference_image.uploaded(b"just some text", "notes.txt")

    def test_an_empty_or_oversized_file_is_refused(self):
        with pytest.raises(reference_image.ReferenceImageError, match="empty"):
            reference_image.uploaded(b"")
        with pytest.raises(reference_image.ReferenceImageError, match="limit"):
            reference_image.uploaded(b"x" * (reference_image.MAX_BYTES + 1))

    def test_it_is_kept_until_it_is_used(self):
        kept = scene_editor.keep_uploaded_photo(_png(), "mine.png")
        assert kept["width"] == 800 and kept["name"] == "mine.png"
        got = scene_editor.uploaded_reference_photo(kept["file"])
        assert got.provider == reference_image.UPLOAD

    def test_a_name_that_was_never_handed_out_finds_nothing(self):
        with pytest.raises(scene_editor.SceneError, match="upload it again"):
            scene_editor.uploaded_reference_photo("../../config.toml")

    def test_only_the_last_few_are_kept(self):
        tokens = [scene_editor.keep_uploaded_photo(_png(), f"{n}.png")["file"]
                  for n in range(scene_editor._UPLOADED_KEPT + 2)]
        assert len(scene_editor._UPLOADED) == scene_editor._UPLOADED_KEPT
        with pytest.raises(scene_editor.SceneError):
            scene_editor.uploaded_reference_photo(tokens[0])


class TestTheQueueDrawsFromIt:
    def test_a_chosen_photo_is_used_without_searching_again(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        photo = reference_image.uploaded(_png(), "mine.png")
        drawn = {}

        def fake_regenerate(task, index, **kwargs):
            drawn.update(kwargs)
            return {"index": index}

        with patch.object(scene_editor, "find_reference_photo") as search, \
                patch.object(scene_editor, "regenerate_material",
                             side_effect=fake_regenerate), \
                patch.object(scene_regen_queue, "_ensure_worker"):
            job = scene_regen_queue.enqueue(task_id, 0, photo=photo, strength=0.4)
            assert job["from_photo"] is True
            # 排着的时候界面就能显示"照的是哪一张"。
            assert job["reference"]["provider"] == reference_image.UPLOAD
            with scene_regen_queue._LOCK:
                queued = scene_regen_queue._QUEUE.pop()
            scene_regen_queue._run_material(queued)
        search.assert_not_called()
        assert drawn["reference"] is photo
        assert drawn["strength"] == 0.4

    def test_the_bytes_of_the_photo_are_not_sent_to_the_page(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        photo = reference_image.uploaded(_png(), "mine.png")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            job = scene_regen_queue.enqueue(task_id, 0, photo=photo)
            with scene_regen_queue._LOCK:
                scene_regen_queue._QUEUE.pop()
        assert "photo" not in job
        assert b"PNG" not in repr(job).encode()


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_search_then_choose(self, client, task_root, openverse):
        task_id, _ = _reviewed_task(task_root)
        base = f"/api/v1/tasks/{task_id}"
        found = client.get(f"{base}/scenes/0/photos", params={"search": "red apple"})
        assert found.status_code == 200, found.text
        url = found.json()["data"]["photos"][0]["url"]

        queued = {}

        def fake_enqueue(task, index, **kwargs):
            queued.update(kwargs)
            return {"id": "j1", "position": 1}

        with patch.object(reference_image, "_fetch",
                          return_value=(b"png", 2000, 1500)), \
                patch.object(scene_regen_queue, "enqueue", side_effect=fake_enqueue):
            got = client.post(f"{base}/scenes/0/material/queue",
                              json={"from_photo": True, "photo_url": url})
        assert got.status_code == 200, got.text
        assert queued["photo"].source_url == url

    def test_an_address_that_was_not_offered_is_refused_before_queueing(
        self, client, task_root
    ):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_regen_queue, "enqueue") as enqueue, \
                patch.object(reference_image, "_fetch") as fetch:
            got = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
                json={"from_photo": True, "photo_url": "http://127.0.0.1/secret"},
            )
        assert got.status_code == 400
        assert "search results" in got.json()["message"]
        enqueue.assert_not_called()
        fetch.assert_not_called()

    def test_upload_then_draw_from_it(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        base = f"/api/v1/tasks/{task_id}"
        up = client.post(f"{base}/photos",
                         files={"file": ("mine.png", _png(), "image/png")})
        assert up.status_code == 200, up.text
        token = up.json()["data"]["file"]

        queued = {}
        with patch.object(scene_regen_queue, "enqueue",
                          side_effect=lambda t, i, **kw: queued.update(kw)
                          or {"id": "j1", "position": 1}):
            got = client.post(f"{base}/scenes/0/material/queue",
                              json={"from_photo": True, "photo_upload": token})
        assert got.status_code == 200, got.text
        assert queued["photo"].provider == reference_image.UPLOAD

    def test_a_bad_upload_is_a_400_with_the_reason(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        got = client.post(f"/api/v1/tasks/{task_id}/photos",
                          files={"file": ("notes.txt", b"hello", "text/plain")})
        assert got.status_code == 400
        assert "notes.txt" in got.json()["message"]

    def test_without_a_choice_the_request_is_what_it_always_was(
        self, client, task_root
    ):
        """老的调用方不带这两项，行为一个字不变。"""
        task_id, _ = _reviewed_task(task_root)
        queued = {}
        with patch.object(scene_regen_queue, "enqueue",
                          side_effect=lambda t, i, **kw: queued.update(kw)
                          or {"id": "j1", "position": 1}):
            got = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
                json={"from_photo": True, "search": "red apple"},
            )
        assert got.status_code == 200, got.text
        assert queued["photo"] is None
        assert queued["from_photo"] is True and queued["search"] == "red apple"


class TestTheEditorOffersIt:
    @staticmethod
    def _script() -> str:
        return (ROOT / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _body(source: str, name: str) -> str:
        start = source.index(f"function {name}(")
        ends = [at for at in (source.find("\nfunction ", start + 1),
                              source.find("\nasync function ", start + 1)) if at > 0]
        return source[start:min(ends)]

    def test_the_button_opens_the_picker_instead_of_drawing_at_once(self):
        script = self._script()
        start = script.index('const fromPhoto = button("Redraw from a photo"')
        handler = script[start:script.index("fromPhoto.title", start)]
        assert "openPhotoPicker(" in handler
        assert "queueRegeneration(" not in handler

    def test_clicking_a_result_draws_from_that_one(self):
        body = self._body(self._script(), "drawPhotos")
        assert "redrawFromPhoto({ photo_url: photo.url })" in body

    def test_my_own_photo_is_uploaded_then_drawn_from(self):
        script = self._script()
        start = script.index('ui.photoUpload.addEventListener("click"')
        handler = script[start:script.index("ui.photoModal.addEventListener", start)]
        assert "/photos`, file)" in handler
        assert "redrawFromPhoto({ photo_upload: data.file })" in handler

    def test_the_automatic_way_is_still_there(self):
        script = self._script()
        assert 'ui.photoAuto.addEventListener("click", () => redrawFromPhoto(null));' in (
            script
        )

    def test_the_search_words_can_be_changed_and_searched_again(self):
        script = self._script()
        assert 'ui.photoFind.addEventListener("click", searchPhotosFromBox);' in script
        assert "if (words) findPhotos(words);" in self._body(
            script, "searchPhotosFromBox"
        )
        find = self._body(script, "findPhotos")
        # 模型写出来的词填回框里，不合适就改了再搜。
        assert "ui.photoSearch.value = (data && data.query) || search;" in find
        # 搜不到的时候词还在框里。
        assert "ui.photoSearch.value = search;" in find

    def test_the_box_opens_filled_with_what_was_searched_before(self):
        """打开时框里得是"之前搜的那个"：刚才在这里搜的，没有的话是上次照着
        照片画这一段用的词；都没有才让模型写。"""
        body = self._body(self._script(), "openPhotoPicker")
        assert "photoPick.words[photoPick.key]" in body
        assert "scene.reference.query" in body
        assert body.index("photoPick.words[photoPick.key]") < body.index(
            "scene.reference.query"
        )
        assert "ui.photoSearch.value = before;" in body
        # 打开时搜的是 Google 图片的窗口；图库那一遍不再自己跑。
        assert "if (before) browseGoogle(before);" in body
        assert "else writePhotoWords();" in body
        assert "findPhotos(" not in body

    def test_the_words_are_remembered_per_scene_even_if_never_searched(self):
        script = self._script()
        find = self._body(script, "findPhotos")
        assert "photoPick.words[photoPick.key] = search;" in find
        typed = script[script.index('ui.photoSearch.addEventListener("input"'):]
        assert "photoPick.words[photoPick.key] = ui.photoSearch.value;" in (
            typed[:typed.index("});")]
        )

    def test_the_models_words_reach_the_box_before_the_search_runs(self):
        """词先落框、后搜索：搜索失败的时候框里不能是空的。"""
        body = self._body(self._script(), "writePhotoWords")
        assert "/photos/words`" in body
        assert body.index("ui.photoSearch.value = words;") < body.index(
            "browseGoogle(words);"
        )

    def test_clicking_the_button_always_ends_in_a_search(self):
        """点"照着照片重画"就会自己搜一遍。现在搜的是 Google 图片的窗口
        （用户说了：有它就不用再让图库搜一遍）。写词那一步没成的时候
        （接口没重启、模型没应答）不能停在一个空框和一句"正在想"上：
        说清楚，让用户自己打词。"""
        body = self._body(self._script(), "writePhotoWords")
        failed = body[body.index("} catch (error) {"):]
        failed = failed[:failed.index("\n  }\n")]
        # 写词失败不 return、不停下来。
        assert "return" not in failed
        assert "browseGoogle(words);" in body
        assert "Could not work out what to search for." in body
        # 图库的搜索还在，只是要自己点。
        assert "findPhotos(" not in body
        find = self._body(self._script(), "findPhotos")
        assert "ui.photoSearch.value = (data && data.query) || search;" in find

    def test_a_slow_search_does_not_land_on_another_scene(self):
        find = self._body(self._script(), "findPhotos")
        assert find.count("if (photoPick.at !== at) return;") == 2

    def test_a_picture_that_will_not_load_is_not_left_as_a_dead_tile(self):
        body = self._body(self._script(), "drawPhotos")
        assert 'shot.addEventListener("error", () => tile.remove());' in body
        assert 'shot.referrerPolicy = "no-referrer";' in body

    def test_every_element_the_script_looks_up_is_on_the_page(self):
        page = (ROOT / "resource" / "public" / "editor" / "index.html").read_text(
            encoding="utf-8"
        )
        for dom_id in re.findall(r'el\("([a-z-]+)"\)', self._script()):
            assert f'id="{dom_id}"' in page, dom_id
