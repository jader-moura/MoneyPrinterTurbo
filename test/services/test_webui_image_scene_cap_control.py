"""界面上得有"一张图最多停几秒"这个格子。

Why this test exists
--------------------
``ollama_model_name`` 那次的教训：一个只能在 config.toml 里改的值，等于用户
不知道它存在。这个数是口味问题——"停多久算太久"只有用户自己知道——所以它必须
在界面上，而不是只能去改文件。

这里用 AppTest 真起一遍应用：直接 import Main 会把整个应用跑起来并在
streamlit-tour 上报错，所以照仓库里其他 webui 测试的做法走 AppTest。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

KEY = "max_image_scene_duration"


@pytest.fixture(scope="module")
def app():
    # 冷启动导入 Streamlit 和可选的媒体栈可能超过 30 秒，超时要给够。
    instance = AppTest.from_file(str(WEBUI_MAIN), default_timeout=90)
    instance.session_state["ui_language"] = "en"
    instance.run()
    assert [str(item.value) for item in instance.exception] == []
    return instance


def _control(app):
    for item in app.number_input:
        key = str(getattr(item, "key", ""))
        if key == KEY or key.startswith(f"{KEY}_"):
            return item
    raise AssertionError(
        f"no {KEY} control on the page; "
        f"found {[str(getattr(i, 'key', '')) for i in app.number_input]}"
    )


def test_the_cap_has_a_control_on_the_page(app):
    assert _control(app) is not None


def test_it_starts_at_the_configured_default(app):
    """默认 8 秒：按用户那条真片子选的,96 句里 15 句超过。"""
    assert float(_control(app).value) == pytest.approx(8.0)


def test_zero_is_allowed_because_it_means_no_limit():
    """0 是"不设上限"，是个有效选择，不能被控件挡掉或改成别的数。

    自己起一个实例：上面那个是 module 级的，改了它会影响别的测试。
    """
    instance = AppTest.from_file(str(WEBUI_MAIN), default_timeout=90)
    instance.session_state["ui_language"] = "en"
    instance.session_state[KEY] = 0.0
    instance.run()
    assert [str(item.value) for item in instance.exception] == []
    assert float(_control(instance).value) == 0.0


def test_it_is_labelled_in_seconds(app):
    """光写"最大长度"看不出单位,而这个数填 8 还是 8000 差别很大。"""
    label = str(_control(app).label).lower()
    assert "(s)" in label or "second" in label or "sn" in label
