"""编辑器里放片子、缩放时间线时画面和位置不乱跳。

钉住的事：

* 缩放之后红线回到可视区正中，人看着的那一段不跑掉；
* 旁白在走时间时，片段早放完（或到了出点）就停在那一帧等着，不去推播放头
  ——推了会被下一帧照旁白拉回来，两段来回重新加载，一直闪；
* 换片段的空当里留着上一帧，新的一帧出来才撤，过期的回调不作数。

Zooming the timeline keeps the playhead centred, and playback does not flicker
between two scenes or flash black when the scene changes.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (PAGE / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (PAGE / "editor.css").read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(name):
    return JS.split(f"\nfunction {name}(")[1].split("\n}\n")[0]


def _listener(event):
    return JS.split(f'ui.preview.addEventListener("{event}"')[1].split("\n});")[0]


class TestZoomingTheTimeline:
    def test_the_playhead_is_centred_after_the_tracks_are_redrawn(self):
        handler = JS.split('ui.zoom.addEventListener("input"')[1].split("\n});")[0]
        assert handler.index("draw();") < handler.index("centerOnPlayhead();")

    def test_centred_means_half_the_visible_width_before_it(self):
        body = _function("centerOnPlayhead")
        assert "state.playhead * state.pxPerSecond" in body
        assert "ui.scroll.clientWidth / 2" in body
        assert "ui.scroll.scrollLeft = Math.max(0," in body


class TestAClipThatEndsBeforeItsScene:
    def test_the_narration_clock_is_the_one_tick_uses(self):
        assert "!!ui.narration.src && !ui.narration.paused" in _function(
            "narrationIsTheClock")
        assert "ui.narration.src && !ui.narration.paused" in _function("tick")

    def test_ending_early_does_not_push_the_playhead_under_the_narration(self):
        body = _listener("ended")
        assert body.index("if (narrationIsTheClock()) return;") < body.index(
            "state.playhead = offsets[index + 1]")

    def test_reaching_the_out_point_holds_that_frame_under_the_narration(self):
        body = _listener("timeupdate")
        held = "if (narrationIsTheClock()) { ui.preview.pause(); return; }"
        assert body.index(held) < body.index("state.playhead = offsets[index + 1]")

    def test_without_narration_the_pictures_still_move_on_by_themselves(self):
        for event in ("ended", "timeupdate"):
            assert "state.playhead = offsets[index + 1]" in _listener(event)

    def test_scrubbing_back_into_a_held_scene_plays_it_again(self):
        body = _function("syncPreview")
        forced = body.split("if (force) {")[1]
        assert "if (state.playing && ui.preview.paused) ui.preview.play()" in forced


class TestChangingScene:
    def test_the_last_frame_is_kept_over_the_video(self):
        frame = HTML.split('id="frame"')[1].split('id="fx"')[0]
        assert frame.index('id="preview"') < frame.index('<canvas id="hold"')
        assert 'id="hold" class="hold" hidden' in frame
        assert ".hold[hidden] { display: none; }" in CSS

    def test_it_is_drawn_before_the_source_changes(self):
        body = _function("syncPreview")
        assert body.index("holdCurrentFrame();") < body.index(
            "ui.preview.src = scene.url;")

    def test_nothing_is_held_when_there_is_no_frame_to_hold(self):
        assert "!video.videoWidth || video.readyState < 2" in _function(
            "holdCurrentFrame")

    def test_it_goes_when_the_new_clip_shows_a_frame_or_never_loads(self):
        body = _function("syncPreview")
        assert "requestVideoFrameCallback(shown)" in body
        assert 'addEventListener("seeked", shown' in body
        assert "setTimeout(() => { if (swap === clipSwap) releaseHeldFrame(); }" in body

    def test_a_clip_that_was_replaced_while_loading_does_not_seek_or_reveal(self):
        body = _function("syncPreview")
        loaded = body.split('addEventListener("loadedmetadata"')[1]
        assert loaded.index("if (swap !== clipSwap) return;") < loaded.index(
            "seekWithin(scene, offset);")

    def test_it_is_not_left_over_a_gap_or_a_missing_clip(self):
        body = _function("syncPreview")
        assert body.count("releaseHeldFrame();") >= 4
