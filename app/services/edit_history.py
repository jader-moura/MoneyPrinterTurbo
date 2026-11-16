"""撤销和重做：时间线上每改一次，记下改之前的样子。

为什么要有这个模块
------------------
编辑器里的每一次改动都是直接落盘的：拖一下、删一段、换一张图，清单当场
就改了。这很省事，但也意味着手一滑就没有回头路——删错一段，只能重新出一
张图把它补回来。剪辑软件里人人都靠 Ctrl+Z 干活，没有它，每一次点击都得先
想清楚。

怎么记
------
所有改场景的操作最后都走 ``scene_manifest.save_scenes``。所以不去改二十个
调用方，只在那一个口子上记：写之前看一眼现在的清单，和要写的不一样，就把
现在的存进撤销栈。这样新加的编辑操作自动就能撤销，不会漏。

记的是场景列表（不含每段的"换过哪些图"那份历史——那份很大，而且撤销不该
把它也倒回去）、脚本原文和"配音过期"标记。素材文件本身从不删除，所以
倒回去的清单指向的图一定还在盘上。

不管什么
--------
字幕样式、配乐、整条片子的转场——那些存在任务参数里，不在场景列表里，这里
不记。撤销只管时间线。

Undo and redo for the timeline. Every scene edit ends in
``scene_manifest.save_scenes``, so the history is recorded at that one point
rather than at each caller: if what is about to be written differs from what
is on disk, the current state goes on the undo stack. Material files are never
deleted, so a restored scene list always points at files that still exist.
Settings stored in the task parameters (subtitle style, music) are out of
scope.
"""

import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from loguru import logger

from app.utils import utils

LIMIT = 40
FILE_NAME = "edit-history.json"
# 每段场景里不参与比较、也不存进快照的键。``index`` 永远等于位置；``history``
# 是"这一段换过哪些图"，撤销一次删除不该让它也倒退。
_IGNORED = ("index", "history")

_local = threading.local()
_write_lock = threading.Lock()


def _path(task_id: str) -> str:
    # 不用 ``utils.task_dir(task_id)``：它会顺手把目录建出来，而一个不存在的
    # 任务不该因为有人问了一句"能撤销吗"就多出一个空目录。
    return os.path.join(utils.task_dir(), str(task_id), FILE_NAME)


def _read(task_id: str) -> dict:
    try:
        with open(_path(task_id), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "undo": [one for one in data.get("undo", []) if isinstance(one, dict)],
        "redo": [one for one in data.get("redo", []) if isinstance(one, dict)],
    }


def _write(task_id: str, data: dict) -> None:
    path = _path(task_id)
    if not os.path.isdir(os.path.dirname(path)):
        return
    temp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with _write_lock:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(temp, path)
    except OSError as exc:
        # 记不下来不该挡住编辑本身：最坏是这一步没法撤销。
        logger.warning(f"could not record the edit history of {task_id}: {exc}")
        try:
            os.remove(temp)
        except OSError:
            pass


def _plain(scenes: Any) -> list:
    return [
        {key: value for key, value in scene.items() if key not in _IGNORED}
        for scene in (scenes or [])
        if isinstance(scene, dict)
    ]


def _audio_mark(payload: dict) -> str:
    """现在这份配音是哪一份。重新配过音之后它会变。"""
    path = str(payload.get("audio_file") or "")
    try:
        return f"{os.path.basename(path)}:{os.path.getmtime(path):.3f}"
    except OSError:
        return os.path.basename(path)


def snapshot(payload: dict, scenes: list, label: str = "") -> dict:
    return {
        "label": label,
        "at": round(time.time(), 3),
        "scenes": _plain(scenes),
        "script": str(payload.get("script") or ""),
        "audio_stale": bool(payload.get("audio_stale")),
        "audio": _audio_mark(payload),
    }


def describe_change(before: list, after: list) -> str:
    """这一步改了什么。给撤销按钮的提示用："撤销：删除场景"。"""
    if len(after) < len(before):
        return "Delete scene"
    if len(after) > len(before):
        return "Add scene"
    ids_before = [scene.get("id") for scene in before]
    ids_after = [scene.get("id") for scene in after]
    if ids_before != ids_after and sorted(map(str, ids_before)) == sorted(
        map(str, ids_after)
    ):
        return "Move scene"
    changed: set = set()
    count = 0
    for old, new in zip(before, after):
        keys = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
        if keys:
            count += 1
            changed |= keys
    if count > 1:
        return "Edit scenes"
    for keys, label in (
        (("material", "kind"), "Change picture"),
        (("trim_start", "duration"), "Trim scene"),
        (("narration", "narration_edited"), "Edit narration"),
        (("keyword",), "Edit prompt"),
        (("transition", "motion"), "Change effect"),
    ):
        if changed & set(keys):
            return label
    return "Edit scene"


