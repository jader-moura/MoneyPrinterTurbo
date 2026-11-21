"""审片期间对单个场景的编辑操作：重新生成、替换、改台词、重排、删除。

Why this is not in ``scene_manifest``
------------------------------------
``scene_manifest`` 被 ``task`` 导入,所以它不能反过来导入 ``task`` 或 ``material``。
编辑操作需要真正去调供应商和读任务参数,因此单独成模块：依赖方向保持
``task -> scene_manifest`` 和 ``scene_editor -> {task, material, scene_manifest}``,
不出现环。

``scene_manifest`` is imported by ``task``, so it cannot import ``task`` or
``material`` back. These operations do call providers and read task params, so
they live here instead: the dependency direction stays
``task -> scene_manifest`` and ``scene_editor -> {task, material, scene_manifest}``.

Why regeneration goes through ``material.download_videos``
----------------------------------------------------------
每个素材源的差异已经在 ``download_videos`` 里收敛过一次了。重新生成一个场景
只要把 ``search_terms`` 收窄成一个关键词、把目标时长收窄成一段片长,就能对
所有素材源复用同一条路径,而不是为 openai_image 单独写一份、再为 pexels 写
一份。``get_video_materials`` 则是整批规划,不能用来做单条重生成。

Every source's differences are already reconciled inside ``download_videos``.
Regenerating one scene narrows ``search_terms`` to a single keyword and the
target duration to one clip, which reuses that single path for every source
instead of special-casing each one. ``get_video_materials`` plans the whole set
at once and is the wrong entry point for a single scene.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from loguru import logger

from app.models import image_style as image_style_model
from app.models.schema import (
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, material, picture_shelf, reference_image
from app.services import edit_history, scene_manifest, scene_plan, task_artifacts, timeline
from app.services import bgm, finishing, transition_sound, voice
from app.utils import subtitle_reader, subtitle_writer
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

SceneError = scene_manifest.SceneError


class SceneBusyError(SceneError):
    """任务正在渲染或发布，此时编辑场景会和渲染读到的文件打架。"""


# 这些源不是按关键词取素材的，重新生成一个场景没有意义。
# These sources are not keyword-driven, so regenerating one scene is meaningless.
_UNSUPPORTED_REGEN_SOURCES = ("local", "loomloom")


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _editing(task_id: str) -> Iterator[None]:
    """把"读清单 → 改 → 写回"圈成一个不可分割的动作。

    为什么现在需要：重生成排了队之后，写清单的不只有正在处理的那个 HTTP
    请求了——后台那个工作线程出完一张图就会写一次。两边都是"先读整份清单，
    改掉一段，再整份写回去",交错起来后写的那次会把先写的那次完全盖掉:
    用户在等图的时候改了一句台词,图出来的瞬间那句台词就没了。

    锁按任务分：两个任务互不相干，没有理由互相等。

    注意**不要**把出图圈进来。出图要几分钟，拿着锁出图等于审片界面在这几
    分钟里全部卡住,而排队的全部意义正是让用户能接着干别的。所以慢的那一段
    在锁外面跑,跑完重新读一遍清单再写——这也是 ``_locate_scene`` 存在的原因。

    Both the request thread and the regeneration worker read the whole
    manifest, change one scene and write it all back, so without this the
    later write silently discards the earlier one -- a narration edited while
    a picture was being generated would vanish the moment it finished. The
    lock is per task, and deliberately does not cover generation itself:
    holding it for minutes would freeze the editor, which is exactly what the
    queue is meant to prevent.
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.get(task_id)
        if lock is None:
            # 可重入：下面好几个操作会互相调用（比如重排之后重新读一遍），
            # 普通锁会把自己锁死。
            lock = threading.RLock()
            _LOCKS[task_id] = lock
    with lock:
        yield


def _require_idle(task_id: str) -> None:
    task = sm.state.get_task(task_id)
    if task and tm.is_task_busy(task):
        raise SceneBusyError(
            "this task is still running; wait for it to finish before editing scenes"
        )
    # A batch of video scenes holds the whole graphics card, whichever task it
    # is for. Imported here because that module imports this one.
    from app.services import scene_video

    scene_video.refuse_while_running()


def load_params(task_id: str) -> VideoParams:
    """从任务清单读回参数。

    清单是审片唯一可靠的参数来源：任务状态里不保存 params,而 WebUI 的
    session 在浏览器刷新后就没了。未知字段一律丢弃,因为清单可能是旧版本
    写的,不能让一个历史字段让整个审片界面打不开。
    """
    payload = scene_manifest.load_raw(task_id)
    raw = payload.get("params")
    if not isinstance(raw, dict):
        raise SceneError(
            "this task has no saved parameters; it cannot be re-rendered"
        )
    allowed = set(VideoParams.model_fields)
    cleaned = {key: value for key, value in raw.items() if key in allowed}
    try:
        return VideoParams(**cleaned)
    except Exception as exc:
        raise SceneError(f"saved parameters are not usable: {exc}") from exc


