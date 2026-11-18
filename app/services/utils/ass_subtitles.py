"""把字幕写成 ASS，交给 ffmpeg 的 libass 烧进画面。

为什么不用原来那条路
--------------------
原来的字幕是 MoviePy 一条一条画成图层再合成的（``video.generate_video``）。
那条路没有动：它仍然是默认的渲染方式。这里是给"成片精修"那一遍用的
（``app/services/finishing.py``）——那一遍整个在 ffmpeg 里做，字幕也就得是
ffmpeg 能烧的格式。

三种显示方式
------------
* ``sentence``      一条字幕一行，和字幕文件里的一条对应；
* ``word_by_word``  一次一个词；
* ``highlight``     几个词一组，念到哪个词哪个词变色（新加的）。

词的时间从哪来
--------------
字幕文件里的一条是旁白的一个分句，起止时间是准的（配音是一句一句合成再
拼起来的）。分句里每个词的时间按词的长短摊开——不用另外跑一遍语音识别，
拼写也直接是脚本里的拼写。要更准的词级时间，可以把识别出来的词和脚本的词
对齐（``align_spoken_words``）：时间用识别的，拼写用脚本的。

Subtitles as an ASS file for ffmpeg's libass. Word times come from the cue
file: a cue is one spoken clause with exact start and end, and the words
inside share that time by length. Spelling is therefore the script's own.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

Cue = Tuple[Tuple[float, float], str]

DISPLAY_MODES = ("sentence", "word_by_word", "highlight")

# 一组最多几个词；两个词之间停顿超过这么久就另起一组。
GROUP_MAX_WORDS = 4
GROUP_GAP_SECONDS = 0.45
_BREAK_AFTER = re.compile(r"[.,!?;:…。，！？；：]['\"”’)\]]*$")
_TRIM = re.compile(r"[.,;:。，；：]+$")


@dataclass
class Word:
    start: float
    end: float
    text: str
    # 这个词是不是它那一条字幕的最后一个词。
    ends_cue: bool = False


@dataclass
class Style:
    """字幕长什么样。尺寸都是成片分辨率下的像素。"""

    font: str = "Arial"
    size: float = 76
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: float = 5
    shadow: float = 3
    # ""（没有底框）或者 "#RRGGBB"。
    box_color: str = ""
    # bottom / top / center / two_thirds_bottom / custom
    position: str = "bottom"
    custom_position: float = 70.0
    highlight_color: str = "#FFD73F"
    uppercase: bool = False
    pop: bool = False
    bold: bool = True


@dataclass
class TitleCard:
    """左上角的一张标题卡：一行大字，一行小字。"""

    start: float
    end: float
    title: str
    subtitle: str = ""


@dataclass
class _Sheet:
    width: int
    height: int
    style: Style
    events: List[str] = field(default_factory=list)


def ass_time(seconds: float) -> str:
    """``h:mm:ss.cc``。按厘秒取整之后再拆，免得出现 ``0:00:60.00``。"""
    total = max(0, int(round(float(seconds) * 100)))
    hours, rest = divmod(total, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def ass_colour(value: str, alpha: int = 0) -> str:
    """``#RRGGBB`` → ``&HAABBGGRR``。认不出来的颜色当白色。"""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text[:6] or ""):
        text = "FFFFFF"
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def _clean(text: str) -> str:
    """ASS 里花括号是指令、反斜杠是转义：字幕文字里不能有。"""
    text = str(text or "").replace("\\", "/").replace("{", "(").replace("}", ")")
    return " ".join(text.split())


def words_from_cues(cues: Iterable[Cue]) -> List[Word]:
    """把一条条字幕拆成带时间的词。

    一条里的时间按词长摊：每个词算"字母数 + 2"，那个 2 是词与词之间的那点
    间隔——不加它，"a" 这样的短词只分到一两帧。
    """
    words: List[Word] = []
    for (start, end), text in cues:
        tokens = _clean(text).split()
        if not tokens:
            continue
        start, end = float(start), float(end)
        weights = [len(re.sub(r"\W", "", token)) + 2 for token in tokens]
        total = float(sum(weights)) or 1.0
        at = start
        for position, (token, weight) in enumerate(zip(tokens, weights)):
            until = end if position == len(tokens) - 1 else at + (end - start) * weight / total
            words.append(Word(at, until, token, position == len(tokens) - 1))
            at = until
    return words


def _key(token: str) -> str:
    return re.sub(r"[\W_]+", "", str(token).lower())


