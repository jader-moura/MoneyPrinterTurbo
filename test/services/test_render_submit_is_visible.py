# -*- coding: utf-8 -*-
"""点了"渲染"之后，界面必须看得见它在跑。

用户的原话："I've clicked to render the final video, it's loading and loading
but nothing happens"。片子其实渲完了（11:25:45 提交，11:33:04 出片，7 分 19
秒），但那块挡板全程停在"正在提交"。

两处合起来才会这样：
1. ``_submit`` 把任务交给调度器就返回了，状态要等流水线线程自己去写。编辑器
   提交完立刻查状态，查到的是 404——重启过接口时更是一条都没有。
2. 编辑器那边查一次不到就不再跟着看了（``catch { break; }``），于是挡板永远
   转圈，而后台照样在渲。最坏的结果是用户以为没点上，再点一次，同一条片子
   渲两遍。
"""

import pytest

from app.controllers.v1 import editor
from app.models import const
from app.models.exception import HttpException
from app.controllers.manager.base_manager import TaskQueueFullError
from app.services import state as sm


class Accepts:
    """接住任务并记下那一刻状态里写的是什么。"""

    def __init__(self):
        self.seen = None
        self.calls = 0

    def add_task(self, func, **kwargs):
        self.calls += 1
        self.seen = sm.state.get_task(kwargs["task_id"])


class Refuses:
    def __init__(self, error):
        self.error = error

    def add_task(self, func, **kwargs):
        raise self.error


@pytest.fixture
def task_id():
    name = "test-submit-visible"
    sm.state.delete_task(name)
    yield name
    sm.state.delete_task(name)


def submit(task_id):
    return editor._submit(
        task_id, "req-1", object(), stop_at="video", start_from="video"
    )


class TestTheStatusIsThereWhenTheRequestAnswers:
    def test_the_task_is_already_running_when_the_scheduler_takes_it(
        self, monkeypatch, task_id
    ):
        """编辑器下一个请求就是查状态，所以提交返回时状态必须已经在那儿。"""
        scheduler = Accepts()
        monkeypatch.setattr(editor, "task_manager", scheduler)
        submit(task_id)
        assert scheduler.calls == 1
        assert scheduler.seen is not None, "调度器接手时状态里一条都没有"
        assert scheduler.seen["state"] == const.TASK_STATE_PROCESSING

    def test_it_survives_a_restart_that_wiped_the_state(self, monkeypatch, task_id):
        """状态是进程内存里的：重启过接口，这条任务在里面根本不存在。"""
        monkeypatch.setattr(editor, "task_manager", Accepts())
        assert sm.state.get_task(task_id) is None
        submit(task_id)
        task = sm.state.get_task(task_id)
        assert task and task["state"] == const.TASK_STATE_PROCESSING

    def test_it_answers_with_the_task_id(self, monkeypatch, task_id):
        monkeypatch.setattr(editor, "task_manager", Accepts())
        assert submit(task_id)["data"]["task_id"] == task_id


