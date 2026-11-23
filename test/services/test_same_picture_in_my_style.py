"""照着照片画的第三种办法：同一张图，换成我的画风。

"保留多少照片"是一个旋钮：拧小了照片在、照片味也在；拧大了画风对了、图
却被重新编了一遍。没有哪个位置是"就这张图，画成漫画"。第三种办法不在那
个旋钮上：只留照片的轮廓（ControlNet），按画风从头画。

钉住的事：

* 出图服务：``follow: "outlines"`` 走 ControlNet 那条管线，交过去的是轮廓
  图而不是照片；没有它时请求和以前一字不差；
* 轮廓图是黑底白线、和画幅一样大；
* 画完之后变换器和 ControlNet 一起下显卡（它们共用模块，只下一个会留下
  一个一半在卡上的变换器）；
* 从编辑器到出图请求，一路带着 ``follow``，而且带了它就不带 ``strength``；
* 这一段记得自己是这么画出来的；
* 下拉框多了一项，原来那两项一个没动。

A third way to draw from a photo: follow its outlines and draw them in the
video's style, so every shape stays where it was. Additive: without ``follow``
nothing about a request changes.
"""

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app.services import material, reference_image, scene_editor, scene_manifest

from test.services.test_genserver import (  # noqa: F401
    FakePipe,
    a_png,
    draw,
    server,
    srv,
)
from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")


class _Part:
    def __init__(self):
        self.moves = []

    def to(self, where):
        self.moves.append(where)
        return self


class FakeOutlinePipe(FakePipe):
    """The ControlNet pipeline: records like the others, and has the two parts
    that have to leave the card together."""

    def __init__(self):
        super().__init__()
        self.transformer = _Part()
        self.controlnet = _Part()


@pytest.fixture
def following(server, monkeypatch):  # noqa: F811
    pipe = FakeOutlinePipe()
    monkeypatch.setattr(srv.RESIDENT, "following_outlines", lambda spec: pipe)
    server.outline_pipe = pipe
    return server


class TestTheImageServer:
    def test_the_outlines_reach_the_controlnet_pipeline(self, following):
        response = draw(following, image=a_png(64, 36), follow="outlines")
        assert response.status_code == 200, response.text
        assert following.pipe.calls == [], "the text-to-image pipeline must not be used"
        call = following.outline_pipe.calls[0]
        assert call["control_image"].size == (1536, 864)
        assert call["controlnet_conditioning_scale"] == srv.OUTLINES["zimage-turbo"].scale
        # 不是从照片出发改：没有 image，也没有 strength。
        assert "image" not in call and "strength" not in call
        body = response.json()
        assert body["follow"] == "outlines" and body["strength"] is None

    def test_both_leave_the_card_afterwards(self, following):
        draw(following, image=a_png(64, 36), follow="outlines")
        assert following.outline_pipe.transformer.moves == ["cpu"]
        assert following.outline_pipe.controlnet.moves == ["cpu"]

    def test_how_hard_the_outlines_hold_can_be_named(self, following):
        draw(following, image=a_png(64, 36), follow="outlines", followScale=1.1)
        assert following.outline_pipe.calls[0]["controlnet_conditioning_scale"] == 1.1

    @pytest.mark.parametrize("body, said", [
        ({"follow": "outlines"}, "needs an image"),
        ({"follow": "depth", "image": "x"}, '"outlines"'),
        ({"follow": "outlines", "image": None, "followScale": 0.5}, "needs an image"),
    ])
    def test_a_request_that_cannot_be_drawn_is_refused(self, following, body, said):
        if body.get("image") == "x":
            body["image"] = a_png(64, 36)
        response = draw(following, **{k: v for k, v in body.items() if v is not None})
        assert response.status_code == 400
        assert said in response.json()["error"]

    def test_an_impossible_scale_is_refused(self, following):
        response = draw(following, image=a_png(64, 36), follow="outlines", followScale=5)
        assert response.status_code == 400 and "followScale" in response.json()["error"]

    def test_the_openai_route_carries_it_through(self, following):
        response = following.image(image=a_png(64, 36), follow="outlines", size="864x1536")
        assert response.status_code == 200, response.text
        assert following.outline_pipe.calls[0]["control_image"].size == (864, 1536)

    def test_without_the_word_it_is_the_redraw_it_always_was(self, server, monkeypatch):  # noqa: F811
        image_pipe = FakePipe()
        monkeypatch.setattr(srv.RESIDENT, "starting_from_image", lambda spec: image_pipe)
        response = draw(server, image=a_png(64, 36), strength=0.5)
        assert response.status_code == 200
        assert image_pipe.calls[0]["strength"] == 0.5
        assert "control_image" not in image_pipe.calls[0]
        assert response.json()["follow"] is None

    def test_models_json_did_not_grow(self):
        """``GET /models`` 是别处逐字段比对的；ControlNet 的事不放在那里面。"""
        assert "outlines" not in srv.as_json(srv.REGISTRY["zimage-turbo"])
        assert srv.OUTLINES["zimage-turbo"].pipeline == "ZImageControlNetPipeline"


