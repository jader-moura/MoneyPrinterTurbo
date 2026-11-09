"""转场音效：音效库的解析规则，以及它在成片音轨上落在哪里。"""

import math
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from moviepy import AudioFileClip, ColorClip

from app.models.schema import VideoAspect, VideoConcatMode, VideoParams
from app.services import transition_sound, video


def _write_tone(path: Path, seconds: float, frequency: float = 880.0, fps: int = 22050):
    """写一个固定频率的单声道 WAV，当作音效或旁白。"""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(fps)
        frames = bytearray()
        for index in range(int(seconds * fps)):
            value = int(32767 * 0.5 * math.sin(2 * math.pi * frequency * index / fps))
            frames += struct.pack("<h", value)
        handle.writeframes(bytes(frames))
    return path


def _write_silence(path: Path, seconds: float, fps: int = 22050):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(fps)
        handle.writeframes(b"\x00\x00" * int(seconds * fps))
    return path


@pytest.fixture
def sound_library(tmp_path, monkeypatch):
    """把音效库指向临时目录，测试不碰仓库里的 resource/ 和 storage/。"""
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    monkeypatch.setattr(
        transition_sound.utils, "transition_sound_dir", lambda sub_dir="": str(builtin)
    )
    monkeypatch.setattr(
        transition_sound, "user_transition_sound_dir", lambda create=True: str(user)
    )
    return builtin, user


def test_an_empty_choice_or_a_muted_one_skips_the_whole_feature():
    """没选音效和把音量拧到 0 是同一件事：整条音效链路都不该启动。"""
    assert transition_sound.should_use_transition_sound("whoosh.mp3", 0.5) is True
    assert transition_sound.should_use_transition_sound("", 0.5) is False
    assert transition_sound.should_use_transition_sound("   ", 0.5) is False
    assert transition_sound.should_use_transition_sound(None, 0.5) is False
    assert transition_sound.should_use_transition_sound("whoosh.mp3", 0) is False
    assert transition_sound.should_use_transition_sound("whoosh.mp3", -1) is False
    assert transition_sound.should_use_transition_sound("whoosh.mp3", None) is False
    assert transition_sound.should_use_transition_sound("whoosh.mp3", float("nan")) is False


def test_the_library_lists_both_folders_and_the_user_file_wins(sound_library):
    builtin, user = sound_library
    _write_tone(builtin / "whoosh.wav", 0.2)
    _write_tone(builtin / "pop.wav", 0.2)
    _write_tone(user / "whoosh.wav", 0.2)
    (builtin / "notes.txt").write_text("not audio")

    listed = transition_sound.list_transition_sounds()
    assert [Path(item).name for item in listed] == ["pop.wav", "whoosh.wav"]
    # 用户目录在后，同名时覆盖内置文件。
    assert Path(transition_sound.resolve_transition_sound("whoosh.wav")).parent == user
    assert [
        Path(item).name for item in transition_sound.list_builtin_transition_sounds()
    ] == ["pop.wav", "whoosh.wav"]


def test_a_name_outside_the_library_is_refused(sound_library):
    builtin, _ = sound_library
    _write_tone(builtin / "whoosh.wav", 0.2)

    with pytest.raises(ValueError, match="required"):
        transition_sound.resolve_transition_sound("")
    with pytest.raises(ValueError, match="filename"):
        transition_sound.resolve_transition_sound("../whoosh.wav")
    with pytest.raises(ValueError, match="not available"):
        transition_sound.resolve_transition_sound("missing.wav")


def test_each_cut_gets_its_own_copy_and_none_runs_past_the_end(tmp_path):
    """音效在每个剪辑点各放一份，而且不能伸到成片结尾之外。

    伸出去的音频会把 ``CompositeAudioClip`` 的时长一起拉长，成片末尾就会多出
    一截只有声音的黑屏。
    """
    sound = _write_tone(tmp_path / "whoosh.wav", 1.0)
    clips = video._build_transition_sound_clips(
        str(sound), [3.0, 6.0, 9.6, 12.0, 0.0], volume=0.5, timeline_duration=10.0
    )
    try:
        # 12.0 在成片之外，0.0 是开头而不是剪辑点，两个都要被丢掉。
        assert [round(clip.start, 3) for clip in clips] == [3.0, 6.0, 9.6]
        assert all(clip.end <= 10.0 + 1e-6 for clip in clips)
        # 9.6 处只剩 0.4 秒，音效要被截断而不是顶出成片。
        assert clips[-1].duration == pytest.approx(0.4, abs=0.01)
    finally:
        for clip in clips:
            clip.close()


