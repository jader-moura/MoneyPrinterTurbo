"""在外面写好的分镜：照它念、照它画、照它挑照片，不再问模型。

钉住的事：

* 分镜读得懂就拆成旁白、每段的提示词、哪几段照着照片画；读不懂就说是
  第几段、缺什么；
* 表单多了一项，不填的时候一切照旧；填了就用它的旁白和提示词，一定先停
  下来等人看，库存素材的来源点名拒绝；
* 场景一段对一段照分镜摆，旁白存在场景上，时间线照它数每段占几条字幕；
* 照着照片画的那几段、要搜什么，记成和模型挑出来的同一项。

A scene plan written outside the app is followed as written: its narration,
one prompt per scene, and which scenes are drawn from a photo.
"""

import json

import pytest

from unittest.mock import patch

from app.models.schema import NewVideoRequest, VideoParams
from app.services import llm, new_video, scene_editor, scene_manifest, scene_plan
from app.services import task as tm
from app.services import timeline
from app.utils import utils
from test.services.test_new_video import _fields, saved  # noqa: F401
from test.services.test_prompt_review_before_drawing import (  # noqa: F401
    _run_to_the_checkpoint,
    no_cross_post,
    task_root,
)

PLAN = {
    "subject": "The Green River Killer",
    "scenes": [
        {"narration": "It is November, two thousand one. A man drives to work.",
         "prompt": "A pickup truck on a wet road at dawn, pine trees either side",
         "image": "text"},
        {"narration": "His name is Gary Ridgway.",
         "prompt": "A middle-aged man with a moustache in a plaid shirt",
         "image": "photo", "photo_kind": "person",
         "photo_name": "Gary Ridgway", "photo_search": "Gary Ridgway 2001 arrest"},
        {"narration": "For twenty years, nobody stopped him.",
         "prompt": "An empty riverbank under grey sky, police tape in the reeds"},
    ],
}


def _text(plan=PLAN):
    return json.dumps(plan)


class TestReadingAPlan:
    def test_it_gives_the_narration_the_prompts_and_the_photo_scenes(self):
        plan = scene_plan.parse(_text())
        assert plan["subject"] == "The Green River Killer"
        assert [scene["prompt"] for scene in plan["scenes"]] == [
            one["prompt"] for one in PLAN["scenes"]]
        assert plan["scenes"][0]["photo"] is None and plan["scenes"][2]["photo"] is None
        assert plan["scenes"][1]["photo"] == {
            "kind": "person", "name": "Gary Ridgway",
            "search": "Gary Ridgway 2001 arrest"}

    def test_the_script_is_one_scene_per_line(self):
        script = scene_plan.script_of(scene_plan.parse(_text()))
        assert script.split("\n") == [one["narration"] for one in PLAN["scenes"]]

    def test_a_code_fence_around_it_is_ignored(self):
        fenced = "```json\n" + _text() + "\n```"
        assert len(scene_plan.parse(fenced)["scenes"]) == 3

    def test_a_bare_list_of_scenes_is_a_plan(self):
        assert len(scene_plan.parse(json.dumps(PLAN["scenes"]))["scenes"]) == 3

    def test_a_photo_scene_with_only_a_name_searches_for_the_name(self):
        scenes = [dict(PLAN["scenes"][1])]
        del scenes[0]["photo_search"]
        photo = scene_plan.parse(json.dumps(scenes))["scenes"][0]["photo"]
        assert photo["search"] == "Gary Ridgway"

    @pytest.mark.parametrize("change, said", [
        ({"narration": ""}, "scene 2 has no narration"),
        ({"prompt": " "}, "scene 2 has no prompt"),
        ({"image": "video"}, "scene 2: image is 'video'"),
        ({"photo_kind": "animal"}, "scene 2: photo_kind is 'animal'"),
        ({"photo_name": "", "photo_search": ""}, "scene 2 is drawn from a photo"),
    ])
    def test_what_is_wrong_is_said_with_the_scene_number(self, change, said):
        scenes = [dict(one) for one in PLAN["scenes"]]
        scenes[1].update(change)
        with pytest.raises(scene_plan.ScenePlanError, match=said):
            scene_plan.parse(json.dumps({"scenes": scenes}))

    @pytest.mark.parametrize("text, said", [
        ("", "empty"), ("{not json", "not valid JSON"),
        ('{"subject": "x"}', 'needs a "scenes" list'), ('{"scenes": []}', "no scenes"),
    ])
    def test_something_that_is_not_a_plan_is_refused(self, text, said):
        with pytest.raises(scene_plan.ScenePlanError, match=said):
            scene_plan.parse(text)

    def test_the_scenes_add_up_to_the_subtitle_lines(self):
        plan = scene_plan.parse(_text())
        whole = utils.split_string_by_punctuations(scene_plan.script_of(plan))
        each = [len(utils.split_string_by_punctuations(scene["narration"]))
                for scene in plan["scenes"]]
        assert sum(each) == len(whole) and each[0] > 1

    def test_the_share_drawn_from_photos_is_never_rounded_below_the_plan(self):
        stored = scene_plan.stored(scene_plan.parse(_text()))
        assert scene_plan.photo_percent(stored) == 34
        assert scene_plan.photo_percent([{"photo": None}] * 4) == 0


