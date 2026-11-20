"""成片精修里的声音设计：重音、环境声，以及配乐什么时候让开。

这是什么
--------
分镜（``scene_plan.py``）可以给每一段写"导演"项：这一句上落一记重音、这几段
不要配乐、这几段底下垫风声或者室内的底噪。这个模块做两件事：

* 把那几种声音**合成**出来（不带任何素材文件）：一记低频的重音、一段可以
  无缝循环的风声、一段可以无缝循环的室内底噪；
* 把"从几秒到几秒"写成 ffmpeg ``volume`` 滤镜能算的表达式，让配乐和环境声
  在该出现的地方出现。

混音本身在 ``finishing.mix_graph`` 里。

Sound design for the finishing pass. A scene plan can direct an impact on a
line, scenes with no music, and wind or room tone under scenes. This module
synthesises those sounds (no asset files) and writes the "from here to there"
envelopes as expressions ffmpeg's ``volume`` filter evaluates. The mix itself
is in ``finishing.mix_graph``.

电平都是相对一条处理到 -18 LUFS 的旁白定的（``finishing._VOICE_LUFS``）。
"""

from __future__ import annotations

import wave
from typing import List, Sequence, Tuple

import numpy as np

SAMPLE_RATE = 48000
#: 重音的峰值、两种环境声的均方根，dBFS。在一条真片子上听过的数。
HIT_PEAK_DB = -8.0
BED_RMS_DB = {"wind": -40.0, "room": -33.0}
#: 环境声合成多长再循环。够长，听不出在重复。
BED_SECONDS = 37.0
HIT_SECONDS = 5.0
#: 配乐让开的时候压到多少分贝：听不见。
MUSIC_OFF_DB = -70.0
#: 配乐让开、环境声进出的那几条斜线，秒（见 :func:`gate`）。编辑器里的预览
#: 读的也是这两组数，所以听到的进出和成片是同一条线。
MUSIC_GATE = {"rise": 2.5, "fall": 4.0, "lead": 2.5, "hold": 0.8}
BED_GATE = {"rise": 2.0, "fall": 2.0, "lead": 0.5, "hold": 1.5}

Window = Tuple[float, float]


def _shaped_noise(rng, count: int, low: float, high: float, slope: float) -> np.ndarray:
    """能量在 low 到 high 赫兹之间、按 1/f**slope 往高处掉的噪声。

    是在频域里做的，所以首尾相接：拿来循环没有接缝。
    """
    spectrum = np.fft.rfft(rng.standard_normal(count))
    frequency = np.fft.rfftfreq(count, 1.0 / SAMPLE_RATE)
    frequency[0] = 1.0
    shape = 1.0 / frequency ** (slope / 2.0)
    shape *= 1.0 / (1.0 + (low / frequency) ** 6) / (1.0 + (frequency / high) ** 6)
    sound = np.fft.irfft(spectrum * shape, count)
    return sound / (np.sqrt(np.mean(sound ** 2)) + 1e-12)


def _wander(rng, count: int, rate: float, floor: float) -> np.ndarray:
    """在 floor 和 1 之间慢慢晃的音量，一秒变 rate 次左右；首尾相接。"""
    knots = max(4, int(count / SAMPLE_RATE * rate))
    levels = rng.uniform(floor, 1.0, knots)
    position = np.arange(count) * (knots / count)
    index = position.astype(int) % knots
    blend = (1.0 - np.cos((position - np.floor(position)) * np.pi)) / 2.0
    return levels[index] * (1.0 - blend) + levels[(index + 1) % knots] * blend


def bed(kind: str, seconds: float = BED_SECONDS) -> np.ndarray:
    """一段可以无缝循环的环境声，立体声，``(n, 2)``。"""
    if kind not in BED_RMS_DB:
        raise ValueError(f"unknown ambience {kind!r}; it is one of {', '.join(BED_RMS_DB)}")
    rng = np.random.default_rng(1978 if kind == "wind" else 8213)
    count = int(seconds * SAMPLE_RATE)
    if kind == "wind":
        def one() -> np.ndarray:
            return _shaped_noise(rng, count, 140.0, 1300.0, 1.6)
        shared, floor, rate = 0.6, 0.35, 0.25
    else:
        # 室内：贴着耳朵的一点低频，上面一层很薄的中频。
        def one() -> np.ndarray:
            return (_shaped_noise(rng, count, 28.0, 150.0, 1.0)
                    + 0.22 * _shaped_noise(rng, count, 150.0, 520.0, 2.0))
        shared, floor, rate = 0.8, 0.6, 0.15
    middle = one()
    sound = np.stack(
        [shared * middle + (1.0 - shared) * one() for _ in range(2)], axis=1)
    sound *= _wander(rng, count, rate, floor)[:, None]
    sound /= np.sqrt(np.mean(sound ** 2)) + 1e-12
    return sound * 10.0 ** (BED_RMS_DB[kind] / 20.0)


