# -*- coding: utf-8 -*-
"""播放中也能挪播放头。

用户的原话："the red line timeline cannot be changed when the video is
playing, I need to stop change the red line position in the timeline and then
play again, we should be able of changing it while the video is playing"。

为什么原来不行：放着的时候旁白是时钟——``tick`` 每一帧都把播放头读成
``ui.narration.currentTime``。点一下只改了 ``state.playhead``，下一帧就被
旁白拽回原处，于是看起来"播放时点哪儿都没用"。所以挪播放头必须连旁白一起
挪，这正是下面钉住的事。

这个项目里没有跑 JS 的测试框架，所以这些是读源码的断言，和 editor.js 的
其它测试一样。但它们只读 ``scrubTo`` 这一个函数体，而不是整个文件里 grep
一个词——"这一行在文件里某处出现过"证明不了它在对的地方。
"""

import re
from pathlib import Path

import pytest

EDITOR = Path(__file__).parents[2] / "resource" / "public" / "editor" / "editor.js"
CSS = Path(__file__).parents[2] / "resource" / "public" / "editor" / "editor.css"


def source() -> str:
    return EDITOR.read_text(encoding="utf-8")


def body_of(name: str) -> str:
    """把一个函数的函数体抠出来，按花括号配对。"""
    text = source()
    start = text.index(f"function {name}(")
    opened = text.index("{", start)
    depth = 0
    for at in range(opened, len(text)):
        if text[at] == "{":
            depth += 1
        elif text[at] == "}":
            depth -= 1
            if depth == 0:
                return text[opened : at + 1]
    raise AssertionError(f"{name} has unbalanced braces")


class TestMovingThePlayheadWhilePlaying:
    def test_there_is_one_place_that_moves_it(self):
        assert "function scrubTo(" in source()

    def test_it_moves_the_narration_too(self):
        """这就是那个 bug：不挪旁白，下一帧就被旁白拽回去。"""
        body = body_of("scrubTo")
        assert "ui.narration.currentTime = state.playhead" in body

    def test_it_moves_the_music_too_and_keeps_the_loop_in_phase(self):
        """背景音乐是循环的，按余数对位才听得到成片里那一刻的那一段。"""
        body = body_of("scrubTo")
        assert "ui.musicBed.currentTime" in body
        assert "state.playhead % length" in body

    def test_it_updates_the_picture_and_the_red_line(self):
        body = body_of("scrubTo")
        assert "syncPreview(true)" in body
        assert "movePlayhead()" in body

    def test_it_does_not_reload_the_same_clip_on_every_pixel(self):
        """清掉 loadedClip 等于每挪一像素重载一次同一个文件。

        拖动会卡成幻灯片，而且每次重载都算一次"切片段"，转场动画一直闪。
        """
        assert "state.loadedClip = -1" not in body_of("scrubTo")


class TestDraggingIt:
    @staticmethod
    def handler() -> str:
        text = source()
        start = text.index("for (const track of [ui.ruler, ui.audioTrack])")
        return text[start : start + 1800]

    def test_a_press_starts_a_drag_not_just_a_jump(self):
        wiring = self.handler()
        assert "pointerdown" in wiring
        assert "pointermove" in wiring

    def test_the_drag_ends_on_release_or_cancel(self):
        """pointercancel 也要收尾，不然触控笔被系统打断后播放头会一直跟手。"""
        wiring = self.handler()
        assert "pointerup" in wiring
        assert "pointercancel" in wiring
        assert wiring.count("removeEventListener") >= 3

    def test_it_keeps_following_outside_the_track(self):
        assert "setPointerCapture" in self.handler()

    def test_every_step_of_the_drag_goes_through_the_one_place(self):
        wiring = self.handler()
        assert wiring.count("scrubTo(") >= 2

    def test_the_old_shape_is_gone(self):
        """原来是在 pointerdown 里直接改 state.playhead，不碰旁白。"""
        wiring = self.handler()
        assert "state.playhead = Math.max(0, Math.min(projectTotal()," not in wiring


class TestTheRedLineItself:
    def test_it_does_not_swallow_the_click(self):
        """播放头压在尺子上面，它要是吃掉指针事件，点在它附近就没反应。"""
        styles = CSS.read_text(encoding="utf-8")
        block = styles[styles.index(".playhead {") : styles.index(".playhead::before")]
        assert "pointer-events: none" in block
