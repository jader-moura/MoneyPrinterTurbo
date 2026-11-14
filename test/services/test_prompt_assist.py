"""用一句短话改写一段的画面提示词，同时手写那条路一个字都不能少。

Why these tests exist
---------------------
审片里那个框写的就是最终提示词，手改它一直是可以的。但要改得有用，得记着
``_scene_image_prompt_request`` 里那十来条规矩：画面里要有人在做事、情绪要落在
脸上、年代和地点每条都要重写一遍、画面里不能出现文字、不能写镜头和风格词。
用户敲进去的是"让她在笑"，模型拿到的就是这四个字——于是画出来是一张没有年代、
没有地点、属于别的段落的脸。

所以这里加的是第二条路，不是替换：用户照旧可以自己写整句，也可以只写
"换成晚上"，由模型把它补成符合同一套规矩的一句话。这些测试钉住的是：

* 规矩不在新代码里重写一遍，而是直接复用逐句生成那条请求（``count=1``），
  手改出来的提示词和整批生成出来的因此是同一个标准；
* 旧提示词要在场，并且只改用户要改的那一处——不然模型会从主题重新编一张图，
  把上一轮好不容易对齐的人物、年代、地点全换掉；
* 这一段在整条片子里的位置要带上——第 94 段不该再写成开场镜头或"记得订阅"
  （这正是 8708cf5 修掉的那个毛病，单条改写同样会犯）；
* **什么都不落盘**。模型写歪的时候，一次点击不该已经把用户手写的那条覆盖掉。
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.models.schema import VideoParams
from app.services import llm, scene_editor


SUBJECT = "what happens when you stop sleeping enough"
LINES = [
    "Your body starts keeping score on the very first night",
    "Then the first week goes by",
    "And the damage starts stacking up",
    "By the second week you are a different person",
    "Nobody around you can tell you why",
]
STYLE = "flat vector illustration of {term}, bold outlines"


def _scenes(count=len(LINES), **extra):
    return [
        {
            "index": index,
            "keyword": f"stored prompt {index}",
            "narration": LINES[index],
            "material": f"{index}.png",
            "kind": "image",
            "source": "openai_image",
            "locked": False,
            **extra,
        }
        for index in range(count)
    ]


class _Model:
    """把每次收到的请求原样留下来，按要求的条数返回。"""

    def __init__(self, written="a tired nurse in her thirties rubbing her eyes"):
        self.prompts = []
        self._written = written

    def __call__(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self._written is None:
            return "[]"
        if isinstance(self._written, str) and self._written.startswith("Error: "):
            return self._written
        return json.dumps([self._written])

    @property
    def prompt(self) -> str:
        assert self.prompts, "the model was never asked anything"
        return self.prompts[-1]


@pytest.fixture
def ask():
    """跑一次改写，并把发给模型的那段请求交回来。

    除了 LLM，这里把落盘那一步也换成 mock——它有没有被碰过，下面要断言。
    """

    def run(
        scenes=None,
        *,
        written="a tired nurse rubbing her eyes",
        narration=None,
        **kwargs,
    ):
        scenes = _scenes() if scenes is None else scenes
        spoken = list(LINES) if narration is None else list(narration)
        model = _Model(written)
        saved = MagicMock(return_value=True)
        params = VideoParams(video_subject=SUBJECT, image_prompt_template=STYLE)
        with (
            patch.object(llm, "_generate_response", side_effect=model),
            patch.object(
                scene_editor.scene_manifest,
                "load",
                return_value={"scenes": scenes, "script": " ".join(LINES)},
            ),
            patch.object(scene_editor, "narration_for_scenes", return_value=spoken),
            patch.object(scene_editor, "load_params", return_value=params),
            patch.object(
                scene_editor.task_artifacts, "patch_script_data", new=saved
            ),
        ):
            try:
                result = scene_editor.suggest_image_prompt(**kwargs)
            finally:
                run.model = model
                run.saved = saved
        return result

    run.model = None
    run.saved = None
    return run


class TestTheShortSentenceBecomesAFullPrompt:
    def test_the_users_own_words_reach_the_model(self, ask):
        ask(task_id="t1", index=1, direction="make it at night")
        assert "make it at night" in ask.model.prompt
        assert "### What The User Asked For" in ask.model.prompt

    def test_the_model_sentence_is_what_comes_back(self, ask):
        written = "a nurse in her thirties slumped at a dark nurses' station"
        assert ask(task_id="t1", index=1, direction="at night", written=written) == (
            written
        )

    def test_only_one_prompt_is_asked_for(self, ask):
        """一条请求一张图。这里用的就是逐句那条请求，``count`` 收成 1。"""
        ask(task_id="t1", index=1, direction="at night")
        assert "must have exactly 1 items" in ask.model.prompt
        assert len(ask.model.prompts) == 1

    def test_the_house_rules_come_along(self, ask):
        """规矩不在这里重写一遍，所以它们必须真的在请求里。"""
        ask(task_id="t1", index=1, direction="at night")
        prompt = ask.model.prompt
        assert "show a person or people doing something" in prompt
        assert "never ask for text, captions" in prompt
        assert "repeat the setting in every prompt" in prompt
        assert "no camera directions, no style words" in prompt

    def test_the_narration_line_is_the_one_this_scene_plays_over(self, ask):
        ask(task_id="t1", index=3, direction="at night")
        assert LINES[3] in ask.model.prompt

    def test_the_subject_and_the_style_are_both_there(self, ask):
        """风格词是拼在描述后面的，模型看不到它就会写出打架的两半。"""
        ask(task_id="t1", index=1, direction="at night")
        assert SUBJECT in ask.model.prompt
        assert "flat vector illustration" in ask.model.prompt
        # {term} 是拼接用的占位符，不该当成画面内容交给模型。
        assert "{term}" not in ask.model.prompt

    def test_a_scene_with_its_own_style_uses_that_one(self, ask):
        """这一段单独挑过画风就用它，和重新生成走的是同一套解析。"""
        scenes = _scenes()
        with patch.object(
            scene_editor.image_style_model,
            "template_for_style",
            return_value="pencil sketch of {term}",
        ):
            ask(scenes=[{**scene, "image_style": "sketch"} for scene in scenes],
                task_id="t1", index=1, direction="at night")
        assert "pencil sketch" in ask.model.prompt
        assert "flat vector illustration" not in ask.model.prompt


class TestOnlyWhatWasAskedForChanges:
    def test_the_prompt_in_use_goes_along(self, ask):
        """不给旧提示词，模型就会从主题重新编一张图。"""
        ask(task_id="t1", index=1, direction="at night", current="a nurse at a desk")
        prompt = ask.model.prompt
        assert "### The Prompt This Picture Uses Now" in prompt
        assert "a nurse at a desk" in prompt
        assert "change only what the direction below asks for" in prompt

    def test_unsaved_edits_in_the_box_win_over_the_stored_one(self, ask):
        """框里是用户刚敲的、还没保存的那条，要改的就是它。"""
        ask(task_id="t1", index=1, direction="at night", current="my own wording")
        assert "my own wording" in ask.model.prompt
        assert "stored prompt 1" not in ask.model.prompt

    def test_an_empty_box_falls_back_to_the_stored_prompt(self, ask):
        ask(task_id="t1", index=1, direction="at night", current="   ")
        assert "stored prompt 1" in ask.model.prompt

    def test_a_direction_on_its_own_is_enough(self, ask):
        """这一段还没有提示词时，只有用户那句话也要能写。"""
        scenes = [{**scene, "keyword": ""} for scene in _scenes()]
        result = ask(scenes=scenes, task_id="t1", index=1, direction="two nurses")
        assert result
        assert "### The Prompt This Picture Uses Now" not in ask.model.prompt
        assert "two nurses" in ask.model.prompt

    def test_an_empty_direction_still_rewrites_what_is_there(self, ask):
        """只按"改写"不写方向，等于让模型照规矩把现在这条写好。"""
        result = ask(task_id="t1", index=1, direction="", current="a nurse")
        assert result
        assert "### What The User Asked For" not in ask.model.prompt
        assert "a nurse" in ask.model.prompt


class TestItKnowsWhereTheSceneSits:
    def test_a_scene_in_the_middle_is_not_an_opening_or_an_ending(self, ask):
        ask(task_id="t1", index=2, direction="at night")
        prompt = ask.model.prompt
        assert "lines 3-3 of 5" in prompt
        assert "NOT the beginning of the video" in prompt
        assert "NOT the end of the video" in prompt
        assert "no 'like and comment'" in prompt

    def test_the_lines_around_it_come_as_context_only(self, ask):
        """人物和场景要接得上前后几句，否则单条改写会换一张脸。"""
        ask(task_id="t1", index=2, direction="at night")
        prompt = ask.model.prompt
        assert LINES[0] in prompt and LINES[1] in prompt
        assert LINES[3] in prompt and LINES[4] in prompt
        assert "write no prompts for these" in prompt

    def test_the_only_scene_in_a_task_gets_no_placement_block(self, ask):
        ask(scenes=_scenes(1), task_id="t1", index=0, direction="at night")
        assert "### Where This Section Sits" not in ask.model.prompt

    def test_the_first_scene_is_allowed_to_be_an_opening(self, ask):
        ask(task_id="t1", index=0, direction="at night")
        prompt = ask.model.prompt
        assert "NOT the beginning of the video" not in prompt
        assert "NOT the end of the video" in prompt


class TestNothingIsSavedUntilTheUserSaysSo:
    def test_the_suggestion_is_not_written_to_the_task(self, ask):
        """模型写歪的时候，手写的那条还得在原处。"""
        ask(task_id="t1", index=1, direction="at night")
        assert ask.saved.call_args_list == []

    def test_it_only_returns_text(self, ask):
        result = ask(task_id="t1", index=1, direction="at night")
        assert isinstance(result, str) and result.strip()


class TestWhenItCannotWriteOne:
    def test_an_empty_answer_says_to_write_it_yourself(self, ask):
        with pytest.raises(scene_editor.SceneError) as caught:
            ask(task_id="t1", index=1, direction="at night", written=None)
        assert "write the prompt yourself" in str(caught.value)

    def test_the_provider_failure_is_named(self, ask):
        """"操作失败"让用户自己猜；要改的那一行配置得说出来。"""
        with pytest.raises(scene_editor.SceneError) as caught:
            ask(
                task_id="t1",
                index=1,
                direction="at night",
                written="Error: ollama: model_name is not set",
            )
        assert "model_name is not set" in str(caught.value)

    def test_nothing_to_work_from_is_refused(self, ask):
        """没旁白、没提示词、也没写方向——这时候无话可说，不能瞎编一张。"""
        scenes = [{**scene, "keyword": ""} for scene in _scenes()]
        with pytest.raises(scene_editor.SceneError) as caught:
            ask(
                scenes=scenes,
                narration=[""] * len(LINES),
                task_id="t1",
                index=1,
                direction="  ",
            )
        assert "say in a few words" in str(caught.value)

    def test_a_scene_that_is_not_there_is_refused(self, ask):
        with pytest.raises(scene_editor.SceneError):
            ask(task_id="t1", index=99, direction="at night")


class TestWritingItByHandStillWorks:
    def test_the_box_is_still_a_plain_editable_prompt(self):
        """这次加的是第二条路。手写那条路的接线不能动。"""
        import inspect

        from webui import review

        source = inspect.getsource(review._render_controls)
        assert 'key=f"review_keyword_{task_id}_{index}"' in source
        assert "scene_editor.update_scene(" in source
        assert "keyword=keyword" in source

    def test_the_suggestion_lands_in_that_same_box(self):
        """草稿填进上面那个框里，所以保存和重新生成都照旧走框里的内容。

        控件已经实例化之后不能再改它自己的 session_state 键，所以草稿必须先
        存在另一个键上、重跑这一行，再在控件创建之前填进去。这条顺序钉死,
        否则运行时会抛 StreamlitAPIException。
        """
        import inspect

        from webui import review

        source = inspect.getsource(review._render_controls)
        draft = source.index("review_prompt_draft_")
        widget = source.index('key=f"review_keyword_')
        assert draft < widget, "草稿要在关键词框创建之前填进 session_state"


class TestTheTimelineEditorAsksTheQueue:
    """用户实际用的是嵌在面板里的时间线编辑器。

    写提示词现在是**队列里的一个活**，不是这个页面自己发的一次请求。理由是
    本机这两件事抢同一块显卡：写词要让 ollama 把模型装进显存，出图那边正占
    着 10 GB。排同一条队就不会撞上。

    顺带解决了老问题："等的时候换一段"不再需要任何特殊处理——活在服务端，
    页面关掉再打开都还在。
    """

    @staticmethod
    def _editor() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    @classmethod
    def _writer(cls) -> str:
        source = cls._editor()
        start = source.index("async function startPromptWrite(")
        return source[start : source.index("\n}", start)]

    def test_it_queues_instead_of_writing_there_and_then(self):
        assert "/prompt/queue" in self._writer()

    def test_the_old_direct_route_is_not_what_the_editor_calls(self):
        """同步那条路还在（别的调用方可以用），但界面不该再走它。"""
        source = self._editor()
        assert "`${scenePath(at)}/prompt`" not in source

    def test_it_sends_both_halves_of_the_ask(self):
        body = self._writer()
        assert "{ direction, current }" in body

    def test_undo_is_captured_before_the_job_is_queued(self):
        """撤销的底要留在**按下去的那一刻**：那时框里是什么，撤销就还原成什么。

        活可能几分钟后才跑，那时框里早就不是这句话了。
        """
        body = self._writer()
        assert body.index("assist.undo = String(scene.keyword") < body.index(
            "/prompt/queue"
        )

    def test_a_scene_that_is_gone_is_said_so_not_queued(self):
        body = self._writer()
        assert "no longer in the timeline" in body

    def test_the_queue_is_refreshed_so_the_row_shows_up(self):
        body = self._writer()
        assert "fetchQueue()" in body
        assert "watchQueue()" in body


class TestTheDrawerReadsTheQueue:
    """那一小块的状态不再是这个页面自己记的，而是从队列读的。"""

    @staticmethod
    def _editor() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    def test_the_old_local_set_is_gone(self):
        """``promptWrites`` 是旧设计的核心，现在不该还有人记这件事。"""
        assert "promptWrites" not in self._editor()

    def test_busy_comes_from_the_queue(self):
        source = self._editor()
        assert "const busy = !!promptJobFor(id);" in source

    def test_it_looks_for_this_scene_s_prompt_job(self):
        source = self._editor()
        start = source.index("function promptJobFor(")
        body = source[start : start + 500]
        assert 'job.kind === "prompt"' in body
        assert "jobScene(job) === at" in body
        # 排着的和正在写的都算忙。
        assert '"queued"' in body and '"running"' in body

    def test_the_button_says_queued_not_writing(self):
        """它可能根本还没开始写——前面也许还排着两张图。"""
        assert '"In the queue…"' in self._editor()


class TestTheQueueRowSaysWhichKindOfWork:
    @staticmethod
    def _draw_queue() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        source = (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )
        start = source.index("function drawQueue()")
        opened = source.index("{", start)
        depth = 0
        for at in range(opened, len(source)):
            if source[at] == "{":
                depth += 1
            elif source[at] == "}":
                depth -= 1
                if depth == 0:
                    return source[opened : at + 1]
        raise AssertionError("drawQueue has unbalanced braces")

    def test_a_prompt_job_does_not_say_generating(self):
        """两种活排同一条队，所以这一行必须分得出来是哪一种。"""
        body = self._draw_queue()
        assert 'const writingOne = job.kind === "prompt";' in body
        assert "writing a prompt" in body

    def test_waiting_says_what_it_is_waiting_to_do(self):
        assert "waiting to write the prompt" in self._draw_queue()

    def test_a_finished_prompt_does_not_claim_a_picture_was_redrawn(self):
        assert "prompt written in" in self._draw_queue()

    def test_prompt_jobs_are_ordinary_rows(self):
        """它们现在走和出图一样的那条渲染路径。

        上一版是手写的一段循环，而那段循环被我插错了地方——插进了行的点击
        回调里，语法没错，所以 node --check 和一条"它在文件里靠后"的断言都
        没发现。现在没有那段循环了。
        """
        body = self._draw_queue()
        assert "for (const { id, since } of writing)" not in body

    def test_the_summary_counts_the_prompts_too(self):
        body = self._draw_queue()
        assert "being written" in body
        assert 'job.kind === "prompt"' in body
