"""关键词把"逐句配画"顶掉的时候，界面必须说出来。

这个坑的代价是实打实的：关键词框一非空，``task.generate_terms`` 里的逐句
分支整条就不跑，可界面上那个勾还勾着。用户以为两个都在生效，拿到的却是
几个关键词轮播出来的两百张图——几个小时的显卡时间，画的全是片子别处的
东西，而且看得出不对的时候已经画完了。

更阴的是这个框会自己填上：生成文案、套用候选、点"生成关键词"都会往里写。
所以这里既要钉住"判断条件和流水线那一行一致"，也要钉住"界面真的把它说
出来了"。

Filling the keywords box silently switches off per-line alignment while the
checkbox still claims to be on, and the box fills itself whenever a script is
generated. These tests pin both halves: that the warning's condition is the
same one the pipeline branches on, and that the warning actually renders.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.models.schema import VideoParams
from app.services import llm, task as task_service, twelvelabs, voice
from webui.keyword_alignment import (
    keywords_override_alignment,
    keywords_override_warning_key,
)


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


class TestWhenKeywordsAreOverridingAlignment:
    """判断条件本身。"""

    @pytest.mark.parametrize(
        "terms, align_on, overriding",
        [
            ("a clock, a kitchen", True, True),
            ("", True, False),
            ("   ", True, False),
            (None, True, False),
            (["a clock", "a kitchen"], True, True),
            ([], True, False),
            (["", "  "], True, False),
            # 逐句配画本来就关着，没有什么被顶掉——这时候再弹一条警告只是噪音。
            ("a clock, a kitchen", False, False),
            ("", False, False),
        ],
    )
    def test_the_condition(self, terms, align_on, overriding):
        assert keywords_override_alignment(terms, align_on) is overriding


class TestItSaysWhatThisSourceWillActuallyDo:
    """文生图拿关键词当提示词，库存素材拿它去搜索，后果不是一回事。"""

    @pytest.mark.parametrize("source", ["openai_image", "codex_image"])
    def test_a_generated_image_source_talks_about_drawing(self, source):
        assert (
            keywords_override_warning_key(source)
            == "Keywords Override Alignment Drawn"
        )

    @pytest.mark.parametrize("source", ["pexels", "pixabay", "coverr", "local", ""])
    def test_every_other_source_talks_about_searching(self, source):
        assert (
            keywords_override_warning_key(source)
            == "Keywords Override Alignment Searched"
        )


class TestTheWarningMatchesWhatThePipelineDoes:
    """警告的条件必须和 ``task.generate_terms`` 分支的条件是同一个。

    两边各写各的话，迟早会飘：界面不报警而逐句分支其实没跑，用户就又一次
    为几个小时的显卡时间买单。所以这里不比对代码，直接让流水线跑一遍，看
    逐句生成到底有没有被调用。
    """

    def _params(self, terms):
        return VideoParams(
            video_subject="how a clock works",
            video_terms=terms,
            align_terms_to_narration=True,
            match_materials_to_script=False,
            video_source="openai_image",
        )

    def _run(self, params):
        """跑一遍 generate_terms，回报逐句生成有没有被调用。"""
        with (
            patch.object(
                llm, "generate_terms_per_line", return_value=["per line"]
            ) as per_line,
            patch.object(llm, "generate_terms", return_value=["whole script"]),
            patch.object(
                twelvelabs, "rerank_terms_by_subject", side_effect=lambda **kw: kw["search_terms"]
            ),
        ):
            terms = task_service.generate_terms("t", params, "A clock ticks. It has gears.")
        return per_line.called, terms

    def test_keywords_switch_the_per_line_branch_off_and_the_warning_says_so(self):
        params = self._params("a clock, a kitchen")

        called, terms = self._run(params)

        assert not called, "填了关键词，逐句生成根本没跑"
        assert terms == ["a clock", "a kitchen"], "画的就是用户填的那几个词"
        assert keywords_override_alignment(
            params.video_terms, params.align_terms_to_narration
        ), "流水线跳过了逐句生成，界面就必须报警"

    def test_an_empty_box_runs_the_per_line_branch_and_the_warning_stays_quiet(self):
        params = self._params("")

        called, terms = self._run(params)

        assert called, "框是空的，逐句生成才跑得起来"
        assert terms == ["per line"], "拿到的是逐句写出来的提示词"
        assert not keywords_override_alignment(
            params.video_terms, params.align_terms_to_narration
        ), "没有东西被顶掉，就不该弹警告"


class TestTheUserCanSeeItAndUndoIt:
    """界面上真的要有那条警告，和一个把它改回去的按钮。"""

    def _app(self, *, terms, source="openai_image", align=True):
        app_config = dict(
            config.app,
            video_source=source,
            align_terms_to_narration=align,
            match_materials_to_script=False,
        )
        ui_config = dict(config.ui, language="en")
        patches = (
            patch.object(config, "app", app_config),
            patch.object(config, "ui", ui_config),
            patch.object(config, "try_save_config", return_value=True),
            patch.object(
                voice,
                "get_all_azure_voices",
                return_value=["en-US-JennyNeural-Female"],
            ),
        )
        for entered in patches:
            entered.__enter__()
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=90)
        app.session_state["ui_language"] = "en"
        app.session_state["align_terms_to_narration"] = align
        app.session_state["video_terms"] = terms
        app.run()
        assert [str(item.value) for item in app.exception] == []
        return app, patches

    @staticmethod
    def _close(patches):
        for entered in reversed(patches):
            entered.__exit__(None, None, None)

    @staticmethod
    def _warnings(app):
        return " ".join(str(item.value) for item in app.warning)

    def test_a_filled_box_warns_and_offers_to_clear_it(self):
        app, patches = self._app(terms="a clock, a kitchen")
        try:
            assert "Match each picture to its narration line" in self._warnings(app)
            assert "drawn from this list" in self._warnings(app), "文生图要说画"
            assert any(
                str(getattr(item, "key", "")) == "clear_video_terms"
                for item in app.button
            ), "得有一个按钮能一键改回去"
        finally:
            self._close(patches)

    def test_an_empty_box_says_nothing(self):
        app, patches = self._app(terms="")
        try:
            assert "Match each picture to its narration line" not in self._warnings(app)
            assert not any(
                str(getattr(item, "key", "")) == "clear_video_terms"
                for item in app.button
            )
        finally:
            self._close(patches)

    def test_a_stock_source_warns_about_searching_instead(self):
        app, patches = self._app(terms="a clock, a kitchen", source="pexels")
        try:
            assert "searched from this list" in self._warnings(app), "库存素材要说搜"
            assert "drawn from this list" not in self._warnings(app)
        finally:
            self._close(patches)

    def test_alignment_already_off_is_not_worth_a_warning(self):
        app, patches = self._app(terms="a clock, a kitchen", align=False)
        try:
            assert "Match each picture to its narration line" not in self._warnings(app)
        finally:
            self._close(patches)

    def test_clearing_empties_the_box(self):
        app, patches = self._app(terms="a clock, a kitchen")
        try:
            clear = next(
                item
                for item in app.button
                if str(getattr(item, "key", "")) == "clear_video_terms"
            )
            clear.click().run()
            assert app.session_state["video_terms"] == "", "点完按钮框就该是空的"
            assert "Match each picture to its narration line" not in self._warnings(app)
        finally:
            self._close(patches)
