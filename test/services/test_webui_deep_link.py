"""用任务链接打开 WebUI：表单要填回去，审片面板要出现。

两个问题同源：任务状态存在进程内存里，WebUI 一重启就没了，而任务清单在
磁盘上。因此"状态里查不到"不能当成"还在生成"，参数也不该因此丢失。
"""

import json
import os
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task_artifacts
from app.utils import utils
from webui import review


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)

    task_id = "7b5266c7-c7ee-4cd3-af6a-603b96235baa"
    directory = root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    materials = []
    for name in ("1.mp4", "2.mp4", "audio.mp3"):
        target = directory / name
        target.write_bytes(b"x")
        materials.append(str(target))

    params = VideoParams(
        video_subject="The hidden cost of the first bread",
        video_source="openai_image",
    )
    task_artifacts.write_script_data(
        task_id,
        {
            "script": "One. Two.",
            "search_terms": ["a", "b"],
            "params": params.model_dump(),
        },
    )
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script="One. Two.",
        video_terms=["a", "b"],
        materials=materials[:2],
        audio_file=materials[2],
        audio_duration=61.0,
        subtitle_path="",
        video_source="openai_image",
        clip_duration=5,
    )
    return task_id


class TestPanelWithoutState:
    """进程里查不到状态的任务，仍然要能打开审片面板。"""

    def test_a_task_with_scenes_is_reviewable_without_any_state(self, task):
        # 这正是 WebUI 重启后用 ?generation_task=<id> 打开的情形。
        assert review.has_scenes(task) is True

    def test_describe_works_without_state(self, task):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=False):
            described = scene_editor.describe(task)
        assert len(described["scenes"]) == 2
        assert described["audio_stale"] is False

    def test_a_task_without_scenes_is_not_reviewable(self, tmp_path, monkeypatch):
        root = tmp_path / "tasks"

        def fake_task_dir(sub_dir: str = ""):
            target = root / sub_dir if sub_dir else root
            target.mkdir(parents=True, exist_ok=True)
            return str(target)

        monkeypatch.setattr(utils, "task_dir", fake_task_dir)
        task_artifacts.write_script_data("empty", {"script": "x"})
        # 面板会据此返回 False，调用方继续走原本的"生成中/失败"展示。
        assert review.has_scenes("empty") is False

    def test_an_unknown_task_is_not_reviewable(self, task):
        assert review.has_scenes("no-such-task") is False


class TestRestorePayload:
    """链接打开时要能从任务清单把参数读回来。"""

    def test_the_manifest_params_validate_as_video_params(self, task):
        payload = task_artifacts.read_script_data(task)
        cleaned = {
            key: value
            for key, value in (payload.get("params") or {}).items()
            if key in VideoParams.model_fields
        }
        restored = VideoParams(**cleaned)
        assert restored.video_subject == "The hidden cost of the first bread"
        assert restored.video_source == "openai_image"

    def test_unknown_fields_in_an_older_manifest_are_ignored(self, task):
        """清单可能是旧版本写的，一个历史字段不该让恢复整个失败。"""
        payload = task_artifacts.read_script_data(task)
        payload["params"]["a_field_from_an_older_build"] = True
        task_artifacts.write_script_data(task, payload)
        cleaned = {
            key: value
            for key, value in task_artifacts.read_script_data(task)["params"].items()
            if key in VideoParams.model_fields
        }
        assert VideoParams(**cleaned).video_subject
