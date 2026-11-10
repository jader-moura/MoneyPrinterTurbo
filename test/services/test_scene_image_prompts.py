"""画面提示词要替旁白讲故事，而不是配一张库存图。

Why these tests exist
---------------------
同一条片子里，模型自己给出的关键词是 "bowl of rice"、"rice terraces"、
"ancient stone tools"——没有人、没有动作、没有年代。画出来是一张张库存图：
摆在哪一段下面都成立，也就等于什么都没讲。用户把它们手改成

    "The asian farmer makes a very angry face to the rice"
    "asian people from ten thousand years ago cutting wild rice with small
     stone blades"
    "Ancient Asian farmer selecting rice seeds to plant"

之后，画面立刻开始替旁白讲故事。差别有三条，这组测试把它们钉在提示词里：
有人在做事、情绪长在脸上、年代和地点每一条都重写一遍（每张图是分开画的,
彼此没有记忆）。

另一半是"关键词比场景少"：那条片子 25 段画面只拿到 12 个关键词，后 13 段
是空的，而空关键词在重生成那里直接被拒——那几段永远换不了画面。
"""

from unittest.mock import patch

import pytest

from app.services import llm, scene_editor, scene_manifest

from test.services.test_scene_editor import _reviewed_task, idle_task, task_root  # noqa: F401


class TestWhatThePromptWriterIsTold:
    def _request(self):
        return llm._scene_image_prompt_request(
            "How rice farming was invented",
            "1. Wild rice has a trick\n2. Terrible for anyone harvesting it",
            2,
        )

    def test_it_asks_for_people_doing_something(self):
        text = self._request().lower()
        assert "person or people doing something" in text
        # 并且点名"光一件东西"就是库存图。
        assert "stock photo" in text

    def test_it_asks_for_the_feeling_on_a_face(self):
        """"难收"这句话该配一张恼火的脸，而不是一株稻子。"""
        text = self._request().lower()
        assert "put it on a face" in text
        assert "reacting" in text

    def test_it_asks_for_the_period_in_every_prompt(self):
        """每张图分开画，彼此没有记忆，所以年代要一条条重写。"""
        text = self._request().lower()
        assert "repeat the setting in every prompt" in text
        assert "no memory of the others" in text

    def test_it_asks_for_recurring_people_to_stay_the_same(self):
        text = self._request().lower()
        assert "recurring people recognisably the same" in text

    def test_the_example_shows_a_person_an_action_and_a_period(self):
        """例子比规则管用，所以例子本身必须合规。"""
        text = self._request()
        example = text.split("## Output Example:")[1].split("## Context:")[0]
        assert "farmer" in example
        assert "ten thousand years ago" in example
        assert "frustration" in example

    def test_it_still_refuses_text_in_the_picture_and_style_words(self):
        """原有的约束一条都不能丢。"""
        text = self._request().lower()
        assert "never ask for text" in text
        assert "no camera directions" in text
        assert "json-array" in text

    def test_the_count_and_subject_are_carried_through(self):
        text = llm._scene_image_prompt_request("Why bees vanish", "1. one", 7)
        assert "exactly 7 items" in text
        assert "Why bees vanish" in text


