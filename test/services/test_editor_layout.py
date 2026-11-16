"""编辑器的布局：左边一列工具、停靠的面板、中间画面、右边属性、底下时间线。

以前脚本、字幕、声音、效果、发布这五个面板是浮在画面上的窗，开关在顶栏。
开着就挡着画面——而开它们正是为了看着画面调。现在它们停靠在左边，一次
只开一个。

这里钉住的是搬家之后不能丢的东西：每个面板还在、它们自己的开关逻辑没被
绕开、顶栏只剩整条片子的事。编辑器没有构建步骤和测试运行器，所以钉的是
源码本身；真实浏览器里的检查见提交说明。

The editor layout: a rail of tools on the left, one docked panel at a time,
the preview in the middle, properties on the right, the timeline below. These
pin what the move must not lose — every panel still exists, each panel's own
open/close logic is still what runs, and the top bar holds only whole-video
actions.
"""

import re
from pathlib import Path
from unittest.mock import patch

from app.services import scene_editor

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("script", "subs", "music", "effects", "publish")


def _read(name: str) -> str:
    return (ROOT / "resource" / "public" / "editor" / name).read_text(
        encoding="utf-8"
    )


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


class TestWhereThingsAre:
    def test_the_tools_are_a_rail_inside_the_stage_not_buttons_in_the_top_bar(self):
        page = _read("index.html")
        bar = _between(page, '<header class="bar">', "</header>")
        rail = _between(page, '<nav id="rail"', "</nav>")
        for tool in TOOLS:
            assert f'id="{tool}-toggle"' in rail, tool
            assert f'id="{tool}-toggle"' not in bar, tool
        stage = _between(page, '<main class="stage">', "</main>")
        assert stage.index('id="rail"') < stage.index('id="dock"')
        assert stage.index('id="dock"') < stage.index('class="viewer"')
        assert stage.index('class="viewer"') < stage.index('class="inspector"')

    def test_the_top_bar_holds_only_whole_video_actions(self):
        page = _read("index.html")
        bar = _between(page, '<header class="bar">', "</header>")
        for dom_id in ("library-toggle", "new-video", "project-title",
                       "app-settings", "rerecord", "render"):
            assert f'id="{dom_id}"' in bar, dom_id
        # 一串 uuid 的输入框不再占着顶栏。
        assert 'id="task"' not in bar and 'id="load"' not in bar

    def test_opening_by_id_is_still_possible(self):
        """从日志里抄来一个 id 的时候，它得有地方贴。"""
        page = _read("index.html")
        library = _between(page, '<section id="library"', "</section>")
        assert 'id="task"' in library and 'id="load"' in library
        script = _read("editor.js")
        # 而且打开之后"我的视频"那一屏得让开，否则片子开在它后面。
        assert 'ui.load.addEventListener("click", closeLibrary);' in script

    def test_every_panel_still_exists_exactly_once(self):
        page = _read("index.html")
        for tool in TOOLS:
            assert page.count(f'id="{tool}-panel"') == 1, tool

    def test_the_timeline_stays_at_the_bottom_under_the_edit_bar(self):
        page = _read("index.html")
        assert page.index("</main>") < page.index('id="edit-bar"') < page.index(
            '<section class="timeline">'
        )