def align_spoken_words(
    script_words: Sequence[Word], spoken: Sequence[Tuple[float, float, str]]
) -> List[Word]:
    """时间用识别出来的，拼写用脚本的。

    语音识别会把人名地名拼错（"Kalmogorova"、"Kalat Siakal"），但它给的时间
    是准的；脚本的拼写是对的，时间是估的。把两串词对齐：对得上的词拿识别的
    时间，对不上的那几个留着估出来的时间。

    Timing from the recogniser, spelling from the script. Words the two
    sequences agree on take the recognised time; the rest keep their estimate.
    """
    if not script_words or not spoken:
        return list(script_words)
    ours = [_key(word.text) for word in script_words]
    theirs = [_key(text) for _, _, text in spoken]
    matcher = difflib.SequenceMatcher(None, ours, theirs, autojunk=False)
    timed = [Word(w.start, w.end, w.text, w.ends_cue) for w in script_words]
    heard = [False] * len(timed)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(a1 - a0):
                start, end, _ = spoken[b0 + offset]
                timed[a0 + offset].start = float(start)
                timed[a0 + offset].end = float(end)
                heard[a0 + offset] = True
        elif tag == "replace" and a1 > a0 and b1 > b0:
            for at in range(a0, a1):
                heard[at] = True
            # 拼错的那一段：整段的起止用识别的，里面按词长摊。
            start, end = float(spoken[b0][0]), float(spoken[b1 - 1][1])
            weights = [len(ours[at]) + 2 for at in range(a0, a1)]
            total = float(sum(weights)) or 1.0
            at_time = start
            for at, weight in zip(range(a0, a1), weights):
                timed[at].start = at_time
                at_time += (end - start) * weight / total
                timed[at].end = at_time
    # 识别漏掉的词（它有时整句跳过）：估出来的时间可能差好几秒，不能留着。
    # 夹在前后两个听到的词中间，按词长摊。
    # Words the recogniser skipped sit between their heard neighbours.
    # 先找被拖长的词：识别跳过一句话的时候，常把那句话的时间算在前面两三个词
    # 里的某一个头上（一个 "from" 念了四秒）。那个词截回正常长度，它后面到漏
    # 听处的词也跟着重新摊。
    # A skipped sentence's time usually lands on one of the few words before
    # it (a four-second "from"): cut that word back and re-spread what follows.
    for at in [i for i in range(1, len(timed)) if not heard[i] and heard[i - 1]]:
        for back in range(at - 1, max(at - 4, -1), -1):
            if not heard[back]:
                break
            likely = 0.35 + 0.09 * len(ours[back])
            if timed[back].end - timed[back].start > max(1.0, likely):
                timed[back].end = timed[back].start + likely
                for between in range(back + 1, at):
                    heard[between] = False
                break
    at = 0
    while at < len(timed):
        if heard[at]:
            at += 1
            continue
        until = at
        while until < len(timed) and not heard[until]:
            until += 1
        if at == 0 and until == len(timed):
            break
        length = sum(w.end - w.start for w in timed[at:until])
        start = timed[at - 1].end if at > 0 else max(0.0, timed[until].start - length)
        end = timed[until].start if until < len(timed) else start + length
        end = max(end, start)
        first = at
        if at > 0 and end - start < 0.12 * (until - at):
            # 没给漏掉的词留地方：识别把前一个词拖过了整段漏听的话。
            # 那个词的结尾不可信，连它一起重新摊。
            # No room left: the recogniser stretched the word before across
            # what it skipped, so that word is spread again with the rest.
            first = at - 1
            start = timed[first].start
        weights = [len(ours[i]) + 2 for i in range(first, until)]
        total = float(sum(weights)) or 1.0
        at_time = start
        for i, weight in zip(range(first, until), weights):
            timed[i].start = at_time
            at_time += (end - start) * weight / total
            timed[i].end = at_time
        at = until
    # 时间只能往前走：对齐出来的个别词可能比前一个词早。
    floor = 0.0
    for word in timed:
        word.start = max(word.start, floor)
        word.end = max(word.end, word.start + 0.01)
        floor = word.start
    return timed


