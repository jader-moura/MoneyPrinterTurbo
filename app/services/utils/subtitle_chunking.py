"""一次只让屏幕上出现一句话那么多字。

为什么不改字幕文件
------------------
字幕文件里的一条就是旁白的一个分句，而它同时是**时间**的唯一来源：
``scene_manifest.narration_segments`` 读的就是这个文件，``timeline`` 按"一条
一段画面"去配每张图停多久（见 ``_windows_from_cues``：条数和段数对不上就退回
平均分，同步全丢）。所以这里一个字节都不动那个文件。

这里切的只是"同一时刻画上去多少字"。拿真正会被画上去的那套字体字号去量，
超过 ``max_lines`` 行的那一条在词的边界上切成几条，时间按字数摊开：总时长
不变，第一条的开始和最后一条的结束仍是原来那条的开始和结束。

The subtitle file is also the only source of per-line timing, so it is left
exactly as written. What is split here is only how much text is on screen at
once: a cue that wraps to more than ``max_lines`` lines is cut at word
boundaries and its duration shared out by character count.
"""

import re

# 中日韩和韩文音节：这些字之间没有空格，断行只能断在字与字之间。半角片假名
# 和兼容汉字一并算进来，不然日文字幕会被当成一个长得没有边界的"词"。
#
# CJK and Hangul: no spaces between characters, so a break can only fall
# between two characters.
_CJK = (
    "⺀-鿿"  # CJK radicals, kana, CJK unified ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "･-ﾟ"  # halfwidth katakana
)

# 一个"词"：要么是一个 CJK 字，要么是一串非空白的字符。前面的空白跟着它一起
# 被捕获，拼回去时原样还给它——不然英文里的空格会丢，中英混排的空格会多。
_TOKEN = re.compile(rf"\s*(?:[{_CJK}]|[^\s{_CJK}]+)")

_CJK_EDGE = re.compile(rf"[{_CJK}]")

# 屏幕上最多几行。两行是字幕的老规矩：一眼能读完，又不至于半句话一闪而过。
DEFAULT_MAX_LINES = 2

# 一条至少停多久。念得特别快的那几句切开后会短到看不清，短过这个数就并回去
# ——读不完的两条比一条长的更糟。
DEFAULT_MIN_SECONDS = 0.6


def split_long_cues(
    cues,
    line_count,
    *,
    max_lines=DEFAULT_MAX_LINES,
    min_seconds=DEFAULT_MIN_SECONDS,
):
    """把太长的那几条字幕切成几条短的，形状和进来时一样。

    ``cues``       ``[((start, end), text), ...]``，就是 ``moviepy_subtitles``
                   返回的那个东西。
    ``line_count`` 给一段字，回答它在屏幕上会占几行。由渲染那边注入，因为只
                   有那边知道字体、字号和这条片子多宽。
    ``max_lines``  超过几行才切。0 或负数表示不切（和这个功能上线前一样）。

    量不出来（字体读不出来之类）的时候一律当成"放得下"，也就是不切：字幕条
    数不该因为量了一下就变，宁可长一点也不能因此少画一条。
    """
    chunked = []
    for cue in cues:
        timing, text = cue[0], cue[1]
        chunked.extend(
            _split_cue(timing, text, line_count, max_lines, min_seconds)
        )
    return chunked


def _split_cue(timing, text, line_count, max_lines, min_seconds):
    start = float(timing[0])
    end = float(timing[1])
    original = "" if text is None else str(text)
    if max_lines <= 0 or not original.strip() or end <= start:
        return [((start, end), original)]
    if _lines(line_count, original) <= max_lines:
        # 放得下就原样留着，连空白都不碰：绝大多数条走的是这条路。
        return [((start, end), original)]

    tokens = _TOKEN.findall(" ".join(original.split()))
    greedy = _pack(tokens, line_count, max_lines)
    if len(greedy) < 2:
        # 一个词自己就超了（超长的网址、没有空格的一长串）。切不动，留着。
        return [((start, end), original)]
    # 贪心装会把最后一条剩成一两个词（"……in the sky"），读起来像断了气。知道
    # 要分几条之后按字数重新摊平，装不下就还是用贪心那份。
    pieces = _balance(tokens, len(greedy), line_count, max_lines) or greedy
    return _spread(pieces, start, end, min_seconds)


def _lines(line_count, text):
    try:
        return int(line_count(text))
    except Exception:
        return 1


def _join(tokens):
    return "".join(tokens).strip()


def _glue(left, right):
    """并回去的时候中间要不要留空格：看接缝两边是不是 CJK。"""
    if not left:
        return right
    if not right:
        return left
    if _CJK_EDGE.match(left[-1]) or _CJK_EDGE.match(right[0]):
        return left + right
    return left + " " + right


def _pack(tokens, line_count, max_lines):
    """一个词一个词地装，装到再加一个就超行为止。"""
    pieces = []
    current = []
    for token in tokens:
        if current and _lines(line_count, _join(current + [token])) > max_lines:
            pieces.append(_join(current))
            current = [token]
        else:
            current.append(token)
    if current:
        pieces.append(_join(current))
    return [piece for piece in pieces if piece]


def _balance(tokens, count, line_count, max_lines):
    """照 ``count`` 条摊平，每条字数差不多。摊不出来就返回 ``None``。

    和贪心装比，差别是"一半一半"而不是"满满的两行 + 一个词"。后面每一条都至
    少要留得下一个词，否则宁可不摊。
    """
    if count < 2 or len(tokens) < count:
        return None
    weights = [len(token.strip()) for token in tokens]
    total = sum(weights)
    if total <= 0:
        return None

    pieces = []
    current = []
    carried = 0
    for index, token in enumerate(tokens):
        current.append(token)
        carried += weights[index]
        left = len(tokens) - index - 1
        if len(pieces) + 1 >= count or left <= 0:
            continue
        if left < count - len(pieces) - 1:
            continue
        if carried >= total * (len(pieces) + 1) / count:
            pieces.append(_join(current))
            current = []
    if current:
        pieces.append(_join(current))

    if len(pieces) != count or not all(pieces):
        return None
    if any(_lines(line_count, piece) > max_lines for piece in pieces):
        return None
    return pieces


def _spread(pieces, start, end, min_seconds):
    """时间按字数摊给每一条。总时长不变，首尾两头也不动。"""
    weights = [max(1, len(piece)) for piece in pieces]
    pieces, weights = _merge_too_short(
        list(pieces), weights, end - start, min_seconds
    )
    total = sum(weights)
    chunked = []
    carried = 0
    last = len(pieces) - 1
    for index, piece in enumerate(pieces):
        begin = start + (end - start) * carried / total
        carried += weights[index]
        stop = end if index == last else start + (end - start) * carried / total
        chunked.append(((begin, stop), piece))
    return chunked


def _merge_too_short(pieces, weights, duration, min_seconds):
    """短到看不清的那条并回邻居。并出来可能超行，但读不完更糟。"""
    while len(pieces) > 1:
        total = sum(weights)
        shortest = min(range(len(pieces)), key=lambda at: weights[at])
        if duration * weights[shortest] / total >= min_seconds:
            break
        before = shortest - 1
        after = shortest + 1
        if before < 0 or (after < len(pieces) and weights[after] <= weights[before]):
            neighbour = after
        else:
            neighbour = before
        first, second = sorted((shortest, neighbour))
        pieces[first : second + 1] = [_glue(pieces[first], pieces[second])]
        weights[first : second + 1] = [weights[first] + weights[second]]
    return pieces, weights