class TestTheDock:
    def _dock(self) -> str:
        script = _read("editor.js")
        start = script.index("const DOCK_TABS = [")
        return script[start:script.index("function drawProjectTitle", start)]

    def test_all_five_panels_are_docked(self):
        dock = self._dock()
        for tool in TOOLS:
            assert f"ui.{tool}Toggle, ui.{tool}Panel, " in dock, tool
        assert "ui.dock.appendChild(panel);" in dock

    def test_opening_one_closes_the_others_through_their_own_close(self):
        """字幕面板关的时候要把没保存的预览还原。直接把它藏起来就漏了这一步，
        画面上会留着一份没保存的样式。"""
        dock = self._dock()
        assert "if (other !== panel && !other.hidden) close();" in dock
        table = dock[:dock.index("];")]
        for close in ("closeScript", "closeSubtitles", "closeMusic",
                      "closeEffects", "closePublish"):
            assert close in table, close
        # 关别的面板用的是它们的关闭函数，这里不直接改它们的 hidden。
        assert "other.hidden = true" not in dock

    def test_the_panels_own_toggles_were_not_replaced(self):
        """dock 只是在旁边加了一层，每个面板原来的开关还是原来那个。"""
        script = _read("editor.js")
        for tool in TOOLS:
            assert re.search(
                rf'ui\.{tool}Toggle\.addEventListener\("click", \(\) => \{{\s*'
                rf"if \(ui\.{tool}Panel\.hidden\)", script
            ), tool

    def test_the_rail_follows_the_panels_however_they_were_closed(self):
        """面板有自己的"关闭"按钮，保存之后也会自己关。页签亮不亮得跟着
        面板走，而不是跟着"谁点了页签"走。"""
        dock = self._dock()
        assert "new MutationObserver(syncDock)" in dock
        assert 'attributeFilter: ["hidden"]' in dock
        assert 'toggle.classList.toggle("on", !panel.hidden);' in dock

    def test_the_observer_cannot_wake_itself_up_forever(self):
        dock = self._dock()
        assert "if (ui.dock.hidden === open) ui.dock.hidden = !open;" in dock

    def test_a_docked_panel_is_no_longer_a_floating_window(self):
        css = _read("editor.css")
        rule = _between(css, ".dock .script-panel, .dock .script-panel.aside {", "}")
        for line in ("position: static;", "transform: none;", "box-shadow: none;"):
            assert line in rule, line

    def test_on_a_narrow_screen_the_panel_overlays_instead_of_squeezing(self):
        css = _read("editor.css")
        narrow = css[css.index("@media (max-width: 1100px)"):]
        narrow = narrow[:narrow.index("\n}\n") + 3]
        assert ".dock {" in narrow and "position: absolute;" in narrow


class TestTheTitle:
    def test_the_editor_is_told_what_the_video_is_called(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.timeline, "probe_duration", return_value=5.0):
            data = scene_editor.describe(task_id)
        assert data["subject"] == "seeds"

    def test_it_is_shown_in_the_top_bar(self):
        script = _read("editor.js")
        assert "ui.projectTitle.textContent = name;" in script
        draw = script[script.index("\nfunction draw() {"):]
        assert "drawProjectTitle();" in draw[:draw.index("\n}\n")]


class TestNothingWasLostInTheMove:
    def test_every_element_the_script_looks_up_is_on_the_page(self):
        page = _read("index.html")
        for dom_id in re.findall(r'el\("([a-z-]+)"\)', _read("editor.js")):
            assert f'id="{dom_id}"' in page, dom_id

    def test_no_element_id_appears_twice(self):
        import collections

        ids = re.findall(r'\sid="([a-z0-9-]+)"', _read("index.html"))
        twice = [one for one, count in collections.Counter(ids).items() if count > 1]
        assert twice == []


class TestExplanationsAreBehindAQuestionMark:
    """右边那一栏原来一半是整段的说明文字。字一个没删，收进了 "?" 里。"""

    @staticmethod
    def _inspector() -> str:
        script = _read("editor.js")
        start = script.index("\nfunction drawInspector() {")
        return script[start:script.index("\n/** 新增一段画面的表单", start)]

    def test_the_scene_panel_has_no_paragraph_of_instructions_left(self):
        """留在面板里的只有"状态"（存没存、排没排队、素材多长），没有"教程"。"""
        body = self._inspector()
        for gone in ("sourceNote", "effectsNote"):
            assert gone not in body, gone
        assist = _read("editor.js")
        assist = assist[assist.index("\nfunction drawPromptAssist("):]
        assist = assist[:assist.index("\nfunction ", 1)]
        assert 'hint.textContent' not in assist

    def test_the_words_themselves_were_kept(self):
        script = _read("editor.js")
        for kept in (
            "used exactly as written",
            "Stock searches are cached",
            "Suggest another ignores the box",
            "The motion is baked into this scene's clip",
        ):
            assert kept in script, kept
        body = self._inspector()
        assert "addHelp(keyword.wrap," in body
        assert "addHelp(firstEffect.wrap," in body
        assert "addHelp(direction.wrap, assistHelp);" in script

    def test_the_popup_is_not_clipped_by_the_panel_it_belongs_to(self):
        """右边那一栏会滚动、会裁掉溢出。浮层挂在 body 上、固定在屏幕上，并且
        夹在屏幕里面——那一栏的 "?" 紧贴着屏幕右边。"""
        script = _read("editor.js")
        show = script[script.index("\nfunction showHelp("):]
        show = show[:show.index("\nfunction ", 1)]
        assert "document.body.appendChild(helpPop);" in show
        assert "window.innerWidth - box.width - 8" in show
        assert "window.innerHeight" in show
        css = _read("editor.css")
        assert "position: fixed;" in _between(css, ".help-pop {", "}")

    def test_it_works_without_a_mouse_and_does_not_steal_the_click(self):
        script = _read("editor.js")
        tip = script[script.index("\nfunction helpTip("):]
        tip = tip[:tip.index("\nfunction ", 1)]
        assert "node.tabIndex = 0;" in tip
        assert 'node.addEventListener("focus"' in tip
        # 它坐在 <label> 里：点它不该把光标送进输入框。
        assert "event.preventDefault();" in tip


