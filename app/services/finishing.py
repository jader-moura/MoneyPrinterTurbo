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
* 开头结尾硬切 → 淡入、淡出；
* 字幕 → 写成 ASS 交给 libass 烧（``utils/ass_subtitles.py``），包括"念到
  哪个词哪个词变色"和左上角的标题卡。

每一项都有开关，在 ``config.toml`` 的 ``[finish]`` 里；没写的项用下面
``DEFAULTS`` 里的值。

A second way to make the final file, entirely in ffmpeg: voice EQ and
compression, music ducked under the narration, two-pass loudness to -14 LUFS,
a colour grade with vignette and grain, fades, and subtitles burned from an
ASS file. Used when the task's ``finish_look`` is set; the MoviePy renderer is
untouched and remains what runs when it is empty.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger

from app.config import config
from app.models.schema import VideoAspect, VideoParams
from app.services import bgm as bgm_service
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
    # 开头淡入、结尾淡出各多少秒，0 = 不要。
    "fade_in": 1.0,
    "fade_out": 2.0,
    # 让写脚本的模型挑三到五处，在左上角打出标题卡。
    "title_cards": True,
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
_MUSIC_BELOW_VOICE = 11.0
# ``bgm_volume`` 的这个值对应"比旁白低 11 LU"；调大调小按分贝算。
_REFERENCE_BGM_VOLUME = 0.2
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
) -> str:
    """混音那张滤镜图。输入 0 是旁白，1 是配乐（有的话），后面是转场音效。

    出来两路一样的：``[mix]`` 写成文件，``[meter]`` 只是量响度。
    """
    voice = [_STEREO_48K]
    if voice_processing:
        voice.append(_VOICE_CHAIN)
    voice.append(f"volume={_decibels(voice_gain)}")
    parts: List[str] = []
    layers = ["[voice]"]
    if music_gain is None:
        parts.append(f"[0:a]{','.join(voice)}[voice]")
    else:
        parts.append(f"[0:a]{','.join(voice)},asplit=2[voice][key]")
        music = [_STEREO_48K, f"volume={_decibels(music_gain)}"]
        # 淡的是整条时间线的结尾，不是每一遍循环的结尾。
        fade = min(3.0, duration / 2)
        music.append(f"atrim=duration={duration:.3f}")
        music.append(f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}")
        parts.append(f"[1:a]{','.join(music)}[bed]")
        if ducking:
            parts.append(
                "[bed][key]sidechaincompress=threshold=0.02:ratio=6:attack=40:"
                "release=600[music]"
            )
        else:
            parts.append("[key]anullsink")
            parts.append("[bed]anull[music]")
        layers.append("[music]")
    for input_index, times in cut_sounds:
        names = [f"[cut{input_index}x{n}]" for n in range(len(times))]
        parts.append(
            f"[{input_index}:a]{_STEREO_48K},volume={max(0.0, cut_volume):.3f},"
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
) -> str:
    """画面那一串滤镜：调色、暗角、颗粒、字幕、淡入淡出。"""
    spec = LOOKS[look]
    chain: List[str] = []
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
    return ass_subtitles.Style(
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
    saved_path = os.path.join(task_dir, _SPOKEN_WORDS_FILE)
    stat = os.stat(audio_path)
    stamp = [int(stat.st_size), int(stat.st_mtime)]
    try:
        with open(saved_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if saved.get("audio") == stamp:
            return [(float(a), float(b), str(c)) for a, b, c in saved["words"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    logger.info("timing the subtitle words against the narration (whisper)")
    spoken = _spoken_words(audio_path)
    with open(saved_path, "w", encoding="utf-8") as handle:
        json.dump({"audio": stamp, "words": spoken}, handle, ensure_ascii=False)
    return spoken


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


def title_cards(
    task_dir: str, params: VideoParams, words: Sequence[ass_subtitles.Word],
    duration: float,
) -> List[ass_subtitles.TitleCard]:
    """这条片子的标题卡。

    挑哪几处是模型定的，定完存在任务目录的 ``title-cards.json`` 里：以后每次
    重新出片用的都是同一份，想改就改这个文件（删掉它就重新挑一遍，写成 ``[]``
    就是不要）。每一张都要在旁白里找到它那句话才显示——找不到的那张不显示，
    并且在日志里说是哪一张。
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
    cards: List[ass_subtitles.TitleCard] = []
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
        # 两张不叠在一起，也不拖到片尾的淡出里。
        if cards and start < cards[-1].end + 1.0:
            continue
        end = min(start + _TITLE_CARD_SECONDS, duration - 2.5)
        if end - start < 2.0:
            continue
        cards.append(ass_subtitles.TitleCard(
            start, end, str(entry.get("title") or ""), str(entry.get("subtitle") or "")))
    return cards


def write_subtitles(
    params: VideoParams, subtitle_path: str, audio_path: str, output_file: str,
    width: int, height: int, duration: float,
) -> str:
    """把要烧进画面的字写成 ASS，返回文件路径；没有要烧的返回空串。"""
    cues = []
    if subtitle_path and os.path.exists(subtitle_path):
        cues = moviepy_subtitles(subtitle_path)
    words = ass_subtitles.words_from_cues(cues)
    mode = str(getattr(params, "subtitle_display_mode", "sentence") or "sentence")
    captions = bool(params.subtitle_enabled) and bool(cues)
    if captions and mode != "sentence" and setting("word_timing") == "whisper":
        words = ass_subtitles.align_spoken_words(
            words, _spoken_words_once(audio_path, os.path.dirname(output_file)))
    cards = title_cards(os.path.dirname(output_file), params, words, duration)
    if not captions and not cards:
        return ""
    style = subtitle_style(params)
    content = ass_subtitles.build(
        cues if captions else [], width=width, height=height, style=style, mode=mode,
        max_lines=int(getattr(params, "subtitle_max_lines", 0) or 0),
        words=words, title_cards=cards,
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
    duration = _duration_of(video_path)
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
                + 20.0 * math.log10(volume / _REFERENCE_BGM_VOLUME)
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

    descriptor, mix_file = tempfile.mkstemp(prefix=".finish-mix-", suffix=".wav",
                                            dir=output_dir)
    os.close(descriptor)
    descriptor, temp_output = tempfile.mkstemp(prefix=".finish-", suffix=".mp4",
                                               dir=output_dir)
    os.close(descriptor)
    subtitle_file = ""
    try:
        graph = mix_graph(
            duration=duration, voice_gain=voice_gain, music_gain=music_gain,
            cut_sounds=cut_inputs,
            cut_volume=float(params.transition_sound_volume or 0.0),
            voice_processing=voice_processing, ducking=setting("music_ducking"),
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
            # 第二遍：带着第一遍量出来的数，线性地推到目标响度。
            audio_chain.append(
                f"loudnorm=I={target:g}:TP=-1.5:LRA=11:"
                f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                f"measured_LRA={measured['input_lra']}:"
                f"measured_thresh={measured['input_thresh']}:"
                f"offset={measured['target_offset']}:linear=true"
            )
            audio_chain.append("aresample=48000")
            logger.info(
                f"  ⑦ mix: {measured['input_i']} LUFS, bringing it to {target:g}"
            )
        fade_out = setting("fade_out")
        if fade_out > 0 and duration > fade_out * 2:
            audio_chain.append(f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.2f}")
        audio_chain.append("anull")

        subtitle_file = write_subtitles(
            params, subtitle_path, audio_path, output_file, width, height, duration)
        if subtitle_file and not _has_filter("ass"):
            raise FinishError(
                "this ffmpeg was built without libass, so it cannot burn the "
                "subtitles; point [app] ffmpeg_path at a full build"
            )
        picture = picture_filters(
            look=look, duration=duration,
            subtitle_file=os.path.basename(subtitle_file),
            fonts_dir=utils.font_dir() if subtitle_file else "",
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
        for leftover in (mix_file, temp_output):
            if leftover and os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
    return music_ok
