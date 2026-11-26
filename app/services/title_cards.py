"""Title cards made and changed by hand in the editor.

Why
---
A title card ("December 1978 / West Summerdale Avenue") used to come from two
places only: the scene plan the video was made from, or the model asked at
render time. Changing a word on one meant editing a file in the video's
folder. This lets the editor add a card, reword it, move it and take it out.

Where they are kept
-------------------
In ``title-cards.json`` in the video's folder, the file the finishing pass
already reads and lets decide: ``[{"phrase", "title", "subtitle"}]``. A card
is not kept as a number of seconds. It is kept as the words the narration is
saying when it appears, so it stays on those words when a line before it is
recorded again, a pause is spliced in, or a line is deleted.

A video whose cards came from its scene plan has no such file. The first
change here writes one from the cards the plan places, and from then on the
file decides, which is what the finishing pass does whenever it is there.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Tuple

from loguru import logger

from app.services import direction_preview, finishing, scene_editor, scene_plan
from app.services.utils import ass_subtitles
from app.utils import utils

#: A phrase is as many words as it takes to be found nowhere earlier.
_PHRASE_WORDS = (4, 14)
#: How far from a card's start a click may be and still mean that card.
_NEAR = 0.5
#: Kept between two cards, as the finishing pass does.
_APART = 1.0


class CardError(scene_editor.SceneError):
    """This card cannot be made or changed. Says why."""


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _text(value: Any, what: str, most: int, needed: bool) -> str:
    text = " ".join(str(value or "").split())
    if needed and not text:
        raise CardError(f"the card needs a {what}")
    if len(text) > most:
        raise CardError(
            f"the card's {what} is {len(text)} characters; the most is {most}")
    return text


class _Video:
    """What placing a card needs: the words with their times, how long the
    video runs, and the cards as the file has (or would have) them."""

    def __init__(self, task_id: str) -> None:
        scene_editor._require_idle(task_id)
        params = scene_editor.load_params(task_id)
        task_dir = utils.task_dir(task_id)
        try:
            finished = finishing.wanted(params)
        except finishing.FinishError as exc:
            raise CardError(str(exc)) from exc
        if not finished:
            raise CardError(
                "this video has no Finish look, so the render adds no title "
                "cards; choose a Finish look first")
        if not finishing.setting("title_cards"):
            raise CardError("title cards are turned off in the finishing settings")
        audio, subtitle, seconds = direction_preview._files(task_id)
        if not audio or not subtitle:
            raise CardError(
                "record the narration first: a card is placed on the words it says")

        # The same words, timed the same way, as the editor's preview and the
        # render use. Never listened to here: that takes minutes.
        scenes = finishing._plan_scenes(task_dir, params)
        directed = scene_plan.directed(scenes)
        fade_out = finishing.setting("fade_out")
        self.duration = max(0.0, float(seconds)) + (fade_out if fade_out > 0 else 0.3)
        heard = (finishing.setting("word_timing") == "whisper"
                 and finishing._saved_spoken_words(audio, task_dir) is not None)
        _, self.words = finishing.narration_words(
            params, subtitle, audio, task_dir, precise=directed, listen=heard)
        if not self.words:
            raise CardError("this video's narration has no words to place a card on")

        self.path = os.path.join(task_dir, finishing._TITLE_CARDS_FILE)
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as handle:
                    saved = json.load(handle)
            except (OSError, ValueError) as exc:
                raise CardError(
                    f"{finishing._TITLE_CARDS_FILE} in the video's folder cannot "
                    f"be read: {exc}") from exc
            self.entries: List[dict] = [e for e in saved or [] if isinstance(e, dict)]
        else:
            # The cards the scene plan places become the file's first content.
            planned = finishing.direct(scenes, self.words, self.duration)
            self.entries = [
                {"phrase": self.phrase_at(self._word(start)),
                 "title": title, "subtitle": subtitle}
                for start, title, subtitle in (planned.cards if planned.planned else [])
            ]

    def _word(self, time: float) -> int:
        """The word being said at ``time``, or the next one to be said."""
        for at, word in enumerate(self.words):
            if float(word.end) > time and ass_subtitles._key(word.text):
                return at
        raise CardError("the narration has ended by then; put the card earlier")

    def phrase_at(self, at: int) -> str:
        """Words from word ``at`` on, enough of them to be found nowhere
        earlier in the narration."""
        start = self.words[at].start
        for count in range(_PHRASE_WORDS[0], _PHRASE_WORDS[1] + 1):
            phrase = " ".join(str(word.text) for word in self.words[at: at + count])
            if ass_subtitles.find_phrase(self.words, phrase) == start:
                return phrase
        raise CardError(
            "the words said there are said the same way earlier in the "
            "narration, so a card cannot be tied to them; move it a few words")

    def starts(self) -> List[Optional[float]]:
        """When each entry's phrase is said; ``None`` when it is not."""
        return [ass_subtitles.find_phrase(self.words, str(entry.get("phrase") or ""))
                for entry in self.entries]

    def pick(self, start: Any) -> int:
        """The entry whose card starts at ``start`` seconds."""
        try:
            wanted = float(start)
        except (TypeError, ValueError) as exc:
            raise CardError(f"a card is named by when it starts, got {start!r}") from exc
        near = [(abs(found - wanted), at) for at, found in enumerate(self.starts())
                if found is not None and abs(found - wanted) <= _NEAR]
        if not near:
            raise CardError(f"there is no title card at {_clock(wanted)}")
        return min(near)[1]

    def fits(self, start: float, skip: Optional[int] = None) -> None:
        """Raises when a card starting at ``start`` would not be shown."""
        length = finishing._TITLE_CARD_SECONDS
        if min(start + length, self.duration - 2.5) - start < 2.0:
            raise CardError(
                "that is too close to the end of the video for a card to be read")
        for at, (entry, found) in enumerate(zip(self.entries, self.starts())):
            if at == skip or found is None:
                continue
            if found < start + length + _APART and start < found + length + _APART:
                raise CardError(
                    f"it would overlap the card {str(entry.get('title') or '')!r} at "
                    f"{_clock(found)}; a card stays {length:g}s and they need "
                    f"{_APART:g}s between them")

    def save(self) -> None:
        partial = f"{self.path}.{os.getpid()}.part"
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump(self.entries, handle, ensure_ascii=False, indent=2)
        os.replace(partial, self.path)


