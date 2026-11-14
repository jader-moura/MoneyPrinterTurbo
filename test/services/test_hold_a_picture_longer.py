# -*- coding: utf-8 -*-
"""一张图在编辑器里想停多久就停多久。

用户的原话："the user must be able to increase or decrease the scene/image
duration for how long he wants, at the moment it has a limit, the limit is
related to the 'Maximum Clip Duration (seconds)' this information is super
important in how the way the app creates the scenes at the first time, but
later in the edition the user must be able to do whatever he wants"。

出片的时候每段画面多长，是按 ``max_clip_duration`` 决定的——那条规则在那一
步是对的。但生成的图片在时间线上是一段 ``.zoom-*.mp4``（推拉镜头烘在文件
里），而 ``material_kind`` 按后缀把它判成 video，于是"要停更久就重渲一段"
那条路根本走不到，一张图就被钉死在出片时烘出来的那个长度上。

真·视频（网上下回来的素材）仍然停不过它本身的长度——那是物理，不是策略，
而且会照旧报 ``clamped``。
"""

from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task_artifacts, timeline

from test.services.test_scene_editor import idle_task, task_root  # noqa: F401

BAKED = 4.0


def a_task(task_root, material="shot.zoom-abc123.mp4"):
    """一个等着审的任务，唯一那段画面是出片时烘出来的 4 秒片段。"""
    task_id = "holding"
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / material
    path.write_bytes(b"not really a video")
    params = VideoParams(
        video_subject="seeds",
        video_script="One.",
        video_source="openai_image",
        video_clip_duration=5,
    )
    task_artifacts.write_script_data(
        task_id,
        {"script": "One.", "search_terms": ["a"], "params": params.model_dump()},
    )
    audio = directory / "audio.mp3"
    audio.write_bytes(b"not really audio")
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script="One.",
        video_terms=["a"],
        materials=[str(path)],
        audio_file=str(audio),
        audio_duration=8.0,
        subtitle_path="",
        video_source="openai_image",
        clip_duration=5,
    )
    return task_id, str(path)


class TestAPictureBakedAtFiveSecondsCanStillBeHeldLonger:
    def test_it_re_renders_instead_of_clamping(self, task_root, idle_task):
        """这就是那个限制：后缀是 .mp4，所以重渲那条路从来没走到过。"""
        task_id, material = a_task(task_root)
        longer = material.replace(".mp4", "-long.mp4")
        # 清单会把磁盘上不存在的素材丢掉，所以这段"重渲出来的"也要真的在。
        open(longer, "wb").write(b"a longer clip")
        lengths = {material: BAKED, longer: 12.0}

        with patch.object(
            timeline, "probe_duration", side_effect=lambda p: lengths.get(str(p), BAKED)
        ):
            with patch.object(
                timeline, "ensure_image_length", return_value=longer
            ) as extend:
                scene = scene_editor.update_timing(task_id, 0, duration=10.0)

        extend.assert_called_once()
        assert extend.call_args.args[2] == pytest.approx(10.0)
        assert scene["duration"] == pytest.approx(10.0)
        assert not scene.get("clamped")

    def test_the_scene_now_points_at_the_longer_clip(self, task_root, idle_task):
        task_id, material = a_task(task_root)
        longer = material.replace(".mp4", "-long.mp4")
        # 清单会把磁盘上不存在的素材丢掉，所以这段"重渲出来的"也要真的在。
        open(longer, "wb").write(b"a longer clip")
        lengths = {material: BAKED, longer: 12.0}

        with patch.object(
            timeline, "probe_duration", side_effect=lambda p: lengths.get(str(p), BAKED)
        ):
            with patch.object(timeline, "ensure_image_length", return_value=longer):
                scene_editor.update_timing(task_id, 0, duration=10.0)

        # 清单存的是相对路径，比的是"指向哪个文件"。
        stored = scene_manifest.load(task_id)["scenes"][0]["material"]
        assert stored.endswith("shot.zoom-abc123-long.mp4")

    def test_an_in_point_counts_towards_what_is_needed(self, task_root, idle_task):
        """从第 2 秒开始停 10 秒，要的是 12 秒素材，不是 10 秒。"""
        task_id, material = a_task(task_root)
        longer = material.replace(".mp4", "-long.mp4")
        # 清单会把磁盘上不存在的素材丢掉，所以这段"重渲出来的"也要真的在。
        open(longer, "wb").write(b"a longer clip")
        lengths = {material: BAKED, longer: 20.0}

        with patch.object(
            timeline, "probe_duration", side_effect=lambda p: lengths.get(str(p), BAKED)
        ):
            with patch.object(
                timeline, "ensure_image_length", return_value=longer
            ) as extend:
                scene_editor.update_timing(task_id, 0, trim_start=2.0, duration=10.0)

        assert extend.call_args.args[2] == pytest.approx(12.0)


class TestWhatStaysTheSame:
    def test_a_real_video_still_cannot_outlast_itself(self, task_root, idle_task):
        """网上下回来的片子停不过它自己的长度，而且照旧说出来。

        ``ensure_image_length`` 找不到原图就原样返回，这里就是那条路。
        """
        task_id, material = a_task(task_root, material="downloaded.mp4")

        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(
                timeline, "ensure_image_length", return_value=material
            ):
                scene = scene_editor.update_timing(task_id, 0, duration=10.0)

        assert scene["duration"] == pytest.approx(BAKED)
        assert scene["clamped"]

    def test_making_it_shorter_never_re_renders(self, task_root, idle_task):
        """缩短不需要新素材，碰都不该碰出图那一步。"""
        task_id, _ = a_task(task_root)

        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(timeline, "ensure_image_length") as extend:
                scene = scene_editor.update_timing(task_id, 0, duration=1.5)

        extend.assert_not_called()
        assert scene["duration"] == pytest.approx(1.5)

    def test_asking_for_exactly_what_is_there_re_renders_nothing(
        self, task_root, idle_task
    ):
        task_id, _ = a_task(task_root)

        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(timeline, "ensure_image_length") as extend:
                scene = scene_editor.update_timing(task_id, 0, duration=BAKED)

        extend.assert_not_called()
        assert scene["duration"] == pytest.approx(BAKED)

    def test_the_floor_is_still_a_floor(self, task_root, idle_task):
        """0.2 秒以下没有意义，这一条不是"上限"那回事。"""
        task_id, _ = a_task(task_root)

        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with pytest.raises(Exception):
                scene_editor.update_timing(task_id, 0, duration=0.05)
