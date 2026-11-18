"""每一条画面提示词自己就说得通，不指着别的画面。

出图模型一次只拿到一条。"Same group of hikers ..."、"The same man ..." 对
它来说只是"一群人"、"一个人"，画出来的是陌生人。

钉住的事：

* 写提示词的那条请求里明说了：人每次都要重新描述，不能写 "the same"；它
  自己举的例子里也没有 "the same"；
* 写完之后查一遍，指着别的画面的那几条连同前面几条交回去改；别的不动、
  也不为它们多问一次；
* 改不好的留着原样（不让整条片子写不出来）；
* 手改一条提示词也一样。

Every image prompt has to stand on its own: the image model sees one prompt.
"""

import re
from unittest.mock import patch

import pytest

from app.services import llm


class TestTheRequestSaysSo:
    def test_the_rule_is_there(self):
        text = llm._scene_image_prompt_request("subject", "1. a line", 1)
        assert "write that description out in full every time" in text
        assert "never point at\n   another picture" in text

    def test_its_own_example_does_not_do_it(self):
        text = llm._scene_image_prompt_request("subject", "1. a line", 1)
        example = text.split("## Output Example:")[1].split("## Context:")[0]
        assert not llm._leans_on_another(example)

    def test_nor_does_the_note_about_the_section_before(self):
        note = llm._continuity_guidance("an old farmer in a straw hat")
        assert "Write the description out again in every prompt" in note
        assert "re-introduce" not in note


class TestWhichPromptsLean:
    @pytest.mark.parametrize("prompt", [
        "Same group of nine hikers pitching a tent on a snowy slope",
        "The same man staring at the instrument panel in disbelief",
        "the same ancient farmer and two others cutting wild rice",
        "the engineer from before signing the report",
        "the pilot seen earlier climbing into the cockpit",
        "the crowd from the previous scene now running",
    ])
    def test_these_do(self, prompt):
        assert llm._leans_on_another(prompt)

    @pytest.mark.parametrize("prompt", [
        "nine hikers in 1959 Soviet ski gear pitching a canvas tent on a snowy slope",
        "two engineers in grey coats reading the dial at the same time",
        "a woman who looks like her mother did before the war, sewing by a window",
        "",
    ])
    def test_these_do_not(self, prompt):
        assert not llm._leans_on_another(prompt)


PROMPTS = [
    "a tall bearded engineer in his fifties in a grey wool coat at a 1952 airfield",
    "a de Havilland Comet airliner on a wet runway in 1952, ground crew around it",
    "the same man staring at a cracked window frame, face pale",
    "passengers in 1950s clothes boarding the airliner up a metal stair",
]


