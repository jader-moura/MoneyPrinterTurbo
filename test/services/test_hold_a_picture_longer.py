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

import os
from pathlib import Path
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
    """拖长一张图是**记一个数**，不是跑一次 ffmpeg。

    第一版是在这里就地重渲的，那是错的：一次重渲几秒起步，挂在拖拽上就是
    每拖一下卡几秒；连着拖几次还会有两次重渲同时在跑，互相盖掉对方的输出，
    场景被指到一个没写完的文件上，编辑器就打不开了——真的发生过。

    出片那一步（``build_edit_list``）本来就会按需要的长度把图片重渲一遍，
    所以这里放行就够了。
    """

    def test_the_longer_hold_is_recorded_and_nothing_is_rendered(
        self, task_root, idle_task
    ):
        task_id, material = a_task(task_root)
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(
                timeline, "source_image_for", return_value="a-still.png"
            ):
                with patch.object(timeline, "ensure_image_length") as render:
                    scene = scene_editor.update_timing(task_id, 0, duration=10.0)

        render.assert_not_called()
        assert scene["duration"] == pytest.approx(10.0)
        assert not scene.get("clamped")

    def test_the_scene_keeps_pointing_at_the_same_file(self, task_root, idle_task):
        """没有新文件，所以清单里的素材一个字都不该变。

        第一版在这里改了素材路径，而且用错了函数：``relative_media_path``
        算的是 ``/stream`` 用的 URL（相对任务根目录，带任务 id 前缀），而
        清单里的路径由 ``resolve_material`` 读，它在任务自己的目录里找。于
        是拖长一次就把这一段指到一个解析不出来的地方，之后每次打开都 400。
        """
        task_id, material = a_task(task_root)
        before = scene_manifest.load(task_id)["scenes"][0]["material"]
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(
                timeline, "source_image_for", return_value="a-still.png"
            ):
                scene_editor.update_timing(task_id, 0, duration=10.0)
        after = scene_manifest.load(task_id)["scenes"][0]["material"]
        assert after == before
        # 而且它还要解析得出来，这正是当时坏掉的地方。
        assert scene_manifest.resolve_material(task_id, after)

    def test_an_in_point_does_not_eat_into_the_hold(self, task_root, idle_task):
        task_id, _ = a_task(task_root)
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(
                timeline, "source_image_for", return_value="a-still.png"
            ):
                scene = scene_editor.update_timing(
                    task_id, 0, trim_start=2.0, duration=10.0
                )
        assert scene["duration"] == pytest.approx(10.0)
        assert not scene.get("clamped")

    def test_the_render_is_what_actually_extends_it(self, task_root, idle_task):
        """放行之后，长出来的那一段由出片那一步渲——这里钉住那一步真的会渲。"""
        task_id, material = a_task(task_root)
        scenes = [{"index": 0, "material": material, "duration": 10.0}]
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            with patch.object(
                timeline, "source_image_for", return_value="a-still.png"
            ):
                with patch.object(
                    timeline, "ensure_image_length", return_value="longer.mp4"
                ) as render:
                    timeline.build_edit_list(task_id, scenes)
        render.assert_called_once()
        assert render.call_args.args[2] == pytest.approx(10.0)


class TestTheDragHandleKnowsToo:
    """服务端放开了不够：拖拽把手在浏览器里就先夾了一次。

    浏览器没办法知道磁盘上还在不在那张原图，所以这件事必须由服务端
    告诉它。用文件名判断：64 位十六进制的摘要前缀就是"图片烘出来的"。
    """

    def test_a_clip_baked_from_a_picture_says_so(self):
        assert timeline.baked_from_an_image("a" * 64 + ".zoom-abc.mp4")

    @pytest.mark.parametrize(
        "name",
        [
            "pexels-12345.mp4",
            "shot.zoom-abc123.mp4",      # 前缀不是 64 位摘要
            ("z" * 64) + ".mp4",          # 64 位但不是十六进制
            "",
        ],
    )
    def test_everything_else_does_not(self, name):
        assert not timeline.baked_from_an_image(name)

    def test_it_never_opens_a_file(self, tmp_path):
        """列表接口每一段都要问一次，所以它不能碰磁盘。"""
        missing = str(tmp_path / ("b" * 64 + ".zoom-x.mp4"))
        assert not os.path.exists(missing)
        assert timeline.baked_from_an_image(missing)

    def test_the_scene_list_carries_the_flag(self, task_root, idle_task):
        task_id, _ = a_task(task_root, material=("c" * 64) + ".zoom-abc.mp4")
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            scenes = scene_editor.describe(task_id, with_timing=True)["scenes"]
        assert scenes[0]["can_hold_longer"] is True

    def test_a_downloaded_clip_is_flagged_as_fixed_length(self, task_root, idle_task):
        task_id, _ = a_task(task_root, material="downloaded.mp4")
        with patch.object(timeline, "probe_duration", return_value=BAKED):
            scenes = scene_editor.describe(task_id, with_timing=True)["scenes"]
        assert scenes[0]["can_hold_longer"] is False

    def test_the_editor_clamps_on_the_flag_not_on_the_kind(self):
        """``scene.kind`` 对一段 .zoom-*.mp4 来说就是 video。"""
        source = (
            Path(__file__).parents[2] / "resource" / "public" / "editor" / "editor.js"
        ).read_text(encoding="utf-8")
        assert "!scene.can_hold_longer" in source
        assert 'scene.kind === "video"' not in source


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
