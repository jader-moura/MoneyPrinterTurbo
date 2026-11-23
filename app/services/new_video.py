"""在编辑器里开一条新片子。

为什么要有这个模块
------------------
以前一条片子只能从 WebUI（Streamlit，:8501）开始：填主题、挑音色、点生成，
然后才跳到编辑器。两个页面、两套界面，而用户想要的是像 CapCut 那样——
一个地方，开始和修改都在里面。

所以这里把 WebUI "点生成之前"能设的东西都搬过来：脚本、画面来源（含付费
视频源和本地素材）、配音（每一家 TTS、上传配音、不要配音）、字幕样式、
配乐和转场音。表单长什么样由 ``form()`` 说了算，界面照着画——能挑什么只有
服务端知道，页面里不写死第二份。

四条规矩
--------
1. **默认值就是上次用的。** 读的是 WebUI 一直在存的那批 ``[ui]`` 键；点了
   创建之后也照 WebUI 的做法把这次的选择存回去，两个入口看到的是同一份。
2. **不替用户换服务商。** 表单没列的来源点名拒绝；音色原样交给流水线，
   哪家失败就是哪家的错误。
3. **花钱的事要点头。** 按次计费的视频源必须勾上确认才开——WebUI 上那个
   勾选框挡的就是这件事，搬过来不能丢。
4. **永远停下来等人看。** 从编辑器开的片子不会一路跑到成片：文生图停在
   字幕（先看提示词再开画），其余停在素材。成片由编辑器里的"渲染"决定。

Starting a video from inside the editor, with everything the WebUI offered
before its Generate button. The form is described by the server and drawn by
the page. Defaults are the last-used choices and are remembered again on
create, a source the form does not offer is refused by name, a per-video
charge has to be confirmed, and the run always stops for review.
"""

import os
import shutil
import uuid
from typing import Any, BinaryIO, Mapping, Optional

from loguru import logger
from pydantic import ValidationError

from app.config import config
from app.models import image_style as image_style_model
from app.models.llm_provider import LLM_PROVIDER_REGISTRY
from app.models.schema import MaterialInfo, TaskVideoRequest, VideoAspect
from app.services import bgm, finishing, local_defaults, material, scene_plan, transition_sound, voice
from app.services import task as tm
from app.utils import utils


class NewVideoError(ValueError):
    """表单里的某一项不能用。文案直接给用户看，所以要点名是哪一项。"""


# 和 webui/Main.py 的 ``support_locales`` 同一张表。那个文件一导入就会启动
# Streamlit，所以没法直接引用；``test_new_video`` 里有一条测试盯着两边一致。
LANGUAGES = (
    "ca-ES", "zh-CN", "zh-HK", "zh-TW", "de-DE", "en-US", "es-ES",
    "fr-FR", "id-ID", "it-IT", "ru-RU", "vi-VN", "th-TH", "tr-TR",
)

# 画面来源，和 WebUI 的分组、顺序一致。``paid`` 是按次计费的那些。
#
# 唯一没列的是 loomloom（胜算云）：它要先询价、拿到报价单再确认扣费，那一套
# 流程还没搬过来。硬列出来只会得到一条跑到一半才失败的任务，所以点名拒绝。
SOURCES = (
    ("openai_image", "AI pictures (image server)", "AI pictures", False),
    ("codex_image", "AI pictures (Codex)", "AI pictures", False),
    ("pexels", "Pexels", "Stock video", False),
    ("pixabay", "Pixabay", "Stock video", False),
    ("coverr", "Coverr", "Stock video", False),
    ("metaso_minimax", "Metaso MiniMax H3", "AI video (paid)", True),
    ("ofox", "OFox AI video", "AI video (paid)", True),
    ("volcengine_seedance", "Volcano Engine Seedance", "AI video (paid)", True),
    ("infistar", "Infistar AI video", "AI video (paid)", True),
    ("jalapeno", "Jalapeno Cloud AI video", "AI video (paid)", True),
    ("wavespeed", "WaveSpeed AI video", "AI video (paid)", True),
    ("muapi", "MuAPI AI video", "AI video (paid)", True),
    ("local", "My own files", "My own files", False),
)
SOURCE_IDS = tuple(row[0] for row in SOURCES)
PAID_SOURCES = tuple(row[0] for row in SOURCES if row[3])
STOCK_SOURCES = ("pexels", "pixabay", "coverr")
IMAGE_SOURCES = tuple(image_style_model.IMAGE_STYLE_SOURCES)

#: 表单里每种画风配几张样图：``styles/<id>.jpg``、``<id>-2.jpg``、……
#: 每一张都是同一个画面按各个画风各画一遍，所以卡片之间比的只有画风。
STYLE_SAMPLES = 3
STILL_SOURCES = IMAGE_SOURCES + ("local",)

ASPECTS = (
    ("9:16", "Portrait 9:16"),
    ("16:9", "Landscape 16:9"),
    ("1:1", "Square 1:1"),
)

VOICE_TTS, VOICE_UPLOAD, VOICE_NONE = "tts", "upload", "none"
TTS_SERVERS = (
    ("chatterbox", "Chatterbox (local)"),
    ("azure-tts-v1", "Edge TTS (free)"),
    ("azure-tts-v2", "Azure TTS V2"),
    ("siliconflow", "SiliconFlow"),
    ("gemini-tts", "Google Gemini TTS"),
    ("mimo-tts", "Xiaomi MiMo TTS"),
    ("minimax-tts", "MiniMax TTS"),
    ("elevenlabs", "ElevenLabs"),
    ("fish_audio", "Fish Audio"),
    ("voxcpm", "VoxCPM"),
)
TTS_SERVER_IDS = tuple(server for server, _ in TTS_SERVERS)