def group_words(
    words: Sequence[Word], *, max_words: int = GROUP_MAX_WORDS, max_chars: int = 22
) -> List[List[Word]]:
    """几个词一组：满了、碰到标点、一条字幕念完、或者停顿了，就另起一组。"""
    groups: List[List[Word]] = []
    current: List[Word] = []
    for position, word in enumerate(words):
        if current and sum(len(w.text) for w in current) + len(word.text) > max_chars:
            groups.append(current)
            current = []
        current.append(word)
        following = words[position + 1] if position + 1 < len(words) else None
        gap = (following.start - word.end) if following else 9.0
        if (
            len(current) >= max_words
            or word.ends_cue
            or _BREAK_AFTER.search(word.text)
            or gap > GROUP_GAP_SECONDS
        ):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def chars_per_line(width: int, size: float) -> int:
    """这个字号下，一行大概放得下几个字符（留出两边的边距）。"""
    return max(8, int(0.86 * width / max(1.0, 0.55 * float(size))))


def split_long_cues(cues: Iterable[Cue], limit: int) -> List[Cue]:
    """一条字幕太长就切成几条，时间按字数摊。``limit`` 是最多多少个字符。"""
    pieces: List[Cue] = []
    for (start, end), text in cues:
        tokens = _clean(text).split()
        if not tokens:
            continue
        if limit <= 0 or len(" ".join(tokens)) <= limit:
            pieces.append(((float(start), float(end)), " ".join(tokens)))
            continue
        parts: List[List[str]] = [[]]
        for token in tokens:
            if parts[-1] and len(" ".join(parts[-1] + [token])) > limit:
                parts.append([])
            parts[-1].append(token)
        total = float(sum(len(" ".join(part)) for part in parts)) or 1.0
        at = float(start)
        for position, part in enumerate(parts):
            share = len(" ".join(part)) / total
            until = float(end) if position == len(parts) - 1 else at + (end - start) * share
            pieces.append(((at, until), " ".join(part)))
            at = until
    return pieces


def find_phrase(words: Sequence[Word], phrase: str) -> Optional[float]:
    """这句话在旁白里是什么时候开始念的；找不到返回 ``None``。"""
    wanted = [key for key in (_key(token) for token in str(phrase or "").split()) if key]
    if not wanted:
        return None
    keys = [_key(word.text) for word in words]
    for at in range(len(keys) - len(wanted) + 1):
        if keys[at:at + len(wanted)] == wanted:
            return words[at].start
    return None


def _placement(sheet: _Sheet) -> Tuple[int, int, str]:
    """``(对齐方式, 垂直边距, 每条都要带的位置指令)``。"""
    style, width, height = sheet.style, sheet.width, sheet.height
    position = str(style.position or "bottom")
    if position == "top":
        return 8, int(height * 0.06), ""
    if position == "center":
        return 5, 0, ""
    if position in ("two_thirds_bottom", "two_thirds", "2/3_bottom"):
        return 5, 0, f"{{\\pos({width // 2},{int(height / 3)})}}"
    if position == "custom":
        share = min(max(float(style.custom_position or 0.0), 4.0), 96.0) / 100.0
        return 5, 0, f"{{\\pos({width // 2},{int(height * share)})}}"
    return 2, int(height * 0.08), ""


def _header(sheet: _Sheet) -> str:
    style = sheet.style
    scale = min(sheet.width, sheet.height) / 1080.0
    alignment, margin_v, _ = _placement(sheet)
    margin_h = int(sheet.width * 0.04)
    primary = ass_colour(style.color)
    outline_colour = ass_colour(style.box_color or style.outline_color)
    # BorderStyle 3 是不透明底框：框的颜色取自 OutlineColour。
    border_style = 3 if style.box_color else 1
    bold = -1 if style.bold else 0
    rows = [
        f"Style: Sub,{style.font},{style.size:g},{primary},{primary},{outline_colour},"
        f"&H96000000,{bold},0,0,0,100,100,1,0,{border_style},{style.outline:g},"
        f"{style.shadow:g},{alignment},{margin_h},{margin_h},{margin_v},1",
        f"Style: CardTitle,{style.font},{54 * scale:g},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,{3 * scale:g},0,1,0,{2 * scale:g},7,0,0,0,1",
        f"Style: CardSub,{style.font},{34 * scale:g},&H00E6E6E6,&H00FFFFFF,&H00000000,"
        f"&H80000000,0,0,0,0,100,100,{2 * scale:g},0,1,0,{2 * scale:g},7,0,0,0,1",
        f"Style: CardBar,{style.font},10,{ass_colour(style.highlight_color)},&H00FFFFFF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
    ]
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {sheet.width}\n"
        f"PlayResY: {sheet.height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(rows)
        + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )


_POP = "{\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)}"


def _shown(sheet: _Sheet, text: str) -> str:
    return text.upper() if sheet.style.uppercase else text


def _line(sheet: _Sheet, start: float, end: float, text: str, layer: int = 0,
          style: str = "Sub") -> None:
    if end - start < 0.01:
        return
    sheet.events.append(
        f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"
    )


