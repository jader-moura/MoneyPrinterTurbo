"""图片风格预设的约束测试。

这些断言针对的是“预设写错了不会报错，只会悄悄生成一堆错图”这类问题：
模板丢了 {term} 就会给每个关键词画同一张图，预设和配置文件的默认值对不上
就会让全新安装一进来就显示“自定义”。
"""

import io
import json
import os
import unittest

from app.models import image_style
from app.services import material


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestImageStylePresets(unittest.TestCase):
    def test_every_preset_keeps_the_term_placeholder(self):
        """没有 {term} 的模板会让所有关键词画出同一张图。"""
        for style in image_style.IMAGE_STYLE_PRESETS:
            with self.subTest(style=style.style_id):
                self.assertIn(
                    "{term}",
                    style.template,
                    f"{style.style_id} would render the same picture for every keyword",
                )

    def test_style_ids_are_unique(self):
        ids = [style.style_id for style in image_style.IMAGE_STYLE_PRESETS]
        self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_templates_are_unique(self):
        """两个预设共用一段模板时，反查风格的结果就不再唯一。"""
        templates = [style.template for style in image_style.IMAGE_STYLE_PRESETS]
        self.assertEqual(sorted(templates), sorted(set(templates)))

    def test_label_keys_are_unique_and_namespaced(self):
        keys = [style.label_key for style in image_style.IMAGE_STYLE_PRESETS]
        self.assertEqual(sorted(keys), sorted(set(keys)))
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(key.startswith("Image Style "))

    def test_custom_is_not_a_preset_id(self):
        """“自定义”是哨兵值，不能和真实预设撞 id。"""
        self.assertNotIn(image_style.IMAGE_STYLE_CUSTOM, image_style.image_style_ids())


class TestStyleTemplateRoundTrip(unittest.TestCase):
    def test_every_preset_round_trips(self):
        for style in image_style.IMAGE_STYLE_PRESETS:
            with self.subTest(style=style.style_id):
                self.assertEqual(
                    image_style.style_for_template(style.template), style.style_id
                )
                self.assertEqual(
                    image_style.template_for_style(style.style_id), style.template
                )

    def test_surrounding_whitespace_still_matches(self):
        """配置写回时会 strip，反查不能因为空白就退回“自定义”。"""
        style = image_style.IMAGE_STYLE_PRESETS[0]
        self.assertEqual(
            image_style.style_for_template(f"  {style.template}\n"), style.style_id
        )

    def test_unknown_and_empty_templates_are_custom(self):
        for template in ("", "   ", None, "{term}, something nobody shipped"):
            with self.subTest(template=template):
                self.assertEqual(
                    image_style.style_for_template(template),
                    image_style.IMAGE_STYLE_CUSTOM,
                )

    def test_custom_and_unknown_ids_yield_no_template(self):
        """返回空字符串即“不要覆盖用户模板”，调用方据此跳过写回。"""
        for style_id in (image_style.IMAGE_STYLE_CUSTOM, "", None, "no_such_style"):
            with self.subTest(style_id=style_id):
                self.assertEqual(image_style.template_for_style(style_id), "")

    def test_lookup_ignores_case_and_padding(self):
        style = image_style.IMAGE_STYLE_PRESETS[0]
        self.assertEqual(
            image_style.template_for_style(f"  {style.style_id.upper()} "),
            style.template,
        )


class TestStyleReachesTheGenerator(unittest.TestCase):
    """预设必须真的能被 material 的提示词拼接函数用上。"""

    def test_each_preset_substitutes_the_keyword(self):
        original = material.config.app.get("openai_image_prompt_template")
        try:
            for style in image_style.IMAGE_STYLE_PRESETS:
                with self.subTest(style=style.style_id):
                    material.config.app["openai_image_prompt_template"] = style.template
                    prompt = material._openai_image_prompt("a stone bridge")
                    self.assertIn("a stone bridge", prompt)
                    self.assertNotIn("{term}", prompt)
        finally:
            if original is None:
                material.config.app.pop("openai_image_prompt_template", None)
            else:
                material.config.app["openai_image_prompt_template"] = original


class TestShippedDefaults(unittest.TestCase):
    def test_default_template_is_the_first_preset(self):
        self.assertEqual(
            image_style.DEFAULT_IMAGE_PROMPT_TEMPLATE,
            image_style.IMAGE_STYLE_PRESETS[0].template,
        )
        self.assertEqual(
            image_style.DEFAULT_IMAGE_STYLE_ID,
            image_style.IMAGE_STYLE_PRESETS[0].style_id,
        )

    def test_example_configs_ship_a_recognised_style(self):
        """
        配置文件里的默认模板必须逐字等于某个预设。

        否则全新安装打开 WebUI 时，风格选择器会显示“自定义”——功能没坏，
        但第一印象是“这里没有默认风格”。
        """
        for name in ("config.example.toml", "config.local.example.toml"):
            path = os.path.join(REPO_ROOT, name)
            if not os.path.exists(path):
                continue
            with self.subTest(config=name):
                shipped = self._read_template(path)
                self.assertIsNotNone(shipped, f"{name} has no prompt template")
                self.assertNotEqual(
                    image_style.style_for_template(shipped),
                    image_style.IMAGE_STYLE_CUSTOM,
                    f"{name} ships a template no preset matches",
                )

    @staticmethod
    def _read_template(path):
        with io.open(path, encoding="utf8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("openai_image_prompt_template"):
                    _, _, value = stripped.partition("=")
                    return value.strip().strip('"')
        return None


class TestStyleLabelsAreTranslated(unittest.TestCase):
    """缺翻译时 tr() 会回退英文；英文都缺就会把原始 key 显示给用户。"""

    def _translation(self, locale):
        path = os.path.join(REPO_ROOT, "webui", "i18n", f"{locale}.json")
        with io.open(path, encoding="utf8") as handle:
            return json.load(handle)["Translation"]

    def test_english_defines_every_label(self):
        translation = self._translation("en")
        expected = [style.label_key for style in image_style.IMAGE_STYLE_PRESETS]
        expected += [
            "Image Style",
            "Image Style Help",
            "Image Style Custom",
            "Image Style Term",
        ]
        for key in expected:
            with self.subTest(key=key):
                self.assertIn(key, translation)
                self.assertTrue(str(translation[key]).strip())

    def test_chinese_defines_every_label(self):
        translation = self._translation("zh")
        for style in image_style.IMAGE_STYLE_PRESETS:
            with self.subTest(key=style.label_key):
                self.assertIn(style.label_key, translation)
                self.assertTrue(str(translation[style.label_key]).strip())


class TestStyleSources(unittest.TestCase):
    def test_local_materials_have_no_style(self):
        """本地素材是用户自己的文件，提示词模板根本不参与。"""
        self.assertNotIn("local", image_style.IMAGE_STYLE_SOURCES)

    def test_the_default_video_source_supports_styles(self):
        from app.models import const

        self.assertIn(const.DEFAULT_VIDEO_SOURCE, image_style.IMAGE_STYLE_SOURCES)


if __name__ == "__main__":
    unittest.main()
