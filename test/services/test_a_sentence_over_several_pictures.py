"""一句话可以配几张图，但不能被拆成几句来念。

画面定成三五秒一张时，一句十来秒的话要两三张图。原来分镜的每一段都必须以
句号结尾、各念各的一行，于是 "and it took nearly four decades for them to
learn what had become of her." 被写成了 "…for them." 和 "To learn what had
become of her." 两段：配音念成两句，字幕断成两条。钉住的事：

* 分镜里前一段不写句末标点，就是这句话还没念完；脚本里它和下一段用空格接
  成一句，不换行；
* 最后一段、带停顿的那一段必须把话说完；
* 时间线上这句话是一条字幕，几张图照字数把它的时间分掉；
* 条数碰巧相等（一段占两条、另一句配了两张图）不当成一对一；
* 编辑器里每张图显示它自己那一截，重新配音时仍然接回一句。

One sentence may be shown over several pictures; it is never recorded as
several sentences. A scene whose narration stops without closing punctuation
runs on into the next: the script joins them with a space, the subtitle stays
one line, and the pictures share its time by their share of its letters.
"""

import json

import pytest

from app.services import edit_history, scene_editor, scene_manifest, scene_plan
from app.services import task_artifacts, timeline
from app.utils import utils

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

PLAN = {
    "subject": "The girl called Sharon Marshall",
    "scenes": [
        {"narration": "They had lost track of her in nineteen seventy five,",
         "prompt": "An empty porch"},
        {"narration": "and it took nearly four decades for them",
         "prompt": "A calendar losing its pages"},
        {"narration": "to learn what had become of her.",
         "prompt": "A letter on a kitchen table", "pause_after": 0.5},
        {"narration": "Floyd did not live to see much beyond that. He died.",
         "prompt": "A prison corridor"},
    ],
}


def _plan(scenes=None):
    return scene_plan.parse(json.dumps({**PLAN, "scenes": scenes or PLAN["scenes"]}))


class TestThePlan:
    def test_a_scene_without_closing_punctuation_runs_on_into_the_next(self):
        script = scene_plan.script_of(_plan())
        assert script.split("\n") == [
            "They had lost track of her in nineteen seventy five, and it took "
            "nearly four decades for them to learn what had become of her. "
            "[pause: 0.5s]",
            "Floyd did not live to see much beyond that. He died.",
        ]

    def test_the_sentence_is_one_spoken_line(self):
        lines = utils.split_string_by_punctuations(
            utils.remove_pause_tags(scene_plan.script_of(_plan())))
        assert ("and it took nearly four decades for them to learn what had "
                "become of her") in lines

    def test_its_line_is_counted_once_on_the_scene_that_ends_it(self):
        plan = _plan()
        assert scene_plan.sentences_of(plan["scenes"]) == [[1], [2, 3], [4]]
        assert scene_plan.cue_counts(plan["scenes"]) == [1, 0, 1, 2]

    def test_sentences_that_end_are_still_one_line_each(self):
        plan = _plan([
            {"narration": "He starts to dig.", "prompt": "a"},
            {"narration": "It is not soil.", "prompt": "b"},
        ])
        assert scene_plan.script_of(plan) == "He starts to dig.\nIt is not soil."

    def test_the_last_scene_must_finish_its_sentence(self):
        with pytest.raises(scene_plan.ScenePlanError, match="mid-sentence"):
            _plan(PLAN["scenes"][:2])

    def test_a_pause_does_not_go_inside_a_sentence(self):
        scenes = [dict(PLAN["scenes"][1], pause_after=1), PLAN["scenes"][2]]
        with pytest.raises(scene_plan.ScenePlanError, match="scene 1 has a pause"):
            _plan(scenes)

    def test_nor_when_direction_is_added_afterwards(self):
        stored = scene_plan.stored(_plan())
        with pytest.raises(scene_plan.ScenePlanError, match="scene 2 has a pause"):
            scene_plan.redirected(
                stored, json.dumps({"scenes": {"2": {"pause_after": 1}}}))

    def test_the_report_says_which_sentences_are_shared(self):
        assert ("1 sentence spoken whole across more than one picture: scenes 2-3"
                in scene_plan.report(_plan()))


    def test_a_full_stop_added_to_half_a_sentence_is_remarked_on(self):
        plan = _plan([
            {"narration": "It took nearly four decades for them.", "prompt": "a"},
            {"narration": "to learn what had become of her.", "prompt": "b"},
        ])
        assert "scene 2 starts in lower case after a full stop" in scene_plan.report(plan)
        assert "lower case" not in scene_plan.report(_plan())


