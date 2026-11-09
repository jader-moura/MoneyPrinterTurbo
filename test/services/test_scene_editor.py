"""审片期间的单场景编辑：重生成、替换、改台词、重排、删除。"""

import os
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import material, scene_editor, scene_manifest, task_artifacts
from app.services import state as sm
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


def _material_file(task_root, task_id, name):
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"x")
    return str(target)


def _reviewed_task(task_root, task_id="review", source="openai_image", scenes=2):
    """造一个"已经跑到素材阶段、等待审片"的任务。"""
    params = VideoParams(
        video_subject="seeds",
        video_script="One. Two.",
        video_source=source,
        video_clip_duration=5,
    )
    task_artifacts.write_script_data(
        task_id,
        {
            "script": "One. Two.",
            "search_terms": ["a", "b"],
            "params": params.model_dump(),
        },
    )
    materials = [
        _material_file(task_root, task_id, f"{index + 1}.png") for index in range(scenes)
    ]
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script="One. Two.",
        video_terms=["a", "b"],
        materials=materials,
        audio_file=_material_file(task_root, task_id, "audio.mp3"),
        audio_duration=8.0,
        subtitle_path="",
        video_source=source,
        clip_duration=5,
    )
    return task_id, materials


@pytest.fixture(autouse=True)
def idle_task():
    """默认任务空闲；需要测"忙"的用例自行覆盖。"""
    with patch.object(sm.state, "get_task", return_value=None):
        yield


class TestLoadParams:
    def test_unknown_fields_in_an_old_manifest_are_dropped(self, task_root):
        task_artifacts.write_script_data(
            "old",
            {"params": {"video_subject": "x", "a_field_from_2023": True}},
        )
        assert scene_editor.load_params("old").video_subject == "x"

    def test_a_manifest_without_params_is_a_named_error(self, task_root):
        task_artifacts.write_script_data("noparams", {"script": "s"})
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.load_params("noparams")
        assert "no saved parameters" in str(error.value)


class TestBusyTask:
    def test_editing_a_running_task_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(sm.state, "get_task", return_value={"state": 1}):
            with patch.object(scene_editor.tm, "is_task_busy", return_value=True):
                with pytest.raises(scene_editor.SceneBusyError):
                    scene_editor.update_scene(task_id, 0, keyword="x")

    def test_busy_error_is_a_scene_error_so_callers_can_catch_one_type(self):
        assert issubclass(scene_editor.SceneBusyError, scene_editor.SceneError)


