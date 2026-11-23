"""Start the local Chatterbox container for a narration, and stop it after.

Why this exists
---------------
Chatterbox runs on the graphics card and its server has no way to drop the
model: once loaded, it holds its VRAM until the process ends. On a 16 GB card
that is room the picture model (about 12 GB at its peak) and the video model
need, and a full card here does not fail cleanly — it shows up as device
errors or as a stage that takes minutes instead of seconds.

So the container only runs while a narration is being recorded. Stopping the
process is the one release that cannot be half done.

Nothing here runs unless ``[chatterbox] container`` names a container and
``[chatterbox] base_url`` points at this machine. A Chatterbox server run any
other way is left alone, exactly as before.
"""

import subprocess
import time
from urllib.parse import urlparse

import requests
from loguru import logger

from app.config import config

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Loading the model takes well under a minute once its weights are on disk.
# The first start downloads them, which is what the long default is for.
DEFAULT_START_TIMEOUT = 600
POLL_SECONDS = 2


def _container() -> str:
    """The container this app starts and stops, or "" when it manages none."""
    name = str(config.chatterbox.get("container", "") or "").strip()
    if not name:
        return ""
    base = str(config.chatterbox.get("base_url", "") or "").strip()
    host = (urlparse(base).hostname or "").lower() if base else ""
    return name if host in LOCAL_HOSTS else ""


def _root() -> str:
    root = str(config.chatterbox.get("base_url", "") or "").strip().rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def _docker(*args: str, timeout: int = 120) -> bool:
    try:
        done = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        logger.error("docker is not installed, so the chatterbox container cannot be managed")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"docker {args[0]} did not finish within {timeout}s")
        return False
    if done.returncode != 0:
        logger.error(f"docker {args[0]} failed: {(done.stderr or done.stdout).strip()[:200]}")
        return False
    return True


def _status() -> str:
    """What the server says of itself: "healthy", "initializing", "error", or
    "" when it does not answer at all."""
    try:
        answer = requests.get(f"{_root()}/health", timeout=5)
        if answer.status_code != 200:
            return ""
        return str(answer.json().get("status") or "")
    except (requests.RequestException, ValueError, AttributeError):
        return ""


def is_ready() -> bool:
    """Whether the server answers and has its model loaded."""
    return _status() == "healthy"


def ensure_running() -> bool:
    """Have the server ready to speak, starting its container if need be.

    With no container configured this is a no-op that returns True: the
    request goes out as it always did, and fails on its own if nothing is
    listening.
    """
    name = _container()
    if not name or is_ready():
        return True

    logger.info(f"starting the {name} container for the narration")
    if not _docker("start", name):
        return False

    timeout = float(config.chatterbox.get("start_timeout", DEFAULT_START_TIMEOUT)
                    or DEFAULT_START_TIMEOUT)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _status()
        if status == "healthy":
            logger.info(f"{name} is ready")
            return True
        if status == "error":
            # The model failed to load, and the server does not try again:
            # waiting out the timeout would only delay saying so.
            logger.error(f"{name} could not load its model; "
                         f"see: docker logs --tail 50 {name}")
            _docker("stop", "-t", "5", name)
            return False
        time.sleep(POLL_SECONDS)
    logger.error(f"{name} did not load its model within {timeout:.0f}s; "
                 f"see: docker logs --tail 50 {name}")
    return False


def release() -> bool:
    """Stop the container, giving its VRAM back. Never raises.

    Returns whether a stop was sent. A failed tidy-up is not a failed
    narration: the audio is already on disk.
    """
    name = _container()
    if not name:
        return False
    try:
        stopped = _docker("stop", "-t", "5", name)
    except Exception as exc:  # noqa: BLE001 - tidy-up must not fail the caller
        logger.warning(f"could not stop {name} ({type(exc).__name__})")
        return False
    if stopped:
        logger.info(f"stopped {name}; the graphics card is free again")
    return stopped
