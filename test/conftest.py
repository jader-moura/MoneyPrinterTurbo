"""跑测试不该动用户的 config.toml。

Why this exists
---------------
``config.save_config()`` 是按内存里的 ``config.app`` 等几个 dict **整份重写**
config.toml 的：写出去的只有当时内存里有的键，注释和没加载的键一概消失。

这些 dict 是模块级的，整个测试进程共用一份。于是只要有一个用例把
``config.app`` 换成自己那几个键而没还回去，后面任何一次真正的保存就会把用户
仓库里的 config.toml 改写成那几行——video_source、本地出图地址、各家 Key
全部消失，而测试本身一个都不会失败。这件事真的发生过：跑完一轮测试之后，
``[app]`` 里只剩下 5 个 kimi 的键。

所以测试期间的保存一律落在一份临时副本上，真文件不写。见下面那个 fixture。
"""

from __future__ import annotations

import os

import pytest


def _config_path() -> str:
    try:
        from app.config import config
    except Exception:  # pragma: no cover - 配置模块本身坏了时不挡着测试
        return ""
    return str(getattr(config, "config_file", "") or "")


@pytest.fixture(scope="session", autouse=True)
def keep_the_real_config_intact(tmp_path_factory):
    """整轮测试期间，保存配置写的是一份临时副本，不是仓库里的 config.toml。

    以前这里的做法是"跑完发现被改过就还回去"。那有两个洞，而且都真的咬到
    过人：

    * 测试跑着的那几十秒里，真文件里是测试写进去的东西。这时启动应用，读到
      的就是那几行假配置。
    * 更糟的是"还回去"本身：用户在测试跑着的时候从界面里存了设置，测试一
      结束，这里把文件还原成开跑前的样子——他刚存的东西没了，而且没有任何
      提示。从他那边看就是"每次都得重新填一遍"。

    所以改成根本不写真文件：把 ``config.config_file`` 指到一份临时副本上，
    测试里的每一次保存都落在那里。真文件在这一轮里要是变了，那是用户自己
    改的，不动它。

    While the tests run, saving the config writes a temporary copy rather than
    the repository's config.toml. The old approach (restore the file if it
    changed) left the real file holding test data mid-run, and its restore
    silently reverted anything the user saved from the app during the run.
    """
    try:
        from app.config import config
    except Exception:  # pragma: no cover - 配置模块本身坏了时不挡着测试
        yield
        return

    real = _config_path()
    before = None
    if real and os.path.isfile(real):
        try:
            with open(real, "rb") as handle:
                before = handle.read()
        except OSError:
            before = None

    scratch = tmp_path_factory.mktemp("config") / "config.toml"
    scratch.write_bytes(before or b"")
    config.config_file = str(scratch)
    try:
        yield
    finally:
        config.config_file = real

    if before is None or not real:
        return
    try:
        with open(real, "rb") as handle:
            after = handle.read()
    except OSError:
        return
    if after != before:
        # 不还原：写它的可能是正在用应用的人。但要说一声——如果其实是某个
        # 用例绕过了上面那一层自己去写的，这一行是唯一的线索。开跑前的那份
        # 留在下面这个路径里。
        keep = tmp_path_factory.mktemp("config-before") / "config.toml"
        keep.write_bytes(before)
        print(
            f"\n!! {real} changed while the tests were running. It was "
            f"left as it is now (the app may have saved it).\n"
            f"   The copy from before the run is at {keep}",
        )
