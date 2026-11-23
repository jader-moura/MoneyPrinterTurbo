# -*- coding: utf-8 -*-
"""音色下拉框按语言分组。

用户的原话："when selecting the voice (match script language) we should order
them by language (english, chinese, etc) might be good to add a title
splitting them"。

Azure 那份清单是 331 个音色、75 种语言。平铺成一个下拉框的时候，找"和脚本
同一种语言的那几个"全靠滚动和眼力。这里钉的是：分组、组的顺序、标题，以及
一个音色都不会在分组的过程中丢掉。

Streamlit 的 selectbox 没有分组，所以"标题"只能是混在选项里的一个特殊值。
它不能被选中这件事做不到，所以下面钉的是"点到它会发生什么"。
"""

import ast
from pathlib import Path

import pytest

from app.services import voice

MAIN = Path(__file__).parents[2] / "webui" / "Main.py"


def webui_helpers():
    """把 Main.py 里那几个纯函数挖出来单独跑。

    和这个目录里别的 webui 测试一样：导入 Main 会把 streamlit 整个拉起来，
    而这几个函数只认 ``voice``。
    """
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    wanted = {"_is_voice_heading", "_with_language_headings", "_first_real_voice"}
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "VOICE_HEADING" for t in node.targets)
        )
    ]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert wanted <= found, f"missing from Main.py: {wanted - found}"
    space = {"voice": voice}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<webui>", "exec"), space)
    return space


HELPERS = webui_helpers()


class TestReadingTheLanguageOffAVoice:
    @pytest.mark.parametrize(
        "name,code",
        [
            ("en-US-AvaNeural-Female", "en"),
            ("zh-CN-XiaoxiaoNeural-Female", "zh"),
            ("fil-PH-AngeloNeural-Male", "fil"),
            ("pt-BR-FranciscaNeural-Female", "pt"),
        ],
    )
    def test_the_locale_prefix_is_the_language(self, name, code):
        assert voice.language_of(name) == code

    @pytest.mark.parametrize(
        "name",
        [
            "minimax:male-qn-qingse",
            "elevenlabs:abc123:Rachel",
            "chatterbox:default-Female",  # 自托管音色的名字里没有语言
            "",
            "   ",
            "NoVoice",
        ],
    )
    def test_a_name_without_a_language_has_none(self, name):
        """名字里真没有语言的时候，瞎猜一个比不猜更糟。"""
        assert voice.language_of(name) == ""

    def test_a_language_it_knows_reads_as_a_language(self):
        assert voice.language_label("en") == "English"
        assert voice.language_label("zh") == "Chinese"

    def test_one_it_does_not_know_still_says_something(self):
        """表里没有的语言显示代码，总比显示 Unknown 有用。"""
        assert voice.language_label("xx") == "XX"


class TestMaleOrFemale:
    """挑音色时第一个要问的就是这个。

    Azure 和 Chatterbox 都放在名字末尾（``-Female``）。
    """

    @pytest.mark.parametrize(
        "name,gender",
        [
            ("en-US-AvaNeural-Female", "female"),
            ("zh-CN-YunxiNeural-Male", "male"),
            ("chatterbox:default-Female", "female"),
            ("chatterbox:Thomas-Male", "male"),
        ],
    )
    def test_it_reads_the_suffix(self, name, gender):
        assert voice.gender_of(name) == gender

    @pytest.mark.parametrize(
        "name",
        [
            "minimax:male-qn-qingse",  # id 里的 male 不是后缀
            "chatterbox:my_clone",     # 没写后缀就是不知道
            "elevenlabs:abc:Rachel",
            "",
        ],
    )
    def test_it_does_not_guess(self, name):
        """``minimax:male-qn-qingse`` 里有 male 两个字母，那不是性别后缀。"""
        assert voice.gender_of(name) == ""

    def test_the_dropdown_actually_shows_it(self):
        """转成界面文案那一步在 Main.py 的闭包里，这里只能看源码。

        """
        source = MAIN.read_text(encoding="utf-8")
        assert "voice.gender_of(v)" in source
        assert 'tr("Female")' in source


