"""渲染器的剪辑表入口：真的按入出点出片，而且不给剪辑表时行为不变。

这里用 ffmpeg 现生成几段纯色短片再真渲一次，而不是打桩。剪辑表的全部意义
就是"成片长度和画面顺序必须等于用户排的那条时间线"，这件事只有真渲过并量过
输出时长才算验证过。
"""

import subprocess
import wave

import pytest

from app.models.schema import VideoAspect, VideoConcatMode
from app.services import video


def _probe_duration(path) -> float:
    binary = video.utils.get_ffmpeg_binary().replace("ffmpeg", "ffprobe")
    result = subprocess.run(
        [binary, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """三段 3 秒纯色片 + 一段 6 秒静音，尺寸压到 64x64 让真渲足够快。"""
    binary = video.utils.get_ffmpeg_binary()
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\0\0" * 24000 * 6)

    sources = []
    for color in ("red", "green", "blue"):
        source = tmp_path / f"{color}.mp4"
        subprocess.run(
            [binary, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=10",
             "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
            check=True, capture_output=True,
        )
        sources.append(source)

    monkeypatch.setattr(VideoAspect, "to_resolution", lambda self: (64, 64))
    monkeypatch.setattr(video, "_get_configured_video_codec", lambda: "libx264")
    monkeypatch.setattr(video, "_get_effective_video_codec", lambda *args: "libx264")
    return tmp_path, sources, audio


class TestEditListRendering:
    def test_the_output_length_is_the_sum_of_the_trimmed_clips(self, workspace):
        """这是整个功能的核心断言：成片长度等于用户排的时间线，不是旁白长度。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "out.mp4"
        edit_list = [
            {"path": str(sources[0]), "start": 0.0, "end": 1.0},
            {"path": str(sources[1]), "start": 0.5, "end": 1.5},
        ]
        video.combine_videos(
            str(output), [str(source) for source in sources], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            edit_list=edit_list,
        )
        assert output.exists()
        # 1.0s + 1.0s；容差留给关键帧对齐和容器开销。
        assert _probe_duration(output) == pytest.approx(2.0, abs=0.35)

    def test_a_timeline_longer_than_the_narration_is_not_cut_back(self, workspace):
        """自动模式按旁白收尾；时间线模式下长度由用户决定，不能再按旁白截。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "long.mp4"
        edit_list = [
            {"path": str(source), "start": 0.0, "end": 3.0} for source in sources
        ]
        video.combine_videos(
            str(output), [str(source) for source in sources], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            edit_list=edit_list,
        )
        # 旁白只有 6 秒，剪辑表是 9 秒。
        assert _probe_duration(output) == pytest.approx(9.0, abs=0.5)

    def test_the_clip_order_follows_the_edit_list(self, workspace):
        """重排过的时间线必须真的改变画面顺序，否则拖动只是个摆设。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "ordered.mp4"
        seen = []
        real_open = video._open_video_clip_quietly

        def record(path, *args, **kwargs):
            seen.append(str(path))
            return real_open(path, *args, **kwargs)

        edit_list = [
            {"path": str(sources[2]), "start": 0.0, "end": 0.6},
            {"path": str(sources[0]), "start": 0.0, "end": 0.6},
        ]
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(video, "_open_video_clip_quietly", record)
            video.combine_videos(
                str(output), [str(source) for source in sources], str(audio),
                video_concat_mode=VideoConcatMode.sequential, threads=1,
                edit_list=edit_list,
            )
        # 蓝色在红色之前被打开，说明渲染顺序就是剪辑表顺序。
        blue_at = next(i for i, path in enumerate(seen) if "blue" in path)
        red_at = next(i for i, path in enumerate(seen) if "red" in path)
        assert blue_at < red_at

    def test_a_clip_longer_than_max_clip_duration_is_not_truncated(self, workspace):
        """用户把一段拉到 3 秒，就不能再被 max_clip_duration 悄悄切回 1 秒。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "long-clip.mp4"
        video.combine_videos(
            str(output), [str(sources[0])], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            max_clip_duration=1,
            edit_list=[{"path": str(sources[0]), "start": 0.0, "end": 3.0}],
        )
        assert _probe_duration(output) == pytest.approx(3.0, abs=0.35)

    def test_clips_are_not_looped_to_fill_the_narration(self, workspace):
        """时间线比旁白短是用户的选择，不替他循环补片。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "short.mp4"
        video.combine_videos(
            str(output), [str(sources[0])], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            edit_list=[{"path": str(sources[0]), "start": 0.0, "end": 1.0}],
        )
        assert _probe_duration(output) == pytest.approx(1.0, abs=0.35)


class TestEditListValidation:
    def test_an_empty_range_is_refused_by_name(self, workspace):
        tmp_path, sources, audio = workspace
        with pytest.raises(ValueError, match="empty range"):
            video.combine_videos(
                str(tmp_path / "bad.mp4"), [str(sources[0])], str(audio),
                threads=1,
                edit_list=[{"path": str(sources[0]), "start": 2.0, "end": 1.0}],
            )

    def test_a_missing_path_is_refused_by_name(self, workspace):
        tmp_path, sources, audio = workspace
        with pytest.raises(ValueError, match="no material path"):
            video.combine_videos(
                str(tmp_path / "bad.mp4"), [str(sources[0])], str(audio),
                threads=1, edit_list=[{"start": 0.0, "end": 1.0}],
            )

    def test_an_unreadable_material_is_refused_rather_than_skipped(self, workspace):
        """少渲一段画面，用户只会以为渲染器把它吞了。所以这里报错而不是跳过。"""
        tmp_path, sources, audio = workspace
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a video")
        with pytest.raises(ValueError, match="cannot be read"):
            video.combine_videos(
                str(tmp_path / "bad.mp4"), [str(sources[0])], str(audio),
                threads=1,
                edit_list=[{"path": str(broken), "start": 0.0, "end": 1.0}],
            )

    def test_an_out_of_range_end_is_clamped_to_the_source(self, workspace):
        """剪辑表可能是几小时前存的，期间素材被换成更短的一段也不该直接失败。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "clamped.mp4"
        video.combine_videos(
            str(output), [str(sources[0])], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            edit_list=[{"path": str(sources[0]), "start": 0.0, "end": 99.0}],
        )
        assert _probe_duration(output) == pytest.approx(3.0, abs=0.35)


class TestDefaultPathUnchanged:
    def test_without_an_edit_list_the_output_still_matches_the_narration(self, workspace):
        """一键出片那条路必须一个字节都不变：仍然按旁白长度收尾。"""
        tmp_path, sources, audio = workspace
        output = tmp_path / "auto.mp4"
        video.combine_videos(
            str(output), [str(source) for source in sources], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
            max_clip_duration=2,
        )
        # 旁白 6 秒，三段各取 2 秒恰好覆盖，成片按旁白收尾。
        assert _probe_duration(output) == pytest.approx(6.0, abs=0.6)