class TestTheEditingModel:
    """``follow: "picture"``：换一个看得见照片的模型来画，提示词是一句指令。"""

    def test_the_picture_itself_reaches_the_other_model(self, server):  # noqa: F811
        response = draw(server, image=a_png(64, 36), follow="picture",
                        prompt="Redraw this exact image in this style: comic book art.")
        assert response.status_code == 200, response.text
        editor = srv.EDITORS["zimage-turbo"]
        # 卡上只放得下一个模型：换上去的是改图模型。
        assert srv.RESIDENT.model_id == editor.id
        call = server.pipe.calls[0]
        assert call["image"].size == (1536, 864)
        assert call["num_inference_steps"] == editor.steps
        assert call["guidance_scale"] == editor.guidance
        assert "strength" not in call and "control_image" not in call
        body = response.json()
        assert body["follow"] == "picture" and body["drawnBy"] == editor.id
        assert body["model"] == "zimage-turbo" and body["repo"] == editor.repo

    def test_the_next_plain_picture_gets_its_own_model_back(self, server):  # noqa: F811
        draw(server, image=a_png(64, 36), follow="picture")
        response = draw(server)
        assert response.status_code == 200, response.text
        assert srv.RESIDENT.model_id == "zimage-turbo"
        assert response.json()["drawnBy"] == "zimage-turbo"

    def test_it_needs_the_picture(self, server):  # noqa: F811
        response = draw(server, follow="picture")
        assert response.status_code == 400
        assert "needs an image" in response.json()["error"]

    def test_it_is_not_one_more_entry_in_models(self):
        assert srv.EDITORS["zimage-turbo"].id not in srv.REGISTRY

    def test_the_instruction_names_the_style_and_not_the_scene(self):
        said = material._restyle_instruction(
            "{term}, comic book art, bold ink outlines, saturated colour"
        )
        assert said == (
            "Redraw this exact image in this style: comic book art, bold ink "
            "outlines, saturated colour. Keep the same composition, subjects "
            "and poses."
        )

    def test_no_style_is_an_error_not_another_way_of_drawing(self):
        with patch.object(material.config, "app", {}):
            with pytest.raises(ValueError, match="no style"):
                material._restyle_instruction("{term}")

    def test_the_request_carries_the_instruction_instead_of_the_prompt(self):
        sent = {}

        def request(endpoint, payload):
            sent.update(payload)
            return None, "stop here"

        with patch.object(material, "_request_openai_image", side_effect=request), \
                patch.object(material, "_openai_image_endpoint",
                             return_value=("http://x/v1", "zimage-turbo")):
            material.generate_images_openai(
                "Vietnam War helicopters", 4, reference_image="QUJD",
                prompt_template="{term}, comic book art",
                reference_follow="picture",
            )
        assert sent["follow"] == "picture" and sent["image"] == "QUJD"
        assert "Vietnam" not in sent["prompt"]
        assert sent["prompt"].startswith("Redraw this exact image in this style: comic book art.")
        assert "strength" not in sent

    def test_the_scene_remembers_it(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        made = task_root / task_id / "new-picture.png"
        made.write_bytes(b"x")
        with patch.object(scene_editor, "_produce_material",
                          return_value=(str(made), "openai_image")):
            scene_editor.regenerate_material(
                task_id, 0, reference=_photo(), follow="picture"
            )
        recorded = scene_manifest.load(task_id)["scenes"][0]["reference"]
        assert recorded["follow"] == "picture" and "strength" not in recorded


class TestTheOutlines:
    def _picture(self):
        picture = Image.new("RGB", (160, 90), (30, 30, 30))
        for x in range(50, 110):
            for y in range(25, 65):
                picture.putpixel((x, y), (230, 230, 230))
        return picture

    def test_white_lines_on_black_the_same_size(self):
        lines = srv.outlines_of(self._picture())
        assert lines.size == (160, 90) and lines.mode == "RGB"
        colours = {pixel for pixel in lines.getdata()}
        assert colours == {(0, 0, 0), (255, 255, 255)}

    def test_the_lines_are_where_the_shape_ends(self):
        lines = srv.outlines_of(self._picture())
        white = [(x, y) for y in range(90) for x in range(160)
                 if lines.getpixel((x, y)) == (255, 255, 255)]
        assert white, "a square on a plain ground has an outline"
        # 都贴着那个方块的边，方块里面和远处的平地上没有线。
        assert all(44 <= x <= 116 and 19 <= y <= 71 for x, y in white)
        assert all(not (58 <= x <= 102 and 33 <= y <= 57) for x, y in white)

    def test_a_flat_picture_has_none(self):
        lines = srv.outlines_of(Image.new("RGB", (64, 36), (90, 90, 90)))
        assert set(lines.getdata()) == {(0, 0, 0)}


def _photo() -> reference_image.Reference:
    out = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 120, 200)).save(out, "PNG")
    return reference_image.Reference(
        data=out.getvalue(), query="red apple", source_url="", page_url="",
        provider="upload", width=800, height=600,
    )


