"""成片精修：在 ffmpeg 里把拼好的画面、旁白和配乐做成一条"像样"的片子。

这是什么
--------
``video.generate_video`` 的另一条路，签名一样、返回值的含义一样。任务参数里
``finish_look`` 不为空时，``task.generate_final_videos`` 走这里；为空时走原来
那条 MoviePy 的路，一个字节都不变。

它做的事（一条真片子上量出来的问题 → 对应的处理）：

* 旁白只有 -31 LUFS，YouTube 按 -14 播 → 高通、去浑、提亮、压缩，再把整条
  混音两遍 ``loudnorm`` 到 -14 LUFS；
* 句子之间是纯数字静音 → 配乐垫在旁白下面约 11 LU，旁白响的时候再往下压
  （sidechain），停顿里自己浮上来；
* 画面平、数字味重 → 调色、暗角、胶片颗粒；
* 结尾硬切 → 淡出；画面比旁白短 → 最后一帧定住，等旁白说完再淡；
* 字幕 → 写成 ASS 交给 libass 烧（``utils/ass_subtitles.py``），包括"念到
  哪个词哪个词变色"、左上角的标题卡和片名；
* 从头到尾一个样 → 分镜（``scene_plan.py``）里写的导演项：片名、标题卡、
  重音、"这几段不要配乐"、环境声（声音在 ``sound_design.py`` 里合成）。

每一项都有开关，在 ``config.toml`` 的 ``[finish]`` 里；没写的项用下面
``DEFAULTS`` 里的值。

A second way to make the final file, entirely in ffmpeg: voice EQ and
compression, music ducked under the narration, two-pass loudness to -14 LUFS,
a colour grade with vignette and grain, a fade out after the narrator has
finished, and subtitles burned from an ASS file. A scene plan's direction is
carried out here too: its title and cards, impacts, music dropouts and
ambience. Used when the task's ``finish_look`` is set; the MoviePy renderer is
untouched and remains what runs when it is empty.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import bgm as bgm_service
from app.services import scene_plan, sound_design
from app.services import transition_sound as transition_sound_service
from app.services.utils import ass_subtitles
from app.utils import file_security, utils
from app.utils.subtitle_reader import moviepy_subtitles

# 调色。"clean" 是什么都不调：只要声音、淡入淡出和字幕。
#
# cold_documentary 是在一条悬疑纪录片上调出来、用户看过说好的那一套：
# 略降饱和、暗部偏冷、亮部偏暖。
LOOKS: dict[str, dict[str, Any]] = {
    "clean": {"label": "Clean (no colour grade)", "grade": "", "texture": False},
    "cold_documentary": {
        "label": "Cold documentary",
        "grade": (
            "eq=contrast=1.05:saturation=0.82,"
            "colorbalance=rs=-0.03:bs=0.04:rm=-0.02:bm=0.02:rh=0.03:bh=-0.02"
        ),
        "texture": True,
    },
    "warm": {
        "label": "Warm",
        "grade": (
            "eq=contrast=1.04:saturation=1.04,"
            "colorbalance=rs=0.02:bs=-0.03:rm=0.02:bm=-0.02:rh=0.03:bh=-0.03"
        ),
        "texture": True,
    },
    "vivid": {
        "label": "Vivid",
        "grade": "eq=contrast=1.06:saturation=1.18",
        "texture": False,
    },
    "noir": {
        "label": "Black and white",
        "grade": "hue=s=0,eq=contrast=1.1",
        "texture": True,
    },
}

DEFAULTS: dict[str, Any] = {
    # 旁白的高通、均衡和压缩。
    "voice_processing": True,
    # 旁白响的时候把配乐往下压。
    "music_ducking": True,
    # 整条片子的响度（LUFS）。0 = 不做响度归一。
    "loudness": -14.0,
    # 胶片颗粒的强度，0 = 不加。只在带质感的调色上用。
    "grain": 5,
    "vignette": True,
    # 开头淡入、结尾淡出各多少秒，0 = 不要。开头默认不淡入：第一句是钩子，
    # 第一帧就该是亮的。
    "fade_in": 0.0,
    "fade_out": 2.0,
    # 让写脚本的模型挑三到五处，在左上角打出标题卡。分镜自己带了导演项的
    # 片子不问模型，照分镜来。
    "title_cards": True,
    # 分镜里写的重音、环境声、"这几段不要配乐"、片名：照着做。
    "sound_design": True,
    # 词的时间从哪来：cues = 按字幕文件摊；whisper = 再跑一遍语音识别对齐。
    "word_timing": "whisper",
    # "念到哪个词哪个词变色"那种字幕要不要全大写。
    "subtitle_uppercase": True,
    # 变色变成什么颜色；空 = 自己挑一个和字幕颜色分得开的。
    "highlight_color": "",
    "crf": 19,
    "preset": "medium",
}

# 旁白处理完之后的响度，配乐相对它的位置。
_VOICE_LUFS = -18.0
_MUSIC_BELOW_VOICE = 10.0
# ``bgm_volume`` 的这个值对应"比旁白低 10 LU"（句子之间浮上来的那个位置；
# 旁白响的时候还要再压七八个分贝）。
_REFERENCE_BGM_VOLUME = 0.2
# 滑块每变一个分贝，配乐只变这么多。照实算的话 5% 就是再低 12 dB，压完之后
# 比旁白低三十多 LU——实测就是听不见，整条片子等于没有配乐。
# How much of the slider's change in dB reaches the music. Taken at face value
# a 5% setting put the music 36 LU under the voice once ducked: inaudible.
_BGM_VOLUME_SLOPE = 0.3
# 片子的最后一帧最多定住多久，等旁白说完、配乐淡掉。
_MAX_END_HOLD_SECONDS = 6.0
_TITLE_CARD_SECONDS = 6.0
_TITLE_CARD_MIN_VIDEO_SECONDS = 60.0
_TITLE_CARDS_FILE = "title-cards.json"

_VOICE_CHAIN = (
    "highpass=f=70,"
    "equalizer=f=250:t=q:w=1:g=-2,"
    "equalizer=f=3500:t=q:w=1.2:g=2.5,"
    "acompressor=threshold=-24dB:ratio=3:attack=8:release=180:makeup=6dB"
)
_STEREO_48K = "aformat=sample_rates=48000:channel_layouts=stereo"


class FinishError(RuntimeError):
    """精修这一遍没做成。消息里带着 ffmpeg 自己说的那几句。"""


def look_of(params: Any) -> str:
    """这条片子要哪一种调色；空串 = 不走精修。认不出来的名字直接报错。"""
    look = str(getattr(params, "finish_look", "") or "").strip()
    if look and look not in LOOKS:
        raise FinishError(
            f"unknown finish look {look!r}; pick one of: {', '.join(LOOKS)} "
            "(or leave it empty for the classic render)"
        )
    return look


def wanted(params: Any) -> bool:
    return bool(look_of(params))


def setting(name: str) -> Any:
    """``[finish]`` 里的一项；没写、或者写坏了，用默认值。"""
    default = DEFAULTS[name]
    section = getattr(config, "finish", None) or {}
    value = section.get(name, default)
    try:
        if isinstance(default, bool):
            return value if isinstance(value, bool) else str(value).strip().lower() in (
                "1", "true", "yes", "on")
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        logger.warning(f"[finish] {name} = {value!r} is not usable; using {default!r}")
        return default
    return str(value)


def _ffmpeg() -> str:
    return utils.get_ffmpeg_binary()


@lru_cache(maxsize=8)
def _filters(binary: str) -> str:
    result = subprocess.run(
        [binary, "-hide_banner", "-filters"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=30,
    )
    return result.stdout or ""


def _has_filter(name: str) -> bool:
    return bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", _filters(_ffmpeg()), re.M))


def _run(arguments: Sequence[str], *, what: str, cwd: Optional[str] = None) -> str:
    """跑一次 ffmpeg，返回它写在 stderr 上的东西；失败就带着原话报错。"""
    command = [_ffmpeg(), "-hide_banner", "-nostdin", "-y", *arguments]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, cwd=cwd or None,
    )
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-1200:]
        raise FinishError(f"ffmpeg failed while {what}: {tail}")
    return result.stderr or ""


def _loudness_report(stderr: str) -> dict:
    """``loudnorm=print_format=json`` 写在 stderr 末尾的那一段。"""
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not found:
        raise FinishError("ffmpeg reported no loudness measurement")
    return json.loads(found[-1])


def _measure(path: str, chain: str = "", seconds: float = 0.0) -> float:
    """这个文件（过了 ``chain`` 之后）的整体响度，LUFS。静音返回 ``-inf``。"""
    filters = ",".join(part for part in (_STEREO_48K, chain,
                                         "loudnorm=print_format=json") if part)
    arguments = ["-i", path]
    if seconds > 0:
        arguments = ["-t", f"{seconds:.2f}", *arguments]
    report = _loudness_report(_run(
        [*arguments, "-vn", "-af", filters, "-f", "null", "-"],
        what=f"measuring {os.path.basename(path)}",
    ))
    try:
        return float(report.get("input_i"))
    except (TypeError, ValueError):
        return float("-inf")


def _decibels(value: float) -> str:
    return f"{max(-60.0, min(40.0, value)):.2f}dB"


def _quoted(path: str) -> str:
    """滤镜参数里的一个路径：正斜杠，冒号转义，外面包单引号。"""
    return "'" + str(path).replace("\\", "/").replace(":", "\\:").replace("'", "") + "'"


def _music_file(params: VideoParams, override: Optional[str]) -> str:
    """和原来那条路同一套规则：``None`` 照参数解析，``""`` 就是不要。"""
    if not bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
        if params.bgm_type:
            logger.info(
                "skipping background music because volume is not positive: "
                f"type={params.bgm_type}, volume={params.bgm_volume}"
            )
        return ""
    if override is not None:
        return override
    from app.services import video as video_service

    return video_service.get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file)


def _cut_sounds(params: VideoParams, times, names) -> dict[str, List[float]]:
    if not times or float(params.transition_sound_volume or 0) <= 0:
        return {}
    from app.services import video as video_service

    video_wide = (
        str(params.transition_sound or "")
        if transition_sound_service.should_use_transition_sound(
            params.transition_sound, params.transition_sound_volume)
        else ""
    )
    return video_service._resolve_cut_sounds(
        list(times), list(names) if names else None, video_wide)


def mix_graph(
    *,
    duration: float,
    voice_gain: float,
    music_gain: Optional[float],
    cut_sounds: Sequence[Tuple[int, Sequence[float]]] = (),
    cut_volume: float = 0.0,
    voice_processing: bool = True,
    ducking: bool = True,
    music_off: Sequence[Tuple[float, float]] = (),
    hits: Optional[Tuple[int, Sequence[float]]] = None,
    beds: Sequence[Tuple[int, Sequence[Tuple[float, float]]]] = (),
) -> str:
    """混音那张滤镜图。输入 0 是旁白，1 是配乐（有的话），后面是转场音效。

    ``music_off`` 是配乐让开的那几段；``hits`` 是重音那个输入和它落在哪几秒；
    ``beds`` 是每一种环境声的输入（循环着读）和它响的那几段。

    出来两路一样的：``[mix]`` 写成文件，``[meter]`` 只是量响度。
    """
    voice = [_STEREO_48K]
    if voice_processing:
        voice.append(_VOICE_CHAIN)
    voice.append(f"volume={_decibels(voice_gain)}")
    # 垫到整条片子那么长：让位那一节是跟着旁白走的，旁白一结束它就停，
    # 配乐会跟着在最后一个字上被掐掉，而不是淡出去。
    # Padded to the full length: the ducker ends with its key, which cut the
    # music off on the narrator's last word instead of letting it fade.
    voice.append(f"apad=whole_dur={duration:.3f}")
    parts: List[str] = []
    layers = ["[voice]"]
    if music_gain is None:
        parts.append(f"[0:a]{','.join(voice)}[voice]")
    else:
        parts.append(f"[0:a]{','.join(voice)},asplit=2[voice][key]")
        music = [_STEREO_48K, f"volume={_decibels(music_gain)}"]
        riding = sound_design.music_filter(music_off)
        if riding:
            music.append(riding)
        # 淡的是整条时间线的结尾，不是每一遍循环的结尾。
        fade = min(3.0, duration / 2)
        music.append(f"atrim=duration={duration:.3f}")
        music.append(f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}")
        parts.append(f"[1:a]{','.join(music)}[bed]")
        if ducking:
            # 压七八个分贝：旁白底下还听得见有音乐，停顿里它自己浮上来。
            parts.append(
                "[bed][key]sidechaincompress=threshold=0.05:ratio=4:attack=40:"
                "release=700[music]"
            )
        else:
            parts.append("[key]anullsink")
            parts.append("[bed]anull[music]")
        layers.append("[music]")
    for input_index, windows in beds:
        if not windows:
            continue
        parts.append(
            f"[{input_index}:a]{_STEREO_48K},{sound_design.bed_filter(windows)},"
            f"atrim=duration={duration:.3f}[amb{input_index}]"
        )
        layers.append(f"[amb{input_index}]")
    placed = [(index, times, max(0.0, cut_volume)) for index, times in cut_sounds]
    if hits and hits[1]:
        placed.append((hits[0], hits[1], 1.0))
    for input_index, times, volume in placed:
        names = [f"[cut{input_index}x{n}]" for n in range(len(times))]
        parts.append(
            f"[{input_index}:a]{_STEREO_48K},volume={volume:.3f},"
            f"asplit={len(times)}{''.join(f'[raw{input_index}x{n}]' for n in range(len(times)))}"
        )
        for n, at in enumerate(times):
            parts.append(
                f"[raw{input_index}x{n}]adelay={max(0, int(round(at * 1000)))}:all=1"
                f"{names[n]}"
            )
        layers.extend(names)
    mixed = "".join(layers)
    # normalize=0：amix 默认会把每一路除以路数，旁白就又小下去了。
    parts.append(
        f"{mixed}amix=inputs={len(layers)}:normalize=0:duration=longest,"
        f"apad,atrim=duration={duration:.3f},asplit=2[mix][meter_in]"
    )
    parts.append("[meter_in]loudnorm=print_format=json[meter]")
    return ";".join(parts)


def picture_filters(
    *, look: str, duration: float, subtitle_file: str = "", fonts_dir: str = "",
    hold: float = 0.0,
) -> str:
    """画面那一串滤镜：调色、暗角、颗粒、字幕、淡入淡出。

    ``hold`` 是最后一帧要定住多少秒：画面比旁白短的时候，等旁白说完再淡出，
    而不是把最后几个字一起淡掉。``duration`` 是定住之后的总长。
    """
    spec = LOOKS[look]
    chain: List[str] = []
    if hold > 0.05:
        # 多垫半秒，由编码时的 -t 截齐：宁可多一帧，不能少一帧露出黑。
        chain.append(f"tpad=stop_mode=clone:stop_duration={hold + 0.5:.3f}")
    if spec["grade"]:
        chain.append(spec["grade"])
    if spec["texture"]:
        if setting("vignette"):
            # PI/4.5 实测太暗。
            chain.append("vignette=angle=PI/7")
        grain = setting("grain")
        if grain > 0:
            chain.append(f"noise=c0s={min(grain, 40)}:c0f=t+u")
    if subtitle_file:
        fonts = f":fontsdir={_quoted(fonts_dir)}" if fonts_dir else ""
        chain.append(f"ass={_quoted(subtitle_file)}{fonts}")
    fade_in, fade_out = setting("fade_in"), setting("fade_out")
    if fade_in > 0 and duration > fade_in * 2:
        chain.append(f"fade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0 and duration > fade_out * 2:
        chain.append(f"fade=t=out:st={duration - fade_out:.3f}:d={fade_out:.2f}")
    from app.services import video as video_service

    chain.append(video_service._BT709_VIDEO_FILTER)
    chain.append("format=yuv420p")
    return ",".join(chain)


def _font_family(font_path: str) -> str:
    from PIL import ImageFont

    try:
        return str(ImageFont.truetype(font_path, 20).getname()[0])
    except Exception as exc:
        raise FinishError(
            f"the subtitle font cannot be read: {os.path.basename(font_path)} ({exc})"
        ) from exc


def _contrast_colour(colour: str) -> str:
    """字幕是白的就用黄的点亮，字幕本身就是暖色就用白的。"""
    text = str(colour or "").strip().lstrip("#")
    try:
        red, green, blue = (int(text[at:at + 2], 16) for at in (0, 2, 4))
    except ValueError:
        return "#FFD73F"
    return "#FFD73F" if blue > 150 else "#FFFFFF"


def _card_font() -> Tuple[str, int]:
    """标题卡和片名用什么字体、横向压到百分之几。

    Windows 自带的 Bahnschrift 是 DIN 那一路的字，压到 78% 就是纪录片里常见的
    窄体标题字。别的系统上没有它：用字幕的字体，不压。
    """
    windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or ""
    if windows and os.path.isfile(os.path.join(windows, "Fonts", "bahnschrift.ttf")):
        return "Bahnschrift", 78
    return "", 100


def subtitle_style(params: VideoParams) -> ass_subtitles.Style:
    """把任务里的字幕设置翻成 ASS 的样式。"""
    font_name = params.font_name or "STHeitiMedium.ttc"
    font_path = file_security.resolve_path_within_directory(utils.font_dir(), font_name)
    mode = str(getattr(params, "subtitle_display_mode", "sentence") or "sentence")
    background = params.text_background_color
    box = ""
    if isinstance(background, str) and background.strip():
        box = background.strip()
    elif background is True:
        box = "#000000"
    highlight = str(setting("highlight_color") or "").strip()
    card_font, card_squeeze = _card_font()
    return ass_subtitles.Style(
        card_font=card_font,
        card_squeeze=card_squeeze,
        font=_font_family(font_path),
        # libass 的字号是"上沿到下沿"，PIL 的是 em：同一个数字看起来小一圈。
        size=round(float(params.font_size or 60) * 1.12, 1),
        color=params.text_fore_color or "#FFFFFF",
        outline_color=params.stroke_color or "#000000",
        outline=float(params.stroke_width or 0),
        shadow=0 if box else 3,
        box_color=box,
        position=str(params.subtitle_position or "bottom"),
        custom_position=float(params.custom_position or 70.0),
        highlight_color=highlight or _contrast_colour(params.text_fore_color or "#FFFFFF"),
        uppercase=mode == "highlight" and setting("subtitle_uppercase"),
        pop=mode == "highlight"
        or str(getattr(params, "subtitle_animation", "none") or "none") != "none",
    )


def _spoken_words(audio_path: str) -> List[Tuple[float, float, str]]:
    """跑一遍 whisper，要每个词的起止时间。跑不了就报错，不悄悄换办法。"""
    from app.services import subtitle as whisper

    if whisper.WhisperModel is None or not whisper._ensure_model_loaded():
        raise FinishError(
            "[finish] word_timing is \"whisper\" but the whisper model cannot be "
            "loaded; set it to \"cues\" or fix the [whisper] settings"
        )
    segments, _ = whisper.model.transcribe(
        audio_path, beam_size=5, word_timestamps=True, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    spoken = []
    for segment in segments:
        for word in segment.words or []:
            if word.word.strip():
                spoken.append((float(word.start), float(word.end), word.word.strip()))
    return spoken


_SPOKEN_WORDS_FILE = "spoken-words.json"


def _spoken_words_once(audio_path: str, task_dir: str) -> List[Tuple[float, float, str]]:
    """同一条配音只听一遍：结果存在任务目录里，配音文件变了才重听。

    Recognition takes a minute or two for a long narration; the result is kept
    beside the video and reused until the audio file changes.
    """
    # 编辑器的预览和出片可能同时要这份结果：排队，后到的那个等前一个听完，
    # 直接读它存下的，不再听第二遍。
    # 已经听过的不排队：不然别的片子正在听的那几分钟里，这一条明明有现成的
    # 结果，编辑器的请求也得跟着等。
    saved = _saved_spoken_words(audio_path, task_dir)
    if saved is not None:
        return saved
    with _LISTENING:
        saved = _saved_spoken_words(audio_path, task_dir)
        if saved is not None:
            return saved
        logger.info("timing the subtitle words against the narration (whisper)")
        spoken = _spoken_words(audio_path)
        target = os.path.join(task_dir, _SPOKEN_WORDS_FILE)
        partial = f"{target}.{os.getpid()}.{threading.get_ident()}.part"
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump({"audio": _audio_stamp(audio_path), "words": spoken}, handle,
                      ensure_ascii=False)
        os.replace(partial, target)
        return spoken


_LISTENING = threading.Lock()


def _audio_stamp(audio_path: str) -> list:
    stat = os.stat(audio_path)
    return [int(stat.st_size), int(stat.st_mtime)]


def _saved_spoken_words(
    audio_path: str, task_dir: str,
) -> Optional[List[Tuple[float, float, str]]]:
    """这条配音已经听过的结果；没听过、或者配音换过了，是 ``None``。"""
    try:
        with open(os.path.join(task_dir, _SPOKEN_WORDS_FILE), encoding="utf-8") as handle:
            saved = json.load(handle)
        if saved.get("audio") == _audio_stamp(audio_path):
            return [(float(a), float(b), str(c)) for a, b, c in saved["words"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _script_of(task_dir: str, params: VideoParams) -> Tuple[str, str]:
    """``(题目, 脚本)``。任务目录里的清单比参数里的新。"""
    subject = str(getattr(params, "video_subject", "") or "")
    script = str(getattr(params, "video_script", "") or "")
    manifest = os.path.join(task_dir, "script.json")
    try:
        with open(manifest, encoding="utf-8") as handle:
            saved = json.load(handle)
        script = str(saved.get("script") or script)
        subject = str((saved.get("params") or {}).get("video_subject") or subject)
    except (OSError, ValueError):
        pass
    return subject, script


@dataclass
class Direction:
    """分镜里的导演项，换算成了成片上的秒数。什么都没写就是全空的。"""

    # 这条片子的导演是不是分镜管的：是的话标题卡照它来，不问模型。
    planned: bool = False
    title: Optional[ass_subtitles.Title] = None
    # ``(几秒, 大字, 小字)``
    cards: List[Tuple[float, str, str]] = field(default_factory=list)
    hits: List[float] = field(default_factory=list)
    music_off: List[Tuple[float, float]] = field(default_factory=list)
    beds: dict = field(default_factory=dict)
    timelines: List[ass_subtitles.Timeline] = field(default_factory=list)

    @property
    def sounds(self) -> bool:
        return bool(self.hits or self.music_off or any(self.beds.values()))


def _plan_scenes(task_dir: str, params: VideoParams) -> List[dict]:
    """这条片子的分镜（没有就是空的）。任务目录里的清单比参数里的新。"""
    scenes = getattr(params, "scene_plan", None)
    try:
        with open(os.path.join(task_dir, "script.json"), encoding="utf-8") as handle:
            saved = (json.load(handle).get("params") or {}).get("scene_plan")
        if saved:
            scenes = saved
    except (OSError, ValueError, AttributeError):
        pass
    return [scene for scene in scenes or [] if isinstance(scene, dict)]


def direct(
    scenes: Sequence[dict], words: Sequence[ass_subtitles.Word], duration: float,
) -> Direction:
    """把分镜每一段的导演项落到时间上。

    每一段靠它自己的旁白在念出来的词里找位置，所以在编辑器里挪过、拆过画面
    都不影响；旁白被改掉、找不到的那一段，它的导演项不做，并且在日志里说。

    Places each scene's direction in time by finding its narration among the
    spoken words. A scene whose narration is no longer spoken is skipped and
    named in the log.
    """
    if not scene_plan.directed(scenes):
        return Direction()
    found = Direction(planned=True)
    if not words:
        return found
    places = ass_subtitles.locate_lines(
        words, [str(scene.get("narration") or "") for scene in scenes])
    sound = setting("sound_design")
    found.timelines = _timelines(scenes, places, duration)
    for number, (scene, place) in enumerate(zip(scenes, places), start=1):
        wanted = scene.get("direction") or {}
        if not wanted:
            continue
        if place is None:
            logger.warning(
                f"scene {number}'s direction ({', '.join(wanted)}) is not carried "
                "out: its narration is not in the recording any more"
            )
            continue
        start, end = place
        card = wanted.get("card")
        if isinstance(card, dict) and card.get("title"):
            found.cards.append(
                (start, str(card["title"]), str(card.get("subtitle") or "")))
        title = wanted.get("title")
        if isinstance(title, dict) and title.get("text"):
            found.title = ass_subtitles.Title(
                max(0.0, start - 0.15), min(end + 0.55, duration - 2.5),
                str(title["text"]), str(title.get("tagline") or ""))
            if sound:
                found.hits.append(max(0.0, start - 0.1))
        if not sound:
            continue
        if wanted.get("impact") == "start":
            found.hits.append(max(0.0, start - 0.1))
        elif wanted.get("impact") == "end":
            found.hits.append(end + 0.05)
        if wanted.get("music") == "off":
            found.music_off.append((start, end))
        if wanted.get("ambience") in sound_design.BED_RMS_DB:
            found.beds.setdefault(wanted["ambience"], []).append((start, end))
    # 片名和"这一句开头落一记"会落在同一处：只落一记，不然就是两倍响。
    hits: List[float] = []
    for at in sorted(at for at in found.hits if at < duration - 1.0):
        if not hits or at - hits[-1] > 0.2:
            hits.append(at)
    found.hits = hits
    found.music_off = sound_design.merged(found.music_off, gap=4.0)
    found.beds = {kind: sound_design.merged(windows, gap=2.0)
                  for kind, windows in found.beds.items()}
    return found


def _timelines(
    scenes: Sequence[dict], places: Sequence[Optional[Tuple[float, float]]],
    duration: float,
) -> List[ass_subtitles.Timeline]:
    """The plan's timeline dates, grouped into the strips that show them.

    A strip opens a moment before its first event is spoken and stays through
    every scene after it, dated or not, until the end of a scene marked
    ``"end"`` - or of its last event's scene when none is. Each strip has its
    own axis, from its earliest date to its latest, and an event sits on it
    where its date falls.
    """
    found: List[ass_subtitles.Timeline] = []
    events: List[Tuple[float, float, dict, float]] = []

    def close(until: float) -> None:
        if not events:
            return
        years = [when for _, _, _, when in events]
        low, high = min(years), max(years)
        start = max(0.0, events[0][0] - 0.3)
        end = min(until, duration - 2.5)
        if end - start >= 2.0:
            found.append(ass_subtitles.Timeline(
                start, end,
                [ass_subtitles.TimelineEvent(
                    at, str(event.get("date") or ""), str(event.get("label") or ""),
                    (when - low) / (high - low) if high > low else 0.5)
                 for at, _, event, when in events if at < end],
                str(int(low)), str(int(high)) if int(high) != int(low) else ""))
        events.clear()

    for scene, place in zip(scenes, places):
        event = (scene.get("direction") or {}).get("timeline")
        if not isinstance(event, dict) or place is None:
            continue
        start, end = place
        when = scene_plan.timeline_when(event.get("date"))
        if when is not None:
            events.append((start, end, event, when))
        if event.get("end"):
            close(end + 0.4)
    if events:
        close(events[-1][1] + 1.0)
    return found


def title_cards(
    task_dir: str, params: VideoParams, words: Sequence[ass_subtitles.Word],
    duration: float, planned: Optional[Sequence[Tuple[float, str, str]]] = None,
    ask: bool = True,
) -> List[ass_subtitles.TitleCard]:
    """这条片子的标题卡。

    分镜自己管导演的片子（``planned`` 不是 ``None``）：照分镜写的那几张来，
    不问模型。

    其余的，挑哪几处是模型定的，定完存在任务目录的 ``title-cards.json`` 里：
    以后每次重新出片用的都是同一份，想改就改这个文件（删掉它就重新挑一遍，
    写成 ``[]`` 就是不要）。每一张都要在旁白里找到它那句话才显示——找不到的
    那张不显示，并且在日志里说是哪一张。这个文件在的时候，它说了算，分镜里
    的也让它。
    """
    if not setting("title_cards") or not words:
        return []
    saved_path = os.path.join(task_dir, _TITLE_CARDS_FILE)
    chosen: Optional[list] = None
    if os.path.isfile(saved_path):
        try:
            with open(saved_path, encoding="utf-8") as handle:
                chosen = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(f"{_TITLE_CARDS_FILE} cannot be read and is ignored: {exc}")
            return []
    elif planned is not None:
        return _placed_cards(
            [(start, {"title": title, "subtitle": subtitle})
             for start, title, subtitle in planned], duration)
    elif not ask:
        # 编辑器的预览：还没挑过就是还没有，不为了看一眼去问模型。
        return []
    elif duration >= _TITLE_CARD_MIN_VIDEO_SECONDS:
        subject, script = _script_of(task_dir, params)
        if not script.strip():
            return []
        from app.services import llm

        try:
            chosen = llm.generate_title_cards(subject, script)
        except Exception as exc:
            # 标题卡是锦上添花：模型没答上来不该让一条已经渲到最后一步的片子
            # 失败。不存文件，下次出片再问一次。
            logger.warning(f"no title cards this time, the model did not answer: {exc}")
            return []
        with open(saved_path, "w", encoding="utf-8") as handle:
            json.dump(chosen, handle, ensure_ascii=False, indent=2)
        logger.info(f"chose {len(chosen)} title cards, saved to {_TITLE_CARDS_FILE}")
    found: List[Tuple[float, dict]] = []
    for entry in chosen or []:
        if not isinstance(entry, dict):
            continue
        start = ass_subtitles.find_phrase(words, str(entry.get("phrase") or ""))
        if start is None:
            logger.warning(
                f"title card {entry.get('title')!r} is not shown: its phrase "
                f"{entry.get('phrase')!r} is not in the narration"
            )
            continue
        found.append((start, entry))
    return _placed_cards(found, duration)


def _placed_cards(
    found: List[Tuple[float, dict]], duration: float,
) -> List[ass_subtitles.TitleCard]:
    """``(几秒, {"title", "subtitle"})`` 排成不互相叠着的标题卡。"""
    # 给的顺序不一定是念到的顺序：先按时间排，再判断叠不叠。不排的话，
    # 排在后面、念得却更早的那张会被当成"和上一张叠了"悄悄丢掉。
    found = sorted(found, key=lambda item: item[0])
    cards: List[ass_subtitles.TitleCard] = []
    for start, entry in found:
        # 两张不叠在一起，也不拖到片尾的淡出里。
        if cards and start < cards[-1].end + 1.0:
            logger.warning(
                f"title card {entry.get('title')!r} is not shown: it would overlap "
                f"{cards[-1].title!r}"
            )
            continue
        end = min(start + _TITLE_CARD_SECONDS, duration - 2.5)
        if end - start < 2.0:
            continue
        cards.append(ass_subtitles.TitleCard(
            start, end, str(entry.get("title") or ""), str(entry.get("subtitle") or "")))
    return cards


def _strict_captions(params: VideoParams, cues: Sequence) -> bool:
    """字幕是不是"念到哪个词哪个词变色"那一类：它们非要真的词时间不可。"""
    mode = str(getattr(params, "subtitle_display_mode", "sentence") or "sentence")
    return bool(params.subtitle_enabled) and bool(cues) and mode != "sentence"


def narration_words(
    params: VideoParams, subtitle_path: str, audio_path: str, task_dir: str,
    precise: bool = False, listen: bool = True,
) -> Tuple[list, List[ass_subtitles.Word]]:
    """``(字幕, 带时间的词)``。

    词的时间先从字幕里摊；"念到哪个词哪个词变色"的字幕、以及分镜带着导演项
    的片子（``precise``），再拿语音识别对一遍——很多配音不给每个词的时间，
    字幕里的时间是按字数估的，实测能差到两秒，重音落在那上面就是错的。

    识别跑不了的时候：变色字幕照旧报错（它没有别的办法）；导演项退回估的
    时间，在日志里说一声。
    """
    cues = []
    if subtitle_path and os.path.exists(subtitle_path):
        cues = moviepy_subtitles(subtitle_path)
    words = ass_subtitles.words_from_cues(cues)
    strict = _strict_captions(params, cues)
    if listen and words and (strict or precise) and setting("word_timing") == "whisper":
        try:
            words = ass_subtitles.align_spoken_words(
                words, _spoken_words_once(audio_path, task_dir))
        except FinishError as exc:
            if strict:
                raise
            logger.warning(
                f"the direction is timed from the subtitle estimates, not the "
                f"recording: {exc}"
            )
    return cues, words


def waits_for_listening(
    params: VideoParams, subtitle_path: str, audio_path: str, task_dir: str,
) -> bool:
    """编辑器要把这条片子的导演项摆对位置，是不是得先听一遍配音。

    听一遍要几分钟，所以编辑器不在请求里做：这里说"要"，它就放到后台去听
    （:func:`listen`），听完再来拿。已经听过的、用不着听的，都是"不要"。
    """
    if setting("word_timing") != "whisper":
        return False
    if not (subtitle_path and os.path.exists(subtitle_path)):
        return False
    directed = scene_plan.directed(_plan_scenes(task_dir, params))
    chosen = os.path.isfile(os.path.join(task_dir, _TITLE_CARDS_FILE))
    if not directed and not (
            chosen and _strict_captions(params, moviepy_subtitles(subtitle_path))):
        return False
    return _saved_spoken_words(audio_path, task_dir) is None


def listen(audio_path: str, task_dir: str) -> None:
    """把配音听一遍、存下每个词的时间。出片时用的就是这一份，不再听第二遍。"""
    _spoken_words_once(audio_path, task_dir)


def editor_direction(
    params: VideoParams, subtitle_path: str, audio_path: str, task_dir: str,
    voice_seconds: float,
) -> dict:
    """成片里会加上去的片名、标题卡和声音，各在第几秒——给编辑器预览用。

    和 :func:`render` 走的是同一套：同一份分镜、同一份词时间、同一个
    :func:`direct`。两处不一样：这里不问模型要标题卡（还没挑过就是没有），
    片长按旁白算（成片的画面还没合出来）。这里**从不**去听配音（那要几分钟，
    见 :func:`waits_for_listening`）：听过就用听到的，没听过位置照字幕估的
    时间摆，返回里的 ``timing`` 说是哪一种。

    What the finished video will add and when, for the editor's preview. The
    same plan, word timings and :func:`direct` as the render.
    """
    scenes = _plan_scenes(task_dir, params)
    directed = scene_plan.directed(scenes)
    fade_out = setting("fade_out")
    duration = max(0.0, float(voice_seconds)) + (fade_out if fade_out > 0 else 0.3)
    heard = (
        setting("word_timing") == "whisper"
        and _saved_spoken_words(audio_path, task_dir) is not None
    )
    cues, words = narration_words(
        params, subtitle_path, audio_path, task_dir, precise=directed, listen=heard)
    # 听过不等于用了：只有带导演项的分镜和逐词变色的字幕才照听到的时间摆。
    heard = heard and bool(words) and (directed or _strict_captions(params, cues))
    direction = direct(scenes, words, duration)
    cards = title_cards(
        task_dir, params, words, duration,
        planned=direction.cards if direction.planned else None, ask=False)
    font, squeeze = _card_font()
    title = direction.title
    if title is not None and title.end - title.start < 1.0:
        title = None
    top = False
    if direction.timelines and params.subtitle_enabled and cues:
        try:
            top = ass_subtitles.timeline_on_top(subtitle_style(params), True)
        except Exception as exc:  # a missing caption font is the render's to report
            logger.warning(f"the timeline is previewed at the bottom: {exc}")
    return {
        "planned": direction.planned,
        "timing": "recording" if heard else "estimate",
        "duration": round(duration, 3),
        "title": None if title is None else {
            "start": round(title.start, 3), "end": round(title.end, 3),
            "text": title.text, "tagline": title.tagline,
        },
        "cards": [
            {"start": round(card.start, 3), "end": round(card.end, 3),
             "title": card.title, "subtitle": card.subtitle}
            for card in cards
        ],
        "hits": [round(at, 3) for at in direction.hits],
        "hit_seconds": sound_design.HIT_SECONDS,
        "music_off": _windows(direction.music_off),
        "music": {
            "ramps": _windows(sound_design.ramps(
                direction.music_off, sound_design.MUSIC_GATE["fall"],
                sound_design.MUSIC_GATE["lead"], sound_design.MUSIC_GATE["hold"])),
            "rise": sound_design.MUSIC_GATE["rise"],
            "fall": sound_design.MUSIC_GATE["fall"],
            "floor_db": sound_design.MUSIC_OFF_DB,
        },
        "beds": [
            {
                "kind": kind,
                "windows": _windows(windows),
                "ramps": _windows(sound_design.ramps(
                    windows, sound_design.BED_GATE["fall"],
                    sound_design.BED_GATE["lead"], sound_design.BED_GATE["hold"])),
                "rise": sound_design.BED_GATE["rise"],
                "fall": sound_design.BED_GATE["fall"],
            }
            for kind, windows in sorted(direction.beds.items()) if windows
        ],
        "card_font": font,
        "card_squeeze": squeeze,
        "timelines": [
            {
                "start": round(line.start, 3), "end": round(line.end, 3),
                "first": line.first, "last": line.last,
                "events": [
                    {"at": round(event.at, 3), "date": event.date,
                     "label": event.label, "place": round(event.place, 4)}
                    for event in sorted(line.events, key=lambda event: event.at)
                ],
            }
            for line in direction.timelines
        ],
        # The same numbers the render draws the strip from.
        "timeline": {
            "top": top,
            "holes": _windows(ass_subtitles.timeline_holes(
                title, cards, top, direction.timelines)),
            "edge": ass_subtitles.TIMELINE_EDGE,
            "from_edge": (ass_subtitles.TIMELINE_FROM_TOP if top
                          else ass_subtitles.TIMELINE_FROM_BOTTOM),
            "move": ass_subtitles.TIMELINE_MOVE_SECONDS,
            "label_seconds": ass_subtitles.TIMELINE_LABEL_SECONDS,
            "fade": ass_subtitles.TIMELINE_FADE_SECONDS,
        },
    }


def _windows(windows: Sequence[Tuple[float, float]]) -> List[List[float]]:
    return [[round(float(a), 3), round(float(b), 3)] for a, b in windows]


def write_subtitles(
    params: VideoParams, subtitle_path: str, audio_path: str, output_file: str,
    width: int, height: int, duration: float,
    timed: Optional[Tuple[list, List[ass_subtitles.Word]]] = None,
    direction: Optional[Direction] = None,
) -> str:
    """把要烧进画面的字写成 ASS，返回文件路径；没有要烧的返回空串。

    ``timed`` 是 :func:`narration_words` 已经算好的那一对（不给就在这里算）；
    ``direction`` 是分镜的导演项：片名，和它自己定的标题卡。
    """
    task_dir = os.path.dirname(output_file)
    cues, words = timed or narration_words(params, subtitle_path, audio_path, task_dir)
    mode = str(getattr(params, "subtitle_display_mode", "sentence") or "sentence")
    captions = bool(params.subtitle_enabled) and bool(cues)
    direction = direction or Direction()
    cards = title_cards(
        task_dir, params, words, duration,
        planned=direction.cards if direction.planned else None)
    if not captions and not cards and direction.title is None and not direction.timelines:
        return ""
    style = subtitle_style(params)
    content = ass_subtitles.build(
        cues if captions else [], width=width, height=height, style=style, mode=mode,
        max_lines=int(getattr(params, "subtitle_max_lines", 0) or 0),
        words=words, title_cards=cards, title=direction.title,
        timelines=direction.timelines,
    )
    target = os.path.splitext(output_file)[0] + ".ass"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(content)
    return target


def _duration_of(path: str) -> float:
    from app.services import timeline

    return float(timeline.probe_duration(path))


def render(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    bgm_file_override: Optional[str] = None,
    transition_sound_times: Optional[List[float]] = None,
    transition_sound_names: Optional[List[str]] = None,
) -> bool:
    """出成片。返回值和 ``video.generate_video`` 一样：配乐有没有混成。"""
    look = look_of(params)
    if not look:
        raise FinishError("finishing.render was called for a task with no finish look")
    from app.services import video as video_service

    width, height = VideoAspect(params.video_aspect).to_resolution()
    picture_seconds = _duration_of(video_path)
    # 画面常常比旁白短一点（实测短过 0.8 秒），结尾的淡出又有两秒：最后一句
    # 就被淡掉了。让最后一帧定住，等旁白说完再淡。
    # The picture is often a little shorter than the narration, and the fade
    # out then swallows the last sentence. Hold the last frame until the
    # narrator has finished, then fade.
    from app.services import voice as voice_service

    fade_out = setting("fade_out")
    try:
        voice_seconds = float(voice_service.get_audio_duration(audio_path) or 0.0)
    except Exception:
        voice_seconds = 0.0
    duration = min(
        max(picture_seconds, voice_seconds + (fade_out if fade_out > 0 else 0.3)),
        picture_seconds + _MAX_END_HOLD_SECONDS,
    )
    hold = duration - picture_seconds
    output_dir = os.path.dirname(os.path.abspath(output_file))
    logger.info(f"finishing video: {width} x {height}, look: {look}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    voice_processing = setting("voice_processing")
    voice_chain = _VOICE_CHAIN if voice_processing else ""
    voice_level = _measure(audio_path, voice_chain)
    voice_gain = (_VOICE_LUFS - voice_level) if math.isfinite(voice_level) else 0.0
    logger.info(
        f"  ⑤ narration: {voice_level:.1f} LUFS, gain {voice_gain:+.1f} dB"
    )

    # 配乐。原来那条路的约定：配乐读不出来不让整条片子失败，返回 False，由
    # 任务那一层给用户一句警告。
    music = _music_file(params, bgm_file_override)
    music_gain: Optional[float] = None
    music_ok = True
    if music:
        try:
            music_level = _measure(music, seconds=120.0)
            if not math.isfinite(music_level):
                raise FinishError("the track is silent")
            volume = max(float(params.bgm_volume or 0.0), 1e-3)
            target = (
                _VOICE_LUFS - _MUSIC_BELOW_VOICE
                + _BGM_VOLUME_SLOPE * 20.0 * math.log10(volume / _REFERENCE_BGM_VOLUME)
            )
            music_gain = target - music_level
            logger.info(
                f"  ⑥ music: {os.path.basename(music)}, {music_level:.1f} LUFS, "
                f"gain {music_gain:+.1f} dB"
            )
        except Exception:
            music_ok = False
            music = ""
            logger.exception(
                f"failed to mix background music: type={params.bgm_type}, file={music}"
            )

    sounds = _cut_sounds(params, transition_sound_times, transition_sound_names)
    inputs = ["-i", audio_path]
    if music:
        # 配乐由提供商生成时已经是整条片子那么长；自己挑的要循环铺满。
        if bgm_file_override is None:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-i", music]
    for sound_file in sounds:
        inputs += ["-i", sound_file]
    # 输入 0 是旁白，有配乐的话 1 是配乐，音效排在它们后面。
    cut_inputs = [
        ((2 if music else 1) + position, times)
        for position, times in enumerate(sounds.values())
    ]

    def temporary(prefix: str, suffix: str) -> str:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=output_dir)
        os.close(descriptor)
        return name

    mix_file = temporary(".finish-mix-", ".wav")
    temp_output = temporary(".finish-", ".mp4")
    made: List[str] = []
    subtitle_file = ""
    try:
        # 分镜的导演项：先把每一段落到时间上，声音和画面都要用。
        timed = narration_words(
            params, subtitle_path, audio_path, output_dir,
            precise=scene_plan.directed(_plan_scenes(output_dir, params)))
        direction = direct(_plan_scenes(output_dir, params), timed[1], duration)
        if direction.planned:
            logger.info(
                f"  ⑥ʹ direction from the scene plan: "
                f"{'a title, ' if direction.title else ''}{len(direction.cards)} cards, "
                f"{len(direction.hits)} impacts, music off {len(direction.music_off)} "
                f"times, ambience under "
                f"{sum(len(found) for found in direction.beds.values())} stretches"
            )
        next_input = inputs.count("-i")
        hit_input = None
        if direction.hits:
            made.append(sound_design.write_wav(
                temporary(".finish-hit-", ".wav"), sound_design.hit()))
            inputs += ["-i", made[-1]]
            hit_input = (next_input, direction.hits)
            next_input += 1
        bed_inputs = []
        for kind, windows in direction.beds.items():
            if not windows:
                continue
            made.append(sound_design.write_wav(
                temporary(f".finish-{kind}-", ".wav"), sound_design.bed(kind)))
            inputs += ["-stream_loop", "-1", "-i", made[-1]]
            bed_inputs.append((next_input, windows))
            next_input += 1
        graph = mix_graph(
            duration=duration, voice_gain=voice_gain, music_gain=music_gain,
            cut_sounds=cut_inputs,
            cut_volume=float(params.transition_sound_volume or 0.0),
            voice_processing=voice_processing, ducking=setting("music_ducking"),
            music_off=direction.music_off if music else (),
            hits=hit_input, beds=bed_inputs,
        )
        measured = _loudness_report(_run(
            [*inputs, "-filter_complex", graph, "-map", "[mix]", "-c:a", "pcm_s24le",
             mix_file, "-map", "[meter]", "-f", "null", "-"],
            what="mixing the narration and the music",
        ))

        audio_chain: List[str] = []
        target = setting("loudness")
        try:
            mixed_level = float(measured.get("input_i"))
        except (TypeError, ValueError):
            mixed_level = float("-inf")
        if target < 0 and not (math.isfinite(mixed_level) and mixed_level > -70.0):
            # 没有旁白也没有配乐：没有东西可以归一。
            logger.info("  ⑦ mix: silent, loudness is left alone")
        elif target < 0:
            # 第二遍：一个固定的增益，加一个只管峰值的限幅器。不用 loudnorm 的
            # 第二遍——压过的旁白峰值高，它的线性模式推不上去，就会悄悄换成
            # 动态模式，每一记重音之后都在拉音量。
            # A fixed gain and a peak limiter. loudnorm's second pass cannot push
            # a compressed voice this far in its linear mode, and silently falls
            # back to the dynamic one, which rides the level after every impact.
            audio_chain.append(f"volume={_decibels(target - mixed_level)}")
            audio_chain.append("alimiter=limit=-2.5dB:attack=5:release=80:level=false")
            audio_chain.append("aresample=48000")
            logger.info(
                f"  ⑦ mix: {measured['input_i']} LUFS, bringing it to {target:g}"
            )
        if fade_out > 0 and duration > fade_out * 2:
            audio_chain.append(f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.2f}")
        audio_chain.append("anull")

        subtitle_file = write_subtitles(
            params, subtitle_path, audio_path, output_file, width, height, duration,
            timed=timed, direction=direction)
        if subtitle_file and not _has_filter("ass"):
            raise FinishError(
                "this ffmpeg was built without libass, so it cannot burn the "
                "subtitles; point [app] ffmpeg_path at a full build"
            )
        picture = picture_filters(
            look=look, duration=duration,
            subtitle_file=os.path.basename(subtitle_file),
            fonts_dir=utils.font_dir() if subtitle_file else "",
            hold=hold,
        )
        final_graph = f"[0:v]{picture}[v];[1:a]{','.join(audio_chain)}[a]"

        def encode(codec: str) -> None:
            quality = (
                ["-preset", setting("preset"), "-crf", str(setting("crf")),
                 "-profile:v", "high"]
                if codec == "libx264" else ["-b:v", "12M"]
            )
            _run(
                ["-i", os.path.abspath(video_path), "-i", mix_file,
                 "-filter_complex", final_graph, "-map", "[v]", "-map", "[a]",
                 "-c:v", codec, *quality, "-r", str(video_service.fps),
                 "-c:a", video_service.audio_codec, "-b:a", video_service.audio_bitrate,
                 "-t", f"{duration:.3f}", "-movflags", "+faststart", temp_output],
                what="rendering the final video", cwd=output_dir,
            )

        codec = video_service._get_effective_video_codec()
        with video_service._stage_heartbeat("final video render"):
            try:
                encode(codec)
            except FinishError as exc:
                if codec == "libx264":
                    raise
                video_service._disable_runtime_video_codec(codec, str(exc)[-300:])
                encode("libx264")
        os.replace(temp_output, output_file)
        temp_output = ""
    finally:
        for leftover in (mix_file, temp_output, *made):
            if leftover and os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
    return music_ok
