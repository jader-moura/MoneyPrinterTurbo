"""照着照片画的时候，照片一点都不裁；窗里摆着已经存下的那一张。

钉住的事：

* 出图服务：照片整张缩进画幅里，空出来的地方用它自己糊开了的样子垫上，
  不是黑边，也不是裁掉；
* "同一张图"那条路：改图模型拿到的是整张照片、照片自己的比例；
* 编辑器："保留多少"一开始是"同一张图，换成我的画风"；
* 已经存着照片的那一段，开窗就看得见那张照片，点它就用。

A photograph that a picture is drawn from is never cropped, and the photo a
scene already has is shown in the photo window.
"""

from pathlib import Path

import pytest
from PIL import Image

from app.services import reference_image, scene_editor

from test.services.test_genserver import (  # noqa: F401
    a_png,
    draw,
    redraw,
    server,
    srv,
)

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"
JS = (EDITOR / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (EDITOR / "editor.css").read_text(encoding="utf-8")


def _marked(width: int, height: int) -> Image.Image:
    """红底，四个角各一块绿：角还在，就是没裁。"""
    photo = Image.new("RGB", (width, height), (200, 30, 30))
    corner = Image.new("RGB", (width // 5, height // 5), (20, 220, 40))
    for x in (0, width - corner.width):
        for y in (0, height - corner.height):
            photo.paste(corner, (x, y))
    return photo


def _reference(photo: Image.Image) -> reference_image.Reference:
    import io

    raw = io.BytesIO()
    photo.save(raw, "PNG")
    return reference_image.Reference(
        data=raw.getvalue(), query="q", source_url="", page_url="",
        provider="upload", width=photo.width, height=photo.height,
    )


def _green(pixel) -> bool:
    return pixel[1] > 150 and pixel[0] < 110


class TestNothingIsCutOff:
    def test_a_tall_photo_in_a_wide_frame_keeps_its_top_and_bottom(self):
        fitted = srv.fit_reference(_marked(400, 800), 1536, 864)
        assert fitted.size == (1536, 864)
        # 整张照片缩成 432x864，摆在正中：四个角都还在画幅里。
        left, right = (1536 - 432) // 2, (1536 + 432) // 2
        for x in (left + 20, right - 20):
            for y in (20, 864 - 20):
                assert _green(fitted.getpixel((x, y))), (x, y)

    def test_a_wide_photo_in_a_tall_frame_keeps_its_sides(self):
        fitted = srv.fit_reference(_marked(800, 200), 864, 1536)
        assert fitted.size == (864, 1536)
        top, bottom = (1536 - 216) // 2, (1536 + 216) // 2
        for x in (20, 864 - 20):
            for y in (top + 25, bottom - 25):
                assert _green(fitted.getpixel((x, y))), (x, y)

    def test_what_is_left_over_is_the_photo_blurred_not_black(self):
        fitted = srv.fit_reference(_marked(400, 800), 1536, 864)
        for at in ((40, 432), (1500, 432), (200, 100)):
            assert sum(fitted.getpixel(at)) > 120, at

    def test_a_photo_of_the_same_shape_is_only_resized(self):
        fitted = srv.fit_reference(_marked(640, 360), 1536, 864)
        assert fitted.size == (1536, 864)
        assert _green(fitted.getpixel((8, 8))) and _green(fitted.getpixel((1527, 855)))

    def test_it_is_not_squashed(self):
        whole = srv.whole_reference(_marked(400, 800), 1536, 864)
        assert whole.size == (432, 864)

    def test_the_request_still_gets_the_size_it_asked_for(self, redraw):  # noqa: F811
        body = draw(redraw, size="864x1536", image=a_png(width=400, height=100)).json()
        assert (body["width"], body["height"]) == (864, 1536)
        assert redraw.image_pipe.calls[0]["image"].size == (864, 1536)


class TestTheEditingModelSeesTheWholePhoto:
    def test_in_its_own_shape(self, server):  # noqa: F811
        response = draw(server, image=a_png(400, 800), follow="picture",
                        prompt="Redraw this exact image in this style: comic book art.")
        assert response.status_code == 200, response.text
        seen = server.pipe.calls[0]["image"]
        # 竖的照片还是竖的，而且每一边都是 16 的倍数（管线自己凑的话是裁）。
        assert seen.size == (432, 864)
        assert seen.width % 16 == 0 and seen.height % 16 == 0
        body = response.json()
        assert (body["width"], body["height"]) == (1536, 864)

    def test_sides_are_brought_to_the_models_step_by_resizing(self):
        whole = srv.whole_reference(_marked(500, 333), 1536, 864, 16)
        assert whole.size == (1296, 864)
        odd = srv.whole_reference(_marked(333, 500), 1536, 864, 16)
        assert odd.size == (560, 864)


class TestTheEditor:
    def test_same_picture_is_what_is_chosen_to_begin_with(self):
        assert "let photoKeep = SAME_PICTURE;" in JS
        picker = JS.split("function photoKeepPicker()")[1].split("\n}\n")[0]
        assert "input.value = String(photoKeep);" in picker

    def test_the_photo_a_scene_already_has_is_in_the_window_when_it_opens(self):
        opened = JS.split("function openPhotoPicker(")[1].split("\n}\n")[0]
        assert "holdPhoto({ photo_kept: photoPick.kept }," in opened
        assert "holdPhoto({ photo_picked: true }," in opened
        where = JS.split("function photoAddress(extra)")[1].split("\n}\n")[0]
        assert "/photos/used/${encodeURIComponent(extra.photo_kept)}" in where
        assert "`${API}${scenePath(photoPick.at)}/photo-pick`" in where
        assert 'button("Draw from this photo", "primary small"' in JS

    def test_not_while_choosing_photos_for_many_scenes(self):
        opened = JS.split("function openPhotoPicker(")[1].split("\n}\n")[0]
        assert "if (!round && photoPick.kept) {" in opened
        assert "} else if (!round && photoPick.saved) {" in opened


class TestThePhotoCanBeResized:
    """照片在画框里摆多大、摆在哪，由用户定；没动过就是整张、不裁。"""

    def test_a_chosen_photo_is_put_in_the_frame_before_it_is_drawn(self):
        redraw_from = JS.split("function redrawFromPhoto(extra)")[1].split("\n}\n")[0]
        hold = redraw_from.index("holdPhoto(extra,")
        assert hold < redraw_from.index("queueRegeneration(")
        assert hold < redraw_from.index("keepRoundPhoto(")
        assert "photoFrameFirst.checked && photoAddress(extra)" in redraw_from

    def test_the_one_in_hand_is_drawn_with_what_was_set(self):
        redraw_from = JS.split("function redrawFromPhoto(extra)")[1].split("\n}\n")[0]
        assert "const frame = held ? photoFrameChosen() : null;" in redraw_from
        assert "...(frame ? { frame } : {})," in redraw_from

    def test_an_untouched_frame_says_nothing(self):
        chosen = JS.split("function photoFrameChosen()")[1].split("\n}\n")[0]
        assert "if (zoom === 1 && !x && !y) return null;" in chosen

    def test_the_browser_and_the_server_place_it_the_same_way(self):
        placed = JS.split("function placePhotoInFrame()")[1].split("\n}\n")[0]
        assert "Math.min(wide / w, high / h) * photoInHand.zoom" in placed
        assert "wide / 2 + photoInHand.x * wide - (w * fit) / 2" in placed
        source = (ROOT / "app/services/scene_editor.py").read_text(encoding="utf-8")
        # The placing itself is shared with a photo used as it is.
        assert "return _placed_in_frame(reference, width, height, zoom, x, y)" in (
            source.split("def framed_photo(")[1].split("\ndef ")[0])
        body = source.split("def _placed_in_frame(")[1].split("\ndef ")[0]
        assert "min(width / photo.width, height / photo.height) * zoom" in body
        assert "width / 2 + x * width - size[0] / 2" in body

    def test_it_can_be_turned_off(self):
        assert 'localStorage.getItem("editor.photoFrameFirst") !== "no"' in JS

    def test_untouched_is_the_photo_as_it_was(self, task_root):  # noqa: F811
        task_id, _ = _reviewed_task(task_root)
        photo = _reference(_marked(400, 800))
        same = scene_editor.framed_photo(task_id, photo, {"zoom": 1, "x": 0, "y": 0})
        assert same is photo

    def test_made_larger_it_fills_the_frame(self, task_root):  # noqa: F811
        import io

        task_id, _ = _reviewed_task(task_root)
        wide, high = scene_editor.load_params(task_id).video_aspect.to_resolution()
        photo = _reference(Image.new("RGB", (600, 600), (20, 220, 40)))
        framed = scene_editor.framed_photo(task_id, photo, {"zoom": 4, "x": 0, "y": 0})
        got = Image.open(io.BytesIO(framed.data))
        assert got.width / got.height == pytest.approx(wide / high, rel=0.01)
        assert max(got.size) == scene_editor.FRAMED_LONG_EDGE
        assert (framed.width, framed.height) == got.size
        for at in ((4, 4), (got.width - 5, got.height - 5), (got.width // 2, 5)):
            assert _green(got.getpixel(at)), at

    def test_made_smaller_and_moved_it_sits_where_it_was_put(self, task_root):  # noqa: F811
        import io

        task_id, _ = _reviewed_task(task_root)
        # 红底，正中一块绿：绿块在哪，照片的中心就在哪。
        marked = Image.new("RGB", (600, 600), (200, 30, 30))
        marked.paste(Image.new("RGB", (120, 120), (20, 220, 40)), (240, 240))
        framed = scene_editor.framed_photo(
            task_id, _reference(marked), {"zoom": 0.5, "x": 0.25, "y": 0})
        got = Image.open(io.BytesIO(framed.data))
        middle = (got.width // 2 + got.width // 4, got.height // 2)
        assert _green(got.getpixel(middle))
        assert not _green(got.getpixel((middle[0] + 150, middle[1])))

    def test_a_photo_pushed_out_of_the_frame_is_refused(self, task_root):  # noqa: F811
        task_id, _ = _reviewed_task(task_root)
        photo = _reference(Image.new("RGB", (600, 600), (20, 220, 40)))
        with pytest.raises(scene_editor.SceneError, match="out of the frame"):
            scene_editor.framed_photo(task_id, photo, {"zoom": 0.3, "x": 1, "y": 1})

    def test_the_request_carries_it_to_the_photo_that_is_drawn_from(self, task_root):  # noqa: F811
        import io
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from app import asgi
        from app.services import scene_regen_queue

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(
            task_id, 1, photo=_reference(Image.new("RGB", (600, 600), (20, 220, 40))))
        seen = {}

        def enqueue(*_args, **kwargs):
            seen.update(kwargs)
            return {"id": "job"}

        with patch.object(scene_regen_queue, "enqueue", side_effect=enqueue):
            got = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/scenes/1/material/queue",
                json={"from_photo": True, "photo_picked": True,
                      "frame": {"zoom": 2, "x": 0, "y": 0}},
            )
        assert got.status_code == 200, got.text
        assert max(seen["photo"].width, seen["photo"].height) == scene_editor.FRAMED_LONG_EDGE
        assert Image.open(io.BytesIO(seen["photo"].data)).size == (
            seen["photo"].width, seen["photo"].height)

    def test_a_frame_without_a_chosen_photo_is_an_error(self, task_root):  # noqa: F811
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        got = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
            json={"from_photo": True, "frame": {"zoom": 2}},
        )
        assert got.status_code == 400 and "no photo was chosen" in got.text

    @pytest.mark.parametrize("frame", [{"zoom": 9}, {"zoom": 1, "x": 3}, {"wide": 1}])
    def test_a_frame_that_makes_no_sense_is_refused(self, task_root, frame):  # noqa: F811
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        got = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
            json={"from_photo": True, "photo_picked": True, "frame": frame},
        )
        assert got.status_code in (400, 422)

    def test_a_photo_just_chosen_can_be_looked_at(self, task_root):  # noqa: F811
        import io

        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        raw = io.BytesIO()
        Image.new("RGB", (900, 700), (20, 220, 40)).save(raw, "PNG")
        token = scene_editor.keep_uploaded_photo(raw.getvalue(), "mine.png")["file"]
        client = TestClient(asgi.app)
        got = client.get(f"/api/v1/tasks/{task_id}/photos/uploaded/{token}")
        assert got.status_code == 200 and got.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(got.content)).size == (900, 700)
        assert client.get(
            f"/api/v1/tasks/{task_id}/photos/uploaded/nothing").status_code == 400
