"""编辑器里的"设置"：各家服务商的地址、Key 和模型。

为什么要有这个模块
------------------
这些东西以前只在 WebUI（Streamlit，:8501）的设置对话框里。开片子搬进编辑器
之后，表单底下那个"更多设置"还得把人送回 :8501——两个页面还是两个页面。
这里把同一批配置项搬过来：同样的分区、同样的键、同样写进 config.toml。

三条规矩
--------
1. **白名单。** 只有下面这张表里列了的 ``(分区, 键)`` 能被读写。接口收到表外
   的键直接拒绝——这是一个能写配置文件的 HTTP 接口，不能是"随便写"。
2. **Key 不回传。** 凭据类字段读出来只有"设过没有"，不带值。浏览器里不该
   躺着一份明文 Key；要换就重填，要清就明说清。留空等于不动。
3. **和 WebUI 走同一条保存路径。** 用的是 WebUI 那一对非阻塞函数：有任务在
   跑时配置被锁着，改动先排队，任务结束后生效——而不是让保存按钮卡住。

The provider settings that used to live only in the WebUI dialog. A whitelist
decides what this HTTP surface may read or write, secrets are reported as
"set / not set" and never sent back, and writes go through the same
non-blocking path the WebUI uses, so a change made while a video is running is
queued rather than blocking.
"""

from typing import Any, Mapping, Optional

from loguru import logger

from app.config import config
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID, LLM_PROVIDER_REGISTRY
from app.services import infistar_video, metaso_minimax, voice, volcengine_seedance


class SettingsError(ValueError):
    """某一项设置不能这样改。文案直接给用户看。"""


def _sections() -> dict:
    return {
        "app": config.app,
        "ui": config.ui,
        "azure": config.azure,
        "chatterbox": config.chatterbox,
        "kokoro": config.kokoro,
        "elevenlabs": config.elevenlabs,
        "minimax_tts": config.minimax_tts,
        "siliconflow": config.siliconflow,
        "fish_audio": config.fish_audio,
        "voxcpm": config.voxcpm,
    }


def field(section, key, label, kind="text", **more) -> dict:
    """一项设置。``kind``：text / secret / keys / select / number / bool / textarea。

    ``keys`` 是"可以填好几个、逗号分开"的 Key 列表（素材站那种），存成数组。
    """
    row = {"id": f"{section}.{key}", "section": section, "key": key,
           "label": label, "kind": kind}
    row.update({name: value for name, value in more.items() if value is not None})
    return row


# 用账号登录、不用 Key 的那三家。界面在它们的面板里画登录按钮，
# 见 ``app.services.signin``。
SIGN_IN_PROVIDERS = ("openai_codex", "openai_chatgpt", "kimi_code")

VIDEO_CODECS = (
    ("", "Default"), ("libx264", "libx264 (CPU)"),
    ("h264_nvenc", "NVIDIA NVENC (h264_nvenc)"), ("h264_amf", "AMD AMF (h264_amf)"),
    ("h264_qsv", "Intel QSV (h264_qsv)"),
    ("h264_mf", "Windows MediaFoundation (h264_mf)"),
    ("h264_videotoolbox", "macOS VideoToolbox (h264_videotoolbox)"),
)


def _choices(values, default_label="Default") -> list:
    """下拉框的选项。最前面留一个空的：没设过就是"按默认"，不替用户挑一个。"""
    return [{"id": "", "label": default_label}] + [
        {"id": value, "label": str(value)} for value in values
    ]


def _resolutions(values) -> list:
    return sorted(values, key=lambda value: int("".join(
        ch for ch in str(value) if ch.isdigit()) or 0))


