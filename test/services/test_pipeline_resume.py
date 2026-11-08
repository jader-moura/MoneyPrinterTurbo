"""``start_from``：审片之后恢复渲染，而不是把整条流水线再跑一遍。

这组用例要守住的核心保证只有两条：
1. 不传 ``start_from`` 时，行为与以前完全一致（每个阶段都执行）。
2. ``start_from="video"`` 时，渲染拿到的是审片后的场景顺序，并且不会再去
   调 LLM、TTS 或任何素材供应商。
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
    """跨平台发布与本组用例无关，固定关闭以免触碰真实配置。"""
    with patch.object(
        task.upload_post.upload_post_service, "is_configured", return_value=False
    ):
        yield


def _params(**overrides):
    defaults = dict(
        video_subject="seeds",
        video_script="One seed. Then a harvest.",
        video_terms=["a seed", "a harvest"],
        video_source="pexels",
        subtitle_enabled=False,
        bgm_type="",
        video_clip_duration=5,
    )
    defaults.update(overrides)
    return VideoParams(**defaults)


def _files(task_root, task_id, *names):
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    produced = []
    for name in names:
        target = directory / name
        target.write_bytes(b"x")
        produced.append(str(target))
    return produced


def _review(task_root, task_id, params=None, source="pexels"):
    """跑到素材阶段并停下，等待审片。返回生成的素材路径。"""
    params = params or _params(video_source=source)
    audio, clip_one, clip_two = _files(
        task_root, task_id, "audio.mp3", "1.mp4", "2.mp4"
    )
    with (
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_audio", return_value=(audio, 8.0, None)),
        patch.object(
            task, "get_video_materials", return_value=[clip_one, clip_two]
        ),
    ):
        result = task.start(task_id, params, stop_at="materials")
    assert result == {"materials": [clip_one, clip_two]}
    return audio, clip_one, clip_two


class TestReviewCheckpoint:
    def test_stopping_at_materials_records_a_scene_list(self, task_root):
        audio, clip_one, clip_two = _review(task_root, "checkpoint")
        manifest = scene_manifest.load("checkpoint")
        assert [scene["material"] for scene in manifest["scenes"]] == [
            clip_one,
            clip_two,
        ]
        assert [scene["keyword"] for scene in manifest["scenes"]] == [
            "a seed",
            "a harvest",
        ]
        # 断句沿用字幕用的那个切分函数，它会去掉行尾标点。
        assert manifest["scenes"][0]["narration"] == "One seed"
        assert manifest["audio_file"] == audio
        assert manifest["audio_duration"] == 8.0
        assert manifest["audio_stale"] is False

    def test_a_one_click_run_also_records_scenes_so_it_can_be_re_rendered(
        self, task_root
    ):
        """完整一次性生成也要留下场景清单，否则这次任务没法重新审片。"""
        task_id = "oneclick"
        audio, clip = _files(task_root, task_id, "audio.mp3", "1.mp4")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_audio", return_value=(audio, 4.0, None)),
            patch.object(task, "get_video_materials", return_value=[clip]),
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ),
        ):
            task.start(task_id, _params(), stop_at="video")
        assert len(scene_manifest.load(task_id)["scenes"]) == 1


class TestResumeRender:
    def test_render_uses_the_reviewed_order_and_calls_no_provider(self, task_root):
        """整个功能的核心断言。"""
        task_id = "resume"
        _audio, clip_one, clip_two = _review(task_root, task_id)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.reorder_scenes(task_id, [1, 0])

        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_script") as script,
            patch.object(task, "generate_terms") as terms,
            patch.object(task, "generate_audio") as audio_stage,
            patch.object(task, "get_video_materials") as materials,
            patch.object(task, "generate_subtitle") as subtitles,
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ) as render,
        ):
            result = task.start(
                task_id, _params(), stop_at="video", start_from="video"
            )

        assert result["videos"] == ["final.mp4"]
        # 渲染拿到的是重排后的顺序，不是上一轮的 materials。
        assert render.call_args.args[2] == [clip_two, clip_one]
        for stage in (script, terms, audio_stage, materials, subtitles):
            assert stage.call_count == 0

    def test_a_replaced_scene_reaches_the_renderer(self, task_root):
        task_id = "replaced"
        _audio, clip_one, _clip_two = _review(task_root, task_id)
        (mine,) = _files(task_root, task_id, "mine.mp4")
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.replace_material(task_id, 1, mine)

        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ) as render,
        ):
            task.start(task_id, _params(), stop_at="video", start_from="video")
        assert render.call_args.args[2] == [clip_one, mine]

    def test_a_deleted_scene_is_gone_from_the_render(self, task_root):
        task_id = "deleted"
        _audio, _clip_one, clip_two = _review(task_root, task_id)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.delete_scene(task_id, 0)

        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ) as render,
        ):
            task.start(task_id, _params(), stop_at="video", start_from="video")
        assert render.call_args.args[2] == [clip_two]

    def test_resume_does_not_need_the_material_provider_to_be_configured(
        self, task_root
    ):
        """审片后点渲染的用户可能早已把本地出图服务关掉了。"""
        task_id = "noprovider"
        params = _params(video_source="openai_image")
        _review(task_root, task_id, params=params)

        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(
                task.material, "is_openai_image_enabled", return_value=False
            ) as enabled,
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ),
        ):
            result = task.start(task_id, params, stop_at="video", start_from="video")
        assert result["videos"] == ["final.mp4"]
        assert enabled.call_count == 0

    def test_resume_does_not_require_llm_credentials(self, task_root):
        """脚本和关键词都从清单读回，所以恢复渲染不该坚持要一个可用的 LLM。

        这里断言的是 ``require_credentials=False``：配置解析本身仍然会发生
        (后面要用它来卸载 Ollama 模型腾显存)，但它不再能因为缺少 Key 而让
        这次渲染失败。
        """
        task_id = "nollm"
        _review(task_root, task_id)
        resolve = task.llm.resolve_llm_app_config
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(
                task.llm, "resolve_llm_app_config", side_effect=resolve
            ) as resolver,
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ),
        ):
            # 参数里既没有脚本也没有关键词：一次正常的生成会在这里去问 LLM。
            params = _params(video_script="", video_terms=None)
            result = task.start(task_id, params, stop_at="video", start_from="video")
        assert result["videos"] == ["final.mp4"]
        assert resolver.call_args.kwargs["require_credentials"] is False


class TestResumeRefusals:
    def test_an_unknown_start_from_is_named(self, task_root):
        result = task.start("badstage", _params(), start_from="rendering")
        assert result["failed_stage"] == "preflight"
        assert "start_from" in result["error"]

    def test_start_from_after_stop_at_is_refused(self, task_root):
        result = task.start(
            "backwards", _params(), stop_at="audio", start_from="materials"
        )
        assert result["failed_stage"] == "preflight"
        assert "after stop_at" in result["error"]

    def test_resuming_a_task_with_no_manifest_fails_at_the_script_stage(
        self, task_root
    ):
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True):
            result = task.start(
                "nomanifest", _params(), stop_at="video", start_from="video"
            )
        assert result["failed_stage"] == "script"
        assert "no saved script" in result["error"]

    def test_resuming_without_narration_on_disk_fails_at_the_audio_stage(
        self, task_root
    ):
        task_id = "noaudio"
        audio, clip = _files(task_root, task_id, "audio.mp3", "1.mp4")
        _review(task_root, task_id)
        import os

        os.remove(audio)
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True):
            result = task.start(
                task_id, _params(), stop_at="video", start_from="video"
            )
        assert result["failed_stage"] == "audio"
        assert "prepared narration audio" in result["error"]

    def test_a_stale_narration_refuses_to_render(self, task_root):
        task_id = "stale"
        _review(task_root, task_id)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.update_scene(task_id, 0, narration="a different line")

        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True):
            result = task.start(
                task_id, _params(), stop_at="video", start_from="video"
            )
        assert result["failed_stage"] == "audio"
        assert "regenerate the audio" in result["error"]

    def test_resuming_with_no_scenes_fails_at_the_materials_stage(self, task_root):
        task_id = "noscenes"
        audio, _clip = _files(task_root, task_id, "audio.mp3", "1.mp4")
        task_artifacts.write_script_data(
            task_id,
            {
                "script": "One seed.",
                "search_terms": ["a seed"],
                "params": _params().model_dump(),
                "audio_file": audio,
                "audio_duration": 4.0,
            },
        )
        with patch.object(task.utils, "check_ffmpeg_ready", return_value=True):
            result = task.start(
                task_id, _params(), stop_at="video", start_from="video"
            )
        assert result["failed_stage"] == "materials"
        assert "no reviewed scenes" in result["error"]


class TestRerecordNarration:
    def test_rerecording_clears_the_stale_flag_and_keeps_the_scenes(self, task_root):
        task_id = "rerecord"
        _review(task_root, task_id)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.update_scene(task_id, 0, narration="A brand new line.")
            params = scene_editor.prepare_audio_rerun(task_id)
        assert scene_manifest.load(task_id)["audio_stale"] is True

        new_audio, = _files(task_root, task_id, "audio-2.mp3")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_audio", return_value=(new_audio, 9.0, object())),
            patch.object(task, "generate_subtitle", return_value="subtitle.srt"),
        ):
            task.start(task_id, params, stop_at="subtitle", start_from="audio")

        manifest = scene_manifest.load(task_id)
        assert manifest["audio_stale"] is False
        assert manifest["audio_file"] == new_audio
        # 重新配音不能把审片过的场景列表弄丢。
        assert len(manifest["scenes"]) == 2

    def test_the_render_is_unblocked_after_rerecording(self, task_root):
        task_id = "unblocked"
        _review(task_root, task_id)
        with patch.object(scene_editor.sm.state, "get_task", return_value=None):
            scene_editor.update_scene(task_id, 0, narration="A brand new line.")
            params = scene_editor.prepare_audio_rerun(task_id)

        new_audio, = _files(task_root, task_id, "audio-2.mp3")
        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(task, "generate_audio", return_value=(new_audio, 9.0, object())),
            patch.object(task, "generate_subtitle", return_value="subtitle.srt"),
        ):
            task.start(task_id, params, stop_at="subtitle", start_from="audio")

        with (
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(
                task, "generate_final_videos", return_value=(["final.mp4"], [], None)
            ) as render,
        ):
            result = task.start(task_id, params, stop_at="video", start_from="video")
        assert result["videos"] == ["final.mp4"]
        # 渲染用的是新配音，不是审片前那一条。
        assert render.call_args.args[3] == new_audio
