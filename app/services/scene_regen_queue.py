"""重生成队列：排队出图，用户不用站在那儿等一张画完再点下一张。

为什么需要它
------------
本机出一张图要几分钟（实测 zimage-turbo 约 3 分钟）。在这之前，重生成是一个
同步接口：点下去，那个 HTTP 请求就一直挂到图出来为止，界面上所有改数据的
控件全都禁掉。于是审片时最自然的那个动作——"这张、这张、还有这张都不行，
都重出"——只能一张一张地做，每张之间干等三分钟。

现在点一下只是**排队**：请求立刻返回，用户接着点下一张，或者去改别处的
台词和时长。队列在服务端，所以刷新页面、关掉再打开编辑器，排着的活还在
跑；它不属于某一个浏览器标签页。

一次只出一张
------------
队列是全局 FIFO，一个工作线程。不是为了省事：本机出图服务就一块显卡，
同时塞两张只会两张都变慢；而且 ``regenerate_material`` 是"读清单 → 出图 →
写清单"，两条并发的流程会互相覆盖掉对方写进去的那一段。

错误不汇总
----------
每个任务各自记着自己失败的原因，点名是哪个源怎么失败的，和同步接口返回的
是同一句话。一张失败不影响后面排着的：那是用户另外点的，没有理由跟着一起
没了。

The queue exists because one local image takes minutes, during which the old
synchronous endpoint held the request open and disabled every control. Now a
click only enqueues: the request returns at once and the user can queue the
next picture or edit something else. The queue lives in the server, so it
survives a page reload and is not tied to one browser tab.

One picture at a time, globally: the local image server has a single GPU, and
``regenerate_material`` is a read-modify-write of the scene manifest, so two
concurrent runs would overwrite each other. A failure is recorded per job with
the same named reason the synchronous endpoint gives, and never cancels the
jobs queued behind it.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

from loguru import logger

from app.services import scene_editor
from app.services.scene_manifest import SceneError

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

#: 还排着和正在跑的活永远留着；跑完的只留最近这些，否则一个长期不重启的
#: 进程会把每一次重生成都记到内存里。界面只需要它们来显示"刚完成"和
#: "为什么失败"。
#: Finished jobs are kept only so the UI can show what just completed and why
#: something failed; without a cap a long-lived process would remember every
#: regeneration it ever ran.
_HISTORY_LIMIT = 40


class _Job:
    """一次重生成。字段都是普通值，方便直接发给界面。"""

    __slots__ = (
        "id",
        "task_id",
        "index",
        "keyword",
        "image_style",
        "material",
        "state",
        "error",
        "enqueued_at",
        "started_at",
        "finished_at",
    )

    def __init__(
        self,
        task_id: str,
        index: int,
        keyword: str,
        image_style: Optional[str],
        material: str,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.task_id = task_id
        self.index = index
        self.keyword = keyword
        self.image_style = image_style
        # 排队时这一段用的素材文件。队列排着的时候用户可能删掉、插入或者
        # 重排场景，下标就不再指向同一段画面了，所以执行前按这个文件把它
        # 找回来。见 ``scene_editor.regenerate_material`` 的 ``expect_material``。
        self.material = material
        self.state = QUEUED
        self.error = ""
        self.enqueued_at = time.time()
        self.started_at = 0.0
        self.finished_at = 0.0

    def payload(self, position: int = 0) -> dict:
        now = time.time()
        if self.state == RUNNING:
            elapsed = now - self.started_at
        elif self.finished_at:
            elapsed = self.finished_at - self.started_at
        else:
            elapsed = 0.0
        return {
            "id": self.id,
            "task_id": self.task_id,
            "index": self.index,
            "keyword": self.keyword,
            # ``None`` 和 "" 在重生成里不是一回事（沿用这一段上次挑的风格 /
            # 回到任务的风格），所以这里原样带出去，不要折成空字符串。
            "image_style": self.image_style,
            "state": self.state,
            "error": self.error,
            # 排在第几个，1 表示下一个就是它。跑着的和跑完的是 0。
            "position": position,
            "elapsed": round(max(elapsed, 0.0), 2),
            "waited": round(max((self.started_at or now) - self.enqueued_at, 0.0), 2),
        }


_LOCK = threading.Lock()
_READY = threading.Condition(_LOCK)
_QUEUE: list[_Job] = []
_HISTORY: list[_Job] = []
_RUNNING: Optional[_Job] = None
_WORKER: Optional[threading.Thread] = None


def _ensure_worker() -> None:
    """按需起那一个工作线程。``_LOCK`` 必须已经拿在手里。

    守护线程：进程退出时不该被一张还没出完的图拖住，排着的活本来就只活在
    内存里。
    """
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    _WORKER = threading.Thread(
        target=_work, name="scene-regen-queue", daemon=True
    )
    _WORKER.start()


def enqueue(
    task_id: str,
    index: Any,
    *,
    keyword: Optional[str] = None,
    image_style: Optional[str] = None,
) -> dict:
    """把一次重生成排进队列，立刻返回。

    排得进去之前先把该报的错报掉：任务在渲染、下标越界、这个素材源不按
    关键词出图、风格 id 不认识——这些都是改请求才能解决的问题,等三分钟
    之后再说毫无意义,而且用户会以为是出图失败。真正可能慢的只有出图本身。

    Validation that a different request would fix -- a busy task, an index out
    of range, a source that does not generate from keywords, an unknown style
    id -- happens here, synchronously, so the caller is told now instead of
    minutes later as if the generator had failed.
    """
    plan = scene_editor.check_regenerate(
        task_id, index, keyword=keyword, image_style=image_style
    )
    job = _Job(
        task_id=task_id,
        index=plan["index"],
        keyword=plan["keyword"],
        image_style=image_style,
        material=plan["material"],
    )
    with _LOCK:
        _QUEUE.append(job)
        position = len(_QUEUE)
        _ensure_worker()
        _READY.notify()
    logger.info(
        f"queued a regeneration of scene {job.index} of task {task_id} "
        f"with keyword {job.keyword!r} (position {position})"
    )
    return job.payload(position)


def snapshot(task_id: str = "") -> dict:
    """这个任务的队列现状。不给 ``task_id`` 就是全部任务的。

    界面靠它画"排着 3 张、正在出第 2 段"和失败原因，所以跑完的也要带上，
    不然一次失败会一声不响地消失。
    """
    with _LOCK:
        running = _RUNNING
        queued = list(_QUEUE)
        history = list(_HISTORY)

    def mine(job: _Job) -> bool:
        return not task_id or job.task_id == task_id

    jobs = [job.payload() for job in history if mine(job)]
    if running is not None and mine(running):
        jobs.append(running.payload())
    jobs.extend(
        job.payload(position) for position, job in enumerate(queued, 1) if mine(job)
    )
    waiting = sum(1 for job in queued if mine(job))
    running_payload = (
        running.payload() if running is not None and mine(running) else None
    )
    return {
        "jobs": jobs,
        "waiting": waiting,
        "running": running_payload,
        # 界面用这一个值决定要不要继续轮询。
        "busy": bool(waiting or running_payload),
    }


def cancel(job_id: str) -> dict:
    """撤掉一个还排着的活。

    已经在出图的那一张撤不掉——出图是一次没法中断的外部调用，谎称撤掉了
    只会让用户看着它继续跑。
    """
    wanted = str(job_id or "").strip()
    with _LOCK:
        if _RUNNING is not None and _RUNNING.id == wanted:
            raise SceneError(
                f"job {wanted} is already generating and cannot be cancelled; "
                "it will finish on its own"
            )
        for position, job in enumerate(_QUEUE):
            if job.id == wanted:
                _QUEUE.pop(position)
                job.state = CANCELLED
                job.finished_at = time.time()
                _remember(job)
                logger.info(f"cancelled queued regeneration {wanted}")
                return job.payload()
    raise SceneError(f"no queued regeneration with id {wanted!r}")


def dismiss(task_id: str = "") -> int:
    """把跑完的记录清掉，返回清掉了几条。排着的和正在跑的不动。"""
    with _LOCK:
        keep = [
            job
            for job in _HISTORY
            if task_id and job.task_id != task_id
        ]
        removed = len(_HISTORY) - len(keep)
        _HISTORY[:] = keep
    return removed


def _remember(job: _Job) -> None:
    """记下一个跑完的活。``_LOCK`` 必须已经拿在手里。"""
    _HISTORY.append(job)
    if len(_HISTORY) > _HISTORY_LIMIT:
        del _HISTORY[: len(_HISTORY) - _HISTORY_LIMIT]


def _take() -> Optional[_Job]:
    """等到有活可干，把它标成正在跑。没有就一直等。"""
    global _RUNNING
    with _READY:
        while not _QUEUE:
            _READY.wait()
        job = _QUEUE.pop(0)
        job.state = RUNNING
        job.started_at = time.time()
        _RUNNING = job
        return job


def _work() -> None:
    while True:
        job = _take()
        if job is None:  # pragma: no cover - _take 只在有活时返回
            continue
        _run(job)


def _run(job: _Job) -> None:
    global _RUNNING
    try:
        scene = scene_editor.regenerate_material(
            job.task_id,
            job.index,
            keyword=job.keyword,
            image_style=job.image_style,
            expect_material=job.material,
        )
    except SceneError as exc:
        # 场景层的错误本来就是给人看的（哪个源失败了、下标越界多少），
        # 照原样留着。
        job.state = FAILED
        job.error = str(exc)
        logger.warning(
            f"queued regeneration of scene {job.index} of task {job.task_id} "
            f"failed: {exc}"
        )
    except Exception as exc:  # noqa: BLE001 - 工作线程不能因为一次意外就死掉
        # 这个线程挂掉的话，后面排着的活会永远停在"排队中"，而界面上看起来
        # 只是很慢。所以什么都接住，记下来，接着干下一个。
        job.state = FAILED
        job.error = f"unexpected error: {exc}"
        logger.exception(
            f"queued regeneration of scene {job.index} of task {job.task_id} "
            f"hit an unexpected error"
        )
    else:
        job.state = DONE
        # 执行时下标可能已经变了（队列排着的时候用户删了前面的某一段），
        # 界面要显示的是真正换掉的那一段。
        try:
            job.index = int(scene.get("index", job.index))
        except (TypeError, ValueError):
            pass
        logger.success(
            f"queued regeneration finished for scene {job.index} "
            f"of task {job.task_id}"
        )
    finally:
        job.finished_at = time.time()
        with _LOCK:
            _RUNNING = None
            _remember(job)


def _reset_for_tests() -> None:
    """只给测试用：清空队列和历史。

    工作线程不停——它是守护线程，而且停一个线程比重新起一个更容易留下
    半个状态。测试之间要隔离的是队列内容，不是那个线程。
    """
    global _RUNNING
    with _LOCK:
        _QUEUE.clear()
        _HISTORY.clear()
        _RUNNING = None