def _llm_panels() -> list:
    panels = []
    for provider in LLM_PROVIDER_REGISTRY:
        fields = []
        if provider.show_api_key:
            fields.append(field("app", provider.config_key("api_key"),
                                "API key", "secret"))
        if provider.show_base_url:
            fields.append(field(
                "app", provider.config_key("base_url"), "Base URL",
                # Ollama 的地址留空时由程序自己找本机那个；把它找到的写在框里
                # 当提示，免得一个空框看起来像"没配置"。
                placeholder=(
                    provider.effective_default_base_url
                    or (config.get_default_ollama_base_url()
                        if provider.provider_id == "ollama" else "")
                ),
            ))
        fields.append(field(
            "app", provider.config_key("model_name"), "Model",
            placeholder=provider.default_model or "",
        ))
        for extra in provider.extra_fields:
            fields.append(field(
                "app", provider.config_key(extra.config_suffix), extra.label_key,
                "secret" if extra.secret else "text",
                placeholder=extra.default_value or "",
            ))
        note = ""
        signs_in = provider.provider_id in SIGN_IN_PROVIDERS
        if signs_in:
            note = "This provider uses your account instead of a key."
        elif not provider.show_api_key:
            note = ("This provider runs on its own (a local server or a "
                    "command-line login), so there is no key to paste here.")
        panel = {
            "id": provider.provider_id,
            "label": provider.default_label or provider.provider_id,
            "note": note,
            "fields": fields,
        }
        if signs_in:
            panel["signin"] = provider.provider_id
        panels.append(panel)
    return panels


def _text_fields(section, rows) -> list:
    return [field(section, key, label, kind, **more)
            for key, label, kind, more in rows]


