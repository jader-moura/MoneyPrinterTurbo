"""审片接口：路由匹配顺序、状态码映射，以及"忙的时候不许改"。"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task_artifacts
from app.services import state as sm
from app.utils import utils


@pytest.fixture
def client():
    return TestClient(asgi.app)


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


@pytest.fixture
def reviewed(task_root):
    """一个已经跑到素材阶段、两个场景、等待审片的任务。"""
    task_id = "api-review"
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    materials = []
    for name in ("1.png", "2.png", "audio.mp3"):
        target = directory / name
        target.write_bytes(b"x")
        materials.append(str(target))

    params = VideoParams(video_subject="seeds", video_source="openai_image")
    task_artifacts.write_script_data(
        task_id,
        {"script": "One. Two.", "search_terms": ["a", "b"], "params": params.model_dump()},
    )
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script="One. Two.",
        video_terms=["a", "b"],
        materials=materials[:2],
        audio_file=materials[2],
        audio_duration=8.0,
        subtitle_path="",
        video_source="openai_image",
        clip_duration=5,
    )
    with patch.object(sm.state, "get_task", return_value={"task_id": task_id}):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=False):
            yield task_id


class TestRouting:
    def test_reorder_is_not_swallowed_by_the_indexed_route(self, client, reviewed):
        """``/scenes/reorder`` 必须匹配到重排接口，而不是 ``/scenes/{index}``。"""
        response = client.post(
            f"/api/v1/tasks/{reviewed}/scenes/reorder", json={"order": [1, 0]}
        )
        assert response.status_code == 200
        assert [scene["index"] for scene in response.json()["data"]["scenes"]] == [0, 1]

    def test_listing_scenes_exposes_stream_urls(self, client, reviewed):
        response = client.get(f"/api/v1/tasks/{reviewed}/scenes")
        assert response.status_code == 200
        scenes = response.json()["data"]["scenes"]
        assert len(scenes) == 2
        assert scenes[0]["material"] == "1.png"
        assert "/api/v1/stream/" in scenes[0]["url"]

    def test_an_unknown_task_is_404(self, client, task_root):
        with patch.object(sm.state, "get_task", return_value=None):
            response = client.get("/api/v1/tasks/ghost/scenes")
        assert response.status_code == 404

    def test_a_negative_scene_index_is_rejected_by_validation(self, client, reviewed):
        """本应用把请求校验错误统一映射成 400，而不是 FastAPI 默认的 422。"""
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/-1", json={"keyword": "x"}
        )
        assert response.status_code == 400


class TestStatusCodes:
    def test_editing_a_busy_task_is_409(self, client, reviewed):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
            response = client.put(
                f"/api/v1/tasks/{reviewed}/scenes/0", json={"keyword": "x"}
            )
        assert response.status_code == 409

    def test_an_out_of_range_scene_is_400(self, client, reviewed):
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/9", json={"keyword": "x"}
        )
        assert response.status_code == 400

    def test_a_partial_reorder_is_400(self, client, reviewed):
        response = client.post(
            f"/api/v1/tasks/{reviewed}/scenes/reorder", json={"order": [0]}
        )
        assert response.status_code == 400

    def test_rendering_with_a_stale_narration_is_400(self, client, reviewed):
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0", json={"narration": "changed"}
        )
        response = client.post(f"/api/v1/tasks/{reviewed}/render")
        assert response.status_code == 400
        assert "regenerate the audio" in response.json()["message"]


class TestEditing:
    def test_editing_narration_marks_the_audio_stale(self, client, reviewed):
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0", json={"narration": "A new line."}
        )
        assert response.status_code == 200
        assert response.json()["data"]["narration"] == "A new line."
        listed = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()
        assert listed["data"]["audio_stale"] is True

    def test_regenerating_one_scene_calls_the_provider_once(self, client, reviewed):
        new_file = str(utils.task_dir(reviewed) + "/new.png")
        with open(new_file, "wb") as handle:
            handle.write(b"x")
        with patch.object(
            scene_editor.material, "download_videos", return_value=[new_file]
        ) as download:
            response = client.post(
                f"/api/v1/tasks/{reviewed}/scenes/0/material",
                json={"keyword": "a farmer sowing"},
            )
        assert response.status_code == 200
        assert download.call_count == 1
        assert download.call_args.kwargs["search_terms"] == ["a farmer sowing"]

    def test_deleting_a_scene_returns_the_remaining_list(self, client, reviewed):
        response = client.delete(f"/api/v1/tasks/{reviewed}/scenes/0")
        assert response.status_code == 200
        assert len(response.json()["data"]["scenes"]) == 1


class TestSubmission:
    def test_materials_route_starts_a_reviewable_run(self, client, task_root):
        """没有这条路由，HTTP 调用方根本无法开始一次可审片的生成。"""
        with patch("app.controllers.v1.video.task_manager") as manager:
            response = client.post(
                "/api/v1/materials",
                json={"video_subject": "seeds", "video_source": "pexels"},
            )
        assert response.status_code == 200
        assert manager.add_task.call_args.kwargs["stop_at"] == "materials"

    def test_render_queues_a_resumed_task(self, client, reviewed):
        with patch.object(scene_editor.tm, "start") as start:
            with patch("app.controllers.v1.editor.task_manager") as manager:
                response = client.post(f"/api/v1/tasks/{reviewed}/render")
        assert response.status_code == 200
        assert response.json()["data"]["task_id"] == reviewed
        kwargs = manager.add_task.call_args.kwargs
        assert kwargs["start_from"] == "video"
        assert kwargs["stop_at"] == "video"
        assert start.call_count == 0

    def test_rerecording_runs_the_subtitle_stage_too(self, client, reviewed):
        """只重配音会留下一个保证错位的字幕文件，所以必须跑到 subtitle。"""
        client.put(f"/api/v1/tasks/{reviewed}/scenes/0", json={"narration": "New."})
        with patch("app.controllers.v1.editor.task_manager") as manager:
            response = client.post(f"/api/v1/tasks/{reviewed}/audio")
        assert response.status_code == 200
        kwargs = manager.add_task.call_args.kwargs
        assert kwargs["start_from"] == "audio"
        assert kwargs["stop_at"] == "subtitle"
