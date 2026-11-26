"""在外面写好的分镜：每一段念什么、画什么、哪几段照着照片画。

平常这三件事是这里的模型做的：把脚本断成一段一段、给每一段写提示词、挑出
点了真人真地名的那几段去找照片。分镜就是把这三件事的**结果**直接给进来——
比如让 Claude Code 照着 ``.claude/skills/scene-plan`` 写一份——这里只管照着
念、照着画，不再问模型。

A scene plan written outside the app: what each scene says, what it shows, and
which scenes are drawn from a real photo. Normally the app's own model does
those three things; a plan hands in their result, and the app narrates and
draws from it without asking a model.

格式 / Format::

    {
      "subject": "The Green River Killer",
      "scenes": [
        {"narration": "One or two spoken sentences.",
         "prompt": "What the picture shows, written to stand alone.",
         "image": "text"},
        {"narration": "...", "prompt": "...",
         "image": "photo", "photo_kind": "person",
         "photo_name": "Gary Ridgway",
         "photo_search": "Gary Ridgway 2001 arrest"}
      ]
    }

分镜还可以带"导演"的那几样（都可以不写）：片名打在哪一段上、哪几段出标题
卡、哪一句后面停一下、哪一句上落一记重音、哪几段不要配乐、哪几段垫环境声。
停顿是配音时真的停；其余几样由成片精修（``finishing.py``）照着做。

A plan may also direct the edit; every item is optional::

    {
      "title": {"text": "John Wayne Gacy",
                "tagline": "The house on Summerdale Avenue", "scene": 12},
      "scenes": [
        {"narration": "He starts to dig.", "prompt": "...",
         "pause_after": 1.0,            # silence after this line, in seconds
         "impact": "end",               # a low hit: "start" or "end" of the line
         "music": "off",                # no music under this scene
         "ambience": "room",            # "wind" or "room" under this scene
         "card": {"title": "December 1978", "subtitle": "West Summerdale Avenue"},
         "timeline": {"date": "Dec 1978", "label": "The search of the house"}}
      ]
    }

The pause is recorded into the narration; the rest is carried out by the
finishing pass (``finishing.py``), so it needs a Finish look to be set.

``timeline`` puts a date on a strip that stays on screen: an axis from the
first date to the last, with a marker that moves to each event as its scene
is spoken. The strip stays up from one event to the next, through the scenes
between them, until the end of a scene marked ``"timeline": "end"`` (or of the
last event's scene when none is).

``timeline`` 是一条留在画面上的时间轴：从第一个日期到最后一个，念到哪件事，
标记就挪到哪儿。它从一件事留到下一件事，直到写了 ``"timeline": "end"`` 的那
一段念完（没有写的话，到最后一件事那一段念完）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, List, Optional

from app.services import locator_map
from app.utils import utils

#: 一份分镜最多多少段。一段四五秒，这已经是半个多小时的片子。
MAX_SCENES = 600
#: 照着照片画的那几段，照片上是什么。
PHOTO_KINDS = ("person", "place", "thing")
#: 分镜点了名的段在"值不值得找照片"上的分数：满分，一定挑中。
PHOTO_SCORE = 10

_IMAGE_TEXT = ("", "text", "text-to-image", "text2img", "txt2img", "t2i")
_IMAGE_PHOTO = ("photo", "image", "image-to-image", "img2img", "i2i")

#: 垫在画面底下的环境声有哪几种（``sound_design.py`` 里合成）。
AMBIENCES = ("wind", "room")
#: 一句后面最多停多久（秒）。再长就不是停顿，是出了故障。
MAX_PAUSE_SECONDS = 4.0
MIN_PAUSE_SECONDS = 0.2
_CARD_TITLE_CHARS = 40
_CARD_SUBTITLE_CHARS = 60
_STAT_FIGURE_CHARS = 16
_STAT_CAPTION_CHARS = 48
_TIMELINE_DATE_CHARS = 14
_TIMELINE_LABEL_CHARS = 36


class ScenePlanError(ValueError):
    """分镜读不懂，或者哪一段缺东西。说清楚是第几段、缺什么。"""


def _lines(value: Any, number: int, what: str, first: str, second: str) -> dict:
    """``{"title", "subtitle"}`` 这种两行字：给对象，或者 ``"大字 | 小字"``。"""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split("|")]
        value = {first: parts[0], second: parts[1] if len(parts) > 1 else ""}
    if not isinstance(value, dict):
        raise ScenePlanError(
            f'{what} is an object like {{"{first}": "...", "{second}": "..."}}'
            if not number else
            f'scene {number}: {what} is an object like '
            f'{{"{first}": "...", "{second}": "..."}}'
        )
    return {first: _words(value.get(first)), second: _words(value.get(second))}


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec")
#: 没写月份、只写了"初 / 中 / 末"或者季节的日期，落在这一年的哪儿（0 到 1）。
_PARTS_OF_A_YEAR = (
    ("early", 0.15), ("mid", 0.5), ("late", 0.85), ("end of", 0.95),
    ("winter", 0.08), ("spring", 0.3), ("summer", 0.55), ("autumn", 0.8),
    ("fall", 0.8),
)


def timeline_when(date: Any) -> Optional[float]:
    """A date as a number of years, so it can be placed on an axis:
    ``"Nov 12, 1988"`` is about 1988.86, ``"late 1960s"`` about 1968.5.
    ``None`` when it names no year.

    一条日期落在时间轴的哪儿。要有四位数的年份；月份、日子、"late"、季节会
    把它往这一年里面挪。``1970s`` 这样的年代落在它的中间。
    """
    text = _words(date).lower()
    year = re.search(r"(?<!\d)(\d{4})(s)?(?!\d)", text)
    if not year:
        return None
    start, decade = float(year.group(1)), bool(year.group(2))
    rest = text[:year.start()] + " " + text[year.end():]
    share = None
    for position, name in enumerate(_MONTHS):
        if re.search(rf"\b{name}", rest):
            day = re.search(r"(?<!\d)(\d{1,2})(?!\d)", rest)
            share = (position + (min(int(day.group(1)), 31) - 1) / 31.0
                     if day else position + 0.5) / 12.0
            break
    if share is None:
        for word, part in _PARTS_OF_A_YEAR:
            if re.search(rf"\b{word}\b", rest):
                share = part
                break
    if decade:
        return start + 10.0 * (0.5 if share is None else share)
    return start + (0.0 if share is None else share)


def _timeline(value: Any, number: int) -> dict:
    """一段上的时间轴项：一件事（``{"date", "label"}``），或者 ``"end"``。"""
    if isinstance(value, str) and value.strip().lower() in ("end", "off", "stop"):
        return {"end": True}
    ends = isinstance(value, dict) and bool(value.get("end"))
    if ends and not _words(value.get("date")):
        return {"end": True}
    event = _lines(value, number, "timeline", "date", "label")
    if timeline_when(event["date"]) is None:
        raise ScenePlanError(
            f"scene {number}: the timeline date is {event['date']!r}; it needs a "
            'year in figures, like "1978", "Nov 12, 1988" or "late 1960s" '
            '(or write "end" to take the timeline off after this scene)'
        )
    if len(event["date"]) > _TIMELINE_DATE_CHARS:
        raise ScenePlanError(
            f"scene {number}: the timeline date is {len(event['date'])} "
            f"characters; the most is {_TIMELINE_DATE_CHARS} - shorten the month"
        )
    event["label"] = event["label"][:_TIMELINE_LABEL_CHARS]
    if ends:
        event["end"] = True
    return event


def _direction(entry: dict, number: int) -> dict:
    """这一段的"导演"项。没写的不出现在结果里；写错了说是第几段、错在哪。"""
    found: dict = {}

    pause = entry.get("pause_after")
    if pause not in (None, "", 0, False):
        try:
            seconds = float(str(pause).strip().lower().rstrip("s").strip())
        except ValueError:
            raise ScenePlanError(
                f"scene {number}: pause_after is {pause!r}; it is a number of "
                "seconds, like 1 or 0.6"
            ) from None
        if not MIN_PAUSE_SECONDS <= seconds <= MAX_PAUSE_SECONDS:
            raise ScenePlanError(
                f"scene {number}: pause_after is {seconds:g} seconds; it is "
                f"between {MIN_PAUSE_SECONDS:g} and {MAX_PAUSE_SECONDS:g}"
            )
        found["pause_after"] = round(seconds, 2)

    impact = entry.get("impact")
    if impact not in (None, "", False):
        where = "end" if impact is True else _words(impact).lower()
        if where not in ("start", "end"):
            raise ScenePlanError(
                f'scene {number}: impact is {impact!r}; it is "start" or "end" '
                "(of this scene's narration)"
            )
        found["impact"] = where

    music = entry.get("music")
    if music not in (None, "", True):
        word = "off" if music is False else _words(music).lower()
        if word in ("off", "out", "none", "silent"):
            found["music"] = "off"
        elif word != "on":
            raise ScenePlanError(
                f'scene {number}: music is {music!r}; write "off" to drop the '
                "music under this scene, or leave it out"
            )

    ambience = _words(entry.get("ambience")).lower()
    if ambience and ambience != "none":
        if ambience not in AMBIENCES:
            raise ScenePlanError(
                f"scene {number}: ambience is {ambience!r}; it is one of "
                f"{', '.join(AMBIENCES)}"
            )
        found["ambience"] = ambience

    if entry.get("card") not in (None, "", False):
        card = _lines(entry["card"], number, "card", "title", "subtitle")
        if not card["title"]:
            raise ScenePlanError(f"scene {number}: the card has no title")
        if len(card["title"]) > _CARD_TITLE_CHARS:
            raise ScenePlanError(
                f"scene {number}: the card's title is {len(card['title'])} "
                f"characters; the most is {_CARD_TITLE_CHARS}"
            )
        card["subtitle"] = card["subtitle"][:_CARD_SUBTITLE_CHARS]
        found["card"] = card

    # A figure the narration has just said, large in the lower left with a
    # line under it saying what it counts: "7 BODIES / found in her garden".
    if entry.get("stat") not in (None, "", False):
        stat = _lines(entry["stat"], number, "stat", "figure", "caption")
        if not stat["figure"]:
            raise ScenePlanError(
                f'scene {number}: the stat has no figure; write it like '
                '"7 bodies | Found in her garden in three days"'
            )
        for key, most in (("figure", _STAT_FIGURE_CHARS), ("caption", _STAT_CAPTION_CHARS)):
            if len(stat[key]) > most:
                raise ScenePlanError(
                    f"scene {number}: the stat's {key} is {len(stat[key])} "
                    f"characters; the most is {most}"
                )
        found["stat"] = stat

    timeline = entry.get("timeline")
    if timeline not in (None, "", False):
        found["timeline"] = _timeline(timeline, number)

    # 这一段的画面是一张定位地图（``locator_map``），不是画出来的图。
    if entry.get("map") not in (None, "", False):
        try:
            found["map"] = locator_map.checked(entry["map"], number)
        except locator_map.MapError as exc:
            raise ScenePlanError(str(exc)) from None
    return found


def _title(data: dict, scenes: List[dict]) -> None:
    """片名：打在第几段上。存在那一段的导演项里，跟着那一段的旁白走。"""
    if data.get("title") in (None, "", False):
        return
    raw = data["title"]
    title = _lines(raw, 0, "title", "text", "tagline")
    if not title["text"]:
        raise ScenePlanError('the title has no "text"')
    if len(title["text"]) > _CARD_TITLE_CHARS:
        raise ScenePlanError(
            f"the title is {len(title['text'])} characters; the most is "
            f"{_CARD_TITLE_CHARS}"
        )
    title["tagline"] = title["tagline"][:_CARD_SUBTITLE_CHARS]
    # "on": "black" - the title alone on a black frame, in the silence before
    # its scene, instead of over that scene's darkened picture.
    over = _words(raw.get("on")).lower() if isinstance(raw, dict) else ""
    if over in ("black", "a black frame"):
        title["on"] = "black"
    elif over not in ("", "picture", "the picture"):
        raise ScenePlanError(
            f'the title\'s "on" is {over!r}; it is "black" (alone on a black '
            'frame) or "picture" (over its scene, the usual)'
        )
    at = raw.get("scene", 1) if isinstance(raw, dict) else 1
    try:
        at = int(at)
    except (TypeError, ValueError):
        raise ScenePlanError(
            f'the title\'s "scene" is {at!r}; it is the number of the scene '
            "the title appears over, counting from 1"
        ) from None
    if not 1 <= at <= len(scenes):
        raise ScenePlanError(
            f"the title is over scene {at}, and the plan has {len(scenes)} scenes"
        )
    if scenes[at - 1]["direction"].get("card"):
        raise ScenePlanError(
            f"scene {at} has both the title and a card; put the card on "
            "another scene"
        )
    if scenes[at - 1]["direction"].get("stat"):
        raise ScenePlanError(
            f"scene {at} has both the title and a stat; put the stat on "
            "another scene"
        )
    scenes[at - 1]["direction"]["title"] = title


def _strip_fence(text: str) -> str:
    """模型爱把 JSON 包在 ```json 里：剥掉。"""
    match = re.match(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", text, re.S)
    return match.group(1) if match else text


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def look(data: Any) -> str:
    """The finish look a plan or a direction file asks for with a top-level
    ``"look"`` (``"cinematic"``, ``"cold_documentary"``...); ``""`` when it
    does not say, which leaves the choice to the form or the editor."""
    name = _words(data.get("look")) if isinstance(data, dict) else ""
    name = name.strip().lower().replace(" ", "_").replace("-", "_")
    if not name:
        return ""
    from app.services import finishing  # it imports this module

    if name not in finishing.LOOKS:
        raise ScenePlanError(
            f'"look": {name!r} is not a finish look; it is one of '
            f"{', '.join(finishing.LOOKS)}"
        )
    return name


def redirected_look(text: str) -> str:
    """The ``"look"`` of a direction file (see :func:`redirected`)."""
    try:
        data = json.loads(_strip_fence(str(text or "")).strip())
    except ValueError:
        return ""
    return look(data)


def parse(text: str) -> dict:
    """读一份分镜，返回 ``{"subject", "scenes"}``；每一段是
    ``{"narration", "prompt", "photo"}``，``photo`` 是 ``None`` 或
    ``{"kind", "name", "search"}``。读不懂就报 :class:`ScenePlanError`。
    """
    raw = _strip_fence(str(text or "")).strip()
    if not raw:
        raise ScenePlanError("the scene plan is empty")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ScenePlanError(
            f"the scene plan is not valid JSON ({exc}); paste the whole file, "
            "from the first { to the last }"
        ) from exc
    if isinstance(data, list):
        data = {"scenes": data}
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise ScenePlanError(
            'the scene plan needs a "scenes" list; see .claude/skills/scene-plan'
        )
    listed = data["scenes"]
    if not listed:
        raise ScenePlanError("the scene plan has no scenes")
    if len(listed) > MAX_SCENES:
        raise ScenePlanError(
            f"the scene plan has {len(listed)} scenes; the most is {MAX_SCENES}"
        )

    scenes: List[dict] = []
    for number, entry in enumerate(listed, start=1):
        if not isinstance(entry, dict):
            raise ScenePlanError(f"scene {number} is not an object")
        narration = _words(entry.get("narration"))
        if not narration:
            raise ScenePlanError(f"scene {number} has no narration")
        if not utils.split_string_by_punctuations(narration):
            raise ScenePlanError(
                f"scene {number}: the narration {narration!r} has nothing to say"
            )
        direction = _direction(entry, number)
        prompt = _words(entry.get("prompt"))
        image = _words(entry.get("image")).lower()
        if image == "map":
            # ``"image": "map"`` 只是把话说明白：这一段的画面是地图。
            if "map" not in direction:
                raise ScenePlanError(
                    f'scene {number} says its image is a map but has no "map" '
                    '(lat, lon and label)'
                )
            image = "text"
        if not prompt and "map" in direction:
            # 地图不用提示词。留一句话在那儿：以后想把这一段改回画出来的图，
            # 编辑器里有东西可改。
            prompt = f"A dark documentary locator map showing {direction['map']['label']}"
        if not prompt:
            raise ScenePlanError(f"scene {number} has no prompt for its picture")
        if image in _IMAGE_PHOTO:
            kind = _words(entry.get("photo_kind")).lower() or "thing"
            if kind not in PHOTO_KINDS:
                raise ScenePlanError(
                    f"scene {number}: photo_kind is {kind!r}; it is one of "
                    f"{', '.join(PHOTO_KINDS)}"
                )
            name = _words(entry.get("photo_name"))
            search = _words(entry.get("photo_search")) or name
            if not search:
                raise ScenePlanError(
                    f"scene {number} is drawn from a photo but says neither "
                    "photo_name nor photo_search, so there is nothing to look for"
                )
            photo: Optional[dict] = {"kind": kind, "name": name or search,
                                     "search": search}
        elif image in _IMAGE_TEXT:
            photo = None
        else:
            raise ScenePlanError(
                f'scene {number}: image is {image!r}; it is "text" (drawn from '
                'the prompt) or "photo" (drawn from a real photo)'
            )
        if photo is not None and "map" in direction:
            raise ScenePlanError(
                f"scene {number} is both a map and drawn from a photo; it is one "
                "or the other"
            )
        scenes.append({"narration": narration, "prompt": prompt, "photo": photo,
                       "direction": direction})
    _title(data, scenes)

    # 配音和字幕是按分句来的，画面按段来：每一段占几条字幕，加起来必须正好
    # 是整篇的条数，不然画面对不上声音。一段一行就是为了让这一条成立。
    whole = len(utils.split_string_by_punctuations(
        "\n".join(scene["narration"] for scene in scenes)))
    parts = sum(
        len(utils.split_string_by_punctuations(scene["narration"])) for scene in scenes
    )
    if whole != parts:
        raise ScenePlanError(
            "the scenes cannot be lined up with the narration: read as one "
            f"script they make {whole} spoken lines, scene by scene {parts}. "
            "End each scene's narration with a full stop, question mark or "
            "exclamation mark"
        )
    return {"subject": _words(data.get("subject")), "scenes": scenes,
            "look": look(data)}


def script_of(plan: dict) -> str:
    """整篇旁白：一段一行。换行是硬的断句点，所以每一段都自己成句。

    要停一下的那一段，行尾带一个 ``[pause: 1s]``：配音时照它插一段真的静音
    （``voice.tts``），字幕、关键词那几处本来就会把这种标签去掉。
    """
    lines = []
    for scene in plan["scenes"]:
        pause = (scene.get("direction") or {}).get("pause_after")
        lines.append(
            f"{scene['narration']} [pause: {pause:g}s]" if pause else scene["narration"]
        )
    return "\n".join(lines)


def prompts_of(plan: dict) -> List[str]:
    return [scene["prompt"] for scene in plan["scenes"]]


def stored(plan: dict) -> List[dict]:
    """跟着这条片子走的那一份：每段的旁白、照片和导演项。提示词另存在关键词里。"""
    kept = []
    for scene in plan["scenes"]:
        one = {"narration": scene["narration"], "photo": scene["photo"]}
        if scene.get("direction"):
            one["direction"] = dict(scene["direction"])
        kept.append(one)
    return kept


def directed(plan_scenes: Any) -> bool:
    """这份分镜自己管不管成片的"导演"：有一段写了就算管。"""
    return any(
        isinstance(scene, dict) and scene.get("direction")
        for scene in plan_scenes or []
    )


#: 给一条已经做出来的片子补导演项时，一段上能写的那几样。
_REDIRECT_FIELDS = ("card", "stat", "impact", "music", "ambience", "pause_after",
                    "map", "timeline")


def _scene_numbers(key: Any, total: int) -> List[int]:
    """``"6"``、``"6-9"``、``"6-9, 12"`` -> 段号。"""
    numbers: List[int] = []
    for part in str(key).replace("–", "-").split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", part)
        if not match:
            raise ScenePlanError(
                f'"{key}" is not a scene number; write "6", or "6-9" for a run '
                "of scenes"
            )
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if not 1 <= first <= last <= total:
            raise ScenePlanError(
                f'"{key}": the video has scenes 1 to {total}'
            )
        numbers.extend(range(first, last + 1))
    return numbers


def redirected(
    plan_scenes: List[dict], text: str, recorded: Optional[dict] = None,
) -> List[dict]:
    """给一条已经做出来的片子换一份导演项，返回新的那份分镜（原来的不动）。

    旁白已经录好了，所以这里不重写分镜，只写"哪几段上加什么"::

        {
          "look": "cinematic",
          "title": {"text": "The Cadaver Synod", "tagline": "Rome, 897", "scene": 8},
          "scenes": {
            "1": {"card": "January 897 | Rome"},
            "1-7": {"ambience": "room"},
            "6-7": {"music": "off"},
            "48": {"impact": "end"}
          }
        }

    这一份就是全部：没写到的段，原来的导演项去掉。

    停顿（``pause_after``）也可以写：它是往录好的旁白里插一段静音
    （``narration_pauses``），不用重新配音。``recorded`` 是配音时就录进去的那些
    ``{段号: 秒}``——它们在声音里，去不掉也缩不短，所以每一段至少是这么长。
    不给 ``recorded`` 时，分镜里现有的停顿都当成录进去的。

    A direction file for a video that already exists: what to add, keyed by
    scene number or run. It replaces all the direction the plan had. A
    ``pause_after`` is spliced into the recorded narration as silence;
    ``recorded`` names the pauses that were spoken with the voice, which can
    be neither removed nor shortened.
    """
    if not plan_scenes:
        raise ScenePlanError(
            "this video was not made from a scene plan, so there are no "
            "scenes to direct"
        )
    raw = _strip_fence(str(text or "")).strip()
    if not raw:
        raise ScenePlanError("the direction file is empty")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ScenePlanError(f"the direction file is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ScenePlanError('the direction file is an object with "title" and "scenes"')
    listed = data.get("scenes") or {}
    if not isinstance(listed, dict):
        raise ScenePlanError(
            'in a direction file "scenes" is an object keyed by scene number, '
            'like {"6-7": {"music": "off"}}'
        )

    total = len(plan_scenes)
    wanted: List[dict] = [{} for _ in range(total)]
    for key, entry in listed.items():
        if not isinstance(entry, dict):
            raise ScenePlanError(f'"{key}" is not an object')
        unknown = sorted(set(entry) - set(_REDIRECT_FIELDS))
        if unknown:
            raise ScenePlanError(
                f'"{key}": {unknown[0]!r} is not something a scene can be given; '
                f"it is one of {', '.join(_REDIRECT_FIELDS)}"
            )
        for number in _scene_numbers(key, total):
            wanted[number - 1].update(entry)

    scenes = []
    for number, (old, entry) in enumerate(zip(plan_scenes, wanted), start=1):
        direction = _direction(entry, number)
        if recorded is None:
            spoken = (old.get("direction") or {}).get("pause_after") or 0.0
        else:
            spoken = recorded.get(number) or 0.0
        if spoken and spoken > direction.get("pause_after", 0.0):
            direction["pause_after"] = spoken
        scenes.append({**old, "direction": direction})
    _title(data, scenes)
    for scene in scenes:
        if not scene["direction"]:
            del scene["direction"]
    return scenes


def outline(plan_scenes: List[dict]) -> str:
    """一段一行：段号、大约第几秒、已有的导演项、旁白。补导演项之前先看这个。"""
    lines = []
    at = 0.0
    for number, scene in enumerate(plan_scenes, start=1):
        direction = scene.get("direction") or {}
        marks = []
        if direction.get("title"):
            marks.append(f"title: {direction['title'].get('text', '')}")
        if direction.get("card"):
            marks.append(f"card: {direction['card'].get('title', '')}")
        if direction.get("stat"):
            marks.append(f"stat: {direction['stat'].get('figure', '')}")
        if direction.get("map"):
            marks.append(f"map: {direction['map'].get('label', '')}")
        if direction.get("timeline"):
            event = direction["timeline"]
            marks.append("timeline: " + " ".join(
                part for part in (str(event.get("date") or ""),
                                  "end" if event.get("end") else "") if part))
        if direction.get("impact"):
            marks.append(f"impact {direction['impact']}")
        if direction.get("music"):
            marks.append("music off")
        if direction.get("ambience"):
            marks.append(direction["ambience"])
        if direction.get("pause_after"):
            marks.append(f"pause {direction['pause_after']:g}s")
        lines.append(
            f"{number:>4}  {int(at // 60)}:{int(at % 60):02d}  "
            + (f"[{'; '.join(marks)}] " if marks else "")
            + str(scene.get("narration") or "")
        )
        at += utils.estimate_spoken_seconds(str(scene.get("narration") or ""))
        at += float(direction.get("pause_after") or 0.0)
    return "\n".join(lines)


#: 一张标题卡在画面上停多久（秒）；两张之间至少隔这么久。
_CARD_GAP_SECONDS = 7.0
#: 配乐让开不到这么久，听不出来：淡出淡入就把它吃掉了。
_MUSIC_OFF_MIN_SECONDS = 6.0
#: 两处让开之间至少隔这么久，配乐才来得及回来。
_MUSIC_OFF_APART_SECONDS = 20.0


def direction_notes(scenes: List[dict]) -> List[str]:
    """导演项用得对不对：太密的卡、听不出来的静音、太多的重音。只是提醒，不拦。

    时间是照字数估的（出片时照录音来），所以这里的秒数只用来比大小。
    """
    lengths = [
        utils.estimate_spoken_seconds(str(scene.get("narration") or ""))
        + float((scene.get("direction") or {}).get("pause_after") or 0.0)
        for scene in scenes
    ]
    starts = [0.0]
    for length in lengths:
        starts.append(starts[-1] + length)
    minutes = starts[-1] / 60.0

    def having(key: str) -> List[int]:
        return [at for at, scene in enumerate(scenes, start=1)
                if (scene.get("direction") or {}).get(key)]

    notes: List[str] = []
    cards = having("card")
    most = max(6, round(minutes / 1.5))
    if len(cards) > most:
        notes.append(
            f"{len(cards)} cards is a lot for {minutes:.0f} minutes - about one "
            f"every 90 seconds ({most} here) is the most that still reads"
        )
    for first, second in zip(cards, cards[1:]):
        apart = starts[second - 1] - starts[first - 1]
        if apart < _CARD_GAP_SECONDS:
            notes.append(
                f"the cards on scenes {first} and {second} are about {apart:.0f}s "
                f"apart - leave {_CARD_GAP_SECONDS:g}s, the first is still on screen"
            )

    stretches: List[List[int]] = []
    for number in having("music"):
        if stretches and number == stretches[-1][1] + 1:
            stretches[-1][1] = number
        else:
            stretches.append([number, number])

    def named(first: int, last: int) -> str:
        return f"scene {first}" if first == last else f"scenes {first}-{last}"

    for first, last in stretches:
        held = starts[last] - starts[first - 1]
        if held < _MUSIC_OFF_MIN_SECONDS:
            notes.append(
                f"the music is off for only about {held:.0f}s in {named(first, last)} "
                f"- under {_MUSIC_OFF_MIN_SECONDS:g}s the fade swallows it; mark "
                "the scenes around it too"
            )
    for (_, last), (first, _end) in zip(stretches, stretches[1:]):
        apart = starts[first - 1] - starts[last]
        if apart < _MUSIC_OFF_APART_SECONDS:
            notes.append(
                f"the music returns for only about {apart:.0f}s between scenes "
                f"{last} and {first} - join the two stretches or drop one"
            )

    # 时间轴：一条轴上只有一件事，标记就没有地方可挪；"end" 前面没有事，
    # 就是什么都没关掉。
    run: List[int] = []
    for number in having("timeline"):
        event = scenes[number - 1]["direction"]["timeline"]
        if event.get("date"):
            run.append(number)
        elif not run:
            notes.append(
                f'scene {number} ends a timeline, and no timeline is showing '
                "there - put the dates on the scenes before it"
            )
        if event.get("end"):
            if len(run) == 1:
                notes.append(
                    f"the timeline on scene {run[0]} has one date - with nothing "
                    "to move to it is a caption; a card says that better"
                )
            run = []
    if len(run) == 1:
        notes.append(
            f"the timeline on scene {run[0]} has one date - with nothing to "
            "move to it is a caption; a card says that better"
        )

    impacts = having("impact")
    most = max(3, math.ceil(minutes / 2.0))
    if len(impacts) > most:
        notes.append(
            f"{len(impacts)} impacts is a lot for {minutes:.0f} minutes - they "
            f"stop landing past one every two minutes ({most} here)"
        )
    return notes


def _runs(numbers: List[int]) -> str:
    """``[3, 4, 5, 9]`` -> ``"3-5, 9"``。"""
    parts: List[List[int]] = []
    for number in numbers:
        if parts and number == parts[-1][1] + 1:
            parts[-1][1] = number
        else:
            parts.append([number, number])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in parts)


def photo_percent(plan_scenes: List[dict]) -> int:
    """照着照片画的占百分之几：往上取整，份额只多不少（多出来的没有候选）。"""
    total = len(plan_scenes)
    wanted = sum(1 for scene in plan_scenes if scene.get("photo"))
    if not total or not wanted:
        return 0
    return min(100, math.ceil(100.0 * wanted / total))


def report(plan: dict) -> str:
    """写完之后自己看一眼的那几行：几段、多长、几段照着照片画、哪几段太长。"""
    scenes = plan["scenes"]
    seconds = [utils.estimate_spoken_seconds(scene["narration"]) for scene in scenes]
    photos = [at for at, scene in enumerate(scenes, start=1) if scene.get("photo")]

    def having(key: str, value: Any = None) -> List[int]:
        return [
            at for at, scene in enumerate(scenes, start=1)
            if (scene.get("direction") or {}).get(key)
            and (value is None or scene["direction"][key] == value)
        ]

    pauses = sum((scene.get("direction") or {}).get("pause_after", 0.0) for scene in scenes)
    lines = [
        f"{len(scenes)} scenes, about {(sum(seconds) + pauses) / 60.0:.1f} minutes "
        "of narration",
        f"{len(photos)} drawn from a photo ({photo_percent(scenes)}%)"
        + (f": scenes {', '.join(str(at) for at in photos)}" if photos else ""),
    ]
    if plan.get("look"):
        lines.append(f"look: {plan['look']} (the plan chooses the Finish)")
    held = [f"{at} ({length:.0f}s)" for at, length in enumerate(seconds, start=1)
            if length > 9.0]
    if held:
        lines.append(
            "one picture is held for over 9 seconds in scenes " + ", ".join(held)
            + " - consider splitting them"
        )
    if directed(scenes):
        def counted(key: str, word: str) -> str:
            at = having(key)
            text = f"{len(at)} {word}{'' if len(at) == 1 else 's'}"
            return text + (f" (scene{'' if len(at) == 1 else 's'} {_runs(at)})" if at else "")

        titled = having("title")
        noted = [
            (f"title over scene {titled[0]}"
             + (" (on black)" if scenes[titled[0] - 1]["direction"]["title"].get("on")
                else "")) if titled else "no title",
            counted("card", "card"),
            *([counted("stat", "stat")] if having("stat") else []),
            f"{len(having('pause_after'))} pause"
            f"{'' if len(having('pause_after')) == 1 else 's'} ({pauses:.1f}s)",
            counted("impact", "impact"),
        ]
        if having("music"):
            noted.append(f"music off in scenes {_runs(having('music'))}")
        for kind in AMBIENCES:
            if having("ambience", kind):
                noted.append(f"{kind} under scenes {_runs(having('ambience', kind))}")
        if having("map"):
            noted.append(f"map on scene{'' if len(having('map')) == 1 else 's'} "
                         f"{_runs(having('map'))}")
        dated = [at for at in having("timeline")
                 if scenes[at - 1]["direction"]["timeline"].get("date")]
        if dated:
            noted.append(
                f"{len(dated)} timeline date{'' if len(dated) == 1 else 's'} "
                f"(scenes {dated[0]} to {dated[-1]})")
        lines.append("direction: " + "; ".join(noted))
        # 每张地图标在了哪儿，用数据说一遍：坐标写错了（标到了海里、标到了
        # 另一个国家）这一行一眼就看得出来。
        for at in having("map"):
            place = scenes[at - 1]["direction"]["map"]
            try:
                said = locator_map.where_is(place["lat"], place["lon"])
            except locator_map.MapError as exc:
                said = f"not checked ({exc})"
            lines.append(
                f"map on scene {at}: {place['label']} at {place['lat']:g}, "
                f"{place['lon']:g} is {said}; the view is {place['width_km']:g} km wide"
            )
        lines.extend(direction_notes(scenes))
    else:
        lines.append(
            "no direction (title, cards, timeline, pauses, impacts, music, ambience): the "
            "finishing pass will choose title cards itself and add nothing else"
        )
    return "\n".join(lines)


def photo_candidates(plan_scenes: List[dict], scene_ids: List[str]) -> dict:
    """分镜点了名的段，写成清单里"哪几段值得找照片"的那一项。"""
    found = {}
    for key, scene in zip(scene_ids, plan_scenes):
        photo = scene.get("photo") if isinstance(scene, dict) else None
        if isinstance(photo, dict):
            found[key] = {
                "kind": str(photo.get("kind") or "thing"),
                "score": PHOTO_SCORE,
                "name": str(photo.get("name") or ""),
                "search": str(photo.get("search") or ""),
            }
        else:
            found[key] = {"kind": "none", "score": 0, "name": ""}
    return found


if __name__ == "__main__":  # pragma: no cover - python -m app.services.scene_plan plan.json
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.services.scene_plan <plan.json>")
    with open(sys.argv[1], encoding="utf-8") as handle:
        try:
            print(report(parse(handle.read())))
        except ScenePlanError as problem:
            sys.exit(f"not a usable scene plan: {problem}")