def groups() -> list:
    """整张设置表：分组 → 面板 → 字段。界面照着画，接口照着校验。"""
    llm_options = [
        {"id": provider.provider_id,
         "label": provider.default_label or provider.provider_id}
        for provider in LLM_PROVIDER_REGISTRY
    ]
    return [
        {
            "id": "llm", "label": "Language model",
            "note": "Writes the script, the picture prompts and the YouTube text.",
            "fields": [field("app", "llm_provider", "Use this provider", "select",
                             options=llm_options, default=DEFAULT_LLM_PROVIDER_ID)],
            # 面板很多（四十来家），界面只展开被选中的那一家。
            "panel_field": "app.llm_provider",
            "panels": _llm_panels(),
        },
        {
            "id": "pictures", "label": "Pictures and video",
            "note": "Where the pictures and clips come from.",
            "fields": [],
            "panels": [
                {"id": "openai_image", "label": "AI pictures (image server)",
                 "fields": _text_fields("app", [
                     ("openai_image_base_url", "Base URL", "text",
                      {"placeholder": "http://127.0.0.1:8188/v1"}),
                     ("openai_image_api_keys", "API key(s)", "keys", {}),
                     ("openai_image_model", "Model", "text", {}),
                     ("openai_image_size", "Image size", "text",
                      {"placeholder": "auto"}),
                     ("openai_image_prompt_template", "Prompt template", "textarea",
                      {"help": "{term} is replaced by each picture's prompt. "
                               "Picking a picture style writes this for you."}),
                 ])},
                {"id": "codex_image", "label": "AI pictures (Codex)",
                 # 画图和写字用的是同一个 ChatGPT 账号。
                 "signin": "openai_codex",
                 "fields": _text_fields("app", [
                     ("codex_image_model_name", "Model", "text", {}),
                     ("codex_image_timeout", "Timeout (seconds)", "number",
                      {"min": 30, "max": 3600}),
                 ])},
                {"id": "pexels", "label": "Pexels",
                 "fields": [field("app", "pexels_api_keys", "API key(s)", "keys")]},
                {"id": "pixabay", "label": "Pixabay",
                 "fields": [field("app", "pixabay_api_keys", "API key(s)", "keys")]},
                {"id": "coverr", "label": "Coverr",
                 "fields": [field("app", "coverr_api_keys", "API key(s)", "keys")]},
                {"id": "wavespeed", "label": "WaveSpeed AI video (paid)",
                 "fields": [field("app", "wavespeed_api_keys", "API key(s)", "keys")]},
                {"id": "volcengine_seedance",
                 "label": "Volcano Engine Seedance (paid)",
                 "fields": _text_fields("app", [
                     ("volcengine_seedance_api_key", "Ark API key", "secret", {}),
                     ("volcengine_seedance_model", "Model", "text", {}),
                     ("volcengine_seedance_base_url", "Base URL", "text", {}),
                 ])},
                {"id": "infistar", "label": "Infistar AI video (paid)",
                 "fields": _text_fields("app", [
                     ("infistar_video_api_key", "API key", "secret", {}),
                     ("infistar_video_base_url", "Base URL", "text", {}),
                     ("infistar_video_model", "Model", "select",
                      {"options": _choices(infistar_video.MODELS)}),
                 ])},
                {"id": "jalapeno", "label": "Jalapeno Cloud AI video (paid)",
                 "fields": _text_fields("app", [
                     ("jalapeno_video_api_key", "API key", "secret", {}),
                     ("jalapeno_video_model", "Model", "text", {}),
                     ("jalapeno_video_base_url", "Base URL", "text", {}),
                     ("jalapeno_video_resolution", "Resolution", "select",
                      {"options": _choices(_resolutions(
                          volcengine_seedance.SUPPORTED_RESOLUTIONS))}),
                 ])},
                {"id": "ofox", "label": "OFox AI video (paid)",
                 "fields": _text_fields("app", [
                     ("ofox_api_key", "API key", "secret", {}),
                     ("ofox_text_to_video_model", "Text-to-video model", "text", {}),
                     ("ofox_base_url", "Base URL", "text", {}),
                     ("ofox_provider", "Upstream vendor", "select",
                      {"options": [{"id": "", "label": "Let OFox choose"},
                                   {"id": "byteplus", "label": "BytePlus"},
                                   {"id": "volcengine", "label": "Volcengine"}]}),
                 ])},
                {"id": "metaso_minimax", "label": "Metaso MiniMax H3 (paid)",
                 "fields": _text_fields("app", [
                     ("metaso_minimax_api_key", "API key", "secret", {}),
                     ("metaso_minimax_base_url", "Base URL", "text", {}),
                     ("metaso_minimax_resolution", "Resolution", "select",
                      {"options": _choices(_resolutions(
                          metaso_minimax.SUPPORTED_RESOLUTIONS))}),
                 ])},
                {"id": "muapi", "label": "MuAPI AI video (paid)",
                 "fields": _text_fields("app", [
                     ("muapi_api_key", "API key", "secret", {}),
                     ("muapi_base_url", "Base URL", "text", {}),
                     ("muapi_video_endpoint", "Video endpoint", "text", {}),
                     ("muapi_resolution", "Resolution", "text", {}),
                 ])},
                {"id": "loomloom", "label": "Shengsuan Cloud AI video (paid)",
                 "fields": [field("app", "loomloom_api_token", "API key", "secret")]},
            ],
        },
        {
            "id": "voices", "label": "Voices",
            "note": "The services that read the narration.",
            "fields": [],
            "panels": [
                {"id": "kokoro", "label": "Kokoro (local)",
                 "fields": _text_fields("kokoro", [
                     ("base_url", "Base URL", "text",
                      {"placeholder": "http://127.0.0.1:8880/v1"}),
                     ("api_key", "API key", "secret", {}),
                     ("model_id", "Model", "text", {"placeholder": "kokoro"}),
                     ("voices", "Voices", "text",
                      {"help": "Optional, comma separated. Leave empty to ask "
                               "the server for its list."}),
                 ])},
                {"id": "azure", "label": "Azure TTS V2",
                 "fields": _text_fields("azure", [
                     ("speech_region", "Speech region", "text", {}),
                     ("speech_key", "Speech key", "secret", {}),
                 ])},
                {"id": "siliconflow", "label": "SiliconFlow",
                 "fields": [field("siliconflow", "api_key", "API key", "secret")]},
                {"id": "gemini", "label": "Google Gemini TTS",
                 "fields": [field("app", "gemini_api_key", "API key", "secret")]},
                {"id": "mimo", "label": "Xiaomi MiMo TTS",
                 "fields": [field("app", "mimo_api_key", "API key", "secret")]},
                {"id": "minimax_tts", "label": "MiniMax TTS",
                 "fields": _text_fields("minimax_tts", [
                     ("api_key", "API key", "secret", {}),
                     ("base_url", "Base URL", "text", {}),
                     ("model_id", "Model", "select",
                      {"options": _choices(voice.MINIMAX_TTS_MODELS)}),
                     ("voice_id", "Voice id", "text", {}),
                 ])},
                {"id": "elevenlabs", "label": "ElevenLabs (voice and music)",
                 "fields": _text_fields("elevenlabs", [
                     ("api_key", "API key", "secret", {}),
                     ("model_id", "Voice model", "select",
                      {"options": _choices(("eleven_multilingual_v2",
                                            "eleven_flash_v2_5", "eleven_v3"))}),
                 ])},
                {"id": "chatterbox", "label": "Chatterbox",
                 "fields": _text_fields("chatterbox", [
                     ("base_url", "Base URL", "text", {}),
                     ("api_key", "API key", "secret", {}),
                     ("model_id", "Model", "text", {}),
                     ("voices", "Voices", "text",
                      {"help": "Comma separated."}),
                 ])},
                {"id": "fish_audio", "label": "Fish Audio",
                 "fields": _text_fields("fish_audio", [
                     ("api_key", "API key", "secret", {}),
                     ("model", "Model", "select",
                      {"options": _choices(voice.FISH_AUDIO_MODELS)}),
                 ])},
                {"id": "voxcpm", "label": "VoxCPM",
                 "fields": _text_fields("voxcpm", [
                     ("api_key", "API key", "secret", {}),
                     ("model_id", "Model id", "text", {}),
                     ("base_url", "Base URL", "text", {}),
                 ])},
            ],
        },
        {
            "id": "music", "label": "Generated music",
            "note": "Only needed when the music is composed for the video. "
                    "ElevenLabs music uses the ElevenLabs key under Voices.",
            "fields": [field("app", "sonilo_api_key", "Sonilo API key", "secret")],
            "panels": [],
        },
        {
            "id": "render", "label": "Rendering",
            "note": "How the final video is put together on this machine.",
            "fields": [
                field("app", "video_codec", "Video encoder", "select",
                      options=[{"id": value, "label": label}
                               for value, label in VIDEO_CODECS]),
                field("app", "material_concurrency", "Stock downloads at once",
                      "select", options=_numbers(1, 2, 4, 6, 8)),
                field("app", "video_clip_concurrency", "Clips rendered at once",
                      "select", options=_numbers(1, 2, 4, 6, 8)),
                field("ui", "review_prompts_before_images",
                      "Show me the picture prompts before drawing", "bool",
                      default=True),
                field("ui", "open_task_folder_on_completion",
                      "Open the folder when a video finishes", "bool",
                      default=True),
            ],
            "panels": [],
        },
        {
            "id": "publish", "label": "Publishing (Upload-Post)",
            "note": "Posting finished videos to social accounts.",
            "fields": [
                field("app", "upload_post_enabled", "Enable Upload-Post", "bool"),
                field("app", "upload_post_auto_upload",
                      "Post automatically when a video finishes", "bool"),
                field("app", "upload_post_api_key", "API key", "secret"),
                field("app", "upload_post_username", "Profile name", "text"),
                field("app", "upload_post_platforms", "Platforms", "keys",
                      help="Comma separated, for example: youtube, tiktok.",
                      plain=True),
                field("app", "upload_post_youtube_privacy_status",
                      "YouTube privacy", "select",
                      options=[{"id": value, "label": value.capitalize()}
                               for value in ("public", "unlisted", "private")]),
                field("app", "upload_post_youtube_made_for_kids",
                      "YouTube: made for kids", "bool"),
            ],
            "panels": [],
        },
        {
            # 这一组不是填空：是几个动作（备份、清理），界面单独画。
            # 见 ``app.services.upkeep``。
            "id": "upkeep", "label": "Backup and upkeep", "tools": True,
            "note": "Move your keys and settings to another computer, free "
                    "up disk space, and turn on more detailed logs.",
            "fields": [], "panels": [],
        },
    ]


