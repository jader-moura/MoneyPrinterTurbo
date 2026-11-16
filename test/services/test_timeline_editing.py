"""像剪辑软件那样改时间线：撤销、重做、分割、复制，以及快捷键。

钉住的事：

* 时间线上的每一种改动都能撤销，而且不用每个操作各自登记；
* 撤销把清单原样放回去，重做再放回来，新的改动让"重做"作废；
* 分割不改变成片——两段接起来就是原来那一段；
* 撤销不会把"这一段换过哪些图"的记录也倒回去；
* 撤销自己不被记成一次新的改动；
* 在输入框里打字时，快捷键不抢键盘。

Undo, redo, split and duplicate on the timeline. Every kind of scene edit is
undoable without each operation registering itself, a split leaves the final
video unchanged, undo does not rewind a scene's picture history or record
itself, and the shortcuts stay out of the way while typing.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import edit_history, scene_editor, scene_manifest, timeline

from test.services.test_scene_editor import (  # noqa: F401
    _material_file,
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ten_second_clips():
    """每个素材都是 10 秒。测试里的素材是一个字节的假文件，量不出长度。"""
    with patch.object(timeline, "probe_duration", return_value=10.0):
        yield


def _scenes(task_id):
    return scene_manifest.load(task_id)["scenes"]


def _shape(task_id):
    """清单的"样子"：每段的身份、素材、入点、时长、台词、提示词。"""
    return [
        (s["id"], s["material"], s.get("trim_start"), s.get("duration"),
         s["narration"], s["keyword"], bool(s.get("locked")))
        for s in _scenes(task_id)
    ]


class TestEveryEditCanBeUndone:
    def test_nothing_to_undo_on_a_video_nobody_has_touched(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        assert scene_editor.history_state(task_id) == {
            "undo": 0, "redo": 0, "undo_label": "", "redo_label": "",
        }
        with pytest.raises(scene_editor.SceneError, match="nothing to undo"):
            scene_editor.undo(task_id)

    def test_building_the_scenes_is_not_a_step(self, task_root):
        """流水线第一次写进场景不算一步：那之前没有东西可以撤回去。"""
        task_id, _ = _reviewed_task(task_root)
        assert scene_editor.history_state(task_id)["undo"] == 0

    @pytest.mark.parametrize("edit, label", [
        (lambda t: scene_editor.delete_scene(t, 0), "Delete scene"),
        (lambda t: scene_editor.reorder_scenes(t, [1, 0]), "Move scene"),
        (lambda t: scene_editor.update_scene(t, 0, keyword="a fox"), "Edit prompt"),
        (lambda t: scene_editor.update_scene(t, 0, narration="New words."),
         "Edit narration"),
        (lambda t: scene_editor.update_timing(t, 0, trim_start=1, duration=2),
         "Trim scene"),
        (lambda t: scene_editor.duplicate_scene(t, 0), "Duplicate scene"),
        (lambda t: scene_editor.split_scene(t, 0, 3), "Split scene"),
        (lambda t: scene_editor.set_locked(t, 0, True), "Edit scene"),
    ], ids=["delete", "reorder", "prompt", "narration", "trim", "duplicate",
            "split", "lock"])
    def test_it_is_undone_and_redone_exactly(
        self, task_root, ten_second_clips, edit, label
    ):
        task_id, _ = _reviewed_task(task_root)
        before = _shape(task_id)
        edit(task_id)
        after = _shape(task_id)
        assert after != before
        assert scene_editor.history_state(task_id)["undo_label"] == label

        undone = scene_editor.undo(task_id)
        assert _shape(task_id) == before
        assert undone["label"] == label
        assert undone["history"] == {
            "undo": 0, "redo": 1, "undo_label": "", "redo_label": label,
        }

        scene_editor.redo(task_id)
        assert _shape(task_id) == after
        assert scene_editor.history_state(task_id)["undo"] == 1

    def test_a_new_picture_can_be_undone_too(self, task_root):
        """换图不是这个模块里的操作，没在这里登记过——它能撤销，是因为所有
        改场景的操作都走同一个口子。"""
        task_id, materials = _reviewed_task(task_root)
        other = _material_file(task_root, task_id, "other.mp4")
        with patch.object(scene_editor.material, "download_videos",
                          return_value=[other]):
            scene_editor.regenerate_material(task_id, 0)
        assert _scenes(task_id)[0]["material"] == other
        assert scene_editor.undo(task_id)["label"] == "Change picture"
        assert _scenes(task_id)[0]["material"] == materials[0]

    def test_several_steps_come_back_in_reverse_order(self, task_root):
        task_id, _ = _reviewed_task(task_root, scenes=3)
        first = _shape(task_id)
        scene_editor.update_scene(task_id, 0, keyword="one")
        second = _shape(task_id)
        scene_editor.delete_scene(task_id, 2)
        scene_editor.undo(task_id)
        assert _shape(task_id) == second
        scene_editor.undo(task_id)
        assert _shape(task_id) == first

    def test_a_new_edit_throws_away_what_could_have_been_redone(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="one")
        scene_editor.undo(task_id)
        scene_editor.update_scene(task_id, 0, keyword="two")
        assert scene_editor.history_state(task_id)["redo"] == 0
        with pytest.raises(scene_editor.SceneError, match="nothing to redo"):
            scene_editor.redo(task_id)

    def test_saving_the_same_thing_again_is_not_a_step(self, task_root):
        """提示词框一失焦就存一次。没改内容的那一次不该占掉一步撤销。"""
        task_id, _ = _reviewed_task(task_root)
        keyword = _scenes(task_id)[0]["keyword"]
        scene_editor.update_scene(task_id, 0, keyword=keyword)
        assert scene_editor.history_state(task_id)["undo"] == 0

    def test_undoing_is_not_recorded_as_an_edit_of_its_own(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="one")
        scene_editor.undo(task_id)
        # 撤销之后：没有可撤销的了。如果撤销把自己也记了一笔，这里会是 1，
        # 再按一次 Ctrl+Z 就把刚撤销的东西做了回来。
        assert scene_editor.history_state(task_id)["undo"] == 0

    def test_a_picture_arriving_for_the_first_time_is_not_a_step(self, task_root):
        """开画之后每出一张图就写一次清单。那是出图在往前走，不是编辑：
        记下来的话两百张图会把真正的编辑全挤出撤销栈。"""
        task_id, materials = _reviewed_task(task_root)
        scenes = _scenes(task_id)
        drawn = scenes[0]["material"]
        scenes[0]["material"] = ""
        with edit_history.paused():
            scene_manifest.save_scenes(task_id, scenes)

        scenes = _scenes(task_id)
        scenes[0]["material"] = drawn
        scene_manifest.save_scenes(task_id, scenes)
        assert scene_editor.history_state(task_id)["undo"] == 0

        # 而已经有图的那一段换一张，是一步。
        scenes = _scenes(task_id)
        scenes[0]["material"] = materials[1]
        scene_manifest.save_scenes(task_id, scenes)
        assert scene_editor.history_state(task_id)["undo_label"] == "Change picture"

    def test_undoing_an_earlier_step_does_not_take_the_pictures_away(
        self, task_root
    ):
        """改了一句提示词，然后图才画出来。撤销那句提示词，图得留着：画图不是
        一步编辑，所以也不该被别的一步顺手倒回去。"""
        task_id, _ = _reviewed_task(task_root)
        scenes = _scenes(task_id)
        drawn = [scene["material"] for scene in scenes]
        for scene in scenes:
            scene["material"] = ""
        with edit_history.paused():
            scene_manifest.save_scenes(task_id, scenes)

        before = _scenes(task_id)[0]["keyword"]
        scene_editor.update_scene(task_id, 0, keyword="something else")
        # 图一张一张画出来。
        for at, material in enumerate(drawn):
            scenes = _scenes(task_id)
            scenes[at]["material"] = material
            scene_manifest.save_scenes(task_id, scenes)

        scene_editor.undo(task_id)
        after = _scenes(task_id)
        assert after[0]["keyword"] == before
        assert [scene["material"] for scene in after] == drawn

        scene_editor.redo(task_id)
        after = _scenes(task_id)
        assert after[0]["keyword"] == "something else"
        assert [scene["material"] for scene in after] == drawn

    def test_what_the_program_fills_in_does_not_throw_redo_away(self, task_root):
        """撤销之后界面重新读清单，读的时候程序会把旁白配回各段并存一次。
        那不是用户的编辑：把它当成编辑的话，"重做"当场就没了。"""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="one")
        scene_editor.undo(task_id)

        scenes = _scenes(task_id)
        scenes[1]["narration"] = "paired back by the program"
        with edit_history.automatic():
            scene_manifest.save_scenes(task_id, scenes)

        state = scene_editor.history_state(task_id)
        assert (state["undo"], state["redo"]) == (0, 1)
        scene_editor.redo(task_id)
        after = _scenes(task_id)
        assert after[0]["keyword"] == "one"
        # 程序补上的那一句也还在，没有被"重做"盖回去。
        assert after[1]["narration"] == "paired back by the program"

    def test_pairing_the_narration_is_done_as_the_program(self):
        source = (ROOT / "app/services/scene_editor.py").read_text(encoding="utf-8")
        assert re.search(
            r"with edit_history\.automatic\(\):\s+"
            r"scene_manifest\.apply_narration_pairing\(", source
        )

    def test_the_history_is_bounded(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        for at in range(edit_history.LIMIT + 5):
            scene_editor.update_scene(task_id, 0, keyword=f"k{at}")
        assert scene_editor.history_state(task_id)["undo"] == edit_history.LIMIT

    def test_it_survives_a_restart(self, task_root):
        """历史在任务目录里，不在内存里。"""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="one")
        stored = json.loads(
            (task_root / task_id / edit_history.FILE_NAME).read_text("utf-8")
        )
        assert len(stored["undo"]) == 1 and stored["redo"] == []

    def test_a_running_video_cannot_be_undone_under_the_renderer(self, task_root):
        from app.models import const
        from app.services import state as sm

        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="one")
        with patch.object(sm.state, "get_task",
                          return_value={"state": const.TASK_STATE_PROCESSING}):
            with pytest.raises(scene_editor.SceneBusyError):
                scene_editor.undo(task_id)
        assert _scenes(task_id)[0]["keyword"] == "one"

    def test_asking_about_a_video_that_does_not_exist_creates_nothing(
        self, task_root
    ):
        assert edit_history.state("no-such-video")["undo"] == 0
        assert not (task_root / "no-such-video").exists()


class TestWhatUndoLeavesAlone:
    def test_the_record_of_earlier_pictures_is_not_rewound(self, task_root):
        """换一张图，再改提示词，再撤销提示词：那张被换掉的图仍然得在
        "画过的图"里找得到。"""
        task_id, materials = _reviewed_task(task_root)
        other = _material_file(task_root, task_id, "other.mp4")
        with patch.object(scene_editor.material, "download_videos",
                          return_value=[other]):
            scene_editor.regenerate_material(task_id, 0)
        remembered = _scenes(task_id)[0].get("history")
        assert remembered
        scene_editor.update_scene(task_id, 0, keyword="a fox")
        scene_editor.undo(task_id)
        assert _scenes(task_id)[0].get("history") == remembered

    def test_the_snapshots_do_not_carry_the_picture_history(self, task_root):
        """那份记录很大，每一步都抄一遍，历史文件会比清单本身大几十倍。"""
        task_id, _ = _reviewed_task(task_root)
        other = _material_file(task_root, task_id, "other.mp4")
        with patch.object(scene_editor.material, "download_videos",
                          return_value=[other]):
            scene_editor.regenerate_material(task_id, 0)
        scene_editor.update_scene(task_id, 0, keyword="a fox")
        stored = (task_root / task_id / edit_history.FILE_NAME).read_text("utf-8")
        assert '"history"' not in stored

    def test_undoing_changed_words_brings_back_whether_the_voice_matched(
        self, task_root
    ):
        task_id, _ = _reviewed_task(task_root)
        assert scene_manifest.load(task_id)["audio_stale"] is False
        scene_editor.update_scene(task_id, 0, narration="New words.")
        assert scene_manifest.load(task_id)["audio_stale"] is True
        scene_editor.undo(task_id)
        # 台词回到了配音念的那一句，就不该再催着重新配音。
        assert scene_manifest.load(task_id)["audio_stale"] is False
        scene_editor.redo(task_id)
        assert scene_manifest.load(task_id)["audio_stale"] is True


class TestSplit:
    def test_the_two_halves_play_exactly_what_the_scene_played(
        self, task_root, ten_second_clips
    ):
        task_id, materials = _reviewed_task(task_root)
        scene_editor.update_timing(task_id, 0, trim_start=1, duration=6)
        scene_editor.split_scene(task_id, 0, 2.5)
        first, second = _scenes(task_id)[:2]
        assert first["material"] == second["material"] == materials[0]
        assert (first["trim_start"], first["duration"]) == (1.0, 2.5)
        assert (second["trim_start"], second["duration"]) == (3.5, 3.5)
        # 接缝处是连续的，两段加起来还是那 6 秒。
        assert first["trim_start"] + first["duration"] == second["trim_start"]
        assert first["duration"] + second["duration"] == 6

    def test_a_scene_with_no_set_length_is_measured_first(
        self, task_root, ten_second_clips
    ):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.split_scene(task_id, 0, 4)
        first, second = _scenes(task_id)[:2]
        assert (first["duration"], second["trim_start"], second["duration"]) == (
            4.0, 4.0, 6.0,
        )

    def test_the_cut_is_in_timeline_time_when_the_clip_is_slowed_down(
        self, task_root, ten_second_clips
    ):
        """半速播放时，时间线上的 2 秒只走过素材的 1 秒。"""
        from app.services import task_artifacts

        task_id, _ = _reviewed_task(task_root)
        payload = scene_manifest.load_raw(task_id)
        payload["params"]["video_clip_speed"] = 0.5
        task_artifacts.patch_script_data(task_id, params=payload["params"])
        scene_editor.update_timing(task_id, 0, trim_start=0, duration=8)
        scene_editor.split_scene(task_id, 0, 2)
        second = _scenes(task_id)[1]
        assert (second["trim_start"], second["duration"]) == (1.0, 6.0)

    def test_the_new_half_is_its_own_scene(self, task_root, ten_second_clips):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="Hand-written.")
        scene_editor.split_scene(task_id, 0, 3)
        first, second = _scenes(task_id)[:2]
        assert first["id"] != second["id"]
        assert first["narration"] == "Hand-written."
        # 手改的那句留在前一段，不抄到后一段去说两遍。
        assert not second.get("narration_edited")
        assert second["keyword"] == first["keyword"]
        assert len(_scenes(task_id)) == 3

    @pytest.mark.parametrize("at", [0.05, 9.95, 10, 25, -1])
    def test_a_cut_that_would_leave_a_sliver_is_refused(
        self, task_root, ten_second_clips, at
    ):
        task_id, _ = _reviewed_task(task_root)
        before = _shape(task_id)
        with pytest.raises(scene_editor.SceneError, match="split it between"):
            scene_editor.split_scene(task_id, 0, at)
        assert _shape(task_id) == before
        assert scene_editor.history_state(task_id)["undo"] == 0

    def test_a_scene_with_no_picture_yet_cannot_be_split(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scenes = _scenes(task_id)
        scenes[0]["material"] = ""
        scene_manifest.save_scenes(task_id, scenes)
        with pytest.raises(scene_editor.SceneError, match="no picture yet"):
            scene_editor.split_scene(task_id, 0, 1)


class TestDuplicate:
    def test_the_copy_sits_right_after_and_is_its_own_scene(self, task_root):
        task_id, materials = _reviewed_task(task_root, scenes=3)
        scene_editor.duplicate_scene(task_id, 1)
        scenes = _scenes(task_id)
        assert [s["material"] for s in scenes] == [
            materials[0], materials[1], materials[1], materials[2],
        ]
        assert scenes[1]["id"] != scenes[2]["id"]
        assert scenes[2]["keyword"] == scenes[1]["keyword"]

    def test_the_copy_keeps_the_trim(self, task_root, ten_second_clips):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_timing(task_id, 0, trim_start=2, duration=3)
        scene_editor.duplicate_scene(task_id, 0)
        copy = _scenes(task_id)[1]
        assert (copy["trim_start"], copy["duration"]) == (2.0, 3.0)


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_split_duplicate_undo_and_redo(self, client, task_root, ten_second_clips):
        task_id, _ = _reviewed_task(task_root)
        base = f"/api/v1/tasks/{task_id}"

        got = client.post(f"{base}/scenes/0/split", json={"at": 4})
        assert got.status_code == 200, got.text
        assert got.json()["data"]["selected"] == 1
        assert len(_scenes(task_id)) == 3

        got = client.post(f"{base}/scenes/0/duplicate")
        assert got.status_code == 200, got.text
        assert len(_scenes(task_id)) == 4

        assert client.get(f"{base}/history").json()["data"]["undo"] == 2
        undone = client.post(f"{base}/history/undo")
        assert undone.status_code == 200, undone.text
        assert undone.json()["data"]["label"] == "Duplicate scene"
        assert len(_scenes(task_id)) == 3
        assert client.post(f"{base}/history/redo").status_code == 200
        assert len(_scenes(task_id)) == 4

    def test_the_refusals(self, client, task_root, ten_second_clips):
        task_id, _ = _reviewed_task(task_root)
        base = f"/api/v1/tasks/{task_id}"
        assert client.post(f"{base}/history/undo").status_code == 400
        assert client.post(f"{base}/history/sideways").status_code == 404
        assert client.post(f"{base}/scenes/0/split", json={"at": 0}).status_code == 400
        sliver = client.post(f"{base}/scenes/0/split", json={"at": 9.99})
        assert sliver.status_code == 400
        assert "split it between" in sliver.json()["message"]
        assert client.post(f"{base}/scenes/9/duplicate").status_code == 400
        assert client.get("/api/v1/tasks/nope/history").status_code == 404


class TestTheEditorOffersIt:
    @staticmethod
    def _read(name: str) -> str:
        return (ROOT / "resource" / "public" / "editor" / name).read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _body(source: str, name: str) -> str:
        start = source.index(f"function {name}(")
        ends = [at for at in (source.find("\nfunction ", start + 1),
                              source.find("\nasync function ", start + 1)) if at > 0]
        return source[start:min(ends)]

    def _keys(self) -> str:
        script = self._read("editor.js")
        start = script.index("if (event.altKey || typingSomewhere(event)")
        return script[start:script.index("/* ---------- thumbnails", start)]

    def test_the_bar_sits_right_above_the_timeline(self):
        page = self._read("index.html")
        bar = page.index('id="edit-bar"')
        assert page.index("</main>") < bar < page.index('<section class="timeline">')
        for dom_id in ("undo", "redo", "split", "duplicate", "delete-scene"):
            assert f'id="{dom_id}"' in page[bar:page.index('<section class="timeline">')]

    def test_typing_in_a_box_is_never_taken_for_a_shortcut(self):
        """在提示词框里按空格、按 Delete、按 S，都得是打字。"""
        script = self._read("editor.js")
        guard = self._body(script, "typingSomewhere")
        for tag in ("INPUT", "TEXTAREA", "SELECT"):
            assert tag in guard
        assert "isContentEditable" in guard
        # 开着的窗口（新片子、设置）里也不抢。
        assert '.modal:not([hidden])' in guard
        keys = self._keys()
        assert keys.index("typingSomewhere(event)") < keys.index("stepHistory(")

    def test_the_shortcuts_people_already_know(self):
        keys = self._keys()
        for wired in (
            'ctrl && key === "z" && !event.shiftKey) stepHistory("undo")',
            'key === "y" || (key === "z" && event.shiftKey)',
            'ctrl && key === "d") duplicateSelected()',
            'key === " "',
            'key === "s") splitAtPlayhead()',
            'key === "Delete" || key === "Backspace") deleteSelected()',
            'key === "ArrowLeft"', 'key === "ArrowRight"',
            'key === "ArrowUp") stepScene(-1)', 'key === "ArrowDown") stepScene(1)',
        ):
            assert wired in keys, wired

    def test_a_key_it_does_not_handle_is_left_to_the_browser(self):
        """Ctrl+C、Ctrl+R、F5 这些不归它管，不能一律拦掉。"""
        keys = self._keys()
        assert "else if (ctrl) handled = false;" in keys
        assert "if (handled) event.preventDefault();" in keys

    def test_delete_does_not_ask_because_it_can_be_undone(self):
        body = self._body(self._read("editor.js"), "deleteSelected")
        assert "confirm(" not in body
        assert 'call("DELETE", scenePath(state.selected))' in body

    def test_split_cuts_where_the_playhead_is(self):
        script = self._read("editor.js")
        body = self._body(script, "splitAtPlayhead")
        assert "sceneUnderPlayhead()" in body
        assert "{ at: hit.inside }" in body
        assert "state.selected = hit.index + 1;" in body
        under = self._body(script, "sceneUnderPlayhead")
        assert "state.playhead - offsets[at]" in under

    def test_the_count_of_undoable_steps_follows_every_change(self):
        """挂在所有请求都经过的那个函数上，新加的操作不用记得来登记。"""
        script = self._read("editor.js")
        call = self._body(script, "call")
        assert "scheduleHistorySync();" in call
        assert 'method !== "GET"' in call
        assert '!path.includes("/history")' in call

    def test_nothing_can_be_cut_while_the_video_is_rendering(self):
        script = self._read("editor.js")
        assert "!state.taskBusy" in self._body(script, "canEdit")
        for name in ("splitAtPlayhead", "duplicateSelected", "deleteSelected",
                     "stepHistory"):
            assert "canEdit()" in self._body(script, name), name

    def test_every_element_the_script_looks_up_is_on_the_page(self):
        page = self._read("index.html")
        for dom_id in re.findall(r'el\("([a-z-]+)"\)', self._read("editor.js")):
            assert f'id="{dom_id}"' in page, dom_id
