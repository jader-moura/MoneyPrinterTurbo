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

所以这里加一道兜底：整轮测试开始前记下那个文件，结束时发现被改过就还回去，
并且喊一声。它不替代"用例自己收拾干净"（泄漏的那个用例已经修好了），但
下一次有人写出同样的泄漏时，赔上的不再是本机配置。
"""

from __future__ import annotations

import os

from unittest.mock import patch

import pytest


def _config_path() -> str:
    try:
        from app.config import config
    except Exception:  # pragma: no cover - 配置模块本身坏了时不挡着测试
        return ""
    return str(getattr(config, "config_file", "") or "")


@pytest.fixture(autouse=True)
def never_call_a_real_llm_for_an_image_prompt():
    """测试不准真的去问 LLM 要画面提示词。

    重生成一段画面现在会先让 LLM 把关键词写成一句带上下文的描述
    （``scene_editor._written_image_prompt``）。测试里那条路是用假的
    ``download_videos`` 跑通的，但写提示词那一步会**真的发出去**——在配了
    API Key 的机器上，跑一次测试就是一串真金白银的调用；在没配的机器上则是
    每个用例都等一次失败。

    默认返回空串，也就是"写不出来"，于是走拼接兜底那条路——这正是本机现在的
    真实行为。要测"写出来了"的用例自己 patch 回去。
    """
    from app.services import llm

    with patch.object(llm, "generate_scene_image_prompt", return_value=""):
        yield


@pytest.fixture(scope="session", autouse=True)
def keep_the_real_config_intact():
    """整轮测试跑完后，仓库里的 config.toml 必须和开跑前一模一样。"""
    path = _config_path()
    before = None
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as handle:
                before = handle.read()
        except OSError:
            before = None

    yield

    if before is None or not path:
        return
    try:
        with open(path, "rb") as handle:
            after = handle.read()
    except OSError:
        return
    if after == before:
        return
    try:
        with open(path, "wb") as handle:
            handle.write(before)
    except OSError as exc:  # pragma: no cover - 还不回去时至少要说清楚
        pytest.exit(f"the test run rewrote {path} and it could not be restored: {exc}")
        return
    # 不让它悄悄过去：泄漏的那个用例还在，下一次仍然会写。
    print(
        f"\n!! a test rewrote {path} during this run; it has been restored.\n"
        "   Something replaced a module-level config dict without putting it "
        "back — see test/conftest.py.",
    )