class TestNothingIsLeftRunningThatIsNotRunning:
    def test_a_full_queue_puts_the_old_status_back(self, monkeypatch, task_id):
        """交不出去时不能留下一个永远"在跑"的任务：编辑器会一直锁着控件。"""
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final-1.mp4"],
        )
        monkeypatch.setattr(
            editor, "task_manager", Refuses(TaskQueueFullError("queue is full"))
        )
        with pytest.raises(HttpException) as caught:
            submit(task_id)
        assert caught.value.status_code == 429
        task = sm.state.get_task(task_id)
        assert task["state"] == const.TASK_STATE_COMPLETE
        assert task["progress"] == 100
        assert task["videos"] == ["final-1.mp4"]

    def test_any_other_failure_also_puts_it_back(self, monkeypatch, task_id):
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED, progress=40)
        monkeypatch.setattr(editor, "task_manager", Refuses(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            submit(task_id)
        task = sm.state.get_task(task_id)
        assert task["state"] == const.TASK_STATE_FAILED
        assert task["progress"] == 40

    def test_a_task_that_had_no_status_leaves_no_ghost(self, monkeypatch, task_id):
        monkeypatch.setattr(editor, "task_manager", Refuses(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            submit(task_id)
        assert sm.state.get_task(task_id) is None


class TestTheEditorKeepsWatching:
    @staticmethod
    def _editor_source():
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    def test_one_failed_status_read_does_not_end_the_watch(self):
        source = self._editor_source()
        assert "const STATUS_RETRIES = 5;" in source
        assert "misses += 1;" in source
        assert "if (misses < STATUS_RETRIES) {" in source

    def test_giving_up_says_so_instead_of_spinning(self):
        source = self._editor_source()
        assert "function loseRender(error)" in source
        assert "if (state.wasBusy) loseRender(error);" in source
        assert "Lost track of the render" in source

    def test_it_stays_quiet_when_nothing_was_submitted(self):
        """打开页面时查不到状态是常事（重启过接口的旧任务），不该报警。"""
        source = self._editor_source()
        assert "loseRender(error);" in source
        assert "loseRender(error);" not in source.replace(
            "if (state.wasBusy) loseRender(error);", ""
        )

    def test_it_does_not_claim_the_video_is_ready_when_it_cannot_tell(self):
        """查不到状态时说"好了"就是撒谎——片子可能还在渲，也可能崩了。"""
        source = self._editor_source()
        start = source.index("function loseRender(error)")
        end = source.index("function finishRender(task)")
        assert "Your video is ready" not in source[start:end]
        assert "renderPlay.hidden = true" in source[start:end]


class TestItAlwaysSaysTheRenderIsDone:
    """渲完了必须收尾，而且必须说出来。

    两个漏洞让"渲完了"变成"界面毫无反应"：收尾挂在 ``await refresh()`` 后面
    （刷新一抛，挡板就永远转着），而收尾本身只画那块挡板——挡板被按"隐藏"
    收起来、或者页面是渲染中途打开的时候，界面上一个字都没有。
    """

    @staticmethod
    def _editor_source():
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    def test_a_failed_reload_does_not_leave_the_overlay_spinning(self):
        source = self._editor_source()
        watch = source[
            source.index("async function watchTask()") : source.index(
                "function setControlsEnabled("
            )
        ]
        refresh = watch.index("await refresh();")
        guard = watch.index("try {", watch.index("state.wasBusy = false;"))
        caught = watch.index("} catch (error) {", refresh)
        finish = watch.index("finishRender(task);")
        assert guard < refresh < caught < finish, "收尾必须在刷新的 try/catch 之后"

    def test_finishing_is_announced_even_with_the_overlay_hidden(self):
        source = self._editor_source()
        assert "function announceRender(task, reloaded)" in source
        assert "announceRender(task, reloaded);" in source
        announce = source[
            source.index("function announceRender(task, reloaded)") : source.index(
                "function loseRender(error)"
            )
        ]
        assert "if (ui.renderModal.hidden) say(" in announce
        assert "Your video is ready." in announce

    def test_a_failed_render_is_not_announced_as_ready(self):
        source = self._editor_source()
        announce = source[
            source.index("function announceRender(task, reloaded)") : source.index(
                "function loseRender(error)"
            )
        ]
        failed = announce.index("The render failed")
        ready = announce.index("setTitleNote(\"Video ready\")")
        assert failed < ready, "失败那条路要在说「好了」之前就返回"
        assert "return;" in announce[failed:ready]

    def test_the_tab_title_carries_the_news(self):
        """渲染要好几分钟，用户早就切到别的窗口了。"""
        source = self._editor_source()
        assert "const PAGE_TITLE = document.title;" in source
        assert "function setTitleNote(note)" in source
        assert "setTitleNote(pct > 0 ? `Rendering ${pct}%` : \"Rendering\");" in source
        assert 'setTitleNote("Video ready");' in source
        assert 'setTitleNote("Render failed");' in source

    def test_good_news_does_not_look_like_a_warning(self):
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        css = (root / "resource" / "public" / "editor" / "editor.css").read_text(
            encoding="utf-8"
        )
        assert ".banner.done" in css
