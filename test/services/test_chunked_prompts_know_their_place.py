"""分批写提示词时，每一批要知道自己在整条片子的哪一段。

Why these tests exist
---------------------
一条 125 句的片子是分 6 次请求写完的，每次 24 句（``_PER_LINE_BATCH``）。原来
每次请求看到的都是 "1. … 24."，没有任何东西说明前面还有 72 句——于是模型把
每一批都当成一整条片子来写：开头一个定场镜头，结尾一段"记得订阅"。

这不是推测，是在用户那条真任务（46516451）里量出来的：

* 第 94、95 个画面是 "signs that read 'Subscribe!'" 和 "wave at the audience"，
  而旁白还在讲补觉；
* 第 72 和第 96 个画面的提示词就是视频主题本身——模型那两批各只给了 23 条，
  第 24 条被兜底填成了主题；
* 第 97 个画面又是一个全新的定场镜头。

72 和 96 正好是第三、第四批的最后一句，97 正好是第五批的第一句。所以这里钉住
的就是这几件事：每一批知道自己的位置、看得到接缝前后的句子、接得上上一批的
人物，以及模型少给时先再问一次而不是直接填主题。
"""

import json
from unittest.mock import patch

import pytest

from app.services import llm


SUBJECT = "what happens when you stop sleeping enough"


def _lines(count: int, first: int = 1):
    return [f"narration line {number}" for number in range(first, first + count)]


def _wanted_count(prompt: str) -> int:
    """从提示词里读出这一批要几条——模型就是照这个数返回的。"""
    return int(prompt.split("must have exactly ")[1].split(" items")[0])


def _first_line_number(prompt: str) -> int:
    """这一批从第几句开始。用它认批次，比数调用次数稳。"""
    body = prompt.split("### Narration Lines")[1].strip()
    return int(body.split(".", 1)[0])