def _numbers(*values) -> list:
    return [{"id": value, "label": str(value)} for value in values]


def _all_fields(tree: Optional[list] = None) -> dict:
    found = {}
    for group in tree if tree is not None else groups():
        for row in group.get("fields", []):
            found[row["id"]] = row
        for panel in group.get("panels", []):
            for row in panel.get("fields", []):
                found[row["id"]] = row
    return found


def _is_secret(row: Mapping) -> bool:
    return row["kind"] == "secret" or (row["kind"] == "keys" and not row.get("plain"))


def _shown(row: Mapping, value: Any) -> Any:
    """这一项给界面看的样子。凭据只说"设过没有"。"""
    if _is_secret(row):
        if isinstance(value, (list, tuple)):
            count = len([one for one in value if str(one).strip()])
        else:
            count = 1 if str(value or "").strip() else 0
        return {"set": count > 0, "count": count}
    if row["kind"] == "keys":
        if isinstance(value, str):
            return value
        return ", ".join(str(one) for one in (value or []))
    if row["kind"] == "bool":
        return bool(row.get("default", False)) if value is None else bool(value)
    if value is None:
        return row.get("default", "")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(one) for one in value)
    return value


def describe() -> dict:
    """设置表连同每一项现在的值（凭据不带值）。"""
    tree = groups()
    sections = _sections()
    values = {}
    for row in _all_fields(tree).values():
        snapshot = config.snapshot_config_with_pending(sections[row["section"]])
        values[row["id"]] = _shown(row, snapshot.get(row["key"]))
    return {"groups": tree, "values": values}


