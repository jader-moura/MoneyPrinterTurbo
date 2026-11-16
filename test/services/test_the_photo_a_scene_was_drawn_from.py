"""照着照片画的那一段，把照片本身留一份，并在编辑器里摆出来。

钉住的事：

* 照片存进任务目录的子目录（不混进"画过的图"），同一张只存一份；
* 这一段记得那个文件名，存盘、读回都还在；乱写的名字被丢掉；
* 接口只认它自己起的那种名字，别的一概不给；
* 存不下不连累那张已经画好的图；
* 检查器里两条画法各占一块，照片就摆在"照着照片画"那一块里。

The photo a scene was drawn from is kept in the task folder and shown in the
inspector, so a good or bad result can be compared with what it came from.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import reference_image, scene_editor, scene_manifest

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"
JS = (EDITOR / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")


def _photo(data: bytes = b"png-bytes") -> reference_image.Reference:
    return reference_image.Reference(
        data=data, query="red apple", source_url="https://site.test/a.png",
        page_url="https://site.test/a.png", provider="upload",
        width=800, height=600,
    )


class TestKeepingIt:
    def test_it_is_written_into_the_task_folder(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        name = scene_editor.keep_used_photo(task_id, _photo())
        kept = task_root / task_id / scene_editor.USED_PHOTO_DIR / name
        assert kept.read_bytes() == b"png-bytes"

    def test_it_is_not_among_the_pictures_that_were_drawn(self, task_root):
        """任务目录本身是"画过的图"那面墙要扫的地方。"""
        task_id, _ = _reviewed_task(task_root)
        name = scene_editor.keep_used_photo(task_id, _photo())
        assert name not in [p.name for p in (task_root / task_id).iterdir()]

    def test_the_same_photo_is_kept_once(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        first = scene_editor.keep_used_photo(task_id, _photo())
        again = scene_editor.keep_used_photo(task_id, _photo())
        other = scene_editor.keep_used_photo(task_id, _photo(b"another"))
        assert first == again != other
        folder = task_root / task_id / scene_editor.USED_PHOTO_DIR
        assert len(list(folder.iterdir())) == 2

    def test_a_disk_that_refuses_does_not_lose_the_picture(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.os, "makedirs", side_effect=OSError("full")):
            assert scene_editor.keep_used_photo(task_id, _photo()) == ""

    def test_the_scene_remembers_which_file(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        name = scene_editor.keep_used_photo(task_id, _photo())
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {**_photo().payload(), "strength": 0.55, "file": name}
        scene_manifest.save_scenes(task_id, scenes)
        assert scene_manifest.load(task_id)["scenes"][0]["reference"]["file"] == name

    @pytest.mark.parametrize("wrong", ["../../config.toml", "a.png", "x" * 16 + ".png",
                                       "0123456789abcdef.jpg"])
    def test_a_made_up_file_name_is_dropped(self, task_root, wrong):
        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {**_photo().payload(), "file": wrong}
        scene_manifest.save_scenes(task_id, scenes)
        assert "file" not in scene_manifest.load(task_id)["scenes"][0]["reference"]

    def test_redrawing_from_a_photo_records_it(self):
        source = (ROOT / "app/services/scene_editor.py").read_text(encoding="utf-8")
        body = source.split("def regenerate_material(")[1].split("\ndef ")[0]
        assert "keep_used_photo(task_id, reference) if reference is not None" in body
        assert 'scene["reference"]["file"] = used_photo' in body


class TestServingIt:
    def test_over_http(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        name = scene_editor.keep_used_photo(task_id, _photo())
        client = TestClient(asgi.app)
        got = client.get(f"/api/v1/tasks/{task_id}/photos/used/{name}")
        assert got.status_code == 200 and got.content == b"png-bytes"
        assert got.headers["content-type"] == "image/png"

    @pytest.mark.parametrize("name", ["script.json", "..", "0123456789abcdef.png",
                                      "0123456789ABCDEF.png", "final-1.mp4"])
    def test_only_a_kept_photo_is_served(self, task_root, name):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError):
            scene_editor.used_photo_path(task_id, name)


class TestDrawingFromItAgain:
    """同一张照片换一档"留多少"再画一次，不用重新去找。"""

    def _kept(self, task_root, width=800, height=600):
        import io

        from PIL import Image

        task_id, _ = _reviewed_task(task_root)
        out = io.BytesIO()
        Image.new("RGB", (width, height), (10, 120, 200)).save(out, "PNG")
        name = scene_editor.keep_used_photo(task_id, _photo(out.getvalue()))
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {**_photo().payload(), "strength": 0.55, "file": name}
        scene_manifest.save_scenes(task_id, scenes)
        return task_id, name

    def test_the_kept_photo_comes_back_with_where_it_was_from(self, task_root):
        task_id, name = self._kept(task_root)
        again = scene_editor.kept_reference_photo(task_id, name)
        assert (again.width, again.height) == (800, 600)
        assert again.query == "red apple"
        assert again.source_url == "https://site.test/a.png"

    def test_a_photo_that_is_not_kept_is_named(self, task_root):
        task_id, _ = self._kept(task_root)
        with pytest.raises(scene_editor.SceneError, match="not kept"):
            scene_editor.kept_reference_photo(task_id, "0123456789abcdef.png")

    def test_queueing_with_it_uses_that_photo(self, task_root):
        from fastapi.testclient import TestClient

        from app import asgi
        from app.services import scene_regen_queue

        task_id, name = self._kept(task_root)
        seen = {}

        def enqueue(*args, **kwargs):
            seen.update(kwargs)
            return {"id": "job"}

        with patch.object(scene_regen_queue, "enqueue", side_effect=enqueue):
            got = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
                json={"from_photo": True, "photo_kept": name, "strength": 0.9},
            )
        assert got.status_code == 200, got.text
        assert seen["photo"].width == 800 and seen["strength"] == 0.9

    def test_the_button_uses_the_choice_above_it(self):
        place = JS.split('button("Redraw from this same photo"')[1][:520]
        assert "Number(keep.input.value) || photoKeep" in place
        assert "{ photo_kept: kept," in place
        # 只有存下来的照片才有这个按钮。
        before = JS.split('button("Redraw from this same photo"')[0][-160:]
        assert "if (kept) {" in before


class TestTheInspector:
    def test_each_way_of_drawing_has_its_own_block(self):
        assert 'inspectorGroup(isGenerated(scene) ? "Draw it from the prompt"' in JS
        assert 'inspectorGroup("Draw it from a photo")' in JS
        assert "drawBox.appendChild(regenerate);" in JS
        assert "photoBox.appendChild(keep.wrap);" in JS
        assert "photoBox.appendChild(fromPhoto);" in JS

    def test_the_two_small_buttons_sit_apart_from_them(self):
        assert 'pair.className = "insp-pair";' in JS
        assert "pair.appendChild(earlier);" in JS and "pair.appendChild(save);" in JS

    def test_the_photo_is_shown_where_it_was_used(self):
        body = JS.split("function usedPhotoNote(scene)")[1].split("\n}\n")[0]
        assert "/photos/used/${encodeURIComponent(used.file)}" in body
        assert "Drawn from this photo" in body
        place = JS.split("photoBox.appendChild(fromPhoto);")[1][:260]
        assert "usedPhotoNote(scene)" in place and "photoBox.appendChild(used)" in place

    def test_a_scene_drawn_before_photos_were_kept_says_so(self):
        body = JS.split("function usedPhotoNote(scene)")[1].split("\n}\n")[0]
        assert "The photo itself was not kept" in body