class TestTheScenePanelIsInSections:
    """画面、效果、时长原来排成一条长卷。现在一节一个抽屉。"""

    @staticmethod
    def _inspector() -> str:
        script = _read("editor.js")
        start = script.index("\nfunction drawInspector() {")
        return script[start:script.index("\n/** 新增一段画面的表单", start)]

    def test_the_three_sections_come_in_this_order(self):
        body = self._inspector()
        marks = re.findall(r'inspectorSection\("([^"]*)"\)', body)
        assert marks == ["Picture", "Effects", "Timing", ""]

    def test_each_section_starts_where_its_controls_start(self):
        body = self._inspector()
        assert body.index('inspectorSection("Picture")') < body.index("const keyword = field(")
        assert body.index('inspectorSection("Effects")') < body.index(
            "const transitions = (state.data"
        )
        assert body.index('inspectorSection("Timing")') < body.index(
            'const inPoint = field("In-point (s)"'
        )

    def test_lock_and_delete_are_about_the_whole_scene_not_one_section(self):
        body = self._inspector()
        end = body.index('inspectorSection("")')
        assert end < body.index('button(scene.locked ? "Unlock" : "Lock"')
        assert end < body.index('button("Delete scene"')

    def test_only_the_picture_section_starts_open(self):
        script = _read("editor.js")
        assert (
            "const inspectorOpen = { Picture: true, Effects: false, Timing: false };"
            in script
        )

    def test_a_section_stays_open_from_scene_to_scene(self):
        """把"时长"打开之后一段一段往下点，它应该一直开着：开没开是按节记
        的，不是按场景记的。"""
        script = _read("editor.js")
        group = script[script.index("\nfunction groupInspector("):]
        group = group[:group.index("\nfunction ", 1)]
        assert "box.open = inspectorOpen[name] !== false;" in group
        assert "inspectorOpen[name] = box.open;" in group

    def test_the_grouping_runs_and_leaves_no_marker_behind(self):
        body = self._inspector()
        assert body.rstrip().endswith("groupInspector();\n}".rstrip()) or (
            "groupInspector();\n}" in body
        )
        script = _read("editor.js")
        mark = script[script.index("\nfunction inspectorSection("):]
        mark = mark[:mark.index("\nfunction ", 1)]
        # 记号本身看不见：某条路提前返回、没走到分组，也不会露出来。
        assert "mark.hidden = true;" in mark


class TestTheLeftPanelsLostTheirParagraphsToo:
    def test_only_static_explanations_are_tucked_away(self):
        """带 id 的是会变的状态（"已保存"、"这条片子是竖版"），不是说明。
        把它们摘掉，往里写字的那段代码就写到空气里去了。"""
        script = _read("editor.js")
        tuck = script[script.index("\nfunction tuckPanelHints("):]
        tuck = tuck[:tuck.index("\ntuckPanelHints();")]
        assert 'if (hint.id || hint.querySelector("[id]")) continue;' in tuck
        assert "label.appendChild(helpTip(text));" in tuck
        assert "hint.remove();" in tuck

    def test_the_explanations_are_still_written_in_the_page(self):
        page = _read("index.html")
        for kept in ("Plays under the whole video",
                     "How the captions look in the finished video",
                     "Nothing here goes into the video"):
            assert kept in page, kept