class _Model:
    """按要求的条数老实返回的假模型，可以让某几批故意少给。"""

    def __init__(self, short_batches=(), blank=(), empty_from=None, empty_at=()):
        self.prompts = []
        self._short = dict(short_batches)
        self._blank = set(blank)
        # 从第几次调用开始什么都给不出来。要按"调用次数"而不是"第几批"算:
        # 写不出来的时候 _request_terms 自己会重试，重试也是一次调用。
        self._empty_from = empty_from
        # 哪几批无论重试多少次都写不出来，按这一批的首句序号指定——这样就不用
        # 关心 _max_retries 是几次。
        self._empty_at = set(empty_at)

    def __call__(self, prompt, **kwargs):
        index = len(self.prompts)
        self.prompts.append(prompt)
        count = _wanted_count(prompt)
        if self._empty_from is not None and index >= self._empty_from:
            return "[]"
        if _first_line_number(prompt) in self._empty_at:
            return "[]"
        give = self._short.get(index, count)
        items = [f"picture for item {number}" for number in range(give)]
        if index in self._blank and items:
            items[len(items) // 2] = "   "
        return json.dumps(items)

    def block(self, index: int) -> str:
        """某一批提示词里"位置"那一段。"""
        prompt = self.prompts[index]
        marker = "### Where This Section Sits"
        if marker not in prompt:
            return ""
        return prompt[prompt.index(marker):prompt.index("### Narration Lines")]


def _run(model, lines, **kwargs):
    with patch.object(llm, "_generate_response", side_effect=model):
        return llm.generate_terms_per_line(SUBJECT, lines, visual_prompt=True, **kwargs)


class TestEachBatchKnowsWhereItSits:
    def test_a_long_script_still_goes_out_in_batches(self):
        """分批本身没有变——它是对的，错的只是没告诉模型批次的位置。"""
        model = _Model()
        terms = _run(model, _lines(125))
        assert len(model.prompts) == 6
        assert len(terms) == 125

    def test_a_middle_batch_is_told_it_is_neither_the_start_nor_the_end(self):
        model = _Model()
        _run(model, _lines(125))
        block = model.block(3)  # lines 73-96
        assert "lines 73-96 of 125" in block
        assert "NOT the beginning" in block
        assert "NOT the end" in block

    def test_a_middle_batch_is_told_not_to_write_a_call_to_action(self):
        """第 94、95 个画面要的是旁白讲的内容，不是"记得订阅"。"""
        model = _Model()
        _run(model, _lines(125))
        block = model.block(3)
        for banned in ["Subscribe", "like and comment", "waving at the camera"]:
            assert banned in block, banned

    def test_the_first_batch_is_not_told_it_is_a_continuation(self):
        model = _Model()
        _run(model, _lines(125))
        block = model.block(0)
        assert "NOT the beginning" not in block
        assert "NOT the end" in block

    def test_the_last_batch_may_still_close_the_video(self):
        """最后一批是真的结尾，不该禁止它收尾。"""
        model = _Model()
        _run(model, _lines(125))
        block = model.block(5)
        assert "NOT the beginning" in block
        assert "NOT the end" not in block

    def test_lines_are_numbered_by_their_real_position(self):
        """第四批的第一句是第 73 句，不是第 1 句。"""
        model = _Model()
        _run(model, _lines(125))
        assert "73. narration line 73" in model.prompts[3]
        assert "1. narration line 1\n" not in model.prompts[3]

    def test_a_script_that_fits_in_one_request_is_untouched(self):
        """短脚本本来就没有接缝，不该多出一段位置说明。"""
        model = _Model()
        terms = _run(model, _lines(12))
        assert len(model.prompts) == 1
        assert len(terms) == 12
        assert "Where This Section Sits" not in model.prompts[0]
        assert "Continuity With" not in model.prompts[0]
        assert "1. narration line 1" in model.prompts[0]


class TestTheSeamIsVisible:
    def test_the_lines_on_both_sides_are_shown_as_context(self):
        model = _Model()
        _run(model, _lines(125))
        block = model.block(3)
        assert "72. narration line 72" in block
        assert "97. narration line 97" in block
        assert block.count("context only") == 2

    def test_the_first_batch_has_nothing_before_it(self):
        model = _Model()
        _run(model, _lines(125))
        block = model.block(0)
        assert "Lines Just Before" not in block
        assert "Lines Just After" in block

    def test_the_context_lines_are_not_counted_as_work(self):
        """前后那几句是给它看的，不是让它写的——条数只能是这一批的句子数。"""
        model = _Model()
        _run(model, _lines(125))
        assert _wanted_count(model.prompts[3]) == 24


class TestTheCastCarriesOver:
    def test_the_previous_batch_last_picture_is_handed_over(self):
        """"the same office worker" 每 24 句重新开始一次，是因为模型看不到上一批。"""
        model = _Model()
        _run(model, _lines(125))
        prompt = model.prompts[3]
        assert "Continuity With The Previous Section" in prompt
        assert "picture for item 23" in prompt

    def test_the_first_batch_has_no_previous_section(self):
        model = _Model()
        _run(model, _lines(125))
        assert "Continuity With" not in model.prompts[0]


class TestAShortBatchIsAskedAgain:
    def test_the_missing_lines_are_re_asked_instead_of_filled_with_the_subject(self):
        """第 72、96 个画面曾经就是主题本身，那是两张跟旁白无关的泛图。"""
        model = _Model(short_batches={0: 20})
        terms = _run(model, _lines(48))
        assert len(model.prompts) == 3, "两批加一次补问"
        assert len(terms) == 48
        assert SUBJECT not in terms

    def test_the_re_ask_asks_for_exactly_the_lines_that_are_missing(self):
        model = _Model(short_batches={0: 20})
        _run(model, _lines(48))
        followup = model.prompts[1]
        assert _wanted_count(followup) == 4
        assert "21. narration line 21" in followup
        assert "24. narration line 24" in followup
        assert "20. narration line 20" not in followup.split("### Narration Lines")[1]

    def test_the_re_ask_happens_only_once(self):
        """补问还是少给就认了——否则一条写不出来的句子能把整轮拖住。"""
        model = _Model(short_batches={0: 20, 1: 1})
        terms = _run(model, _lines(24 * 2))
        assert len(model.prompts) == 3
        assert len(terms) == 48

    def test_what_still_cannot_be_written_falls_back_to_the_subject(self):
        """补问也写不出来时才填主题——那是保住对齐的最后手段,不是第一选择。"""
        model = _Model(short_batches={0: 20}, empty_from=1)
        terms = _run(model, _lines(24))
        assert len(terms) == 24
        assert terms[:20] == [f"picture for item {n}" for n in range(20)]
        assert terms[20:] == [SUBJECT] * 4, "兜底仍然要保住对齐"


class TestAlignmentIsNeverLost:
    """错位一格比画面泛一点糟得多：从那一句起每一张图都配错。"""

    def test_a_batch_that_fails_completely_pads_instead_of_shifting(self):
        """这是改之前真实存在的坑：一批返回空,后面 101 句整体前移 24 格。"""
        model = _Model(empty_at={1})
        terms = _run(model, _lines(48))
        assert len(terms) == 48
        # 第二批原样返回，不能被挪到第一批的位置上。
        assert terms[24] == "picture for item 0"
        assert terms[:24] == [SUBJECT] * 24

    def test_a_blank_item_in_the_middle_still_falls_back(self):
        model = _Model(blank={0})
        terms = _run(model, _lines(10))
        assert len(terms) == 10
        assert terms[5] == SUBJECT

    @pytest.mark.parametrize("count", [1, 23, 24, 25, 49, 125])
    def test_the_result_always_matches_the_line_count(self, count):
        model = _Model()
        assert len(_run(model, _lines(count))) == count

    def test_a_single_batch_that_fails_still_reports_failure(self):
        """整篇只有一批还写不出来时，要让上层报错，而不是交出一堆主题。"""
        model = _Model(empty_from=0)
        assert _run(model, _lines(5)) == []
