"""逐句生成画面关键词。

默认那条路给整篇脚本要固定 5 或 8 个主题词，下游却按下标把第 i 个词配给
第 i 句旁白。两边条数不相等，所以"旁白在讲早餐、画面却是超市"这类错配是
结构性的，不是偶发的。这里的测试钉住"条数由句子数决定"和"顺序不被打乱"。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import llm


class TestGenerateTermsPerLine:
    def test_one_term_per_line_in_order(self):
        lines = ["In northern Syria", "a place called Abu Hureyra", "They ate bread"]
        reply = '["a dry riverbank in northern syria", "a mud brick village", "flatbread on a stone"]'
        with patch.object(llm, "_generate_response", return_value=reply):
            terms = llm.generate_terms_per_line("bread history", lines)
        assert terms == [
            "a dry riverbank in northern syria",
            "a mud brick village",
            "flatbread on a stone",
        ]

    def test_the_result_is_always_the_same_length_as_the_lines(self):
        """少给就补，不能让后面的句子整体前移——错位一格比画面泛一点糟得多。"""
        lines = ["one", "two", "three", "four"]
        with patch.object(llm, "_generate_response", return_value='["a", "b"]'):
            terms = llm.generate_terms_per_line("my subject", lines)
        assert len(terms) == 4
        assert terms[:2] == ["a", "b"]
        # 兜底用主题，而不是把别的句子的词挪过来。
        assert terms[2] == "my subject"
        assert terms[3] == "my subject"

    def test_extra_terms_are_truncated(self):
        lines = ["one", "two"]
        with patch.object(llm, "_generate_response", return_value='["a","b","c","d"]'):
            terms = llm.generate_terms_per_line("s", lines)
        assert terms == ["a", "b"]

    def test_blank_items_fall_back_rather_than_shifting(self):
        lines = ["one", "two", "three"]
        with patch.object(llm, "_generate_response", return_value='["a", "   ", "c"]'):
            terms = llm.generate_terms_per_line("s", lines)
        assert terms == ["a", "s", "c"]

    def test_empty_lines_are_dropped_before_asking(self):
        lines = ["one", "   ", "", "two"]
        with patch.object(llm, "_generate_response", return_value='["a","b"]') as call:
            terms = llm.generate_terms_per_line("s", lines)
        assert terms == ["a", "b"]
        prompt = call.call_args.args[0]
        assert "1. one" in prompt and "2. two" in prompt

    def test_no_lines_asks_nothing(self):
        with patch.object(llm, "_generate_response") as call:
            assert llm.generate_terms_per_line("s", []) == []
        assert call.call_count == 0

    def test_a_provider_error_returns_empty_not_the_error_text(self):
        """错误文案当成关键词传下去，素材源会按字符去搜，产生无意义的请求。"""
        with patch.object(llm, "_generate_response", return_value="Error: no key"):
            assert llm.generate_terms_per_line("s", ["one"]) == []

    def test_the_prompt_numbers_the_lines_and_asks_for_that_count(self):
        lines = ["alpha", "beta", "gamma"]
        with patch.object(llm, "_generate_response", return_value='["a","b","c"]') as call:
            llm.generate_terms_per_line("my subject", lines)
        prompt = call.call_args.args[0]
        assert "exactly 3 items" in prompt
        assert "1. alpha" in prompt
        assert "3. gamma" in prompt
        assert "my subject" in prompt


class TestParamDefault:
    def test_the_option_is_on_by_default(self):
        """默认打开：固定条数的关键词在下游按下标配给旁白，条数不等就必然错配。

        12 个主题词配 28 句旁白时，用户听到的是"早餐",看到的是超市货架。
        代价是按句子数出图、付费素材源花费上升,但错配的画面本来就等于白花。

        这个开关只在第一次生成画面时起作用：它属于 terms 阶段,审片之后的
        渲染不跑那个阶段,所以重新出片不会重新生成任何图片。
        """
        assert VideoParams(video_subject="x").align_terms_to_narration is True

    def test_the_option_round_trips_through_the_manifest(self):
        params = VideoParams(video_subject="x", align_terms_to_narration=True)
        restored = VideoParams(**params.model_dump())
        assert restored.align_terms_to_narration is True


class TestPipelineWiring:
    """流水线必须真的走逐句这条路，而且不能再把顺序打乱。"""

    def _params(self, **kwargs):
        return VideoParams(
            video_subject="Why do people eat three times a day",
            video_terms="",
            **kwargs,
        )

    def test_enabled_uses_the_per_line_generator(self):
        from app.services import task as tm

        script = "People wake up hungry. Breakfast came first. Then came lunch."
        params = self._params(align_terms_to_narration=True)
        with patch.object(tm.llm, "generate_terms_per_line") as per_line:
            with patch.object(tm.llm, "generate_terms") as whole_script:
                per_line.return_value = ["a", "b", "c"]
                terms = tm.generate_terms("t", params, script)
        assert terms == ["a", "b", "c"]
        assert whole_script.call_count == 0
        # 传进去的必须是切好的句子，不是整篇脚本。
        assert len(per_line.call_args.kwargs["narration_lines"]) == 3

    def test_disabled_keeps_the_original_generator(self):
        from app.services import task as tm

        params = self._params(align_terms_to_narration=False)
        with patch.object(tm.llm, "generate_terms_per_line") as per_line:
            with patch.object(tm.llm, "generate_terms", return_value=["x"]) as whole:
                with patch.object(
                    tm.twelvelabs, "rerank_terms_by_subject", side_effect=lambda **k: k["search_terms"]
                ):
                    terms = tm.generate_terms("t", params, "One. Two.")
        assert terms == ["x"]
        assert per_line.call_count == 0
        assert whole.call_count == 1

    def test_aligned_terms_are_never_reranked(self):
        """重排一次就全错位，恰好把这个功能要修的问题重新制造出来。"""
        from app.services import task as tm

        params = self._params(align_terms_to_narration=True)
        with patch.object(tm.llm, "generate_terms_per_line", return_value=["a", "b"]):
            with patch.object(tm.twelvelabs, "rerank_terms_by_subject") as rerank:
                terms = tm.generate_terms("t", params, "One. Two.")
        assert rerank.call_count == 0
        assert terms == ["a", "b"]

    def test_an_empty_per_line_result_fails_the_task_loudly(self):
        """不静默回退到旧逻辑：那会让用户以为逐句生效了，拿到的却是错配结果。"""
        from app.services import task as tm

        params = self._params(align_terms_to_narration=True)
        with patch.object(tm.llm, "generate_terms_per_line", return_value=[]):
            with patch.object(tm, "_mark_task_failed") as failed:
                assert tm.generate_terms("t", params, "One. Two.") is None
        assert failed.call_count == 1
        assert "terms" in failed.call_args.args


class TestVisualPromptMode:
    """文生图要的是一整句画面描述，库存素材要的是短查询。

    文生图模型看到的就是这段文字本身。只给它"dry eye"加一串风格词时，它既
    不知道这一句在讲什么，也不知道整条片子在讲什么，于是画出来的东西经常
    属于片子的别处——这正是用户反复遇到的那个问题。
    """

    def _captured_prompt(self, **kwargs):
        seen = {}

        def fake(prompt, **_ignored):
            seen["prompt"] = prompt
            return '["a close-up of a human eye glossy with tears"]'

        with patch.object(llm, "_generate_response", side_effect=fake):
            llm.generate_terms_per_line("what if you never blink", ["one"], **kwargs)
        return seen["prompt"]

    def test_the_visual_mode_asks_for_a_full_sentence(self):
        prompt = self._captured_prompt(visual_prompt=True)
        assert "Scene Image Prompt Writer" in prompt
        assert "12-30 words" in prompt
        assert "2-6 words" not in prompt

    def test_the_search_mode_still_asks_for_a_short_query(self):
        """库存素材源拿它去搜索：一整句描述在 Pexels 上搜不到东西。"""
        prompt = self._captured_prompt()
        assert "2-6 words" in prompt
        assert "Scene Image Prompt Writer" not in prompt

    def test_the_subject_and_the_line_both_reach_the_model(self):
        """主题是上下文，旁白是这一刻在讲什么，缺一个画面就会跑偏。"""
        prompt = self._captured_prompt(visual_prompt=True)
        assert "what if you never blink" in prompt
        assert "1. one" in prompt

    def test_the_visual_mode_forbids_text_in_the_picture(self):
        prompt = self._captured_prompt(visual_prompt=True)
        assert "never ask for text" in prompt

    def test_the_visual_mode_handles_lines_that_cannot_be_drawn(self):
        """"Left untreated" 本身画不出来，要结合主题推断出一个具体场面。"""
        prompt = self._captured_prompt(visual_prompt=True)
        assert "cannot be drawn on its own" in prompt

    def test_the_length_guarantee_holds_in_visual_mode(self):
        lines = ["one", "two", "three"]
        with patch.object(llm, "_generate_response", return_value='["a scene"]'):
            prompts = llm.generate_terms_per_line(
                "my subject", lines, visual_prompt=True
            )
        assert len(prompts) == 3
        assert prompts[0] == "a scene"


class TestGeneratedImageSources:
    """哪些素材源吃提示词原文，由一处定义说了算。"""

    def test_the_image_sources_are_the_ones_that_draw_the_picture(self):
        from app.services import material

        assert "openai_image" in material.GENERATED_IMAGE_SOURCES
        assert "codex_image" in material.GENERATED_IMAGE_SOURCES
        assert "pexels" not in material.GENERATED_IMAGE_SOURCES

    def test_a_generated_image_task_asks_for_visual_prompts(self):
        from app.services import task as tm

        params = VideoParams(
            video_subject="what if you never blink",
            video_script="",
            video_source="openai_image",
            align_terms_to_narration=True,
        )
        with patch.object(tm.llm, "generate_terms_per_line", return_value=["a"]) as gen:
            tm.generate_terms("task-id", params, "You blink often")
        assert gen.call_args.kwargs["visual_prompt"] is True

    def test_a_stock_footage_task_still_asks_for_search_terms(self):
        from app.services import task as tm

        params = VideoParams(
            video_subject="what if you never blink",
            video_script="",
            video_source="pexels",
            align_terms_to_narration=True,
        )
        with patch.object(tm.llm, "generate_terms_per_line", return_value=["a"]) as gen:
            with patch.object(tm.twelvelabs, "rerank_terms_by_subject",
                              side_effect=lambda **kw: kw["search_terms"]):
                tm.generate_terms("task-id", params, "You blink often")
        assert gen.call_args.kwargs["visual_prompt"] is False


class TestStyleAwareDescriptions:
    """风格词是拼在描述后面的，所以写描述时就得知道最终是什么画风。

    火柴人那套预设要的是"一个小人正在做某件事"（round white head, thin black
    line arms and legs, clearly holding the objects of the action）。描述如果
    写的是"大脑扫描图高亮视觉皮层"，两半就在互相打架。
    """

    STICK = (
        "{term}, cute stick figure cartoon, round white head, dot eyes, "
        "thin black line arms and legs, clearly holding the objects of the "
        "action, mid-action pose full of energy"
    )

    def _captured_prompt(self, style):
        seen = {}

        def fake(prompt, **_ignored):
            seen["prompt"] = prompt
            return '["a scene"]'

        with patch.object(llm, "_generate_response", side_effect=fake):
            llm.generate_terms_per_line(
                "never blinking", ["one"], visual_prompt=True, image_style=style
            )
        return seen["prompt"]

    def test_the_chosen_style_is_shown_to_the_model(self):
        prompt = self._captured_prompt(self.STICK)
        assert "Picture Style" in prompt
        assert "cute stick figure cartoon" in prompt

    def test_the_placeholder_is_not_shown_as_style(self):
        """``{term}`` 是模板占位符，不是风格描述，不该出现在提示词里。"""
        assert "{term}" not in self._captured_prompt(self.STICK)

    def test_drawn_figure_styles_ask_for_a_person_doing_something(self):
        prompt = self._captured_prompt(self.STICK)
        assert "put a person in frame doing something visible" in prompt

    def test_the_model_is_told_not_to_repeat_the_style_words(self):
        prompt = self._captured_prompt(self.STICK)
        # 这句在提示词里是折行的，所以只断言不跨行的那一段。
        assert "Do not repeat these style" in prompt

    def test_no_style_configured_adds_no_style_section(self):
        """没配风格模板时不该凭空多出一段空的风格说明。"""
        prompt = self._captured_prompt("")
        assert "Picture Style" not in prompt

    def test_a_template_that_is_only_the_placeholder_adds_nothing(self):
        assert "Picture Style" not in self._captured_prompt("{term}")

    def test_the_style_never_reaches_the_search_term_mode(self):
        """库存素材那条路搜的是短查询，风格词与它无关。"""
        seen = {}

        def fake(prompt, **_ignored):
            seen["prompt"] = prompt
            return '["a"]'

        with patch.object(llm, "_generate_response", side_effect=fake):
            llm.generate_terms_per_line("subject", ["one"], image_style=self.STICK)
        assert "Picture Style" not in seen["prompt"]

    def test_the_configured_template_is_passed_through_from_the_task(self):
        from app.config import config as app_config
        from app.services import task as tm

        params = VideoParams(
            video_subject="never blinking",
            video_script="",
            video_source="openai_image",
            align_terms_to_narration=True,
        )
        previous = app_config.app.get("openai_image_prompt_template")
        app_config.app["openai_image_prompt_template"] = self.STICK
        try:
            with patch.object(
                tm.llm, "generate_terms_per_line", return_value=["a"]
            ) as gen:
                tm.generate_terms("task-id", params, "You blink often")
        finally:
            if previous is None:
                app_config.app.pop("openai_image_prompt_template", None)
            else:
                app_config.app["openai_image_prompt_template"] = previous
        assert gen.call_args.kwargs["image_style"] == self.STICK