MUSIC_NONE, MUSIC_RANDOM = "", "random"
MUSIC_TYPES = (
    (MUSIC_NONE, "No music"),
    (MUSIC_RANDOM, "A random track"),
    ("preset", "A track from the library"),
    ("custom", "My own file"),
    ("sonilo", "Composed by Sonilo (paid)"),
    ("elevenlabs", "Composed by ElevenLabs (paid)"),
)
GENERATED_MUSIC = ("sonilo", "elevenlabs")

CUSTOM_AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
MAX_CUSTOM_AUDIO_BYTES = 200 * 1024 * 1024
NO_SOUND = ""

# ``[ui]`` 里和任务参数同名的那些键——WebUI 每改一项就存一项，所以这就是
# "上一次用的设置"。创建之后也把同一批键存回去。
UI_FIELDS = (
    "video_language", "paragraph_number", "video_script_prompt",
    "custom_system_prompt", "video_concat_mode", "video_transition_mode",
    "video_fit_mode", "image_motion", "video_clip_duration",
    "video_clip_speed", "video_count", "voice_name", "voice_volume",
    "voice_rate", "bgm_volume", "subtitle_enabled", "font_name",
    "subtitle_position", "custom_position", "subtitle_display_mode",
    "subtitle_animation", "subtitle_max_lines", "text_fore_color",
    "font_size", "stroke_color", "stroke_width", "rounded_subtitle_background",
    "transition_sound", "transition_sound_volume",
    "finish_look",
)
# 从来没存过 ``finish_look`` 时，新片子用哪种精修。任务参数本身的默认值是空
# （不精修）——那是给 API 和老任务的；在这张表里开的新片子默认就精修。存过
# 的值（包括存成空的"关掉"）照旧优先。
DEFAULT_FINISH_LOOK = "cold_documentary"
# 第一次带精修开片子时一起换上的那几项，见 ``saved_settings``。
FIRST_FINISHED_VIDEO = {
    "subtitle_display_mode": "highlight",
    "image_motion": "varied",
    "video_transition_mode": "CrossFade",
}
# 这几项 WebUI 存在 ``[app]`` 里。
APP_FIELDS = (
    "match_materials_to_script", "align_terms_to_narration",
    "one_picture_per_sentence", "max_image_scene_duration",
)
# 收在"更多设置"后面的那些项。
#
# 一页摆十七项，没人找得到"画面从哪来"。平时要动的就那么几样——来源、画幅、
# 画风、音色、字幕开不开、配乐——其余的是"偶尔调一次"的，收起来。收起来的
# 项照样用上次的值、照样发给服务端：收的是屏幕，不是功能。
ADVANCED_FIELDS = frozenset({
    "image_motion", "one_picture_per_sentence", "max_image_scene_duration",
    "align_terms_to_narration", "review_prompts", "video_concat_mode",
    "match_materials_to_script", "video_transition_mode", "video_fit_mode",
    "video_clip_speed", "video_count",
    "voice_volume",
    "stroke_color", "stroke_width", "subtitle_display_mode",
    "subtitle_animation", "subtitle_max_lines", "subtitle_background_enabled",
    "subtitle_background_color", "rounded_subtitle_background",
    "transition_sound", "transition_sound_volume",
})

# 表单原样交给任务参数的那些项。
DIRECT_FIELDS = UI_FIELDS + APP_FIELDS + (
    "llm_provider", "llm_model",
    # 只属于这一条片子，不记成"上次用的"：每条片子有多少画面值得去找照片
    # 是看题材的。
    "photo_scene_percent",
)
# "多少画面照着照片画"能挑的那几档。
PHOTO_SHARES = (
    (0, "None — draw every scene from its prompt"),
    (10, "10% of the scenes"), (25, "25% of the scenes"),
    (50, "50% of the scenes"), (75, "75% of the scenes"),
    (100, "Every scene"),
)


# ---------------------------------------------------------------------------
# 存下来的设置
# ---------------------------------------------------------------------------

def _valid(name: str, value: Any) -> bool:
    try:
        TaskVideoRequest.model_validate({"video_subject": "x", name: value})
    except ValidationError:
        return False
    return True


def _tracks() -> list:
    try:
        return [os.path.basename(path) for path in bgm.list_bgm_files()]
    except Exception as exc:  # pragma: no cover - 列目录失败不该挡住表单
        logger.warning(f"cannot list the background music: {exc}")
        return []


def _builtin_tracks() -> list:
    try:
        return [os.path.basename(path) for path in bgm.list_builtin_bgm_files()]
    except Exception as exc:  # pragma: no cover
        logger.warning(f"cannot list the built-in music: {exc}")
        return []


def _sounds() -> list:
    try:
        return [os.path.basename(path)
                for path in transition_sound.list_transition_sounds()]
    except Exception as exc:  # pragma: no cover
        logger.warning(f"cannot list the transition sounds: {exc}")
        return []


def _fonts() -> list:
    from app.services import scene_editor

    try:
        return scene_editor.available_fonts()
    except Exception as exc:  # pragma: no cover
        logger.warning(f"cannot list the fonts: {exc}")
        return []


def _saved_aspects(ui: Mapping) -> dict:
    """WebUI 按来源分别记画幅（``video_aspect_<来源>``）。"""
    known = {value for value, _ in ASPECTS}
    aspects = {}
    for source in SOURCE_IDS:
        value = str(ui.get(f"video_aspect_{source}") or "").strip()
        if value in known:
            aspects[source] = value
    return aspects


def _saved_music(ui: Mapping) -> dict:
    """存下来的配乐：哪一种、哪一首、给作曲服务的提示词。"""
    kind = str(ui.get("bgm_type", MUSIC_RANDOM) or "").strip()
    if kind == "none":
        kind = MUSIC_NONE
    if kind not in {value for value, _ in MUSIC_TYPES}:
        kind = MUSIC_RANDOM
    track = ""
    if kind == "preset":
        song = os.path.basename(str(ui.get("preset_song") or "").strip())
        track = song if song in _builtin_tracks() else ""
    elif kind == "custom":
        track = os.path.basename(str(ui.get("custom_bgm_file") or "").strip())
    prompt = ""
    if kind == "sonilo":
        prompt = str(ui.get("sonilo_bgm_prompt") or "")
    elif kind == "elevenlabs":
        prompt = str(ui.get("elevenlabs_music_prompt") or "")
    return {"music_type": kind, "music_track": track, "music_prompt": prompt}


