"""A fourth thing to do with a chosen photo: use it, as it is.

In the user's words: "we have a lot of options of using the image to redraw it,
but now we also need another option, the option to exact the image we got from
google, the original one."

The other three ways all hand the photo to a model. This one hands it to
nobody: the photo is fitted to the video's frame, saved in the task as a
picture, and baked into a clip the way every picture is. So nothing is drawn,
the graphics card is not needed, and the scene remembers how it came about.
"""

import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from app.models.schema import SceneRegenerateRequest, ScenePhotoPickRequest
from app.services import (
    material,
    reference_image,
    scene_editor,
    scene_manifest,
    scene_regen_queue,
    scene_video,
)
from app.services import video as video_service

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")


def a_photo(width=1200, height=800, colour=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "PNG")
    return reference_image.uploaded(out.getvalue(), "a photo")


@pytest.fixture
def baked(task_root):
    """Stands in for the picture-to-clip encode; remembers what it was given."""
    seen = {}

    def bake(picture, seconds=5, image_motion="zoom"):
        seen.update(picture=picture, seconds=seconds, motion=str(image_motion))
        with Image.open(picture) as opened:
            seen["size"] = opened.size
            seen["middle"] = opened.convert("RGB").getpixel(
                (opened.width // 2, opened.height // 2))
        clip = os.path.splitext(picture)[0] + ".clip.mp4"
        Path(clip).write_bytes(b"clip")
        return clip

    with patch.object(video_service, "render_image_zoom_video", side_effect=bake):
        yield seen


class TestTheSceneShowsThePhoto:
    def test_nothing_is_drawn(self, task_root, baked):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(material, "download_videos") as drawing:
            scene = scene_editor.regenerate_material(
                task_id, 0, reference=a_photo(), follow="original")

        drawing.assert_not_called()
        assert scene["material"].endswith(".clip.mp4")
        assert scene["kind"] == "video"
        assert scene["source"] == "local"
        assert scene["locked"] is True

    def test_the_whole_photo_is_in_the_videos_frame(self, task_root, baked):
        """A landscape photo in a portrait video: all of it, in the middle,
        at the video's own size."""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.regenerate_material(
            task_id, 0, reference=a_photo(1200, 800), follow="original")

        assert baked["size"] == (1080, 1920)
        assert baked["middle"] == (200, 30, 30)
        assert os.path.dirname(baked["picture"]) == str(task_root / task_id)

    def test_a_photo_already_placed_by_the_user_is_not_placed_again(
        self, task_root, baked
    ):
        task_id, _ = _reviewed_task(task_root)
        placed = a_photo(864, 1536)
        scene_editor.regenerate_material(
            task_id, 0, reference=placed, follow="original")

        assert baked["size"] == (864, 1536)
        assert Path(baked["picture"]).read_bytes() == placed.data

    def test_it_is_as_long_as_its_scene_and_moves_like_the_video(
        self, task_root, baked
    ):
        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["duration"] = 7.5
        scene_manifest.save_scenes(task_id, scenes)

        scene_editor.regenerate_material(
            task_id, 0, reference=a_photo(), follow="original")

        assert baked["seconds"] == 7.5
        assert baked["motion"] == "zoom"

    def test_the_scene_remembers_it_is_the_photo_itself(self, task_root, baked):
        task_id, _ = _reviewed_task(task_root)
        scene = scene_editor.regenerate_material(
            task_id, 0, reference=a_photo(), follow="original")

        assert scene["reference"]["follow"] == "original"
        assert "strength" not in scene["reference"]
        # The photo is kept, and what the scene showed before can be put back.
        assert scene["reference"]["file"]
        assert scene_manifest.load(task_id)["scenes"][0].get("history")

    def test_it_needs_a_photo(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError, match="needs a photo"):
            scene_editor.regenerate_material(task_id, 0, follow="original")

    def test_it_really_becomes_a_clip_that_can_be_held_longer(self, task_root):
        """No stand-in: the real encode, and the timeline finds the picture
        behind the clip again."""
        from app.services import timeline

        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["duration"] = 1.0
        scene_manifest.save_scenes(task_id, scenes)

        scene = scene_editor.regenerate_material(
            task_id, 0, reference=a_photo(), follow="original")

        clip = scene_manifest.resolve_material(task_id, scene["material"])
        assert os.path.getsize(clip) > 0
        behind = timeline.source_image_for(task_id, clip)
        assert behind and os.path.basename(behind).startswith("photo-")


class TestItDoesNotWaitForTheCard:
    def test_it_is_accepted_and_done_while_video_scenes_hold_the_card(
        self, task_root, baked
    ):
        task_id, _ = _reviewed_task(task_root)
        busy = scene_editor.SceneError("video scenes are being made")
        with patch.object(scene_video, "refuse_while_running", side_effect=busy):
            plan = scene_editor.check_regenerate(task_id, 0, drawing=False)
            scene = scene_editor.regenerate_material(
                task_id, 0, reference=a_photo(), follow="original")
            with pytest.raises(scene_editor.SceneError):
                scene_editor.check_regenerate(task_id, 0)

        assert plan["index"] == 0
        assert scene["reference"]["follow"] == "original"

    def test_a_source_that_cannot_draw_can_still_show_a_photo(self, task_root):
        task_id, _ = _reviewed_task(task_root, source="local")
        assert scene_editor.check_regenerate(task_id, 0, drawing=False)["index"] == 0

    def test_the_queue_asks_for_no_drawing(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor, "check_regenerate",
            return_value={"index": 0, "keyword": "", "scene_id": "s", "material": ""},
        ) as check:
            scene_regen_queue._material_job(
                task_id, 0, photo=a_photo(), follow="original")
            scene_regen_queue._material_job(
                task_id, 0, photo=a_photo(), follow="picture")

        assert [call.kwargs["drawing"] for call in check.call_args_list] == [False, True]


class TestTheRequest:
    @pytest.mark.parametrize("model", [SceneRegenerateRequest, ScenePhotoPickRequest])
    def test_original_is_a_way_the_api_accepts(self, model):
        assert model(follow="original").follow == "original"
        with pytest.raises(ValueError):
            model(follow="untouched")

    def test_a_photo_can_be_kept_as_the_original_before_drawing(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(
            task_id, 0, photo=a_photo(), follow="original")

        ready = scene_editor.photo_picks_for_drawing(task_id)
        assert [pick["follow"] for pick in ready.values()] == ["original"]


class TestThePage:
    def test_the_list_has_a_fourth_choice_and_keeps_the_other_three(self):
        assert '[AS_IT_IS, "Use the photo itself — the original, not redrawn"]' in JS
        assert '[SAME_PICTURE, "Same picture — redrawn in my style"]' in JS
        assert '[KEEP_THE_PHOTO, "Keep the photograph' in JS
        assert '[KEEP_MY_STYLE, "Loosely inspired' in JS

    def test_the_choice_is_sent_as_the_way_not_as_an_amount(self):
        assert 'const AS_IT_IS = "original";' in JS
        assert "if (photoWay(strength)) body.follow = strength;" in JS
        assert "if (photoWay(pick.strength)) body.follow = pick.strength;" in JS

    def test_the_queue_and_the_scene_say_it_was_not_redrawn(self):
        assert "the photo itself, not redrawn" in JS
        assert "This photo, as it is" in JS
