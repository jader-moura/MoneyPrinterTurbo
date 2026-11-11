"""素材阶段半路死掉之后，这条任务还能被接着跑完。

Why these tests exist
---------------------
恢复渲染需要的东西——配音、字幕、脚本——原来只在素材阶段**全部成功之后**由
``record_prepared_artifacts`` 一次性写进清单。文生图一张一张买，二十九张之后
连接被掐断，于是：

* 清单里只有 ``material_sources``，没有配音、没有字幕、没有场景；
* 界面上这条任务是 "0 scenes"，点开一片空白；
* ``start_from="materials"`` 也起不来，因为 ``_resumed_audio`` 读不到配音；
* 用户唯一的出路是从脚本整条重来，把那二十九张重新买一遍。

那二十九张的钱已经花了，文件也在盘上。这组用例钉住的是："花掉的算力留在
盘上，并且清单里有足够的东西让下一趟接着跑"。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task, task_artifacts
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
        video_source="pexels",
        subtitle_enabled=False,
        bgm_type="",
        video_clip_duration=3,
    )
    defaults.update(overrides)
    return VideoParams(**defaults)


def _write(task_root, task_id, *names):
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    produced = []
    for name in names:
        target = directory / name
        target.write_bytes(b"x")
        produced.append(str(target))
    return produced


def _died_in_materials(task_root, task_id, *, params=None):
    """素材阶段抬手就失败的一趟，模拟买到第 29 张时被掐断。"""
    params = params or _params()
    (audio,) = _write(task_root, task_id, "audio.mp3")
    with (
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_audio", return_value=(audio, 30.0, None)),
        patch.object(task, "get_video_materials", return_value=[]),
    ):
        result = task.start(task_id, params, stop_at="materials")
    assert result["failed_stage"] == "materials"
    return audio


class TestWhatSurvivesTheFailure:
    def test_the_narration_is_in_the_manifest_even_though_materials_failed(
        self, task_root
    ):
        audio = _died_in_materials(task_root, "died")
        manifest = scene_manifest.load("died")
        assert manifest["audio_file"] == audio
        assert manifest["audio_duration"] == 30.0

    def test_the_script_is_in_the_manifest(self, task_root):
        _died_in_materials(task_root, "died")
        assert "Clocks ran late" in scene_manifest.load("died")["script"]

    def test_no_scenes_are_claimed(self, task_root):
        """场景清单还是不能写：画面没出齐，这条任务不该看起来可以渲了。"""
        _died_in_materials(task_root, "died")
        assert scene_manifest.load("died")["scenes"] == []

    def test_the_subtitle_is_recorded_when_there_is_one(self, task_root):
        task_id = "withsubs"
        audio, subtitle = _write(task_root, task_id, "audio.mp3", "subtitle.srt")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_audio", return_value=(audio, 30.0, None)),
            patch.object(task, "generate_subtitle", return_value=subtitle),
            patch.object(task, "get_video_materials", return_value=[]),
        ):
            task.start(task_id, _params(subtitle_enabled=True), stop_at="materials")
        assert scene_manifest.load(task_id)["subtitle_path"] == subtitle


class TestPickingItUpAgain:
    def test_the_materials_stage_runs_again_without_redoing_anything_earlier(
        self, task_root
    ):
        """接着跑只跑素材：不重写脚本、不重出关键词、不重配音。"""
        task_id = "pickup"
        _died_in_materials(task_root, task_id)
        (clip,) = _write(task_root, task_id, "1.mp4")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_script") as script,
            patch.object(task, "generate_terms") as terms,
            patch.object(task, "generate_audio") as audio,
            patch.object(task, "get_video_materials", return_value=[clip]),
        ):
            result = task.start(
                task_id, _params(), stop_at="materials", start_from="materials"
            )
        script.assert_not_called()
        terms.assert_not_called()
        audio.assert_not_called()
        assert result == {"materials": [clip]}

    def test_the_second_run_records_the_scenes(self, task_root):
        task_id = "pickup2"
        _died_in_materials(task_root, task_id)
        (clip,) = _write(task_root, task_id, "1.mp4")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "get_video_materials", return_value=[clip]),
        ):
            task.start(task_id, _params(), stop_at="materials", start_from="materials")
        assert len(scene_manifest.load(task_id)["scenes"]) == 1

    def test_the_keywords_from_the_first_run_are_reused(self, task_root):
        """关键词从清单读回来，不再问一次模型——问一次就换成另一批画面了。"""
        task_id = "keywords"
        _died_in_materials(task_root, task_id)
        (clip,) = _write(task_root, task_id, "1.mp4")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "get_video_materials", return_value=[clip]) as fetch,
        ):
            task.start(task_id, _params(), stop_at="materials", start_from="materials")
        assert fetch.call_args[0][2] == ["a clock", "a kitchen"]


class TestResumingWithNothingRecorded:
    """清单里连配音都没写的老任务（这个修复之前失败的那些）也要能接着跑。"""

    def test_the_narration_on_disk_is_used_when_the_manifest_has_none(
        self, task_root
    ):
        task_id = "legacy"
        (_audio, clip) = _write(task_root, task_id, "audio.mp3", "1.mp4")
        # 修复之前那条任务的清单长这样：只有脚本、关键词和素材来源。
        task_artifacts.write_script_data(
            task_id,
            {
                "script": "Clocks ran late.",
                "search_terms": ["a clock"],
                "material_sources": [
                    {"provider": "openai_image", "local_file": "1.mp4", "duration": 3}
                ],
            },
        )
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "get_video_materials", return_value=[clip]),
            patch.object(task.voice, "get_audio_duration", return_value=30.0),
        ):
            result = task.start(
                task_id, _params(), stop_at="materials", start_from="materials"
            )
        assert result == {"materials": [clip]}


class TestTellingTheUserItCanBePickedUp:
    """界面要能看出这条任务画面出了一半，并且能一键接着跑。"""

    def test_an_unfinished_task_reports_how_far_it_got(self, task_root):
        task_id = "halfway"
        _died_in_materials(task_root, task_id)
        task_artifacts.patch_script_data(
            task_id,
            material_sources=[
                {"provider": "openai_image", "local_file": f"{i}.mp4", "duration": 3}
                for i in range(7)
            ],
        )
        state = scene_editor.unfinished_materials(task_id)
        assert state["resumable"] is True
        assert state["pictures"] == 7
        assert state["needed"] == 10  # 30 秒旁白、每段 3 秒
        assert state["narration"] == 30.0

    def test_a_reviewable_task_is_not_offered_as_unfinished(self, task_root):
        task_id = "fine"
        _died_in_materials(task_root, task_id)
        (clip,) = _write(task_root, task_id, "1.mp4")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "get_video_materials", return_value=[clip]),
        ):
            task.start(task_id, _params(), stop_at="materials", start_from="materials")
        assert scene_editor.unfinished_materials(task_id)["resumable"] is False

    def test_a_task_with_no_pictures_at_all_is_not_offered(self, task_root):
        _died_in_materials(task_root, "nothing")
        assert scene_editor.unfinished_materials("nothing")["resumable"] is False

    def test_a_task_with_no_narration_is_not_offered(self, task_root):
        task_id = "nonarration"
        task_artifacts.write_script_data(
            task_id,
            {
                "script": "Clocks ran late.",
                "search_terms": ["a clock"],
                "material_sources": [
                    {"provider": "openai_image", "local_file": "1.mp4"}
                ],
            },
        )
        assert scene_editor.unfinished_materials(task_id)["resumable"] is False

    def test_preparing_a_rerun_refuses_a_task_with_nothing_to_pick_up(
        self, task_root
    ):
        _died_in_materials(task_root, "empty")
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            with pytest.raises(scene_editor.SceneError) as caught:
                scene_editor.prepare_materials_rerun("empty")
        assert "nothing to pick up" in str(caught.value)