def saved_settings(ui: Optional[Mapping] = None, app: Optional[Mapping] = None) -> dict:
    """上一次用的设置，整理成任务参数的样子。

    一项一项地校验：某个存下来的值现在不合法了（比如片段时长超了 API 的
    上限），丢掉那一项用默认值，不让它挡住整张表。
    """
    ui = config.ui if ui is None else ui
    app = config.app if app is None else app
    saved: dict = {}
    for source, names in ((ui, UI_FIELDS), (app, APP_FIELDS)):
        for name in names:
            if name not in source:
                continue
            if _valid(name, source[name]):
                saved[name] = source[name]
            else:
                logger.warning(
                    f"the saved {name} is not usable for a new video, "
                    f"using the default instead"
                )

    source_id = str(app.get("video_source") or "").strip()
    if source_id in SOURCE_IDS:
        saved["video_source"] = source_id

    if ui.get("subtitle_background_enabled"):
        colour = str(ui.get("subtitle_background_color") or "#000000")
        if _valid("text_background_color", colour):
            saved["text_background_color"] = colour
    if "finish_look" not in ui:
        # 从来没存过 ``finish_look``：上一次开片子时还没有精修，那时存下来的
        # "一句一句"、"慢推"、"不要转场"是在没有别的可挑的时候挑的。第一条
        # 新片子用新的那一套；开过一条之后这几项和别的一样记着上次挑的。
        #
        # Nothing was ever saved for the finish, so the saved subtitle mode,
        # camera move and transition were chosen before the new ones existed.
        # The first new video starts from the new set; after that the form
        # remembers what was chosen, like every other field.
        saved.update(FIRST_FINISHED_VIDEO)
    if saved.get("finish_look") not in ("", *finishing.LOOKS):
        saved["finish_look"] = DEFAULT_FINISH_LOOK
    return saved


def _saved_voice_mode(ui: Mapping) -> str:
    mode = str(ui.get("voice_mode") or "").strip()
    if mode in (VOICE_TTS, VOICE_UPLOAD, VOICE_NONE):
        return mode
    if voice.is_no_voice(str(ui.get("voice_name") or "")):
        return VOICE_NONE
    return VOICE_TTS


def server_of(voice_name: str, ui: Optional[Mapping] = None) -> str:
    """这个音色是哪一家的。名字认不出来时用存下来的那一家。"""
    name = str(voice_name or "").strip()
    checks = (
        ("siliconflow", voice.is_siliconflow_voice),
        ("gemini-tts", voice.is_gemini_voice), ("mimo-tts", voice.is_mimo_voice),
        ("minimax-tts", voice.is_minimax_voice),
        ("elevenlabs", voice.is_elevenlabs_voice),
        ("chatterbox", voice.is_chatterbox_voice),
        ("fish_audio", voice.is_fish_audio_voice), ("voxcpm", voice.is_voxcpm_voice),
    )
    for server, matches in checks:
        try:
            if name and matches(name):
                return server
        except Exception:  # pragma: no cover - 认不出就往下走
            continue
    if name and voice.is_azure_v2_voice(name):
        return "azure-tts-v2"
    saved = str((config.ui if ui is None else ui).get("tts_server") or "").strip()
    if not name and saved in TTS_SERVER_IDS:
        return saved
    return "azure-tts-v1" if name else "chatterbox"


# ---------------------------------------------------------------------------
# 音色
# ---------------------------------------------------------------------------

def _voice_row(name: str) -> dict:
    gender = voice.gender_of(name)
    label = name.split(":", 1)[1] if ":" in name else name
    for suffix in ("-Female", "-Male"):
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    if gender:
        label = f"{label} ({gender})"
    return {"id": name, "label": label}


def _list_voices(server: str) -> list:
    if server == "azure-tts-v1":
        return [name for name in voice.get_all_azure_voices()
                if not voice.is_azure_v2_voice(name)]
    if server == "azure-tts-v2":
        return [name for name in voice.get_all_azure_voices()
                if voice.is_azure_v2_voice(name)]
    if server == "siliconflow":
        return voice.get_siliconflow_voices()
    if server == "gemini-tts":
        return voice.get_gemini_voices()
    if server == "mimo-tts":
        return voice.get_mimo_voices()
    if server == "minimax-tts":
        return voice.get_minimax_voices()
    if server == "elevenlabs":
        return voice.get_elevenlabs_voices(voice.get_elevenlabs_api_key())
    if server == "chatterbox":
        return voice.get_chatterbox_voices()
    if server == "fish_audio":
        return voice.get_fish_audio_voices()
    if server == "voxcpm":
        return voice.get_voxcpm_voices()
    raise NewVideoError(f"unknown voice service {server!r}")


def voices(server: str, prefer: str = "", keep: str = "") -> dict:
    """这一家的音色，按语言分组，脚本的语言排在前面。

    读不到就照实说是哪一家读不到（服务没开、Key 没填），不拿别家的顶上。
    ``keep`` 是已经选着的那个音色：目录暂时读不到时也留着它，断线不等于
    用户改了主意。
    """
    server = str(server or "").strip()
    label = dict(TTS_SERVERS).get(server)
    if label is None:
        raise NewVideoError(f"unknown voice service {server!r}")
    notes = []
    try:
        names = list(_list_voices(server) or [])
    except NewVideoError:
        raise
    except Exception as exc:
        logger.warning(f"{server} voice list unavailable ({type(exc).__name__})")
        names = []
    if not names:
        notes.append(
            f"{label} did not return any voices — check that it is running "
            f"and set up under Settings."
        )
    groups = []
    for code, group in voice.by_language(names, prefer=prefer):
        if not group:
            continue
        groups.append({
            "label": voice.language_label(code) if code else label,
            "voices": [_voice_row(name) for name in group],
        })
    keep = str(keep or "").strip()
    if keep and keep not in names and server_of(keep) == server:
        groups.insert(0, {"label": "Selected", "voices": [_voice_row(keep)]})
    return {"server": server, "groups": groups, "notes": notes}