def _coerce(row: Mapping, value: Any) -> Any:
    kind = row["kind"]
    label = row["label"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{label}: expected on or off")
        return value
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{label}: expected a number") from exc
        low, high = row.get("min"), row.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise SettingsError(f"{label}: must be between {low} and {high}")
        return int(number) if number == int(number) else number
    if kind == "select":
        allowed = {option["id"]: option["id"] for option in row.get("options", [])}
        by_text = {str(key): key for key in allowed}
        if str(value) not in by_text:
            raise SettingsError(
                f"{label}: {value!r} is not one of the choices"
            )
        return by_text[str(value)]
    if kind == "keys":
        text = value if isinstance(value, str) else ",".join(map(str, value or []))
        return [one for one in text.replace(" ", "").split(",") if one]
    text = str(value if value is not None else "")
    if len(text) > 8000:
        raise SettingsError(f"{label}: too long")
    return text.strip() if kind != "textarea" else text.strip("\r\n")


def apply(changes: Mapping, clear: Optional[list] = None) -> dict:
    """保存改动。``changes`` 是 ``{"分区.键": 新值}``，``clear`` 是要清空的凭据。

    凭据留空等于不动：界面上那个框本来就是空的（值不回传），把"没填"当成
    "清空"会让用户每保存一次别的东西就丢一把 Key。
    """
    known = _all_fields()
    sections = _sections()
    planned: dict = {}

    for field_id in clear or []:
        row = known.get(field_id)
        if row is None:
            raise SettingsError(f"unknown setting {field_id!r}")
        if not _is_secret(row):
            raise SettingsError(f"{row['label']}: only a key can be cleared this way")
        planned[field_id] = [] if row["kind"] == "keys" else ""

    for field_id, value in dict(changes or {}).items():
        row = known.get(field_id)
        if row is None:
            raise SettingsError(f"unknown setting {field_id!r}")
        if _is_secret(row):
            blank = (not value) if not isinstance(value, str) else not value.strip()
            if blank:
                continue
        planned[field_id] = _coerce(row, value)

    deferred = False
    by_section: dict = {}
    for field_id, value in planned.items():
        row = known[field_id]
        by_section.setdefault(row["section"], {})[row["key"]] = value
    for section, values in by_section.items():
        applied = config.update_config_values_nonblocking(sections[section], values)
        deferred = deferred or not applied
    if planned:
        saved = config.try_save_config()
        deferred = deferred or not saved
        logger.info(
            f"settings changed from the editor: {sorted(planned)}"
            + (" (queued until the running video finishes)" if deferred else "")
        )
    result = describe()
    result["changed"] = sorted(planned)
    result["deferred"] = deferred
    return result