class TestTheForm:
    def test_the_field_sits_with_the_script_settings(self, saved):  # noqa: F811
        form = new_video.form()
        assert _fields(form)["scene_plan"]["kind"] == "textarea"
        side = next(tab for tab in form["tabs"] if tab["id"] == "script")
        assert side["fields"][0]["name"] == "scene_plan"

    def test_it_is_not_remembered_as_a_setting(self):
        assert "scene_plan" not in new_video.UI_FIELDS
        assert "scene_plan" not in new_video.APP_FIELDS

    def test_without_a_plan_nothing_changes(self, saved):  # noqa: F811
        params = new_video.build_params({"video_subject": "cats"})
        assert params.scene_plan is None and params.video_terms is None
        assert VideoParams(video_subject="cats").scene_plan is None

    def test_a_plan_sets_the_script_the_prompts_and_the_photo_share(self, saved):  # noqa: F811
        params = new_video.build_params(
            NewVideoRequest(scene_plan=_text()).model_dump())
        assert params.video_subject == "The Green River Killer"
        assert params.video_script.count("\n") == 2
        assert params.video_terms == [one["prompt"] for one in PLAN["scenes"]]
        assert [bool(one["photo"]) for one in params.scene_plan] == [False, True, False]
        assert params.photo_scene_percent == 34

    def test_the_subject_typed_in_the_form_wins(self, saved):  # noqa: F811
        params = new_video.build_params(
            {"video_subject": "Ridgway", "scene_plan": _text()})
        assert params.video_subject == "Ridgway"

    def test_a_plan_and_a_pasted_script_together_are_refused(self, saved):  # noqa: F811
        with pytest.raises(new_video.NewVideoError, match="carries its own narration"):
            new_video.build_params({"video_script": "Hello.", "scene_plan": _text()})

    def test_a_stock_source_is_refused_by_name(self, saved):  # noqa: F811
        with pytest.raises(new_video.NewVideoError, match="pexels is not one"):
            new_video.build_params({"video_source": "pexels", "scene_plan": _text()})

    def test_a_broken_plan_says_what_is_wrong(self, saved):  # noqa: F811
        with pytest.raises(new_video.NewVideoError, match="not valid JSON"):
            new_video.build_params({"scene_plan": "{oops"})

    def test_a_planned_video_always_stops_before_drawing(self, saved):  # noqa: F811
        params = new_video.build_params({"scene_plan": _text()})
        assert new_video.stop_stage(params, review_prompts=False) == "subtitle"
        assert tm.reviews_prompts_first(
            params, {"review_prompts_before_images": False}) is True


