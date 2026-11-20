"""定位地图：告诉观众"这是在哪儿"的那一张图。

这是什么
--------
纪录片讲到一个观众不认识的地方，会切一张地图：深色的陆地和海，一个红点，
一行地名，角上一个小图说明它在大洲的哪个位置。这里画的就是这一张——一张
普通的 PNG，所以它走的是和别的画面完全一样的路：烘成片段、带慢推、能在编辑
器里看、能换、能删。

为什么自己画
------------
常见的做法是 Mapbox 或者 OpenStreetMap 的瓦片：要账号或者出片时联网，而且
每条片子都得在画面上署名。这里用 Natural Earth 的矢量数据（公有领域，不用
署名，不用联网），用 Pillow 画。样式也就能和成片的冷色调、和标题卡是同一套
——红色是标题卡那条竖线的红，字是同一个字体。

数据放在 ``storage/map_data``（见 :data:`FILES`），没有就报错说缺哪个文件、
从哪儿下。

投影
----
以要标的地方为中心的等距圆柱投影，经度方向按 ``cos(纬度)`` 收窄：在一张图
覆盖的范围内（几百到几千公里）形状看着是对的，而且就是几行算术。跨过 180°
经线的视野不处理。

A locator map: dark land and sea, a red marker, the place name and an inset
showing where on the continent it is. Drawn locally with Pillow from Natural
Earth data (public domain, no network, no credit line needed), in the same
palette and type as the title cards. The result is an ordinary PNG, so it goes
through the same pipeline as every other scene picture.
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.utils import utils

#: 画地图要的数据文件（Natural Earth，geojson）。
SOURCE = "https://github.com/nvkelso/natural-earth-vector/tree/master/geojson"
FILES = {
    "land": "ne_10m_land.geojson",
    "islands": "ne_10m_minor_islands.geojson",
    "lakes": "ne_10m_lakes.geojson",
    "countries": "ne_50m_admin_0_countries.geojson",
    "borders": "ne_10m_admin_0_boundary_lines_land.geojson",
    "states": "ne_50m_admin_1_states_provinces_lines.geojson",
    "places": "ne_10m_populated_places_simple.geojson",
}

#: 一张图左右一共看多宽（公里）。
MIN_WIDTH_KM = 40.0
MAX_WIDTH_KM = 18000.0
DEFAULT_WIDTH_KM = 900.0
#: ``zoom`` 的几档：名字好记，对应的是一张图左右看多宽（公里）。要更细就直接
#: 写 ``width_km``。
ZOOMS = {
    "city": 120.0,
    "area": 400.0,
    "region": 900.0,
    "country": 2200.0,
    "continent": 6500.0,
    "world": 17000.0,
}
_KM_PER_DEGREE = 111.32

# 配色：和冷色调的成片、和标题卡是一套。
SEA = (11, 15, 20)
LAND = (38, 43, 50)
COAST = (86, 95, 106)
LAKE = (14, 19, 25)
BORDER = (104, 113, 125)
STATE = (62, 69, 79)
COUNTRY_NAME = (92, 101, 113)
RED = (200, 58, 58)
WHITE = (240, 240, 236)
GREY = (168, 174, 182)

#: 先画成两倍大再缩回去：海岸线和斜线就不锯齿了。
_SUPERSAMPLE = 2


class MapError(ValueError):
    """这张地图画不了。说清楚缺什么、哪个数不对。"""


def data_dir() -> str:
    return utils.storage_dir("map_data")


def _load(kind: str) -> dict:
    path = os.path.join(data_dir(), FILES[kind])
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise MapError(
            f"the map data file {FILES[kind]} is missing from {data_dir()}; "
            f"download it from {SOURCE}"
        ) from None
    except (OSError, ValueError) as exc:
        raise MapError(f"the map data file {FILES[kind]} cannot be read: {exc}") from exc


def _rings_of(geometry: dict) -> List[List[np.ndarray]]:
    """一个几何体里的每个多边形：``[外圈, 洞, 洞...]``，每圈是 N×2 的经纬度。"""
    kind = geometry.get("type")
    if kind == "Polygon":
        polygons = [geometry["coordinates"]]
    elif kind == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        return []
    return [
        [np.asarray(ring, dtype=np.float64)[:, :2] for ring in polygon if len(ring) >= 3]
        for polygon in polygons
    ]


def _lines_of(geometry: dict) -> List[np.ndarray]:
    kind = geometry.get("type")
    if kind == "LineString":
        lines = [geometry["coordinates"]]
    elif kind == "MultiLineString":
        lines = geometry["coordinates"]
    else:
        return []
    return [np.asarray(line, dtype=np.float64)[:, :2] for line in lines if len(line) >= 2]


def _box(points: np.ndarray) -> Tuple[float, float, float, float]:
    return (float(points[:, 0].min()), float(points[:, 1].min()),
            float(points[:, 0].max()), float(points[:, 1].max()))


@lru_cache(maxsize=None)
def _polygons(kind: str) -> list:
    """``[(外框, [外圈, 洞...])]``。读一次，之后都在内存里。"""
    found = []
    for feature in _load(kind)["features"]:
        for rings in _rings_of(feature.get("geometry") or {}):
            if rings:
                found.append((_box(rings[0]), rings))
    return found


@lru_cache(maxsize=None)
def _lines(kind: str) -> list:
    found = []
    for feature in _load(kind)["features"]:
        for line in _lines_of(feature.get("geometry") or {}):
            found.append((_box(line), line))
    return found


@lru_cache(maxsize=None)
def _countries() -> list:
    """``[(名字, 标注点经度, 纬度, [(外框, 外圈)])]``。"""
    found = []
    for feature in _load("countries")["features"]:
        props = feature.get("properties") or {}
        outers = [(_box(rings[0]), rings[0])
                  for rings in _rings_of(feature.get("geometry") or {}) if rings]
        if not outers:
            continue
        found.append((
            str(props.get("NAME") or props.get("ADMIN") or ""),
            props.get("LABEL_X"), props.get("LABEL_Y"), outers,
            float(props.get("POP_EST") or 0.0),
        ))
    return found


@lru_cache(maxsize=None)
def _places() -> Tuple[np.ndarray, list]:
    """有名字的城镇：``(N×2 的经纬度, [(名字, 国家)])``。"""
    points, names = [], []
    for feature in _load("places")["features"]:
        geometry = feature.get("geometry") or {}
        props = feature.get("properties") or {}
        if geometry.get("type") != "Point":
            continue
        points.append(geometry["coordinates"][:2])
        names.append((str(props.get("name") or ""), str(props.get("sov0name") or "")))
    return np.asarray(points, dtype=np.float64), names


def _inside(ring: np.ndarray, lon: float, lat: float) -> bool:
    """点在不在这一圈里（射线法）。"""
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    crosses = (y > lat) != (y2 > lat)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = (x2 - x) * (lat - y) / (y2 - y) + x
    return bool(np.count_nonzero(crosses & (lon < at)) % 2)


def where_is(lat: float, lon: float) -> str:
    """这个经纬度是哪儿：哪个国家，离哪个有名字的城镇多远。

    写分镜的人用它核对坐标：把罗阿诺克岛标到了大西洋中间，这一行会说"不在
    任何国家里，离最近的城镇 900 公里"。
    """
    country = ""
    for name, _x, _y, outers, _people in _countries():
        for (west, south, east, north), ring in outers:
            if west <= lon <= east and south <= lat <= north and _inside(ring, lon, lat):
                country = name
                break
        if country:
            break
    points, names = _places()
    said = f"in {country}" if country else "at sea or on land too small for the country data"
    if len(points):
        scale = math.cos(math.radians(lat))
        distance = np.hypot((points[:, 0] - lon) * scale, points[:, 1] - lat) * _KM_PER_DEGREE
        nearest = int(np.argmin(distance))
        town, nation = names[nearest]
        said += (f", {distance[nearest]:.0f} km from {town}"
                 + (f" ({nation})" if nation and nation != country else ""))
    return said


def checked(value, number: int = 0) -> dict:
    """分镜里写的一张地图，核对过、补上默认值。错了说是第几段、错在哪。"""
    where = f"scene {number}: " if number else ""
    if not isinstance(value, dict):
        raise MapError(
            f'{where}map is an object like {{"lat": 35.9, "lon": -75.7, '
            '"label": "Roanoke Island"}'
        )

    def number_of(key: str, low: float, high: float, source: dict = value, who: str = "map"):
        try:
            found = float(source[key])
        except (KeyError, TypeError, ValueError):
            raise MapError(f"{where}the {who} needs a number for {key!r}") from None
        if not low <= found <= high:
            raise MapError(
                f"{where}the {who}'s {key} is {found:g}; it is between {low:g} and {high:g}")
        return found

    def words(text) -> str:
        return " ".join(str(text or "").split())

    label = words(value.get("label"))
    if not label:
        raise MapError(f"{where}the map has no label (the name of the place)")
    if len(label) > 40:
        raise MapError(f"{where}the map's label is {len(label)} characters; the most is 40")
    found = {
        "lat": round(number_of("lat", -85.0, 85.0), 5),
        "lon": round(number_of("lon", -180.0, 180.0), 5),
        "label": label,
        "sublabel": words(value.get("sublabel"))[:60],
        "width_km": DEFAULT_WIDTH_KM,
        # 今天的国界和国名。讲一千年前的事，可以关掉：只剩海岸线。
        "modern": value.get("modern") not in (False, "false", "no", 0),
        "also": [],
    }
    zoom = words(value.get("zoom")).lower()
    if zoom:
        if zoom not in ZOOMS:
            raise MapError(
                f"{where}the map's zoom is {zoom!r}; it is one of {', '.join(ZOOMS)} "
                "(or give width_km, the width of the view in kilometres)")
        found["width_km"] = ZOOMS[zoom]
    # 两样都写了，听数字的：它更细。
    if value.get("width_km") not in (None, ""):
        found["width_km"] = round(number_of("width_km", MIN_WIDTH_KM, MAX_WIDTH_KM), 1)
    also = value.get("also") or []
    if not isinstance(also, list) or len(also) > 6:
        raise MapError(f'{where}the map\'s "also" is a list of at most 6 other places')
    for other in also:
        if not isinstance(other, dict):
            raise MapError(f'{where}each place in "also" is an object with lat, lon and label')
        found["also"].append({
            "lat": round(number_of("lat", -85.0, 85.0, other, "other place"), 5),
            "lon": round(number_of("lon", -180.0, 180.0, other, "other place"), 5),
            "label": words(other.get("label"))[:40],
        })
    return found


# ---------------------------------------------------------------------------
# 画
# ---------------------------------------------------------------------------
class _View:
    """一个视野：经纬度 → 画布上的像素。"""

    def __init__(self, lat: float, lon: float, width_km: float, width: int, height: int,
                 left: int = 0, top: int = 0, at: Tuple[float, float] = (0.5, 0.5)):
        self.lat, self.lon = lat, lon
        self.width, self.height, self.left, self.top = width, height, left, top
        self.squeeze = max(0.05, math.cos(math.radians(lat)))
        # 每一度纬度多少像素。
        self.scale = width / (width_km / _KM_PER_DEGREE)
        self.centre = (left + width * at[0], top + height * at[1])
        half_lon = width / self.scale / self.squeeze
        half_lat = height / self.scale
        self.box = (lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)

    def sees(self, box: Tuple[float, float, float, float]) -> bool:
        west, south, east, north = self.box
        return not (box[2] < west or box[0] > east or box[3] < south or box[1] > north)

    def point(self, lon: float, lat: float) -> Tuple[float, float]:
        return (self.centre[0] + (lon - self.lon) * self.squeeze * self.scale,
                self.centre[1] - (lat - self.lat) * self.scale)

    def path(self, points: np.ndarray) -> List[Tuple[float, float]]:
        x = self.centre[0] + (points[:, 0] - self.lon) * self.squeeze * self.scale
        y = self.centre[1] - (points[:, 1] - self.lat) * self.scale
        return list(zip(x.tolist(), y.tolist()))


def _draw_base(draw: ImageDraw.ImageDraw, view: _View, *, detail: bool, stroke: int,
               borders: bool = True) -> None:
    """陆地、岛、湖、海岸线；``borders`` 时加国界，再 ``detail`` 时加省界。"""
    for kind in ("land", "islands"):
        for box, rings in _polygons(kind):
            if not view.sees(box):
                continue
            draw.polygon(view.path(rings[0]), fill=LAND)
            for hole in rings[1:]:
                draw.polygon(view.path(hole), fill=SEA)
    for box, rings in _polygons("lakes"):
        if view.sees(box):
            draw.polygon(view.path(rings[0]), fill=LAKE)
    for kind in ("land", "islands"):
        for box, rings in _polygons(kind):
            if not view.sees(box):
                continue
            for ring in rings:
                draw.line(view.path(ring), fill=COAST, width=stroke, joint="curve")
    if not borders:
        return
    if detail:
        for box, line in _lines("states"):
            if view.sees(box):
                draw.line(view.path(line), fill=STATE, width=max(1, stroke - 1))
    for box, line in _lines("borders"):
        if view.sees(box):
            draw.line(view.path(line), fill=BORDER, width=stroke)


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont:
    """标题卡那个字体（Windows 的 Bahnschrift，压窄）；没有就用自带的。"""
    for path, narrow in (
        (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "bahnschrift.ttf"), True),
        (os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf" if bold
                      else "BeVietnamPro-Medium.ttf"), False),
    ):
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        if narrow:
            try:
                # 字重、字宽：粗一点、窄一点，就是标题卡那个样子。
                font.set_variation_by_axes([700 if bold else 400, 80])
            except (OSError, AttributeError, ValueError):
                pass
        return font
    return ImageFont.load_default()


def _spaced(draw: ImageDraw.ImageDraw, at: Tuple[float, float], text: str, font, fill,
            spacing: float = 0.0, anchor: str = "l") -> float:
    """一个字一个字地画，字和字之间多留一点。返回画了多宽。"""
    widths = [draw.textlength(char, font=font) for char in text]
    total = sum(widths) + spacing * max(0, len(text) - 1)
    x = at[0] - (total / 2 if anchor == "m" else total if anchor == "r" else 0)
    for char, width in zip(text, widths):
        draw.text((x, at[1]), char, font=font, fill=fill)
        x += width + spacing
    return total


def render(place: dict, size: Tuple[int, int] = (1920, 1080)) -> Image.Image:
    """画一张定位地图。``place`` 是 :func:`checked` 给的那种。"""
    place = checked(place)
    factor = _SUPERSAMPLE
    width, height = size[0] * factor, size[1] * factor
    unit = min(width, height) / 1080.0
    canvas = Image.new("RGB", (width, height), SEA)
    draw = ImageDraw.Draw(canvas)

    # 要标的地方放在画面中间偏左一点：地名写在它右边，要有地方写。
    view = _View(place["lat"], place["lon"], place["width_km"], width, height, at=(0.44, 0.5))
    _draw_base(draw, view, detail=place["width_km"] <= 4500,
               stroke=max(2, round(2.2 * unit)), borders=place["modern"])
    # 国名等地名的牌子摆好了再写：不能压在牌子上（见下面）。
    country_names = []
    if place["modern"]:
        for name, label_lon, label_lat, _outers, people in _countries():
            if label_lon is None or label_lat is None:
                continue
            x, y = view.point(float(label_lon), float(label_lat))
            # 摩纳哥、圣马力诺这种小国，在一张看得见半个大洲的图上只是添乱。
            if people < 500000 and place["width_km"] > 500:
                continue
            if 0.04 * width < x < 0.96 * width and 0.06 * height < y < 0.94 * height:
                country_names.append((people, name.upper(), x, y))

    # 别的地方：小白点和小字。
    small = _font(round(30 * unit), bold=False)
    for other in place["also"]:
        x, y = view.point(other["lon"], other["lat"])
        radius = 6 * unit
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=WHITE)
        if other["label"]:
            draw.text((x + 16 * unit, y - 19 * unit), other["label"], font=small, fill=GREY)

    # 要标的地方：一圈光晕、一个红点、白色的芯。
    x, y = view.point(place["lon"], place["lat"])
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    halo = ImageDraw.Draw(glow)
    for radius, alpha in ((46, 46), (30, 90)):
        r = radius * unit
        halo.ellipse((x - r, y - r, x + r, y + r), outline=RED + (alpha * 2,),
                     width=max(2, round(3 * unit)))
    canvas.paste(glow, (0, 0), glow)
    r = 15 * unit
    draw.ellipse((x - r, y - r, x + r, y + r), fill=RED)
    r = 5.5 * unit
    draw.ellipse((x - r, y - r, x + r, y + r), fill=WHITE)

    # 地名：红竖线、大字、小字——和标题卡一个样子。写在右边，放不下就写左边。
    big = _font(round(74 * unit))
    sub = _font(round(38 * unit), bold=False)
    label = place["label"].upper()
    label_width = sum(draw.textlength(char, font=big) for char in label) + 2 * unit * len(label)
    sub_width = draw.textlength(place["sublabel"], font=sub) if place["sublabel"] else 0
    block = max(label_width, sub_width) + 34 * unit
    gap = 62 * unit
    left = x + gap if x + gap + block < width - 60 * unit else x - gap - block
    top = y - (56 if place["sublabel"] else 38) * unit
    plate_bottom = top + (132 if place["sublabel"] else 86) * unit
    # 国名：很淡，字距拉开，垫在底下——是背景，不是标注。人多的国家先写；
    # 会压到地名牌子、红点或者别的国名的就不写（摩纳哥、梵蒂冈这种挤在一起
    # 的小国就是这样被让掉的）。
    names = _font(round(30 * unit), bold=False)
    taken = [
        (left - 30 * unit, top - 30 * unit, left + block + 30 * unit, plate_bottom + 30 * unit),
        (x - 60 * unit, y - 60 * unit, x + 60 * unit, y + 60 * unit),
    ]
    for _people, name, name_x, name_y in sorted(country_names, reverse=True):
        half = (sum(draw.textlength(char, font=names) for char in name)
                + 7 * unit * (len(name) - 1)) / 2 + 14 * unit
        box = (name_x - half, name_y - 10 * unit, name_x + half, name_y + 46 * unit)
        if any(box[0] < other[2] and box[2] > other[0] and box[1] < other[3]
               and box[3] > other[1] for other in taken):
            continue
        taken.append(box)
        _spaced(draw, (name_x, name_y), name, names, COUNTRY_NAME,
                spacing=7 * unit, anchor="m")

    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle(
        (left - 14 * unit, top - 12 * unit, left + block + 16 * unit, plate_bottom + 10 * unit),
        fill=(8, 11, 15, 150))
    canvas.paste(shade, (0, 0), shade)
    draw.rectangle((left, top, left + 7 * unit, plate_bottom), fill=RED)
    _spaced(draw, (left + 26 * unit, top - 4 * unit), label, big, WHITE, spacing=2 * unit)
    if place["sublabel"]:
        draw.text((left + 26 * unit, top + 84 * unit), place["sublabel"], font=sub, fill=GREY)

    # 角上的小图：它在大洲的哪儿。已经看得见整个大洲的就不用了。
    if place["width_km"] <= 3200:
        side = round(300 * unit)
        margin = round(56 * unit)
        box_left, box_top = margin, height - margin - side
        wide = min(MAX_WIDTH_KM, max(5200.0, place["width_km"] * 7))
        inset_view = _View(place["lat"], place["lon"], wide, side, side, box_left, box_top)
        inset = Image.new("RGB", (width, height), SEA)
        _draw_base(ImageDraw.Draw(inset), inset_view, detail=False,
                   stroke=max(1, round(1.2 * unit)), borders=place["modern"])
        canvas.paste(inset.crop((box_left, box_top, box_left + side, box_top + side)),
                     (box_left, box_top))
        px, py = inset_view.point(place["lon"], place["lat"])
        half_w = max(5 * unit, place["width_km"] / wide * side / 2)
        half_h = half_w * height / width
        draw.rectangle((px - half_w, py - half_h, px + half_w, py + half_h),
                       outline=RED, width=max(2, round(3 * unit)))
        draw.rectangle((box_left, box_top, box_left + side, box_top + side),
                       outline=COAST, width=max(2, round(2 * unit)))

    # 四角压暗一点：眼睛往中间去。
    yy, xx = np.mgrid[0:height:8, 0:width:8]
    far = np.hypot((xx - width / 2) / (width / 2), (yy - height / 2) / (height / 2))
    dark = np.clip((far - 0.62) / 0.8, 0.0, 1.0) * 120
    mask = Image.fromarray(dark.astype(np.uint8)).resize((width, height), Image.BILINEAR)
    canvas.paste(Image.new("RGB", (width, height), (4, 6, 9)), (0, 0), mask)

    return canvas.resize(size, Image.LANCZOS).filter(
        ImageFilter.UnsharpMask(radius=1.2, percent=40, threshold=2))


def save(place: dict, path: str, size: Tuple[int, int] = (1920, 1080)) -> str:
    """画好存成 PNG（先写旁边再换名，路径上出现的一定是整张图）。"""
    image = render(place, size)
    partial = f"{path}.{os.getpid()}.part.png"
    image.save(partial, "PNG")
    os.replace(partial, path)
    return path


if __name__ == "__main__":  # pragma: no cover
    # python -m app.services.locator_map <lat> <lon> [label] [width_km] [out.png]
    import sys

    arguments = sys.argv[1:]
    if len(arguments) < 2:
        sys.exit("usage: python -m app.services.locator_map <lat> <lon> "
                 "[label] [width_km] [out.png]\n"
                 "  with only lat and lon: says where that is, to check the numbers")
    try:
        latitude, longitude = float(arguments[0]), float(arguments[1])
        print(f"{latitude:g}, {longitude:g} is {where_is(latitude, longitude)}")
        if len(arguments) >= 3:
            wanted = {"lat": latitude, "lon": longitude, "label": arguments[2]}
            if len(arguments) >= 4:
                wanted["width_km"] = arguments[3]
            target = arguments[4] if len(arguments) >= 5 else "locator-map.png"
            print("drawn:", save(wanted, target))
    except (MapError, ValueError) as problem:
        sys.exit(f"cannot draw this map: {problem}")