class TestUpdateScene:
    def test_editing_narration_marks_the_audio_stale(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="A new line.")
        manifest = scene_manifest.load(task_id)
        assert manifest["audio_stale"] is True
        assert manifest["scenes"][0]["narration"] == "A new line."

    def test_editing_only_the_keyword_leaves_the_audio_alone(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="a different keyword")
        assert scene_manifest.load(task_id)["audio_stale"] is False

    def test_rewriting_the_same_narration_is_not_a_change(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        current = scene_manifest.load(task_id)["scenes"][0]["narration"]
        scene_editor.update_scene(task_id, 0, narration=current)
        assert scene_manifest.load(task_id)["audio_stale"] is False

    def test_out_of_range_index_names_the_scene_count(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.update_scene(task_id, 9, keyword="x")
        assert "has 2 scenes" in str(error.value)

    def test_a_non_numeric_index_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError):
            scene_editor.update_scene(task_id, "first", keyword="x")


class TestRegenerateMaterial:
    def test_one_scene_is_regenerated_from_a_single_keyword(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        replacement = _material_file(task_root, task_id, "new.png")
        with patch.object(
            material, "download_videos", return_value=[replacement]
        ) as download:
            scene = scene_editor.regenerate_material(task_id, 0)
        # 单关键词、单片长：不能为一个场景重新规划一整批素材。
        assert download.call_args.kwargs["search_terms"] == ["a"]
        assert download.call_args.kwargs["audio_duration"] == 5.0
        assert download.call_args.kwargs["source"] == "openai_image"
        assert scene["material"] == replacement
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == replacement

    def test_a_regenerated_scene_is_locked(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        replacement = _material_file(task_root, task_id, "new.png")
        with patch.object(material, "download_videos", return_value=[replacement]):
            assert scene_editor.regenerate_material(task_id, 0)["locked"] is True

    def test_a_new_keyword_is_written_back(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        replacement = _material_file(task_root, task_id, "new.png")
        with patch.object(
            material, "download_videos", return_value=[replacement]
        ) as download:
            scene_editor.regenerate_material(task_id, 0, keyword="a farmer sowing")
        assert download.call_args.kwargs["search_terms"] == ["a farmer sowing"]
        assert scene_manifest.load(task_id)["scenes"][0]["keyword"] == "a farmer sowing"

    def test_other_scenes_are_untouched(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        replacement = _material_file(task_root, task_id, "new.png")
        with patch.object(material, "download_videos", return_value=[replacement]):
            scene_editor.regenerate_material(task_id, 0)
        assert scene_manifest.load(task_id)["scenes"][1]["material"] == materials[1]

    def test_a_provider_failure_names_the_provider(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            material, "download_videos", side_effect=RuntimeError("gateway down")
        ):
            with pytest.raises(scene_editor.SceneError) as error:
                scene_editor.regenerate_material(task_id, 0)
        assert "openai_image" in str(error.value)
        assert "gateway down" in str(error.value)

    def test_an_empty_result_is_not_silently_accepted(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        with patch.object(material, "download_videos", return_value=[]):
            with pytest.raises(scene_editor.SceneError):
                scene_editor.regenerate_material(task_id, 0)
        # 失败不能留下半条清单。
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == materials[0]

    def test_keywordless_sources_are_refused_with_advice(self, task_root):
        task_id, _ = _reviewed_task(task_root, task_id="localsrc", source="local")
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.regenerate_material(task_id, 0)
        assert "replace this scene" in str(error.value)

    def test_a_scene_without_a_keyword_asks_for_one(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, keyword="")
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.regenerate_material(task_id, 0)
        assert "no keyword" in str(error.value)


class TestReplaceMaterial:
    def test_an_uploaded_file_replaces_the_scene(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        mine = _material_file(task_root, task_id, "mine.mp4")
        scene = scene_editor.replace_material(task_id, 1, mine)
        assert scene["kind"] == "video"
        assert scene["locked"] is True
        assert scene_manifest.load(task_id)["scenes"][1]["material"] == os.path.realpath(
            mine
        )

    def test_a_file_from_the_uploads_directory_is_copied_into_the_task(
        self, task_root, tmp_path, monkeypatch
    ):
        """``/stream`` 只服务任务目录，所以替换素材必须进到任务目录里。"""
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        source = uploads / "mine.mp4"
        source.write_bytes(b"payload")
        monkeypatch.setattr(
            scene_manifest, "_uploaded_material_dir", lambda: str(uploads)
        )

        task_id, _ = _reviewed_task(task_root)
        scene = scene_editor.replace_material(task_id, 0, str(source))

        assert os.path.dirname(scene["material"]) == os.path.realpath(
            str(task_root / task_id)
        )
        assert open(scene["material"], "rb").read() == b"payload"
        # 原文件留在上传目录，供别的任务继续使用。
        assert source.exists()

    def test_adopting_does_not_overwrite_an_existing_task_file(
        self, task_root, tmp_path, monkeypatch
    ):
        uploads = tmp_path / "uploads"
        uploads.mkdir()
        source = uploads / "1.png"
        source.write_bytes(b"new")
        monkeypatch.setattr(
            scene_manifest, "_uploaded_material_dir", lambda: str(uploads)
        )

        task_id, materials = _reviewed_task(task_root)
        scene = scene_editor.replace_material(task_id, 0, str(source))

        assert scene["material"] != materials[0]
        assert open(materials[0], "rb").read() == b"x"

    def test_a_file_already_in_the_task_directory_is_not_copied(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        mine = _material_file(task_root, task_id, "mine.mp4")
        scene = scene_editor.replace_material(task_id, 0, mine)
        assert scene["material"] == os.path.realpath(mine)

    def test_a_file_outside_the_allowed_directories_is_refused(
        self, task_root, tmp_path
    ):
        task_id, _ = _reviewed_task(task_root)
        outside = tmp_path / "elsewhere.mp4"
        outside.write_bytes(b"x")
        with pytest.raises(scene_editor.SceneError):
            scene_editor.replace_material(task_id, 0, str(outside))


class TestReorderAndDelete:
    def test_reordering_rewrites_the_render_order(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert scene_manifest.materials_for_render(task_id) == [
            os.path.realpath(materials[1]),
            os.path.realpath(materials[0]),
        ]

    def test_reordering_does_not_invalidate_the_narration(self, task_root):
        """重排只换画面顺序。配音是一整条，顺序没变，字幕也还对得上。

        所以重排之后可以直接渲染，不强制重新配音——那会让"调一下画面顺序"
        这种便宜的修改变成一次完整的 TTS。

        台词也不跟着画面走：重排之后再重新配音，念的顺序还是脚本的顺序。
        这一条由 test_narration_follows_time.py 钉着。
        """
        task_id, _ = _reviewed_task(task_root)
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert scene_manifest.load(task_id)["audio_stale"] is False
        assert scene_editor.prepare_render(task_id) is not None

    def test_deleting_does_not_invalidate_the_narration_either(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.delete_scene(task_id, 0)
        assert scene_manifest.load(task_id)["audio_stale"] is False

    def test_a_partial_order_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.reorder_scenes(task_id, [1])
        assert "permutation" in str(error.value)

    def test_a_duplicated_index_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError):
            scene_editor.reorder_scenes(task_id, [0, 0])

    def test_deleting_removes_one_scene_and_keeps_the_file(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        remaining = scene_editor.delete_scene(task_id, 0)
        assert len(remaining) == 1
        assert remaining[0]["material"] == materials[1]
        # 磁盘上的图是上一次已经花掉的算力，删场景不删文件。
        assert os.path.exists(materials[0])

    def test_the_last_scene_cannot_be_deleted(self, task_root):
        task_id, _ = _reviewed_task(task_root, task_id="single", scenes=1)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.delete_scene(task_id, 0)
        assert "at least one scene" in str(error.value)


class TestPrepareRender:
    def test_a_stale_narration_blocks_the_render(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="changed")
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.prepare_render(task_id)
        assert "regenerate the audio" in str(error.value)

    def test_a_reviewed_task_is_ready_to_render(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        assert scene_editor.prepare_render(task_id).video_subject == "seeds"

    def test_a_missing_material_fails_before_the_task_is_queued(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        os.remove(materials[0])
        with pytest.raises(scene_editor.SceneError):
            scene_editor.prepare_render(task_id)

    def test_a_task_that_never_ran_cannot_render(self, task_root):
        task_artifacts.write_script_data("fresh", {"params": {"video_subject": "x"}})
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.prepare_render("fresh")
        assert "no reviewed scenes" in str(error.value)


class TestPrepareAudioRerun:
    def test_the_edited_lines_become_the_new_script(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="A brand new first line.")
        params = scene_editor.prepare_audio_rerun(task_id)
        assert params.video_script.startswith("A brand new first line.")
        # 清单里也要存，因为 start_from="audio" 时脚本是从清单读的。
        assert scene_manifest.load(task_id)["script"] == params.video_script

    def test_all_blank_narration_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="")
        scene_editor.update_scene(task_id, 1, narration="")
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.prepare_audio_rerun(task_id)
        assert "nothing to narrate" in str(error.value)


class TestDescribe:
    def test_the_read_model_exposes_basenames_and_stream_urls(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        described = scene_editor.describe(task_id, endpoint="http://host:8080")
        assert described["scenes"][0]["material"] == "1.png"
        assert described["scenes"][0]["url"].startswith(
            "http://host:8080/api/v1/stream/"
        )
        assert described["scenes"][0]["exists"] is True

    def test_a_missing_file_is_reported_rather_than_hidden(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        os.remove(materials[0])
        described = scene_editor.describe(task_id)
        assert described["scenes"][0]["exists"] is False
