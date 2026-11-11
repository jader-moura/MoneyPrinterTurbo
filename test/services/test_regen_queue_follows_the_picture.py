"""排着的活要跟着那张图走，不跟着下标走。

Why this file exists
--------------------
队列里的活记着"排队那一刻这是第几段"。出图要几分钟，而用户在这几分钟里干
的事恰恰就是接着审片——拖动画面、删掉几段。下标一旦不再指向同一张图，界面
就把"正在重出"标在另一段上：那一段用户根本没点过，看起来像是程序自己在乱
改别的画面。

真正写回去的位置一直是对的——执行那边按素材文件把那一段找回来
（``scene_editor._locate_scene``）。错的只有显示。这组用例把显示也钉在同一个
口径上：队列快照里的 ``scene`` 是按素材文件现算出来的位置。

The worker has always resolved the scene by its material file, so results land
on the right picture; only the display counted positions. These tests pin the
display to the same footing.
"""

import threading
from unittest.mock import patch

import pytest

from app.services import scene_editor, scene_manifest, scene_regen_queue
from app.controllers.v1 import editor as editor_routes

from test.services.test_image_style_pinning import (  # noqa: F401 - fixtures
    COMIC,
    _reviewed_task,
    idle_task,
    task_root,
)
from test.services.test_scene_regen_queue import (  # noqa: F401 - fixtures
    FakeGenerator,
    finished,
    generator,
    wait_until,
)


def _queued_job(task_id, index, keyword):
    return scene_regen_queue.enqueue(task_id, index, keyword=keyword)


def _job_in(task_id, job_id):
    for job in scene_regen_queue.snapshot(task_id)["jobs"]:
        if job["id"] == job_id:
            return job
    raise AssertionError(f"job {job_id} is not in the snapshot")


class TestAQueuedJobFollowsItsPicture:
    def test_reordering_moves_the_job_with_the_picture(self, task_root, generator):
        """用户报的就是这一条：排好队再去拖画面，标记跑到别的图上去了。"""
        task_id, materials = _reviewed_task(task_root, "reorder", pinned_template=COMIC)
        generator.gate.clear()

        # 第 0 段卡在出图中，第 1 段排在后面。
        running = _queued_job(task_id, 0, "castle")
        generator.wait_for_entry()
        waiting = _queued_job(task_id, 1, "dragon")

        # 现在把两段对调。被重出的那张图从第 0 段跑到了第 1 段。
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.reorder_scenes(task_id, [1, 0])

        moved = _job_in(task_id, running["id"])
        assert moved["scene"] == 1
        assert _job_in(task_id, waiting["id"])["scene"] == 0

        # 这一行是整个修复的意思：排队时记下的下标现在已经指向另一张图了，
        # 界面要是还照着它画，"正在重出"就会标在用户没点过的那一段上。
        assert moved["index"] == 0, "排队时记的下标不变"
        assert moved["scene"] != moved["index"], "显示必须用现算的位置"

        generator.gate.set()
        finished(running["id"])
        finished(waiting["id"])

    def test_the_picture_that_is_being_redrawn_is_the_one_the_user_picked(
        self, task_root, generator
    ):
        """重排之后,"正在重出"必须标在用户当初点的那张图上。"""
        task_id, materials = _reviewed_task(task_root, "which", pinned_template=COMIC)
        generator.gate.clear()

        job = _queued_job(task_id, 0, "castle")
        generator.wait_for_entry()
        picked = scene_manifest.load(task_id)["scenes"][0]["material"]

        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.reorder_scenes(task_id, [1, 0])

        at = _job_in(task_id, job["id"])["scene"]
        assert scene_manifest.load(task_id)["scenes"][at]["material"] == picked

        generator.gate.set()
        finished(job["id"])

    def test_an_untouched_timeline_still_reports_the_same_scene(
        self, task_root, generator
    ):
        """没动过时间线的常规情况一个字都不该变。"""
        task_id, _ = _reviewed_task(task_root, "still", pinned_template=COMIC)
        generator.gate.clear()

        job = _queued_job(task_id, 1, "dragon")
        wait_until(
            lambda: _job_in(task_id, job["id"])["state"] in ("queued", "running"),
            what="the job to register",
        )
        assert _job_in(task_id, job["id"])["scene"] == 1

        generator.gate.set()
        finished(job["id"])

    def test_deleting_the_picture_reports_no_scene_rather_than_someone_else_s(
        self, task_root, generator
    ):
        """那张图被删了就说"没有了",不能报一个编号——报出来的是别人的。"""
        task_id, _ = _reviewed_task(task_root, "deleted", pinned_template=COMIC)
        generator.gate.clear()

        job = _queued_job(task_id, 0, "castle")
        generator.wait_for_entry()

        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.delete_scene(task_id, 0)

        assert _job_in(task_id, job["id"])["scene"] is None

        generator.gate.set()
        wait_until(
            lambda: not scene_regen_queue.snapshot(task_id)["busy"],
            what="the queue to drain",
        )


