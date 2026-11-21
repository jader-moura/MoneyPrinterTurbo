# -*- coding: utf-8 -*-
"""这条片子画过的每一张图，都还在。

为什么要有这个模块
------------------
重生成不是修改，是**换掉**。用户看着一张"还行但不是最好"的图，点一次重出，
拿回来一张更差的——原来那张就再也回不去了。清单里只存"现在用的是哪一个
文件"，而上一个文件名没人记过。

但那张图本身从来没丢：重生成只是把场景的 ``material`` 指向新文件，旧文件
一直躺在任务目录里（整个服务里没有一处删除场景素材的代码）。所以这件事不是
"把图找回来"，是"把那个文件名找回来"——而文件名就在目录里。

于是这里有两条路，都要：

* ``scene_manifest`` 从现在起把换下来的那一个记进场景的 ``history``。顺序和
  来历（当时的提示词、画风、照着哪张照片）只有记下来才有，目录本身答不出
  "这是第 4 段上一张"这种问题。
* 这个模块扫一遍任务目录。**老任务没有 history**，而用户丢掉的那张正是在老
  任务里——只靠 history 的方案对他此刻的问题毫无用处。扫目录是唯一能立刻
  把那些图还给他的办法。

为什么不列那些 png
------------------
本机出图落地的是一张 png，紧接着烘成 ``<摘要>.zoom-<参数>.mp4``（运镜烘进去
了），上时间线的是后者。两个都列等于每张图出现两遍。所以：**视频一律列，图片
只在某一段现在用着它或曾经用着它时才列**。后者是上传替换进来的素材，那种
png 本身就是素材；中间产物不是。

为什么按摘要归并
----------------
同一张 png 可能烘出好几个 mp4（时长变了、运镜换了），文件名前 64 位是同一个
摘要。那是同一张画面，不该在墙上出现三格。归并只看文件名，不碰磁盘——
``timeline.source_image_for`` 要把目录里每张 png 重算一遍摘要，25 张图一次就
是一分钟，不能放进一个列表接口里。
"""

import os
import re
from typing import Any, Iterable, Optional

from app.services import scene_manifest, timeline
from app.utils import utils


class ShelfError(Exception):
    """这面墙摆不出来。文案给人看。"""


#: 渲染出来的成品不是"画过的一张图"，它是这些图拼起来的结果。
RENDER_OUTPUTS = re.compile(r"^(?:combined|final)-\d+\.mp4$", re.IGNORECASE)

VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


# A clip made by the video model, named by scene_video: how it was made is in
# the name, so it is known even for a file no scene has a record of.
GENERATED_CLIP = re.compile(r"^wan-(image|text)-", re.IGNORECASE)


def generated_mode(name: Any) -> str:
    """"image" or "text" for a generated video clip, "" for anything else."""
    found = GENERATED_CLIP.match(scene_manifest.material_key(name))
    return found.group(1).lower() if found else ""


def media_of(name: Any) -> str:
    """What the viewer sees: a "picture" that is held still, or a "video".

    A picture is stored as a clip too (the still, baked with its camera move),
    so the file type does not say. What it was made from does.
    """
    basename = scene_manifest.material_key(name)
    if os.path.splitext(basename)[1].lower() in IMAGE_SUFFIXES:
        return "picture"
    return "picture" if timeline.baked_from_an_image(basename) else "video"


def task_folder(task_id: str) -> str:
    """任务目录的绝对路径。不存在就报错，而不是建一个。

    自己拼，不走 ``utils.task_dir(task_id)``：那个函数会把目录建出来，于是
    下面这句 ``isdir`` 永远为真。同 ``reveal.path_in_task``。
    """
    folder = os.path.abspath(os.path.join(utils.task_dir(), str(task_id or "")))
    if not os.path.isdir(folder):
        raise ShelfError(f"task {task_id} has no folder on disk yet")
    return folder


def group_key(name: Any) -> str:
    """同一张画面的几个文件共用的身份。

    图片烘出来的片段叫 ``<64 位摘要>.zoom-<参数>.mp4``，摘要算的是那张 png，
    所以前 64 位相同就是同一张画面。别的素材（库里下回来的 mp4、上传的图）
    文件名本身就是身份。
    """
    basename = scene_manifest.material_key(name)
    if timeline.baked_from_an_image(basename):
        return basename.split(".", 1)[0]
    return basename


def _is_material(name: str, *, referenced: set) -> bool:
    """这个文件是不是"画过的一张图"。"""
    if RENDER_OUTPUTS.match(name):
        return False
    suffix = os.path.splitext(name)[1].lower()
    if suffix in VIDEO_SUFFIXES:
        return True
    # 中间产物的 png 不列；上传替换进来的 png 列。区别就是有没有被当成素材用过。
    return suffix in IMAGE_SUFFIXES and name in referenced


def _history_of(scene: Any) -> list:
    recorded = scene.get("history") if isinstance(scene, dict) else None
    return recorded if isinstance(recorded, list) else []