CUES = [
    {"start": 0.0, "end": 2.0, "text": "One two six ten"},
    {"start": 2.0, "end": 6.0, "text": "Five"},
    {"start": 6.0, "end": 7.0, "text": "Nine"},
]
SCENES = [{"narration": "One two"}, {"narration": "six ten."},
          {"narration": "Five. Nine."}]


class TestTheTimeline:
    def test_the_second_picture_starts_inside_the_line(self):
        starts, missing = timeline.match_scenes_to_cues(
            CUES, [scene["narration"] for scene in SCENES])
        assert missing == -1
        assert starts == [0.0, 1.0, 2.0]

    def test_as_many_lines_as_scenes_is_not_one_each(self):
        # Three subtitle lines, three scenes - and none of them one to one.
        assert timeline.spoken_windows("no-task", CUES, SCENES, 8.0) == [
            {"start": 0.0, "end": 1.0},
            {"start": 1.0, "end": 2.0},
            {"start": 2.0, "end": 8.0},
        ]

    def test_one_line_each_is_still_one_line_each(self):
        scenes = [{"narration": "One two six ten."}, {"narration": "Five."},
                  {"narration": "Nine."}]
        assert timeline.spoken_windows("no-task", CUES, scenes, 8.0) == [
            {"start": 0.0, "end": 2.0},
            {"start": 2.0, "end": 6.0},
            {"start": 6.0, "end": 8.0},
        ]

    def test_scenes_that_do_not_say_the_whole_narration_are_not_trusted(self):
        # An old task's scenes may store only the first lines, paired by index.
        scenes = [{"narration": "One two"}, {"narration": "six ten."}]
        assert timeline._windows_from_spoken_text(CUES, scenes, 8.0) == []

    def test_a_scene_that_is_not_spoken_is_named(self):
        starts, missing = timeline.match_scenes_to_cues(
            CUES, ["One two", "six eleven.", "Five. Nine."])
        assert missing == 1


SRT = """1
00:00:00,000 --> 00:00:02,000
One two six ten

2
00:00:02,000 --> 00:00:06,000
Five

3
00:00:06,000 --> 00:00:07,000
Nine
"""


def _shared_task(task_root):
    task_id, _ = _reviewed_task(task_root, scenes=len(SCENES))
    (task_root / task_id / "subtitle.srt").write_text(SRT, encoding="utf-8")
    task_artifacts.patch_script_data(
        task_id, subtitle_path="subtitle.srt", audio_duration=8.0,
        script="One two six ten.\nFive. Nine.",
    )
    scenes = scene_manifest.load(task_id)["scenes"]
    for scene, wanted in zip(scenes, SCENES):
        scene["narration"] = wanted["narration"]
        scene.pop("duration", None)
        scene.pop("trim_start", None)
    with edit_history.paused():
        scene_manifest.save_scenes(task_id, scenes)
    return task_id


class TestTheEditor:
    def test_the_pictures_share_the_time_of_their_sentence(self, task_root):
        task_id = _shared_task(task_root)
        assert scene_editor.narration_paced_durations(task_id) == [1.0, 1.0, 6.0]

    def test_the_render_plans_the_same_windows(self, task_root):
        task_id = _shared_task(task_root)
        manifest = scene_manifest.load(task_id)
        windows = scene_editor.planned_windows(task_id, manifest, probe=False)
        assert [(w["start"], w["end"]) for w in windows] == [
            (0.0, 1.0), (1.0, 2.0), (2.0, 8.0)]

    def test_each_picture_shows_its_own_part(self, task_root):
        task_id = _shared_task(task_root)
        shown = scene_editor.narration_for_scenes(task_id)
        assert shown[:2] == ["One two", "six ten."]

    def test_recording_again_keeps_the_sentence_whole(self, task_root):
        task_id = _shared_task(task_root)
        lines = scene_editor.narration_script(task_id).split("\n")
        assert lines[0] == "One two six ten."
        assert "six ten." not in lines[1:]
        assert len(utils.split_string_by_punctuations("\n".join(lines))) == 3
