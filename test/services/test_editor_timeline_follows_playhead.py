"""放的时候时间线跟着红线走。

不跟的话红线几秒钟就走出可视区，人得一边看一边自己横向滚。做法是平滑滚动：
红线走到可视区正中就停在那里，时间线从它底下流过去；人自己滚走了就先不跟。

While the preview plays, the playhead walks to the middle of the view and the
timeline then slides under it. After the user scrolls away it leaves them
alone for a few seconds.
"""

from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "resource/public/editor/editor.js").read_text(
    encoding="utf-8").replace("\r\n", "\n")


def _function(name):
    return JS.split(f"\nfunction {name}(")[1].split("\n}\n")[0]


def test_every_frame_of_playback_checks_that_the_playhead_is_in_view():
    body = _function("tick")
    assert body.index("movePlayhead();") < body.index("followPlayhead();")


def test_the_playhead_walks_to_the_middle_and_then_the_timeline_slides_under_it():
    body = _function("followPlayhead")
    assert "const target = Math.max(0, at - width / 2);" in body
    # 还没到正中：时间线不动，红线自己走。
    assert "if (behind <= 0) { follow.lag = 0; return; }" in body
    # 到了正中之后每一帧都跟着：是滚，不是翻页。
    assert "put(target - follow.lag);" in body
    assert "width * 0.1" not in body


def test_clicking_ahead_while_playing_is_caught_up_with_not_jumped_to():
    """放着的时候往前点：把时间线直接摆过去，点的那个地方就从手底下跑了。"""
    body = _function("followPlayhead")
    # 红线一下子跑到正中右边：记下落后了多少，然后一帧一帧追回去。
    assert "if (behind > follow.lag + FOLLOW_JUMP_PX) follow.lag = behind;" in body
    assert "follow.lag *= Math.exp(-FOLLOW_CATCH_UP * elapsed);" in body
    assert body.index("follow.lag = behind;") < body.index("put(target - follow.lag);")
    # 只有红线根本不在眼前的时候才直接摆过去。
    gone = body.split("if (at < left || at > left + width) {")[1].split("}")[0]
    assert "put(target);" in gone


def test_pressing_play_brings_the_playhead_into_view_and_the_end_goes_back_to_the_start():
    assert "followPlayhead(true);" in _function("play")
    ended = _function("tick").split("state.playhead >= projectTotal()")[1].split("return;")[0]
    assert "followPlayhead(true);" in ended


def test_scrolling_away_by_hand_is_respected_for_a_while():
    body = _function("followPlayhead")
    assert "if (!force && now < follow.holdUntil) { follow.lag = 0; return; }" in body
    listener = JS.split('ui.scroll.addEventListener("scroll", () => {')[1].split("});")[0]
    # 自己翻的页不算。
    assert "Math.abs(ui.scroll.scrollLeft - follow.expected) < 2" in listener
    assert "follow.holdUntil = performance.now() + FOLLOW_HOLD_MS" in listener
