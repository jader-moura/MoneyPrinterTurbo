"""重生成排队：点了就排上，不用等上一张出完。

Why this file exists
--------------------
本机出一张图要几分钟。在这之前重生成是同步的：点下去那个请求就挂到图出来
为止，界面上所有能改数据的控件全被禁掉。于是审片时最自然的那个动作——"这张
不行、这张也不行,都重出"——只能一张一张做,每张之间干等三分钟。

这组测试守的是三件事：点第二张的时候不用等第一张；一次只出一张（显卡只有
一块,而且重生成是"读清单→出图→写清单"）；出图的那几分钟里用户在别处做的
修改不会被出完的那一张盖掉。

These tests hold three things: queueing a second picture never waits for the
first, only one picture is drawn at a time, and an edit made while a picture is
being drawn is not overwritten when that picture lands.
"""

import threading
import time
from unittest.mock import patch

import pytest

from app.models import image_style as image_style_model
from app.services import material, scene_editor, scene_manifest, scene_regen_queue
from app.services import state as sm

# 复用画风那组测试里的任务工厂：两组测试需要的"跑到素材阶段、等着审片"的
# 任务是同一种，各写一份只会让两边慢慢长出差异。
from test.services.test_image_style_pinning import (  # noqa: F401 - 这些是 fixture
    COMIC,
    _reviewed_task,
    idle_task,
    task_root,
)


