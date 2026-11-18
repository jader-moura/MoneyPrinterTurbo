"""每一条提示词配的是它自己那一句，不是按位置数出来的那一句。

以前按位置配：模型多写一条、少写一条，后面整批全部错位。实测一条片子里
"这个名字常被译作死亡之山"配的是帐篷里的收音机，讲曼西老人的那一条跑到了
五句之后。

钉住的事：

* 一句旁白在编号列表里只占一行，里面的换行不带进去；
* 请求里要模型给每一条写上句子的编号；
* 回来的按编号配：顺序乱了、中间多了一条，都不影响别的句子；
* 中间缺的那几句单独再问，只问缺的；
* 模型没写编号时，和以前一样按位置配。

A prompt belongs to the line whose number it carries, not to its position.
"""

from unittest.mock import patch

from app.services import llm

from test.services.test_scene_editor import task_root  # noqa: F401

LINES = [
    "The name is often translated as Dead Mountain",
    "They dug a flat platform into the snow\nraised the canvas",
    "and settled in for the night",
    "After that\nthere is only silence",
]


class TestOneLineEach:
    def test_a_line_break_inside_a_line_is_not_another_line(self):
        numbered = llm._numbered_lines(LINES, 39)
        assert numbered.split("\n") == [
            "39. The name is often translated as Dead Mountain",
            "40. They dug a flat platform into the snow raised the canvas",
            "41. and settled in for the night",
            "42. After that there is only silence",
        ]

    def test_the_model_is_asked_to_number_its_answers(self):
        with patch.object(llm, "_request_terms", return_value=[]) as asked:
            llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        request = asked.call_args.args[0]
        assert '"39: ..." for line 39' in request and "line\n42" in request

    def test_and_for_search_terms_too(self):
        with patch.object(llm, "_request_terms", return_value=[]) as asked:
            llm._terms_for_chunk("subject", LINES, start=1)
        assert "### How To Answer" in asked.call_args.args[0]


class TestMatchedByNumber:
    def test_out_of_order_answers_land_on_their_own_lines(self):
        answers = ["41: hikers asleep in the tent", "39: a Mansi elder pointing at the mountain",
                   "42: an empty snow slope at night", "40: hikers digging a platform"]
        with patch.object(llm, "_request_terms", return_value=answers):
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got == ["a Mansi elder pointing at the mountain", "hikers digging a platform",
                       "hikers asleep in the tent", "an empty snow slope at night"]

    def test_an_extra_item_does_not_shift_the_rest(self):
        answers = ["39: a Mansi elder pointing at the mountain",
                   "40: hikers digging a platform", "40: hikers raising the canvas",
                   "41: hikers asleep in the tent", "42: an empty snow slope at night"]
        with patch.object(llm, "_request_terms", return_value=answers):
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got[1] == "hikers digging a platform"
        assert got[2] == "hikers asleep in the tent"
        assert got[3] == "an empty snow slope at night"

    def test_a_number_from_another_section_is_not_used(self):
        answers = ["39: a", "40: b", "41: c", "97: d"]
        with patch.object(llm, "_request_terms", side_effect=[answers, ["42: e"]]):
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got == ["a", "b", "c", "e"]

    def test_other_ways_of_writing_the_number(self):
        got = llm._by_line_number(["39. a", "Line 40: b", "#41) c", "42 - d"], 39, 4)
        assert got == ["a", "b", "c", "d"]

    def test_a_skipped_line_is_asked_for_on_its_own(self):
        first = ["39: a Mansi elder pointing at the mountain",
                 "41: hikers asleep in the tent", "42: an empty snow slope at night"]
        with patch.object(llm, "_request_terms",
                          side_effect=[first, ["40: hikers digging a platform"]]) as asked:
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got[1] == "hikers digging a platform" and len(got) == 4
        again = asked.call_args_list[1].args[0]
        assert "40. They dug a flat platform into the snow raised the canvas" in again
        assert "\n41. and settled" not in again.split("### Narration Lines")[1]

    def test_a_line_still_missing_is_left_for_the_padding(self):
        first = ["39: a", "41: c", "42: d"]
        with patch.object(llm, "_request_terms", side_effect=[first, []]):
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got == ["a", "", "c", "d"]
        assert llm._pad_to_lines(LINES, got, "the subject", 39)[1] == "the subject"


class TestAModelThatDoesNotNumber:
    def test_is_matched_by_position_as_before(self):
        answers = ["a Mansi elder", "hikers digging", "hikers asleep", "an empty slope"]
        with patch.object(llm, "_request_terms", return_value=answers) as asked:
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got == answers and asked.call_count == 1

    def test_a_prompt_that_only_starts_with_a_year_is_not_a_number(self):
        answers = ["1959: nine hikers on skis", "a tent on a slope",
                   "hikers asleep", "an empty slope"]
        with patch.object(llm, "_request_terms", return_value=answers):
            got = llm._terms_for_chunk("subject", LINES, visual_prompt=True, start=39)
        assert got == answers


class TestSuggestAnother:
    """"换个想法"是从这一句旁白重新想，不是照着存着的那条改。"""

    def _asked(self, task_root, **how):
        from app.services import scene_editor, scene_manifest
        from test.services.test_scene_editor import _reviewed_task

        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["keyword"] = "a Mansi elder pointing at the mountain from a fire"
        scene_manifest.save_scenes(task_id, scenes)
        with patch.object(llm, "_request_terms", return_value=["a new picture"]) as asked:
            scene_editor.suggest_image_prompt(task_id, 0, **how)
        return asked.call_args.args[0]

    def test_the_stored_prompt_is_not_handed_over(self, task_root):
        request = self._asked(task_root, fresh=True)
        assert "Mansi elder" not in request
        assert "The Prompt This Picture Uses Now" not in request

    def test_writing_the_prompt_still_works_from_what_is_there(self, task_root):
        request = self._asked(task_root)
        assert "a Mansi elder pointing at the mountain from a fire" in request

    def test_what_is_in_the_box_is_still_used_when_given(self, task_root):
        request = self._asked(task_root, fresh=True, current="a radio operator")
        assert "a radio operator" in request and "Mansi elder" not in request

    def test_the_queue_asks_for_a_fresh_one_when_the_box_was_not_sent(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2]
                  / "app/services/scene_regen_queue.py").read_text(encoding="utf-8")
        assert 'fresh=not str(job.keyword or "").strip(),' in source