def _only_first_pictures(old: list, new: list) -> bool:
    """这次写入是不是只有"还没画的那几段拿到了第一张图"。

    那不是用户的一次编辑，是出图在往前走：一条两百段的片子开画之后会写
    两百次。把每一次都记成一步，撤销栈里就全是"换图"，真正的编辑被挤出去；
    而撤销它的意思是"把刚画好的图再变回没画"——没人想要这个。
    """
    if len(old) != len(new):
        return False
    for before, after in zip(old, new):
        if before == after:
            continue
        if before.get("id") != after.get("id"):
            return False
        if str(before.get("material") or "").strip():
            return False
        if not str(after.get("material") or "").strip():
            return False
    return True


@contextmanager
def paused() -> Iterator[None]:
    """这一段里的保存不记历史。撤销自己写回清单时用，否则撤销会把自己记成
    一次新的改动。"""
    before = getattr(_local, "paused", False)
    _local.paused = True
    try:
        yield
    finally:
        _local.paused = before


@contextmanager
def automatic() -> Iterator[None]:
    """这一段里的保存不是用户的编辑，是程序自己在补东西（比如把旁白按时间
    配回各段）。不记成一步，也不作废"重做"；补上的内容带进已有的每一份快照
    （见 ``_carry``）。

    没有这个的时候：撤销一步 → 界面重新读清单 → 读的时候顺手把旁白补回去
    → 这次保存被当成一次新的编辑 → "重做"被清空。用户撤销了就再也回不来。
    """
    before = getattr(_local, "automatic", False)
    _local.automatic = True
    try:
        yield
    finally:
        _local.automatic = before


def _carry(task_id: str, old: list, new: list) -> None:
    """程序自己补上的东西（第一张图、配回去的旁白）带进每一份快照。

    这些写入不记成一步。但快照是在它们之前拍的，里面还是"没画"的样子——
    于是撤销更早的一步，会把后来画好的图一起抹掉，而"重做"里也没有它们。
    所以补的时候把同一段（按 id）的同一个字段在快照里也补上；快照里那个字段
    已经是别的值（用户改过）的就不动。
    """
    changes: dict = {}
    for before, after in zip(old, new):
        if before == after or before.get("id") != after.get("id"):
            continue
        keys = {
            key for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        }
        changes[after.get("id")] = [
            (key, before.get(key), after.get(key)) for key in keys
        ]
    if not changes:
        return
    data = _read(task_id)
    touched = False
    for taken in data["undo"] + data["redo"]:
        for scene in taken.get("scenes") or []:
            for key, was, now in changes.get(scene.get("id"), ()):
                if scene.get(key) == was and was != now:
                    if now is None:
                        scene.pop(key, None)
                    else:
                        scene[key] = now
                    touched = True
    if touched:
        _write(task_id, data)


@contextmanager
def labelled(label: str) -> Iterator[None]:
    """给这一段里的改动起个名字。分割、复制这种从清单的前后对比里看不出来
    是什么的操作用它。"""
    before = getattr(_local, "label", "")
    _local.label = label
    try:
        yield
    finally:
        _local.label = before


def note(task_id: str, payload: dict, before: list, after: list) -> None:
    """清单要被写了。和现在不一样的话，把现在的存进撤销栈。"""
    if getattr(_local, "paused", False):
        return
    # 清单里还没有场景：这是流水线第一次把它们写进来，没有"之前"可以撤回。
    if not before:
        return
    old, new = _plain(before), _plain(after)
    if old == new:
        return
    if getattr(_local, "automatic", False) or _only_first_pictures(old, new):
        if len(old) == len(new):
            _carry(task_id, old, new)
        return
    label = getattr(_local, "label", "") or describe_change(old, new)
    data = _read(task_id)
    data["undo"].append(snapshot(payload, before, label))
    data["undo"] = data["undo"][-LIMIT:]
    # 改了新的东西，"重做"那一支就作废了。
    data["redo"] = []
    _write(task_id, data)


def state(task_id: str) -> dict:
    """现在能撤销几步、能重做几步，以及各自最近的那一步叫什么。"""
    data = _read(task_id)
    return {
        "undo": len(data["undo"]),
        "redo": len(data["redo"]),
        "undo_label": data["undo"][-1].get("label", "") if data["undo"] else "",
        "redo_label": data["redo"][-1].get("label", "") if data["redo"] else "",
    }


def step(task_id: str, direction: str, payload: dict, scenes: list) -> Optional[dict]:
    """往回（或往前）走一步：取出要恢复的那份，把现在的放到另一边。

    没有可走的就返回 ``None``，什么都不动。
    """
    source, target = ("undo", "redo") if direction == "undo" else ("redo", "undo")
    data = _read(task_id)
    if not data[source]:
        return None
    wanted = data[source].pop()
    data[target].append(snapshot(payload, scenes, wanted.get("label", "")))
    data[target] = data[target][-LIMIT:]
    _write(task_id, data)
    return wanted


def forget(task_id: str) -> None:
    """清掉历史。整条片子重新生成之后，旧的步骤指向的是另一条片子。"""
    try:
        os.remove(_path(task_id))
    except OSError:
        pass