class TestFromTheEditorToTheRequest:
    def test_the_request_names_it_and_sends_no_strength(self):
        sent = {}

        def request(endpoint, payload):
            sent.update(payload)
            return None, "stop here"

        with patch.object(material, "_request_openai_image", side_effect=request), \
                patch.object(material, "_openai_image_endpoint",
                             return_value=("http://x/v1", "zimage-turbo")):
            material.generate_images_openai(
                "an apple", 4, reference_image="QUJD", reference_strength=0.55,
                reference_follow="outlines",
            )
        assert sent["follow"] == "outlines" and sent["image"] == "QUJD"
        assert "strength" not in sent

    def test_without_it_the_request_is_what_it_was(self):
        sent = {}

        def request(endpoint, payload):
            sent.update(payload)
            return None, "stop here"

        with patch.object(material, "_request_openai_image", side_effect=request), \
                patch.object(material, "_openai_image_endpoint",
                             return_value=("http://x/v1", "zimage-turbo")):
            material.generate_images_openai(
                "an apple", 4, reference_image="QUJD", reference_strength=0.55,
            )
        assert sent["strength"] == 0.55 and "follow" not in sent

    def test_the_scene_remembers_how_it_was_drawn(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        made = task_root / task_id / "new-picture.png"
        made.write_bytes(b"x")
        seen = {}

        def produce(*args, **kwargs):
            seen.update(kwargs)
            return str(made), "openai_image"

        with patch.object(scene_editor, "_produce_material", side_effect=produce):
            scene_editor.regenerate_material(
                task_id, 0, reference=_photo(), strength=0.55, follow="outlines"
            )
        assert seen["follow"] == "outlines" and seen["strength"] is None
        recorded = scene_manifest.load(task_id)["scenes"][0]["reference"]
        assert recorded["follow"] == "outlines" and "strength" not in recorded
        assert recorded["file"]

    def test_an_unknown_way_is_named(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError, match="outlines"):
            scene_editor.regenerate_material(
                task_id, 0, reference=_photo(), follow="depth"
            )

    def test_it_needs_a_photo(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError, match="needs a photo"):
            scene_editor.regenerate_material(task_id, 0, follow="outlines")

    def test_queueing_carries_it(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi
        from app.services import scene_regen_queue

        task_id, _ = _reviewed_task(task_root)
        seen = {}

        def enqueue(*args, **kwargs):
            seen.update(kwargs)
            return {"id": "job"}

        client = TestClient(asgi.app)
        with patch.object(scene_regen_queue, "enqueue", side_effect=enqueue):
            got = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
                json={"from_photo": True, "follow": "outlines"},
            )
            bad = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
                json={"from_photo": True, "follow": "anything"},
            )
        assert got.status_code == 200, got.text
        assert seen["follow"] == "outlines"
        assert bad.status_code in (400, 422)

    def test_a_job_says_so(self):
        from app.services import scene_regen_queue

        job = scene_regen_queue._Job(
            task_id="t", index=0, keyword="k", image_style=None, anchor="a",
            from_photo=True, follow="outlines",
        )
        assert job.payload()["follow"] == "outlines"


class TestThePage:
    def test_there_is_a_third_choice_and_the_first_two_are_as_they_were(self):
        picker = JS.split("function photoKeepPicker()")[1].split("\n}\n")[0]
        assert '[KEEP_THE_PHOTO, "Keep the photograph — the real subject and framing"]' in picker
        assert '[KEEP_MY_STYLE, "Loosely inspired — my style exactly"]' in picker
        assert '[SAME_PICTURE, "Same picture — redrawn in my style"]' in picker
        assert 'const SAME_PICTURE = "picture";' in JS

    def test_choosing_it_sends_the_word_instead_of_a_number(self):
        queue = JS.split("async function queueRegeneration(")[1].split("\n}\n")[0]
        # A way of using the photo ("picture", and now "original") is sent as
        # the word; see test_the_photo_itself.py for the other one.
        assert "value === SAME_PICTURE || value === AS_IT_IS ? value" in JS
        assert "if (photoWay(strength)) body.follow = strength;" in queue
        assert "else if (strength) body.strength = strength;" in queue

    def test_every_way_of_choosing_a_photo_reads_the_one_choice(self):
        # 选择现在在挑照片的那扇窗里，所有的路都从 ``redrawFromPhoto`` 过。
        redraw = JS.split("function redrawFromPhoto(extra)")[1].split("\n}\n")[0]
        assert "const strength = photoKeepOf(photoKeepChoice.input);" in redraw
        assert JS.count("photoKeepOf(") == 2  # 定义一次，用一次

    def test_the_queue_and_the_scene_say_which_way_it_was(self):
        assert ': job.follow ? " · same picture, my style" : "")' in JS
        note = JS.split("function usedPhotoNote(scene)")[1].split("\n}\n")[0]
        assert 'lines.push("same picture, in your style");' in note