# ---------------------------------------------------------------------------
# 表单
# ---------------------------------------------------------------------------

def _options(rows) -> list:
    return [{"id": value, "label": label} for value, label in rows]


def _numbers(values, suffix="") -> list:
    return [{"id": value, "label": f"{value}{suffix}"} for value in values]


def _field(name, label, kind, **more) -> dict:
    row = {"name": name, "label": label, "kind": kind}
    row.update({key: value for key, value in more.items() if value is not None})
    return row


def form() -> dict:
    """表单的全部：每一页有哪些项、能挑什么、默认是什么。"""
    # 启动时 Ollama 可能还没起来，那时挑不了模型；开表单时再补一次。什么都
    # 不缺的话这一步不发任何请求。
    local_defaults.apply()
    ui, app = config.ui, config.app
    saved = saved_settings()
    params = TaskVideoRequest.model_validate({"video_subject": "x", **saved})
    dump = params.model_dump(mode="json")

    source = dump["video_source"] if dump["video_source"] in SOURCE_IDS else SOURCE_IDS[0]
    aspects = _saved_aspects(ui)
    template = tm.resolve_image_prompt_template(params)
    style = image_style_model.style_for_template(template)
    mode = _saved_voice_mode(ui)
    saved_voice = "" if voice.is_no_voice(dump["voice_name"]) else dump["voice_name"]
    server = server_of(saved_voice)
    music = _saved_music(ui)
    background = dump.get("text_background_color")

    values = {name: dump[name] for name in DIRECT_FIELDS}
    values["video_transition_mode"] = dump["video_transition_mode"] or "None"
    values.update({
        "video_source": source,
        "video_aspect": aspects.get(source, dump["video_aspect"]),
        # 设置里是自定义模板时不谎报一个预设：空的就是"按设置来"。
        "image_style": "" if style == image_style_model.IMAGE_STYLE_CUSTOM else style,
        "confirm_charge": False,
        "local_materials": [],
        "review_prompts": bool(ui.get("review_prompts_before_images", True)),
        "video_terms": "",
        "voice_mode": mode,
        "tts_server": server,
        "voice_name": saved_voice,
        "custom_audio": "",
        "subtitle_background_enabled": isinstance(background, str) and bool(background),
        "subtitle_background_color": (
            background if isinstance(background, str) and background
            else str(ui.get("subtitle_background_color") or "#000000")
        ),
        "transition_sound": (
            dump["transition_sound"] if dump["transition_sound"] in _sounds()
            else NO_SOUND
        ),
        "music_type": music["music_type"],
        "music_preset": music["music_track"] if music["music_type"] == "preset" else "",
        "music_custom": music["music_track"] if music["music_type"] == "custom" else "",
        "music_prompt": music["music_prompt"],
    })

    drawn = {"video_source": list(IMAGE_SOURCES)}
    tts = {"voice_mode": [VOICE_TTS]}
    subtitled = {"subtitle_enabled": [True]}
    with_music = {"music_type": [kind for kind, _ in MUSIC_TYPES if kind]}

    source_options = [
        {"id": source_id, "label": label, "group": group}
        for source_id, label, group, _ in SOURCES
    ]
    providers = [{"id": "", "label": "As in settings"}] + [
        {"id": provider.provider_id,
         "label": provider.default_label or provider.provider_id}
        for provider in LLM_PROVIDER_REGISTRY
    ]

    tabs = [
        {"id": "pictures", "label": "Pictures", "fields": [
            _field("video_source", "Where the pictures come from", "select",
                   options=source_options),
            _field("confirm_charge",
                   "I understand this source is charged for every video", "bool",
                   show={"video_source": list(PAID_SOURCES)}, warn=True),
            _field("local_materials", "Your videos and pictures", "materials",
                   show={"video_source": ["local"]}),
            _field("video_aspect", "Format", "choice", options=_options(ASPECTS)),
            # "styles"：一排带样图的卡片，而不是一个只有名字的下拉框——画风
            # 是要看的，名字说不清"水彩"和"油画"差在哪。挑出来的值和原来
            # 一样（画风 id，或者空串＝照设置里的）。``settings_style`` 让
            # "照设置"那一张也有图可看。
            _field("image_style", "Picture style", "styles", show=drawn,
                   options=[{"id": "", "label": "As in settings"}] + [
                       {"id": one, "label": one}
                       for one in image_style_model.image_style_ids()],
                   style_labels=True,
                   # 每种画风有几张样图（同样的几个画面，各画风各画一遍）。
                   samples=STYLE_SAMPLES,
                   settings_style=(
                       "" if style == image_style_model.IMAGE_STYLE_CUSTOM
                       else str(style or ""))),
            # 照着照片画的那几张是用户自己挑照片的：点"开画"时先把这几张的
            # 照片挑完，然后才开始排队出图。
            _field("photo_scene_percent", "Scenes drawn from photos you choose",
                   "select", show=drawn, options=_options(PHOTO_SHARES)),
            _field("finish_look", "Finish (colour, sound levels, fades)", "select",
                   options=[{"id": "", "label": "Off — the classic render"}] + [
                       {"id": look, "label": spec["label"]}
                       for look, spec in finishing.LOOKS.items()]),
            _field("image_motion", "Camera move on a still", "select",
                   show={"video_source": list(STILL_SOURCES)},
                   options=_options((
                       ("varied", "Varied (a different move per picture)"),
                       ("zoom", "Slow zoom"), ("zoom_out", "Pull back"),
                       ("pan_right", "Pan right"), ("pan_left", "Pan left"),
                       ("zoom_top", "Push in on the upper third"),
                       ("static", "Still")))),
            _field("video_clip_duration", "Longest a picture stays (seconds)",
                   "number", min=1, max=15, step=1),
            _field("one_picture_per_sentence", "One picture per sentence", "bool",
                   show=drawn),
            _field("max_image_scene_duration",
                   "Split a picture that would stay longer than (seconds, 0 = never)",
                   "number", min=0, max=60, step=0.5, show=drawn),
            _field("align_terms_to_narration",
                   "Match each picture to the line being spoken", "bool"),
            _field("review_prompts",
                   "Show me the picture prompts before drawing", "bool", show=drawn),
            _field("video_concat_mode", "Clip order", "select",
                   options=_options((("random", "Random"),
                                     ("sequential", "In order")))),
            _field("match_materials_to_script",
                   "Keep the clips in the order of the script", "bool"),
            _field("video_transition_mode", "Transition between pictures", "select",
                   options=_options((
                       ("None", "None"), ("CrossFade", "Crossfade"),
                       ("Shuffle", "Shuffle"), ("FadeIn", "Fade in"),
                       ("FadeOut", "Fade out"), ("SlideIn", "Slide in"),
                       ("SlideOut", "Slide out"), ("ZoomIn", "Zoom in"),
                       ("ZoomOut", "Zoom out")))),
            _field("video_fit_mode", "When the shape does not match", "select",
                   options=_options((("cover", "Fill and crop"),
                                     ("contain", "Fit with black bars")))),
            _field("video_clip_speed", "Clip speed", "range",
                   min=0.5, max=2, step=0.05, unit="×"),
            _field("video_count", "How many versions to make", "select",
                   options=_numbers((1, 2, 3, 4, 5))),
        ]},
        {"id": "voice", "label": "Voice", "fields": [
            _field("voice_mode", "Narration", "choice", options=_options((
                (VOICE_TTS, "Read it for me"), (VOICE_UPLOAD, "Use my recording"),
                (VOICE_NONE, "No narration")))),
            _field("tts_server", "Voice service", "select", show=tts,
                   options=_options(TTS_SERVERS)),
            _field("voice_name", "Voice", "voice", show=tts),
            _field("voice_rate", "Speed", "select", show=tts,
                   options=_numbers((0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0), "×")),
            _field("voice_volume", "Volume", "select",
                   show={"voice_mode": [VOICE_TTS, VOICE_UPLOAD]},
                   options=_numbers((0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0), "×")),
            _field("custom_audio", "Your recording", "audio",
                   show={"voice_mode": [VOICE_UPLOAD]},
                   accept=",".join(CUSTOM_AUDIO_EXTENSIONS)),
        ]},
        {"id": "subtitles", "label": "Subtitles", "fields": [
            _field("subtitle_enabled", "Show subtitles", "bool"),
            _field("font_name", "Font", "select", show=subtitled,
                   options=[{"id": name, "label": name} for name in _fonts()]),
            _field("font_size", "Size", "number", min=8, max=300, step=1,
                   show=subtitled),
            _field("text_fore_color", "Colour", "color", show=subtitled),
            _field("stroke_color", "Outline colour", "color", show=subtitled),
            _field("stroke_width", "Outline width", "number", min=0, max=20,
                   step=0.5, show=subtitled),
            _field("subtitle_position", "Position", "select", show=subtitled,
                   options=_options((
                       ("bottom", "Bottom"), ("two_thirds_bottom", "Lower third"),
                       ("center", "Centre"), ("top", "Top"), ("custom", "Custom")))),
            _field("custom_position", "Distance from the top (%)", "number",
                   min=0, max=100, step=1,
                   show={"subtitle_enabled": [True], "subtitle_position": ["custom"]}),
            _field("subtitle_display_mode", "Show", "select", show=subtitled,
                   options=_options((("sentence", "A sentence at a time"),
                                     ("word_by_word", "A word at a time"),
                                     ("highlight", "A few words, the spoken one "
                                                   "lit up (needs a finish)")))),
            _field("subtitle_animation", "Animation", "select", show=subtitled,
                   options=_options((("none", "None"), ("pop_spring", "Pop up")))),
            _field("subtitle_max_lines", "Most lines at once", "number",
                   min=1, max=4, step=1, show=subtitled),
            _field("subtitle_background_enabled", "Box behind the words", "bool",
                   show=subtitled),
            _field("subtitle_background_color", "Box colour", "color",
                   show={"subtitle_enabled": [True],
                         "subtitle_background_enabled": [True]}),
            _field("rounded_subtitle_background", "Rounded box", "bool",
                   show={"subtitle_enabled": [True],
                         "subtitle_background_enabled": [True]}),
        ]},
        {"id": "music", "label": "Music", "fields": [
            _field("music_type", "Background music", "select",
                   options=_options(MUSIC_TYPES)),
            _field("music_preset", "Track", "select",
                   show={"music_type": ["preset"]},
                   options=[{"id": name, "label": os.path.splitext(name)[0]}
                            for name in _builtin_tracks()]),
            _field("music_custom", "Your music file", "music",
                   show={"music_type": ["custom"]}),
            _field("music_prompt", "What the music should sound like", "textarea",
                   show={"music_type": list(GENERATED_MUSIC)},
                   placeholder="Leave empty to let it follow the video."),
            _field("bgm_volume", "Music volume", "range", min=0, max=1, step=0.05,
                   percent=True, show=with_music),
            _field("transition_sound", "Sound at every cut", "select",
                   options=[{"id": NO_SOUND, "label": "None"}] + [
                       {"id": name, "label": os.path.splitext(name)[0]}
                       for name in _sounds()]),
            _field("transition_sound_volume", "Cut sound volume", "range",
                   min=0, max=1, step=0.05, percent=True),
        ]},
        # ``side``：这一页不在右边的页签里，而是收在左边脚本框底下——它讲的
        # 是"说什么"，和主题、脚本是一回事。
        {"id": "script", "label": "More script settings", "side": True, "fields": [
            _field("scene_plan", "Scene plan (written outside the app)",
                   "textarea",
                   placeholder="Optional. Paste the JSON a scene plan was "
                               "exported as: it carries the narration, one "
                               "picture prompt per scene, which scenes are "
                               "drawn from a real photo, and the direction "
                               "of the edit (title, cards, pauses, impacts, "
                               "music, ambience; these need a Finish look). "
                               "The script box and the keywords are then "
                               "not used."),
            _field("video_terms", "Picture keywords", "textarea",
                   placeholder="Optional, comma separated. Leave empty and "
                               "they are written from the script."),
            _field("video_script_prompt", "Extra instructions for the script",
                   "textarea", placeholder="For example: keep it punchy, "
                                           "open with a question."),
            _field("custom_system_prompt", "Replace the script system prompt",
                   "textarea", placeholder="Advanced. Leave empty to use the "
                                           "built-in one."),
            _field("llm_provider", "Language model for this video", "select",
                   options=providers),
            _field("llm_model", "Model name", "text",
                   placeholder="Leave empty for that provider's default."),
        ]},
    ]

    for tab in tabs:
        for row in tab["fields"]:
            if row["name"] in ADVANCED_FIELDS:
                row["advanced"] = True

    return {
        "tabs": tabs,
        "values": values,
        "languages": list(LANGUAGES),
        "saved_aspects": aspects,
        "paid_sources": list(PAID_SOURCES),
        # 补完默认值之后还缺的东西。现在说，好过跑到一半才失败。
        "setup_problems": local_defaults.problems(),
        "voices": voices(server, prefer=values["video_language"], keep=saved_voice),
    }


