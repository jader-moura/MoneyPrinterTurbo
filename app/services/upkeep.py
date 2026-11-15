"""备份和维护：密钥备份、设置预设、素材缓存清理、诊断日志。

这四样以前只在 WebUI 的设置对话框里。它们和"开片子"无关，但少了它们，
换一台机器、盘满了、要查问题的时候就还得回 :8501 去——而整件事的目的是
只留一个端口。

文件格式和 WebUI 的完全一样（同一个 schema 名、同一个版本号），所以两边导
出的文件可以互相导入：在 WebUI 里备份过密钥的人不用重新备份一遍。

关于密钥备份
------------
设置页的规矩是"Key 不回传"。备份是唯一的例外，而且是用户点了"下载备份"
才发生的例外——它的全部用途就是把密钥拿出去。所以它不走 GET（GET 会被
浏览器预取、会留在历史记录里），而且要请求体里明说 ``confirm``。

Key backup, settings presets, material cache clean-up and diagnostic logging,
moved over from the WebUI dialog. The file formats are identical to the
WebUI's, so files exported on either side import on the other. The key backup
is the one deliberate exception to "keys are never sent back": it only happens
on an explicit, confirmed request, and never over GET.
"""

import json
import os
from typing import Any, Mapping, Optional

from loguru import logger

from app.config import config
from app.models.llm_provider import LLM_PROVIDER_REGISTRY
from app.models.schema import VideoParams
from app.services import bgm, cache_manager
from app.utils.logging_utils import LOG_LEVELS, get_logging_options


class UpkeepError(ValueError):
    """这一步做不了。文案直接给用户看。"""


SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
MAX_TRANSFER_BYTES = 2 * 1024 * 1024

# 预设只描述生成参数。素材、配音和配乐是本机文件路径，换一台机器就指向
# 不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {"video_materials", "custom_audio_file", "bgm_file"}
)
# 密钥按配置项名称的后缀识别：新加一家服务商只要沿用现有命名，就自动进备份。
CREDENTIAL_KEY_SUFFIXES = (
    "api_key", "api_keys", "api_token", "access_key", "secret_key", "speech_key",
)
# 只恢复密钥、不恢复它的配套项，那把密钥仍然用不了。
CREDENTIAL_COMPANION_KEYS = {
    "azure": ("speech_region",),
    "app": tuple(
        provider.config_key(extra.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for extra in provider.extra_fields
    ) + ("upload_post_username",),
}
CACHE_RANGES = (30, 7, 90, None)


def _sections() -> dict:
    """能备份的分区。``ui`` 只有界面偏好，没有凭据，不在里面。"""
    return {
        "app": config.app,
        "azure": config.azure,
        "chatterbox": config.chatterbox,
        "kokoro": config.kokoro,
        "elevenlabs": config.elevenlabs,
        "minimax_tts": config.minimax_tts,
        "siliconflow": config.siliconflow,
        "fish_audio": config.fish_audio,
        "voxcpm": config.voxcpm,
    }


# ---------------------------------------------------------------------------
# 密钥备份
# ---------------------------------------------------------------------------

def _is_backup_key(section: str, key: str) -> bool:
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES) or (
        key in CREDENTIAL_COMPANION_KEYS.get(section, ())
    )


def _backup_value(value: Any):
    if isinstance(value, list):
        items = [str(item).strip() for item in value
                 if isinstance(item, (str, int, float)) and str(item).strip()]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def key_backup() -> dict:
    """现在存着的每一把密钥，连同它们的配套项。"""
    keys: dict = {}
    for name, section in _sections().items():
        entries = {}
        # 连同排着队还没落盘的改动一起备份：刚在设置里填的 Key 也得在里面。
        for key, value in config.snapshot_config_with_pending(section).items():
            if not _is_backup_key(name, key):
                continue
            kept = _backup_value(value)
            if kept is not None:
                entries[key] = kept
        if entries:
            keys[name] = entries
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(config.project_version),
        "keys": keys,
    }


