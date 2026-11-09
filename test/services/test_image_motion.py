"""验证图片动态效果的兼容默认值、参数传递和真实渲染像素。"""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from moviepy import ImageClip, VideoFileClip
from PIL import Image, ImageDraw
from pydantic import ValidationError

import cli
from app.config import config
from app.models.schema import ImageMotion, MaterialInfo, TaskVideoRequest, VideoParams
from app.services import material, task, video


def test_old_requests_keep_zoom_and_invalid_motion_is_rejected():
    assert TaskVideoRequest(video_subject="old task").image_motion == ImageMotion.zoom
    assert TaskVideoRequest(video_subject="still", image_motion="static").image_motion == ImageMotion.static
    with pytest.raises(ValidationError, match="image_motion"):
        TaskVideoRequest(video_subject="bad", image_motion="unknown")


def test_api_motion_reaches_task_queue_and_rejects_invalid_input():
    from fastapi.testclient import TestClient
    from app import asgi
    from app.controllers.v1 import video as controller

    with (
        patch.dict(config.app, {"api_key": ""}),
        patch.object(controller.task_manager, "add_task") as enqueue,
        patch.object(controller.sm.state, "update_task"),
    ):
        client = TestClient(asgi.app)
        for value, expected in ((None, "zoom"), ("static", "static"), ("zoom", "zoom")):
            payload = {"video_subject": "test", "subtitle_enabled": False}
            if value is not None:
                payload["image_motion"] = value
            response = client.post("/api/v1/videos", json=payload)
            assert response.status_code == 200
            assert enqueue.call_args.kwargs["params"].image_motion == expected
        enqueue.reset_mock()
        response = client.post("/api/v1/videos", json={"video_subject": "test", "image_motion": "bad"})
        assert response.status_code == 400
        enqueue.assert_not_called()


def test_cli_motion_uses_saved_value_but_explicit_argument_wins():
    args = ["--video-subject", "test"]
    with patch.dict(config.ui, {}, clear=True):
        assert cli.build_video_params(cli.parse_args(args)).image_motion == ImageMotion.zoom
    with patch.dict(config.ui, {"image_motion": "static"}, clear=True):
        assert cli.build_video_params(cli.parse_args(args)).image_motion == ImageMotion.static
        explicit = cli.build_video_params(cli.parse_args(args + ["--image-motion", "zoom"]))
        assert explicit.image_motion == ImageMotion.zoom
    with patch.dict(config.ui, {"image_motion": "bad"}, clear=True):
        assert cli.build_video_params(cli.parse_args(args)).image_motion == ImageMotion.zoom
    with pytest.raises(SystemExit):
        cli.parse_args(args + ["--image-motion", "bad"])


@pytest.mark.parametrize("motion", [ImageMotion.static, ImageMotion.zoom])
def test_task_passes_motion_to_local_and_generated_images(motion):
    params = VideoParams(video_subject="test", video_source="local", image_motion=motion)
    with patch.object(video, "preprocess_video", return_value=[MaterialInfo(url="local.mp4")]) as preprocess:
        assert task.get_video_materials("motion-test", params, [], 2) == ["local.mp4"]
        assert preprocess.call_args.kwargs["image_motion"] == motion
    params.video_source = "openai_image"
    with patch.object(material, "download_videos", return_value=["generated.mp4"]) as download:
        assert task.get_video_materials("motion-test", params, ["scene"], 2) == ["generated.mp4"]
        assert download.call_args.kwargs["image_motion"] == motion


def test_generated_image_motion_reaches_renderer_without_extra_paid_requests(tmp_path):
    item = MaterialInfo(provider="openai_image", url=str(tmp_path / "scene.png"), duration=2)
    with (
        patch.object(material, "generate_images_openai", return_value=[item]) as generate,
        patch.object(video, "render_image_zoom_video", return_value="static.mp4") as render,
        patch.object(material, "_persist_material_sources"),
    ):
        paths = material.download_videos(
            "motion-test", ["first", "unused"], source="openai_image",
            audio_duration=2, max_clip_duration=2, image_motion="static",
        )
    assert paths == ["static.mp4"]
    generate.assert_called_once()
    assert render.call_args.kwargs["image_motion"] == "static"