# 旧名字留着：别处（和测试）还在用它。
options = form


# ---------------------------------------------------------------------------
# 拼出任务参数
# ---------------------------------------------------------------------------

def _resolve_track(name: str, what: str) -> str:
    try:
        return bgm.resolve_bgm_file(str(name or "").strip())
    except ValueError as exc:
        raise NewVideoError(f"unknown {what} {name!r}: {exc}") from exc


def _music_params(kind: str, track: str, prompt: str) -> dict:
    kind = str(kind or "").strip()
    if kind not in {value for value, _ in MUSIC_TYPES}:
        raise NewVideoError(f"unknown kind of background music {kind!r}")
    fields = {"bgm_type": kind, "bgm_file": "", "video_music_prompt": ""}
    if kind in ("preset", "custom"):
        if not str(track or "").strip():
            raise NewVideoError(
                "choose a track from the library first" if kind == "preset"
                else "upload your music file first"
            )
        fields["bgm_file"] = _resolve_track(track, "background music")
    if kind in GENERATED_MUSIC:
        fields["video_music_prompt"] = str(prompt or "").strip()
    return fields


def _local_materials(names: Any) -> list:
    folder = utils.storage_dir("local_videos", create=True)
    materials = []
    for name in names or []:
        base = os.path.basename(str(name or "").strip())
        if not base or base != str(name).strip():
            raise NewVideoError(f"{name!r} is not one of your uploaded files")
        path = os.path.join(folder, base)
        if not os.path.isfile(path):
            raise NewVideoError(f"{base} is no longer on disk; upload it again")
        info = MaterialInfo()
        info.provider = "local"
        info.url = path
        materials.append(info)
    if not materials:
        raise NewVideoError("upload at least one video or picture first")
    return materials