def _load(raw: bytes, schema: str, version: int, what: str) -> dict:
    """解析一个导出文件，并确认它确实是这个功能、这个版本导出的。

    用户可能传上来任何 JSON。只认声明了正确 schema 和版本的，让错误停在
    导入这一步，而不是把认不出的东西写进配置。
    """
    if len(raw) > MAX_TRANSFER_BYTES:
        raise UpkeepError(f"that {what} file is larger than 2 MB")
    try:
        # Windows 的编辑器会存带 BOM 的 JSON。
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpkeepError(f"that file is not a {what} file: it is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise UpkeepError(f"that file is not a {what} file")
    if payload.get("version") != version:
        raise UpkeepError(
            f"that {what} file is version {payload.get('version')!r}, "
            f"and this app reads version {version}"
        )
    return payload


def restore_keys(raw: bytes) -> dict:
    """从备份文件恢复密钥。只认当前认识的分区和密钥项。

    备份文件可以手工改，也可能来自更新的版本。表外的分区、不是密钥的项一律
    忽略——否则"恢复密钥"就成了一个能改任何配置的后门。
    """
    payload = _load(raw, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION, "key backup")
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise UpkeepError("that key backup has no keys in it")
    sections = _sections()
    restored: dict = {}
    for name, entries in keys.items():
        if name not in sections or not isinstance(entries, dict):
            continue
        kept = {}
        for key, value in entries.items():
            if not _is_backup_key(name, str(key)):
                continue
            value = _backup_value(value)
            if value is not None:
                kept[str(key)] = value
        if kept:
            restored[name] = kept
    if not restored:
        raise UpkeepError("that key backup has no keys this app can use")

    deferred = False
    for name, entries in restored.items():
        applied = config.update_config_values_nonblocking(sections[name], entries)
        deferred = deferred or not applied
    saved = config.try_save_config()
    count = sum(len(entries) for entries in restored.values())
    logger.info(f"restored {count} key(s) from a backup file")
    return {"restored": count, "deferred": deferred or not saved}


# ---------------------------------------------------------------------------
# 设置预设
# ---------------------------------------------------------------------------

def settings_preset() -> dict:
    """"上次用的"那一套生成设置，导成一个能带到别的机器上的文件。"""
    from app.services import new_video

    saved = dict(new_video.saved_settings())
    music = new_video._saved_music(config.ui)
    saved["bgm_type"] = music["music_type"]
    if music["music_type"] == "preset" and music["music_track"]:
        saved["bgm_file"] = music["music_track"]
    if music["music_type"] in new_video.GENERATED_MUSIC:
        saved["video_music_prompt"] = music["music_prompt"]
    aspect = new_video._saved_aspects(config.ui).get(saved.get("video_source", ""))
    if aspect:
        saved["video_aspect"] = aspect
    params = VideoParams.model_validate({"video_subject": "", **saved})
    dump = params.model_dump(mode="json")
    preset = {key: value for key, value in dump.items()
              if key not in PRESET_EXCLUDED_PARAM_KEYS}
    if dump.get("bgm_type") == "preset" and dump.get("bgm_file"):
        # 内置曲库每台机器都有，所以这一首可以带着走——只带文件名。
        preset["bgm_file"] = os.path.basename(str(dump["bgm_file"]))
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(config.project_version),
        "params": preset,
    }


def apply_preset(raw: bytes) -> dict:
    """导入一份预设，让它成为"上次用的"——下一次开片子的表单就从它开始。"""
    from pydantic import ValidationError

    from app.services import new_video

    payload = _load(raw, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION,
                    "settings preset")
    given = payload.get("params")
    if not isinstance(given, dict):
        raise UpkeepError("that settings preset has no settings in it")
    wanted = {key: value for key, value in given.items()
              if key not in PRESET_EXCLUDED_PARAM_KEYS}
    if given.get("bgm_type") == "preset" and given.get("bgm_file"):
        try:
            path = bgm.resolve_builtin_bgm_file(str(given["bgm_file"]))
        except ValueError as exc:
            raise UpkeepError(
                f"the preset uses the track {given['bgm_file']!r}, which this "
                f"computer does not have"
            ) from exc
        wanted["bgm_file"] = os.path.basename(path)
    wanted.setdefault("video_subject", "")
    try:
        # 预设可能来自别的机器，也可能被手工改过。走模型校验，非法的在导入
        # 时就拒掉，而不是等到开片子才炸。
        params = VideoParams.model_validate(wanted)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "a setting"
        raise UpkeepError(
            f"that settings preset is not valid: {where} {first.get('msg', '')}"
        ) from exc
    new_video.remember({}, params)
    return {"applied": True}


# ---------------------------------------------------------------------------
# 素材缓存
# ---------------------------------------------------------------------------

def _range(days: Optional[int]) -> Optional[int]:
    if days not in CACHE_RANGES:
        raise UpkeepError("choose 7, 30 or 90 days, or everything")
    return days


def cache_stats(days: Optional[int] = None) -> dict:
    """素材缓存有多大，以及按这个范围清会清掉多少。只看，不删。"""
    total = cache_manager.get_video_cache_stats()
    would = cache_manager.get_video_cache_stats(max_age_days=_range(days))
    return {
        "folder": cache_manager.video_cache_dir(),
        "files": total.file_count,
        "bytes": total.total_size,
        "oldest": total.oldest_mtime,
        "range": days,
        "ranges": list(CACHE_RANGES),
        "would_delete": {"files": would.file_count, "bytes": would.total_size},
    }


def clean_cache(days: Optional[int], confirm: bool) -> dict:
    """清素材缓存。删了就没了，所以要请求里明说 ``confirm``。

    清掉的是从素材站下载、可以重新下载的片段——不是任何一条片子自己的文件。
    """
    if confirm is not True:
        raise UpkeepError("confirm that these files may be deleted first")
    result = cache_manager.clean_video_cache(max_age_days=_range(days))
    logger.info(
        f"material cache cleaned from the editor: {result.deleted_count} file(s), "
        f"{result.deleted_size} byte(s), {result.failed_count} failed"
    )
    data = cache_stats(days)
    data["deleted"] = {
        "files": result.deleted_count, "bytes": result.deleted_size,
        "failed": result.failed_count,
    }
    return data


# ---------------------------------------------------------------------------
# 诊断日志
# ---------------------------------------------------------------------------

def logging_options() -> dict:
    level, requests = get_logging_options()
    return {"level": level, "log_requests": bool(requests),
            "levels": list(LOG_LEVELS)}


def set_logging(level: str, log_requests: bool) -> dict:
    """改日志级别。立刻生效，不打断正在跑的片子。"""
    wanted = str(level or "").strip().upper()
    if wanted not in LOG_LEVELS:
        raise UpkeepError(f"unknown log level {level!r}")
    if not isinstance(log_requests, bool):
        raise UpkeepError("tracing requests is either on or off")
    config.update_logging_options(wanted, log_requests)
    config.try_save_config()
    return logging_options()


def describe() -> dict:
    return {"cache": cache_stats(30), "logging": logging_options()}
