# -*- coding: utf-8 -*-
"""在文件管理器里把成片指出来。

为什么这件事得由服务端做
------------------------
编辑器是一个网页。浏览器打不开资源管理器，这是它该有的样子——网页能随手
打开本机的文件管理器才是出事了。但这个 API 跑在用户自己的机器上，和那个
文件管理器是同一台，所以它能做这件事，而网页只是请它做。

于是这里变成了一个"能让服务端在桌面上弹窗"的接口，所以有两道闸：

* 路径必须落在这条任务自己的目录里，和 ``scene_manifest.resolve_material``
  一个意思。客户端递过来的是文件名，不是路径。
* 只认本机来的请求。这个 API 绑的是 ``0.0.0.0``（见 ``main.py``），局域网
  里的另一台机器不该能让这台机器的桌面上冒出一个窗口。

没有桌面的时候（Docker、纯命令行的服务器）不假装成功：这种部署下
``xdg-open`` 只会悄悄失败。那时返回路径，让界面把它显示出来，用户自己复制
——这正是 WebUI 那边 ``_open_task_path`` 早就在做的事。
"""

import os
import subprocess
import sys
from typing import Optional

from loguru import logger

from app.services import scene_manifest
from app.utils import utils


class RevealError(Exception):
    """指不出来。文案是给人看的，并且总是带着那条路径。"""


def headless() -> bool:
    """这台机器上有没有一个能弹窗的桌面。

    和 ``webui/Main.py`` 的 ``_is_headless_server`` 同一套判断：macOS 和
    Windows 的桌面部署一定有，Linux 上看有没有 X 或 Wayland。
    """
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def is_local(host: Optional[str]) -> bool:
    """这个请求是不是从本机来的。

    认不出来的时候返回 False：不确定就不开窗，代价只是用户自己复制一下
    路径。
    """
    return str(host or "").strip().lower() in {
        "127.0.0.1",
        "::1",
        "localhost",
        "::ffff:127.0.0.1",
    }


def path_in_task(task_id: str, name: str = "") -> str:
    """任务目录，或者目录里那一个文件的绝对路径。

    ``name`` 走 ``resolve_material`` 的那套限定，所以 ``../`` 这类东西到不了
    别的地方去。
    """
    # 自己拼，不要 ``utils.task_dir(task_id)``：那个函数会把目录建出来，于是
    # 下面这句 ``isdir`` 永远为真——一道永远不会拦下任何东西的闸，顺便还让
    # "点一下按钮"在磁盘上留下一个空目录。
    folder = os.path.abspath(os.path.join(utils.task_dir(), task_id))
    if not os.path.isdir(folder):
        raise RevealError(f"task {task_id} has no folder on disk yet")
    wanted = os.path.basename(str(name or "").strip())
    if not wanted:
        return folder
    try:
        return scene_manifest.resolve_material(task_id, wanted)
    except scene_manifest.SceneError as exc:
        raise RevealError(f"{wanted} is not a file of this task: {exc}") from exc


def _command(path: str) -> "list[str]":
    """把这一个文件**选中**，而不是只把目录打开。

    打开目录之后用户还得在几十个 ``<64 位哈希>.zoom-….mp4`` 里找出那一个
    ``final-1.mp4``，而我们本来就知道是哪一个。
    """
    if sys.platform.startswith("win"):
        # 逗号后面不能有空格，explorer 认的就是这个写法。
        return ["explorer", f"/select,{path}"]
    if sys.platform == "darwin":
        return ["open", "-R", path]
    # Linux 没有通用的"选中某个文件"，退而求其次打开它所在的目录。
    return ["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path]


def reveal(task_id: str, name: str = "", *, client: Optional[str] = None) -> dict:
    """在文件管理器里指出这个文件，返回它的绝对路径。

    ``opened`` 为假时路径仍然是对的，界面据此改成"显示路径让用户自己去"。
    """
    path = path_in_task(task_id, name)

    if not is_local(client):
        return {
            "path": path,
            "opened": False,
            "reason": "this request did not come from this machine, so opening "
            "a window here would be opening it on somebody else's screen",
        }
    if headless():
        return {
            "path": path,
            "opened": False,
            "reason": "this machine has no desktop to open a window on",
        }

    command = _command(path)
    try:
        # 不等它退出：资源管理器开着的时候这个调用会立刻返回，而第一次启动
        # 时它要好几秒——没必要让一次点击等在那里。
        #
        # 退出码一概不看：Windows 的 explorer 即使成功也返回 1，拿它判断
        # 成败只会把每一次成功都报成失败。
        subprocess.Popen(command)
    except Exception as exc:
        logger.warning(f"cannot reveal {path}: {type(exc).__name__}: {exc}")
        raise RevealError(
            f"could not open the file manager ({type(exc).__name__}). "
            f"The file is at {path}"
        ) from exc
    logger.info(f"revealed {path} in the file manager")
    return {"path": path, "opened": True, "reason": ""}