def build_params(choices: Mapping) -> TaskVideoRequest:
    """表单里挑的，叠在存下来的设置上，拼成一份任务参数。

    ``choices`` 里是 ``None`` 的项表示"没动过"，用存下来的值。
    """
    chosen = {key: value for key, value in dict(choices).items() if value is not None}
    ui = config.ui

    subject = str(chosen.get("video_subject") or "").strip()
    script = str(chosen.get("video_script") or "").strip()
    if not subject and not script and not str(chosen.get("scene_plan") or "").strip():
        raise NewVideoError(
            "say what the video is about, or paste a script — both are empty"
        )

    params: dict = dict(saved_settings())
    params["video_subject"] = subject
    params["video_script"] = script

    source = str(chosen.get("video_source") or params.get("video_source")
                 or SOURCE_IDS[0]).strip()
    if source not in SOURCE_IDS:
        raise NewVideoError(
            f"{source!r} cannot be started from the editor; the sources "
            f"offered here are {', '.join(SOURCE_IDS)}"
        )
    params["video_source"] = source
    if source in PAID_SOURCES and chosen.get("confirm_charge") is not True:
        label = {row[0]: row[1] for row in SOURCES}[source]
        raise NewVideoError(
            f"{label} is charged for every video; tick the box to confirm "
            f"before starting"
        )
    if source == "local":
        params["video_materials"] = _local_materials(chosen.get("local_materials"))

    aspect = str(
        chosen.get("video_aspect") or _saved_aspects(ui).get(source)
        or VideoAspect.portrait.value
    ).strip()
    if aspect not in {value for value, _ in ASPECTS}:
        raise NewVideoError(
            f"unknown format {aspect!r}; choose one of "
            f"{', '.join(value for value, _ in ASPECTS)}"
        )
    params["video_aspect"] = aspect

    style = str(chosen.get("image_style") or "").strip()
    if style:
        if source not in IMAGE_SOURCES:
            raise NewVideoError(
                f"a picture style only applies to generated pictures, and "
                f"{source} is not one"
            )
        template = image_style_model.template_for_style(style)
        if not template:
            raise NewVideoError(f"unknown picture style {style!r}")
        # 钉在这条片子自己的参数上，不写进全局配置：在这里挑的风格只管这一条。
        params["image_prompt_template"] = template

    for name in DIRECT_FIELDS:
        if name in chosen:
            params[name] = chosen[name]

    terms = str(chosen.get("video_terms") or "").strip()
    if terms:
        params["video_terms"] = terms

    plan_text = str(chosen.get("scene_plan") or "").strip()
    if plan_text:
        # 分镜自己带着旁白、每段的提示词、哪几段照着照片画：这三样都照它来，
        # 不再问模型。
        if source not in IMAGE_SOURCES:
            raise NewVideoError(
                "a scene plan carries picture prompts, so it needs generated "
                f"pictures, and {source} is not one; choose one of "
                f"{', '.join(IMAGE_SOURCES)}"
            )
        if script:
            raise NewVideoError(
                "the scene plan carries its own narration; empty the script "
                "box, or remove the scene plan"
            )
        try:
            plan = scene_plan.parse(plan_text)
        except scene_plan.ScenePlanError as exc:
            raise NewVideoError(str(exc)) from exc
        params["video_subject"] = subject or plan["subject"]
        params["video_script"] = scene_plan.script_of(plan)
        params["video_terms"] = scene_plan.prompts_of(plan)
        params["scene_plan"] = scene_plan.stored(plan)
        params["photo_scene_percent"] = scene_plan.photo_percent(params["scene_plan"])

    mode = str(chosen.get("voice_mode") or _saved_voice_mode(ui)).strip()
    if mode not in (VOICE_TTS, VOICE_UPLOAD, VOICE_NONE):
        raise NewVideoError(f"unknown kind of narration {mode!r}")
    if mode == VOICE_NONE:
        params["voice_name"] = voice.NO_VOICE_NAME
        params["voice_volume"] = 1.0
        params["voice_rate"] = 1.0
    elif mode == VOICE_UPLOAD:
        if not str(chosen.get("custom_audio") or "").strip():
            raise NewVideoError("upload your recording first")
        staged_audio(chosen["custom_audio"])
        params["voice_rate"] = 1.0
    if mode != VOICE_NONE and (
        not str(params.get("voice_name") or "").strip()
        or voice.is_no_voice(str(params.get("voice_name")))
    ):
        if mode == VOICE_TTS:
            raise NewVideoError("choose a voice first")
        params.pop("voice_name", None)

    if "subtitle_background_enabled" in chosen:
        colour = str(chosen.get("subtitle_background_color")
                     or ui.get("subtitle_background_color") or "#000000")
        params["text_background_color"] = (
            colour if chosen["subtitle_background_enabled"] else False
        )

    if "music_type" in chosen:
        kind = str(chosen["music_type"] or "")
        params.update(_music_params(
            kind,
            chosen.get("music_custom" if kind == "custom" else "music_preset", ""),
            chosen.get("music_prompt", ""),
        ))
    else:
        music = _saved_music(ui)
        if music["music_type"] in ("preset", "custom") and not music["music_track"]:
            music["music_type"] = MUSIC_RANDOM
        params.update(_music_params(**{
            "kind": music["music_type"], "track": music["music_track"],
            "prompt": music["music_prompt"],
        }))

    sound = str(params.get("transition_sound") or "").strip()
    if sound and sound not in _sounds():
        if "transition_sound" in chosen:
            raise NewVideoError(f"unknown cut sound {sound!r}")
        params["transition_sound"] = NO_SOUND

    look = str(params.get("finish_look") or "").strip()
    if look and look not in finishing.LOOKS:
        raise NewVideoError(
            f"unknown finish {look!r}; choose one of {', '.join(finishing.LOOKS)}, "
            "or leave it empty for the classic render"
        )
    params["finish_look"] = look

    try:
        return TaskVideoRequest.model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "the form"
        raise NewVideoError(f"{where}: {first.get('msg', 'is not valid')}") from exc


