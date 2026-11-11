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