def test_invalid_motion_is_rejected_before_paid_image_generation():
    with patch.object(material, "generate_images_openai") as generate:
        with pytest.raises(ValueError, match="ImageMotion"):
            material.download_videos(
                "motion-test", ["scene"], source="openai_image",
                audio_duration=2, image_motion="invalid",
            )
    generate.assert_not_called()


def _white_width(frame):
    _, x = np.where(frame.min(axis=2) > 220)
    return int(x.max() - x.min() + 1)


def _bright_centroid_x(frame):
    """画面里亮线的亚像素横向重心。"""
    weight = frame.astype(float).mean(axis=(0, 2))
    weight = np.clip(weight - weight.mean(), 0, None)
    return float((weight * np.arange(frame.shape[1])).sum() / weight.sum())


def test_the_zoom_moves_the_picture_one_way_only():
    """放大过程中画面不能抖。

    老写法是 ``clip.resized(lambda t: ...)``：每帧把画幅重算成
    ``int(w * z(t))``，合成时再按 ``int((W - w) / 2)`` 居中。画幅每长一个
    像素，中心就移动半个像素，截断会把相邻两帧算到同一个偏移上——于是亮线
    一边外移一边来回跳，89 帧里有 23 帧往反方向走，单帧最大 0.8 像素。

    一次缓慢放大里，画面上每一点都只能朝外走。这里量的就是"只朝一个方向
    走"：任何一帧的反向位移都是用户看到的抖动。
    """
    size, duration, fps = 256, 2.0, 30
    pixels = np.zeros((size, size, 3), dtype=np.uint8)
    pixels[:, :, 0] = 30
    pixels[:, 59:62] = 255
    source = ImageClip(pixels).with_duration(duration).with_position("center")
    zoomed = video._zoom_image_clip(source, duration)
    try:
        frames = [zoomed.get_frame(index / fps) for index in range(int(duration * fps))]
    finally:
        zoomed.close()
        source.close()

    assert {frame.shape for frame in frames} == {(size, size, 3)}
    positions = [_bright_centroid_x(frame) for frame in frames]
    steps = np.diff(positions)
    assert positions[-1] < positions[0] - 2, "the zoom did not magnify the picture"
    assert steps.max() < 1e-9, f"the picture jitters backwards by up to {steps.max():.3f}px"


def test_the_zoom_keeps_a_transparent_edge_within_range():
    """图片运镜用 BICUBIC 采样，它会在硬边两侧过冲。

    颜色帧是 uint8，Pillow 自己截断了；遮罩帧是浮点的，过冲出 [0, 1] 会在
    合成时把透明边缘变成一圈亮边或暗边。
    """
    pixels = np.zeros((64, 64, 4), dtype=np.uint8)
    pixels[:, :, :3] = 255
    pixels[20:44, 20:44, 3] = 255
    source = ImageClip(pixels).with_duration(1.0).with_position("center")
    zoomed = video._zoom_image_clip(source, 1.0)
    try:
        assert zoomed.mask is not None, "the alpha channel was dropped"
        for moment in (0.0, 0.25, 0.5, 0.99):
            mask = zoomed.mask.get_frame(moment)
            assert mask.min() >= 0.0, f"alpha undershoots to {mask.min():.3f} at {moment}s"
            assert mask.max() <= 1.0, f"alpha overshoots to {mask.max():.3f} at {moment}s"
    finally:
        zoomed.close()
        source.close()


def test_native_static_pixels_zoom_default_and_output_identity(tmp_path):
    source = tmp_path / "scene.png"
    image = Image.new("RGB", (256, 256), (220, 0, 0))
    ImageDraw.Draw(image).rectangle((80, 80, 175, 175), fill="white")
    image.save(source)
    default = video.render_image_zoom_video(str(source), 3)
    zoom_bytes = Path(default).read_bytes()
    still = video.render_image_zoom_video(str(source), 3, image_motion="static")
    assert still != default
    assert Path(default).read_bytes() == zoom_bytes
    with VideoFileClip(still, audio=False) as clip:
        first, last = clip.get_frame(0), clip.get_frame(2.5)
        assert clip.size == [256, 256]
        assert clip.duration == pytest.approx(3, abs=0.04)
        assert abs(_white_width(first) - _white_width(last)) <= 1
        assert np.abs(first.astype(float) - last).mean() < 1
    with VideoFileClip(default, audio=False) as clip:
        assert _white_width(clip.get_frame(2.5)) > _white_width(clip.get_frame(0)) + 4
    assert video.render_image_zoom_video(str(source), 3, image_motion="zoom") == default


