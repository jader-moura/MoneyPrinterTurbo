"""封面上的那行大字。

Why the words are a separate step from the picture
--------------------------------------------------
封面由两样东西组成：一张图，和压在图上的几个字。这两样的代价差了几个数量
级——换一张图要么重跑一次文生图（本机几分钟），要么重新从片段里取一帧；而
改几个字只是重画一次文字。

所以它们在磁盘上是两个文件：``youtube-thumbnail-base.png`` 是那张图本身，
``youtube-thumbnail.png`` 是压好字的成品。改字只从 base 重新合成一次，不碰
图；换图则保留那几个字，重新压一遍。否则"把标题改一个词"就得重新出一次图,
而"换一张图"会把辛苦调好的文字弄丢。

The picture and the lettering are two files, because re-picking a picture costs
minutes and re-typing the words costs nothing. Changing one never re-does the
other.

Why the text is drawn here and not in the browser
-------------------------------------------------
预览里用 CSS 把字叠在图上更省事，但那样下载下来的 PNG 里没有字——而用户要
交给 YouTube 的正是那个文件。所以字必须烧进图里,预览显示的就是成品本身。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

from app.utils import utils


class ThumbnailTextError(ValueError):
    """这几个字画不上去。失败一律点名，不悄悄画成默认的样子。"""


#: 文字放在画面的哪一段。封面上的字几乎只会在这三个位置之一。
TEXT_POSITIONS: tuple[str, ...] = ("top", "middle", "bottom")

#: 字号按画面高度的百分比算，不按像素。1080x1920 和 1280x720 的高度差了一
#: 倍多，同一个像素字号在两者上完全不是一回事；而"占画面高度的 9%"在两种
#: 画幅上看起来是一样大的。
DEFAULT_SIZE = 9.0
MIN_SIZE = 3.0
MAX_SIZE = 30.0

#: 描边按字号的百分比算，同理：字大了描边也该跟着粗，否则大字会显得没有边。
DEFAULT_STROKE = 10.0
MAX_STROKE = 40.0

#: 左右各留出的边距，按画面宽度算。YouTube 在手机上会把封面缩得很小，
#: 字贴着边缘时最先被裁掉的就是它。
MARGIN_RATIO = 0.06

MAX_TEXT_LENGTH = 120

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def defaults(font_name: str = "") -> dict:
    """一份能直接用的样式。字体跟着这条片子的字幕走。

    为什么默认跟字幕同一款字：封面和片子里的字出自同一条片子，用同一款字
    看起来才像一套。而且那一款是用户已经为这条片子挑过的,不用再挑一次。
    """
    return {
        "text": "",
        "font": str(font_name or ""),
        "position": "bottom",
        "size": DEFAULT_SIZE,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "stroke_width": DEFAULT_STROKE,
        "band": True,
    }


def _colour(value: Any, field: str, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if not _COLOUR.match(text):
        raise ThumbnailTextError(f"{field} must look like #RRGGBB, not {text!r}")
    return text.upper()


def _number(value: Any, field: str, fallback: float, low: float, high: float) -> float:
    if value is None or value == "":
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ThumbnailTextError(f"{field} must be a number") from exc
    if number < low or number > high:
        raise ThumbnailTextError(
            f"{field} must be between {low:g} and {high:g}, not {number:g}"
        )
    return number


def normalise(wanted: Any, *, font_name: str = "", known_fonts: Any = None) -> dict:
    """把界面交上来的那一份整理成可用的样式，不合法的一律报错。

    越界一律拒绝而不是夹到边界上：界面显示的值和实际画出来的必须是同一个,
    否则用户会对着一个"看起来是 40 其实按 30 画"的输入框较劲。
    """
    style = defaults(font_name)
    if wanted is None:
        return style
    if not isinstance(wanted, dict):
        raise ThumbnailTextError("the thumbnail text settings must be an object")

    if "text" in wanted:
        text = str(wanted.get("text") or "").strip()
        if len(text) > MAX_TEXT_LENGTH:
            raise ThumbnailTextError(
                f"the thumbnail text is {len(text)} characters; keep it under "
                f"{MAX_TEXT_LENGTH} — a thumbnail is read in half a second"
            )
        style["text"] = text
    if "font" in wanted:
        font = str(wanted.get("font") or "").strip()
        if font and known_fonts is not None and font not in known_fonts:
            raise ThumbnailTextError(f"unknown font {font!r}")
        style["font"] = font or style["font"]
    if "position" in wanted:
        position = str(wanted.get("position") or "").strip().lower()
        if position and position not in TEXT_POSITIONS:
            raise ThumbnailTextError(
                f"position must be one of: {', '.join(TEXT_POSITIONS)}"
            )
        style["position"] = position or style["position"]
    style["size"] = _number(
        wanted.get("size", style["size"]), "size", DEFAULT_SIZE, MIN_SIZE, MAX_SIZE
    )
    style["stroke_width"] = _number(
        wanted.get("stroke_width", style["stroke_width"]),
        "outline width",
        DEFAULT_STROKE,
        0.0,
        MAX_STROKE,
    )
    style["color"] = _colour(wanted.get("color", style["color"]), "colour", "#FFFFFF")
    style["stroke_color"] = _colour(
        wanted.get("stroke_color", style["stroke_color"]), "outline colour", "#000000"
    )
    if "band" in wanted:
        style["band"] = bool(wanted.get("band"))
    return style


def _load_font(name: str, pixels: int) -> ImageFont.FreeTypeFont:
    """按名字加载字体。加载不了就报错，不悄悄退回 PIL 的默认位图字体。

    默认位图字体不认字号，退回它会得到一张"字只有几个像素高"的封面——那比
    报错更难查，因为看起来像是字号没生效。
    """
    candidate = os.path.join(utils.font_dir(), *str(name or "").split("/"))
    if not name or not os.path.isfile(candidate):
        raise ThumbnailTextError(
            f"the font {name!r} is not in {utils.font_dir()}; choose another one"
        )
    try:
        return ImageFont.truetype(candidate, pixels)
    except Exception as first_error:
        # .ttc 是字体合集，有些需要显式给出索引才能打开。
        try:
            return ImageFont.truetype(candidate, pixels, index=0)
        except Exception as exc:
            raise ThumbnailTextError(
                f"the font {name!r} cannot be used for the thumbnail: {exc}"
            ) from first_error


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """按真实字宽折行。

    不按字数折：等宽假设对 "WWW" 和 "ill" 差出好几倍，对中文和拉丁字母混排
    更是完全不成立。单个词本身就超宽时不硬拆——拆开的半个词比探出边界更难
    读,而用户看到字贴边会自己把字号调小。
    """
    def measure(line: str) -> float:
        try:
            return font.getlength(line)
        except AttributeError:  # pragma: no cover - 老版本 Pillow
            return font.getsize(line)[0]

    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if measure(candidate) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render(base_path: str, target_path: str, style: Any) -> dict:
    """把字压在 ``base_path`` 那张图上，写到 ``target_path``。

    文字为空时成品就是那张图本身的副本——而不是"不写这个文件"：下载的永远
    是同一个路径，所以把字删光也得留下一张能下载的封面。
    """
    if not base_path or not os.path.isfile(base_path):
        raise ThumbnailTextError("there is no picture to put the text on")

    settings = style if isinstance(style, dict) else normalise(style)
    text = str(settings.get("text") or "").strip()

    with Image.open(base_path) as opened:
        picture = opened.convert("RGB")
        width, height = picture.size

        if text:
            pixels = max(8, int(round(height * float(settings["size"]) / 100.0)))
            font = _load_font(str(settings.get("font") or ""), pixels)
            margin = int(round(width * MARGIN_RATIO))
            lines = _wrap(text, font, max(1, width - margin * 2))
            stroke = int(round(pixels * float(settings["stroke_width"]) / 100.0))

            # 行高按字号给，不按每行实测的高度：实测会让"没有下伸部的那一行"
            # 比别的行矮，几行字叠起来间距就忽大忽小。
            line_height = int(round(pixels * 1.18))
            # 整块的高度不是"行高 × 行数"。anchor="ma" 把 y 对在上伸部顶端，
            # 所以最后一行还要把它自己的下伸部算进去——不算的话，贴底放置时
            # 最后一行会被画出画面外，"farmed" 的下半截就没了。
            ascent, descent = font.getmetrics()
            block_height = line_height * (len(lines) - 1) + ascent + descent
            if settings["position"] == "top":
                top = margin
            elif settings["position"] == "middle":
                top = max(margin, (height - block_height) // 2)
            else:
                top = max(margin, height - block_height - margin)

            if settings.get("band"):
                # 一条半透明的暗带。压在乱的画面上时，这一条比描边更能救
                # 可读性——而封面的图恰恰常常是乱的。
                band = Image.new("RGBA", (width, 1), (0, 0, 0, 0))
                overlay = Image.new("RGBA", picture.size, (0, 0, 0, 0))
                pad = int(round(pixels * 0.3))
                ImageDraw.Draw(overlay).rectangle(
                    [0, max(0, top - pad), width, min(height, top + block_height + pad)],
                    fill=(0, 0, 0, 140),
                )
                picture = Image.alpha_composite(
                    picture.convert("RGBA"), overlay
                ).convert("RGB")
                band.close()
                overlay.close()

            draw = ImageDraw.Draw(picture)
            for number, line in enumerate(lines):
                draw.text(
                    (width // 2, top + line_height * number),
                    line,
                    font=font,
                    fill=settings["color"],
                    anchor="ma",  # 水平居中，纵向按这一行的顶端对齐
                    stroke_width=stroke,
                    stroke_fill=settings["stroke_color"],
                )
            drawn = len(lines)
        else:
            drawn = 0

        directory = os.path.dirname(target_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # 先写临时文件再替换：成品是固定文件名，直接写的话一次失败就把上一张
        # 还能用的封面毁掉了。
        staging = f"{target_path}.{uuid.uuid4().hex[:8]}.part"
        try:
            picture.save(staging, format="PNG")
            os.replace(staging, target_path)
        except OSError as exc:
            if os.path.exists(staging):
                try:
                    os.remove(staging)
                except OSError:
                    pass
            raise ThumbnailTextError(f"cannot save the thumbnail: {exc}") from exc

    logger.info(f"thumbnail text rendered: {drawn} line(s) on {target_path}")
    return {"lines": drawn, "width": width, "height": height}