def hit(seconds: float = HIT_SECONDS) -> np.ndarray:
    """一记重音：往下掉的低频、手机喇叭也听得见的一声闷响、一条暗的尾巴。"""
    rng = np.random.default_rng(34)
    count = int(seconds * SAMPLE_RATE)
    time = np.arange(count) / SAMPLE_RATE
    attack = 1.0 - np.exp(-time * 500.0)
    pitch = 40.0 + 36.0 * np.exp(-time * 6.0)
    sub = np.sin(2.0 * np.pi * np.cumsum(pitch) / SAMPLE_RATE) * np.exp(-time * 1.5)
    release = np.minimum(1.0, (seconds - time) / 0.5)
    sound = np.zeros((count, 2))
    for channel in range(2):
        thud = _shaped_noise(rng, count, 70.0, 650.0, 1.8) * np.exp(-time * 4.0)
        tail = _shaped_noise(rng, count, 110.0, 1100.0, 2.2) * np.exp(-time * 1.0)
        sound[:, channel] = (sub + 0.55 * thud + 0.16 * tail) * attack * release
    sound /= np.max(np.abs(sound))
    return sound * 10.0 ** (HIT_PEAK_DB / 20.0)


def write_wav(path: str, sound: np.ndarray) -> str:
    """写成 48 kHz、16 位、立体声的 WAV。"""
    samples = (np.clip(sound, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())
    return path


def merged(windows: Sequence[Window], gap: float = 0.0) -> List[Window]:
    """叠在一起、或者隔得不到 ``gap`` 秒的几段，并成一段。"""
    joined: List[Window] = []
    for start, end in sorted((float(a), float(b)) for a, b in windows if b > a):
        if joined and start <= joined[-1][1] + gap:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def gate(windows: Sequence[Window], rise: float, fall: float,
         lead: float = 0.0, hold: float = 0.0) -> str:
    """一个在这几段里是 1、别处是 0 的表达式（给 ``volume`` 的 ``eval=frame``）。

    每一段：从 ``start - lead`` 开始用 ``rise`` 秒升到 1，到 ``end + hold`` 开始
    用 ``fall`` 秒落回 0。几段先并过，所以加起来不会超过 1。没有段时返回
    ``"0"``。
    """
    parts = [
        f"clip((t-{up:.3f})/{max(rise, 0.01):.3f},0,1)"
        f"-clip((t-{down:.3f})/{max(fall, 0.01):.3f},0,1)"
        for up, down in ramps(windows, fall, lead, hold)
    ]
    return "+".join(parts) if parts else "0"


def ramps(windows: Sequence[Window], fall: float,
          lead: float = 0.0, hold: float = 0.0) -> List[Window]:
    """:func:`gate` 的每一段从几秒开始升、从几秒开始落。

    编辑器的预览拿这几对数自己算同一条线，不用去读 ffmpeg 的表达式。
    """
    return [
        (max(0.0, start - lead), end + hold)
        for start, end in merged(windows, gap=lead + hold + fall)
    ]


def music_filter(off: Sequence[Window]) -> str:
    """配乐在这几段里让开的那一节滤镜；没有要让开的返回空串。

    在上一句还没说完的时候就开始往下走（2.5 秒），到这一段开始时已经没有了；
    这一段结束后停不到一秒再回来，用四秒。按分贝走直线，听起来是匀的。
    """
    if not off:
        return ""
    expression = gate(off, **MUSIC_GATE)
    return f"volume='pow(10,{MUSIC_OFF_DB / 20.0:g}*({expression}))':eval=frame"


def bed_filter(windows: Sequence[Window]) -> str:
    """环境声只在这几段里响：提前半秒进来，多留一秒半，两头各用两秒。"""
    expression = gate(windows, **BED_GATE)
    return f"volume='{expression}':eval=frame"
