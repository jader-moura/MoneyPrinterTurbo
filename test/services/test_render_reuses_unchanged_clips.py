"""出片时不做白工：没变的片段不重新编码，要重渲的图片几段同时渲。

为什么要这些测试
----------------
一条 13 分钟、140 段的片子实测出一次片 56 分钟，其中 33 分钟花在两件一段一段
排着队做的事上：把图片片段按新时长重渲（13 分钟），和把每一段裁剪、缩放、
重新编码（20 分钟）。机器有十六个线程，那半个小时里大半闲着。更糟的是人在
编辑器里通常只改了几段就再出一次片，其余一百多段又原样重做一遍。

这里钉住两件事：

* 处理好的片段留在任务目录的 ``render-cache`` 里，决定它长什么样的东西一样
  都没变就直接用；变了的那一段才重做，用不上的旧文件出完片就删。
* 要按新时长重渲的图片片段几段同时渲，结果和排着队渲的一模一样。

Rendering does no work twice: a processed clip is reused while nothing that
decides its content has changed, and image clips that need a new length are
re-rendered several at a time.
"""

import json
import subprocess
import threading
import time

import pytest

from app.models.schema import VideoConcatMode, VideoTransitionMode
from app.services import timeline, video
from test.services.test_edit_list_render import _probe_duration, workspace  # noqa: F401


def _render(tmp_path, sources, audio, edit_list, name="out.mp4", **more):
    output = tmp_path / name
    video.combine_videos(
        str(output), [str(source) for source in sources], str(audio),
        video_concat_mode=VideoConcatMode.sequential, threads=1,
        edit_list=edit_list, **more,
    )
    return output


@pytest.fixture
def encodes(monkeypatch):
    """每编码一段就记一笔。"""
    written = []
    real = video._write_videofile_with_codec_fallback

    def counted(clip, output_file, *args, **kwargs):
        written.append(output_file)
        return real(clip, output_file, *args, **kwargs)

    monkeypatch.setattr(video, "_write_videofile_with_codec_fallback", counted)
    return written


def _kept(tmp_path):
    folder = tmp_path / video._CLIP_CACHE_DIR
    return sorted(path.name for path in folder.glob("*.mp4")) if folder.is_dir() else []


def _timeline(sources):
    return [
        {"path": str(sources[0]), "start": 0.0, "end": 1.0},
        {"path": str(sources[1]), "start": 0.5, "end": 1.5},
    ]


class TestAnUnchangedClipIsNotEncodedAgain:
    def test_the_second_render_of_the_same_timeline_encodes_nothing(
            self, workspace, encodes):  # noqa: F811
        tmp_path, sources, audio = workspace
        first = _render(tmp_path, sources, audio, _timeline(sources), "first.mp4")
        assert len(encodes) == 2 and len(_kept(tmp_path)) == 2
        encodes.clear()
        second = _render(tmp_path, sources, audio, _timeline(sources), "second.mp4")
        assert encodes == []
        # 拿上次的文件拼出来的，就是上次那条片子。
        assert _probe_duration(second) == pytest.approx(_probe_duration(first), abs=0.01)
        assert second.stat().st_size == first.stat().st_size

    def test_only_the_scene_that_changed_is_done_again(self, workspace, encodes):  # noqa: F811
        tmp_path, sources, audio = workspace
        _render(tmp_path, sources, audio, _timeline(sources))
        before = _kept(tmp_path)
        encodes.clear()
        changed = _timeline(sources)
        changed[1]["end"] = 2.0
        output = _render(tmp_path, sources, audio, changed, "changed.mp4")
        assert len(encodes) == 1
        assert _probe_duration(output) == pytest.approx(2.5, abs=0.35)
        # 第一段的文件还是那一个；第二段换了新的，旧的那个删掉了。
        after = _kept(tmp_path)
        assert len(after) == 2 and len(set(after) & set(before)) == 1

    def test_a_picture_that_was_redrawn_is_done_again(self, workspace, encodes):  # noqa: F811
        tmp_path, sources, audio = workspace
        _render(tmp_path, sources, audio, _timeline(sources))
        encodes.clear()
        # 同一个文件名，换了内容：编辑器里重画一张图就是这样。
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=yellow:s=64x64:r=10",
             "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sources[0])],
            check=True, capture_output=True,
        )
        _render(tmp_path, sources, audio, _timeline(sources), "redrawn.mp4")
        assert len(encodes) == 1

    def test_everything_that_decides_the_clip_is_in_its_name(self, workspace):  # noqa: F811
        _tmp, sources, _audio = workspace
        base = dict(start=0.0, end=1.0, tail=False, speed=1.0, canvas=[64, 64],
                    fit="cover", transition=None, fps=30, codec="libx264")
        key = video._clip_cache_key(str(sources[0]), **base)
        assert key == video._clip_cache_key(str(sources[0]), **base)
        for changed in (
            {"start": 0.1}, {"end": 1.2}, {"tail": True}, {"speed": 1.5},
            {"canvas": [128, 64]}, {"fit": "contain"}, {"transition": "CrossFade"},
            {"fps": 25}, {"codec": "h264_nvenc"},
        ):
            assert video._clip_cache_key(str(sources[0]), **{**base, **changed}) != key
        assert video._clip_cache_key(str(sources[1]), **base) != key
        assert video._clip_cache_key(str(sources[0]) + ".gone", **base) == ""

    def test_a_clip_cut_off_while_it_was_written_is_done_again(
            self, workspace, encodes):  # noqa: F811
        tmp_path, sources, audio = workspace
        _render(tmp_path, sources, audio, _timeline(sources))
        broken = tmp_path / video._CLIP_CACHE_DIR / _kept(tmp_path)[0]
        broken.write_bytes(broken.read_bytes()[:200])
        encodes.clear()
        output = _render(tmp_path, sources, audio, _timeline(sources), "again.mp4")
        assert len(encodes) == 1
        assert _probe_duration(output) == pytest.approx(2.0, abs=0.35)