def stop_stage(params, review_prompts: Optional[bool] = None) -> str:
    """这条片子跑到哪一步停下来等人。

    文生图先停在字幕：提示词摆出来给人看，点头了才开画（出图是最慢最贵的
    一步）——除非用户关掉了那个开关。其余的停在素材。永远不是 ``video``。
    """
    source = str(getattr(params, "video_source", "") or "")
    if source not in material.GENERATED_IMAGE_SOURCES:
        return "materials"
    if getattr(params, "scene_plan", None):
        # 分镜里照着照片画的那几段，要在开画之前挑照片：一定先停下来。
        return "subtitle"
    wanted = tm.reviews_prompts_first(params) if review_prompts is None else review_prompts
    return "subtitle" if wanted else "materials"


def remember(choices: Mapping, params) -> None:
    """把这一次的选择存成"上次用的"，和 WebUI 存的是同一批键。

    WebUI 是每改一项存一项；这里等创建成功了才存——没开成的那一次不算。
    存不上不挡片子：片子已经开了，记不住只是下次要多挑一遍。
    """
    try:
        dump = params.model_dump(mode="json")
        ui = {name: dump[name] for name in UI_FIELDS}
        ui["video_transition_mode"] = dump["video_transition_mode"] or "None"
        mode = str(choices.get("voice_mode") or "")
        if mode in (VOICE_TTS, VOICE_UPLOAD, VOICE_NONE):
            ui["voice_mode"] = mode
        if mode == VOICE_TTS:
            ui["tts_server"] = server_of(dump["voice_name"])
        if mode == VOICE_NONE:
            # "不要配音"不该把上次挑的音色冲掉。
            for name in ("voice_name", "voice_volume", "voice_rate"):
                ui.pop(name, None)
        ui[f"video_aspect_{dump['video_source']}"] = dump["video_aspect"]
        background = dump.get("text_background_color")
        ui["subtitle_background_enabled"] = isinstance(background, str) and bool(background)
        if ui["subtitle_background_enabled"]:
            ui["subtitle_background_color"] = background
        ui["bgm_type"] = dump["bgm_type"]
        if dump["bgm_type"] == "preset":
            ui["preset_song"] = os.path.basename(dump["bgm_file"])
        elif dump["bgm_type"] == "custom":
            ui["custom_bgm_file"] = os.path.basename(dump["bgm_file"])
        elif dump["bgm_type"] == "sonilo":
            ui["sonilo_bgm_prompt"] = dump["video_music_prompt"]
        elif dump["bgm_type"] == "elevenlabs":
            ui["elevenlabs_music_prompt"] = dump["video_music_prompt"]
        if choices.get("review_prompts") is not None:
            ui["review_prompts_before_images"] = bool(choices["review_prompts"])

        app = {name: dump[name] for name in APP_FIELDS}
        app["video_source"] = dump["video_source"]

        config.update_config_values_nonblocking(config.ui, ui)
        config.update_config_values_nonblocking(config.app, app)
        config.try_save_config()
    except Exception as exc:  # pragma: no cover - 见上
        logger.warning(f"could not remember the new-video choices: {exc}")


