"""先看提示词，再开画。

Why this file exists
--------------------
出图是整条流水线里最慢最贵的一步：本机一张要几分钟，按张计费的源按张烧钱。
而在这之前，用户第一次看到提示词的时机是**图全部画完之后**——一条 10 分钟
的旁白是 200 多张图、好几个小时，等看到的时候想改一个字就意味着整批重画。

现在流水线跑到字幕就停：编辑器里摆好每一段的位置，写着这一段**将要**画
什么，旁白和字幕都已经在，用户能一边听一边看。改完提示词点"开画"，才一段
一个地排进出图队列。

这组用例钉住的是：停在该停的地方、摆出来的张数和真开画时的张数一致、
用户改的提示词就是真正送出去的那个、以及点头之前一张图都不许画。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import (
    scene_editor,
    scene_manifest,
    scene_regen_queue,
    task,
    task_artifacts,
)
from app.utils import utils


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


@pytest.fixture(autouse=True)
def no_cross_post():
    with patch.object(
        task.upload_post.upload_post_service, "is_configured", return_value=False
    ):
        yield


def _params(**overrides):
    defaults = dict(
        video_subject="clocks",
        video_script="Clocks ran late. Nobody touched them.",
        video_terms=["a clock", "a kitchen"],
        video_source="openai_image",
        subtitle_enabled=False,
        bgm_type="",
        video_clip_duration=3,
    )
    defaults.update(overrides)
    return VideoParams(**defaults)


def _run_to_the_checkpoint(task_root, task_id, *, params=None, duration=30.0):
    """整条跑到"等着看提示词"那一步。一张图都不该画。"""
    params = params or _params()
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    audio = directory / "audio.mp3"
    audio.write_bytes(b"x")
    with (
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_audio", return_value=(str(audio), duration, None)),
        patch.object(task, "get_video_materials") as drew,
    ):
        task.start(task_id, params, stop_at="subtitle")
    drew.assert_not_called()
    return str(audio)


class TestWhereTheRunStops:
    def test_a_generated_source_stops_before_drawing_anything(self, task_root):
        _run_to_the_checkpoint(task_root, "stops")
        scenes = scene_manifest.load("stops")["scenes"]
        assert scenes, "编辑器要有东西可显示"
        assert all(not scene["material"] for scene in scenes)

    def test_the_narration_and_the_script_are_there_to_check_against(self, task_root):
        audio = _run_to_the_checkpoint(task_root, "narration")
        manifest = scene_manifest.load("narration")
        assert manifest["audio_file"] == audio
        assert manifest["audio_duration"] == 30.0
        assert "Clocks ran late" in manifest["script"]

    def test_one_slot_per_picture_the_narration_needs(self, task_root):
        """30 秒旁白、每段 3 秒：10 个位置,不是"模型吐了几个关键词"个。"""
        _run_to_the_checkpoint(task_root, "count")
        assert len(scene_manifest.load("count")["scenes"]) == 10

    def test_the_prompts_cycle_the_keywords_in_order(self, task_root):
        _run_to_the_checkpoint(task_root, "cycle")
        shown = [s["keyword"] for s in scene_manifest.load("cycle")["scenes"]]
        assert shown == ["a clock", "a kitchen"] * 5

    def test_every_slot_has_its_own_identity(self, task_root):
        """队列靠身份认人。占位场景没有素材文件,所以 id 是唯一的身份。"""
        _run_to_the_checkpoint(task_root, "ids")
        ids = [s["id"] for s in scene_manifest.load("ids")["scenes"]]
        assert all(ids) and len(set(ids)) == len(ids)


class TestWhoGetsTheCheckpoint:
    def test_stock_footage_keeps_the_one_click_run(self):
        """库存搜索又快又免费，多一道确认只是多一次点击。"""
        assert task.reviews_prompts_first(_params(video_source="pexels")) is False

    def test_generated_images_get_it(self):
        assert task.reviews_prompts_first(_params(video_source="openai_image")) is True
        assert task.reviews_prompts_first(_params(video_source="codex_image")) is True

    def test_it_is_on_when_the_config_has_never_said_otherwise(self):
        assert task.reviews_prompts_first(_params(), app_config={}) is True

    def test_a_user_who_turned_it_off_is_left_alone(self):
        assert task.reviews_prompts_first(
            _params(), app_config={"review_prompts_before_images": False}
        ) is False


class TestApprovingThePrompts:
    def test_editing_a_prompt_is_what_gets_drawn(self, task_root):
        _run_to_the_checkpoint(task_root, "edited")
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.update_scene(
                "edited", 0, keyword="Thousand of clocks running late"
            )
        plan = scene_editor.check_regenerate("edited", 0)
        assert plan["keyword"] == "Thousand of clocks running late"

    def test_nothing_is_drawn_until_the_user_says_so(self, task_root):
        _run_to_the_checkpoint(task_root, "patient")
        assert scene_regen_queue.snapshot("patient")["busy"] is False


class TestDrawingThemAll:
    @pytest.fixture(autouse=True)
    def clean_queue(self):
        scene_regen_queue._reset_for_tests()
        yield
        scene_regen_queue._reset_for_tests()

    def test_approving_queues_one_job_per_missing_picture(self, task_root):
        _run_to_the_checkpoint(task_root, "draw")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("draw")
        assert result["queued"] == 10
        assert result["waiting"] == 10

    def test_each_job_carries_its_own_prompt(self, task_root):
        _run_to_the_checkpoint(task_root, "prompts")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("prompts")
        assert [job["keyword"] for job in result["jobs"]] == (
            ["a clock", "a kitchen"] * 5
        )

    def test_each_job_is_anchored_to_its_slot(self, task_root):
        _run_to_the_checkpoint(task_root, "anchored")
        scenes = scene_manifest.load("anchored")["scenes"]
        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("anchored")
        assert [job["scene_id"] for job in result["jobs"]] == [
            scene["id"] for scene in scenes
        ]

    def test_asking_twice_does_not_draw_everything_twice(self, task_root):
        """用户点两下"开画"不该把每一张画两次——那是双倍的钱和双倍的时间。"""
        _run_to_the_checkpoint(task_root, "twice")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            scene_regen_queue.enqueue_pending("twice")
            with pytest.raises(scene_manifest.SceneError) as caught:
                scene_regen_queue.enqueue_pending("twice")
        assert "already queued" in str(caught.value)
        assert scene_regen_queue.snapshot("twice")["waiting"] == 10

    def test_a_task_whose_pictures_all_exist_has_nothing_to_draw(self, task_root):
        task_id = "complete"
        directory = task_root / task_id
        directory.mkdir(parents=True, exist_ok=True)
        clip = directory / "1.mp4"
        clip.write_bytes(b"x")
        task_artifacts.write_script_data(
            task_id,
            {
                "script": "One.",
                "search_terms": ["a clock"],
                "params": _params().model_dump(),
                "scenes": [
                    {"index": 0, "id": "abc", "keyword": "a clock",
                     "material": str(clip), "source": "openai_image"}
                ],
            },
        )
        with pytest.raises(scene_manifest.SceneError) as caught:
            scene_regen_queue.enqueue_pending(task_id)
        assert "already has a picture" in str(caught.value)

    def test_a_bad_prompt_stops_the_whole_batch_before_anything_is_queued(
        self, task_root
    ):
        """排一半比一张不排更难收拾，所以一个不合格就整批不排。"""
        _run_to_the_checkpoint(task_root, "bad")
        with patch.object(
            scene_editor, "check_regenerate",
            side_effect=scene_manifest.SceneError("no keyword to generate from"),
        ):
            with pytest.raises(scene_manifest.SceneError):
                scene_regen_queue.enqueue_pending("bad")
        assert scene_regen_queue.snapshot("bad")["waiting"] == 0


class TestTryingOneFirst:
    """先试一张，再开整批。

    出图最贵的错误不是某一张画坏了，而是两百张全照着一个不对的画风、一种
    不对的提示词写法画完——几个小时之后才看得出来，而且钱已经花了。所以
    提示词那一屏上要有"只画这一张"的入口：看过一张再决定整批。

    服务端本来就允许还没出图的那几段单独进队（``check_regenerate`` 不要求
    这一段已经有文件），所以那个入口不需要新接口。这组用例钉住的就是这条
    路确实通、试的是用户改过的那个提示词、以及试完之后整批照常能开，不会
    把试过的那张再画一遍。
    """

    @pytest.fixture(autouse=True)
    def clean_queue(self):
        scene_regen_queue._reset_for_tests()
        yield
        scene_regen_queue._reset_for_tests()

    def test_one_slot_can_be_drawn_on_its_own(self, task_root):
        _run_to_the_checkpoint(task_root, "sample")
        scenes = scene_manifest.load("sample")["scenes"]
        with patch.object(scene_regen_queue, "_ensure_worker"):
            job = scene_regen_queue.enqueue("sample", 3)
        assert job["scene_id"] == scenes[3]["id"]
        assert job["keyword"] == scenes[3]["keyword"]
        # 排的就是这一张，别人一张都没动。
        assert scene_regen_queue.snapshot("sample")["waiting"] == 1

    def test_the_prompt_typed_in_the_panel_is_the_one_drawn(self, task_root):
        """改一笔再试——试的就该是改过的那一笔，否则这一趟白花。"""
        _run_to_the_checkpoint(task_root, "typed")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            job = scene_regen_queue.enqueue(
                "typed", 1, keyword="a cracked wall clock at dawn"
            )
        assert job["keyword"] == "a cracked wall clock at dawn"

    def test_drawing_the_rest_afterwards_skips_the_one_already_queued(self, task_root):
        """试完一张接着开整批：试过的那张不该再排一次（双倍的钱和时间）。"""
        _run_to_the_checkpoint(task_root, "then_all")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            scene_regen_queue.enqueue("then_all", 2)
            result = scene_regen_queue.enqueue_pending("then_all")
        assert result["queued"] == 9
        assert scene_regen_queue.snapshot("then_all")["waiting"] == 10


class TestTheEditedPromptSurvivesTheClick:
    """改完提示词就存，不等用户点保存。

    "开画"和"先试一张"读的都是**清单里存着的**那个词。而提示词框原来只有
    点了 "Save prompt" 才落盘：改完直接开画，画出来的是旧词那两百张，界面
    上还看不出哪里不对——框里明明写着新的。编辑器现在改完就存（停手半秒、
    焦点离开、开画之前各存一次），这一组钉住的是服务端这一半：存下去的就是
    真正画出来的，而且存一个词不会把配音和字幕带下水。
    """

    @pytest.fixture(autouse=True)
    def clean_queue(self):
        scene_regen_queue._reset_for_tests()
        yield
        scene_regen_queue._reset_for_tests()

    def test_a_saved_prompt_is_what_the_batch_draws(self, task_root):
        _run_to_the_checkpoint(task_root, "saved")
        scene_editor.update_scene("saved", 4, keyword="a cracked wall clock at dawn")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("saved")
        assert result["jobs"][4]["keyword"] == "a cracked wall clock at dawn"
        # 别人一个字都没被带着改。
        assert result["jobs"][0]["keyword"] == "a clock"

    def test_a_saved_prompt_is_what_a_single_draw_uses(self, task_root):
        _run_to_the_checkpoint(task_root, "saved_one")
        scene_editor.update_scene("saved_one", 2, keyword="a kitchen at night")
        with patch.object(scene_regen_queue, "_ensure_worker"):
            job = scene_regen_queue.enqueue("saved_one", 2)
        assert job["keyword"] == "a kitchen at night"

    def test_saving_a_prompt_does_not_invalidate_the_narration(self, task_root):
        """自动保存现在每改几个字就发一次，所以它绝不能作废配音。

        改台词会让配音和字幕一起失效（时间轴对不上了）。提示词不是台词：
        它只说这一段画什么。如果存提示词顺手把 ``audio_stale`` 立起来，
        用户在这一屏改几个提示词就会被要求重新配音一次。
        """
        _run_to_the_checkpoint(task_root, "quiet")
        scene_editor.update_scene("quiet", 0, keyword="a kitchen at night")
        assert not scene_manifest.load_raw("quiet").get("audio_stale")

    def test_the_prompt_survives_a_reload(self, task_root):
        """存下去的要能读回来——用户关掉页面再回来，改过的词还在。"""
        _run_to_the_checkpoint(task_root, "reload")
        scene_editor.update_scene("reload", 1, keyword="a hand winding a clock")
        scenes = scene_manifest.load("reload")["scenes"]
        assert scenes[1]["keyword"] == "a hand winding a clock"


class TestWhatTheBatchLeavesAlone:
    """开整批时，已经画出来的那几张不许再画一遍。

    "先试一张"之后点"开画"，是这条流程里最自然的下一步。如果整批会把刚
    试出来的那张再画一遍，用户就白花了一次等待和一次出图——而且试出来的
    那张（他点头认可的那张）会被另一张悄悄换掉。

    这不是靠"开画"那边特意记着谁画过：画出来的那一段清单里就有了素材，
    而整批只认"还没有素材"的那几段。
    """

    @pytest.fixture(autouse=True)
    def clean_queue(self):
        scene_regen_queue._reset_for_tests()
        yield
        scene_regen_queue._reset_for_tests()

    def _draw_one(self, task_root, task_id, index):
        """真的把第 index 段画出来（只把出图那一步换掉）。"""
        drawn = task_root / task_id / f"sample-{index}.mp4"
        drawn.parent.mkdir(parents=True, exist_ok=True)
        drawn.write_bytes(b"x")
        with patch.object(
            scene_editor,
            "_produce_material",
            return_value=(str(drawn), "openai_image"),
        ):
            scene_editor.regenerate_material(task_id, index)
        return str(drawn)

    def test_a_picture_already_drawn_is_not_drawn_again(self, task_root):
        _run_to_the_checkpoint(task_root, "once")
        drawn = self._draw_one(task_root, "once", 3)
        scenes = scene_manifest.load("once")["scenes"]
        assert scenes[3]["material"] == drawn, "先试的那一张要真的画出来了"

        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("once")

        assert result["queued"] == 9, "十段里画过一段，整批只剩九段"
        assert all(job["scene_id"] != scenes[3]["id"] for job in result["jobs"])

    def test_the_picture_it_already_drew_is_left_on_disk(self, task_root):
        """用户点头认可的那张不该被另一张悄悄换掉。"""
        _run_to_the_checkpoint(task_root, "keep")
        drawn = self._draw_one(task_root, "keep", 0)
        with patch.object(scene_regen_queue, "_ensure_worker"):
            scene_regen_queue.enqueue_pending("keep")
        scenes = scene_manifest.load("keep")["scenes"]
        assert scenes[0]["material"] == drawn

    def test_drawing_every_remaining_picture_empties_the_waiting_list(self, task_root):
        """画过的加上排着的，正好是全部——不多排，也不漏排。"""
        _run_to_the_checkpoint(task_root, "tally")
        self._draw_one(task_root, "tally", 7)
        with patch.object(scene_regen_queue, "_ensure_worker"):
            result = scene_regen_queue.enqueue_pending("tally")
        scenes = scene_manifest.load("tally")["scenes"]
        assert len(scene_manifest.pending_scenes(scenes)) == result["queued"] == 9
