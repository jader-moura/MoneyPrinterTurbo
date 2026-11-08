"""时间线接口：读模型、时长设置、以及剪辑表什么时候才会写进任务清单。"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task_artifacts, timeline
from app.services import state as sm
from app.utils import utils


@pytest.fixture
def client():
    return TestClient(asgi.app)


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    """一个跑到素材阶段、两个场景、等待审片的任务。"""
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)

    task_id = "tl-task"
    directory = root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    materials = []
    for name in ("1.mp4", "2.mp4", "audio.mp3"):
        target = directory / name
        target.write_bytes(b"x")
        materials.append(str(target))

    params = VideoParams(video_subject="seeds", video_source="openai_image")
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
        audio_duration=8.0,
        subtitle_path="",
        video_source="openai_image",
        clip_duration=5,
    )
    with patch.object(sm.state, "get_task", return_value={"task_id": task_id}):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=False):
            # 素材是 1 字节占位文件，真探测不出时长；时长语义本身由
            # test_timeline.py 覆盖，这里关心的是接口行为。
            with patch.object(timeline, "probe_duration", return_value=5.0):
                yield task_id


class TestTimelineRead:
    def test_the_timeline_route_reports_source_durations(self, client, reviewed):
        """时间线必须知道源长度才能画出拖拽边界。"""
        response = client.get(f"/api/v1/tasks/{reviewed}/timeline")
        assert response.status_code == 200
        data = response.json()["data"]
        assert [scene["source_duration"] for scene in data["scenes"]] == [5.0, 5.0]
        assert data["clip_speed"] == 1.0
        assert data["audio_duration"] == 8.0
        assert data["has_timing_edits"] is False

    def test_the_scenes_route_does_not_probe_durations(self, client, reviewed):
        """Phase 1 的审片列表不需要源长度，不该为此多付十几次解码器启动。"""
        with patch.object(timeline, "probe_duration") as probe:
            response = client.get(f"/api/v1/tasks/{reviewed}/scenes")
        assert response.status_code == 200
        assert probe.call_count == 0

    def test_an_unedited_scene_reports_no_duration(self, client, reviewed):
        response = client.get(f"/api/v1/tasks/{reviewed}/timeline")
        scene = response.json()["data"]["scenes"][0]
        assert scene["duration"] is None
        assert scene["trim_start"] == 0.0


class TestTimingUpdates:
    def test_setting_a_duration_is_stored(self, client, reviewed):
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing",
            json={"trim_start": 1.0, "duration": 2.0},
        )
        assert response.status_code == 200
        scene = response.json()["data"]
        assert scene["trim_start"] == 1.0
        assert scene["duration"] == 2.0

    def test_a_duration_past_the_material_is_clamped_and_flagged(self, client, reviewed):
        """拖过素材末尾不报错，但要让界面知道被收住了。"""
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing",
            json={"trim_start": 0.0, "duration": 99.0},
        )
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["duration"] == 5.0
        assert body["clamped"] is True

    def test_reset_returns_the_scene_to_an_automatic_duration(self, client, reviewed):
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing",
            json={"trim_start": 1.0, "duration": 2.0},
        )
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"reset": True}
        )
        assert response.status_code == 200
        listed = client.get(f"/api/v1/tasks/{reviewed}/timeline").json()["data"]
        assert listed["scenes"][0]["duration"] is None
        assert listed["has_timing_edits"] is False

    def test_a_negative_duration_is_rejected_by_validation(self, client, reviewed):
        """本应用把请求校验错误统一映射成 400，而不是 FastAPI 默认的 422。"""
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"duration": -3}
        )
        assert response.status_code == 400

    def test_an_out_of_range_scene_is_400(self, client, reviewed):
        response = client.put(
            f"/api/v1/tasks/{reviewed}/scenes/9/timing", json={"duration": 2.0}
        )
        assert response.status_code == 400

    def test_editing_timing_on_a_busy_task_is_409(self, client, reviewed):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
            response = client.put(
                f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"duration": 2.0}
            )
        assert response.status_code == 409


class TestEditListPersistence:
    def test_no_edit_list_is_written_when_nobody_touched_the_timeline(
        self, client, reviewed
    ):
        """没拖过时间线就必须回到自动切片逻辑，默认出片结果才会一个字节不变。"""
        with patch("app.controllers.v1.editor.task_manager"):
            response = client.post(f"/api/v1/tasks/{reviewed}/render")
        assert response.status_code == 200
        assert task_artifacts.read_script_data(reviewed).get("edit_list") == []

    def test_an_edit_list_is_written_once_a_scene_is_trimmed(self, client, reviewed):
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing",
            json={"trim_start": 1.0, "duration": 2.0},
        )
        with patch("app.controllers.v1.editor.task_manager"):
            response = client.post(f"/api/v1/tasks/{reviewed}/render")
        assert response.status_code == 200
        stored = task_artifacts.read_script_data(reviewed)["edit_list"]
        # 只拖了第一段，但两段都要进剪辑表，否则另一段会回到自动时长。
        assert len(stored) == 2
        # 素材按"任务目录相对名"存，因为 /stream 是从 tasks 根目录往下服务的。
        assert stored[0] == {
            "material": "1.mp4",
            "start": 1.0,
            "end": 3.0,
        }
        assert stored[1]["material"] == "2.mp4"

    def test_the_stored_edit_list_follows_the_reordered_scenes(self, client, reviewed):
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"duration": 2.0}
        )
        client.post(f"/api/v1/tasks/{reviewed}/scenes/reorder", json={"order": [1, 0]})
        with patch("app.controllers.v1.editor.task_manager"):
            client.post(f"/api/v1/tasks/{reviewed}/render")
        stored = task_artifacts.read_script_data(reviewed)["edit_list"]
        assert [item["material"] for item in stored] == [
            "2.mp4",
            "1.mp4",
        ]

    def test_the_edit_list_resolves_back_to_absolute_paths_for_the_renderer(
        self, client, reviewed
    ):
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"duration": 2.0}
        )
        with patch("app.controllers.v1.editor.task_manager"):
            client.post(f"/api/v1/tasks/{reviewed}/render")
        resolved = scene_manifest.edit_list_for_render(reviewed)
        assert len(resolved) == 2
        assert resolved[0]["path"].endswith("1.mp4")
        assert resolved[0]["end"] == 2.0

    def test_no_edit_list_means_the_renderer_gets_nothing(self, client, reviewed):
        assert scene_manifest.edit_list_for_render(reviewed) == []


class TestRenderSettings:
    """渲染设置要能从接口读到。

    "看不到画面在动"和"看不到转场"最常见的原因不是界面没画，而是这次生成
    压根没配：``image_motion=static`` 的素材在生成时就烘成了静止片段，
    ``transition=None`` 则是渲染阶段不加转场。界面要能说清这件事。
    """

    def test_the_timeline_reports_the_render_settings(self, client, reviewed):
        response = client.get(f"/api/v1/tasks/{reviewed}/timeline")
        settings = response.json()["data"]["render_settings"]
        assert settings["aspect"] == "9:16"
        assert settings["image_motion"] in {"static", "zoom"}
        assert "transition" in settings
        assert settings["fit"] in {"cover", "contain"}

    def test_settings_are_plain_strings_not_enum_reprs(self, client, reviewed):
        """枚举必须序列化成 "9:16" 这类值，不能是 "VideoAspect.portrait"。"""
        settings = client.get(
            f"/api/v1/tasks/{reviewed}/timeline"
        ).json()["data"]["render_settings"]
        for key in ("aspect", "image_motion", "transition", "fit"):
            assert "." not in settings[key] or key == "aspect", settings[key]
            assert "VideoAspect" not in settings[key]
            assert "ImageMotion" not in settings[key]

    def test_the_scenes_route_does_not_carry_render_settings(self, client, reviewed):
        """Phase 1 的审片列表不需要这些，保持载荷不变。"""
        data = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()["data"]
        assert "render_settings" not in data


class TestFitToNarration:
    """把画面铺满旁白。

    素材张数由关键词个数决定，不由旁白长度决定，所以"画面 24 秒、旁白 64 秒"
    是常见结果：自动出片靠循环播放凑时长，时间线不替用户循环。
    """

    def test_fitting_stretches_the_scenes_to_the_narration(self, client, reviewed):
        # 两段各 5 秒源素材，旁白 8 秒 -> 每段 4 秒。
        with patch.object(scene_manifest, "material_kind", return_value="video"):
            response = client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        assert response.status_code == 200
        scenes = response.json()["data"]["scenes"]
        assert [s["duration"] for s in scenes] == [4.0, 4.0]

    def test_fitting_scales_proportionally_and_keeps_manual_edits(
        self, client, reviewed
    ):
        """已经手调过的长短要按比例保留，平均分配会把那些编辑抹掉。"""
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/0/timing", json={"duration": 1.0}
        )
        client.put(
            f"/api/v1/tasks/{reviewed}/scenes/1/timing", json={"duration": 3.0}
        )
        # 源素材给到 10 秒，否则 6 秒那一段会被素材长度收住，测不出比例。
        with patch.object(scene_manifest, "material_kind", return_value="video"):
            with patch.object(timeline, "probe_duration", return_value=10.0):
                response = client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        scenes = response.json()["data"]["scenes"]
        # 1:3 的比例在 8 秒上就是 2 和 6。
        assert [s["duration"] for s in scenes] == [2.0, 6.0]

    def test_video_sources_are_clamped_rather_than_stretched_past_their_length(
        self, client, reviewed
    ):
        """视频素材拉不过自身长度，这时铺不满是事实，不能假装铺满了。"""
        with patch.object(scene_manifest, "material_kind", return_value="video"):
            with patch.object(timeline, "probe_duration", return_value=3.0):
                response = client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        scenes = response.json()["data"]["scenes"]
        assert [s["duration"] for s in scenes] == [3.0, 3.0]

    def test_an_image_scene_is_re_rendered_to_the_new_length(self, client, reviewed):
        """图片片段是按固定时长烘好的，要停更久必须按新时长重渲。

        源素材设成 2 秒：两段共 4 秒要铺满 8 秒旁白，每段得停 4 秒，
        比烘出来的片段还长，这才会走到重渲那条路。
        """
        with patch.object(scene_manifest, "material_kind", return_value="image"):
            with patch.object(timeline, "probe_duration", return_value=2.0):
                with patch.object(timeline, "ensure_image_length") as extend:
                    extend.side_effect = lambda task, material, needed: material
                    client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        assert extend.call_count >= 1

    def test_fitting_marks_the_task_as_having_timing_edits(self, client, reviewed):
        with patch.object(scene_manifest, "material_kind", return_value="video"):
            client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        listed = client.get(f"/api/v1/tasks/{reviewed}/timeline").json()["data"]
        assert listed["has_timing_edits"] is True

    def test_fitting_a_busy_task_is_409(self, client, reviewed):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
            response = client.post(f"/api/v1/tasks/{reviewed}/timeline/fit")
        assert response.status_code == 409


class TestAddScene:
    """在时间线末尾补一段新画面。

    素材张数由关键词个数决定，画面比旁白短时，"每段停得更久"和"多补几张"
    是两个不同的答案，这里是后者。
    """

    def test_a_generated_scene_is_appended(self, client, reviewed):
        new_file = str(utils.task_dir(reviewed) + "/added.png")
        with open(new_file, "wb") as handle:
            handle.write(b"x")
        with patch.object(
            scene_editor.material, "download_videos", return_value=[new_file]
        ) as download:
            response = client.post(
                f"/api/v1/tasks/{reviewed}/scenes",
                json={"keyword": "a hand dropping a coin into a jar"},
            )
        assert response.status_code == 200
        assert download.call_args.kwargs["search_terms"] == [
            "a hand dropping a coin into a jar"
        ]
        scenes = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()["data"]["scenes"]
        assert len(scenes) == 3
        assert scenes[2]["keyword"] == "a hand dropping a coin into a jar"
        assert scenes[2]["index"] == 2

    def test_the_new_scene_has_empty_narration(self, client, reviewed):
        """补画面不该顺手改旁白，否则会悄悄让已经录好的配音失效。"""
        new_file = str(utils.task_dir(reviewed) + "/added2.png")
        with open(new_file, "wb") as handle:
            handle.write(b"x")
        with patch.object(
            scene_editor.material, "download_videos", return_value=[new_file]
        ):
            client.post(f"/api/v1/tasks/{reviewed}/scenes", json={"keyword": "seeds"})
        listed = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()["data"]
        assert listed["scenes"][2]["narration"] == ""
        assert listed["audio_stale"] is False

    def test_after_inserts_in_the_middle(self, client, reviewed):
        new_file = str(utils.task_dir(reviewed) + "/added3.png")
        with open(new_file, "wb") as handle:
            handle.write(b"x")
        with patch.object(
            scene_editor.material, "download_videos", return_value=[new_file]
        ):
            client.post(
                f"/api/v1/tasks/{reviewed}/scenes",
                json={"keyword": "middle", "after": 0},
            )
        scenes = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()["data"]["scenes"]
        assert [s["keyword"] for s in scenes] == ["a", "middle", "b"]
        # index 必须重排成位置，否则后续按下标操作会错位。
        assert [s["index"] for s in scenes] == [0, 1, 2]

    def test_a_keywordless_request_is_rejected(self, client, reviewed):
        response = client.post(f"/api/v1/tasks/{reviewed}/scenes", json={})
        assert response.status_code == 400

    def test_a_blank_keyword_is_refused_by_name(self, client, reviewed):
        response = client.post(
            f"/api/v1/tasks/{reviewed}/scenes", json={"keyword": "   "}
        )
        assert response.status_code == 400
        assert "keyword" in response.json()["message"]

    def test_adding_to_a_busy_task_is_409(self, client, reviewed):
        with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
            response = client.post(
                f"/api/v1/tasks/{reviewed}/scenes", json={"keyword": "seeds"}
            )
        assert response.status_code == 409

    def test_a_provider_failure_is_named_and_adds_nothing(self, client, reviewed):
        with patch.object(
            scene_editor.material, "download_videos", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                f"/api/v1/tasks/{reviewed}/scenes", json={"keyword": "seeds"}
            )
        assert response.status_code == 400
        assert "openai_image" in response.json()["message"]
        scenes = client.get(f"/api/v1/tasks/{reviewed}/scenes").json()["data"]["scenes"]
        assert len(scenes) == 2
