"""场景清单的结构、配对规则和路径边界。"""

import json
import os

import pytest

from app.services import scene_manifest, task_artifacts
from app.utils import utils


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    """把任务目录指向临时目录，避免测试写进真实 storage/tasks。"""
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


def _material(task_root, task_id, name):
    path = task_root / task_id
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_bytes(b"x")
    return str(target)


class TestStageOrder:
    def test_stage_order_matches_the_pipeline(self):
        assert scene_manifest.PIPELINE_STAGES == (
            "script",
            "terms",
            "audio",
            "subtitle",
            "materials",
            "video",
        )

    def test_default_start_runs_every_stage(self):
        for stage in scene_manifest.PIPELINE_STAGES:
            assert scene_manifest.runs_stage(stage, "script") is True

    def test_resuming_at_video_skips_everything_before_it(self):
        for stage in ("script", "terms", "audio", "subtitle", "materials"):
            assert scene_manifest.runs_stage(stage, "video") is False
        assert scene_manifest.runs_stage("video", "video") is True

    def test_unknown_stage_is_named_in_the_error(self):
        with pytest.raises(scene_manifest.SceneError) as error:
            scene_manifest.stage_index("render", field="start_from")
        assert "start_from" in str(error.value)
        assert "render" in str(error.value)

    def test_empty_stage_is_rejected_rather_than_defaulted(self):
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.stage_index("")


class TestNarrationSplitting:
    def test_lines_match_the_subtitle_splitter(self):
        script = "One seed. Then a harvest. Finally a field."
        assert scene_manifest.split_narration(script) == [
            line.strip()
            for line in utils.split_string_by_punctuations(script)
            if line.strip()
        ]

    def test_blank_script_yields_no_lines(self):
        assert scene_manifest.split_narration("   ") == []
        assert scene_manifest.split_narration(None) == []


class TestTermNormalisation:
    def test_comma_separated_string_is_accepted(self):
        assert scene_manifest.normalise_terms("a, b ,c") == ["a", "b", "c"]

    def test_list_is_accepted_and_blanks_dropped(self):
        assert scene_manifest.normalise_terms(["a", "", "  ", "b"]) == ["a", "b"]

    def test_other_types_yield_nothing(self):
        assert scene_manifest.normalise_terms(None) == []
        assert scene_manifest.normalise_terms(7) == []


class TestBuildScenes:
    def test_scene_count_follows_the_materials(self):
        scenes = scene_manifest.build_scenes(
            video_script="One. Two.",
            video_terms=["a", "b", "c"],
            materials=["/t/1.png", "/t/2.png"],
        )
        assert [scene["index"] for scene in scenes] == [0, 1]
        assert [scene["keyword"] for scene in scenes] == ["a", "b"]

    def test_surplus_material_gets_an_empty_keyword_not_a_recycled_one(self):
        """空关键词在界面上是看得见的提示；错位的关键词会误导用户。"""
        scenes = scene_manifest.build_scenes(
            video_script="Only one line.",
            video_terms=["a"],
            materials=["/t/1.png", "/t/2.png"],
        )
        assert scenes[1]["keyword"] == ""
        assert scenes[1]["narration"] == ""

    def test_kind_comes_from_the_extension(self):
        scenes = scene_manifest.build_scenes(
            video_script="",
            video_terms=[],
            materials=["/t/a.png", "/t/b.mp4", "/t/c.xyz"],
        )
        assert [scene["kind"] for scene in scenes] == ["image", "video", "unknown"]

    def test_blank_material_entries_are_dropped(self):
        scenes = scene_manifest.build_scenes(
            video_script="", video_terms=[], materials=["/t/a.png", "", None]
        )
        assert len(scenes) == 1

    def test_clip_duration_is_the_requested_value_or_none(self):
        assert (
            scene_manifest.build_scenes(
                video_script="", video_terms=[], materials=["/t/a.png"], clip_duration=5
            )[0]["clip_duration"]
            == 5.0
        )
        assert (
            scene_manifest.build_scenes(
                video_script="", video_terms=[], materials=["/t/a.png"], clip_duration=0
            )[0]["clip_duration"]
            is None
        )