def test_a_long_ambient_file_is_cut_down_and_faded_out(tmp_path):
    """素材站的"转场音效"里混着几十秒的环境音，整段铺上去会盖掉旁白。"""
    sound = _write_tone(tmp_path / "ambience.wav", 6.0)
    samples, fps = video._load_transition_sound_samples(str(sound))

    assert len(samples) / fps == pytest.approx(
        transition_sound.MAX_TRANSITION_SOUND_SECONDS, abs=0.05
    )
    # 末尾硬切会"啪"一声，所以最后一段必须是淡出的。
    tail = np.abs(samples[-int(0.02 * fps):]).max()
    head = np.abs(samples[: int(0.2 * fps)]).max()
    assert tail < head * 0.1


def test_the_renderer_reports_where_the_cuts_are(tmp_path, monkeypatch):
    """音效要响在剪辑点上，而"剪辑点在哪"只有切片那一层知道。

    合成之后就只剩一条拼好的 MP4 了，所以 ``combine_videos`` 必须把交界时刻
    交出来。这里真渲一次三段纯色片，量的就是它报回来的那几个时刻。
    """
    binary = video.utils.get_ffmpeg_binary()
    narration = _write_silence(tmp_path / "narration.wav", 6.0, fps=24000)
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

    boundaries = []
    video.combine_videos(
        str(tmp_path / "combined.mp4"),
        [str(source) for source in sources],
        str(narration),
        video_concat_mode=VideoConcatMode.sequential,
        threads=1,
        max_clip_duration=2,
        clip_boundaries=boundaries,
    )

    assert boundaries, "the renderer reported no cuts at all"
    # 每段 2 秒铺满 6 秒旁白，所以交界点落在 2 秒和 4 秒。
    assert boundaries == pytest.approx([2.0, 4.0], abs=0.25)
    # 开头不是交界点，结尾之后也没有下一段。
    assert all(0.0 < moment < 6.0 for moment in boundaries)
    assert list(boundaries) == sorted(boundaries)


def test_the_cut_is_audible_in_the_rendered_video_and_silent_without_a_sound(
    tmp_path, sound_library
):
    """选了音效，成片在剪辑点上就要真的响一下；不选则音轨原样不动。"""
    builtin, _ = sound_library
    _write_tone(builtin / "whoosh.wav", 0.5)
    narration = _write_silence(tmp_path / "narration.wav", 2.0)
    source_video = tmp_path / "combined.mp4"
    with ColorClip((64, 64), color=(10, 10, 10), duration=2.0) as clip:
        clip.write_videofile(str(source_video), fps=12, logger=None, audio=False)

    def render(output_name, sound_name):
        params = VideoParams(
            video_subject="transition sound",
            subtitle_enabled=False,
            bgm_type="",
            voice_volume=1.0,
            transition_sound=sound_name,
            transition_sound_volume=1.0,
        )
        output_file = tmp_path / output_name
        video.generate_video(
            video_path=str(source_video),
            audio_path=str(narration),
            subtitle_path="",
            output_file=str(output_file),
            params=params,
            transition_sound_times=[1.0],
        )
        with AudioFileClip(str(output_file)) as audio:
            return (
                float(np.abs(audio.subclipped(1.0, 1.3).to_soundarray()).max()),
                float(np.abs(audio.subclipped(0.2, 0.5).to_soundarray()).max()),
            )

    at_the_cut, before_the_cut = render("with-sound.mp4", "whoosh.wav")
    assert at_the_cut > 0.1, "the transition sound is missing from the rendered video"
    assert before_the_cut < 0.01, "the transition sound leaked outside the cut"

    silent_cut, _ = render("no-sound.mp4", "")
    assert silent_cut < 0.01, "a video with no transition sound selected gained audio"
