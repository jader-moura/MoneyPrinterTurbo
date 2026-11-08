"""审片面板的文案覆盖。

``test_webui_i18n.py`` 只扫描 ``webui/Main.py``。审片面板是独立模块，它用到的
key 不在那个扫描范围内，所以这里单独覆盖一遍，避免以后在面板里加一句文案
却只写了英文，其它 13 种语言静默回退。
"""

import ast
import json
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent
REVIEW_PANEL = ROOT_DIR / "webui" / "review.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _keys_used_in_panel():
    visitor = _TrKeyVisitor()
    visitor.visit(ast.parse(REVIEW_PANEL.read_text(encoding="utf-8")))
    return visitor.keys


def _translation(locale):
    path = I18N_DIR / f"{locale}.json"
    return json.loads(path.read_text(encoding="utf-8")).get("Translation", {})


class TestReviewPanelTranslations(unittest.TestCase):
    def test_the_panel_uses_at_least_one_translated_label(self):
        """防止这个测试在面板改成别的取文案方式后静默变成空断言。"""
        self.assertGreater(len(_keys_used_in_panel()), 10)

    def test_every_locale_covers_the_panel(self):
        used = _keys_used_in_panel()
        for path in sorted(I18N_DIR.glob("*.json")):
            with self.subTest(locale=path.stem):
                missing = sorted(used - set(_translation(path.stem)))
                self.assertEqual(missing, [])

    def test_placeholders_survive_translation(self):
        """``{count}`` 和 ``{index}`` 丢掉会让运行时 format 抛 KeyError。"""
        english = _translation("en")
        placeholders = {
            key: value
            for key, value in english.items()
            if key in _keys_used_in_panel() and "{" in value
        }
        self.assertTrue(placeholders, "expected at least one formatted label")
        for path in sorted(I18N_DIR.glob("*.json")):
            translation = _translation(path.stem)
            for key, english_value in placeholders.items():
                with self.subTest(locale=path.stem, key=key):
                    expected = {
                        part.split("}")[0]
                        for part in english_value.split("{")[1:]
                    }
                    actual = {
                        part.split("}")[0]
                        for part in translation[key].split("{")[1:]
                    }
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
