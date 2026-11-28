"""A few seconds of a YouTube video, as a silent clip for a scene.

The user watches the video in their own browser, says where the part they want
starts and ends ("2:34" to "2:38"), and this module fetches only that part:
``yt-dlp`` downloads the section, ``ffmpeg`` cuts it exactly and writes it
without sound. The clip is then placed on the scene like a file the user
uploaded.

Only the part asked for is downloaded, never the whole video, and it is capped
at ``MAX_SECONDS`` — a scene lasts a few seconds.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from app.config import config
from app.utils import utils


class FootageError(Exception):
    """The footage could not be had. The message is for the user."""


MAX_SECONDS = 60.0
MIN_SECONDS = 0.5
MAX_HEIGHT = 1080
_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_ID = re.compile(r"[A-Za-z0-9_-]{11}")

INSTALL_HINT = (
    "getting footage needs the yt-dlp package, which is not installed; run "
    "“python -m pip install yt-dlp” in this app's Python environment and "
    "restart the app"
)


def seconds_of(value: Any, what: str = "time") -> float:
    """``"2:34"``, ``"1:02:34"``, ``"154"`` or ``"2:34.5"`` as seconds."""
    text = str(value if value is not None else "").strip().replace(",", ".")
    if not text:
        raise FootageError(f"the {what} is missing; write it like 2:34")
    parts = text.split(":")
    if len(parts) > 3:
        raise FootageError(f"{text!r} is not a time; write it like 2:34")
    total = 0.0
    try:
        for position, part in enumerate(parts):
            last = position == len(parts) - 1
            number = float(part) if last else float(int(part))
            if number < 0 or (position > 0 and number >= 60):
                raise ValueError(part)
            total = total * 60 + number
    except ValueError:
        raise FootageError(f"{text!r} is not a time; write it like 2:34") from None
    return round(total, 3)


def video_id(address: Any) -> str:
    """The id of the YouTube video an address points at. Anything that is not
    a YouTube video is refused by name, rather than handed to the downloader."""
    text = str(address or "").strip()
    if _ID.fullmatch(text):
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        parts = None
    host = (parts.hostname or "").lower() if parts else ""
    if not parts or parts.scheme not in ("http", "https") or not any(
        host == known or host.endswith("." + known) for known in _HOSTS
    ):
        raise FootageError(
            "that is not the address of a YouTube video; copy it from the "
            "browser's address bar while the video is open"
        )
    found = ""
    if host.endswith("youtu.be"):
        found = parts.path.strip("/").split("/")[0]
    else:
        found = (parse_qs(parts.query).get("v") or [""])[0]
        if not found:
            pieces = [piece for piece in parts.path.split("/") if piece]
            if len(pieces) >= 2 and pieces[0] in ("shorts", "embed", "live", "v"):
                found = pieces[1]
    if not _ID.fullmatch(found or ""):
        raise FootageError(
            "that YouTube address does not name a video; open the video itself "
            "and copy its address"
        )
    return found


def checked_part(start: Any, end: Any) -> "tuple[float, float]":
    begin = seconds_of(start, "start time")
    finish = seconds_of(end, "end time")
    if finish <= begin:
        raise FootageError("the end time has to be after the start time")
    length = finish - begin
    if length < MIN_SECONDS:
        raise FootageError("that part is too short to use")
    if length > MAX_SECONDS:
        raise FootageError(
            f"that part is {length:.0f} seconds long; at most "
            f"{MAX_SECONDS:.0f} seconds can be taken at a time"
        )
    return begin, finish


def _proxy() -> Optional[str]:
    proxies = getattr(config, "proxy", None) or {}
    if isinstance(proxies, dict):
        return proxies.get("https") or proxies.get("http") or None
    return None


def _download(address: str, begin: float, finish: float, folder: str,
              ffmpeg: str) -> str:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise FootageError(INSTALL_HINT) from None

    options = {
        # Picture only: the finished video never uses the clip's sound.
        "format": (
            f"bv*[height<={MAX_HEIGHT}][vcodec^=avc1]/bv*[height<={MAX_HEIGHT}]/"
            f"b[height<={MAX_HEIGHT}]/b"
        ),
        "download_ranges": download_range_func(None, [(begin, finish)]),
        # Cut where the user said, not at the nearest keyframe before it —
        # that can be seconds earlier.
        "force_keyframes_at_cuts": True,
        "outtmpl": os.path.join(folder, "part.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "ffmpeg_location": ffmpeg,
    }
    proxy = _proxy()
    if proxy:
        options["proxy"] = proxy
    try:
        with YoutubeDL(options) as downloader:
            downloader.download([address])
    except Exception as exc:
        reason = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
        reason = re.sub(r"^ERROR:\s*", "", reason)
        raise FootageError(
            f"YouTube would not hand that part over: {reason[:300]}"
        ) from None
    for name in sorted(os.listdir(folder)):
        if name.startswith("part.") and not name.endswith((".part", ".ytdl")):
            return os.path.join(folder, name)
    raise FootageError("nothing was downloaded for that part of the video")


def _cut(source: str, length: float, target: str, ffmpeg: str) -> None:
    """Exactly ``length`` seconds from the start of ``source``, without sound,
    in the format every other clip of the app is in."""
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-i", source,
        "-t", f"{length:.3f}", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", target,
    ]
    try:
        done = subprocess.run(command, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FootageError(f"the part could not be cut: {exc}") from None
    if done.returncode != 0 or not os.path.isfile(target) or not os.path.getsize(target):
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise FootageError(f"the part could not be cut: {detail or 'ffmpeg failed'}")


def fetch(address: Any, start: Any, end: Any, folder: str) -> str:
    """Fetch that part of the video into ``folder`` and return the clip's path.

    The same part of the same video is fetched once: asking again returns the
    file that is already there.
    """
    video = video_id(address)
    begin, finish = checked_part(start, end)
    digest = hashlib.sha1(
        f"{video}:{begin:.3f}:{finish:.3f}".encode("utf-8")).hexdigest()[:10]
    target = os.path.join(folder, f"footage-{video}-{digest}.mp4")
    if os.path.isfile(target) and os.path.getsize(target):
        return target
    ffmpeg = utils.get_ffmpeg_binary()
    work = tempfile.mkdtemp(prefix="footage-")
    try:
        logger.info(f"getting footage {video} {begin:.2f}-{finish:.2f}")
        part = _download(
            f"https://www.youtube.com/watch?v={video}", begin, finish, work, ffmpeg
        )
        staged = os.path.join(work, "cut.mp4")
        _cut(part, finish - begin, staged, ffmpeg)
        os.makedirs(folder, exist_ok=True)
        shutil.move(staged, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return target


# ---- the part chosen in the browser, waiting for the editor to ask for it

_LOCK = threading.Lock()
_PICKED: dict = {}
_PICKED_TASKS = 20


def keep_pick(task_id: str, session: str, address: Any, start: Any, end: Any) -> dict:
    """The browser extension says which part the user marked. It is checked
    here so a wrong one is refused in the YouTube window, where the user is."""
    video = video_id(address)
    begin, finish = checked_part(start, end)
    kept = {
        "session": str(session or ""),
        "url": f"https://www.youtube.com/watch?v={video}",
        "start": begin, "end": finish,
    }
    with _LOCK:
        _PICKED.pop(str(task_id), None)
        _PICKED[str(task_id)] = kept
        for stale in list(_PICKED)[:-_PICKED_TASKS]:
            _PICKED.pop(stale, None)
    return {key: value for key, value in kept.items() if key != "session"}


def picked(task_id: str, session: str = "") -> dict:
    """Handed over once, and only to the search it was marked for."""
    with _LOCK:
        kept = _PICKED.get(str(task_id))
        if not kept or kept["session"] != str(session or ""):
            return {"state": "waiting"}
        _PICKED.pop(str(task_id), None)
    return {"state": "picked",
            **{key: value for key, value in kept.items() if key != "session"}}