class TestWhatIsNeverKept:
    def test_a_transition_that_comes_out_different_every_time(
            self, workspace, encodes):  # noqa: F811
        """滑入的方向是随机挑的：留着上次的就是把随机变成了固定。"""
        tmp_path, sources, audio = workspace
        for name in ("one.mp4", "two.mp4"):
            _render(tmp_path, sources, audio, _timeline(sources), name,
                    video_transition_mode=VideoTransitionMode.slide_in)
        assert len(encodes) == 4 and _kept(tmp_path) == []

    def test_the_automatic_render_keeps_nothing(self, workspace, encodes):  # noqa: F811
        tmp_path, sources, audio = workspace
        output = tmp_path / "auto.mp4"
        video.combine_videos(
            str(output), [str(source) for source in sources], str(audio),
            video_concat_mode=VideoConcatMode.sequential, threads=1,
        )
        assert output.exists() and encodes
        assert not (tmp_path / video._CLIP_CACHE_DIR).exists()

    def test_the_processed_clips_of_a_failed_join_are_kept_for_the_retry(
            self, workspace, encodes, monkeypatch):  # noqa: F811
        tmp_path, sources, audio = workspace

        def fails(*args, **kwargs):
            raise RuntimeError("the join failed")

        monkeypatch.setattr(video, "concat_video_clips_with_ffmpeg", fails)
        with pytest.raises(RuntimeError):
            _render(tmp_path, sources, audio, _timeline(sources))
        assert len(_kept(tmp_path)) == 2
        # 没进缓存的临时片段照旧不留。
        assert not list(tmp_path.glob("temp-clip-*.mp4"))


class TestImageClipsAreRenderedSeveralAtATime:
    @pytest.fixture
    def slow_renders(self, monkeypatch):
        """假的重渲：记下同时有几段在渲。"""
        seen = {"now": 0, "most": 0, "calls": []}
        guard = threading.Lock()

        def render(task_id, material, needed):
            with guard:
                seen["now"] += 1
                seen["most"] = max(seen["most"], seen["now"])
                seen["calls"].append((material, needed))
            time.sleep(0.05)
            with guard:
                seen["now"] -= 1
            return f"{material}@{needed}"

        monkeypatch.setattr(timeline, "ensure_image_length", render)
        return seen

    def test_they_overlap_up_to_the_configured_number(self, slow_renders, monkeypatch):
        monkeypatch.setattr(video, "_get_clip_processing_concurrency", lambda: 4)
        wanted = [(f"clip{at}.mp4", 6.0) for at in range(12)]
        found = timeline._extend_images("task", wanted)
        assert found == {pair: f"{pair[0]}@{pair[1]}" for pair in wanted}
        assert 2 <= slow_renders["most"] <= 4

    def test_one_at_a_time_when_that_is_the_setting(self, slow_renders, monkeypatch):
        monkeypatch.setattr(video, "_get_clip_processing_concurrency", lambda: 1)
        wanted = [(f"clip{at}.mp4", 6.0) for at in range(4)]
        timeline._extend_images("task", wanted)
        assert slow_renders["most"] == 1
        assert slow_renders["calls"] == wanted

    def test_the_same_clip_and_length_is_rendered_once(self, slow_renders, monkeypatch):
        monkeypatch.setattr(video, "_get_clip_processing_concurrency", lambda: 4)
        found = timeline._extend_images(
            "task", [("a.mp4", 6.0), ("a.mp4", 6.0), ("a.mp4", 7.0)])
        assert len(slow_renders["calls"]) == 2
        assert set(found) == {("a.mp4", 6.0), ("a.mp4", 7.0)}

    def test_nothing_to_render_starts_nothing(self, slow_renders):
        assert timeline._extend_images("task", []) == {}
        assert slow_renders["calls"] == []


def test_the_note_beside_a_kept_clip_says_how_long_it_is(workspace):  # noqa: F811
    tmp_path, sources, audio = workspace
    _render(tmp_path, sources, audio, _timeline(sources))
    notes = sorted((tmp_path / video._CLIP_CACHE_DIR).glob("*.json"))
    assert len(notes) == 2
    for note in notes:
        assert json.loads(note.read_text("utf-8"))["duration"] == pytest.approx(1.0, abs=0.01)