def test_static_image_preserves_alpha_composition(tmp_path):
    source = tmp_path / "alpha.png"
    Image.new("RGBA", (64, 64), (240, 0, 0, 128)).save(source)
    output = video.render_image_zoom_video(str(source), 0.2, image_motion="static")
    with VideoFileClip(output, audio=False) as clip:
        pixel = clip.get_frame(0)[32, 32]
        assert 110 <= pixel[0] <= 130
        assert max(pixel[1:]) < 5


@pytest.mark.parametrize("motion", ["static", "zoom"])
def test_failed_motion_render_keeps_previous_output_and_removes_partial_file(tmp_path, motion):
    """一次中断的编码既不能毁掉上一段好片子，也不能留下半截临时文件。

    第二次请求必须换一个时长：同一张图、同样时长、同样运镜的输出是内容寻址
    的，已经渲好的那个文件就是正确答案，``render_image_zoom_video`` 会直接
    返回它而不再编码（时间线把十几段画面铺满旁白时，每段都要问一次，重编码
    一遍纯属白等）。换个时长才真的会走到编码那一步。
    """
    source = tmp_path / "scene.png"
    Image.new("RGB", (64, 64), "red").save(source)
    output = video.render_image_zoom_video(str(source), 0.2, image_motion=motion)
    completed = Path(output).read_bytes()

    def interrupted_write(_clip, filename, **_kwargs):
        Path(filename).write_bytes(b"partial video")
        raise RuntimeError("encoder interrupted")

    with patch.object(video.CompositeVideoClip, "write_videofile", interrupted_write):
        with pytest.raises(RuntimeError, match="encoder interrupted"):
            video.render_image_zoom_video(str(source), 0.3, image_motion=motion)
    assert Path(output).read_bytes() == completed
    assert not list(tmp_path.glob(".image-zoom-*.mp4"))


@pytest.mark.parametrize("motion", [ImageMotion.zoom, ImageMotion.static])
def test_an_identical_render_request_reuses_the_rendered_clip(tmp_path, motion):
    """同一张图、同样时长、同样运镜，第二次请求不该再编码一遍。

    输出名按内容寻址，所以磁盘上那个文件就是答案。时间线为每一段画面各问
    一次,一次七秒的重编码乘以十几段就是用户在渲染开始前白等的一分半钟。
    """
    source = tmp_path / "scene.png"
    Image.new("RGB", (64, 64), "red").save(source)
    first = video.render_image_zoom_video(str(source), 0.2, image_motion=motion)

    def must_not_encode(_clip, filename, **_kwargs):
        raise AssertionError("re-encoded a clip that was already rendered")

    with patch.object(video.CompositeVideoClip, "write_videofile", must_not_encode):
        second = video.render_image_zoom_video(str(source), 0.2, image_motion=motion)
    assert second == first


@pytest.mark.parametrize("motion", [ImageMotion.zoom, ImageMotion.static])
def test_an_unreadable_leftover_clip_is_rendered_again(tmp_path, motion):
    """上一次写到一半留下的坏文件不能被当成"已经渲好了"。

    文件在、能打开目录项，解码却是空的。把它交给渲染器只会让整条片子在拼接
    阶段才失败，所以这里重渲一遍。
    """
    source = tmp_path / "scene.png"
    Image.new("RGB", (64, 64), "red").save(source)
    rendered = video.render_image_zoom_video(str(source), 0.2, image_motion=motion)
    Path(rendered).write_bytes(b"not a video")

    again = video.render_image_zoom_video(str(source), 0.2, image_motion=motion)
    assert again == rendered
    with VideoFileClip(again) as clip:
        assert clip.duration > 0