class TestTheOrderOfTheGroups:
    VOICES = [
        "zh-CN-XiaoxiaoNeural-Female",
        "en-US-AvaNeural-Female",
        "de-DE-KatjaNeural-Female",
        "en-GB-SoniaNeural-Female",
    ]

    def test_the_script_s_language_comes_first(self):
        """十次里有九次，要找的就是和脚本同一种语言的那几个。"""
        groups = voice.by_language(self.VOICES, prefer="zh-CN")
        assert groups[0][0] == "zh"

    def test_a_bare_language_code_works_too(self):
        assert voice.by_language(self.VOICES, prefer="de")[0][0] == "de"

    def test_the_rest_are_alphabetical_by_name_not_by_code(self):
        """按"Chinese"排，不是按"zh"排：用户看到的是名字。"""
        groups = voice.by_language(self.VOICES, prefer="en")
        rest = [voice.language_label(code) for code, _ in groups[1:]]
        assert rest == sorted(rest)

    def test_with_no_preference_it_is_all_alphabetical(self):
        groups = voice.by_language(self.VOICES, prefer="")
        names = [voice.language_label(code) for code, _ in groups]
        assert names == sorted(names)

    def test_one_group_holds_every_locale_of_that_language(self):
        """en-US 和 en-GB 是同一门语言，不该分成两组。"""
        groups = dict(voice.by_language(self.VOICES, prefer=""))
        assert sorted(groups["en"]) == [
            "en-GB-SoniaNeural-Female",
            "en-US-AvaNeural-Female",
        ]

    def test_voices_without_a_language_go_last_in_their_own_order(self):
        mixed = [
            "en-US-AvaNeural-Female",
            "minimax:male-qn-qingse",
            "elevenlabs:abc:Rachel",
        ]
        groups = voice.by_language(mixed, prefer="")
        assert groups[-1][0] == ""
        assert groups[-1][1] == ["minimax:male-qn-qingse", "elevenlabs:abc:Rachel"]

    def test_not_one_voice_is_lost_or_duplicated(self):
        """这是分组这件事唯一不能出的错。真清单，331 个。"""
        everything = voice.get_all_azure_voices(filter_locals=None)
        regrouped = [
            name for _, names in voice.by_language(everything, prefer="en-US")
            for name in names
        ]
        assert sorted(regrouped) == sorted(everything)
        assert len(regrouped) == len(everything)


class TestTheHeadingsInTheDropdown:
    VOICES = [
        "en-US-AvaNeural-Female",
        "en-GB-SoniaNeural-Female",
        "zh-CN-XiaoxiaoNeural-Female",
    ]

    def test_every_language_gets_one_heading(self):
        options, _ = HELPERS["_with_language_headings"](self.VOICES, prefer="en-US")
        headings = [o for o in options if HELPERS["_is_voice_heading"](o)]
        assert len(headings) == 2

    def test_the_heading_comes_before_its_voices(self):
        options, _ = HELPERS["_with_language_headings"](self.VOICES, prefer="en-US")
        assert HELPERS["_is_voice_heading"](options[0])
        assert options[1:3] == ["en-GB-SoniaNeural-Female", "en-US-AvaNeural-Female"]

    def test_a_heading_cannot_be_mistaken_for_a_voice(self):
        """前缀要选一个真音色名不可能长成的样子。"""
        options, _ = HELPERS["_with_language_headings"](
            voice.get_all_azure_voices(filter_locals=None), prefer=""
        )
        real = [o for o in options if not HELPERS["_is_voice_heading"](o)]
        assert len(real) == len(voice.get_all_azure_voices(filter_locals=None))
        assert not any(o.startswith(HELPERS["VOICE_HEADING"]) for o in real)

    def test_picking_a_heading_means_that_language(self):
        """标题做不成不可选的，所以点到它必须是个有用的结果。"""
        options, heads = HELPERS["_with_language_headings"](
            self.VOICES, prefer="zh-CN"
        )
        assert heads[options[0]] == "zh-CN-XiaoxiaoNeural-Female"
        for head, first in heads.items():
            assert not HELPERS["_is_voice_heading"](first)

    def test_nothing_without_a_language_grows_a_heading(self):
        options, heads = HELPERS["_with_language_headings"](
            ["minimax:male-qn-qingse", "elevenlabs:abc:Rachel"], prefer=""
        )
        assert options == ["minimax:male-qn-qingse", "elevenlabs:abc:Rachel"]
        assert heads == {}


class TestTheDefaultIsNeverAHeading:
    def test_it_moves_past_a_heading(self):
        options = [HELPERS["VOICE_HEADING"] + "en", "en-US-AvaNeural-Female"]
        assert HELPERS["_first_real_voice"](options, 0) == 1

    def test_a_real_choice_is_left_where_it_is(self):
        options, _ = HELPERS["_with_language_headings"](
            ["en-US-AvaNeural-Female", "zh-CN-XiaoxiaoNeural-Female"], prefer="en"
        )
        at = options.index("zh-CN-XiaoxiaoNeural-Female")
        assert HELPERS["_first_real_voice"](options, at) == at

    def test_it_looks_backwards_rather_than_give_up(self):
        """最后一条是标题的时候（不该发生），也要给出一个真音色。"""
        options = ["en-US-AvaNeural-Female", HELPERS["VOICE_HEADING"] + "zh"]
        assert HELPERS["_first_real_voice"](options, 1) == 0