class TestRegeneratingASceneWithNoKeyword:
    """关键词比场景少是常态，空关键词不该让那一段永远换不了画面。"""

    def _blank_keyword_task(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[1]["keyword"] = ""
        scene_manifest.save_scenes(task_id, scenes)
        return task_id

    def test_it_falls_back_to_the_subject_and_the_spoken_line(self, task_root):
        task_id = self._blank_keyword_task(task_root)
        suggestion = scene_editor.suggested_keyword(task_id, 1)
        assert suggestion.startswith("seeds")
        assert suggestion != "seeds"  # 主题后面要带上这一段念的那句话

    def test_regenerating_no_longer_refuses(self, task_root):
        task_id = self._blank_keyword_task(task_root)
        plan = scene_editor.check_regenerate(task_id, 1)
        assert plan["keyword"].startswith("seeds")

    def test_an_explicit_keyword_still_wins(self, task_root):
        task_id = self._blank_keyword_task(task_root)
        plan = scene_editor.check_regenerate(task_id, 1, keyword="a flooded paddy")
        assert plan["keyword"] == "a flooded paddy"

    def test_the_editor_is_told_what_it_would_draw_from(self, task_root):
        """界面把它当占位文字显示，用户看得见要画什么。"""
        task_id = self._blank_keyword_task(task_root)
        scenes = scene_editor.describe(task_id)["scenes"]
        assert scenes[1]["keyword_suggestion"].startswith("seeds")
        # 有关键词的那一段不需要提示。
        assert scenes[0]["keyword_suggestion"] == ""

    def test_a_task_with_nothing_to_go_on_still_refuses_clearly(self, task_root):
        task_id = self._blank_keyword_task(task_root)
        with patch.object(scene_editor, "suggested_keyword", return_value=""):
            with pytest.raises(scene_editor.SceneError) as error:
                scene_editor.check_regenerate(task_id, 1)
        assert "no keyword" in str(error.value)


class TestWhatActuallyReachesTheImageModel:
    """关键词只说"要什么东西"。上下文和这一刻的台词也必须到出图模型手上。

    这是用户指出的那件事：出片那条路上提示词是读着主题和整篇旁白写出来的，
    而审片界面里重出一张图只把关键词替进画风模板
    （``material._openai_image_prompt``），主题和台词一个字都没带。
    """

    def test_the_writer_gets_the_subject_the_line_and_the_keyword(self):
        from app.services import llm as llm_service

        request = llm_service._one_scene_image_prompt_request(
            "How rice farming was invented",
            "Terrible for anyone trying to harvest it",
            "the farmer is frustrated",
        )
        assert "How rice farming was invented" in request
        assert "Terrible for anyone trying to harvest it" in request
        assert "the farmer is frustrated" in request
        # 用户填的东西是指令，不是建议。
        assert "that is the instruction" in request

    def test_a_written_prompt_is_used_when_the_llm_answers(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        params = scene_editor.load_params(task_id)
        with patch.object(
            scene_editor.llm, "generate_scene_image_prompt",
            return_value="an ancient farmer gripping falling rice, face tight",
        ):
            drawn = scene_editor._written_image_prompt(
                params, "rice", "Terrible to harvest", "{term}, comic"
            )
        assert drawn == "an ancient farmer gripping falling rice, face tight"

    def test_without_an_llm_the_context_is_still_carried(self, task_root):
        """退回去的不是"只剩关键词"——那正是用户在抱怨的那张库存图。"""
        task_id, _ = _reviewed_task(task_root)
        params = scene_editor.load_params(task_id)
        with patch.object(
            scene_editor.llm, "generate_scene_image_prompt", return_value=""
        ):
            drawn = scene_editor._written_image_prompt(
                params, "bowl of rice", "Nobody planned it", "{term}, comic"
            )
        assert "bowl of rice" in drawn
        assert "Nobody planned it" in drawn
        assert "seeds" in drawn  # 这个夹具的 video_subject

    def test_the_keyword_comes_first_so_it_weighs_most(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        params = scene_editor.load_params(task_id)
        drawn = scene_editor._composed_image_prompt(
            params, "an angry farmer", "Terrible to harvest"
        )
        assert drawn.startswith("an angry farmer")

    def test_the_composition_does_not_repeat_itself(self, task_root):
        """关键词本身就是"主题 + 台词"兜底来的时候，别把同一句说三遍。"""
        task_id, _ = _reviewed_task(task_root)
        params = scene_editor.load_params(task_id)
        suggestion = scene_editor.suggested_keyword(task_id, 0)
        drawn = scene_editor._composed_image_prompt(
            params, suggestion, "One. Two."
        )
        assert drawn.lower().count("seeds") == 1

    def test_a_stock_source_is_left_alone(self, task_root):
        """库存素材源拿它去搜索：一整句描述在 Pexels 上搜不到东西。"""
        task_id, _ = _reviewed_task(task_root, source="pexels")
        with patch.object(scene_editor.material, "download_videos") as download:
            download.return_value = [str(task_root / task_id / "1.png")]
            with patch.object(
                scene_editor.llm, "generate_scene_image_prompt"
            ) as writer:
                scene_editor._produce_material(
                    task_id, "rice fields", what="scene 0", narration="spoken line"
                )
        writer.assert_not_called()
        assert download.call_args.kwargs["search_terms"] == ["rice fields"]

    def test_a_generated_source_sends_the_written_prompt(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.material, "download_videos") as download:
            download.return_value = [str(task_root / task_id / "1.png")]
            with patch.object(
                scene_editor.llm, "generate_scene_image_prompt",
                return_value="a farmer in a paddy, ten thousand years ago",
            ):
                scene_editor._produce_material(
                    task_id, "rice", what="scene 0", narration="spoken line"
                )
        assert download.call_args.kwargs["search_terms"] == [
            "a farmer in a paddy, ten thousand years ago"
        ]