# ---------------------------------------------------------------------------
# 上传的配音
# ---------------------------------------------------------------------------

def _audio_staging_dir() -> str:
    return utils.storage_dir("new_video_audio", create=True)


def stage_audio(filename: str, source: BinaryIO) -> str:
    """先把上传的配音放在一边，返回一个凭它来取的名字。

    任务目录要等点了创建才有，而文件是在那之前选的，所以分两步。
    """
    extension = os.path.splitext(str(filename or ""))[1].lower()
    if extension not in CUSTOM_AUDIO_EXTENSIONS:
        raise NewVideoError(
            f"{extension or 'this file'} is not a recording this can use; "
            f"use one of {', '.join(CUSTOM_AUDIO_EXTENSIONS)}"
        )
    token = f"{uuid.uuid4().hex}{extension}"
    path = os.path.join(_audio_staging_dir(), token)
    size = 0
    try:
        with open(path, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CUSTOM_AUDIO_BYTES:
                    raise NewVideoError(
                        "that recording is larger than "
                        f"{MAX_CUSTOM_AUDIO_BYTES // (1024 * 1024)} MB"
                    )
                target.write(chunk)
        if not size:
            raise NewVideoError("that recording is empty")
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return token


def staged_audio(token: str) -> str:
    """凭名字找回放在一边的那个文件。只认文件名，不认路径。"""
    name = str(token or "").strip()
    if not name or name != os.path.basename(name):
        raise NewVideoError("that recording cannot be found; upload it again")
    path = os.path.join(_audio_staging_dir(), name)
    if not os.path.isfile(path):
        raise NewVideoError("that recording cannot be found; upload it again")
    return path


def place_audio(task_id: str, token: str) -> str:
    """把放在一边的配音搬进这条片子自己的目录——流水线只认那里的文件。"""
    source = staged_audio(token)
    target = os.path.join(
        utils.task_dir(task_id), f"custom-audio{os.path.splitext(source)[1]}"
    )
    shutil.copyfile(source, target)
    try:
        os.remove(source)
    except OSError:  # pragma: no cover - 留着也不碍事
        pass
    return target


# ---------------------------------------------------------------------------
# 试听和写脚本
# ---------------------------------------------------------------------------

VOICE_SAMPLE = "Hello! This is how this voice sounds reading your video."


def preview_voice(voice_name: str, rate: float = 1.0, volume: float = 1.0,
                  text: str = "") -> str:
    """念一小段，返回音频文件的路径。调用方负责播完删掉。"""
    name = str(voice_name or "").strip()
    if not name or voice.is_no_voice(name):
        raise NewVideoError("choose a voice first")
    sample = " ".join(str(text or "").split())[:240] or VOICE_SAMPLE
    path = os.path.join(
        utils.storage_dir("temp", create=True), f"tmp-voice-{uuid.uuid4().hex}.mp3"
    )
    service = dict(TTS_SERVERS).get(server_of(name), "the voice service")
    # 和流水线抢同一把配置锁：有片子在跑时不去动它正在用的 TTS 配置。
    with config.try_runtime_config_lock() as free:
        if not free:
            raise NewVideoError(
                "a video is being made right now; try the voice again when "
                "it has finished"
            )
        try:
            made = voice.tts(text=sample, voice_name=name, voice_rate=float(rate),
                             voice_file=path, voice_volume=float(volume))
        except Exception as exc:
            raise NewVideoError(f"{service} could not read the sample: {exc}") from exc
    if not made or not os.path.isfile(path) or not os.path.getsize(path):
        try:
            os.remove(path)
        except OSError:
            pass
        raise NewVideoError(f"{service} did not return any sound for this voice")
    return path


def write_script(choices: Mapping) -> str:
    """按主题写一份脚本，先给人看、给人改，再决定要不要开这条片子。

    和流水线里那一步用的是同一个调用、同一套提示词，所以在这里看到的脚本
    就是"留空让它自己写"会得到的那一种。
    """
    from app.services import llm

    wanted = dict(choices)
    if not str(wanted.get("video_subject") or "").strip():
        raise NewVideoError("say what the video is about first")
    # 只借用脚本那几项。来源要不要确认扣费、配音传没传，和写脚本无关。
    asked = {key: wanted.get(key) for key in (
        "video_subject", "video_language", "paragraph_number",
        "video_script_prompt", "custom_system_prompt", "llm_provider", "llm_model",
    )}
    asked.update(video_source=SOURCE_IDS[0], voice_mode=VOICE_NONE,
                 music_type=MUSIC_NONE)
    params = build_params(asked)
    script = llm.generate_script(
        video_subject=params.video_subject,
        language=params.video_language,
        paragraph_number=params.paragraph_number,
        video_script_prompt=params.video_script_prompt,
        custom_system_prompt=params.custom_system_prompt,
        **tm._llm_override_kwargs(params),
    )
    script = str(script or "").strip()
    if not script or script.startswith("Error: "):
        provider = str(params.llm_provider or config.app.get("llm_provider")
                       or "the language model")
        raise NewVideoError(
            f"{provider} did not write a script"
            + (f": {script[7:].strip()}" if script else "")
        )
    return script
