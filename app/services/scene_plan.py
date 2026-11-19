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
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, List, Optional

from app.utils import utils

#: 一份分镜最多多少段。一段四五秒，这已经是半个多小时的片子。
MAX_SCENES = 600
#: 照着照片画的那几段，照片上是什么。
PHOTO_KINDS = ("person", "place", "thing")
#: 分镜点了名的段在"值不值得找照片"上的分数：满分，一定挑中。
PHOTO_SCORE = 10

_IMAGE_TEXT = ("", "text", "text-to-image", "text2img", "txt2img", "t2i")
_IMAGE_PHOTO = ("photo", "image", "image-to-image", "img2img", "i2i")


class ScenePlanError(ValueError):
    """分镜读不懂，或者哪一段缺东西。说清楚是第几段、缺什么。"""


def _strip_fence(text: str) -> str:
    """模型爱把 JSON 包在 ```json 里：剥掉。"""
    match = re.match(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*$", text, re.S)
    return match.group(1) if match else text


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


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
        prompt = _words(entry.get("prompt"))
        if not prompt:
            raise ScenePlanError(f"scene {number} has no prompt for its picture")
        image = _words(entry.get("image")).lower()
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
        scenes.append({"narration": narration, "prompt": prompt, "photo": photo})

    # 配音和字幕是按分句来的，画面按段来：每一段占几条字幕，加起来必须正好
    # 是整篇的条数，不然画面对不上声音。一段一行就是为了让这一条成立。
    whole = len(utils.split_string_by_punctuations(script_of({"scenes": scenes})))
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
    return {"subject": _words(data.get("subject")), "scenes": scenes}


def script_of(plan: dict) -> str:
    """整篇旁白：一段一行。换行是硬的断句点，所以每一段都自己成句。"""
    return "\n".join(scene["narration"] for scene in plan["scenes"])


def prompts_of(plan: dict) -> List[str]:
    return [scene["prompt"] for scene in plan["scenes"]]


def stored(plan: dict) -> List[dict]:
    """跟着这条片子走的那一份：每段的旁白和照片。提示词另存在关键词里。"""
    return [
        {"narration": scene["narration"], "photo": scene["photo"]}
        for scene in plan["scenes"]
    ]


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
    photos = [at for at, scene in enumerate(scenes, start=1) if scene["photo"]]
    lines = [
        f"{len(scenes)} scenes, about {sum(seconds) / 60.0:.1f} minutes of narration",
        f"{len(photos)} drawn from a photo ({photo_percent(scenes)}%)"
        + (f": scenes {', '.join(str(at) for at in photos)}" if photos else ""),
    ]
    held = [f"{at} ({length:.0f}s)" for at, length in enumerate(seconds, start=1)
            if length > 9.0]
    if held:
        lines.append(
            "one picture is held for over 9 seconds in scenes " + ", ".join(held)
            + " - consider splitting them"
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