class TestTheScenes:
    def _planned(self):
        plan = scene_plan.parse(_text())
        return plan, scene_manifest.build_planned_scenes(
            video_script=scene_plan.script_of(plan),
            video_terms=scene_plan.prompts_of(plan),
            # 一张图停 4 秒、旁白 60 秒：没有分镜的话会摆 15 段并轮播提示词。
            audio_duration=60.0, clip_duration=4,
            video_source="openai_image",
            narrations=[scene["narration"] for scene in plan["scenes"]],
        )

    def test_there_is_exactly_one_scene_per_planned_scene(self):
        plan, scenes = self._planned()
        assert [scene["keyword"] for scene in scenes] == scene_plan.prompts_of(plan)
        assert [scene["narration"] for scene in scenes] == [
            one["narration"] for one in plan["scenes"]]

    def test_the_timeline_counts_subtitle_lines_from_the_stored_narration(self):
        plan, scenes = self._planned()
        counts = timeline._counts_from_scene_narration(scenes)
        cues = utils.split_string_by_punctuations(scene_plan.script_of(plan))
        assert len(counts) == 3 and sum(counts) == len(cues)

    def test_without_a_plan_scenes_are_laid_out_as_before(self):
        scenes = scene_manifest.build_planned_scenes(
            video_script="One. Two.", video_terms=["a", "b"],
            audio_duration=12.0, clip_duration=4)
        assert [scene["keyword"] for scene in scenes] == ["a", "b", "a"]
        assert all(scene["narration"] == "" for scene in scenes)

    def test_a_mismatch_is_an_error_not_a_repeated_prompt(self):
        with pytest.raises(scene_manifest.SceneError, match="2 narration lines for 3"):
            scene_manifest.build_planned_scenes(
                video_script="x", video_terms=["a", "b", "c"], audio_duration=9,
                clip_duration=4, narrations=["One.", "Two."])

    def test_the_photo_scenes_are_recorded_like_the_models_choice(self):
        stored = scene_plan.stored(scene_plan.parse(_text()))
        found = scene_plan.photo_candidates(stored, ["a", "b", "c"])
        assert found["a"] == {"kind": "none", "score": 0, "name": ""}
        assert found["b"] == {"kind": "person", "score": 10, "name": "Gary Ridgway",
                              "search": "Gary Ridgway 2001 arrest"}


class TestThePipeline:
    """整条跑到"等着看提示词"那一步：场景、旁白、照片候选都照分镜来。"""

    def _started(self, task_root, task_id):  # noqa: F811
        plan = scene_plan.parse(_text())
        params = VideoParams(
            video_subject=plan["subject"],
            video_script=scene_plan.script_of(plan),
            video_terms=scene_plan.prompts_of(plan),
            scene_plan=scene_plan.stored(plan),
            photo_scene_percent=34,
            video_source="openai_image",
            subtitle_enabled=False, bgm_type="", video_clip_duration=3,
        )
        # 60 秒旁白、每段 3 秒：没有分镜的话是 20 个位置。
        with patch.object(llm, "rate_scenes_for_photos") as asked:
            _run_to_the_checkpoint(task_root, task_id, params=params, duration=60.0)
        asked.assert_not_called()
        return plan

    def test_the_scenes_are_the_plans_scenes(self, task_root):  # noqa: F811
        plan = self._started(task_root, "planned")
        scenes = scene_manifest.load("planned")["scenes"]
        assert [scene["keyword"] for scene in scenes] == scene_plan.prompts_of(plan)
        assert [scene["narration"] for scene in scenes] == [
            one["narration"] for one in plan["scenes"]]
        assert all(not scene["material"] for scene in scenes)

    def test_the_photo_scenes_are_the_plans_and_no_model_is_asked(self, task_root):  # noqa: F811
        self._started(task_root, "photos")
        with patch.object(llm, "rate_scenes_for_photos") as asked:
            listed = scene_editor.photo_candidates("photos")["scenes"]
        asked.assert_not_called()
        assert [one["kind"] for one in listed] == ["none", "person", "none"]
        assert listed[1]["score"] >= 6 and listed[1]["name"] == "Gary Ridgway"

    def test_the_photo_search_is_the_plans_words(self, task_root):  # noqa: F811
        self._started(task_root, "search")
        with patch.object(llm, "image_search_query") as asked:
            words = scene_editor.reference_photo_words("search", 1)
        asked.assert_not_called()
        assert words == {"query": "Gary Ridgway 2001 arrest", "from": "plan"}
