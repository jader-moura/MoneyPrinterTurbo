"""A score for the cold open, made here: from the first frame up to the title.

A scene plan asks for it on its title (``"score": "cold open"``). The score is
written to the cuts of the video it is for: a low drone on D, four pad chords
that change on cuts, a heartbeat that comes in at the first cut, a clock that
joins a third of the way in, one piano note on each cut, low booms on the
turning points, a riser over the last seconds and one large impact on the
title, which then rings out while the video's own music comes in.

Everything is synthesised with numpy, from a fixed seed: the same cuts give
the same sound, and nothing has to be downloaded or licensed.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

from app.services.sound_design import SAMPLE_RATE

# The title has to be at least this far in for a build-up to make sense.
SHORTEST_SECONDS = 12.0
# How long the title's impact and chord ring after it.
TAIL_SECONDS = 8.0
# Levels are set over the cold open and this much after it.
_LEVEL_TAIL_SECONDS = 3.8
_MUSIC_GAIN = 0.66
_SEED = 3

# D minor, B flat major seven, G minor, A suspended with a flat nine (MIDI
# notes), and the piano notes that go with each.
_CHORDS = (
    (50, 57, 62, 65, 69),
    (46, 58, 62, 65, 69),
    (43, 55, 62, 67, 70),
    (45, 57, 62, 64, 70),
)
_MOTIF = ((69, 65, 62), (65, 62, 69), (67, 70, 74), (64, 69, 70))
# Where the chords change, as shares of the way to the title, when no cut is
# near enough to change on.
_CHORD_SHARES = (0.28, 0.56, 0.78)
_PIANO_SHORTEST_GAP = 1.6


def _gain(decibels: float) -> float:
    return 10.0 ** (decibels / 20.0)


def _pitch(note: float) -> float:
    return 440.0 * 2.0 ** ((note - 69.0) / 12.0)


def _stereo(sound: np.ndarray) -> np.ndarray:
    return np.stack([sound, sound], 1) if sound.ndim == 1 else sound


def _filtered(sound: np.ndarray, low: float = 0.0, high: float = 0.0,
              order: int = 2) -> np.ndarray:
    """Keeps what lies between ``low`` and ``high`` Hz (0 leaves that side
    open), with Butterworth slopes and no phase shift."""
    count = len(sound)
    size = 1 << max(1, int(np.ceil(np.log2(count + SAMPLE_RATE // 4))))
    spectrum = np.fft.rfft(sound, size, axis=0)
    frequency = np.fft.rfftfreq(size, 1.0 / SAMPLE_RATE)
    response = np.ones_like(frequency)
    if high > 0:
        response /= np.sqrt(1.0 + (frequency / high) ** (2 * order))
    if low > 0:
        ratio = (frequency / low) ** order
        response *= ratio / np.sqrt(1.0 + ratio ** 2)
    if sound.ndim == 2:
        response = response[:, None]
    return np.fft.irfft(spectrum * response, size, axis=0)[:count]


def _rms(sound: np.ndarray) -> float:
    return float(np.sqrt(np.mean(sound ** 2) + 1e-12))


def _to_peak(sound: np.ndarray, decibels: float) -> np.ndarray:
    return sound * _gain(decibels) / (float(np.abs(sound).max()) + 1e-12)


def _place(buffer: np.ndarray, sound: np.ndarray, at: float, gain: float = 1.0) -> None:
    start = int(at * SAMPLE_RATE)
    if start < 0:
        sound, start = sound[-start:], 0
    count = min(len(sound), len(buffer) - start)
    if count > 0:
        buffer[start:start + count] += _stereo(sound)[:count] * gain


def _saw(frequency: float, count: int, phase: float = 0.0) -> np.ndarray:
    time = np.arange(count) / SAMPLE_RATE
    return 2.0 * ((frequency * time + phase) % 1.0) - 1.0


class _Score:
    def __init__(self, title_at: float, cuts: Sequence[float]):
        self.rng = np.random.default_rng(_SEED)
        self.title = float(title_at)
        self.count = int((self.title + TAIL_SECONDS) * SAMPLE_RATE)
        self.time = np.arange(self.count) / SAMPLE_RATE
        self.level_count = min(
            self.count, int((self.title + _LEVEL_TAIL_SECONDS) * SAMPLE_RATE))
        self.cuts = sorted(
            float(cut) for cut in cuts if 1.5 < float(cut) < self.title - 1.5)
        # The first cut is where the story starts: the heartbeat comes in.
        early = [cut for cut in self.cuts if 2.0 <= cut <= self.title / 3]
        self.opening = early[0] if early else min(4.0, self.title / 6)
        self.changes = [self._near(self.title * share, 3.0) for share in _CHORD_SHARES]

    # -- helpers -----------------------------------------------------------
    def _near(self, at: float, within: float) -> float:
        """The cut nearest ``at``, or ``at`` itself if none is within reach."""
        near = [cut for cut in self.cuts if abs(cut - at) <= within]
        return min(near, key=lambda cut: abs(cut - at)) if near else at

    def _envelope(self, start: float, end: float, rise: float, fall: float) -> np.ndarray:
        return (np.clip((self.time - start) / rise, 0, 1)
                * np.clip((end - self.time) / fall, 0, 1))

    def _to_rms(self, sound: np.ndarray, decibels: float) -> np.ndarray:
        return sound * _gain(decibels) / _rms(sound[:self.level_count])

    def _reverb(self, sound: np.ndarray, decay: float = 1.6, length: float = 5.0,
                low: float = 180.0, high: float = 5000.0) -> np.ndarray:
        count = int(length * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        impulse = self.rng.standard_normal((count, 2)) * np.exp(-time / decay)[:, None]
        impulse = _filtered(impulse, low, high)
        impulse[:int(0.02 * SAMPLE_RATE)] = 0
        impulse /= np.sqrt((impulse ** 2).sum(0))
        sound = _stereo(sound)
        total = len(sound) + count
        size = 1 << int(np.ceil(np.log2(total)))
        wet = np.fft.irfft(
            np.fft.rfft(sound, size, axis=0) * np.fft.rfft(impulse, size, axis=0),
            size, axis=0)
        return wet[:total]

    def _pad_notes(self, notes: Sequence[float], weights: Sequence[float],
                   count: int, cutoff: float) -> np.ndarray:
        """Each note is three detuned saws a side; then one low-pass for all."""
        out = np.zeros((count, 2))
        for note, weight in zip(notes, weights):
            for channel in (0, 1):
                for cents in (-7, 0, 6):
                    frequency = _pitch(note) * 2 ** (
                        (cents + (2 if channel else -2)) / 1200)
                    out[:, channel] += weight * _saw(frequency, count, self.rng.random())
        return _filtered(out, high=cutoff, order=4)

    def _piano(self, note: float, seconds: float = 5.0, velocity: float = 1.0) -> np.ndarray:
        count = int(seconds * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        base = _pitch(note)
        sound = np.zeros(count)
        for partial in range(1, 9):
            frequency = base * partial * np.sqrt(1 + 0.0005 * partial * partial)
            if frequency > 9000:
                break
            sound += (np.sin(2 * np.pi * frequency * time + self.rng.random() * 6.28)
                      * partial ** -1.7 * np.exp(-time / (2.4 / partial ** 0.75)))
        sound *= np.clip(time / 0.006, 0, 1)
        hammer = _filtered(self.rng.standard_normal(count), high=900)
        sound += hammer * np.exp(-time / 0.012) * 0.08
        return _filtered(sound, high=1900 + 600 * velocity) * velocity

    def _boom(self, size: float = 1.0) -> np.ndarray:
        count = int(4 * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        frequency = 34 + 62 * np.exp(-time / 0.10)
        sound = (np.sin(2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE)
                 * np.exp(-time / (0.75 * size)))
        noise = self.rng.standard_normal(count)
        sound += _filtered(noise, high=220, order=4) * np.exp(-time / 0.22) * 0.9
        sound += _filtered(noise, 300, 1800) * np.exp(-time / 0.05) * 0.25
        return np.tanh(sound * 1.6) * np.clip(time / 0.002, 0, 1)

    def _swell(self, seconds: float = 1.3) -> np.ndarray:
        count = int(seconds * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        sound = (_filtered(self.rng.standard_normal((count, 2)), 180, 1600)
                 * ((time / seconds) ** 3.2)[:, None])
        end = int(0.012 * SAMPLE_RATE)
        sound[-end:] *= np.linspace(1, 0, end)[:, None]
        return sound

    # -- the parts ---------------------------------------------------------
    def drone(self) -> np.ndarray:
        time, title = self.time, self.title
        sound = (np.sin(2 * np.pi * _pitch(26) * time) * 0.4
                 + np.sin(2 * np.pi * _pitch(38) * time + 1) * 0.9
                 + np.sin(2 * np.pi * _pitch(50) * time + 2) * 0.25)
        sound = _stereo(sound * (0.8 + 0.2 * np.sin(2 * np.pi * 0.11 * time)))
        growl = _filtered(
            _stereo(_saw(_pitch(26) * 1.003, self.count))
            + _stereo(_saw(_pitch(38) * 0.997, self.count, 0.3)), high=330, order=4)
        sound = sound + growl * 0.7 * (
            0.6 + 0.4 * np.sin(2 * np.pi * 0.07 * time + 2))[:, None]
        # Cut dead just before the title, then a faint ring under it.
        after = (np.clip((time - title) / 0.01, 0, 1) * 0.06
                 * np.exp(-np.clip(time - title, 0, None) / 2.2))
        sound *= (self._envelope(0.0, title - 0.06, 2.5, 0.05) + after)[:, None]
        return self._to_rms(sound, -31)

    def pad(self) -> np.ndarray:
        title = self.title
        bounds = [0.0, *self.changes, title - 0.05]
        pad = np.zeros((self.count, 2))
        for index, notes in enumerate(_CHORDS):
            last = index == len(_CHORDS) - 1
            start = max(0.0, bounds[index] - 1.2)
            end = bounds[index + 1] + (0.0 if last else 2.0)
            count = int((end - start) * SAMPLE_RATE)
            if count <= 0:
                continue
            time = np.arange(count) / SAMPLE_RATE
            envelope = (np.clip(time / 2.4, 0, 1)
                        * np.clip((end - start - time) / (0.04 if last else 2.6), 0, 1))
            chord = self._pad_notes(
                notes, [0.6, 1, 1, 1, 1], count, 520 + 170 * index)
            high = self._pad_notes(
                [note + 12 for note in notes[-2:]], [1, 1], count, 1500
            ) * (0.10 + 0.07 * index)
            _place(pad, (chord + high) * envelope[:, None] * (0.8 + 0.18 * index), start)
        pad *= (0.85 + 0.15 * np.sin(2 * np.pi * 0.23 * self.time))[:, None]
        wet = self._reverb(pad, 2.2, 5.0)[:self.count]
        wet[int((title - 0.05) * SAMPLE_RATE):] = 0.0
        return (self._to_rms(pad * 0.5 + wet, -27.5)
                * self._envelope(0, title - 0.05, 0.1, 0.03)[:, None])

    def heartbeat(self) -> np.ndarray:
        beats = np.zeros((self.count, 2))
        count = int(0.5 * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        thump = (np.sin(2 * np.pi * (44 + 26 * np.exp(-time / 0.03)) * time)
                 * np.exp(-time / 0.085) * np.clip(time / 0.003, 0, 1))
        at, span = self.opening, max(1.0, self.title - self.opening)
        while at < self.title - 0.9:
            level = 0.55 + 0.45 * (at - self.opening) / span
            _place(beats, thump, at, level)
            _place(beats, thump, at + 0.27, level * 0.6)
            at += 1.0
        return _to_peak(beats, -17.5)

    def clock(self) -> np.ndarray:
        ticks = np.zeros((self.count, 2))
        count = int(0.08 * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        start, end = self.changes[0], self.title - 1.1
        at, number = start, 0
        while at < end:
            low, high = (2600, 5200) if number % 2 == 0 else (1700, 3400)
            click = (_filtered(self.rng.standard_normal(count), low, high)
                     * np.exp(-time / 0.010))
            pan = 0.35 if number % 2 == 0 else -0.35
            _place(ticks, np.stack([click * (1 - pan), click * (1 + pan)], 1), at,
                   _gain(-8 + 8 * (at - start) / max(1.0, end - start)))
            at += 0.5
            number += 1
        if not number:
            return ticks
        ticks = ticks + self._reverb(ticks, 0.5, 1.5, 600, 6000)[:self.count] * 0.5
        return _to_peak(ticks, -23)

    def piano_notes(self) -> List[tuple]:
        """``(second, note)``: one on each cut from the opening on, not closer
        together than a note needs, taken from the chord it falls in."""
        notes: List[tuple] = []
        used = [0] * len(_CHORDS)
        on = [self.opening] + [cut for cut in self.cuts if cut > self.opening]
        for cut in on:
            if cut > self.title - 2.0:
                break
            if notes and cut - notes[-1][0] < _PIANO_SHORTEST_GAP:
                # Too close to the note before. A chord change keeps its note
                # and the one before goes; any other cut goes without.
                if not any(abs(cut - change) < 0.01 for change in self.changes):
                    continue
                notes.pop()
            chord = sum(1 for change in self.changes if cut >= change - 0.01)
            pool = _MOTIF[chord]
            notes.append((cut, pool[used[chord] % len(pool)]))
            used[chord] += 1
        return notes

    def piano(self) -> np.ndarray:
        title = self.title
        long = self.count + 8 * SAMPLE_RATE
        line = np.zeros((long, 2))
        notes = self.piano_notes()
        for at, note in notes:
            _place(line, self._piano(note, 5.5, 1.0), at + 0.02)
            _place(line, self._piano(note - 12, 5.5, 0.9), at + 0.035, 0.45)
        chord = np.zeros((long, 2))
        for note, level in ((26, 1.0), (38, 0.9), (45, 0.6), (50, 0.5), (53, 0.35)):
            _place(chord, self._piano(note, 7.0, 1.0), title + 0.01, level)
        chord = chord * 0.7 + self._reverb(chord, 3.0, 6.0, 120, 4000)[:long] * 0.8
        out = _to_peak(chord, -9)[:self.count]
        if notes:
            line = line * 0.55 + self._reverb(line, 2.6, 6.0, 200, 4200)[:long] * 0.9
            line[int((title - 0.05) * SAMPLE_RATE):] *= 0.25
            out = out + _to_peak(line, -15)[:self.count]
        return out

    def riser(self, start: float) -> np.ndarray:
        count = int((self.title - 0.07 - start) * SAMPLE_RATE)
        time = np.arange(count) / SAMPLE_RATE
        along = time / (count / SAMPLE_RATE)
        tones = np.zeros((count, 2))
        for note in (50, 57, 62, 69):
            frequency = _pitch(note) * 2 ** (1.0 * along ** 2)
            phase = 2 * np.pi * np.cumsum(frequency) / SAMPLE_RATE
            tones += _stereo(np.sin(phase) + 0.4 * np.sin(2 * phase)) * 0.25
        tremolo = 0.75 + 0.25 * np.sin(
            2 * np.pi * np.cumsum(4 + 12 * along ** 2) / SAMPLE_RATE)
        noise = self.rng.standard_normal((count, 2))
        air = (_filtered(noise, 300, 1200) * (1 - along)[:, None]
               + _filtered(noise, 1200, 4000) * (1 - np.abs(2 * along - 1))[:, None] * 0.7
               + _filtered(noise, 4000, 10000) * (along ** 2)[:, None] * 0.6)
        sound = ((_filtered(tones, high=5000) * tremolo[:, None] + air * 0.8)
                 * (along ** 2.6)[:, None])
        end = int(0.01 * SAMPLE_RATE)
        sound[-end:] *= np.linspace(1, 0, end)[:, None]
        sound = sound + self._reverb(sound, 0.8, 2.0)[:count] * 0.4
        return _to_peak(sound, -11)

    def boom_times(self) -> List[tuple]:
        """``(second, size, peak in dB, with a swell into it)``."""
        title = self.title
        booms = [(0.12, 0.8, -15.0, False), (self.opening, 1.0, -10.5, True)]
        middle = self._near(title * 0.5, 4.0)
        if middle - self.opening >= 5.0:
            booms.append((middle, 1.0, -10.5, True))
        late = [cut for cut in self.cuts if title - 6.0 <= cut <= title - 2.5]
        last = late[-1] if late else title - 3.66
        if last - booms[-1][0] >= 4.0:
            booms.append((last, 0.9, -12.0, True))
        return booms

    def hits(self) -> np.ndarray:
        title = self.title
        hits = np.zeros((self.count, 2))
        booms = self.boom_times()
        # The riser starts on the last boom when there is one near the end.
        start = booms[-1][0] if booms[-1][0] >= title - 6.0 else title - 3.66
        _place(hits, self.riser(start + 0.03), start + 0.03)
        for at, size, peak, swell in booms:
            boom = _stereo(self._boom(size))
            boom = boom + self._reverb(
                _filtered(boom, 60, 900), 1.6, 4.0, 60, 1500)[:len(boom)] * 0.5
            _place(hits, _to_peak(boom, peak), at)
            if swell:
                _place(hits, _to_peak(self._swell(1.3), peak - 9), at - 1.3)
        # The title: a larger boom with a dark wash and a little metal in it.
        boom = _stereo(self._boom(1.7))
        count = len(boom)
        time = np.arange(count) / SAMPLE_RATE
        boom = boom + (_filtered(self.rng.standard_normal((count, 2)), high=900)
                       * np.exp(-time / 0.45)[:, None] * 0.5)
        metal = sum(
            np.sin(2 * np.pi * _pitch(38) * ratio * time + index)
            * np.exp(-time / (2.2 / (1 + 0.4 * index))) / (1 + index)
            for index, ratio in enumerate((1, 2.76, 5.40, 8.93)))
        boom = boom + _stereo(metal) * 0.10
        boom = np.concatenate([boom, np.zeros((6 * SAMPLE_RATE, 2))])
        boom = boom + self._reverb(
            _filtered(boom, 50, 2500), 2.8, 6.0, 50, 2600)[:len(boom)] * 0.7
        _place(hits, _to_peak(boom, -4.5), title)
        return hits

    def sound(self) -> np.ndarray:
        music = self.drone() + self.pad() + self.heartbeat() + self.clock() + self.piano()
        mixed = music * _MUSIC_GAIN + self.hits()
        fade = int(2.0 * SAMPLE_RATE)
        mixed[-fade:] *= (np.linspace(1, 0, fade) ** 1.5)[:, None]
        return mixed


def possible(title_at: Optional[float]) -> bool:
    """Whether a title this far into the video can have a score before it."""
    return title_at is not None and float(title_at) >= SHORTEST_SECONDS


def score(title_at: float, cuts: Sequence[float] = ()) -> np.ndarray:
    """The score for a cold open whose title appears at ``title_at`` seconds
    and whose pictures change at ``cuts``: stereo, 48 kHz, ``title_at`` plus
    :data:`TAIL_SECONDS` long, at the level it should have beside a narration
    of about -14 LUFS."""
    if not possible(title_at):
        raise ValueError(
            f"a cold open needs at least {SHORTEST_SECONDS:g} seconds before its title")
    return _Score(title_at, cuts).sound()