class TestPersistence:
    def test_save_renumbers_indexes_to_match_position(self, task_root):
        task_id = "renumber"
        task_artifacts.write_script_data(task_id, {"script": "s"})
        scene_manifest.save_scenes(
            task_id,
            [
                {"index": 9, "material": "/t/b.png"},
                {"index": 4, "material": "/t/a.png"},
            ],
        )
        assert [s["index"] for s in scene_manifest.load(task_id)["scenes"]] == [0, 1]

    def test_load_without_a_manifest_is_empty_not_an_error(self, task_root):
        assert scene_manifest.load("never-ran")["scenes"] == []

    def test_corrupt_manifest_is_ignored(self, task_root):
        task_id = "corrupt"
        directory = task_root / task_id
        directory.mkdir(parents=True)
        (directory / "script.json").write_text("{not json", encoding="utf-8")
        assert scene_manifest.load(task_id)["scenes"] == []

    def test_recording_artifacts_keeps_existing_fields(self, task_root):
        task_id = "record"
        task_artifacts.write_script_data(task_id, {"script": "s", "params": {"a": 1}})
        scene_manifest.record_prepared_artifacts(
            task_id,
            video_script="One. Two.",
            video_terms=["a", "b"],
            materials=[_material(task_root, task_id, "1.png")],
            audio_file="audio.mp3",
            audio_duration=12.5,
            subtitle_path="subtitle.srt",
            video_source="openai_image",
            clip_duration=5,
        )
        payload = json.loads((task_root / task_id / "script.json").read_text("utf-8"))
        assert payload["params"] == {"a": 1}
        assert payload["audio_duration"] == 12.5
        assert payload["audio_stale"] is False
        assert len(payload["scenes"]) == 1

    def test_rerecording_audio_clears_the_stale_flag(self, task_root):
        task_id = "rerecord"
        task_artifacts.write_script_data(task_id, {"script": "s", "audio_stale": True})
        scene_manifest.record_audio_artifacts(
            task_id,
            audio_file="new.mp3",
            audio_duration=3,
            subtitle_path="new.srt",
            script="edited",
        )
        manifest = scene_manifest.load(task_id)
        assert manifest["audio_stale"] is False
        assert manifest["script"] == "edited"


class TestPathBoundary:
    def test_material_outside_the_task_directory_is_refused(self, task_root, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"x")
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.resolve_material("boundary", str(outside))

    def test_traversal_is_refused(self, task_root):
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.resolve_material("boundary", "../../etc/passwd")

    def test_missing_file_is_refused(self, task_root):
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.resolve_material("boundary", "absent.png")

    def test_empty_path_is_refused(self, task_root):
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.resolve_material("boundary", "")

    def test_file_inside_the_task_directory_resolves(self, task_root):
        path = _material(task_root, "boundary", "1.png")
        assert scene_manifest.resolve_material("boundary", path) == os.path.realpath(path)

    def test_relative_media_path_is_blank_when_unresolvable(self, task_root):
        assert scene_manifest.relative_media_path("boundary", "absent.png") == ""


class TestMaterialsForRender:
    def test_render_reads_the_edited_order_not_the_original(self, task_root):
        """这条是整个功能的要点：渲染必须看到用户重排后的顺序。"""
        task_id = "order"
        first = _material(task_root, task_id, "1.png")
        second = _material(task_root, task_id, "2.png")
        task_artifacts.write_script_data(task_id, {"script": "s"})
        scene_manifest.save_scenes(
            task_id, [{"material": second}, {"material": first}]
        )
        assert scene_manifest.materials_for_render(task_id) == [
            os.path.realpath(second),
            os.path.realpath(first),
        ]

    def test_no_scenes_is_a_named_error(self, task_root):
        with pytest.raises(scene_manifest.SceneError) as error:
            scene_manifest.materials_for_render("empty")
        assert "no reviewed scenes" in str(error.value)

    def test_a_deleted_material_file_fails_loudly(self, task_root):
        task_id = "vanished"
        path = _material(task_root, task_id, "1.png")
        task_artifacts.write_script_data(task_id, {"script": "s"})
        scene_manifest.save_scenes(task_id, [{"material": path}])
        os.remove(path)
        with pytest.raises(scene_manifest.SceneError):
            scene_manifest.materials_for_render(task_id)


class TestNarrationScript:
    def test_edited_lines_are_joined_back_into_a_script(self, task_root):
        task_id = "narration"
        task_artifacts.write_script_data(task_id, {"script": "old"})
        scene_manifest.save_scenes(
            task_id,
            [
                {"material": "/t/1.png", "narration": "First line."},
                {"material": "/t/2.png", "narration": ""},
                {"material": "/t/3.png", "narration": "Third line."},
            ],
        )
        assert scene_manifest.narration_script(task_id) == "First line.\nThird line."