def _sentences(sheet: _Sheet, cues: Sequence[Cue], max_lines: int) -> None:
    _, _, where = _placement(sheet)
    limit = chars_per_line(sheet.width, sheet.style.size) * max_lines if max_lines > 0 else 0
    for (start, end), text in split_long_cues(cues, limit):
        pop = _POP if sheet.style.pop else ""
        _line(sheet, start, end, f"{where}{pop}{_shown(sheet, text)}")


def _single_words(sheet: _Sheet, words: Sequence[Word]) -> None:
    _, _, where = _placement(sheet)
    for position, word in enumerate(words):
        following = words[position + 1] if position + 1 < len(words) else None
        # 一个词一直留到下一个词出来，除非中间停顿了。
        end = word.end
        if following and following.start - word.end <= GROUP_GAP_SECONDS:
            end = following.start
        pop = _POP if sheet.style.pop else ""
        _line(sheet, word.start, end, f"{where}{pop}{_shown(sheet, _TRIM.sub('', word.text))}")


def _highlighted(sheet: _Sheet, words: Sequence[Word]) -> None:
    """几个词一组，念到的那个词换颜色。每个词一条事件，整组的字都在。"""
    _, _, where = _placement(sheet)
    base = ass_colour(sheet.style.color)[4:]
    lit = ass_colour(sheet.style.highlight_color)[4:]
    # 一组的宽度不超过半个画面多一点：不然一眼看不完。
    limit = max(10, chars_per_line(sheet.width, sheet.style.size) * 55 // 100)
    for group in group_words(words, max_chars=limit):
        tokens = [_shown(sheet, _TRIM.sub("", word.text)) for word in group]
        group_end = group[-1].end
        for position, word in enumerate(group):
            start = word.start if position else group[0].start
            end = group[position + 1].start if position + 1 < len(group) else group_end
            text = " ".join(
                f"{{\\c&H{lit}&}}{token}{{\\c&H{base}&}}" if at == position else token
                for at, token in enumerate(tokens)
            )
            pop = _POP if (position == 0 and sheet.style.pop) else ""
            _line(sheet, start, end, f"{where}{pop}{text}")


def _cards(sheet: _Sheet, cards: Sequence[TitleCard]) -> None:
    """左上角滑进来的标题卡：一条竖线，一行大字，一行小字。"""
    scale = min(sheet.width, sheet.height) / 1080.0
    left, top = int(80 * scale), int(92 * scale)
    bar_w, bar_h = int(8 * scale), int(112 * scale)
    text_x, slide = left + int(30 * scale), int(20 * scale)
    sub_y = top + int(68 * scale)
    for card in cards:
        title = _clean(card.title).upper()
        if not title:
            continue
        _line(sheet, card.start, card.end,
              f"{{\\fad(400,400)\\pos({left},{top})\\p1}}m 0 0 l {bar_w} 0 {bar_w} {bar_h} "
              f"0 {bar_h}{{\\p0}}", 1, "CardBar")
        _line(sheet, card.start, card.end,
              f"{{\\fad(400,400)\\move({text_x - slide},{top - 2},{text_x},{top - 2},0,500)}}"
              f"{title}", 1, "CardTitle")
        subtitle = _clean(card.subtitle)
        if subtitle:
            _line(sheet, card.start, card.end,
                  f"{{\\fad(400,400)\\move({text_x - slide},{sub_y},{text_x},{sub_y},0,500)}}"
                  f"{subtitle}", 1, "CardSub")


def build(
    cues: Sequence[Cue],
    *,
    width: int,
    height: int,
    style: Style,
    mode: str = "sentence",
    max_lines: int = 2,
    words: Optional[Sequence[Word]] = None,
    title_cards: Sequence[TitleCard] = (),
) -> str:
    """整份 ASS 文件的内容。``cues`` 为空时只有标题卡（如果有的话）。

    ``words`` 不给就从 ``cues`` 里按词长摊出来；给了（比如对齐过语音识别的）
    就用给的。
    """
    sheet = _Sheet(int(width), int(height), style)
    if cues:
        if mode == "sentence":
            _sentences(sheet, cues, int(max_lines or 0))
        else:
            timed = list(words) if words is not None else words_from_cues(cues)
            if mode == "word_by_word":
                _single_words(sheet, timed)
            else:
                _highlighted(sheet, timed)
    _cards(sheet, title_cards)
    return _header(sheet) + "\n".join(sheet.events) + "\n"
