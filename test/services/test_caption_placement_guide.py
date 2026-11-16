"""调字幕位置时，画面上画出 YouTube 自己会盖住的地方。

钉住的事：

* 示意图在画面框里、压在字幕上面，默认藏着，只跟着字幕面板出现；
* 竖版画 Shorts，横版画播放器；
* 字幕压在哪一块底下是量出来的，量的就是画出来的那几块；
* 画面上那行字能拖，拖出来的是渲染器认的那种位置（custom + 百分比），
  算法和渲染器一样；
* 加，不替换：原来的位置下拉框、百分比输入框、点字幕改字，都还在。

While the Subtitles panel is open the preview shows a sketch of what YouTube
draws over the video, says when the caption is underneath it, and lets the
caption be dragged. Dragging produces the renderer's own ``custom`` position.
"""

import re
from pathlib import Path

from app.services import scene_editor

ROOT = Path(__file__).resolve().parents[2]
EDITOR = ROOT / "resource/public/editor"


class TestTheSketch:
    html = (EDITOR / "index.html").read_text(encoding="utf-8")
    js = (EDITOR / "editor.js").read_text(encoding="utf-8")
    css = (EDITOR / "editor.css").read_text(encoding="utf-8")

    def test_every_element_the_script_looks_up_is_on_the_page(self):
        for dom_id in ("yt", "subs-yt", "subs-safe"):
            assert f'id="{dom_id}"' in self.html
            assert f'el("{dom_id}")' in self.js

    def test_it_is_inside_the_picture_and_hidden_until_asked_for(self):
        frame = self.html.split('<div id="frame"')[1].split('id="placeholder"')[0]
        assert re.search(r'<div id="yt" class="yt"[^>]*\bhidden\b', frame)
        # 只在字幕面板开着、而且勾着的时候出现。
        assert "const show = open && ui.subsYt.checked;" in self.js
        assert "ui.yt.hidden = !show;" in self.js

    def test_it_never_takes_the_mouse(self):
        """它压在字幕上面；要是它接鼠标，字幕就点不到也拖不动了。"""
        rule = self.css.split("\n.yt {")[1].split("}")[0]
        assert "pointer-events: none" in rule

    def test_it_shows_the_channel_and_the_title(self):
        sketch = self.html.split('<div id="yt"')[1].split('id="placeholder"')[0]
        assert "yt-avatar" in sketch and "Subscribe" in sketch
        assert sketch.count('class="yt-title"') == 2
        assert 'querySelectorAll(".yt-title")' in self.js

    def test_tall_videos_get_shorts_and_wide_ones_the_player(self):
        assert 'const shape = height > width ? "shorts" : "wide";' in self.js
        assert '.yt[data-shape="shorts"] .yt-wide' in self.css
        assert '.yt[data-shape="wide"] .yt-shorts' in self.css

    def test_what_is_covered_is_measured_from_what_is_drawn(self):
        body = self.js.split("function captionClashes()")[1].split("\n}\n")[0]
        assert 'querySelectorAll(".yt-zone")' in body
        assert "getBoundingClientRect()" in body
        # 每一块都说得出自己是什么，警告里才点得了名。
        zones = re.findall(r'class="yt-zone[^"]*"([^>]*)>', self.html)
        assert len(zones) == 5
        assert all("data-covers=" in rest for rest in zones)

    def test_it_is_not_sent_anywhere(self):
        """示意图只在浏览器里：保存字幕时不多发任何东西。"""
        save = self.js.split('ui.subsSave.addEventListener("click"')[1][:700]
        assert "yt" not in save.lower().replace("style", "")


class TestDraggingTheCaption:
    js = (EDITOR / "editor.js").read_text(encoding="utf-8")

    def test_only_while_the_subtitles_panel_is_open(self):
        down = self.js.split('ui.subsText.addEventListener("pointerdown"')[1][:200]
        assert "if (ui.subsPanel.hidden || event.button !== 0) return;" in down

    def test_it_sets_the_position_the_renderer_knows(self):
        assert "custom" in scene_editor.SUBTITLE_POSITIONS
        move = self.js.split('ui.subsText.addEventListener("pointermove"')[1][:900]
        assert 'ui.subsPosition.value = "custom";' in move
        # 百分比乘的是"画面高减字幕高"，和 video.py 里那一行一样。
        assert "(ui.frame.clientHeight || 0) - (ui.subs.offsetHeight || 0)" in move
        assert "previewSubtitleStyle();" in move
        video = (ROOT / "app/services/video.py").read_text(encoding="utf-8")
        assert "(video_height - _clip.h) * (params.custom_position / 100)" in video

    def test_letting_go_does_not_open_the_caption_editor(self):
        assert "captionDrag.justMoved = true;" in self.js
        guard = self.js.split('ui.subs.addEventListener("click"')[1][:260]
        assert "event.stopPropagation();" in guard and "}, true);" in guard

    def test_clicking_the_caption_still_edits_its_words(self):
        assert ('ui.subsText.addEventListener("click", () => {\n'
                "  if (state.cueIndex >= 0) openCaption(state.cueIndex);"
                in self.js.replace("\r\n", "\n"))

    def test_the_old_controls_are_still_there(self):
        html = (EDITOR / "index.html").read_text(encoding="utf-8")
        assert 'id="subs-position"' in html and 'id="subs-custom"' in html
