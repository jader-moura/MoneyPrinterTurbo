# -*- coding: utf-8 -*-
"""把成片在文件管理器里指出来。

用户的原话："after redering the final video we can hit the play button, but
would be better if we had also a button 'Open video in folder' something like
that"。

这件事只能由服务端做：网页打不开本机的资源管理器，而这个 API 就跑在同一台
机器上。于是它变成了一个"能让服务端在桌面上弹窗"的接口，所以这里钉的大半
是那两道闸——只开这条任务自己的文件，只认本机来的请求——以及开不了的时候
不要假装开了。
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import reveal
from app.utils import utils


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


@pytest.fixture
def a_video(task_root):
    folder = Path(utils.task_dir("t1"))
    video = folder / "final-1.mp4"
    video.write_bytes(b"not really a video")
    return "t1", str(video)


class TestWhichFileItWillOpen:
    def test_the_video_of_that_task(self, a_video):
        task_id, video = a_video
        assert reveal.path_in_task(task_id, "final-1.mp4") == video

    def test_no_name_means_the_task_folder(self, a_video):
        task_id, video = a_video
        assert reveal.path_in_task(task_id) == os.path.dirname(video)

    @pytest.mark.parametrize(
        "name",
        [
            "../../../../Windows/System32/calc.exe",
            "..\\..\\secrets.txt",
            "/etc/passwd",
            "nothing-like-this.mp4",
        ],
    )
    def test_anything_outside_the_task_is_refused(self, a_video, name):
        """这个接口能让服务端开窗，所以路径只能由服务端自己解析。"""
        task_id, _ = a_video
        with pytest.raises(reveal.RevealError):
            reveal.path_in_task(task_id, name)

    def test_a_task_with_no_folder_says_so(self, task_root):
        with pytest.raises(reveal.RevealError) as caught:
            reveal.path_in_task("never-rendered", "final-1.mp4")
        assert "no folder" in str(caught.value)


class TestWhoIsAllowedToOpenIt:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"]
    )
    def test_this_machine_may(self, host):
        assert reveal.is_local(host)

    @pytest.mark.parametrize("host", ["192.168.1.5", "10.0.0.2", "", None, "evil.test"])
    def test_anybody_else_may_not(self, host):
        """别人的浏览器不该能让这台机器的桌面上冒出一个窗口。"""
        assert not reveal.is_local(host)

    def test_a_remote_request_gets_the_path_instead_of_a_window(self, a_video):
        task_id, video = a_video
        with patch.object(reveal.subprocess, "Popen") as run:
            answer = reveal.reveal(task_id, "final-1.mp4", client="192.168.1.5")
        run.assert_not_called()
        assert answer["opened"] is False
        assert answer["path"] == video
        assert "somebody else's screen" in answer["reason"]


class TestOpeningIt:
    def test_it_selects_the_file_rather_than_just_the_folder(self, a_video):
        """打开目录之后还要在几十个哈希名里找那一个 final-1.mp4。"""
        task_id, video = a_video
        with patch.object(reveal.subprocess, "Popen") as run:
            with patch.object(reveal, "headless", return_value=False):
                answer = reveal.reveal(task_id, "final-1.mp4", client="127.0.0.1")
        assert answer["opened"] is True
        command = run.call_args.args[0]
        assert video in " ".join(command)
        if sys.platform.startswith("win"):
            assert command[0] == "explorer"
            # explorer 认的就是这个写法，逗号后面不能有空格。
            assert command[1] == f"/select,{video}"

    def test_a_machine_with_no_desktop_gets_the_path(self, a_video):
        task_id, video = a_video
        with patch.object(reveal.subprocess, "Popen") as run:
            with patch.object(reveal, "headless", return_value=True):
                answer = reveal.reveal(task_id, "final-1.mp4", client="127.0.0.1")
        run.assert_not_called()
        assert answer == {
            "path": video,
            "opened": False,
            "reason": "this machine has no desktop to open a window on",
        }

    def test_a_file_manager_that_will_not_start_names_the_path(self, a_video):
        """开不了的时候至少要告诉用户东西在哪里。"""
        task_id, video = a_video
        with patch.object(reveal, "headless", return_value=False):
            with patch.object(
                reveal.subprocess, "Popen", side_effect=FileNotFoundError("no explorer")
            ):
                with pytest.raises(reveal.RevealError) as caught:
                    reveal.reveal(task_id, "final-1.mp4", client="127.0.0.1")
        assert video in str(caught.value)

    def test_it_does_not_wait_for_the_window_to_close(self, a_video):
        """用 Popen 而不是 run/check_call：资源管理器头一回启动要好几秒。

        而且退出码一概不看：Windows 的 explorer 即使成功也返回 1。
        """
        source = (
            Path(__file__).resolve().parents[2] / "app" / "services" / "reveal.py"
        ).read_text(encoding="utf-8")
        assert "subprocess.Popen(command)" in source
        assert "check_call" not in source
        assert "returncode" not in source


class TestTheButton:
    @staticmethod
    def _editor() -> str:
        root = Path(__file__).resolve().parents[2] / "resource" / "public" / "editor"
        return (root / "editor.js").read_text(encoding="utf-8")

    def test_the_modal_has_one(self):
        root = Path(__file__).resolve().parents[2] / "resource" / "public" / "editor"
        html = (root / "index.html").read_text(encoding="utf-8")
        assert 'id="render-reveal"' in html
        assert "Show in folder" in html

    def test_it_sends_a_name_not_a_path(self):
        """路径由服务端自己解析，客户端只说哪一个文件。"""
        source = self._editor()
        start = source.index('ui.renderReveal.addEventListener')
        body = source[start : start + 1600]
        assert ".split(\"/\").pop()" in body
        assert "{ file: name }" in body

    def test_it_shows_the_path_when_no_window_opened(self):
        body = self._editor()
        assert "The video is at" in body

    def test_it_is_hidden_when_the_render_failed(self):
        """没有成片的时候不该留一个"去文件夹看"。"""
        source = self._editor()
        start = source.index("function finishRender(")
        body = source[start : source.index("The render failed", start)]
        assert "ui.renderPlay.hidden = true" in body
        assert "ui.renderReveal.hidden = true" in body
