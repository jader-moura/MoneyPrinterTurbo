"""关掉字幕不该把画面对齐也一起关掉。

Why these tests exist
---------------------
字幕文件有两个用途，而代码原来只认一个：

* 看得见的那个——烧进画面的字幕，由 ``subtitle_enabled`` 控制；
* 看不见的那个——**画面对齐旁白的唯一时间来源**。
  ``scene_manifest.narration_segments`` 读的就是这个文件,读不到就返回空,
  于是 ``plan_windows`` 退回按比例平均铺:每张图一样长,不管那句话念了多久。

于是 ``subtitle_enabled = false`` 的用户，逐句同步一次都没有生效过，而界面上
没有任何地方会说一句。实测就是这么发现的：一条 499 秒、125 段画面的任务，
``subtitle_path`` 是 ``None``，``narration_segments`` 返回 0 条。

另一头的坑同样要钉住：只要时间轴的时候**绝不能**去加载 whisper。那是几 GB
的下载，而用户要的只是别显示字幕。
"""

import os
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import task as task_service


class _SubMaker:
    """够用的假时间轴：只要不是 None，edge 那条路就会被走到。"""


@pytest.fixture
def task(tmp_path, monkeypatch):
    def fake_task_dir(sub_dir: str = ""):
        target = tmp_path / sub_dir if sub_dir else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(task_service.utils, "task_dir", fake_task_dir)
    return "t1"


SCRIPT = "The first night. Then the first week. It keeps going."
CUES = [(1, "00:00:00,000 --> 00:00:02,000", "The first night.")]


def _run(task_id, params, sub_maker, *, provider="edge", written=True):
    """跑一遍字幕阶段，把真正写文件那一步换成可控的假动作。"""
    calls = {"edge": 0, "whisper": 0}

    def fake_edge(text, sub_maker, subtitle_file, word_level=False):
        calls["edge"] += 1
        if written:
            with open(subtitle_file, "w", encoding="utf-8") as handle:
                handle.write(
                    "1\n00:00:00,000 --> 00:00:02,000\nThe first night.\n\n"
                )

    def fake_whisper(audio_file, subtitle_file, word_level=False):
        calls["whisper"] += 1
        if written:
            with open(subtitle_file, "w", encoding="utf-8") as handle:
                handle.write(
                    "1\n00:00:00,000 --> 00:00:02,000\nThe first night.\n\n"
                )

    with (
        patch.dict(task_service.config.app, {"subtitle_provider": provider}),
        patch.object(task_service.voice, "create_subtitle", side_effect=fake_edge),
        patch.object(task_service.subtitle, "create", side_effect=fake_whisper),
        patch.object(
            task_service.subtitle,
            "file_to_subtitles",
            return_value=CUES if written else [],
        ),
        patch.object(task_service.subtitle, "correct"),
    ):
        path = task_service.generate_subtitle(
            task_id, params, SCRIPT, sub_maker, "audio.mp3"
        )
    return path, calls


class TestCaptionsOff:
    def test_the_cue_file_is_still_written(self, task):
        """这是整件事的要点：没有这个文件，逐句同步就是死的。"""
        params = VideoParams(video_subject="x", subtitle_enabled=False)
        path, calls = _run(task, params, _SubMaker())
        assert path, "captions off must still leave a cue file for timing"
        assert os.path.isfile(path)
        assert calls["edge"] == 1

    def test_whisper_is_never_loaded_just_for_timing(self, task):
        """几 GB 的下载，换一个用户没要的东西——绝对不行。"""
        params = VideoParams(video_subject="x", subtitle_enabled=False)
        path, calls = _run(task, params, _SubMaker(), provider="whisper")
        assert calls["whisper"] == 0
        assert calls["edge"] == 1
        assert path

    def test_no_timings_at_all_is_not_an_error(self, task):
        """上传的音频没经过 TTS，没有时间轴可用。这时候平均铺是正确结果。"""
        params = VideoParams(video_subject="x", subtitle_enabled=False)
        path, calls = _run(task, params, None)
        assert path == ""
        assert calls == {"edge": 0, "whisper": 0}

    def test_it_says_why_the_pictures_will_be_spread_evenly(self, task):
        from loguru import logger

        said: list[str] = []
        sink = logger.add(said.append, level="INFO")
        try:
            _run(task, VideoParams(video_subject="x", subtitle_enabled=False), None)
        finally:
            logger.remove(sink)
        text = " ".join(said)
        assert "spread evenly" in text, "同步没生效时要说一句，否则没人会发现"


class TestCaptionsOn:
    """开着字幕的行为一个字都不能变。"""

    def test_the_edge_path_is_unchanged(self, task):
        params = VideoParams(video_subject="x", subtitle_enabled=True)
        path, calls = _run(task, params, _SubMaker())
        assert path and os.path.isfile(path)
        assert calls == {"edge": 1, "whisper": 0}

    def test_whisper_still_runs_when_it_is_chosen(self, task):
        params = VideoParams(video_subject="x", subtitle_enabled=True)
        path, calls = _run(task, params, None, provider="whisper")
        assert path
        assert calls["whisper"] == 1

    def test_a_provider_that_writes_nothing_returns_nothing(self, task):
        params = VideoParams(video_subject="x", subtitle_enabled=True)
        path, _ = _run(task, params, _SubMaker(), written=False)
        assert path == ""


class TestTheRendererStillDecidesWhatIsVisible:
    def test_burning_captions_in_is_gated_on_the_flag_not_the_file(self):
        """文件在不等于字幕要烧进画面——否则这个改动会给所有人加上字幕。

        渲染那边自己查 ``params.subtitle_enabled``（video.py 的 1908 和 2160
        两处），所以多出一个文件不会改变成片的样子。这里只是把那个约定钉住。
        """
        import inspect

        from app.services import video

        source = inspect.getsource(video.generate_video)
        assert "params.subtitle_enabled" in source