class TestThePromptsAreMadeToStandAlone:
    def test_only_the_one_that_leans_is_rewritten(self):
        fixed = ("a tall bearded engineer in his fifties in a grey wool coat "
                 "staring at a cracked window frame, face pale")
        with patch.object(llm, "_request_terms", return_value=[fixed]) as asked:
            got = llm._stand_alone("the Comet disasters", PROMPTS)
        assert got == PROMPTS[:2] + [fixed] + PROMPTS[3:]
        assert asked.call_count == 1
        request = asked.call_args.args[0]
        # 它前面的几条都在请求里：那个人是谁就写在里面。
        assert PROMPTS[0] in request and PROMPTS[1] in request
        assert f"Rewrite this one: {PROMPTS[2]}" in request
        assert PROMPTS[3] not in request
        assert "the Comet disasters" in request

    def test_nothing_is_asked_when_every_prompt_already_stands_alone(self):
        clean = [PROMPTS[0], PROMPTS[1], PROMPTS[3]]
        with patch.object(llm, "_request_terms") as asked:
            assert llm._stand_alone("subject", clean) == clean
        assert not asked.called

    def test_a_later_one_sees_the_earlier_one_already_rewritten(self):
        prompts = [PROMPTS[0], "the same man at his desk", "the same man asleep"]
        answers = [["a tall bearded engineer in a grey wool coat at his desk",
                    "a tall bearded engineer in a grey wool coat asleep in a chair"]]
        with patch.object(llm, "_request_terms", side_effect=answers):
            got = llm._stand_alone("subject", prompts)
        assert not any(llm._leans_on_another(one) for one in got)

    STRIPPED = "the man staring at a cracked window frame, face pale"

    def test_a_rewrite_that_still_leans_is_not_taken(self):
        with patch.object(llm, "_request_terms",
                          return_value=["the same man, but closer"]) as asked:
            got = llm._stand_alone("subject", PROMPTS)
        # 一批里没改成，单独再问一次；还不行，只拿掉那几个字。
        assert asked.call_count == 2
        assert got == PROMPTS[:2] + [self.STRIPPED] + PROMPTS[3:]

    def test_a_rewrite_that_draws_another_picture_is_not_taken(self):
        other = ("a 20-year-old man on a frozen lake at midnight in 1959, "
                 "staring in awe at a green flash above the horizon")
        with patch.object(llm, "_request_terms", return_value=[other]):
            got = llm._stand_alone("subject", PROMPTS)
        assert got[2] == self.STRIPPED

    def test_one_that_fails_in_a_batch_is_asked_for_alone(self):
        fixed = ("a tall bearded engineer in a grey wool coat staring at a "
                 "cracked window frame, face pale")
        with patch.object(llm, "_request_terms", side_effect=[[], [fixed]]):
            got = llm._stand_alone("subject", PROMPTS)
        assert got[2] == fixed

    def test_a_model_that_answers_nothing_still_leaves_no_pointer(self):
        with patch.object(llm, "_request_terms", return_value=[]):
            got = llm._stand_alone("subject", PROMPTS)
        assert got[2] == self.STRIPPED
        assert not any(llm._leans_on_another(one) for one in got)

    @pytest.mark.parametrize("before,after", [
        ("Same group of nine hikers pitching a tent", "group of nine hikers pitching a tent"),
        ("the same tent, now silent", "the tent, now silent"),
        ("the engineer from before signing the report", "the engineer signing the report"),
        ("the crowd from the previous scene now running", "the crowd now running"),
    ])
    def test_taking_the_pointer_out(self, before, after):
        assert llm._without_pointers(before) == after

    def test_many_are_sent_a_few_at_a_time(self):
        prompts = [PROMPTS[0]] + [f"the same man in room {n}" for n in range(23)]

        def answer(request, _config=None):
            count = request.count("Rewrite this one:")
            rooms = re.findall(r"Rewrite this one: the same man (in room \d+)", request)
            assert len(rooms) == count
            return [f"a tall bearded engineer in a grey coat {room}" for room in rooms]

        with patch.object(llm, "_request_terms", side_effect=answer) as asked:
            got = llm._stand_alone("subject", prompts)
        assert asked.call_count == 6
        assert not any(llm._leans_on_another(one) for one in got)


class TestWhereItHappens:
    def test_after_the_prompts_of_a_video_are_written(self):
        lines = ["He looked at the window.", "Then he saw the crack."]
        answers = [
            ["a bearded engineer in a grey coat looking at an aircraft window",
             "the same man pointing at a crack in the metal"],
            ["a bearded engineer in a grey coat pointing at a crack in the metal"],
        ]
        with patch.object(llm, "_request_terms", side_effect=answers) as asked:
            got = llm.generate_terms_per_line("subject", lines, visual_prompt=True)
        assert asked.call_count == 2
        assert got[1] == "a bearded engineer in a grey coat pointing at a crack in the metal"

    def test_search_terms_are_left_alone(self):
        lines = ["He looked at the window.", "Then he saw the crack."]
        with patch.object(llm, "_request_terms",
                          return_value=["aircraft window", "same crack"]) as asked:
            got = llm.generate_terms_per_line("subject", lines)
        assert asked.call_count == 1 and got == ["aircraft window", "same crack"]

    def test_a_prompt_rewritten_by_hand_too(self):
        answers = [["the same man, now smiling"],
                   ["a bearded engineer in a grey coat at a 1952 airfield, now smiling"]]
        with patch.object(llm, "_request_terms", side_effect=answers) as asked:
            got = llm.improve_image_prompt(
                "subject", "He was relieved.", direction="have him smiling",
                current="a bearded engineer in a grey coat at a 1952 airfield",
            )
        assert asked.call_count == 2
        assert got == "a bearded engineer in a grey coat at a 1952 airfield, now smiling"
        assert "a bearded engineer in a grey coat at a 1952 airfield" in asked.call_args.args[0]