def _provenance(scenes: Iterable[dict]) -> dict:
    """文件名 → 关于它我们知道的事。

    现在用着它的那一段最权威（清单里那几个字段说的就是它）；其次是某一段的
    history 里记下的那一条。两边都没有（老任务里那些被换下来的图）就只有
    文件本身，那也够用——用户认得出自己的画面。
    """
    known: dict = {}
    for scene in scenes:
        key = scene_manifest.material_key(scene.get("material"))
        if key:
            known[key] = {
                "keyword": str(scene.get("keyword") or ""),
                "image_style": str(scene.get("image_style") or ""),
                "reference": scene.get("reference"),
                "in_use": scene.get("index"),
                "was_on": None,
            }
    for scene in scenes:
        for entry in _history_of(scene):
            key = scene_manifest.material_key(entry.get("material"))
            if not key or key in known:
                continue
            known[key] = {
                "keyword": str(entry.get("keyword") or ""),
                "image_style": str(entry.get("image_style") or ""),
                "reference": entry.get("reference"),
                "in_use": None,
                "was_on": scene.get("index"),
                "replaced_at": entry.get("at"),
            }
    return known


def shelf(task_id: str, endpoint: str = "") -> list:
    """这条片子画过的每一张图，新的在前。

    每一格是一张**画面**，不是一个文件：同一张图的几个运镜版本归成一格，代表
    文件优先选现在正用着的那一个（它一定能播，而且时长正好），否则选最新的。
    """
    folder = task_folder(task_id)
    try:
        manifest = scene_manifest.load(task_id)
    except Exception:  # noqa: BLE001  清单读不出来时目录本身还是能摆出来的
        manifest = {"scenes": []}
    scenes = manifest.get("scenes") or []

    known = _provenance(scenes)
    referenced = set(known)

    groups: dict = {}
    try:
        names = os.listdir(folder)
    except OSError as exc:
        raise ShelfError(f"cannot read the folder of task {task_id}: {exc}") from exc

    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if not _is_material(name, referenced=referenced):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        about = dict(known.get(name) or {})
        tile = {
            "material": name,
            "url": _stream_url(task_id, name, endpoint),
            "kind": scene_manifest.material_kind(name),
            # "picture" or "video", and for a generated clip how it was made:
            # "image" (a picture animated) or "text" (drawn from the prompt).
            "media": media_of(name),
            "video_mode": generated_mode(name),
            "made_at": round(stat.st_mtime, 3),
            "bytes": stat.st_size,
            "in_use": about.get("in_use"),
            "was_on": about.get("was_on"),
            "keyword": about.get("keyword") or "",
            "image_style": about.get("image_style") or "",
        }
        reference = about.get("reference")
        if isinstance(reference, dict) and reference:
            tile["reference"] = reference
        if about.get("replaced_at"):
            tile["replaced_at"] = about["replaced_at"]

        key = group_key(name)
        kept = groups.get(key)
        if kept is None or _better_tile(tile, kept):
            if kept is not None:
                tile["variants"] = kept.get("variants", 1) + 1
            groups[key] = tile
        else:
            kept["variants"] = kept.get("variants", 1) + 1

    wall = list(groups.values())
    # 新的在前。同一秒落地的几个按文件名定序，免得每次刷新顺序都在跳。
    wall.sort(key=lambda tile: (-tile["made_at"], tile["material"]))
    return wall


def _better_tile(candidate: dict, kept: dict) -> bool:
    """同一张画面的两个文件，哪一个拿出来代表它。

    正在用的那一个赢：它一定播得出来，时长也正好是这一段要的。否则新的赢。
    """
    if (candidate.get("in_use") is None) != (kept.get("in_use") is None):
        return kept.get("in_use") is None
    return candidate["made_at"] > kept["made_at"]


def _stream_url(task_id: str, name: str, endpoint: str = "") -> str:
    base = (endpoint or "").rstrip("/")
    tail = f"/api/v1/stream/{task_id}/{name}"
    return f"{base}{tail}" if base else tail


def find(task_id: str, material: Any) -> Optional[dict]:
    """墙上那一格，按文件名找。找不到返回 ``None``。

    还原时用：客户端递过来的是文件名，而"它到底是不是这条片子画过的东西"
    得由服务端判断，不能信客户端。
    """
    wanted = scene_manifest.material_key(material)
    if not wanted:
        return None
    wall = shelf(task_id)
    for tile in wall:
        if tile["material"] == wanted:
            return tile
    # 归并之后代表变了（同一张图的另一个运镜版本），但那个文件确实是这条片子
    # 画过的东西，仍然该让它还原回去。文件得真的在，否则就不是"还原"而是把
    # 一段画面指向一个不存在的文件。
    key = group_key(wanted)
    for tile in wall:
        if group_key(tile["material"]) != key:
            continue
        if not os.path.isfile(os.path.join(task_folder(task_id), wanted)):
            return None
        return {**tile, "material": wanted, "url": _stream_url(task_id, wanted)}
    return None