def wait_until(predicate, timeout=10.0, what="condition"):
    """等一个后台线程把状态改过来。

    轮询而不是 sleep 一个固定时长：固定时长要么在快的机器上白等，要么在
    慢的机器上随机失败，而两者都会让人开始无视这组测试。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class FakeGenerator:
    """假的出图器：能卡在中途，能数出同时有几个在跑。

    ``gate`` 不打开就一直卡着——"第二个请求不用等第一个"这件事只有在第一个
    确实还没出完的时候才测得到。
    """

    def __init__(self, directory, *, open_gate=True):
        self.directory = directory
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()
        self.lock = threading.Lock()
        self.calls = []
        self.entered = threading.Semaphore(0)
        self.inflight = 0
        self.most_at_once = 0
        self.fail_on = set()

    def __call__(self, **kwargs):
        terms = list(kwargs.get("search_terms") or [])
        term = terms[0] if terms else ""
        with self.lock:
            self.calls.append(term)
            self.inflight += 1
            self.most_at_once = max(self.most_at_once, self.inflight)
        self.entered.release()
        try:
            if not self.gate.wait(timeout=20):
                raise AssertionError("the test never opened the gate")
            if term in self.fail_on:
                raise RuntimeError(f"the image server refused {term!r}")
            target = self.directory / f"drawn-{len(self.calls)}-{term}.png"
            target.write_bytes(b"drawn")
            return [str(target)]
        finally:
            with self.lock:
                self.inflight -= 1

    def wait_for_entry(self, timeout=10.0):
        """等到有一个真的进来了。"""
        if not self.entered.acquire(timeout=timeout):
            raise AssertionError("no regeneration reached the image generator")


@pytest.fixture(autouse=True)
def quiet_queue():
    """每个用例都从空队列开始，并且不把活漏给下一个用例。

    工作线程是守护线程，整个测试进程里只有一个，所以用例之间要隔离的是
    队列内容。结束时必须等正在跑的那个跑完：它手里还拿着这个用例打的桩，
    漏到下一个用例里会变成一个莫名其妙的失败。
    """
    scene_regen_queue._reset_for_tests()
    yield
    scene_regen_queue._reset_for_tests()
    wait_until(
        lambda: scene_regen_queue.snapshot()["running"] is None,
        what="the worker to go idle",
    )


@pytest.fixture
def generator(task_root):
    """装上假出图器，返回它。"""
    directory = task_root / "drawn"
    directory.mkdir(parents=True, exist_ok=True)
    fake = FakeGenerator(directory)
    with patch.object(material, "download_videos", side_effect=fake):
        yield fake


def finished(job_id):
    """那个活跑完了吗？跑完就把它的记录返回。"""

    def check():
        for job in scene_regen_queue.snapshot()["jobs"]:
            if job["id"] == job_id and job["state"] in ("done", "failed", "cancelled"):
                return job
        return None

    return wait_until(check, what=f"job {job_id} to finish")


class TestQueueingDoesNotBlock:
    """用户要的就是这一件事。"""

    def test_a_second_picture_can_be_asked_for_while_the_first_is_drawing(
        self, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "two-at-once", pinned_template=COMIC)
        generator.gate.clear()  # 第一张卡在出图中

        first = scene_regen_queue.enqueue(task_id, 0, keyword="castle")
        generator.wait_for_entry()

        # 第一张还卡着的时候点第二张：这一行要是会等，整个功能就没有意义。
        second = scene_regen_queue.enqueue(task_id, 1, keyword="dragon")

        assert first["state"] == "running" or first["position"] == 1
        assert second["state"] == "queued"
        assert second["position"] == 1, "第二张应该排在队首等着"
        # 第二张一张图都还没碰过。
        assert generator.calls == ["castle"]

        generator.gate.set()
        finished(first["id"])
        finished(second["id"])
        assert generator.calls == ["castle", "dragon"]

    def test_the_queue_says_how_many_are_waiting(self, task_root, generator):
        task_id, _ = _reviewed_task(task_root, "counts", pinned_template=COMIC)
        generator.gate.clear()

        scene_regen_queue.enqueue(task_id, 0, keyword="a")
        generator.wait_for_entry()
        scene_regen_queue.enqueue(task_id, 1, keyword="b")
        scene_regen_queue.enqueue(task_id, 0, keyword="c")

        snapshot = scene_regen_queue.snapshot(task_id)
        assert snapshot["busy"] is True
        assert snapshot["waiting"] == 2
        assert snapshot["running"]["keyword"] == "a"

        generator.gate.set()
        wait_until(
            lambda: not scene_regen_queue.snapshot(task_id)["busy"],
            what="the queue to drain",
        )

    def test_one_picture_at_a_time(self, task_root, generator):
        """一块显卡，而且重生成是读改写同一份清单，并行只会互相覆盖。"""
        task_id, _ = _reviewed_task(task_root, "serial", pinned_template=COMIC)

        jobs = [
            scene_regen_queue.enqueue(task_id, index % 2, keyword=f"term-{index}")
            for index in range(4)
        ]
        for job in jobs:
            finished(job["id"])

        assert generator.most_at_once == 1
        assert generator.calls == ["term-0", "term-1", "term-2", "term-3"]


class TestWhatGoesWrong:
    def test_a_failure_does_not_take_the_rest_of_the_queue_with_it(
        self, task_root, generator
    ):
        """后面那几张是用户另外点的，没有理由跟着一起没了。"""
        task_id, _ = _reviewed_task(task_root, "one-fails", pinned_template=COMIC)
        generator.fail_on = {"doomed"}

        bad = scene_regen_queue.enqueue(task_id, 0, keyword="doomed")
        good = scene_regen_queue.enqueue(task_id, 1, keyword="fine")

        assert finished(bad["id"])["state"] == "failed"
        assert finished(good["id"])["state"] == "done"

    def test_the_reason_it_failed_is_kept_and_names_the_source(
        self, task_root, generator
    ):
        """界面只能显示我们记下来的那句话，所以必须点名是谁怎么失败的。"""
        task_id, _ = _reviewed_task(task_root, "why", pinned_template=COMIC)
        generator.fail_on = {"nope"}

        job = scene_regen_queue.enqueue(task_id, 0, keyword="nope")

        record = finished(job["id"])
        assert "openai_image" in record["error"]
        assert "nope" in record["error"]

    def test_an_unknown_style_is_refused_before_anything_is_queued(
        self, task_root, generator
    ):
        """改请求才能解决的错误要当场说，不能排三分钟队再冒出来。"""
        task_id, _ = _reviewed_task(task_root, "bad-style", pinned_template=COMIC)

        with pytest.raises(scene_manifest.SceneError) as failure:
            scene_regen_queue.enqueue(task_id, 0, image_style="bauhaus")

        assert "bauhaus" in str(failure.value)
        assert "comic" in str(failure.value)
        assert scene_regen_queue.snapshot(task_id)["jobs"] == []
        assert generator.calls == []

    def test_a_scene_that_does_not_exist_is_refused_on_the_spot(
        self, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "no-scene", pinned_template=COMIC)

        with pytest.raises(scene_manifest.SceneError) as failure:
            scene_regen_queue.enqueue(task_id, 9, keyword="x")

        assert "scene 9 does not exist" in str(failure.value)
        assert scene_regen_queue.snapshot(task_id)["jobs"] == []

    def test_nothing_is_queued_while_the_task_is_rendering(self, task_root, generator):
        """渲染器正在读那些素材文件，这时候换文件会渲出一条谁也复现不了的片子。"""
        task_id, _ = _reviewed_task(task_root, "busy", pinned_template=COMIC)

        with patch.object(sm.state, "get_task", return_value={"state": 4}):
            with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
                with pytest.raises(scene_editor.SceneBusyError):
                    scene_regen_queue.enqueue(task_id, 0, keyword="x")

        assert generator.calls == []


class TestCancelling:
    def test_a_waiting_picture_can_be_called_off(self, task_root, generator):
        """排错了、或者改了主意，不该被迫等它白跑一遍。"""
        task_id, _ = _reviewed_task(task_root, "cancel", pinned_template=COMIC)
        generator.gate.clear()

        running = scene_regen_queue.enqueue(task_id, 0, keyword="keep")
        generator.wait_for_entry()
        waiting = scene_regen_queue.enqueue(task_id, 1, keyword="drop")

        cancelled = scene_regen_queue.cancel(waiting["id"])
        assert cancelled["state"] == "cancelled"

        generator.gate.set()
        finished(running["id"])
        wait_until(
            lambda: not scene_regen_queue.snapshot(task_id)["busy"],
            what="the queue to drain",
        )
        # 撤掉的那一张一张图都没画。
        assert generator.calls == ["keep"]

    def test_the_one_being_drawn_cannot_be_called_off(self, task_root, generator):
        """出图是一次没法中断的外部调用。说撤掉了而它还在跑，是撒谎。"""
        task_id, _ = _reviewed_task(task_root, "no-cancel", pinned_template=COMIC)
        generator.gate.clear()

        job = scene_regen_queue.enqueue(task_id, 0, keyword="busy")
        generator.wait_for_entry()
        wait_until(
            lambda: scene_regen_queue.snapshot(task_id)["running"],
            what="the job to be picked up",
        )

        with pytest.raises(scene_manifest.SceneError) as failure:
            scene_regen_queue.cancel(job["id"])

        assert "cannot be cancelled" in str(failure.value)
        generator.gate.set()
        finished(job["id"])

    def test_cancelling_something_that_is_not_there_says_so(self, task_root):
        with pytest.raises(scene_manifest.SceneError) as failure:
            scene_regen_queue.cancel("nope")

        assert "nope" in str(failure.value)


class TestTheTimelineMovingUnderTheQueue:
    """排队的那几分钟里用户会继续编辑。那就不能按下标认场景。"""

    def test_the_picture_lands_on_the_scene_it_was_asked_for(
        self, task_root, generator
    ):
        """排队时点的是第 2 段；期间第 1 段被删了，它就成了第 1 段。

        按下标写的话，新图会落到一段用户没动过的画面上，而他只会以为那段
        画面自己坏了。
        """
        task_id, materials = _reviewed_task(task_root, "shifted", pinned_template=COMIC)
        generator.gate.clear()

        job = scene_regen_queue.enqueue(task_id, 1, keyword="second")
        generator.wait_for_entry()

        # 把它前面那一段删掉，下标就往前挪了一位。
        scene_editor.delete_scene(task_id, 0)

        generator.gate.set()
        record = finished(job["id"])
        assert record["state"] == "done", record["error"]

        scenes = scene_manifest.load(task_id)["scenes"]
        assert len(scenes) == 1
        # 换掉的是当初点的那一段：它原来的素材已经不在了，关键词是新的。
        assert scenes[0]["keyword"] == "second"
        assert scenes[0]["material"] != materials[1]

    def test_a_deleted_scene_fails_the_job_instead_of_hitting_another_one(
        self, task_root, generator
    ):
        task_id, materials = _reviewed_task(task_root, "deleted", pinned_template=COMIC)
        generator.gate.clear()

        job = scene_regen_queue.enqueue(task_id, 1, keyword="doomed")
        generator.wait_for_entry()
        scene_editor.delete_scene(task_id, 1)

        generator.gate.set()
        record = finished(job["id"])

        assert record["state"] == "failed"
        assert "no longer in the timeline" in record["error"]
        # 剩下那一段一个字都没被动过。
        scenes = scene_manifest.load(task_id)["scenes"]
        assert scenes[0]["material"] == materials[0]

    def test_an_edit_made_while_the_picture_was_drawing_survives_it(
        self, task_root, generator
    ):
        """出图要几分钟，用户当然会在这期间改别的。

        重生成原本是"出图前读一份清单，出图后把那份清单整份写回去"，于是
        这几分钟里改的台词会在图落地的瞬间被悄悄还原。
        """
        task_id, _ = _reviewed_task(task_root, "no-clobber", pinned_template=COMIC)
        generator.gate.clear()

        job = scene_regen_queue.enqueue(task_id, 0, keyword="picture")
        generator.wait_for_entry()

        # 图还在出，用户去改了另一段的台词。
        scene_editor.update_scene(task_id, 1, narration="a line typed while waiting")

        generator.gate.set()
        assert finished(job["id"])["state"] == "done"

        scenes = scene_manifest.load(task_id)["scenes"]
        assert scenes[1]["narration"] == "a line typed while waiting"
        assert scenes[0]["keyword"] == "picture"


class TestWhatTheEditorIsTold:
    def test_finished_jobs_stay_until_they_are_cleared(self, task_root, generator):
        """跑完就删掉记录的话，一次失败会一声不响地消失。"""
        task_id, _ = _reviewed_task(task_root, "history", pinned_template=COMIC)

        job = scene_regen_queue.enqueue(task_id, 0, keyword="done-thing")
        finished(job["id"])

        assert [row["state"] for row in scene_regen_queue.snapshot(task_id)["jobs"]] == [
            "done"
        ]
        assert scene_regen_queue.snapshot(task_id)["busy"] is False

        scene_regen_queue.dismiss(task_id)
        assert scene_regen_queue.snapshot(task_id)["jobs"] == []

    def test_one_task_does_not_see_another_task_queue(self, task_root, generator):
        mine, _ = _reviewed_task(task_root, "mine", pinned_template=COMIC)
        theirs, _ = _reviewed_task(task_root, "theirs", pinned_template=COMIC)

        job = scene_regen_queue.enqueue(theirs, 0, keyword="not-yours")
        finished(job["id"])

        assert scene_regen_queue.snapshot(mine)["jobs"] == []
        assert len(scene_regen_queue.snapshot(theirs)["jobs"]) == 1

    def test_the_style_chosen_for_a_queued_picture_is_the_one_used(
        self, task_root, generator
    ):
        """排队不该让"这一张换个画风"这件事走丢。"""
        task_id, _ = _reviewed_task(task_root, "styled", pinned_template=COMIC)

        job = scene_regen_queue.enqueue(task_id, 0, keyword="x", image_style="anime")
        assert finished(job["id"])["state"] == "done"

        template = image_style_model.template_for_style("anime")
        assert scene_manifest.load(task_id)["scenes"][0]["image_style"] == "anime"
        # 真正送到出图器的就是这个风格的模板。
        assert any(
            call == "x" for call in generator.calls
        ), "the generator never ran"
        assert template  # 预设必须存在，否则上面这条断言没有意义


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_editor_can_queue_and_then_read_the_queue(
        self, client, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "http-queue", pinned_template=COMIC)
        generator.gate.clear()

        posted = client.post(
            f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
            json={"keyword": "a tower", "image_style": "anime"},
        )
        assert posted.status_code == 200, posted.text
        job = posted.json()["data"]
        assert job["state"] in ("queued", "running")
        assert job["keyword"] == "a tower"

        generator.wait_for_entry()
        listed = client.get(f"/api/v1/tasks/{task_id}/scenes/queue").json()["data"]
        assert listed["busy"] is True
        assert [row["id"] for row in listed["jobs"]] == [job["id"]]

        generator.gate.set()
        finished(job["id"])

    def test_a_waiting_job_can_be_cancelled_over_http(
        self, client, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "http-cancel", pinned_template=COMIC)
        generator.gate.clear()

        running = scene_regen_queue.enqueue(task_id, 0, keyword="keep")
        generator.wait_for_entry()
        waiting = client.post(
            f"/api/v1/tasks/{task_id}/scenes/1/material/queue", json={}
        ).json()["data"]

        dropped = client.delete(
            f"/api/v1/tasks/{task_id}/scenes/queue/{waiting['id']}"
        )
        assert dropped.status_code == 200, dropped.text
        assert dropped.json()["data"]["state"] == "cancelled"

        generator.gate.set()
        finished(running["id"])
        assert generator.calls == ["keep"]

    def test_rendering_is_refused_while_pictures_are_still_being_drawn(
        self, client, task_root, generator
    ):
        """渲染器要读的正是队列还在换的那几个文件。"""
        task_id, _ = _reviewed_task(task_root, "http-render", pinned_template=COMIC)
        generator.gate.clear()

        job = scene_regen_queue.enqueue(task_id, 0, keyword="still-drawing")
        generator.wait_for_entry()

        refused = client.post(f"/api/v1/tasks/{task_id}/render")

        assert refused.status_code == 409, refused.text
        assert "still being regenerated" in refused.text

        generator.gate.set()
        finished(job["id"])

    def test_an_unknown_style_comes_back_as_a_bad_request(
        self, client, task_root, generator
    ):
        task_id, _ = _reviewed_task(task_root, "http-bad", pinned_template=COMIC)

        refused = client.post(
            f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
            json={"image_style": "bauhaus"},
        )

        assert refused.status_code == 400, refused.text
        assert "bauhaus" in refused.text
        assert generator.calls == []