def _scenes_or_fail(task_id: str) -> list[dict]:
    scenes = scene_manifest.load(task_id)["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )
    return scenes


def _scene_at(scenes: list[dict], index: Any) -> int:
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise SceneError(f"scene index must be a whole number, got {index!r}") from exc
    if not 0 <= position < len(scenes):
        raise SceneError(
            f"scene {position} does not exist; this task has {len(scenes)} scenes"
        )
    return position


def _locate_scene(scenes: list[dict], position: int, anchor: str) -> int:
    """出图期间时间线可能变了，把用户当时点的那一段找回来。

    按身份认，不按下标认：排队的那几分钟里用户可能删掉、插入或者重排了
    场景，下标就指向另一段画面了。把新图写到一段用户没动过的画面上，比
    这次重生成直接失败要糟得多——他会以为那段画面自己坏了。

    身份是 ``scene_manifest.scene_id``：有 ``id`` 就用 ``id``，老清单没有
    这个字段时退回素材文件名。还没出图的占位场景没有素材文件，所以**只能**
    靠 ``id`` 认——新流程里一屏全是这种场景，拿文件名认等于认不出来。

    那一段真的不在了（被删了）就点名说清楚。生成出来的文件留在磁盘上没人
    用，这不好看，但比悄悄塞给另一段画面好。

    Identity is the scene id, falling back to the material file name for
    manifests written before ids existed. A scene waiting to be drawn has no
    file at all, so for those the id is the only thing to go on.
    """
    if not anchor:
        return _scene_at(scenes, position)
    if (
        0 <= position < len(scenes)
        and scene_manifest.scene_id(scenes[position]) == anchor
    ):
        return position
    for candidate, scene in enumerate(scenes):
        if scene_manifest.scene_id(scene) == anchor:
            return candidate
    raise SceneError(
        "the scene this picture was generated for is no longer in the timeline; "
        "ask for it again if you still want it"
    )


def check_regenerate(
    task_id: str,
    index: Any,
    *,
    keyword: str | None = None,
    image_style: str | None = None,
) -> dict:
    """能不能重生成这一段；能的话返回执行它需要的东西。

    为排队而拆出来的：这些失败改请求才能解决（任务在渲染中、下标越界、这个
    素材源不按关键词出图、风格 id 不认识），所以必须在**排队的那一刻**就说，
    而不是等队列轮到它、几分钟之后再冒出来——那时候用户只会以为是出图失败。

    返回的 ``material`` 是这一段现在用的素材文件，排队期间时间线变了就靠它
    把这一段找回来（见 ``_locate_scene``）。

    Split out for the queue: these failures are fixed by a different request,
    so they must be reported when the job is accepted rather than minutes
    later, where they would look like a generator failure.
    """
    _require_idle(task_id)
    params = load_params(task_id)
    source = str(getattr(params, "video_source", "") or "")
    if source in _UNSUPPORTED_REGEN_SOURCES:
        raise SceneError(
            f"video source {source!r} does not generate materials from keywords; "
            "replace this scene with your own file instead"
        )
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = dict(scenes[position])
    term = str(keyword if keyword is not None else scene.get("keyword") or "").strip()
    if not term:
        term = suggested_keyword(task_id, position)
    if not term:
        raise SceneError("no keyword to generate from; provide one with the request")
    # 风格 id 认不认识现在就说。不传就按这一段记着的那个风格验——真正出图时
    # 用的也是它。
    effective_style = (
        image_style if image_style is not None else str(scene.get("image_style") or "")
    )
    _image_prompt_template(params, effective_style)
    return {
        "index": position,
        "keyword": term,
        # 排队的活拿它认人。身份而不是文件名：还没出图的占位场景没有文件，
        # 而"先审提示词再出图"那条流程里排队的几乎全是这种场景。
        "scene_id": scene_manifest.scene_id(scene),
        "material": str(scene.get("material") or ""),
    }


def find_reference_photo(
    task_id: str,
    index: Any,
    *,
    search: str = "",
    keyword: str | None = None,
) -> Any:
    """给这一段找一张真实照片，当重画的底子。

    两步，而且两步都可能失败得很具体：先让模型把这一段压成几个可搜的词
    （提示词本身带着画风，拿它去搜图搜回来的是别人画的漫画），再去搜一张
    能用的照片。``search`` 给了就跳过第一步——用户自己知道要搜什么的时候，
    没有理由再让模型猜一遍。

    返回 ``reference_image.Reference``。失败一律是 ``SceneError``：这两件事
    的失败都是用户能看懂、也能处理的（配置缺一项、搜不到东西、下不下来），
    所以原文照传。
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    position = _scene_at(scenes, index)
    scene = scenes[position]
    term = str(
        keyword if keyword is not None else scene.get("keyword") or ""
    ).strip()

    wanted = " ".join(str(search or "").split())
    if not wanted:
        spoken = narration_for_scenes(task_id, manifest)
        line = ""
        if 0 <= position < len(spoken):
            line = " ".join(str(spoken[position] or "").split())
        params = load_params(task_id)
        subject = str(getattr(params, "video_subject", "") or "").strip()
        try:
            wanted = llm.image_search_query(subject, line, term)
        except llm.TermGenerationError as exc:
            raise SceneError(str(exc)) from exc

    # 别的段落已经在用的照片不要再拿一次：公开授权的库很小，同一个题目很
    # 容易一连几段都拿到同一张。
    used = []
    for other_at, other in enumerate(scenes):
        if other_at == position:
            continue
        recorded = other.get("reference")
        if isinstance(recorded, dict):
            source = str(recorded.get("source_url") or "").strip()
            if source:
                used.append(source)

    try:
        return reference_image.search(wanted, avoid=used)
    except reference_image.ReferenceImageError as exc:
        raise SceneError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 挑一张照片
# ---------------------------------------------------------------------------
#
# "照着照片重画"以前是全自动的：搜一遍，拿第一张下得来的，直接开画。画完
# 三分钟之后才看到它拿的是哪一张——而"第一张下得来的"常常不是要的那一张。
# 现在先把搜到的摆出来给人挑，也可以自己传一张。

# 搜出来给人挑过的那些，按任务记着。排队的时候只认这里面的地址：否则
# "照这个地址下载一张图"就成了一个谁都能让服务器去访问任意网址的接口。
_OFFERED: dict = {}
_OFFERED_TASKS = 40
_OFFERED_PER_TASK = 200
# 传上来、还没用掉的照片。放在内存里：它们只在"传完 → 点重画"这几秒里有用。
_UPLOADED: dict = {}
_UPLOADED_KEPT = 8
_PHOTO_LOCK = threading.Lock()


def _photo_search_words(task_id: str, index: Any, search: str,
                        keyword: str | None) -> tuple:
    """这一段该搜什么，以及这条片子里别的段已经用过哪些照片。"""
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    position = _scene_at(scenes, index)
    scene = scenes[position]
    term = str(keyword if keyword is not None else scene.get("keyword") or "").strip()
    wanted = " ".join(str(search or "").split())
    if not wanted:
        spoken = narration_for_scenes(task_id, manifest)
        line = ""
        if 0 <= position < len(spoken):
            line = " ".join(str(spoken[position] or "").split())
        subject = str(
            getattr(load_params(task_id), "video_subject", "") or ""
        ).strip()
        try:
            wanted = llm.image_search_query(subject, line, term)
        except llm.TermGenerationError as exc:
            raise SceneError(str(exc)) from exc
    used = []
    for other_at, other in enumerate(scenes):
        if other_at == position:
            continue
        recorded = other.get("reference")
        if isinstance(recorded, dict):
            source = str(recorded.get("source_url") or "").strip()
            if source:
                used.append(source)
    return wanted, used


def reference_photo_words(task_id: str, index: Any) -> dict:
    """这一段该搜什么：只写词，不搜。

    和搜索分成两步，是为了让词先落到搜索框里。合在一起的话，搜索那一步
    失败（图库抽风是常事）时词也跟着丢了，用户面前是一个空框和一句报错，
    只能自己从头想搜什么。

    上次照着照片画过的那一段，直接用上次的词：那是用户已经见过、多半也
    改过的，比让模型重新猜一遍靠得住。
    """
    scenes = scene_manifest.load(task_id)["scenes"]
    scene = scenes[_scene_at(scenes, index)]
    recorded = scene.get("reference")
    before = ""
    if isinstance(recorded, dict):
        before = " ".join(str(recorded.get("query") or "").split())
    if before:
        return {"query": before, "from": "last"}
    # 外面写好的分镜给这一段写了要搜什么：用它的，不问模型。
    planned = task_artifacts.read_script_data(task_id).get(_CANDIDATES_KEY)
    entry = planned.get(scene_manifest.scene_id(scene)) if isinstance(planned, dict) else None
    if isinstance(entry, dict):
        wanted = " ".join(str(entry.get("search") or "").split())
        if wanted:
            return {"query": wanted, "from": "plan"}
    words, _ = _photo_search_words(task_id, index, "", None)
    return {"query": words, "from": "model"}


#: 任务清单里记着"哪几段点了一个真实的名字"的那一项。
_CANDIDATES_KEY = "photo_candidates"
#: 任务清单里记着"每一段定下了哪张照片"的那一项。
_PICKS_KEY = "photo_picks"
#: 定下来、还没画的照片放在任务目录下的这个文件夹里。
PHOTO_PICK_DIR = "photo_picks"
_NO_CANDIDATE = {"kind": "none", "score": 0, "name": ""}


def photo_candidates(task_id: str, *, again: bool = False) -> dict:
    """每一段台词有没有点一个真实的人、地方、东西的名字（人排最前）。

    只算一次，记在任务清单里：流水线把场景切出来的时候就算（见
    ``task.py``），之后点多少次"开画"读的都是记下来的这一份——每点一次
    重新问一遍模型，既慢，两次的答案还不一样，上一次挑好的照片就对不上了。
    那一次没算成（模型没应答）的话，第一次点"开画"时在这里补算。

    ``again`` 是"重新算"。后来才加出来的段没有记录，算作没点名。
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    stored = task_artifacts.read_script_data(task_id).get(_CANDIDATES_KEY)
    if again or not isinstance(stored, dict):
        spoken = narration_for_scenes(task_id, manifest)
        subject = str(
            getattr(load_params(task_id), "video_subject", "") or ""
        ).strip()
        material_for_model = [
            (spoken[at] if at < len(spoken) else "", scene.get("keyword") or "")
            for at, scene in enumerate(scenes)
        ]
        try:
            rated = llm.rate_scenes_for_photos(subject, material_for_model)
        except llm.TermGenerationError as exc:
            raise SceneError(str(exc)) from exc
        stored = {
            scene_manifest.scene_id(scene): dict(rated[at])
            for at, scene in enumerate(scenes)
        }
        task_artifacts.patch_script_data(task_id, **{_CANDIDATES_KEY: stored})
    listed = []
    for at, scene in enumerate(scenes):
        key = scene_manifest.scene_id(scene)
        found = stored.get(key) if isinstance(stored.get(key), dict) else {}
        listed.append({
            "index": at, "id": key,
            **{name: found.get(name, default)
               for name, default in _NO_CANDIDATE.items()},
        })
    return {"scenes": listed}


def keep_planned_photo_scenes(task_id: str, planned: list) -> int:
    """照外面写好的分镜记下"哪几段照着照片画、找什么"，返回有几段。

    和 ``photo_candidates`` 记的是同一项，所以"开画"时挑照片的那一轮读到的
    就是分镜点的那几段——不问模型，分镜说了算。
    """
    scenes = scene_manifest.load(task_id)["scenes"]
    found = scene_plan.photo_candidates(
        list(planned or []), [scene_manifest.scene_id(scene) for scene in scenes]
    )
    task_artifacts.patch_script_data(task_id, **{_CANDIDATES_KEY: found})
    return sum(1 for one in found.values() if one["kind"] != "none")


def _photo_pick_file(task_id: str, name: str) -> str:
    return os.path.join(utils.task_dir(), str(task_id), PHOTO_PICK_DIR, name)


def _stored_photo_picks(task_id: str) -> dict:
    stored = task_artifacts.read_script_data(task_id).get(_PICKS_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def keep_photo_pick(
    task_id: str,
    index: Any,
    *,
    photo: Any = None,
    skip: bool = False,
    search: str = "",
    strength: float | None = None,
    follow: str = "",
) -> dict:
    """把一段定下的照片存进这条片子里，不画。

    开画之前一口气给十几段挑照片时用。照片存在磁盘上、选择记在任务清单里：
    以前它们只在内存里放最近八张，挑到第九张第一张就没了；页面一刷新、
    程序一重启也全没了。存下来之后，"开画"点多少次用的都是这一张，出图
    失败了重来也不用再挑。

    ``skip`` 是"这一段不要照片"；``photo`` 空着又不是 ``skip``，是"替我挑
    一张"（开画时自己去搜）。
    """
    import hashlib

    scenes = scene_manifest.load(task_id)["scenes"]
    scene = scenes[_scene_at(scenes, index)]
    key = scene_manifest.scene_id(scene)
    way = str(follow or "").strip()
    if way and way not in FOLLOW_WAYS:
        raise SceneError(f"unknown way to follow a photo: {way!r}")
    if skip:
        entry: dict = {"skip": True}
    else:
        entry = {
            "search": " ".join(str(search or "").split()),
            "strength": strength,
            "follow": way,
        }
        if photo is not None:
            name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".png"
            path = _photo_pick_file(task_id, name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(photo.data)
            except OSError as exc:
                raise SceneError(f"the chosen photo could not be kept: {exc}") from exc
            entry.update(
                file=name,
                query=photo.query, source_url=photo.source_url,
                page_url=photo.page_url, provider=photo.provider,
                width=photo.width, height=photo.height,
                license=photo.licence, creator=photo.creator,
            )
    with _PHOTO_LOCK:
        picks = _stored_photo_picks(task_id)
        picks[key] = entry
        task_artifacts.patch_script_data(task_id, **{_PICKS_KEY: picks})
    return {"scene_id": key, "pick": _pick_kind(entry)}


def picked_photo_path(task_id: str, index: Any) -> str:
    """给这一段定下的那张照片在磁盘上的位置。"""
    scenes = scene_manifest.load(task_id)["scenes"]
    key = scene_manifest.scene_id(scenes[_scene_at(scenes, index)])
    entry = _stored_photo_picks(task_id).get(key)
    if _pick_kind(entry) != "photo":
        raise SceneError("no photo is saved for this scene")
    path = _photo_pick_file(task_id, str(entry["file"]))
    if not os.path.isfile(path):
        raise SceneError("the photo saved for this scene is no longer on disk")
    return path


def picked_reference_photo(task_id: str, index: Any) -> Any:
    """给这一段定下的那张照片，拿来画（或者再画一次）。"""
    scenes = scene_manifest.load(task_id)["scenes"]
    key = scene_manifest.scene_id(scenes[_scene_at(scenes, index)])
    ready = photo_picks_for_drawing(task_id).get(key)
    if not ready or ready.get("photo") is None:
        raise SceneError("no photo is saved for this scene")
    return ready["photo"]


def _pick_kind(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    if entry.get("skip"):
        return "skip"
    return "photo" if entry.get("file") else "auto"


def photo_pick_kinds(task_id: str) -> dict:
    """``{段的身份: "photo" | "auto" | "skip"}``：哪几段已经定过照片了。"""
    return {
        key: _pick_kind(entry)
        for key, entry in _stored_photo_picks(task_id).items()
        if _pick_kind(entry)
    }


def photo_picks_for_drawing(task_id: str) -> dict:
    """``{段的身份: 排队要用的那几项}``，给"开画"用。

    说了不要照片的段不在里面。存着的照片读不回来就报错，不悄悄改成照
    提示词画：那是用户亲手挑的一张。
    """
    ready: dict = {}
    for key, entry in _stored_photo_picks(task_id).items():
        kind = _pick_kind(entry)
        if kind not in ("photo", "auto"):
            continue
        photo = None
        if kind == "photo":
            try:
                with open(_photo_pick_file(task_id, str(entry["file"])), "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                raise SceneError(
                    f"the photo chosen for a scene could not be read back: {exc}"
                ) from exc
            photo = reference_image.Reference(
                data=data,
                query=str(entry.get("query") or ""),
                source_url=str(entry.get("source_url") or ""),
                page_url=str(entry.get("page_url") or ""),
                provider=str(entry.get("provider") or ""),
                width=int(entry.get("width") or 0),
                height=int(entry.get("height") or 0),
                licence=str(entry.get("license") or ""),
                creator=str(entry.get("creator") or ""),
            )
        ready[key] = {
            "from_photo": True,
            "search": str(entry.get("search") or ""),
            "strength": entry.get("strength"),
            "follow": str(entry.get("follow") or ""),
            "photo": photo,
        }
    return ready


def find_reference_photos(
    task_id: str,
    index: Any,
    *,
    search: str = "",
    keyword: str | None = None,
) -> dict:
    """搜一遍，把结果列出来给人挑。不下载、不开画。

    ``search`` 空着就让模型从这一段的台词和提示词里写搜索词（和自动那条路
    是同一步）；给了就照它搜。别的段已经用过的照片不藏起来，只标出来。
    """
    wanted, used = _photo_search_words(task_id, index, search, keyword)
    try:
        found = reference_image.candidates(wanted, avoid=used)
    except reference_image.ReferenceImageError as exc:
        raise SceneError(str(exc)) from exc
    with _PHOTO_LOCK:
        offered = _OFFERED.pop(task_id, {})
        for photo in found["photos"]:
            offered.pop(photo["url"], None)
            offered[photo["url"]] = {
                **photo, "query": found["query"], "provider": found["provider"],
            }
        for stale in list(offered)[:-_OFFERED_PER_TASK]:
            offered.pop(stale, None)
        _OFFERED[task_id] = offered
        for stale in list(_OFFERED)[:-_OFFERED_TASKS]:
            _OFFERED.pop(stale, None)
    return found


def chosen_reference_photo(task_id: str, url: str) -> Any:
    """用户在搜索结果里挑的那一张，下载回来。

    只认刚才搜出来给他看过的地址。
    """
    with _PHOTO_LOCK:
        photo = dict(_OFFERED.get(task_id, {}).get(str(url or "").strip()) or {})
    if not photo:
        raise SceneError(
            "that picture is not one of the search results shown for this "
            "video; search again and choose from the results"
        )
    try:
        return reference_image.chosen(photo)
    except reference_image.ReferenceImageError as exc:
        raise SceneError(str(exc)) from exc


def keep_uploaded_photo(raw: bytes, name: str = "") -> dict:
    """收下用户传上来的一张照片，返回一个凭它来用的名字。"""
    import uuid

    try:
        reference = reference_image.uploaded(raw, name)
    except reference_image.ReferenceImageError as exc:
        raise SceneError(str(exc)) from exc
    token = uuid.uuid4().hex
    with _PHOTO_LOCK:
        _UPLOADED[token] = reference
        for stale in list(_UPLOADED)[:-_UPLOADED_KEPT]:
            _UPLOADED.pop(stale, None)
    return {"file": token, "width": reference.width, "height": reference.height,
            "name": str(name or "")}


def browse_for_photo(task_id: str, index: Any, *, search: str = "") -> dict:
    """开一个小浏览器窗口，在 Google 图片里搜这一段的词，让用户自己点一张。

    不走任何搜索 API。用户点中的那一张和"传一张自己的"是同一条路：同一套
    检查（尺寸、类型、重新编码），排队时凭同一种凭据来用。区别只是它记得
    自己是哪来的，这样别的段再挑到同一张时能标出来。
    """
    from app.services import photo_browser

    scenes = scene_manifest.load(task_id)["scenes"]
    _scene_at(scenes, index)
    words = " ".join(str(search or "").split())
    if not words:
        words = reference_photo_words(task_id, index)["query"]

    def keep(raw: bytes, chosen: dict) -> dict:
        import dataclasses
        import uuid

        reference = reference_image.uploaded(raw, "it")
        source = str(chosen.get("src") or "")
        if not source.lower().startswith(("http://", "https://")):
            source = ""
        reference = dataclasses.replace(
            reference, query=words, source_url=source,
            page_url=source or str(chosen.get("page") or ""),
        )
        token = uuid.uuid4().hex
        with _PHOTO_LOCK:
            _UPLOADED[token] = reference
            for stale in list(_UPLOADED)[:-_UPLOADED_KEPT]:
                _UPLOADED.pop(stale, None)
        return {"file": token, "width": reference.width, "height": reference.height}

    try:
        return photo_browser.start(
            task_id, words, keep, min_edge=reference_image.MIN_LONG_EDGE
        )
    except photo_browser.PhotoBrowserError as exc:
        raise SceneError(str(exc)) from exc


def photo_browser_state(task_id: str) -> dict:
    """那个窗口现在怎么样：还开着、点了一张、被关了。"""
    from app.services import photo_browser

    return photo_browser.status(task_id)


def close_photo_browser(task_id: str) -> dict:
    from app.services import photo_browser

    return photo_browser.close(task_id)


def uploaded_reference_photo(token: str) -> Any:
    with _PHOTO_LOCK:
        reference = _UPLOADED.get(str(token or "").strip())
    if reference is None:
        raise SceneError("that uploaded photo is no longer here; upload it again")
    return reference


# 摆好的照片按这个长边出：和出图的画幅一个量级，再大只是多传几兆。
FRAMED_LONG_EDGE = 1536


def framed_photo(task_id: str, reference: Any, frame: Any) -> Any:
    """照片按用户摆好的大小和位置放进这条片子的画框里，返回放好的那一张。

    ``frame`` 见 ``PhotoFrame``。没动过（1、0、0）就原样返回——那时候整张
    照片交给出图服务，由它自己不裁地放进去。动过的话在这里放好：照片没
    盖住的地方用它自己糊开了的样子垫上，和出图服务那边是同一个办法。

    The photo placed in the video's frame the way the user set it. Untouched
    (zoom 1, centred) it is returned as it is.
    """
    import dataclasses
    import io

    from PIL import Image, ImageFilter

    frame = dict(frame or {})
    try:
        zoom = float(frame.get("zoom", 1) or 1)
        x = float(frame.get("x", 0) or 0)
        y = float(frame.get("y", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise SceneError("the photo's size and position must be numbers") from exc
    if not 0.25 <= zoom <= 4 or abs(x) > 1 or abs(y) > 1:
        raise SceneError(
            "the photo's size must be between 25% and 400%, and it cannot be "
            "moved more than one frame away from the middle"
        )
    if zoom == 1 and x == 0 and y == 0:
        return reference
    params = load_params(task_id)
    wide, high = VideoAspect(
        getattr(params, "video_aspect", None) or VideoAspect.portrait
    ).to_resolution()
    scale = FRAMED_LONG_EDGE / max(wide, high)
    width, height = int(wide * scale + 0.5), int(high * scale + 0.5)
    try:
        with Image.open(io.BytesIO(reference.data)) as opened:
            photo = opened.convert("RGB")
    except Exception as exc:
        raise SceneError(f"the chosen photo could not be read: {exc}") from exc
    fit = min(width / photo.width, height / photo.height) * zoom
    size = (max(1, int(photo.width * fit + 0.5)), max(1, int(photo.height * fit + 0.5)))
    left = int(width / 2 + x * width - size[0] / 2 + 0.5)
    top = int(height / 2 + y * height - size[1] / 2 + 0.5)
    seen = (max(left, 0), max(top, 0),
            min(left + size[0], width), min(top + size[1], height))
    if seen[0] >= seen[2] or seen[1] >= seen[3]:
        raise SceneError(
            "the photo was moved completely out of the frame; move it back in"
        )
    cover = max(width / photo.width, height / photo.height)
    behind = photo.resize(
        (max(width, int(photo.width * cover + 0.5)),
         max(height, int(photo.height * cover + 0.5))),
        Image.BILINEAR,
    )
    edge_x, edge_y = (behind.width - width) // 2, (behind.height - height) // 2
    behind = behind.crop((edge_x, edge_y, edge_x + width, edge_y + height)).filter(
        ImageFilter.GaussianBlur(max(width, height) / 32)
    )
    placed = behind.copy()
    placed.paste(photo.resize(size, Image.LANCZOS), (left, top))
    # 照片和垫底的那一层之间是一条软边，不是一条直线：直线是个形状，模型
    # 会把它画出来。只在照片没顶到画框边的那几条边上。
    soft = max(2, min(width, height) // 90)
    inner = (seen[0] + (soft if seen[0] > 0 else 0),
             seen[1] + (soft if seen[1] > 0 else 0),
             seen[2] - (soft if seen[2] < width else 0),
             seen[3] - (soft if seen[3] < height else 0))
    mask = Image.new("L", (width, height), 0)
    if inner[0] < inner[2] and inner[1] < inner[3]:
        mask.paste(255, inner)
    mask = mask.filter(ImageFilter.GaussianBlur(soft / 2))
    out = io.BytesIO()
    Image.composite(placed, behind, mask).save(out, "PNG")
    return dataclasses.replace(
        reference, data=out.getvalue(), width=width, height=height
    )


def regenerate_material(
    task_id: str,
    index: Any,
    *,
    keyword: str | None = None,
    image_style: str | None = None,
    expect_scene: str | None = None,
    reference: Any = None,
    strength: float | None = None,
    follow: str = "",
) -> dict:
    """重新取这一个场景的素材。

    ``keyword`` 给了就同时改写场景关键词——用户看到画面不对时,改的往往正是
    关键词,所以这两件事应该一次做完,而不是让用户先改关键词再点一次重生成。

    ``image_style`` 给了就只给这一张换个画风;不给就用任务自己记下的那个,
    也就是出片时真正用的那个。换过的风格记在场景上,界面下次打开还显示它,
    用户不用每次重新挑。

    对库存素材源（Pexels 等）有一个已知限制：搜索结果有缓存,同一个关键词
    重新生成很可能拿回同一个文件。本地出图的源每次都是新的采样,所以重生成
    确实会得到不同的画面。这里不绕过缓存——绕过等于让用户在不知情的情况下
    多花一次第三方额度。

    ``expect_scene`` 给的是排队时那一段的身份（``scene_manifest.scene_id``）。
    队列里排着的活执行时下标可能已经指向别的画面了（用户删了、插了、重排
    了），给了它就按身份把那一段找回来。见 ``_locate_scene``。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]
        term = str(
            keyword if keyword is not None else scene.get("keyword") or ""
        ).strip()
        # 三种情况要分开，否则界面上那个下拉框会说谎。
        #
        # 不传（``None``）：沿用这一段上次挑过的风格。不这样的话，在编辑器里
        # 把某一段改成漫画风，下一次重生成又会悄悄变回去。老版客户端和不画
        # 下拉框的界面走的都是这条路。
        # 传空字符串：用户在下拉框里选了"和这条片子一样"，那就回到任务记下
        # 的风格，并且把这一段上记着的那个选择清掉——留着它，下次打开界面
        # 显示的还是旧选择，而画出来的已经不是它了。
        # 传风格 id：只给这一张换画风，并记住它。
        if image_style is None:
            style = str(scene.get("image_style") or "").strip()
        else:
            style = str(image_style).strip()
        # 身份，不是文件名：占位场景还没有文件。
        anchor = str(expect_scene or "") or scene_manifest.scene_id(scene)

    # 出图在锁外面。本机一张图要几分钟，拿着锁出图等于这几分钟里这个任务
    # 什么都改不了。
    # 一次算清楚，出图和记在场景上的是同一个数：日后回头看"这张是怎么来
    # 的"，答案必须是真的那一个。
    # 照着照片画有两种办法，一次只用一种：
    #   * 从照片出发，改掉一部分（``strength`` 说改多少）；
    #   * 只留照片的轮廓，按我的画风从头画（``follow``）——同一张图，换了画风。
    follow = str(follow or "").strip()
    if follow and follow not in FOLLOW_WAYS:
        raise SceneError(
            f"unknown way of following a photo {follow!r}; pick one of: "
            f"{', '.join(FOLLOW_WAYS)}"
        )
    if follow and reference is None:
        raise SceneError("redrawing a photo in your style needs a photo")
    kept = None
    if reference is not None and not follow:
        kept = float(strength) if strength else reference_image.strength()

    new_path, source = _produce_material(
        task_id,
        term,
        what=f"scene {position}",
        image_style=style,
        reference=reference,
        strength=kept,
        follow=follow,
    )
    # 照着画的那张照片留一份（锁外面：只是写一个文件）。
    used_photo = keep_used_photo(task_id, reference) if reference is not None else ""

    # 重新读一遍清单：刚才那几分钟里用户完全可能改过别的场景，拿出图之前
    # 那份内存里的旧清单写回去会把那些改动全盖掉。
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _locate_scene(scenes, position, anchor)
        scene = scenes[position]
        # 换下来的那一张**先**记下来，这一句必须排在所有改写之前：它记的是
        # "那个文件当时是凭什么画出来的"，而下面几行正在把画风、提示词、照片
        # 来历改成这一张新图的。顺序错了，历史里就会是旧文件配新画风——换回去
        # 的时候拿回一张图和一条假的来历。
        #
        # 为什么要记：重生成是"换掉"而不是"修改"。用户点一次，上一张就不在
        # 时间线上了，而他常常是看过新的才知道旧的更好。文件一直都在磁盘上，
        # 缺的只是那个文件名。
        #
        # 路径没变就不记（库存源的搜索带缓存，同一个关键词可能拿回同一个文件）：
        # 把正在用的那一张也记成"旧的"会让界面上多出一格假的历史。
        if scene_manifest.material_key(scene.get("material")) != (
            scene_manifest.material_key(new_path)
        ):
            scene_manifest.remember_material(scene)
        if image_style is not None:
            if style:
                scene["image_style"] = style
            else:
                scene.pop("image_style", None)
        scene["material"] = new_path
        scene["kind"] = scene_manifest.material_kind(new_path)
        scene["source"] = source
        scene["keyword"] = term
        # 这一张是照着哪张照片画的，或者不再是了。记的是**现在这张图**怎么
        # 来的，所以普通重生成要把旧的那条抹掉——留着它就是说谎。
        if reference is not None:
            scene["reference"] = {**reference.payload()}
            if follow:
                scene["reference"]["follow"] = follow
            else:
                scene["reference"]["strength"] = kept
            if used_photo:
                scene["reference"]["file"] = used_photo
        else:
            scene.pop("reference", None)
        # 用户亲手重生成过的结果，不应该被后续批量操作再覆盖掉。
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        return scene


# ---------------------------------------------------------------------------
# 照着画的那张照片，留一份
# ---------------------------------------------------------------------------
#
# 以前只记了照片的地址。可地址说明不了什么：用户想知道的是"这张图为什么画
# 成这样"，答案是那张照片长什么样——而地址会失效，粘贴和上传的根本没有地
# 址。所以把照片本身（已经重新编码过的那份 PNG）存进任务目录，和画出来的图
# 放在一起看。
#
# 放在子目录里：任务目录本身是"画过的图"那面墙要扫的地方，照片不是画出来
# 的图。文件名是内容的摘要，所以同一张照片画几段也只存一份。

USED_PHOTO_DIR = "references"

#: 照着照片画时"跟着它的什么走"。现在只有一种：轮廓。
FOLLOW_WAYS = ("outlines", "picture")
_USED_PHOTO_NAME = re.compile(r"[0-9a-f]{16}\.png")


def keep_used_photo(task_id: str, reference: Any) -> str:
    """把这张照片存进任务目录，返回文件名。存不下不该连累那张已经画了几
    分钟的图：记一条日志，返回空串，这一段就只记得住地址。"""
    import hashlib

    data = getattr(reference, "data", b"") or b""
    if not data:
        return ""
    name = hashlib.sha256(data).hexdigest()[:16] + ".png"
    folder = os.path.join(utils.task_dir(), str(task_id), USED_PHOTO_DIR)
    path = os.path.join(folder, name)
    try:
        if not os.path.isfile(path):
            os.makedirs(folder, exist_ok=True)
            partial = path + ".part"
            with open(partial, "wb") as handle:
                handle.write(data)
            os.replace(partial, path)
    except OSError as exc:
        logger.warning(f"could not keep the photo a scene was drawn from: {exc}")
        return ""
    return name


def used_photo_path(task_id: str, name: str) -> str:
    """某一段照着画的那张照片在磁盘上的位置。"""
    wanted = str(name or "").strip()
    if not _USED_PHOTO_NAME.fullmatch(wanted):
        raise SceneError("that is not the name of a kept photo")
    path = os.path.join(utils.task_dir(), str(task_id), USED_PHOTO_DIR, wanted)
    if not os.path.isfile(path):
        raise SceneError("that photo is not kept for this video")
    return path


def kept_reference_photo(task_id: str, name: str) -> Any:
    """存下来的那张照片，拿来再画一次。

    用户常常是看了结果才知道该留多少：照片留得太多，画风没了；留得太少，
    认不出是那张照片。同一张照片换一档再画，不该让他重新去搜、去点。

    来历（搜的词、照片的地址）从记着这个文件的那一段上抄回来——这一张新
    图照的还是同一张照片。
    """
    import dataclasses

    path = used_photo_path(task_id, name)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        reference = reference_image.uploaded(raw, "the kept photo")
    except (OSError, reference_image.ReferenceImageError) as exc:
        raise SceneError(f"the kept photo could not be used again: {exc}") from exc
    for scene in scene_manifest.load(task_id)["scenes"]:
        recorded = scene.get("reference")
        if isinstance(recorded, dict) and recorded.get("file") == name:
            return dataclasses.replace(
                reference,
                query=str(recorded.get("query") or ""),
                source_url=str(recorded.get("source_url") or ""),
                page_url=str(recorded.get("page_url") or ""),
                licence=str(recorded.get("license") or ""),
                creator=str(recorded.get("creator") or ""),
            )
    return reference


def _image_prompt_template(params: VideoParams, image_style: str | None) -> str:
    """这一次出图要用的画风模板。

    不指定风格就用任务自己记下的那个——出片时真正用的那个,而不是
    config.toml "现在"的值。审片的编辑器跑在 API 进程里,那份配置和 WebUI
    的不是同一份内存,也不会跟着 WebUI 的改动更新,漫画风的片子因此补出过
    一张油画。

    未知的风格 id 直接报错,不静默回退成别的画风：悄悄换一种风格正是这里
    要修的毛病。
    """
    requested = str(image_style or "").strip()
    if not requested:
        return str(getattr(params, "image_prompt_template", "") or "")
    template = image_style_model.template_for_style(requested)
    if not template:
        raise SceneError(
            f"unknown image style {requested!r}; choose one of: "
            + ", ".join(image_style_model.image_style_ids())
        )
    return template


def _produce_material(
    task_id: str,
    term: str,
    *,
    what: str,
    advice: str = "replace this scene with your own file instead",
    image_style: str | None = None,
    reference: Any = None,
    strength: float | None = None,
    follow: str = "",
) -> tuple[str, str]:
    """按关键词取一份素材，返回 ``(文件路径, 素材源)``。

    重生成一段画面和新增一段画面要走完全相同的取材路径，否则两者会在
    画幅、片长、图片动效上慢慢长出差异,最后拼进同一条片子里对不上。

    提示词就是关键词本身（外加画风模板）。重生成是用户在手里调这一张图，
    他填什么就画什么——中间再加一层改写，等于把他刚敲进去的东西又改掉。
    """
    params = load_params(task_id)
    source = str(getattr(params, "video_source", "") or "")
    if source in _UNSUPPORTED_REGEN_SOURCES:
        raise SceneError(
            f"video source {source!r} does not generate materials from keywords; "
            f"{advice}"
        )
    if not term:
        raise SceneError(
            "no keyword to generate from; provide one with the request"
        )

    clip_duration = int(getattr(params, "video_clip_duration", 5) or 5)
    prompt_template = _image_prompt_template(params, image_style)
    starting_from = {}
    if reference is not None:
        starting_from = {
            "reference_image": reference.b64,
            "reference_strength": (
                float(strength) if strength else reference_image.strength()
            ),
        }
        if follow:
            starting_from.pop("reference_strength", None)
            starting_from["reference_follow"] = follow
    logger.info(
        f"generating {what} of task {task_id} "
        f"from source {source!r} with keyword {term!r}"
        + (
            f", starting from a {reference.width}x{reference.height} photo "
            f"found with {reference.query!r}"
            if reference is not None
            else ""
        )
    )
    try:
        produced = material.download_videos(
            task_id=task_id,
            search_terms=[term],
            source=source,
            video_aspect=VideoAspect(
                getattr(params, "video_aspect", None) or VideoAspect.portrait
            ),
            # 顺序模式让它按关键词顺序逐个取，单关键词时就是"取一个"。
            video_concat_mode=VideoConcatMode.sequential,
            # 只要覆盖一段片长，避免为一个场景取回一整批素材。
            audio_duration=float(clip_duration),
            max_clip_duration=clip_duration,
            match_script_order=True,
            image_motion=getattr(params, "image_motion", None) or "zoom",
            # 画风跟着任务走（或者跟着用户这次挑的走），不跟着全局配置走。
            image_prompt_template=prompt_template,
            # 照着一张真实照片重画这一张。没有底图时这两项不出现在请求里。
            **starting_from,
        )
    except Exception as exc:
        # 供应商失败必须点名是谁失败了，不能静默换源，也不能留下半条清单。
        raise SceneError(f"{source} failed to produce a material: {exc}") from exc

    if not produced:
        # 素材源按约定返回空列表而不是抛异常，所以"为什么"要单独去取一次。
        # 不取的话用户看到的就是"没出图"，然后得去翻日志才知道是显卡掉了。
        why = material.last_failure(source)
        raise SceneError(
            f"{source} returned no material for {term!r}"
            + (f": {why}" if why else "")
        )
    return str(produced[0]), source


def add_scene(
    task_id: str,
    *,
    keyword: str,
    narration: str = "",
    after: Any = None,
    image_style: str | None = None,
) -> dict:
    """按关键词生成一段新画面，插进时间线。

    为什么需要它：素材张数由关键词个数决定，不由旁白长度决定。画面比旁白短时
    "把每段停得更久"(fit)只是答案之一,另一个答案是"多补几张画面"——而在这之前
    时间线只能改已有的那些,补不了新的。

    ``after`` 不给就追加到末尾,这也是界面上那个加号的行为。新场景的台词默认
    为空:这里只补画面,不动旁白,否则会悄悄让已经录好的配音失效。

    ``image_style`` 不给就跟着任务记下的风格,补出来的画面和片子里原有的是
    同一种风格——补画面的人想要的本来就是"再来一张一样的",而不是一张漫画
    片子里突然插进来的油画。给了就按它来,并记在这一段上,之后重生成这一段
    还是这个风格。
    """
    _require_idle(task_id)
    term = str(keyword or "").strip()
    style = str(image_style or "").strip()
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        # 插在哪一段后面，同样按身份认：出图这几分钟里时间线可能变了，光记
        # 一个下标会把新画面插到另一个位置去。身份和 ``_locate_scene`` 用的
        # 必须是同一个东西（``scene_manifest.scene_id``），拿素材文件名去对
        # 一份按 id 认人的清单，结果是永远对不上。
        after_position = None if after is None else _scene_at(scenes, after)
        anchor = (
            ""
            if after_position is None
            else scene_manifest.scene_id(scenes[after_position])
        )

    # 和重生成一样，慢的那一段不拿锁。
    new_path, source = _produce_material(
        task_id,
        term,
        what="a new scene",
        advice="add your own file as a scene instead",
        image_style=style,
    )

    scene = {
        "index": 0,  # save_scenes 会按位置重排
        "id": scene_manifest.new_scene_id(),
        "keyword": term,
        "narration": str(narration or ""),
        "material": new_path,
        "kind": scene_manifest.material_kind(new_path),
        "source": source,
        "locked": True,
    }
    if style:
        scene["image_style"] = style
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        position = (
            len(scenes)
            if after_position is None
            else _locate_scene(scenes, after_position, anchor) + 1
        )
        scenes.insert(position, scene)
        scene_manifest.save_scenes(task_id, scenes)
        logger.success(
            f"added scene at position {position} of task {task_id} "
            f"with keyword {term!r}"
        )
        return scene_manifest.load(task_id)["scenes"][position]


def replace_material(task_id: str, index: Any, material_path: str) -> dict:
    """用一个已经通过上传校验的文件替换场景素材。"""
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)

        # 上传目录和任务目录之外的路径一律拒绝，哪怕它确实存在。
        resolved = scene_manifest.resolve_material(task_id, material_path)
        resolved = _adopt_into_task(task_id, resolved)
        scene = scenes[position]
        # 上传一个文件盖掉一张画好的图，损失和重生成盖掉它一样大。见
        # ``regenerate_material`` 里同样这一段。
        if scene_manifest.material_key(scene.get("material")) != (
            scene_manifest.material_key(resolved)
        ):
            scene_manifest.remember_material(scene)
        scene["material"] = resolved
        scene["kind"] = scene_manifest.material_kind(resolved)
        scene["source"] = "local"
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        return scene


def place_map(task_id: str, index: Any, place: dict) -> dict:
    """把这一段的画面换成一张定位地图（``locator_map``）。

    地图就是一张图：画成 PNG 放进任务目录，再走和别的图一样的路烘成片段
    （慢慢推近）。所以它在编辑器里看得见、换得掉，原来那张图也照旧记在这一
    段的历史里，能换回去。同一张地图（同一个地方、同样的范围和画幅）只画
    一次；这一段已经是它了就什么都不做。
    """
    import hashlib
    import json

    from app.models.schema import ImageMotion
    from app.services import locator_map
    from app.services import video as video_service

    _require_idle(task_id)
    place = locator_map.checked(place)
    aspect = VideoAspect(getattr(load_params(task_id), "video_aspect", None)
                         or VideoAspect.portrait)
    size = tuple(int(side) for side in aspect.to_resolution())
    digest = hashlib.sha1(
        json.dumps([place, size], sort_keys=True).encode("utf-8")).hexdigest()[:16]
    picture = os.path.join(utils.task_dir(task_id), f"map-{digest}.png")
    if not os.path.isfile(picture):
        locator_map.save(place, picture, size)

    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        scene = scenes[_scene_at(scenes, index)]
        current = scene.get("material")
        seconds = scene.get("duration") or scene.get("clip_duration") or 5
    if current and os.path.basename(
            timeline.source_image_for(task_id, scene_manifest.resolve_material(
                task_id, current)) or "") == os.path.basename(picture):
        return scene
    clip = video_service.render_image_zoom_video(
        picture, round(max(1.0, float(seconds)), 2), image_motion=ImageMotion.zoom)

    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        scene = scenes[_scene_at(scenes, index)]
        if scene.get("material") and scene_manifest.material_key(
                scene.get("material")) != scene_manifest.material_key(clip):
            scene_manifest.remember_material(scene)
        scene["material"] = clip
        scene["kind"] = scene_manifest.material_kind(clip)
        scene["source"] = "local"
        # 锁上：批量重画、"开画"都不该把一张地图换成画出来的图。
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        logger.info(
            f"scene {int(index) + 1} of {task_id} now shows a map of {place['label']}")
        return scene


def place_planned_maps(task_id: str, planned: list) -> int:
    """分镜里写了地图的那几段，现在就画上，返回画了几张。

    在"开画"之前做：地图不用出图模型，一秒钟一张；画上之后这几段就不再是
    "还没有画面"的占位，开画时自然跳过它们。
    """
    placed = 0
    for position, scene in enumerate(planned or []):
        wanted = (scene.get("direction") or {}).get("map") if isinstance(scene, dict) else None
        if not wanted:
            continue
        try:
            place_map(task_id, position, wanted)
            placed += 1
        except Exception as exc:  # 一张地图画不了不该挡住整条片子
            logger.warning(
                f"the map on scene {position + 1} of {task_id} could not be drawn; "
                f"the scene is left for the picture model: {exc}")
    return placed


def _adopt_into_task(task_id: str, source: str) -> str:
    """把任务目录之外的替换素材复制进任务目录。

    两个理由。一是 ``/stream`` 只服务任务目录，留在公共上传目录里的文件无法
    通过接口播放,审片界面上就会显示成"素材缺失"。二是任务从此自给自足：上传
    目录被清理或那个文件被别的任务替换掉,都不会让这条已经审过的片子渲染失败。

    代价是一次文件复制。重名时加序号,不覆盖任何已有文件。
    """
    task_directory = os.path.realpath(utils.task_dir(task_id))
    if os.path.dirname(os.path.realpath(source)) == task_directory:
        return source

    stem, suffix = os.path.splitext(os.path.basename(source))
    for attempt in range(1000):
        name = f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt}{suffix}"
        target = os.path.join(task_directory, name)
        if not os.path.exists(target):
            try:
                shutil.copyfile(source, target)
            except OSError as exc:
                raise SceneError(f"failed to copy the replacement material: {exc}") from exc
            logger.info(f"adopted replacement material into task {task_id}: {name}")
            return target
    raise SceneError("too many replacement materials with the same name")


def restore_material(task_id: str, index: Any, material: str) -> dict:
    """把这一段换回它画过的某一张图。

    为什么这是一次指针替换，几秒钟就完事：那张图从来没丢。重生成只是把
    ``material`` 指向新文件，旧文件一直在任务目录里。所以"找回上一张"不用
    重画、不用等显卡、不花一分钱——改一个文件名而已。

    这一张同时也被记进 history，所以换回去之后还能再换回来：用户看过两张
    之后改主意是常事，单向的"还原"只是把问题挪到另一边。

    一起换回去的还有画风和"照着哪张照片画的"：清单里这两个字段说的是
    **现在这张图**怎么来的（见 ``_normalise_scene``），图换了而它们不换就是
    说谎。提示词不自动覆盖——那个框是用户正在写的东西，不是一条记录；它当时
    用的那句话在返回值的 ``restored_keyword`` 里，界面请他自己决定要不要用。
    """
    _require_idle(task_id)
    tile = picture_shelf.find(task_id, material)
    if tile is None:
        name = scene_manifest.material_key(material) or str(material)
        raise SceneError(
            f"{name} is not a picture of this video; only the pictures this "
            f"video has drawn can be put back"
        )
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]
        resolved = scene_manifest.resolve_material(task_id, tile["material"])
        was = scene_manifest.material_key(scene.get("material"))
        if was == scene_manifest.material_key(resolved):
            raise SceneError(
                f"scene {position} is already using {tile['material']}"
            )

        scene_manifest.remember_material(scene)
        scene["material"] = resolved
        scene["kind"] = scene_manifest.material_kind(resolved)
        recorded = _recorded_about(scenes, position, tile["material"])
        source = str(recorded.get("source") or "").strip()
        if source:
            scene["source"] = source
        style = str(recorded.get("image_style") or "").strip()
        if style:
            scene["image_style"] = style
        else:
            scene.pop("image_style", None)
        reference = recorded.get("reference")
        if isinstance(reference, dict) and reference:
            scene["reference"] = dict(reference)
        else:
            scene.pop("reference", None)
        # 用户亲手挑的这一张，不该被后续批量操作盖掉。同 ``regenerate_material``。
        scene["locked"] = True
        scene_manifest.save_scenes(task_id, scenes)
        logger.info(
            f"scene {position} of task {task_id} put back {tile['material']} "
            f"(was {was or 'nothing'})"
        )
        restored = dict(scene)
        restored["restored_keyword"] = str(recorded.get("keyword") or "")
        return restored


def _recorded_about(scenes: list, position: int, material: str) -> dict:
    """关于这个文件我们记下过什么：当时的提示词、画风、照着哪张照片。

    先看这一段自己的 history（最可能就是它换下来的那一张），再看别的段落的
    history 和它们正在用的素材——同一张图被另一段用着，那一段的字段说的也
    是这个文件。老任务里被换下来的图没有任何记录，返回空，调用方照旧只换图。
    """
    wanted = scene_manifest.material_key(material)
    here = scenes[position] if 0 <= position < len(scenes) else {}
    ordered = [here] + [one for at, one in enumerate(scenes) if at != position]
    for scene in ordered:
        for entry in scene.get("history") or []:
            if isinstance(entry, dict) and (
                scene_manifest.material_key(entry.get("material")) == wanted
            ):
                return entry
    for scene in ordered:
        if scene_manifest.material_key(scene.get("material")) == wanted:
            return scene
    return {}


def update_scene(
    task_id: str,
    index: Any,
    *,
    narration: str | None = None,
    keyword: str | None = None,
    locked: bool | None = None,
) -> dict:
    """改台词、关键词或锁定状态。

    改台词会让配音和字幕同时失效：配音是按整段脚本合成的,改一句话之后旧
    配音的时间轴对不上任何字幕。这里只打标记,不自动重新配音——重新配音会
    重跑一次 TTS 并作废全部字幕时间,那应该是用户按下按钮的结果,不是改一个
    文本框的副作用。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]

        narration_changed = False
        if narration is not None:
            text = str(narration).strip()
            # 和界面上现在显示的那段比,而不是和存着的比。界面显示的是按时间
            # 算出来的台词,存着的往往是空的——照存着的比,用户只要按一下"保存
            # 文字"而什么都没改,也会被当成改了台词,配音白白作废一次。
            shown = narration_for_scenes(task_id)
            current = (
                shown[position]
                if position < len(shown)
                else str(scene.get("narration") or "")
            )
            narration_changed = text != current
            if narration_changed:
                scene["narration"] = text
                # 标成手改过,重算时不要盖掉他写的字。清空也算改过——那是"重新
                # 配音时别念这几句"的意思,不是"交回去重新算一遍",否则刚删掉
                # 的句子下一次读清单又回来了。
                scene["narration_edited"] = True
        if keyword is not None:
            scene["keyword"] = str(keyword).strip()
        if locked is not None:
            scene["locked"] = bool(locked)

        extra: dict[str, Any] = {}
        if narration_changed:
            extra["audio_stale"] = True
        scene_manifest.save_scenes(task_id, scenes, **extra)
        return scene


def delete_scene(task_id: str, index: Any) -> list[dict]:
    """删除一个场景。素材文件留在磁盘上，不删。

    不删文件是有意的：用户可能只是想先去掉一段看看效果,而磁盘上的那张图
    是上一次已经花掉的算力。清理任务目录本来就由删除任务负责。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        if len(scenes) == 1:
            raise SceneError("a video needs at least one scene")
        removed = scenes.pop(position)
        logger.info(
            f"removed scene {position} from task {task_id}: {removed.get('material')}"
        )
        scene_manifest.save_scenes(task_id, scenes)
        return scene_manifest.load(task_id)["scenes"]


def _on_screen_seconds(task_id: str, scene: dict) -> float:
    """这一段在成片里停多久。没设过时长就是"素材剩下的全长"。"""
    timing = scene_manifest.scene_timing(scene)
    if timing["duration"] is not None:
        return float(timing["duration"])
    material = scene_manifest.resolve_material(task_id, scene.get("material"))
    speed = float(getattr(load_params(task_id), "video_clip_speed", 1.0) or 1.0)
    remaining = timeline.probe_duration(material) - timing["trim_start"]
    return max(0.0, remaining / max(speed, 1e-6))


# 分出来的两段各自至少这么长。再短在时间线上就点不中了，渲染出来也只是一闪。
MIN_SPLIT_SECONDS = 0.2


def split_scene(task_id: str, index: Any, at: Any) -> list[dict]:
    """在这一段的 ``at`` 秒处切一刀，变成前后两段。

    两段用的是同一个素材文件：前一段到切点为止，后一段从切点接着放。所以
    切完马上渲染，成片和没切之前一模一样——切只是多了一个可以单独拖、单独
    换图、单独加转场的接缝，这正是 CapCut 里"分割"的意思。

    ``at`` 是这一段**在成片里**的时间（从这一段开头算起），不是素材上的
    时间：变速之后两者不相等，而用户看着的是时间线。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]
        if not str(scene.get("material") or "").strip():
            raise SceneError(
                f"scene {position} has no picture yet, so there is nothing to split"
            )
        try:
            cut = float(at)
        except (TypeError, ValueError) as exc:
            raise SceneError("the split point must be a number of seconds") from exc
        total = _on_screen_seconds(task_id, scene)
        if not MIN_SPLIT_SECONDS <= cut <= total - MIN_SPLIT_SECONDS:
            raise SceneError(
                f"scene {position} is {total:.2f}s long; split it between "
                f"{MIN_SPLIT_SECONDS:.1f}s and {max(total - MIN_SPLIT_SECONDS, 0):.2f}s"
            )
        speed = float(getattr(load_params(task_id), "video_clip_speed", 1.0) or 1.0)
        start = scene_manifest.scene_timing(scene)["trim_start"]

        second = dict(scene)
        second["id"] = scene_manifest.new_scene_id()
        second["trim_start"] = round(start + cut * speed, 3)
        second["duration"] = round(total - cut, 3)
        # 台词按时间对到画面上，不跟着某一段走。后一段不带前一段手改过的那句。
        second["narration"] = ""
        second.pop("narration_edited", None)
        second.pop("history", None)
        scene["trim_start"] = round(start, 3)
        scene["duration"] = round(cut, 3)
        scenes.insert(position + 1, second)
        with edit_history.labelled("Split scene"):
            scene_manifest.save_scenes(task_id, scenes)
        return scene_manifest.load(task_id)["scenes"]


def duplicate_scene(task_id: str, index: Any) -> list[dict]:
    """把这一段复制一份，放在它后面。同一张图、同样的入点和时长。"""
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        copy = dict(scenes[position])
        copy["id"] = scene_manifest.new_scene_id()
        copy["narration"] = ""
        copy.pop("narration_edited", None)
        copy.pop("history", None)
        scenes.insert(position + 1, copy)
        with edit_history.labelled("Duplicate scene"):
            scene_manifest.save_scenes(task_id, scenes)
        return scene_manifest.load(task_id)["scenes"]


def history_state(task_id: str) -> dict:
    """能撤销几步、能重做几步。"""
    return edit_history.state(task_id)


def _step_history(task_id: str, direction: str) -> dict:
    _require_idle(task_id)
    with _editing(task_id):
        payload = scene_manifest.load_raw(task_id)
        current = scene_manifest.load(task_id)["scenes"]
        wanted = edit_history.step(task_id, direction, payload, current)
        if wanted is None:
            raise SceneError(f"there is nothing to {direction}")

        # 每一段"换过哪些图"的那份记录不跟着倒回去：它是"画过的每一张图"的
        # 索引，撤销一次删除不该让它少一条。按身份把现在的接回去。
        kept = {scene.get("id"): scene.get("history") for scene in current}
        restored = []
        for scene in wanted.get("scenes") or []:
            scene = dict(scene)
            if kept.get(scene.get("id")):
                scene["history"] = kept[scene["id"]]
            restored.append(scene)
        if not restored:
            raise SceneError(f"that step has no scenes in it, so it cannot be used")

        script = str(wanted.get("script") or "")
        same_recording = wanted.get("audio") == edit_history.snapshot(
            payload, current
        )["audio"]
        words_changed = script != str(payload.get("script") or "") or [
            scene.get("narration") for scene in restored
        ] != [scene.get("narration") for scene in current]
        extra: dict[str, Any] = {}
        if script and script != str(payload.get("script") or ""):
            extra["script"] = script
        if same_recording:
            # 配音还是那一份：它过没过期，就是那一步当时的样子。
            extra["audio_stale"] = bool(wanted.get("audio_stale"))
        elif words_changed:
            # 这中间重新配过音。倒回去的台词和现在这份配音对不上。
            extra["audio_stale"] = True
        with edit_history.paused():
            scene_manifest.save_scenes(task_id, restored, **extra)
        return {
            "scenes": scene_manifest.load(task_id)["scenes"],
            "label": str(wanted.get("label") or ""),
            "history": edit_history.state(task_id),
        }


def undo(task_id: str) -> dict:
    """撤销时间线上的上一步。"""
    return _step_history(task_id, "undo")


def redo(task_id: str) -> dict:
    """把刚撤销的那一步做回来。"""
    return _step_history(task_id, "redo")


def reorder_scenes(task_id: str, order: Sequence[Any]) -> list[dict]:
    """按给定的原始下标顺序重排场景。

    要求是一个完整排列,不接受"只给前几个"：部分顺序会让剩下的场景去哪里
    变成一个需要猜的问题,而猜错的结果是成片里少了一段。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        try:
            positions = [int(value) for value in order]
        except (TypeError, ValueError) as exc:
            raise SceneError("scene order must be a list of whole numbers") from exc

        if sorted(positions) != list(range(len(scenes))):
            raise SceneError(
                f"scene order must be a permutation of 0..{len(scenes) - 1}, "
                f"got {positions}"
            )
        # 画面换位置，台词留在原处。
        #
        # 台词属于时间,不属于图：配音是一整条,顺序没变；字幕按那条配音的时间
        # 轴生成,也没变。把图拖到前面只是让它早点出现,第 30 秒念的还是第 30
        # 秒那句话。文字跟着图走的话,界面会把另一段的台词显示在这张图下面,而
        # "重新配音"读的就是这份列表——按一下就把整条旁白按画面顺序重念了。
        #
        # Reordering pictures must not reorder the words: the voice-over is one
        # take and the subtitles are timed against it, so what is heard at a
        # given second does not change when a picture moves to another slot.
        words = [
            (
                str(scene.get("narration") or ""),
                bool(scene.get("narration_edited")),
            )
            for scene in scenes
        ]
        reordered = [scenes[position] for position in positions]
        for scene, (text, edited) in zip(reordered, words):
            scene["narration"] = text
            if edited:
                scene["narration_edited"] = True
            else:
                scene.pop("narration_edited", None)
        scene_manifest.save_scenes(task_id, reordered)
        return scene_manifest.load(task_id)["scenes"]


def set_locked(task_id: str, index: Any, locked: bool) -> dict:
    return update_scene(task_id, index, locked=locked)


def media_url(task_id: str, path: Any, endpoint: str = "") -> str:
    """返回场景素材的可播放地址，无法解析时返回空字符串。"""
    relative = scene_manifest.relative_media_path(task_id, path)
    if not relative:
        return ""
    base = (endpoint or "").rstrip("/")
    return f"{base}/api/v1/stream/{relative}" if base else f"/api/v1/stream/{relative}"


def _enum_value(value: Any) -> str:
    """把 ``(str, Enum)`` 成员和裸字符串统一成界面能直接显示的字符串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def planned_windows(
    task_id: str, manifest: dict, params=None, *, probe: bool = True
) -> list[dict]:
    """场景在成片里各自占的时间段，渲染器用的就是这组数字。

    ``probe`` 关掉时按请求的单段时长估算，不启动解码器——审片列表每次页面
    重跑都要算一遍，不该为此多付十几次解码。
    """
    scenes = manifest.get("scenes") or []
    if not scenes:
        return []
    if params is None:
        params = load_params(task_id)
    return timeline.plan_windows(
        task_id,
        scenes,
        audio_duration=manifest.get("audio_duration") or 0.0,
        clip_speed=getattr(params, "video_clip_speed", 1.0) or 1.0,
        clip_duration=getattr(params, "video_clip_duration", None),
        probe=probe,
    )


def _narration_by_time(
    task_id: str,
    manifest: dict,
    windows: list[dict] | None = None,
) -> list[str]:
    """按时间把旁白分到每一段画面上：这段画面在屏幕上时真正会念到的那几句。

    画面铺不满旁白时整组会轮播,所以要按轮播后的真实时间线分配,再把同一个
    场景的几次出现合回一条。只按第一轮分配的话,第一轮之后的每一句都会堆到
    最后一个场景上。

    改过脚本还没重新配音时不看字幕：那份字幕还是旧词的时间轴,照它分配会把
    用户刚写下的句子显示成改之前的样子。
    """
    scenes = manifest["scenes"]
    if not scenes:
        return []
    if windows is None:
        windows = planned_windows(task_id, manifest, probe=False)
    if len(windows) != len(scenes):
        return ["" for _ in scenes]
    lines = scene_manifest.split_narration(manifest.get("script"))
    segments = (
        []
        if manifest.get("audio_stale")
        else scene_manifest.narration_segments(task_id)
    )
    if not lines and not segments:
        return ["" for _ in scenes]
    cycled = timeline.cycle_windows(
        windows, float(manifest.get("audio_duration") or 0.0)
    )
    blocks = scene_manifest.distribute_narration(
        [{"start": item["start"], "end": item["end"]} for item in cycled],
        segments=segments,
        lines=lines,
    )
    texts = ["" for _ in scenes]
    for item, block in zip(cycled, blocks):
        if not block:
            continue
        index = item["scene"]
        texts[index] = "\n".join(filter(None, (texts[index], block))).strip()
    return texts


def narration_for_scenes(
    task_id: str,
    manifest: dict | None = None,
    windows: list[dict] | None = None,
) -> list[str]:
    """每一段画面该显示的台词。

    台词不属于某一张图,属于某一段时间：配音是一整条,字幕是按那条配音的时
    间轴生成的,所以"这段画面上会念什么"只能由它在成片里的起止时间决定。把
    文字存在场景上只是个缓存——画面一重排、一删、一改时长,缓存就过期了,而
    过期的缓存会让界面显示另一段的台词。

    Narration belongs to a stretch of time, not to a picture: the voice-over
    is one take and the subtitles are timed against it, so what is said over
    a picture follows from where that picture sits in the finished video.
    Recomputing it on every read is what keeps the editor honest after a
    reorder, a delete or a timing change.

    用户亲手改过的那一段例外,用他写的字：那不是缓存,那是他的输入。
    """
    manifest = manifest if manifest is not None else scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    stored = [str(scene.get("narration") or "") for scene in scenes]
    try:
        derived = _narration_by_time(task_id, manifest, windows)
    except scene_manifest.SceneError as exc:
        # 排不出时间线（参数读不回来之类）时宁可显示存着的旧文字，也不要让
        # 审片界面打不开。
        logger.warning(f"cannot pair narration by time for {task_id}: {exc}")
        return stored
    if len(derived) != len(stored):
        return stored
    return [
        stored[position]
        if scenes[position].get("narration_edited") or not derived[position]
        else derived[position]
        for position in range(len(scenes))
    ]


def narration_script(task_id: str) -> str:
    """重新配音要念的脚本,顺序与画面顺序无关。

    不把场景台词按场景顺序拼起来：场景顺序是画面顺序。用户把第 7 张图拖到
    最前面,是想让那张图先出现,不是想让第 7 句话先被念出来——而按场景顺序拼
    接恰恰会那样,再按一下"重新配音"整条旁白就被打乱了,而且没有任何提示。

    按时间分配出来的块本身就是脚本顺序(场景顺序即时间顺序),重排画面动不了
    它。一句都不丢,所以拼回去仍然是完整的脚本。
    """
    manifest = scene_manifest.load(task_id)
    blocks = narration_for_scenes(task_id, manifest)
    script = "\n".join(block.strip() for block in blocks if block.strip())
    # 一句都没排出来（既没有脚本也没有字幕）时退回旧的拼接，总比交一段空
    # 脚本给 TTS 好。
    return script or scene_manifest.narration_script(task_id)


def rewrite_script(task_id: str, script: str) -> str:
    """整条旁白的文字,一次改完。

    台词按时间归到画面上,所以"改第 3 段的词"其实是在改脚本的某一块；想调整
    句子的先后、把一句拆成两句、或者整段重写,都没有哪一段画面是它的归属。
    这里是改词的地方,场景上的那个输入框只是它的局部视图。

    改完配音就不作数了,但不在这里自动重配：重配要跑一次 TTS 并作废全部字幕
    时间,那应该是用户按下按钮的结果,不是存一次文字的副作用。
    """
    _require_idle(task_id)
    text = str(script or "").strip()
    if not text:
        raise SceneError("the narration script cannot be empty")
    with _editing(task_id):
        manifest = scene_manifest.load(task_id)
        if not manifest["scenes"]:
            raise SceneError(f"task {task_id} has no scenes to narrate")
        if text == str(manifest.get("script") or "").strip():
            return text
        if not scene_manifest.patch_script(task_id, text):
            raise SceneError("failed to save the narration script")
        # 脚本就是台词本身了，场景上那些"手改过"的标记跟着作废：留着它们会
        # 让刚写下的句子被旧的局部编辑挡住。
        scenes = manifest["scenes"]
        for scene in scenes:
            scene.pop("narration_edited", None)
            scene["narration"] = ""
        scene_manifest.save_scenes(task_id, scenes, audio_stale=True)
    return text


def realign_narration(task_id: str) -> bool:
    """把场景列表里漏掉的旁白句子按时间补回去，返回是否改动过。

    关键词个数决定画面张数，旁白句数由脚本决定，两者几乎从不相等。按下标
    配对时多出来的句子在界面上直接消失(12 张图配 28 句旁白会丢 16 句),
    而"改台词后重新配音"读的就是这份列表——脚本会被悄悄砍掉一多半。

    这里是幂等的：补过一次之后每条场景台词都是若干句的合并，不再等于脚本
    里的任何单句，``narration_pairing_is_stale`` 随即返回 False。用户手改过
    台词的任务同样返回 False，不会被盖掉。
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    lines = scene_manifest.split_narration(manifest.get("script"))
    if not scene_manifest.narration_pairing_is_stale(scenes, lines):
        return False
    try:
        windows = planned_windows(task_id, manifest, probe=False)
    except scene_manifest.SceneError as exc:
        # 参数读不回来时宁可继续显示旧配对，也不要让审片界面打不开。
        logger.warning(f"cannot re-pair narration for {task_id}: {exc}")
        return False

    # 画面铺不满旁白时整组会轮播，所以要按轮播后的真实时间线分配，再把
    # 同一个场景的几次出现合回一条。只按第一轮分配的话，第一轮之后的每一
    # 句都会堆到最后一个场景上。
    cycled = timeline.cycle_windows(
        windows, float(manifest.get("audio_duration") or 0.0)
    )
    blocks = scene_manifest.distribute_narration(
        [{"start": item["start"], "end": item["end"]} for item in cycled],
        segments=scene_manifest.narration_segments(task_id),
        lines=lines,
    )
    texts = ["" for _ in windows]
    for item, block in zip(cycled, blocks):
        if not block:
            continue
        index = item["scene"]
        texts[index] = "\n".join(filter(None, (texts[index], block))).strip()
    # 这是程序在补，不是用户在改：不该算成一步编辑，更不该清掉"重做"。
    with edit_history.automatic():
        scene_manifest.apply_narration_pairing(task_id, texts)
    logger.info(
        f"re-paired {len(lines)} narration lines across {len(scenes)} scenes "
        f"by on-screen time, task_id: {task_id}"
    )
    return True


def describe(task_id: str, endpoint: str = "", *, with_timing: bool = False) -> dict:
    """审片界面的读模型：场景、可播放地址、以及配音是否已失效。

    ``with_timing`` 打开时额外探测每段素材的真实时长。时间线界面必须知道
    源长度才能画出拖拽边界,但探测要逐个打开解码器,所以默认不做——Phase 1
    的审片列表并不需要它。探测结果按"路径+修改时间+大小"缓存。
    """
    # 先把漏掉的旁白句子按时间补回场景，再读：界面显示的台词必须是这段画面
    # 播放时真正会念的那几句，否则用户会以为渲染器换掉了图片。
    try:
        realign_narration(task_id)
    except Exception as exc:  # pragma: no cover - 补配对失败不该挡住审片
        logger.warning(f"cannot re-pair narration for {task_id}: {exc}")
    # 再让每张画面跟上它那句话（只对刚做出来、没人动过的片子，只做一次）。
    try:
        _pace_new_video(task_id)
    except Exception as exc:  # pragma: no cover - 对不上不该挡住审片
        logger.warning(f"cannot time {task_id} to its narration: {exc}")
    manifest = scene_manifest.load(task_id)
    params = None
    try:
        params = load_params(task_id)
    except scene_manifest.SceneError as exc:
        logger.warning(f"cannot read render settings for {task_id}: {exc}")
    windows = []
    if params is not None:
        try:
            windows = planned_windows(
                task_id, manifest, params, probe=with_timing
            )
        except scene_manifest.SceneError as exc:
            logger.warning(f"cannot plan scene timing for {task_id}: {exc}")
    # 台词按时间算,不直接用存着的那一份：存着的只是缓存,画面一重排、一删、
    # 一改时长就过期了。理由见 ``narration_for_scenes``。
    spoken = narration_for_scenes(task_id, manifest, windows or None)
    scenes = []
    picked = photo_pick_kinds(task_id)
    for scene in manifest["scenes"]:
        record = dict(scene)
        if scene["index"] < len(spoken):
            record["narration"] = spoken[scene["index"]]
        record["narration_edited"] = bool(scene.get("narration_edited"))
        # 开画之前给这一段定过照片没有（"photo"、"auto"、"skip"，或者空）。
        # 定过的不再问第二遍。
        record["photo_pick"] = picked.get(scene_manifest.scene_id(scene), "")
        # 这一段自己挑的转场和运镜。空字符串一律是"跟整条片子一样"，界面
        # 据此把下拉框停在第一项上。
        record["transition"] = transition_id(scene.get("transition"))
        record["image_motion"] = str(scene.get("image_motion") or "")
        record["cut_sound"] = str(scene.get("cut_sound") or "")
        # 关键词是空的那几段：告诉界面"不填的话会拿什么去画"，让它当占位文字
        # 显示出来。空着的关键词原本在重生成那里直接被拒，用户只能逐段手敲。
        if not str(scene.get("keyword") or "").strip():
            line = " ".join(
                str(spoken[scene["index"]] if scene["index"] < len(spoken) else "").split()
            )
            subject = str(getattr(params, "video_subject", "") or "").strip()
            record["keyword_suggestion"] = ". ".join(
                part for part in (subject, line) if part
            )[:400]
        else:
            record["keyword_suggestion"] = ""
        window = windows[scene["index"]] if scene["index"] < len(windows) else None
        # 成片里的起止时间。界面靠它把"第 7 段画面"和"第 30 秒的旁白"对上。
        record["starts_at"] = window["start"] if window else None
        record["ends_at"] = window["end"] if window else None
        record["url"] = media_url(task_id, scene["material"], endpoint)
        record["exists"] = bool(record["url"])
        timing = scene_manifest.scene_timing(scene)
        record["trim_start"] = timing["trim_start"]
        record["duration"] = timing["duration"]
        record["source_duration"] = None
        # 这一段能不能拉得比素材本身还长。图片烘出来的片段可以（按新时长重
        # 渲一段），网上下回来的视频不行（没法凭空造帧）。界面拿它决定拖拽
        # 把手要不要夹——不告诉它的话，它只能按后缀猜，而推拉镜头烘出来的
        # 片段后缀就是 .mp4。
        record["can_hold_longer"] = timeline.baked_from_an_image(
            scene.get("material") or ""
        )
        if with_timing and record["exists"]:
            try:
                record["source_duration"] = timeline.probe_duration(
                    scene_manifest.resolve_material(task_id, scene["material"])
                )
            except scene_manifest.SceneError as exc:
                # 探测不出来不该让整个时间线打不开，这一段按"长度未知"显示。
                logger.warning(
                    f"cannot probe scene duration, task_id: {task_id}, "
                    f"scene: {scene.get('index')}, error: {exc}"
                )
        record["material"] = os.path.basename(scene["material"])
        scenes.append(record)
    payload = {
        "task_id": task_id,
        "scenes": scenes,
        "audio_stale": manifest["audio_stale"],
        "audio_url": media_url(task_id, manifest.get("audio_file"), endpoint),
        "subtitle_path": os.path.basename(str(manifest.get("subtitle_path") or "")),
        "script": manifest["script"],
    }
    # 画风：可选项，以及这条片子出片时用的那个。界面要默认选中后者——
    # 编辑器以前根本不知道风格这件事，重生成只能用 API 进程"现在"的配置。
    #
    # 只有按关键词作图的来源才有画风可挑，别的来源这里是空列表，界面据此
    # 不画那个下拉框。
    supports_style = (
        str(getattr(params, "video_source", "") or "")
        in image_style_model.IMAGE_STYLE_SOURCES
    )
    payload["image_styles"] = (
        list(image_style_model.image_style_ids()) if supports_style else []
    )
    # 这条片子叫什么。顶栏要显示它：开着三个标签页的时候，一串 uuid 说不出
    # 哪个是哪个。
    payload["subject"] = str(getattr(params, "video_subject", "") or "").strip()
    # 开片时挑的"多少画面照着照片画"。界面点"开画"时要先请人挑那几张照片。
    payload["photo_scene_percent"] = int(
        getattr(params, "photo_scene_percent", 0) or 0
    )
    # 这条片子出片前的任务没有记过画风（这个字段比它们新），那时唯一能说的
    # 就是"按设置来"。界面要分得清这两种情况，否则对一个老任务说"和这条片子
    # 一样"等于撒谎——它用的其实是设置里现在那个风格。
    pinned = str(getattr(params, "image_prompt_template", "") or "").strip()
    payload["image_style_pinned"] = bool(pinned)
    task_style = image_style_model.style_for_template(
        tm.resolve_image_prompt_template(params) if params is not None else ""
    )
    # 自定义模板配不上任何预设名,这时不谎报一个预设,界面只说"和这条片子
    # 一样"就够了。
    payload["image_style"] = (
        "" if task_style == image_style_model.IMAGE_STYLE_CUSTOM else task_style
    )
    # 可挑的转场与运镜。界面不写死这两张表：渲染器认的名字只有服务端知道，
    # 写死一份迟早和它对不上。
    payload["transitions"] = list(SCENE_TRANSITIONS)
    payload["image_motions"] = list(SCENE_MOTIONS)
    # 这台机器上装着的转场音效。界面不写死：音效库是本机目录加用户自己丢进
    # 去的文件，只有服务端知道里面有什么。
    try:
        # 只给文件名。音效库返回的是绝对路径，原样交给界面的话，下拉框里
        # 每一项都是一整条磁盘路径；而保存时 ``set_effects`` 要的也是名字,
        # ``resolve_transition_sound`` 认的就是名字。
        payload["cut_sounds"] = [
            os.path.basename(path)
            for path in transition_sound.list_transition_sounds()
        ]
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the transition sounds: {exc}")
        payload["cut_sounds"] = []
    payload["planned_total"] = round(windows[-1]["end"], 3) if windows else 0.0
    # 旁白总长无条件带上：审片列表要靠它说清楚"画面铺不满旁白,所以会轮播"。
    # 这只是读一次清单，不探测素材，所以不必等 with_timing。
    payload["narration_total"] = round(
        float(manifest.get("audio_duration") or 0.0), 3
    )
    if with_timing:
        if params is None:
            params = load_params(task_id)
        # 成片的像素尺寸，用来把字号换算到预览框里。比例认不出来时按竖版
        # 算：那是这个项目的默认，猜错也只是预览里的字略大略小。
        try:
            frame_width, frame_height = VideoAspect(
                getattr(params, "video_aspect", None) or VideoAspect.portrait
            ).to_resolution()
        except (ValueError, TypeError):
            frame_width, frame_height = VideoAspect.portrait.to_resolution()
        payload["clip_speed"] = float(getattr(params, "video_clip_speed", 1.0) or 1.0)
        payload["audio_duration"] = float(manifest.get("audio_duration") or 0.0)
        payload["has_timing_edits"] = scene_manifest.has_timing_edits(
            manifest["scenes"]
        )
        # 旁白是逐句合成的，字幕里的每一条正好对应一句。时间线按句画出来，
        # 用户才看得出哪句话落在哪一段画面上。
        payload["narration_segments"] = scene_manifest.narration_segments(task_id)
        # 渲染设置要一并交给界面。否则用户看不到画面在动、看不到转场时，
        # 无法区分"编辑器没显示"和"这次生成本来就没配"——而后者才是常见原因：
        # image_motion=static 的素材在生成时就烘成了静止片段,
        # transition=None 则是渲染阶段压根不加转场。
        payload["render_settings"] = {
            "aspect": _enum_value(getattr(params, "video_aspect", "")),
            "image_motion": _enum_value(getattr(params, "image_motion", "")),
            "transition": _enum_value(getattr(params, "video_transition_mode", "")),
            # 整条片子的那一声，以及音量。界面要靠它写出"跟片子一样（whoosh）"，
            # 而音量是整条片子一个值——每一段挑的是"响哪一声"，不是"响多大"。
            "transition_sound": str(getattr(params, "transition_sound", "") or ""),
            "transition_sound_volume": float(
                getattr(params, "transition_sound_volume", 0.0) or 0.0
            ),
            "concat_mode": _enum_value(getattr(params, "video_concat_mode", "")),
            "clip_duration": getattr(params, "video_clip_duration", None),
            # 预览要照搬渲染器的适配方式，否则这里看到的构图和成片不一致：
            # cover 会裁掉多余边缘，contain 会留黑边。
            "fit": _enum_value(getattr(params, "video_fit_mode", "")),
            # 成片的像素尺寸。编辑器要按它把字号换算到预览框的大小上——
            # 60 号字在 1920 高的画面里和在 400 高的预览框里完全是两回事。
            "frame_width": frame_width,
            "frame_height": frame_height,
            # 字幕的样子。预览里要照着画，否则"字会不会挡住画面、够不够大"
            # 这件事只能等渲完才知道。
            "subtitle": {
                "enabled": bool(getattr(params, "subtitle_enabled", True)),
                "position": str(getattr(params, "subtitle_position", "") or "bottom"),
                "custom_position": float(
                    getattr(params, "custom_position", 70.0) or 0.0
                ),
                "font_size": int(getattr(params, "font_size", 60) or 60),
                "color": str(getattr(params, "text_fore_color", "") or "#FFFFFF"),
                "stroke_color": str(getattr(params, "stroke_color", "") or "#000000"),
                "stroke_width": float(getattr(params, "stroke_width", 0) or 0.0),
                # False 表示不要底色；字符串是颜色。两者都原样交给界面。
                "background": getattr(params, "text_background_color", False),
                "animation": str(getattr(params, "subtitle_animation", "") or "none"),
            },
        }
    return payload


def prepare_audio_rerun(task_id: str) -> VideoParams:
    """准备"用编辑后的台词重新配音"所需的参数，由调用方提交任务执行。

    真正的重配音仍然走流水线自己的 audio 与 subtitle 阶段
    (``start_from="audio"``, ``stop_at="subtitle"``),而不是在这里直接调
    TTS：那两个阶段里有音量处理、自定义音频、字幕提供方差异等一串既有行为,
    在这里复制一遍必然漏掉其中几条。

    台词先落盘,因为 ``start_from="audio"`` 时脚本是从清单读回来的,不是从
    params 里读的。
    """
    _require_idle(task_id)
    params = load_params(task_id)
    # 按时间排好的台词，而不是按场景顺序拼起来的：重排画面不该改变念词的
    # 顺序。见 ``narration_script``。
    script = narration_script(task_id)
    if not script:
        raise SceneError("every scene's narration is empty; nothing to narrate")

    if not scene_manifest.patch_script(task_id, script):
        raise SceneError("failed to save the edited narration")
    # 让 params 自身保持一致，避免任何入口再去问一次 LLM 要脚本。
    params.video_script = script
    return params


def unfinished_materials(task_id: str) -> dict:
    """这条任务的画面是不是出了一半就断了，断在哪儿。

    素材阶段半路失败是文生图任务最常见的死法：一张一张买，买到第二十九张
    连接被掐断。那二十九张的钱已经付了、文件就在盘上，可任务清单里没有场景
    ——界面上它是 "0 scenes"，点开一片空白。

    这里回答的是"还能不能接着跑"：盘上有已经渲好的画面、有配音、还知道该
    画什么（关键词），就能接着跑。``pictures`` 是已经出完的张数,``needed``
    是铺满旁白一共要几张,给界面说清楚"接着跑"要补多少。

    Materials dying halfway is how a text-to-image task usually fails: one
    picture bought at a time, cut off at the 29th. Those 29 are paid for and
    on disk, but with no scenes in the manifest the task reads "0 scenes" and
    opens empty. This answers whether it can be picked up: rendered pictures
    on disk, a narration, and the keywords that say what to draw.
    """
    empty = {"resumable": False, "pictures": 0, "needed": 0, "narration": 0.0}
    manifest = scene_manifest.load(task_id)
    if manifest["scenes"]:
        # 场景已经写进清单了,这条任务是可以审片、可以渲染的正常任务。
        return empty

    raw = scene_manifest.load_raw(task_id)
    sources = raw.get("material_sources")
    pictures = len(sources) if isinstance(sources, list) else 0
    if not pictures:
        return empty

    terms = raw.get("search_terms")
    if not isinstance(terms, list) or not [t for t in terms if str(t).strip()]:
        return empty

    # 配音是接着跑的时间基准。清单里没写也行,盘上那一份就是（素材阶段失败
    # 的任务正是"配音出完了但清单还没写"）。
    narration = 0.0
    audio = manifest.get("audio_file")
    if not audio:
        fallback = os.path.join(utils.task_dir(task_id), "audio.mp3")
        audio = fallback if os.path.isfile(fallback) else ""
    if not audio:
        return empty
    try:
        narration = float(manifest.get("audio_duration") or 0.0)
    except (TypeError, ValueError):
        narration = 0.0
    if narration <= 0:
        try:
            narration = float(voice.get_audio_duration(audio) or 0.0)
        except Exception as exc:
            logger.warning(f"cannot measure the narration of {task_id}: {exc}")
            narration = 0.0
    if narration <= 0:
        return empty

    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    try:
        hold = max(1.0, float(params.get("video_clip_duration") or 1))
    except (TypeError, ValueError):
        hold = 1.0
    needed = max(1, math.ceil(narration / hold))
    if pictures >= needed:
        # 画面其实已经够了,只是场景清单没写成。接着跑一次就把清单补上,
        # 而且一张图都不用再买。
        needed = pictures
    return {
        "resumable": True,
        "pictures": pictures,
        "needed": needed,
        "narration": round(narration, 1),
    }


def prepare_materials_rerun(task_id: str) -> VideoParams:
    """准备"把这条任务没出完的画面出完"所需的参数。

    走流水线自己的 materials 阶段（``start_from="materials"``,
    ``stop_at="materials"``),不在这里自己拼素材：那个阶段里有提供方校验、
    按需生成、清单落盘一整串既有行为。已经买下并渲好的画面由素材阶段自己
    认出来接着用（见 ``material._clips_already_paid_for``),所以这里不需要
    也不应该告诉它跳过几张。

    停在 materials 而不是一路渲到成片：出完画面先回到审片界面,让用户看过
    再决定渲不渲。这和一条正常任务在 ``review_before_render`` 下的落点一样。
    """
    _require_idle(task_id)
    state = unfinished_materials(task_id)
    if not state["resumable"]:
        raise SceneError(
            "there is nothing to pick up for this task: it needs pictures "
            "already generated on disk, a narration, and the keywords that "
            "say what to draw"
        )
    return load_params(task_id)


# 一段画面可以自己挑的转场。键是接口和界面用的 id，值是渲染器认的那个字符
# 串（``VideoTransitionMode`` 的 value）。"none" 是显式的"这一刀不要转场"，
# 和"没挑过"不是一回事：前者压过整条片子的设置，后者跟着它走。
SCENE_TRANSITIONS: dict[str, str] = {
    "none": "none",
    "fade_in": VideoTransitionMode.fade_in.value,
    "fade_out": VideoTransitionMode.fade_out.value,
    "slide_in": VideoTransitionMode.slide_in.value,
    "slide_out": VideoTransitionMode.slide_out.value,
    "zoom_in": VideoTransitionMode.zoom_in.value,
    "zoom_out": VideoTransitionMode.zoom_out.value,
    "shuffle": VideoTransitionMode.shuffle.value,
    "cross_fade": VideoTransitionMode.cross_fade.value,
}

SCENE_MOTIONS: tuple[str, ...] = (
    "zoom", "static", "varied", "zoom_out", "pan_right", "pan_left", "zoom_top",
)


def transition_id(stored: Any) -> str:
    """把存着的渲染器值翻回界面用的 id，认不出来就当"没挑过"。"""
    value = str(stored or "").strip()
    for key, mapped in SCENE_TRANSITIONS.items():
        if mapped == value:
            return key
    return ""


def set_effects(
    task_id: str,
    index: Any,
    *,
    transition: str | None = None,
    image_motion: str | None = None,
    cut_sound: str | None = None,
) -> dict:
    """设置这一段画面的转场和运镜。

    两者在片子里的位置完全不同，但在界面上是同一件事（"这一段怎么出场"），
    所以放在一个入口里：

    * 转场是渲染期的效果，存下来就行，渲染时按剪辑表逐段应用；
    * 运镜在出图那一刻就烘进 mp4 了,所以改它要从原图重烘一段——几秒钟的
      本机编码,而且 zoom/static 来回切换时第二次起直接命中磁盘上的旧文件。

    两者都用空字符串表示"跟整条片子一样"。
    """
    _require_idle(task_id)
    with _editing(task_id):
        scenes = _scenes_or_fail(task_id)
        position = _scene_at(scenes, index)
        scene = scenes[position]

        if transition is not None:
            chosen = str(transition).strip()
            if chosen and chosen not in SCENE_TRANSITIONS:
                raise SceneError(
                    f"unknown transition {chosen!r}; pick one of: "
                    f"{', '.join(sorted(SCENE_TRANSITIONS))} (or empty to "
                    "follow the video)"
                )
            if chosen:
                scene["transition"] = SCENE_TRANSITIONS[chosen]
            else:
                scene.pop("transition", None)

        if cut_sound is not None:
            sound = str(cut_sound).strip()
            if sound and sound != "none":
                # 名字必须当场校验。等到渲染那一刻才发现"这台机器上没有这个
                # 音效"，用户已经等完了一整次渲染,而片子里就是没响。
                try:
                    transition_sound.resolve_transition_sound(sound)
                except ValueError as exc:
                    raise SceneError(
                        f"unknown transition sound {sound!r}: {exc}"
                    ) from exc
            if sound:
                scene["cut_sound"] = sound
            else:
                scene.pop("cut_sound", None)

        if image_motion is not None:
            motion = str(image_motion).strip()
            if motion and motion not in SCENE_MOTIONS:
                raise SceneError(
                    f"unknown picture motion {motion!r}; pick one of: "
                    f"{', '.join(SCENE_MOTIONS)} (or empty to follow the video)"
                )
            _apply_scene_motion(task_id, scene, motion)

        scene_manifest.save_scenes(task_id, scenes)
        return scenes[position]


def edit_subtitle(task_id: str, index: Any, text: str) -> dict:
    """改一条字幕的文字，时间轴和配音都不动。

    这和改脚本是两件事，不能混在一起：

    * 改脚本是改**要念的词**，必须重新配音才算数，整条字幕的时间点也会跟着
      重来；
    * 改字幕只是改**屏幕上那行字**。听写把 "paddy" 认成 "padding" 的时候，
      要改的就是这一条——为一个字重跑一遍 TTS 并作废全部时间点，代价完全不
      成比例。

    所以这里只重写字幕文件里那一条的正文，时间戳按原样抄回去（不重新格式
    化，免得在浮点换算上丢精度），配音文件一个字节都不动。

    代价要说在前面：之后真去重新配音时，字幕是按新配音重新生成的，这里的
    改动会被盖掉。界面上写明了这一点。
    """
    _require_idle(task_id)
    with _editing(task_id):
        path = _subtitle_file(task_id)
        cues = subtitle_reader.file_to_subtitles(path)
        if not cues:
            raise SceneError("this task has no subtitles to edit")
        try:
            position = int(index)
        except (TypeError, ValueError) as exc:
            raise SceneError(f"subtitle index must be a whole number, got {index!r}") from exc
        if not 0 <= position < len(cues):
            raise SceneError(
                f"there is no subtitle {position}; this task has {len(cues)}"
            )

        wanted = str(text or "").strip()
        if not wanted:
            raise SceneError(
                "a caption cannot be empty; put a space-free dash in it if you "
                "want it blank on screen"
            )

        blocks = []
        for order, (_number, times, body) in enumerate(cues):
            blocks.append(
                f"{order + 1}\n{times}\n{wanted if order == position else body}\n"
            )
        if not subtitle_writer.write_subtitle_file(path, "\n".join(blocks)):
            raise SceneError("failed to save the caption")
        logger.info(f"edited subtitle {position} of task {task_id}")
    return {"index": position, "text": wanted}


def _subtitle_file(task_id: str) -> str:
    """这条任务的字幕文件，没有就报错点名。"""
    payload = scene_manifest.load_raw(task_id)
    stored = str(payload.get("subtitle_path") or "").strip()
    if not stored:
        raise SceneError(
            "this task has no subtitle file; it was generated with subtitles "
            "turned off"
        )
    path = scene_manifest.resolve_task_file(task_id, stored)
    if not os.path.isfile(path):
        raise SceneError(f"the subtitle file is missing: {os.path.basename(path)}")
    return path


def delete_video(task_id: str) -> dict:
    """把一条片子从磁盘上永久删掉：画过的图、配音、字幕、成片，全部。

    为什么不用 ``DELETE /tasks/{id}``：那一条只认进程内存里的任务表，API 一
    重启表就空了，于是"我的视频"里的每一条旧片子都删不掉（404）。这里和
    ``library`` 一样，以磁盘为准。

    正在跑的不删：流水线和出图队列会接着往这个目录里写，删到一半的目录比
    不删更糟。

    先把整个目录改名挪走、再删。改名是一步到位的：有文件正被占用（Windows
    上浏览器还在播它）时改名整个失败，什么都没动，而不是删掉一半剩一半。
    """
    name = str(task_id or "").strip()
    root = os.path.abspath(utils.task_dir())
    folder = os.path.abspath(os.path.join(root, name))
    # 只认任务目录下的一级子目录。``..``、带斜杠的、空的，都不是一条片子。
    if (
        not name
        or name in (".", "..")
        or name != os.path.basename(name)
        or os.path.dirname(folder) != root
    ):
        raise SceneError("that is not the id of a video")
    if not os.path.isdir(folder) or os.path.islink(folder):
        raise SceneError("that video is not on this computer any more")

    task = sm.state.get_task(name)
    if task and tm.is_task_busy(task):
        raise SceneBusyError(
            "this video is still being made; wait for it to finish (or stop "
            "it) before deleting it"
        )
    from app.services import scene_regen_queue

    if scene_regen_queue.snapshot(name).get("busy"):
        raise SceneBusyError(
            "pictures of this video are still being drawn; wait for them to "
            "finish (or cancel them) before deleting it"
        )

    files = 0
    size = 0
    for parent, _, names in os.walk(folder):
        for one in names:
            files += 1
            try:
                size += os.path.getsize(os.path.join(parent, one))
            except OSError:
                pass

    import uuid

    # 挪到任务目录的上一层：同一块盘（改名才是一步到位的），又不在列表里。
    trash = os.path.join(
        os.path.dirname(root), f".deleting-{name}-{uuid.uuid4().hex[:8]}"
    )
    with _editing(name):
        try:
            os.rename(folder, trash)
        except OSError as exc:
            raise SceneError(
                "this video could not be deleted because one of its files is "
                "in use (a preview that is still playing, or a folder window "
                f"open on it). Nothing was deleted; close it and try again. ({exc})"
            ) from exc
    try:
        shutil.rmtree(trash)
    except OSError as exc:
        # 已经不在"我的视频"里了；剩下的是一个占地方的目录，不是一条片子。
        logger.warning(f"could not finish removing {trash}: {exc}")

    sm.state.delete_task(name)
    scene_regen_queue.dismiss(name)
    with _PHOTO_LOCK:
        _OFFERED.pop(name, None)
    logger.success(f"video deleted for good: {name} ({files} files, {size} bytes)")
    return {"task_id": name, "files": files, "bytes": size}


def library(endpoint: str = "", *, page: int = 1, page_size: int = 24) -> dict:
    """磁盘上所有做过的片子，最近动过的排在前面。

    为什么不用 ``GET /tasks``：那一份是进程内存里的任务表，API 一重启就空了。
    而"我做过哪些片子"这件事的真正记录在磁盘上——每个任务目录里的
    ``script.json``。用户隔天回来想接着改前天那条片子，内存里什么都没有。

    先按修改时间排序、再只读这一页的清单：任务目录会一直攒下去（这台机器上
    已经有一百多个），每次请求都把每一份清单读一遍是没必要的。
    """
    root = utils.task_dir()
    try:
        names = os.listdir(root)
    except OSError as exc:
        logger.warning(f"cannot list the task folder: {exc}")
        return {"tasks": [], "total": 0, "page": page, "page_size": page_size}

    found: list[tuple[float, str]] = []
    for name in names:
        manifest = os.path.join(root, name, "script.json")
        try:
            found.append((os.path.getmtime(manifest), name))
        except OSError:
            # 没有清单的目录不是一条做过的片子（半路失败、或别的东西留下的）。
            continue
    found.sort(reverse=True)

    total = len(found)
    start = max(0, (int(page) - 1) * int(page_size))
    window = found[start:start + int(page_size)]

    tasks = []
    for changed_at, task_id in window:
        try:
            tasks.append(_library_entry(task_id, changed_at, endpoint))
        except Exception as exc:  # pragma: no cover - 一条读不出来不该毁掉整页
            logger.warning(f"skipping {task_id} in the library: {exc}")
    return {"tasks": tasks, "total": total, "page": page, "page_size": page_size}


def _library_entry(task_id: str, changed_at: float, endpoint: str) -> dict:
    """列表里的一条：够用户认出这是哪条片子，不多读一个字节。"""
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    raw = scene_manifest.load_raw(task_id)
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}

    subject = str(params.get("video_subject") or "").strip()
    if not subject:
        # 没有主题就用脚本开头，总比一串 uuid 强。
        subject = " ".join(str(manifest.get("script") or "").split())[:80]

    # 出过片的任务要能一眼看出来，并且能直接点开看。
    directory = utils.task_dir(task_id)
    finals = sorted(
        name for name in os.listdir(directory)
        if name.startswith("final-") and name.endswith(".mp4")
    ) if os.path.isdir(directory) else []

    return {
        "task_id": task_id,
        "subject": subject,
        "scenes": len(scenes),
        "narration": round(float(manifest.get("audio_duration") or 0.0), 1),
        "audio_stale": bool(manifest.get("audio_stale")),
        "source": str(params.get("video_source") or ""),
        "updated_at": round(float(changed_at), 3),
        "rendered": bool(finals),
        # 画面出了一半就断掉的任务要能一眼看出来,并且能直接接着跑。只在没有
        # 场景的任务上算,正常任务不多付一次探测配音时长的代价。
        "unfinished": unfinished_materials(task_id) if not scenes else
        {"resumable": False, "pictures": 0, "needed": 0, "narration": 0.0},
        "video_url": media_url(task_id, os.path.join(directory, finals[-1]), endpoint)
        if finals else "",
        # 第一段画面当封面。读不出来就空着，界面画一个占位块。
        "thumb_url": media_url(task_id, scenes[0]["material"], endpoint)
        if scenes else "",
    }


def suggested_keyword(task_id: str, index: Any) -> str:
    """这一段没有关键词时，拿什么去出图。

    一条任务常常关键词比场景少——这条片子 25 段画面只拿到 12 个关键词，后面
    13 段的关键词是空的。空关键词在重生成那里直接被拒，于是用户要么逐段手敲
    一句，要么那几段永远换不了画面。

    退回"片子主题 + 这一段念的那句话"。它不如模型逐句写出来的画面描述，但
    它至少说的是这一刻的事，而且画风模板还会接在后面。界面也把这句话填进
    关键词框当占位文字，所以用户看得见它要拿什么去画，改一笔就是自己的。

    Keyword lists are routinely shorter than the scene list, and an empty
    keyword is refused outright, which leaves those scenes unregenerable. The
    subject plus the line spoken over this scene is a weaker prompt than a
    written-out visual description, but it is about this moment rather than
    about nothing.
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    try:
        position = _scene_at(scenes, index)
    except SceneError:
        return ""
    spoken = narration_for_scenes(task_id, manifest)
    line = " ".join(
        str(spoken[position] if position < len(spoken) else "").split()
    )
    try:
        subject = str(
            getattr(load_params(task_id), "video_subject", "") or ""
        ).strip()
    except SceneError:
        subject = ""
    parts = [part for part in (subject, line) if part]
    return ". ".join(parts)[:400]


def suggest_image_prompt(
    task_id: str, index: Any, *, direction: str = "", current: str = "",
    fresh: bool = False,
) -> str:
    """按用户的一句话，把这一段的提示词写成一条完整的画面描述。

    只返回那句话，不落盘。用户在界面上看到的是填进框里的草稿，要不要留下来
    由他按"保存"或"重新生成"决定——模型写得不对的时候，一次点击不该已经把
    原来那条覆盖掉了。

    Returns the text without saving it: the suggestion lands in the box as a
    draft and the user decides whether to keep it, so one click cannot
    overwrite a prompt they wrote by hand.

    ``current`` 是框里**当前**的内容（可能有还没保存的改动），空的时候才退回
    清单里存着的那条。``direction`` 是用户那句短话；两者都空、这一段又没有
    旁白时才会报错。

    上下文（主题、画风、前后几句旁白、这是第几段）在这里组好再交给
    ``llm.improve_image_prompt``：界面不该知道这些，而模型少了它们就会把一段
    中间的画面写成开场镜头或者"记得订阅"。
    """
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    position = _scene_at(scenes, index)
    scene = scenes[position]

    spoken = narration_for_scenes(task_id, manifest)

    def _line(at: int) -> str:
        if at < 0 or at >= len(spoken):
            return ""
        return " ".join(str(spoken[at] or "").split())

    # 前后各两句只给模型读，用来接住人物和场景，不给它们写提示词。连续取,
    # 中间那句是空的也照样占位,否则 ``_numbered_lines`` 的编号会对错行。
    before = [_line(at) for at in range(max(0, position - 2), position)]
    after = [_line(at) for at in range(position + 1, min(len(scenes), position + 3))]

    params = load_params(task_id)
    subject = str(getattr(params, "video_subject", "") or "").strip()
    # 这一段自己挑过画风就用它，没挑过才用整条片子的——和重新生成走同一个解析,
    # 所以草稿和真正出图时用的是同一套风格词。
    style = _image_prompt_template(params, scene.get("image_style"))

    # ``fresh`` 是"换个想法"：存着的那条不递给模型。递过去的话模型被要求
    # "保留里面已经对的东西"，于是一条配错了句子的提示词怎么点都还是它。
    chosen = str(current or "").strip() or (
        "" if fresh else str(scene.get("keyword") or "")
    )
    try:
        return llm.improve_image_prompt(
            subject,
            _line(position),
            direction=direction,
            current=chosen,
            style=style,
            number=position + 1,
            total=len(scenes),
            before=before if any(before) else (),
            after=after if any(after) else (),
        )
    except llm.TermGenerationError as exc:
        raise SceneError(str(exc)) from exc


def _patch_params(task_id: str, **fields: Any) -> dict:
    """改这条任务自己存着的渲染参数。

    审片之后的渲染读的是清单里的 ``params``，不是 WebUI 的 session，也不是全局
    配置。所以在编辑器里改的每一项都只影响这一条片子——这正是用户要的：为了
    眼前这条片子调一下字幕颜色,不该把下一条也改了。
    """
    payload = scene_manifest.load_raw(task_id)
    stored = payload.get("params")
    if not isinstance(stored, dict):
        raise SceneError("this task has no saved parameters; it cannot be changed")
    stored.update(fields)
    if not task_artifacts.patch_script_data(task_id, params=stored):
        raise SceneError("failed to save the render settings")
    return stored


SUBTITLE_POSITIONS: tuple[str, ...] = (
    "top",
    "center",
    "bottom",
    "two_thirds_bottom",
    "custom",
)

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def available_fonts() -> list[str]:
    """字体目录里的字体文件名，含子目录。"""
    directory = utils.font_dir()
    found: list[str] = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.lower().endswith((".ttf", ".ttc", ".otf")):
                relative = os.path.relpath(os.path.join(root, name), directory)
                found.append(relative.replace(os.sep, "/"))
    return sorted(found)


def subtitle_style(task_id: str) -> dict:
    """字幕的样子，以及这台机器上有哪些字体可选。"""
    params = load_params(task_id)
    background = getattr(params, "text_background_color", False)
    return {
        "enabled": bool(getattr(params, "subtitle_enabled", True)),
        "font_name": str(getattr(params, "font_name", "") or ""),
        "font_size": int(getattr(params, "font_size", 60) or 60),
        "color": str(getattr(params, "text_fore_color", "") or "#FFFFFF"),
        "stroke_color": str(getattr(params, "stroke_color", "") or "#000000"),
        "stroke_width": float(getattr(params, "stroke_width", 0) or 0.0),
        "background": background if isinstance(background, str) else bool(background),
        "position": str(getattr(params, "subtitle_position", "") or "bottom"),
        "custom_position": float(getattr(params, "custom_position", 70.0) or 0.0),
        "positions": list(SUBTITLE_POSITIONS),
        "fonts": available_fonts(),
    }


def set_subtitle_style(task_id: str, **wanted: Any) -> dict:
    """改字幕的样子：字体、字号、颜色、描边、底色、位置。

    只校验、只落盘，不碰字幕文件本身：这些是渲染时才用到的样式，改它们既不
    会动时间轴，也不会让配音失效。

    颜色一律按 ``#RRGGBB`` 收：渲染那边把它交给 Pillow，一个拼错的颜色名会
    在渲染的最后一步才炸，那时用户已经等完了整条片子。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if "enabled" in wanted and wanted["enabled"] is not None:
            fields["subtitle_enabled"] = bool(wanted["enabled"])

        font = wanted.get("font_name")
        if font is not None:
            name = str(font).strip()
            if name and name not in available_fonts():
                raise SceneError(
                    f"unknown font {name!r}; pick one of the fonts in "
                    "resource/fonts"
                )
            fields["font_name"] = name

        size = wanted.get("font_size")
        if size is not None:
            try:
                value = int(size)
            except (TypeError, ValueError) as exc:
                raise SceneError("the font size must be a whole number") from exc
            if not 8 <= value <= 300:
                raise SceneError(f"the font size must be between 8 and 300, got {value}")
            fields["font_size"] = value

        for key, field in (("color", "text_fore_color"), ("stroke_color", "stroke_color")):
            colour = wanted.get(key)
            if colour is None:
                continue
            text = str(colour).strip()
            if not _COLOUR.match(text):
                raise SceneError(
                    f"{key} must look like #RRGGBB, got {text!r}"
                )
            fields[field] = text.upper()

        width = wanted.get("stroke_width")
        if width is not None:
            try:
                value = float(width)
            except (TypeError, ValueError) as exc:
                raise SceneError("the outline width must be a number") from exc
            if not 0 <= value <= 20:
                raise SceneError(
                    f"the outline width must be between 0 and 20, got {value}"
                )
            fields["stroke_width"] = round(value, 2)

        background = wanted.get("background")
        if background is not None:
            # False 是"不要底色"，字符串是颜色。两者都是渲染器认的写法。
            if isinstance(background, bool) or background == "":
                fields["text_background_color"] = False
            else:
                text = str(background).strip()
                if not _COLOUR.match(text):
                    raise SceneError(
                        f"the caption background must look like #RRGGBB or be "
                        f"turned off, got {text!r}"
                    )
                fields["text_background_color"] = text.upper()

        position = wanted.get("position")
        if position is not None:
            text = str(position).strip()
            if text not in SUBTITLE_POSITIONS:
                raise SceneError(
                    f"unknown caption position {text!r}; pick one of: "
                    f"{', '.join(SUBTITLE_POSITIONS)}"
                )
            fields["subtitle_position"] = text

        custom = wanted.get("custom_position")
        if custom is not None:
            try:
                value = float(custom)
            except (TypeError, ValueError) as exc:
                raise SceneError("the custom position must be a number") from exc
            if not 0 <= value <= 100:
                raise SceneError(
                    f"the custom position must be between 0 and 100, got {value}"
                )
            fields["custom_position"] = round(value, 1)

        if fields:
            _patch_params(task_id, **fields)
    return subtitle_style(task_id)


def video_effects(task_id: str) -> dict:
    """整条片子的转场和运镜，以及有几段自己挑过、不跟着它走。"""
    params = load_params(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    return {
        "transition": transition_id(
            _enum_value(getattr(params, "video_transition_mode", "")) or "none"
        ),
        "image_motion": str(_enum_value(getattr(params, "image_motion", "")) or ""),
        "transitions": list(SCENE_TRANSITIONS),
        "image_motions": list(SCENE_MOTIONS),
        # 成片精修：用哪种调色（空 = 关着），以及能挑哪些。
        "finish_look": str(getattr(params, "finish_look", "") or ""),
        "finish_looks": [
            {"id": look, "label": spec["label"]}
            for look, spec in finishing.LOOKS.items()
        ],
        # 界面要据此说清楚"改了这个，那几段还是不跟着变"。
        "scenes_with_own_transition": sum(
            1 for scene in scenes if str(scene.get("transition") or "").strip()
        ),
        "scenes": len(scenes),
    }


def set_video_effects(
    task_id: str,
    *,
    transition: str | None = None,
    image_motion: str | None = None,
    apply_to_scenes: bool = False,
    finish_look: str | None = None,
) -> dict:
    """一次改掉整条片子的转场（和运镜、成片精修）。

    ``apply_to_scenes`` 打开时，顺手清掉每一段自己挑过的转场——否则"全部改成
    淡入"之后，之前单独挑过的那几段仍然我行我素,而用户以为自己已经改完了。

    运镜这一项只影响**之后**出的图：运镜是出图那一刻烘进 mp4 的，已经在盘上
    的素材不会因为改了这个设置而变。要改已经出好的那一段,用那一段自己的运镜
    （会从原图重烘一份）。这件事界面上也写明了。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if transition is not None:
            chosen = str(transition).strip()
            if chosen and chosen not in SCENE_TRANSITIONS:
                raise SceneError(
                    f"unknown transition {chosen!r}; pick one of: "
                    f"{', '.join(sorted(SCENE_TRANSITIONS))}"
                )
            # 渲染器那边 "没有转场" 是 None，不是字符串 "none"。
            fields["video_transition_mode"] = (
                None if not chosen or chosen == "none" else SCENE_TRANSITIONS[chosen]
            )

        if image_motion is not None:
            motion = str(image_motion).strip()
            if motion and motion not in SCENE_MOTIONS:
                raise SceneError(
                    f"unknown picture motion {motion!r}; pick one of: "
                    f"{', '.join(SCENE_MOTIONS)}"
                )
            if motion:
                fields["image_motion"] = motion

        if finish_look is not None:
            look = str(finish_look).strip()
            if look and look not in finishing.LOOKS:
                raise SceneError(
                    f"unknown finish {look!r}; pick one of: "
                    f"{', '.join(finishing.LOOKS)} (or empty to turn it off)"
                )
            fields["finish_look"] = look

        if fields:
            _patch_params(task_id, **fields)

        if apply_to_scenes and transition is not None:
            scenes = scene_manifest.load(task_id)["scenes"]
            cleared = 0
            for scene in scenes:
                if scene.pop("transition", None) is not None:
                    cleared += 1
            if cleared:
                scene_manifest.save_scenes(task_id, scenes)
                logger.info(
                    f"cleared {cleared} per-scene transitions of task {task_id}"
                )
    return video_effects(task_id)


def cut_sound_settings(task_id: str) -> dict:
    """整条片子在每一刀上响的那一声，以及可选的音效。"""
    params = load_params(task_id)
    try:
        library = [os.path.basename(p) for p in transition_sound.list_transition_sounds()]
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the transition sounds: {exc}")
        library = []
    return {
        "sound": str(getattr(params, "transition_sound", "") or ""),
        "volume": float(getattr(params, "transition_sound_volume", 0.0) or 0.0),
        "sounds": library,
    }


def set_cut_sound(
    task_id: str,
    *,
    sound: str | None = None,
    volume: float | None = None,
) -> dict:
    """换整条片子的转场音效，或者把它整个关掉。

    ``sound`` 空字符串就是"一刀都不要响"——这是唯一一个能把它从整条片子上
    拿掉的入口。每一段自己挑的那个"静音"只管自己那一刀。
    """
    _require_idle(task_id)
    fields: dict[str, Any] = {}
    with _editing(task_id):
        if sound is not None:
            name = str(sound).strip()
            if name:
                try:
                    transition_sound.resolve_transition_sound(name)
                except ValueError as exc:
                    raise SceneError(f"unknown transition sound {name!r}: {exc}") from exc
            fields["transition_sound"] = name

        if volume is not None:
            try:
                level = float(volume)
            except (TypeError, ValueError) as exc:
                raise SceneError("the sound volume must be a number") from exc
            if not 0.0 <= level <= 1.0:
                raise SceneError(
                    f"the sound volume must be between 0 and 1, got {level}"
                )
            fields["transition_sound_volume"] = round(level, 3)

        if fields:
            _patch_params(task_id, **fields)
    return cut_sound_settings(task_id)


RANDOM_BACKGROUND = "random"


def background_music(task_id: str) -> dict:
    """这条片子的背景音乐：选了什么、多响，以及这台机器上有哪些可选。"""
    params = load_params(task_id)
    chosen = str(getattr(params, "bgm_file", "") or "").strip()
    kind = str(getattr(params, "bgm_type", "") or "").strip()
    try:
        tracks = bgm.list_bgm_files()
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住审片
        logger.warning(f"cannot list the background music: {exc}")
        tracks = []
    return {
        # 界面只认三种：不要、随机、某一首。bgm_type 里那些外部提供商
        # (sonilo/elevenlabs) 这里原样显示为"由设置决定"，不去改它。
        "track": (
            os.path.basename(chosen)
            if chosen
            else (RANDOM_BACKGROUND if kind == RANDOM_BACKGROUND else "")
        ),
        "kind": kind,
        "volume": float(getattr(params, "bgm_volume", 0.0) or 0.0),
        "tracks": [os.path.basename(path) for path in tracks],
        # 上传的曲子存成一串 UUID：这是它们原来的名字，给下拉框显示用。
        "names": bgm.uploaded_bgm_names(),
        # 这条片子交给外部服务生成配乐时，界面不该假装能在这里换曲子。
        "external": kind in {"sonilo", "elevenlabs"},
    }


def set_background_music(
    task_id: str,
    *,
    track: str | None = None,
    volume: float | None = None,
) -> dict:
    """换掉这条片子的背景音乐，或者调它的音量。

    ``track`` 空字符串是"不要背景音乐"，``"random"`` 是每次渲染随机挑一首，
    其余按文件名解析。名字当场校验：等渲染完才发现这台机器上没有这首曲子,
    用户已经白等了一整次渲染,而片子里就是没有音乐。

    改的是任务清单里的 ``params``——审片之后的渲染读的就是那一份，不是
    WebUI 的 session，也不是全局配置。所以这里换的曲子只影响这一条片子。
    """
    _require_idle(task_id)
    with _editing(task_id):
        params = load_params(task_id)
        payload = scene_manifest.load_raw(task_id)
        stored = payload.get("params")
        if not isinstance(stored, dict):
            raise SceneError(
                "this task has no saved parameters; its music cannot be changed"
            )

        if track is not None:
            wanted = str(track).strip()
            if not wanted:
                stored["bgm_type"] = ""
                stored["bgm_file"] = ""
            elif wanted == RANDOM_BACKGROUND:
                stored["bgm_type"] = RANDOM_BACKGROUND
                stored["bgm_file"] = ""
            else:
                try:
                    resolved = bgm.resolve_bgm_file(wanted)
                except ValueError as exc:
                    raise SceneError(
                        f"unknown background music {wanted!r}: {exc}"
                    ) from exc
                # 内置歌曲和用户自己丢进去的分别记成 preset/custom，WebUI 的
                # 来源下拉框据此显示，不至于把一首内置曲子说成"自定义"。
                builtin = {
                    os.path.basename(path) for path in bgm.list_builtin_bgm_files()
                }
                stored["bgm_type"] = (
                    "preset" if os.path.basename(resolved) in builtin else "custom"
                )
                stored["bgm_file"] = resolved

        if volume is not None:
            try:
                level = float(volume)
            except (TypeError, ValueError) as exc:
                raise SceneError("the music volume must be a number") from exc
            if not 0.0 <= level <= 1.0:
                raise SceneError(
                    f"the music volume must be between 0 and 1, got {level}"
                )
            stored["bgm_volume"] = round(level, 3)

        if not task_artifacts.patch_script_data(task_id, params=stored):
            raise SceneError("failed to save the background music")
    return background_music(task_id)


def _apply_scene_motion(task_id: str, scene: dict, motion: str) -> None:
    """按新的运镜重烘这一段的素材，并记下用户挑的是哪一种。

    空字符串是"跟整条片子一样"，这时按任务参数里的运镜重烘一次——否则"改回
    默认"会让画面停在上一次挑的那个样子,而界面显示的是"跟片子一样"。
    """
    params = load_params(task_id)
    wanted = motion or str(_enum_value(getattr(params, "image_motion", "")) or "zoom")
    material = scene_manifest.resolve_material(task_id, scene["material"])
    if timeline.motion_of(material) == wanted:
        # 已经是这个运镜了，不必重烘。记号还是要更新：用户可能是从"静止"
        # 切回"跟片子一样"，而片子本来就是静止的。
        pass
    else:
        try:
            rendered = timeline.ensure_image_motion(task_id, material, wanted)
        except timeline.TimelineError as exc:
            raise SceneError(str(exc)) from exc
        scene["material"] = scene_manifest.relative_media_path(task_id, rendered)
        scene["kind"] = scene_manifest.material_kind(rendered)
    if motion:
        scene["image_motion"] = motion
    else:
        scene.pop("image_motion", None)


def update_timing(
    task_id: str,
    index: int,
    *,
    render: bool = False,
    trim_start=None,
    duration=None,
    reset: bool = False,
) -> dict:
    """设置一个场景的入点和在成片里的停留时长。

    先按素材真实长度算一遍再落盘：用户在时间线上完全可能拖过素材末尾，
    那时应该收到"已经按素材长度收住"，而不是渲染到这一段才失败。
    """
    _require_idle(task_id)
    scenes = scene_manifest.load(task_id)["scenes"]
    if not 0 <= index < len(scenes):
        raise SceneError(
            f"scene {index} does not exist; this task has {len(scenes)} scenes"
        )

    if reset:
        return scene_manifest.clear_scene_timing(task_id, index)

    scene = scenes[index]
    material = scene_manifest.resolve_material(task_id, scene.get("material"))
    params = load_params(task_id)
    speed = getattr(params, "video_clip_speed", 1.0) or 1.0

    source_duration = timeline.probe_duration(material)
    current = scene_manifest.scene_timing(scene)
    requested_start = current["trim_start"] if trim_start is None else trim_start
    requested_duration = current["duration"] if duration is None else duration

    needed_source = None
    if requested_duration is not None:
        try:
            needed_source = float(requested_duration) * max(float(speed), 1e-6)
        except (TypeError, ValueError):
            needed_source = None

    # 拖一张图拖得比烘出来的片段长，这里**不重渲**（``render`` 为假时），
    # 只把数字记下来。
    #
    # 为什么：重渲是一次 ffmpeg，几秒起步。挂在拖拽上就是每拖一次卡几秒，
    # 连着拖几次还会有两次重渲同时在跑、互相盖掉对方的输出——真的发生过，
    # 场景被指到一个还没写完的文件上，整个编辑器就打不开了。剪辑软件也不
    # 是这么做的：拖的时候只改数字，真正出片的时候才渲。
    #
    # 而且本来就不需要：``build_edit_list`` 在出片时自己会把图片按需要的
    # 长度重渲一遍（它那边的判断一直是对的——"不是视频，或者原图还在"）。
    # 所以这里放行、记下来就够了，剩下的交给出片那一步。
    #
    # 放行的只有"重渲得出来"的那些。网上下回来的视频没有原图，拉不过自己
    # 的长度是物理，照旧夹回去并报 ``clamped``。
    # ``render=True`` 是另一回事：那不是拖拽，是"按旁白铺满"这种一次性的
    # 批量动作，本来就慢，而且它要的是**现在就**拿到正确长度的素材。
    may_exceed = False
    if render:
        if (
            needed_source is not None
            and needed_source > source_duration - float(requested_start or 0.0) + 1e-6
        ):
            extended = timeline.ensure_image_length(
                task_id, material, float(requested_start or 0.0) + needed_source
            )
            if extended != material:
                _point_scene_at(task_id, index, extended)
                material = extended
                source_duration = timeline.probe_duration(material)
    else:
        # 看文件名，不去翻磁盘。``source_image_for`` 要把任务目录里每张
        # png 都打开重算一遍摘要才能确认：实测一次 2.42 秒，挂在拖拽上就
        # 是每松手卡两秒半，那和之前跑 ffmpeg 没差多少。
        #
        # 猜错的代价仍旧是零：原图真被删了的话，出片那一步照旧夹回素材长
        # 度并说一句 clamped。
        may_exceed = timeline.baked_from_an_image(material)

    timing = timeline.normalise_timing(
        source_duration=source_duration,
        trim_start=requested_start,
        duration=requested_duration,
        clip_speed=speed,
        may_exceed_source=may_exceed,
    )
    # 只把写清单那一下圈进锁里。上面的 ``ensure_image_length`` 可能要重渲一
    # 段画面，拿着锁去做它没必要。
    with _editing(task_id):
        scene = scene_manifest.patch_scene_timing(
            task_id,
            index,
            trim_start=timing["trim_start"],
            duration=timing["duration"],
        )
    scene["clamped"] = timing["clamped"]
    scene["source_duration"] = timing["source_duration"]
    return scene


# ---------------------------------------------------------------------------
# 每张画面停多久：跟着它那句旁白走
# ---------------------------------------------------------------------------
#
# 提示词是一句旁白写一条的，所以第 i 张图画的就是第 i 句话。可每一段默认都
# 停同样长（``video_clip_duration``，比如 4 秒），而每句话念多久并不一样：
# "Fact one" 不到一秒，一个长句五秒多。于是时间线上所有画面一样宽，画面
# 越往后越和它那句话对不上——第 10 张图出来的时候，旁白早念到第 14 句了。
# 用户只能一段一段手拖。
#
# 这里把每一段的时长定成"它那句话开始，到下一段那句话开始"。念到哪句话，
# 屏幕上就是为那句话画的图。

_PACED_KEY = "paced_to_narration"


def _spoken_key(text: Any) -> str:
    """只留字母数字，用来比"这是不是同一句话"。字幕和脚本的标点、大小写、
    空白常常不一样。"""
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def narration_paced_durations(task_id: str, manifest: dict | None = None) -> list:
    """每一段该停多久，才能和它那句旁白同时出现、同时换走。

    按文字认：每一段的台词是若干条连着的字幕。中间没有哪一段要的字幕（那
    一段被删了）归前一段——那几句话念着的时候，前一张图接着停。

    对不上就报错并点名是哪一段，不猜：猜错的结果是整条时间线悄悄错位。
    """
    if manifest is None:
        manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    if not scenes:
        raise SceneError("this video has no scenes yet")
    if manifest.get("audio_stale"):
        raise SceneError(
            "the narration was changed and not re-recorded yet, so there is "
            "no timing to match; re-record the narration first"
        )
    cues = scene_manifest.narration_segments(task_id)
    if not cues:
        raise SceneError(
            "this video has no subtitle timing to tell when each line is "
            "spoken, so the pictures cannot be matched to the narration"
        )
    keys = [_spoken_key(cue.get("text")) for cue in cues]

    starts: list[float] = []
    at = 0
    for position, scene in enumerate(scenes):
        wanted = _spoken_key(scene.get("narration"))
        if not wanted:
            raise SceneError(
                f"scene {position + 1} has no narration, so there is no line "
                "to time it to"
            )
        found = -1
        for first in range(at, len(cues)):
            if not keys[first] or not wanted.startswith(keys[first]):
                continue
            joined = ""
            last = first
            while last < len(cues) and len(joined) < len(wanted):
                joined += keys[last]
                last += 1
            if joined == wanted:
                found = first
                at = last
                break
        if found < 0:
            raise SceneError(
                f"the narration of scene {position + 1} is not in the recorded "
                "narration in this order (it was edited or moved), so the "
                "pictures cannot be matched to it automatically"
            )
        starts.append(float(cues[found]["start"]))

    end = max(
        float(manifest.get("audio_duration") or 0.0), float(cues[-1]["end"])
    )
    # 第一段从 0 开始：开头那一点静音里也得有画面。
    starts[0] = 0.0
    edges = starts[1:] + [end]
    return [
        round(max(timeline.MIN_CLIP_DURATION, stop - start), 3)
        for start, stop in zip(starts, edges)
    ]


def _planned_spoken_durations(task_id: str, manifest: dict) -> list | None:
    """没人动过的片子，出片时每张画面本来就会停多久。

    渲染器早就是按旁白逐句铺的（``timeline.plan_windows``），只是时间线界面
    画的不是这组数字，而是每段素材自己的长度——所以看上去每张图一样宽；而且
    用户一拖任何一段，整条片子就从"按句铺"切到"各按各的长度"，全部变成一样
    长。把渲染器那组数字原样写成每段的时长，界面和成片就是同一回事了。

    用渲染器自己的那两个函数，不另算一套：分组（一张图配几条字幕）的账只能
    有一本。没有逐句时间、或者账对不上时返回 ``None``。
    """
    scenes = manifest["scenes"]
    cues = scene_manifest.narration_segments(task_id)
    if not cues or not scenes:
        return None
    target = max(0.0, float(manifest.get("audio_duration") or 0.0))
    if len(cues) == len(scenes):
        windows = timeline._windows_from_cues(cues, target)
    else:
        windows = timeline._windows_from_grouped_cues(task_id, cues, scenes, target)
    if not windows or len(windows) != len(scenes):
        return None
    return [
        round(max(timeline.MIN_CLIP_DURATION,
                  float(window["end"]) - float(window["start"])), 3)
        for window in windows
    ]


def pace_to_narration(task_id: str, *, automatic: bool = False) -> dict:
    """把每一段的时长定成它那句旁白的时长。

    ``automatic`` 是打开片子时程序自己做的那一次：不算一步编辑。用户点按钮
    的那一次算，可以撤销。
    """
    _require_idle(task_id)
    params = load_params(task_id)
    speed = max(float(getattr(params, "video_clip_speed", 1.0) or 1.0), 1e-6)
    with _editing(task_id):
        manifest = scene_manifest.load(task_id)
        scenes = manifest["scenes"]
        durations = None
        if automatic and not scene_manifest.has_timing_edits(scenes):
            durations = _planned_spoken_durations(task_id, manifest)
        if durations is None:
            durations = narration_paced_durations(task_id, manifest)
        short = 0
        for scene, wanted in zip(scenes, durations):
            material = str(scene.get("material") or "").strip()
            # 图片停多久都行（出片时按需要的长度重新烘）。下载来的视频拉不过
            # 它自己的长度。
            if material and not timeline.baked_from_an_image(material) \
                    and scene_manifest.material_kind(material) == "video":
                try:
                    source = timeline.probe_duration(
                        scene_manifest.resolve_material(task_id, material)
                    )
                except Exception:
                    source = 0.0
                trim = scene_manifest.scene_timing(scene)["trim_start"]
                room = (source - trim) / speed
                if room > 0 and wanted > room + 1e-6:
                    wanted = round(max(timeline.MIN_CLIP_DURATION, room), 3)
                    short += 1
            scene["duration"] = wanted
        extra = {_PACED_KEY: True}
        if automatic:
            with edit_history.automatic():
                scene_manifest.save_scenes(task_id, scenes, **extra)
        else:
            with edit_history.labelled("Match narration"):
                scene_manifest.save_scenes(task_id, scenes, **extra)
    logger.info(
        f"timed {len(scenes)} scenes to their narration lines"
        f"{' (automatic)' if automatic else ''}, task_id: {task_id}"
    )
    return {"scenes": len(scenes), "held_short": short}


def _pace_new_video(task_id: str) -> None:
    """刚做出来、还没人动过的片子，打开时自动对一次。

    只做一次，而且只在"还没人碰过"的时候：
      * 拖过任何一段的不动——那是用户定的；
      * 出过片的不动——不该让一条已经做完的片子下次打开变了样；
      * 对过一次的不再对——用户之后拖成什么样就是什么样；
      * 不是"一句话一张图"的（旧的按关键词取材）对不上，也就不动。
    """
    raw = scene_manifest.load_raw(task_id)
    if raw.get(_PACED_KEY) or raw.get("audio_stale"):
        return
    scenes = raw.get("scenes") or []
    if not scenes or scene_manifest.has_timing_edits(scenes):
        return
    directory = os.path.join(utils.task_dir(), str(task_id))
    try:
        if any(name.startswith("final-") and name.endswith(".mp4")
               for name in os.listdir(directory)):
            return
    except OSError:
        return
    task = sm.state.get_task(task_id)
    if task and tm.is_task_busy(task):
        return
    try:
        pace_to_narration(task_id, automatic=True)
    except SceneError as exc:
        logger.debug(f"not timing {task_id} to its narration: {exc}")


def fit_to_narration(task_id: str) -> dict:
    """把现有场景按比例拉长或压缩，铺满整段旁白。

    为什么需要这个：素材的张数由关键词个数决定，不由旁白长度决定。一次
    12 个关键词、每段 2 秒的生成只有 24 秒画面,而旁白可能有 64 秒。自动出片
    那条路会把这 12 段循环播放近三遍来凑够时长;时间线是"所见即所得"的,
    不会替用户循环,于是时间线总长就停在 24 秒。

    这里按比例缩放而不是平均分配：用户可能已经手动调过某几段的长短,平均
    分配会把那些编辑一笔抹掉。全都没调过时,按比例缩放的结果本来就等于平均。

    视频素材拉不过自身长度,会被 ``update_timing`` 收住,所以返回值里带上
    实际达到的总长,让调用方如实告诉用户差了多少,而不是假装铺满了。
    """
    _require_idle(task_id)
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )

    target = float(manifest.get("audio_duration") or 0.0)
    if target <= 0:
        raise SceneError(
            "this task has no narration to fit to; record the narration first"
        )

    params = load_params(task_id)
    speed = float(getattr(params, "video_clip_speed", 1.0) or 1.0) or 1.0

    current: list[float] = []
    for scene in scenes:
        material = scene_manifest.resolve_material(task_id, scene.get("material"))
        source_duration = timeline.probe_duration(material)
        timing = scene_manifest.scene_timing(scene)
        if timing["duration"] is not None:
            held = float(timing["duration"])
        else:
            held = (source_duration - timing["trim_start"]) / speed
        current.append(max(timeline.MIN_CLIP_DURATION, held))

    total = sum(current)
    if total <= 0:
        raise SceneError("these scenes have no usable duration")

    factor = target / total
    for index, held in enumerate(current):
        # 这条路要的是"现在就把素材渲成对的长度"：它是用户点一下
        # 的批量动作，不是拖拽。拖拽那条路只记数字。
        update_timing(
            task_id, index, duration=round(held * factor, 3), render=True
        )

    logger.info(
        f"fitted {len(scenes)} scenes to {target:.2f}s of narration "
        f"(scaled by {factor:.2f}x), task_id: {task_id}"
    )
    return describe(task_id, with_timing=True)


def _point_scene_at(task_id: str, index: int, material_path: str) -> None:
    """把某个场景指向一份新素材文件。供图片按新时长重渲后改写清单使用。"""
    with _editing(task_id):
        scenes = scene_manifest.load(task_id)["scenes"]
        if not 0 <= index < len(scenes):
            return
        # 绝对路径，和 ``record_prepared_artifacts`` 写进来的那些一致。
        # **不要**用 ``relative_media_path``：那个函数算的是 ``/stream`` 用
        # 的 URL 路径（相对于任务根目录，带任务 id 前缀），而清单里的路径是
        # 由 ``resolve_material`` 读的，它在任务自己的目录里找——于是那种写
        # 法永远找不到，一拖长就把这一段弄坏。
        scenes[index]["material"] = scene_manifest.resolve_material(
            task_id, material_path
        )
        scenes[index]["kind"] = scene_manifest.material_kind(material_path)
        scene_manifest.save_scenes(task_id, scenes)


def prepare_render(task_id: str) -> VideoParams:
    """准备恢复渲染所需的参数，并在提交前把能先查的前提都查掉。"""
    _require_idle(task_id)
    params = load_params(task_id)
    manifest = scene_manifest.load(task_id)
    scenes = manifest["scenes"]
    if not scenes:
        raise SceneError(
            "this task has no reviewed scenes; run the pipeline up to materials first"
        )
    waiting = scene_manifest.pending_scenes(scenes)
    if waiting:
        # 还有位置没画就开渲，渲出来的是一条缺着好几段画面的片子。点名还差
        # 几张，用户自己决定是去点"开画"还是先把那几段删掉。
        raise SceneError(
            f"{len(waiting)} scene(s) have no picture yet; draw them first "
            "or remove those scenes"
        )
    if manifest["audio_stale"]:
        raise SceneError(
            "the narration no longer matches the edited script; "
            "regenerate the audio before rendering"
        )
    # 素材文件在审片期间可能被外部删除。这里先全部解析一遍，让错误停在
    # 提交之前，而不是在后台渲染到一半才失败。
    scene_manifest.materials_for_render(task_id)

    # 只有真的拖过时间线才生成剪辑表。一次都没拖过时清掉它，任务就回到
    # 原来的自动切片逻辑——默认出片结果因此一个字节都不会变。
    # 只有真的拖过时间线才写剪辑表。一次都没拖过时清空它，任务就回到原来的
    # 自动切片逻辑——默认出片结果因此一个字节都不会变。
    #
    # 剪辑表落在任务清单里而不是只存内存：渲染任务重启后仍要渲染用户排好的
    # 那条时间线。素材按任务目录相对名存，整个任务目录搬走也还能解析。
    # 挑过转场的任务也要走剪辑表：每段的转场只能从剪辑表交到渲染器手上。
    if scene_manifest.has_render_overrides(scenes):
        speed = getattr(params, "video_clip_speed", 1.0) or 1.0
        edit_list = timeline.build_edit_list(task_id, scenes, clip_speed=speed)
        task_artifacts.patch_script_data(
            task_id,
            edit_list=[
                {
                    # 按文件名存，和 ``scenes[].material`` 一个写法：
                    # ``resolve_material`` 的白名单是"任务目录 + 上传目录",
                    # 存成 ``/stream`` 用的那种 ``<task_id>/<file>`` 反而解析不出来。
                    "material": os.path.basename(item["path"]),
                    "start": round(float(item["start"]), 3),
                    "end": round(float(item["end"]), 3),
                    # 没挑过转场的段不写这个键，渲染器照旧用整条片子的设置。
                    **(
                        {"transition": item["transition"]}
                        if item.get("transition")
                        else {}
                    ),
                    **(
                        {"cut_sound": item["cut_sound"]}
                        if item.get("cut_sound")
                        else {}
                    ),
                }
                for item in edit_list
            ],
        )
    else:
        task_artifacts.patch_script_data(task_id, edit_list=[])
    return params