def _place(video: _Video, time: Any) -> Tuple[float, str]:
    try:
        wanted = max(0.0, float(time))
    except (TypeError, ValueError) as exc:
        raise CardError(f"the time must be a number of seconds, got {time!r}") from exc
    at = video._word(wanted)
    return float(video.words[at].start), video.phrase_at(at)


def add(task_id: str, time: Any, title: Any, subtitle: Any = "") -> dict:
    """A new card on the words said at ``time`` seconds. Returns the
    direction as the editor shows it."""
    with scene_editor._editing(task_id):
        video = _Video(task_id)
        title = _text(title, "title", scene_plan._CARD_TITLE_CHARS, True)
        subtitle = _text(subtitle, "subtitle", scene_plan._CARD_SUBTITLE_CHARS, False)
        start, phrase = _place(video, time)
        video.fits(start)
        video.entries.append({"phrase": phrase, "title": title, "subtitle": subtitle})
        video.save()
        logger.info(f"added title card {title!r} to {task_id} at {start:.2f}s")
    return direction_preview.describe(task_id)


def change(task_id: str, start: Any, *, title: Any = None, subtitle: Any = None,
           time: Any = None) -> dict:
    """Reword the card that starts at ``start`` seconds, and with ``time``
    move it onto the words said then."""
    with scene_editor._editing(task_id):
        video = _Video(task_id)
        at = video.pick(start)
        entry = dict(video.entries[at])
        if title is not None:
            entry["title"] = _text(title, "title", scene_plan._CARD_TITLE_CHARS, True)
        if subtitle is not None:
            entry["subtitle"] = _text(
                subtitle, "subtitle", scene_plan._CARD_SUBTITLE_CHARS, False)
        if time is not None:
            moved, entry["phrase"] = _place(video, time)
            video.fits(moved, skip=at)
        video.entries[at] = entry
        video.save()
        logger.info(f"changed title card {entry.get('title')!r} of {task_id}")
    return direction_preview.describe(task_id)


def remove(task_id: str, start: Any) -> dict:
    """Take out the card that starts at ``start`` seconds."""
    with scene_editor._editing(task_id):
        video = _Video(task_id)
        gone = video.entries.pop(video.pick(start))
        # An empty file is kept: it says "no cards", where no file would have
        # the plan's cards back, or the model asked for some at the render.
        video.save()
        logger.info(f"removed title card {gone.get('title')!r} from {task_id}")
    return direction_preview.describe(task_id)
