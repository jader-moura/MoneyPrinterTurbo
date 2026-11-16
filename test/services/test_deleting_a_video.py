"""永久删除一条片子，以及从一条片子回到"我的视频"。

钉住的事：

* 删的是磁盘上的那条片子，API 重启过（内存里没有它）也删得掉；
* 只删任务目录下的那一个目录——``..`` 之类的名字什么都碰不到；
* 正在做的、正在出图的不删；
* 有文件被占着删不动的时候，什么都没删，而不是删掉一半；
* 界面上删除之前先问，而且开着一条片子时有路回到列表。

Deleting a video for good and going back to the list. Deletion works from
disk (so it survives an API restart), touches only that one folder under the
task root, refuses videos that are still being worked on, and removes nothing
when a file is in use. The page asks before deleting, and an open video has a
way back to the list.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import scene_editor, scene_regen_queue
from app.services import state as sm

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"


class TestDeletingForGood:
    def test_the_whole_folder_goes(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        assert (task_root / task_id).is_dir()
        done = scene_editor.delete_video(task_id)
        assert not (task_root / task_id).exists()
        assert done["task_id"] == task_id and done["files"] >= 3
        # 也没有在旁边留下一个改了名的目录。
        assert [p.name for p in task_root.parent.iterdir()
                if p.name.startswith(".deleting-")] == []

    def test_it_is_gone_from_my_videos(self, task_root):
        _reviewed_task(task_root, "keep")
        _reviewed_task(task_root, "drop")
        scene_editor.delete_video("drop")
        listed = [row["task_id"] for row in scene_editor.library()["tasks"]]
        assert listed == ["keep"]

    def test_other_videos_are_not_touched(self, task_root):
        _reviewed_task(task_root, "keep")
        _reviewed_task(task_root, "drop")
        before = sorted(p.name for p in (task_root / "keep").iterdir())
        scene_editor.delete_video("drop")
        assert sorted(p.name for p in (task_root / "keep").iterdir()) == before

    @pytest.mark.parametrize("name", [
        "", " ", ".", "..", "../review", "review/..", "a/b", "a\\b",
        "..\\..\\config.toml",
    ])
    def test_a_name_that_is_not_one_folder_deletes_nothing(self, task_root, name):
        _reviewed_task(task_root)
        outside = task_root.parent / "precious.txt"
        outside.write_text("keep me", encoding="utf-8")
        with pytest.raises(scene_editor.SceneError):
            scene_editor.delete_video(name)
        assert (task_root / "review").is_dir()
        assert outside.read_text(encoding="utf-8") == "keep me"
        assert task_root.is_dir()

    def test_a_video_that_is_not_there_says_so(self, task_root):
        with pytest.raises(scene_editor.SceneError, match="not on this computer"):
            scene_editor.delete_video("never-made")
        # 问一句不该凭空建出一个目录。
        assert not (task_root / "never-made").exists()

    def test_a_video_still_being_made_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(sm.state, "get_task", return_value={"state": 4}), \
                patch.object(scene_editor.tm, "is_task_busy", return_value=True):
            with pytest.raises(scene_editor.SceneBusyError):
                scene_editor.delete_video(task_id)
        assert (task_root / task_id).is_dir()

    def test_a_video_with_pictures_still_being_drawn_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_regen_queue, "snapshot",
                          return_value={"busy": True}):
            with pytest.raises(scene_editor.SceneBusyError, match="being drawn"):
                scene_editor.delete_video(task_id)
        assert (task_root / task_id).is_dir()

    def test_a_file_in_use_means_nothing_is_deleted(self, task_root):
        """Windows 上被占着的文件删不掉。删到一半停下会留下一条打不开的
        片子；所以先整个改名，改不动就原样不动。"""
        task_id, _ = _reviewed_task(task_root)
        before = sorted(p.name for p in (task_root / task_id).iterdir())
        with patch.object(os, "rename", side_effect=PermissionError("in use")):
            with pytest.raises(scene_editor.SceneError, match="Nothing was deleted"):
                scene_editor.delete_video(task_id)
        assert sorted(p.name for p in (task_root / task_id).iterdir()) == before


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_delete_then_it_is_gone(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        response = client.delete(f"/api/v1/library/{task_id}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["task_id"] == task_id
        assert not (task_root / task_id).exists()
        again = client.delete(f"/api/v1/library/{task_id}")
        assert again.status_code == 400

    def test_a_busy_video_is_a_409(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_regen_queue, "snapshot",
                          return_value={"busy": True}):
            response = client.delete(f"/api/v1/library/{task_id}")
        assert response.status_code == 409
        assert (task_root / task_id).is_dir()

    def test_the_old_route_is_still_there(self):
        """加，不替换：``DELETE /tasks/{id}`` 原样留着。"""
        source = (ROOT / "app/controllers/v1/video.py").read_text(encoding="utf-8")
        assert '@router.delete(\n    "/tasks/{task_id}"' in source.replace("\r\n", "\n")


class TestThePage:
    js = (EDITOR / "editor.js").read_text(encoding="utf-8")
    html = (EDITOR / "index.html").read_text(encoding="utf-8")

    def test_every_element_the_script_looks_up_is_on_the_page(self):
        for dom_id in ("delete-video", "delete-modal", "delete-what",
                       "delete-note", "delete-cancel", "delete-confirm"):
            assert f'id="{dom_id}"' in self.html
            assert f'el("{dom_id}")' in self.js

    def test_deleting_asks_first(self):
        """卡片上和顶栏里的"删除"都只是打开那一问；真正发请求的只有
        "Delete permanently" 那一个按钮。"""
        assert self.js.count('call("DELETE", `/library/') == 1
        body = self.js.split("async function deleteVideoForGood()")[1]
        assert 'call("DELETE", `/library/' in body.split("\nui.deleteCancel")[0]
        assert ('ui.deleteConfirm.addEventListener("click", deleteVideoForGood)'
                in self.js)
        card = self.js.split('remove.className = "card-delete"')[1][:600]
        assert "askDeleteVideo(" in card and "event.stopPropagation()" in card

    def test_the_question_says_it_cannot_be_undone(self):
        modal = self.html.split('id="delete-modal"')[1].split("</div>\n</div>")[0]
        assert "cannot be undone" in modal
        assert "Delete permanently" in modal

    def test_enter_does_not_delete(self):
        ask = self.js.split("function askDeleteVideo(")[1].split("\n}\n")[0]
        assert "ui.deleteCancel.focus()" in ask

    def test_deleting_the_open_video_leaves_it(self):
        body = self.js.split("async function deleteVideoForGood()")[1]
        body = body.split("\nui.deleteCancel")[0]
        assert re.search(r"if \(open\) \{[^}]*backToLibrary\(\);", body, re.S)

    def test_an_open_video_has_a_way_back_to_the_list(self):
        assert "function backToLibrary()" in self.js
        assert "window.location.assign(window.location.pathname)" in self.js
        toggle = self.js.split(
            'ui.libraryToggle.addEventListener("click", () => {')[1][:300]
        assert "if (state.taskId && !EMBEDDED) { backToLibrary(); return; }" in toggle
        # 没开着片子时它还是原来那个开关。
        assert "if (ui.library.hidden) loadLibrary(1); else closeLibrary();" in toggle

    def test_the_delete_button_only_shows_inside_a_video(self):
        assert re.search(r'id="delete-video"[^>]*\bhidden\b', self.html)
        assert "ui.deleteVideo.hidden = !inVideo;" in self.js