class TestAFinishedJobStillPointsAtItsPicture:
    def test_a_finished_job_points_at_the_picture_it_redrew(
        self, task_root, generator
    ):
        """身份不随出图改变，所以出完之后照样认得出是哪一段。"""
        task_id, _ = _reviewed_task(task_root, "done", pinned_template=COMIC)

        job = _queued_job(task_id, 1, "dragon")
        finished(job["id"])

        done = _job_in(task_id, job["id"])
        assert done["state"] == "done"
        assert done["scene"] == 1
        scene = scene_manifest.load(task_id)["scenes"][1]
        assert done["scene_id"] == scene["id"]
        # 素材换成了刚出的那张，身份没变。
        assert done["material"] == scene["material"]

    def test_a_finished_job_follows_a_later_reorder(self, task_root, generator):
        task_id, _ = _reviewed_task(task_root, "done-moved", pinned_template=COMIC)

        job = _queued_job(task_id, 0, "castle")
        finished(job["id"])

        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.reorder_scenes(task_id, [1, 0])

        assert _job_in(task_id, job["id"])["scene"] == 1


class TestTheSnapshotStaysUsable:
    def test_a_job_carries_the_identity_of_its_picture(self, task_root, generator):
        task_id, materials = _reviewed_task(task_root, "anchor", pinned_template=COMIC)
        generator.gate.clear()

        job = _queued_job(task_id, 0, "castle")
        generator.wait_for_entry()

        queued = _job_in(task_id, job["id"])
        assert queued["scene_id"] == scene_manifest.load(task_id)["scenes"][0]["id"]
        assert queued["material"] == materials[0]

        generator.gate.set()
        finished(job["id"])

    def test_an_unreadable_manifest_does_not_break_the_queue_view(
        self, task_root, generator
    ):
        """清单读不出来时,"还剩几张在排队"这件事仍然要问得出来。"""
        task_id, _ = _reviewed_task(task_root, "nomanifest", pinned_template=COMIC)
        generator.gate.clear()

        job = _queued_job(task_id, 0, "castle")
        generator.wait_for_entry()

        with patch.object(
            scene_regen_queue.scene_manifest, "load", side_effect=OSError("gone")
        ):
            snapshot = scene_regen_queue.snapshot(task_id)
        assert snapshot["busy"] is True
        assert snapshot["running"]["keyword"] == "castle"
        assert snapshot["jobs"][0]["scene"] is None

        generator.gate.set()
        finished(job["id"])


class TestTheUiCanMatchTheTwoEndpoints:
    """界面要拿队列里的活去时间线上找那一段，两边得能对上。

    这一条是真正让修复失效的那个细节：时间线接口发给界面的 ``material`` 是
    文件名（``1a2b….mp4``），而队列里记的是整条路径
    （``…/storage/tasks/<id>/1a2b….mp4``）。直接比字符串永远不相等，于是界面
    一个也找不到,又退回那个会说谎的下标——看起来就是"改了位置还是标在错的
    图上"。两边都退到文件名为止才对得上。
    """

    def test_an_older_manifest_falls_back_to_the_file_name(self):
        """老清单里没有 id，那时的身份就是素材文件名。"""
        assert scene_manifest.scene_id(
            {"material": "C:\\tasks\\abc\\1a2b.zoom-99.mp4"}
        ) == "1a2b.zoom-99.mp4"
        assert scene_manifest.scene_id({"id": "abc123", "material": "x.mp4"}) == "abc123"
        assert scene_manifest.scene_id({}) == ""

    def test_the_file_name_is_what_matches(self):
        assert scene_manifest.material_key(
            "C:\\tasks\\abc\\1a2b.zoom-99.mp4"
        ) == "1a2b.zoom-99.mp4"
        assert scene_manifest.material_key(
            "/srv/tasks/abc/1a2b.zoom-99.mp4"
        ) == "1a2b.zoom-99.mp4"
        assert scene_manifest.material_key("1a2b.zoom-99.mp4") == "1a2b.zoom-99.mp4"
        assert scene_manifest.material_key(None) == ""

    def test_the_queue_and_the_timeline_name_the_same_picture(
        self, task_root, generator
    ):
        """两条接口要说得出同一段画面：界面靠这一点把活对回时间线。"""
        task_id, _ = _reviewed_task(task_root, "match", pinned_template=COMIC)
        generator.gate.clear()
        job = _queued_job(task_id, 1, "dragon")
        generator.wait_for_entry()

        shown = scene_editor.describe(task_id, "")["scenes"]
        queued = _job_in(task_id, job["id"])

        at = [
            index for index, scene in enumerate(shown)
            if scene["id"] == queued["scene_id"]
        ]
        assert at == [queued["scene"]], (
            "界面按素材文件找到的那一段，必须就是服务端算出来的那一段"
        )

        generator.gate.set()
        finished(job["id"])


class TestEveryAnchorMeansTheSameThing:
    """认人的地方只能有一套说法。

    ``_locate_scene`` 按 ``scene_manifest.scene_id`` 认人。谁要是自己拼一个
    锚点（比如拿素材文件名），对的就是另一套说法，结果是永远对不上——
    ``add_scene`` 正是这么坏掉的：插一段新画面必定报"那一段已经不在时间线
    上了"，而它明明就在。
    """

    def test_adding_a_scene_after_another_one_still_finds_it(
        self, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "insert", pinned_template=COMIC)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.add_scene(task_id, keyword="middle", after=0)
        shown = [s["keyword"] for s in scene_manifest.load(task_id)["scenes"]]
        assert shown == ["a", "middle", "b"]

    def test_a_new_scene_gets_its_own_identity(self, task_root, generator):
        task_id, _ = _reviewed_task(task_root, "newid", pinned_template=COMIC)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.add_scene(task_id, keyword="fresh")
        ids = [s["id"] for s in scene_manifest.load(task_id)["scenes"]]
        assert all(ids) and len(set(ids)) == len(ids)
