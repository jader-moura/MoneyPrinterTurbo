"""The generation sidecar: one model resident at a time, behind seven routes.

Z-Image-Turbo (and, later, a video model) on the local card, behind a plain
HTTP API. The queue, the disk cache and the retries belong to whoever calls
this, so this process has exactly one job: hold a model on the card and run
it.

    python local_services/genserver/server.py [--host 127.0.0.1] [--port 8730]

The invariant, and the reason this is a server rather than a script: **loading
a model unloads whatever was resident.** A 16 GB card holds one of these at a
time, and the next entry in the registry is a video model.

Routes:
    GET  /health                 resident model, VRAM, torch, arch list, capability
    GET  /models                 the registry, as declared
    POST /load                   {"model": id}
    POST /generate               {"model", "prompt", "width", "height", "seed", ...}
    POST /unload
    GET  /v1/models              the registry in OpenAI's shape
    POST /v1/images/generations  {"model", "prompt", "size", "n"} -> data[].b64_json

The `/v1` pair is a thin translation of the two routes above it, nothing more:
it exists so that MoneyPrinterTurbo's `video_source = "openai_image"` — which
speaks only OpenAI's images API — can drive this server without a shim in the
middle. The native routes keep working exactly as they did; anything already
calling them is unaffected.

Two things here are about *sharing one card* rather than about generating:

  * `unload_ollama()` runs before every generation, because an Ollama model
    that is still resident leaves too little VRAM for the transformer. Set
    `GEN_UNLOAD_OLLAMA=0` to leave Ollama alone.
  * `GEN_IDLE_SECONDS` drops the pipeline entirely after a quiet spell, which
    hands back host RAM for the next stage. Off by default: reloading
    Z-Image means re-reading 24.6 GB of fp32 weights from disk, so this is
    worth it only when something else needs the memory.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import gc
import io
import json
import math
import os
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from registry import EDITORS, OUTLINES, REGISTRY, ModelSpec, as_json, parse_size, spec_for  # noqa: E402

# Importing torch must not take the server down: `/health` reporting a broken
# install is the whole point of `doctor` asking this process rather than
# guessing. The error is carried and reported, not raised at startup.
TORCH_IMPORT_ERROR: Optional[str] = None
# Give freed memory back to Windows at once. Must be set before torch loads.
#
# The Windows build of torch allocates CPU memory through mimalloc, which keeps
# what is freed for later instead of returning it. Measured on this server:
# after one picture, dropping Z-Image left 34.8 GB of commit charge held with
# no tensor alive, and the next model then loaded on top of it (49.5 GB) - the
# machine ran into its commit limit during a model switch and the process died
# with nothing in the log. With this, the same drop leaves 3.1 GB.
# `setdefault`, so a value set by hand still wins.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

try:
    import torch
except Exception as err:  # pragma: no cover - depends on the machine
    torch = None  # type: ignore[assignment]
    TORCH_IMPORT_ERROR = f"{type(err).__name__}: {err}"

MB = 1024 * 1024

# Sharing the card with Ollama. `requests` arrives with huggingface_hub, so
# this costs no new dependency; a missing one must not stop the server from
# generating, only from tidying up after Ollama.
try:
    import requests
except Exception:  # pragma: no cover - requests ships with huggingface_hub
    requests = None  # type: ignore[assignment]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_UNLOAD_TIMEOUT = float(os.environ.get("OLLAMA_UNLOAD_TIMEOUT", "30"))


def unload_ollama_wanted() -> bool:
    return os.environ.get("GEN_UNLOAD_OLLAMA", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


# Dropping the pipeline after a quiet spell. 0 disables it, which is the
# default: the reload is expensive and worth paying only on purpose.
# --- commit charge ---------------------------------------------------------
#
# Not working set. The failure that cost an afternoon had a working set of
# 0.03 GB and 61.25 GB of commit charge, on a machine whose commit limit is
# 79.92 GB with 78.19 GB already used. Past that point every allocation fails
# whatever its size, and none of the errors mention memory pressure: a 40 MB
# CUDA allocation reports "out of memory" with 5.76 GB of VRAM free, the next
# call reports "unknown error", and numpy cannot get 15 MB for an array. So
# this is the number to watch, and the only one that would have said what was
# wrong.

# Past here this process is bigger than working costs. Measured on a 5060 Ti
# with CPU offload, rather than guessed: 23 GB with the model resident and the
# img2img pipeline built, 34 GB after one redraw - the jump is CPU offload
# materialising its host-side copy of the transformer on first use, which is
# how the design works and does not come back. A first guess of 28 GB sat
# *below* that and refused the request after the one that had just succeeded.
# Six redraws in a row then settled it: 41, 41, 43, 43, 43, 43 GB. So working
# costs 43 GB on this card and a 44 GB ceiling was one gigabyte of headroom -
# the backstop would have fired on an ordinary afternoon. 56 GB sits above
# working cost with room to spare and still catches the 61 GB accumulation
# that started all this.
MAX_OWN_COMMIT_MB = int(float(os.environ.get("GEN_MAX_COMMIT_GB", "56")) * 1024)

# Refuse to start work when the machine has less commit charge left than this.
#
# Small on purpose, and nearly off. On Windows the commit limit is RAM plus a
# pagefile that the system resizes by itself: it read 79.9 GB, then 66.6 GB,
# then 61 GB here within an hour, and "free commit" shrinks with it without
# anything being wrong. A first guess of 4 GB turned into the whole failure -
# this machine idles at about 1 GB free with a genserver resident, so every
# redraw after the first one was refused with "the machine has 1.0 GB of
# commit charge left and this needs about 4 GB". Only the last GB before an
# allocation really fails is worth refusing for.
MIN_FREE_COMMIT_MB = int(float(os.environ.get("GEN_MIN_FREE_COMMIT_GB", "0.5")) * 1024)

# Extra commit charge demanded before a *load*, on top of the above. Zero by
# default for the same reason: a load that needs more than the current limit
# makes the system grow its pagefile, which is what a pagefile is for. Set it
# on a machine with a fixed pagefile, where the limit really is a wall and
# running out mid-load surfaces as "the paging file is too small" (error 1455).
LOAD_COMMIT_MB = int(float(os.environ.get("GEN_LOAD_COMMIT_GB", "0")) * 1024)

# When the machine counts as in trouble, for the runaway backstop below. Kept
# separate from the two above: that one decides whether to refuse a request,
# this one decides whether a process holding more than the ceiling is a
# problem worth dropping the model over.
CRITICAL_FREE_COMMIT_MB = int(
    float(os.environ.get("GEN_CRITICAL_FREE_COMMIT_GB", "2")) * 1024
)

# Set once this process is past MAX_OWN_COMMIT_MB and could not give it back,
# *and* the machine has no room left. Nothing clears it: only exiting returns
# commit charge.
BLOATED: Optional[str] = None

# Said once, when this process is large but nothing is suffering for it.
NOTED: Optional[str] = None

IDLE_SECONDS = float(os.environ.get("GEN_IDLE_SECONDS", "0"))
IDLE_CHECK_SECONDS = float(os.environ.get("GEN_IDLE_CHECK_SECONDS", "10"))

# What `/v1/images/generations` loads when a caller names no model. OpenAI's
# API makes `model` optional in practice and MoneyPrinterTurbo always sends
# one, but a default keeps curl honest.
DEFAULT_MODEL = os.environ.get("GEN_DEFAULT_MODEL", "zimage-turbo")

# OpenAI's images API caps `n`; so does this, because each image is a full
# denoise on a card that holds one model.
MAX_IMAGES_PER_REQUEST = 4


# A CUDA context can be lost for good while the process stays up: the
# display driver resets, the machine sleeps with a model resident, a kernel
# faults. Every call afterwards fails, often as something that reads like a
# different problem each time - a 40 MB allocation "out of memory" with six
# gigabytes free, then "unknown error". Watching for these means saying what
# happened once, instead of handing out a fresh traceback per click.
FATAL_CUDA = (
    "cuda error: unknown error",
    "cuda error: unspecified launch failure",
    "cuda error: illegal memory access",
    "an illegal memory access was encountered",
    "device-side assert triggered",
    "cuda error: misaligned address",
    "cublas_status_not_initialized",
    "no kernel image is available",
)

# The words of the first fatal error, once there has been one. Nothing clears
# it: the context does not come back, so only restarting this process does.
LOST: Optional[str] = None


class CardLost(Exception):
    """The card was lost and this process cannot get it back.

    Separate from `Busy`, which means "ask again in a moment". This one means
    "asking again will fail the same way until someone restarts the server",
    which is the one thing the reader needs to know.
    """


def lost_the_card(exc: BaseException) -> Optional[str]:
    """The fatal marker in this exception, or None if it is an ordinary error.

    A plain out-of-memory is **not** fatal: the next request may well fit, and
    a card that refuses work for ever because one picture was too big would be
    worse than the error it is avoiding.
    """
    said = f"{type(exc).__name__}: {exc}".lower()
    for marker in FATAL_CUDA:
        if marker in said:
            return marker
    return None


def remember_if_lost(exc: BaseException) -> None:
    global LOST
    marker = lost_the_card(exc)
    if marker is None or LOST is not None:
        return
    LOST = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    print(
        f"[genserver] the CUDA context is gone ({marker}). Every generation "
        "from here on will fail, and the resident model cannot be given back "
        "without restarting this process.",
        flush=True,
    )


class Leaking(Exception):
    """This process has taken more memory than it can account for.

    Separate from `Busy` ("ask again in a moment") and from `CardLost` ("the
    GPU is gone"): the machine is fine and the card is fine, and this process
    is the problem. Commit charge is only returned when a process exits, so
    there is nothing to wait for and nothing to free.
    """


class NoRoom(Exception):
    """The machine has no commit charge left to work with.

    Not this process's fault and not permanent, so it reads as "try again",
    unlike `Leaking`.
    """


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def own_commit_mb() -> Optional[int]:
    """This process's commit charge in MB, or None where it cannot be read.

    None means "do not guess": every guard below treats it as "carry on", so
    an unreadable number never stops the server from working.
    """
    if sys.platform != "win32":
        return _own_commit_posix_mb()
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    try:
        ask = ctypes.windll.psapi.GetProcessMemoryInfo
        # Declared, not left to ctypes to guess: the current-process
        # pseudo-handle is (HANDLE)-1, and without argtypes ctypes tries to
        # fit it into an int and raises "int too long to convert" - which,
        # swallowed below, would have left this guard silently measuring
        # nothing at all.
        ask.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        ask.restype = ctypes.c_int
        ok = ask(ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb)
    except Exception:
        return None
    if not ok:
        return None
    return int(counters.PagefileUsage // MB)


def _own_commit_posix_mb() -> Optional[int]:
    """`VmSize` on Linux: the nearest thing to the same question."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmSize:"):
                    return int(int(line.split()[1]) // 1024)
    except Exception:
        return None
    return None


def machine_commit_mb() -> "Optional[tuple[int, int]]":
    """(used, limit) commit charge for the whole machine, in MB."""
    if sys.platform != "win32":
        return None
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:
        return None
    if not ok or not status.ullTotalPageFile:
        return None
    limit = int(status.ullTotalPageFile // MB)
    free = int(status.ullAvailPageFile // MB)
    return limit - free, limit


# Everything that allocates or frees model memory runs on this one thread.
#
# The HTTP server answers each request on a new thread, and torch's allocator
# on Windows (mimalloc) keeps a heap per thread: memory freed by a thread other
# than the one that allocated it is not handed back, it waits for the owner.
# The owner was a request thread that had long since ended, so every picture
# and every model switch left its memory behind - measured, a model dropped
# from another thread gave back nothing (27 GB -> 27 GB), and dropped from the
# thread that loaded it gave back all of it (34.8 GB -> 3.1 GB).
_TORCH_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="torch")


def on_the_torch_thread(work):
    """Runs the function on the one thread that owns the model's memory.

    The caller waits for it and gets its result or its exception, so nothing
    else about the call changes - including which thread holds the lock.
    """
    @wraps(work)
    def there(*args, **kwargs):
        if threading.current_thread().name.startswith("torch"):
            return work(*args, **kwargs)
        return _TORCH_THREAD.submit(work, *args, **kwargs).result()
    return there


@on_the_torch_thread
def give_back_what_we_can() -> None:
    """Best effort, and never raises.

    Called after a failed generation as well as a successful one: a failure is
    exactly when the allocator is holding blocks nobody will ever read, and a
    card that has just faulted makes every one of these calls throw.
    """
    try:
        gc.collect()
    except Exception:
        pass
    if torch is None:
        return
    for attempt in (
        lambda: torch.cuda.empty_cache(),
        lambda: torch.cuda.ipc_collect(),
    ):
        try:
            attempt()
        except Exception:
            pass


def guard_memory(for_a_load: bool) -> None:
    """Refuse work the machine cannot pay for, before anything is allocated.

    Three different answers, because they need three different actions:
    this process is leaking (restart it), the machine is full (close
    something), or carry on.
    """
    if BLOATED is not None:
        raise Leaking(
            f"{BLOATED}. Commit charge is only given back when a process ends, "
            "so there is nothing to wait for: stop this server and start it "
            "again. Nothing else on the machine has to be touched."
        )
    room = machine_commit_mb()
    if room is None:
        return
    used, limit = room
    free = limit - used
    wanted = MIN_FREE_COMMIT_MB + (LOAD_COMMIT_MB if for_a_load else 0)
    if free >= wanted or wanted <= 0:
        return
    raise NoRoom(
        f"the machine has {free // 1024}.{(free % 1024) * 10 // 1024} GB of "
        f"commit charge left and this needs about {wanted // 1024} GB"
        + (" to load the model" if for_a_load else "")
        + f" ({used // 1024} of {limit // 1024} GB is already committed). "
        "Close something holding memory, or stop and start this server if it "
        "is the one holding it."
    )


def crowded() -> bool:
    """True when the machine no longer has room to load a model.

    This, not the size of this process, is the harm. A server holding 34 GB on
    a machine with 30 GB of commit charge still free is doing its job; the
    same server on a machine with 1 GB free is why a 40 MB allocation starts
    reporting "CUDA out of memory".
    """
    room = machine_commit_mb()
    if room is None:
        return False
    used, limit = room
    return (limit - used) < CRITICAL_FREE_COMMIT_MB


def watch_own_commit() -> None:
    """Notice this process growing, give back what can be given, then stop.

    The one thing that must never happen again is this process quietly taking
    the machine's commit charge until unrelated things start failing. So the
    growth is capped: one attempt at handing it back, and if that does not
    work it stops taking work and says to restart it.

    Being large is not by itself a reason to refuse work - that mistake cost
    the request right after the first redraw that ever succeeded. It takes
    both: past the ceiling *and* a machine with no room left.
    """
    global BLOATED, NOTED
    if BLOATED is not None:
        return
    mine = own_commit_mb()
    if mine is None or mine <= MAX_OWN_COMMIT_MB:
        return
    if not crowded():
        if NOTED is None:
            NOTED = f"{mine // 1024} GB of commit charge"
            print(
                f"[genserver] this process holds {NOTED}, past the "
                f"{MAX_OWN_COMMIT_MB // 1024} GB ceiling, but the machine has "
                "room. Carrying on, and saying it once in case it keeps "
                "growing.",
                flush=True,
            )
        return
    try:
        with held("letting go of the model"):
            if RESIDENT.model_id is not None:
                RESIDENT.unload()
    except Busy:
        # Mid-generation. Next tick.
        return
    give_back_what_we_can()
    after = own_commit_mb() or mine
    if after <= MAX_OWN_COMMIT_MB:
        print(
            f"[genserver] commit charge was {mine // 1024} GB; dropped the "
            f"model and gave back {(mine - after) // 1024} GB",
            flush=True,
        )
        return
    BLOATED = (
        f"this process holds {after // 1024} GB of commit charge, past the "
        f"{MAX_OWN_COMMIT_MB // 1024} GB ceiling, dropping the model gave back "
        f"only {(mine - after) // 1024} GB, and the machine has no room left"
    )
    print(
        f"[genserver] {BLOATED}. Refusing further work: left alone this is "
        "what makes unrelated allocations fail with messages about CUDA. "
        "Stop this server and start it again.",
        flush=True,
    )


class Busy(Exception):
    """The card is in use and this request will not wait for it.

    `/unload` and `/load` queue behind a generation, and a lock they wait on
    for ever turns into a client-side timeout reported as "could not reach the
    server" — which sends the reader to look for a process that is running
    perfectly well. Busy and unreachable are different answers and get
    different status codes.
    """


class Resident:
    """The one model on the card, and the lock that keeps it one."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.model_id: Optional[str] = None
        self.pipe: Any = None
        self.image_pipe: Any = None
        # The ControlNet pipeline, for redrawing a picture along its outlines.
        self.outline_pipe: Any = None
        # The handle of the hook that arranges the card each time the
        # ControlNet is called. See `following_outlines`.
        self.outline_hook: Any = None
        # How much commit charge building it cost, the last time it was built.
        self.image_pipe_grew_mb = 0
        self.loaded_at: float = 0.0
        # Readable without the lock, so /health answers during a generation.
        self.generating: Optional[str] = None

    @on_the_torch_thread
    def unload(self) -> float:
        """Drops whatever is resident and returns the free VRAM in MB after.

        Says what it gave back, because "dropping the model gave back only
        3 GB" is the kind of thing worth knowing before it matters.
        """
        was = self.model_id
        commit_before = own_commit_mb()
        self.pipe = None
        # The img2img pipeline holds the same transformer, text encoder and
        # VAE. Left behind it would keep all three alive after the unload and
        # the next load would find the card still full.
        self.image_pipe = None
        # Same for the ControlNet pipeline, plus the ControlNet it alone holds.
        self.outline_pipe = None
        self.outline_hook = None
        self.model_id = None
        self.loaded_at = 0.0
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            try:
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                torch.cuda.reset_peak_memory_stats()
            except Exception:
                # A card that cannot run a kernel cannot empty a cache either.
                # Nothing to do about it here; /health reports the real reason.
                pass
        if was is not None and commit_before is not None:
            commit_after = own_commit_mb()
            if commit_after is not None:
                print(
                    f"[genserver] dropped {was}; commit charge "
                    f"{commit_before // 1024} GB -> {commit_after // 1024} GB",
                    flush=True,
                )
        return free_mb()

    @on_the_torch_thread
    def load(self, spec: ModelSpec, offload: bool) -> dict:
        """Loads `spec`, unloading whatever was there first."""
        before = free_mb()
        started = time.monotonic()
        if self.model_id is not None:
            self.unload()

        import diffusers

        cls = getattr(diffusers, spec.pipeline, None)
        if cls is None:
            raise RuntimeError(
                f"diffusers {diffusers.__version__} has no {spec.pipeline}. "
                f"Upgrade it: pip install -U diffusers"
            )
        dtype = getattr(torch, spec.dtype)
        pipe = cls.from_pretrained(spec.repo, torch_dtype=dtype)
        if offload:
            pipe.enable_model_cpu_offload()
            # Decoding a 1536x864 image from latents in one piece is the
            # single biggest allocation after the transformer itself, and it
            # lands while the transformer is still on the card. Measured: a
            # second generation OOM'd asking for 76 MiB with 11.49 GiB held.
            # Slicing and tiling trade a little time for a flat VAE peak.
            for enable in ("enable_slicing", "enable_tiling"):
                getattr(pipe.vae, enable, lambda: None)()
        else:
            pipe.to("cuda")

        self.pipe = pipe
        self.image_pipe = None
        self.outline_pipe = None
        self.outline_hook = None
        self.model_id = spec.id
        self.loaded_at = time.time()
        return {
            "resident": spec.id,
            "seconds": round(time.monotonic() - started, 2),
            "freeMbBefore": before,
            "freeMbAfter": free_mb(),
            "offload": offload,
        }

    @on_the_torch_thread
    def starting_from_image(self, spec: ModelSpec) -> Any:
        """The img2img pipeline over the weights that are already resident.

        ``from_pipe`` shares the transformer, the text encoder and the VAE with
        the text-to-image pipeline, and carries the CPU-offload hooks across.
        Loading a second pipeline from the repo instead would ask a 16 GB card
        for a second 12 GB transformer and fail.

        **The dtype has to be named.** ``from_pipe`` ends in
        ``new_pipeline.to(dtype=dtype)``, and with nothing passed that dtype
        is ``torch.float32``:

            torch_dtype = kwargs.pop("torch_dtype", None)
            dtype = _resolve_dtype(kwargs.pop("dtype", None), torch_dtype) or torch.float32

        So a bfloat16 transformer gets rebuilt at twice the size, every
        parameter a new tensor, while the originals stay referenced by the
        resident pipeline. Measured here: **+29786 MB of commit charge** for
        one redraw, which took the machine to 79 of 79 GB committed and then
        surfaced as a 40 MB CUDA allocation failing "out of memory" with
        5.76 GB of VRAM free, and then as "CUDA error: unknown error". Passing
        the dtype it was loaded with makes the cast a no-op.
        """
        if not spec.img2img:
            raise ValueError(
                f"{spec.id} cannot start from a picture: the registry declares no "
                f"img2img pipeline for it."
            )
        if self.model_id != spec.id or self.pipe is None:
            raise RuntimeError(
                f"{spec.id} is not resident, so there is nothing to build an "
                f"img2img pipeline from."
            )
        if self.image_pipe is not None:
            return self.image_pipe

        import diffusers

        cls = getattr(diffusers, spec.img2img, None)
        if cls is None:
            raise RuntimeError(
                f"diffusers {diffusers.__version__} has no {spec.img2img}. "
                f"Upgrade it: pip install -U diffusers"
            )
        # Measured, because this is the one step whose cost nobody knew. A
        # fresh server went to 52 GB of commit charge drawing its first
        # redraw, and the number that matters is the jump across this line:
        # `from_pipe` shares the weights, but the hooks, the pinned host
        # buffers and anything accelerate re-materialises are not free, and
        # pinned memory is commit charge that cannot be paged out.
        # Named, not left to default to float32. See the docstring.
        dtype = None if torch is None else getattr(torch, spec.dtype, None)
        same_dtype = {} if dtype is None else {"torch_dtype": dtype}
        before = own_commit_mb()
        built = cls.from_pipe(self.pipe, **same_dtype)
        after = own_commit_mb()
        if before is not None and after is not None:
            self.image_pipe_grew_mb = after - before
            print(
                f"[genserver] img2img pipeline built: commit charge "
                f"{before // 1024} GB -> {after // 1024} GB "
                f"(+{self.image_pipe_grew_mb} MB)",
                flush=True,
            )
            if after > MAX_OWN_COMMIT_MB:
                # Let go of it in the same breath. Keeping it would leave the
                # process over the ceiling with a pipeline it is not allowed
                # to use, and the next request would be refused anyway.
                del built
                give_back_what_we_can()
                now = own_commit_mb() or after
                raise Leaking(
                    f"building the img2img pipeline took this process from "
                    f"{before // 1024} GB to {after // 1024} GB of commit "
                    f"charge, past the {MAX_OWN_COMMIT_MB // 1024} GB ceiling "
                    f"({now // 1024} GB after letting go of it again). The "
                    f"picture was not drawn. Starting from a photograph is "
                    f"what costs this, so a plain generation may still work; "
                    f"restart this server either way."
                )
        self.image_pipe = built
        return self.image_pipe


    @on_the_torch_thread
    def following_outlines(self, spec: ModelSpec) -> Any:
        """The ControlNet pipeline over the weights that are already resident.

        Built like the img2img one: `from_pipe`, so the transformer, the text
        encoder and the VAE are shared, and with the dtype named so nothing is
        rebuilt as float32 (see `starting_from_image`).

        The ControlNet itself is the one new thing: 1.55 GB as bf16. It is not
        in the pipeline's offload order, so nothing would ever move it. It gets
        a pre-forward hook here that arranges the card each time it is called,
        and `generate` clears the card again when the picture is done, so a
        plain generation never pays for it.

        **Why the hook moves the transformer too, and not just the ControlNet.**
        The ControlNet does not own all of its layers: `from_transformer` hands
        it the transformer's own embedders and refiners, the very same modules.
        Moving the ControlNet therefore moves *part of the transformer*. And
        accelerate's offload hook decides whether the transformer still needs
        moving by looking at its first parameter only:

            current_device = next(module.parameters()).device
            if current_device == self.execution_device:
                return args, kwargs

        That first parameter is one of the shared ones. Measured, twice: with
        only the ControlNet moved, the transformer's hook saw "already on
        cuda", skipped the move, and the first main layer failed with
        "Expected all tensors to be on the same device ... cuda:0 ... cpu".
        So the whole transformer goes to the card together with it - after the
        text encoder has been taken off, because the three do not fit at once.
        """
        outline = OUTLINES.get(spec.id)
        if outline is None:
            raise ValueError(
                f"{spec.id} cannot follow a picture's outlines: the registry "
                f"declares no ControlNet for it."
            )
        if self.model_id != spec.id or self.pipe is None:
            raise RuntimeError(
                f"{spec.id} is not resident, so there is nothing to build a "
                f"ControlNet pipeline from."
            )
        if self.outline_pipe is not None:
            return self.outline_pipe

        import diffusers
        from huggingface_hub import hf_hub_download

        cls = getattr(diffusers, outline.pipeline, None)
        net_cls = getattr(diffusers, outline.model, None)
        if cls is None or net_cls is None:
            raise RuntimeError(
                f"diffusers {diffusers.__version__} has no {outline.pipeline} / "
                f"{outline.model}. Upgrade it: pip install -U diffusers"
            )
        dtype = getattr(torch, spec.dtype)
        before = own_commit_mb()
        net = net_cls.from_single_file(
            hf_hub_download(outline.repo, filename=outline.filename),
            torch_dtype=dtype,
        )
        built = cls.from_pipe(self.pipe, controlnet=net, torch_dtype=dtype)
        after = own_commit_mb()
        if before is not None and after is not None:
            print(
                f"[genserver] ControlNet pipeline built: commit charge "
                f"{before // 1024} GB -> {after // 1024} GB (+{after - before} MB)",
                flush=True,
            )
            if after > MAX_OWN_COMMIT_MB:
                del built, net
                give_back_what_we_can()
                raise Leaking(
                    f"building the ControlNet pipeline took this process from "
                    f"{before // 1024} GB to {after // 1024} GB of commit "
                    f"charge, past the {MAX_OWN_COMMIT_MB // 1024} GB ceiling. "
                    f"The picture was not drawn; restart this server."
                )
        # **The two take turns on the card.** With both resident the first
        # picture took 576 s - a minute a step - at a peak of 15779 MB on a
        # 16283 MB card: the transformer's 12 GB, the ControlNet's 1.55 GB and
        # the desktop's 2.6 GB do not fit, and the driver spills to host
        # memory. Each step calls the ControlNet and then the transformer,
        # never both at once, so while the ControlNet runs the last few of the
        # transformer's main layers (which it does not use) wait on the CPU,
        # and while the transformer runs the ControlNet's own layers do.
        parked = list(built.transformer.layers)[-OUTLINE_PARKED_LAYERS:]
        own = [
            child for name, child in built.controlnet.named_children()
            if name not in OUTLINE_SHARED_MODULES
        ]

        def make_room(module: Any, args: Any) -> None:
            encoder = built.text_encoder
            if next(encoder.parameters()).device.type != "cpu":
                encoder.to("cpu")
            for layer in parked:
                layer.to("cpu")
            torch.cuda.empty_cache()
            # Its own layers and the ones it borrows from the transformer.
            module.to("cuda")

        def give_it_back(module: Any, args: Any, output: Any) -> None:
            for child in own:
                child.to("cpu")
            torch.cuda.empty_cache()
            # Everything that is not there yet: all of it before the first
            # step, the parked layers before every later one. After this the
            # transformer's own hook finds it on the card and does nothing.
            built.transformer.to("cuda")

        self.outline_hook = (
            built.controlnet.register_forward_pre_hook(make_room),
            built.controlnet.register_forward_hook(give_it_back),
        )
        self.outline_pipe = built
        return self.outline_pipe


# What the ControlNet borrows from the transformer (`from_transformer`): the
# same modules, so moving them moves part of the transformer.
OUTLINE_SHARED_MODULES = frozenset({
    "t_embedder", "all_x_embedder", "cap_embedder", "rope_embedder",
    "noise_refiner", "context_refiner",
})
# How many of the transformer's main layers step off the card while the
# ControlNet runs. Each is about 0.35 GB as bf16; the ControlNet needs 1.55 GB
# for itself plus what it works in.
OUTLINE_PARKED_LAYERS = int(os.environ.get("GEN_OUTLINE_PARKED_LAYERS", "6"))

RESIDENT = Resident()

# How long /load and /unload will wait for the card before saying it is busy.
# Short on purpose: a generation runs for minutes and the caller should be
# told that, not left to time out and conclude the server is dead.
BUSY_WAIT_SECONDS = 2.0


@contextmanager
def held(what: str) -> Any:
    """The resident lock, or `Busy` rather than a wait with no end.

    Only for the routes that *manage* the card. A generation waits its turn:
    the Node side already queues at one in flight, so a second one arriving is
    a caller that meant it.
    """
    if not RESIDENT.lock.acquire(timeout=BUSY_WAIT_SECONDS):
        busy = RESIDENT.generating
        raise Busy(
            f"the card is busy{'' if busy is None else f' generating with {busy}'}, so {what} would have to wait. "
            "Try again when it finishes."
        )
    try:
        yield
    finally:
        RESIDENT.lock.release()


def cuda_ready() -> bool:
    return torch is not None and torch.cuda.is_available()


def free_mb() -> Optional[int]:
    """Free VRAM on the card, in MB, or None when the card cannot be asked."""
    if not cuda_ready():
        return None
    try:
        free, _total = torch.cuda.mem_get_info()
        return int(free // MB)
    except Exception:
        return None


def total_mb() -> Optional[int]:
    if not cuda_ready():
        return None
    try:
        _free, total = torch.cuda.mem_get_info()
        return int(total // MB)
    except Exception:
        return None


def capability() -> Optional[str]:
    """This device's compute capability, e.g. "12.0" for Blackwell.

    The number that decides whether the installed wheel can run here at all.
    `torch.cuda.is_available()` answers a different question and says True on
    a wheel that cannot launch a single kernel on this card.
    """
    if not cuda_ready():
        return None
    try:
        major, minor = torch.cuda.get_device_capability(0)
        return f"{major}.{minor}"
    except Exception:
        return None


def health() -> dict:
    arch_list: list[str] = []
    if torch is not None:
        try:
            arch_list = list(torch.cuda.get_arch_list())
        except Exception:
            arch_list = []
    return {
        # Up is not the same as able to work. A process that has lost the card
        # answers every request and generates nothing, so saying ok here would
        # send the reader looking anywhere but at the real problem.
        "ok": TORCH_IMPORT_ERROR is None and LOST is None and BLOATED is None,
        "lost": LOST,
        "bloated": BLOATED,
        "noted": NOTED,
        # So "is it growing?" is one request rather than an afternoon.
        "commit": commit_report(),
        # The process actually holding the card, so a client can stop a
        # sidecar it did not start. On Windows this is the real interpreter,
        # not the .venv redirector stub that launched it, which is exactly
        # the pid a tree-kill has to be given.
        "pid": os.getpid(),
        "resident": RESIDENT.model_id,
        # Never behind the lock: a /health that waits for a generation is a
        # /health that cannot report one.
        "generating": RESIDENT.generating,
        "models": sorted(REGISTRY),
        "torch": {
            "version": None if torch is None else torch.__version__,
            "cuda": None if torch is None else torch.version.cuda,
            "available": cuda_ready(),
            "archList": arch_list,
            "importError": TORCH_IMPORT_ERROR,
        },
        "device": {
            "name": device_name(),
            "capability": capability(),
            "freeMb": free_mb(),
            "totalMb": total_mb(),
        },
    }


def commit_report() -> dict:
    """What this process and the machine have committed, in MB."""
    room = machine_commit_mb()
    return {
        "ownMb": own_commit_mb(),
        "ceilingMb": MAX_OWN_COMMIT_MB,
        "machineUsedMb": None if room is None else room[0],
        "machineLimitMb": None if room is None else room[1],
    }


def device_name() -> Optional[str]:
    if not cuda_ready():
        return None
    try:
        return str(torch.cuda.get_device_name(0))
    except Exception:
        return None


def offload_wanted(spec: ModelSpec) -> bool:
    env = os.environ.get("GEN_OFFLOAD", "").strip()
    if env in {"1", "true", "yes"}:
        return True
    return spec.offload


def ollama_models() -> list[str]:
    """What Ollama currently holds on the card, or nothing if it cannot be asked.

    An Ollama that is not running is the normal case for this server, not an
    error: `/api/ps` refusing the connection means there is nothing to unload.
    """
    if requests is None:
        return []
    try:
        response = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        models = response.json().get("models", [])
    except Exception:
        return []
    if not isinstance(models, list):
        return []
    return [m["name"] for m in models if isinstance(m, dict) and m.get("name")]


def unload_ollama() -> list[str]:
    """Ask Ollama to let go of the card, and wait until it has.

    `keep_alive: 0` on `/api/generate` is Ollama's own way of saying "drop this
    model now". The wait matters: the POST returns before the VRAM is actually
    back, and loading a 13 GB transformer into memory Ollama has not yet
    released is the OOM this exists to avoid.

    Returning rather than raising when Ollama will not budge: a picture that
    might still fit beats refusing to try.
    """
    if not unload_ollama_wanted() or requests is None:
        return []
    names = ollama_models()
    if not names:
        return []
    for name in names:
        try:
            requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=30,
            )
        except Exception:
            pass
    deadline = time.monotonic() + OLLAMA_UNLOAD_TIMEOUT
    left = ollama_models()
    while left and time.monotonic() < deadline:
        time.sleep(0.5)
        left = ollama_models()
    if left:
        print(
            f"  ! ollama still holds {', '.join(left)} after "
            f"{OLLAMA_UNLOAD_TIMEOUT:g}s; generating anyway",
            file=sys.stderr,
        )
    else:
        print(f"  unloaded ollama: {', '.join(names)}", file=sys.stderr)
    return names


# When the last generation finished, for the idle worker. Written under the
# resident lock, read without it: a stale read only delays an unload by one
# tick.
LAST_USED = 0.0
STOP_IDLE = threading.Event()
IDLE_THREAD: Optional[threading.Thread] = None


def unload_if_idle() -> bool:
    """Drop the resident model once it has been idle long enough.

    Uses `held` rather than the raw lock, so a generation in flight is left
    alone and retried on the next tick instead of queueing behind it.
    """
    if IDLE_SECONDS <= 0 or RESIDENT.model_id is None:
        return False
    if time.monotonic() - LAST_USED < IDLE_SECONDS:
        return False
    try:
        with held("the idle unload"):
            if RESIDENT.model_id is None:
                return False
            if time.monotonic() - LAST_USED < IDLE_SECONDS:
                return False
            was = RESIDENT.model_id
            RESIDENT.unload()
    except Busy:
        return False
    print(f"  idle: unloaded {was}", file=sys.stderr)
    return True


def idle_worker() -> None:
    while not STOP_IDLE.wait(IDLE_CHECK_SECONDS):
        unload_if_idle()
        watch_own_commit()


def start_idle_worker() -> None:
    """Starts for the idle unload, and also just to watch the commit charge.

    GEN_IDLE_SECONDS is 0 by default, so before this there was no background
    thread at all and nothing was watching this process grow.
    """
    global IDLE_THREAD
    if IDLE_SECONDS <= 0 and own_commit_mb() is None:
        return
    STOP_IDLE.clear()
    IDLE_THREAD = threading.Thread(target=idle_worker, name="idle-unload", daemon=True)
    IDLE_THREAD.start()


def stop_idle_worker() -> None:
    STOP_IDLE.set()
    if IDLE_THREAD is not None:
        IDLE_THREAD.join(timeout=5)


# A starting picture arrives as base64 in a JSON body. Big enough for a 4K
# photograph, small enough that a mistyped field cannot make this process hold
# a gigabyte: a 16 MB JPEG is already far more picture than 1536x864 can use.
MAX_REFERENCE_MB = 16

# How far a picture is allowed to move from the one it started from, when the
# caller names no strength. 0.75 redraws nearly everything and keeps the
# composition — which is the point of starting from a photograph of the real
# thing, rather than copying it.
DEFAULT_STRENGTH = 0.75


def decode_reference(value: Any) -> Any:
    """A starting picture, from base64 (with or without a data: prefix).

    Decoded with Pillow here rather than handed to diffusers as bytes, both to
    refuse something that is not an image at all and to re-encode it: what
    comes back has no EXIF, no colour profile and no animation, whatever
    arrived.
    """
    from PIL import Image, UnidentifiedImageError

    if not isinstance(value, str) or not value.strip():
        raise ValueError("image must be a base64 string.")
    payload = value.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as err:
        raise ValueError(f"image is not valid base64: {err}") from None
    if not raw:
        raise ValueError("image is empty.")
    if len(raw) > MAX_REFERENCE_MB * MB:
        raise ValueError(
            f"image is {len(raw) // MB} MB; the limit is {MAX_REFERENCE_MB} MB."
        )
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            return opened.convert("RGB")
    except UnidentifiedImageError:
        raise ValueError("image is not a picture this server can read.") from None
    except Exception as err:
        raise ValueError(f"image could not be read: {err}") from None


def fit_reference(image: Any, width: int, height: int) -> Any:
    """The starting picture at exactly WIDTHxHEIGHT, by cover-crop.

    Letterboxing would hand the model black bars to denoise, and they come back
    as black bars. Squashing bends faces. Cover keeps the proportions and loses
    the edges, which is what a reference picture is for.
    """
    from PIL import Image

    if image.width == width and image.height == height:
        return image
    scale = max(width / image.width, height / image.height)
    grown = image.resize(
        (max(width, int(image.width * scale + 0.5)),
         max(height, int(image.height * scale + 0.5))),
        Image.LANCZOS,
    )
    left = (grown.width - width) // 2
    top = (grown.height - height) // 2
    return grown.crop((left, top, left + width, top + height))


def outlines_of(image: Any, keep: float = 0.12) -> Any:
    """The picture's outlines: thin white lines on black, as a Canny map looks.

    This is what the ControlNet is shown instead of the photograph. Colour,
    texture and lighting are gone — those are exactly what the style is meant
    to replace — and what is left is where every shape is.

    Written out with numpy rather than taken from OpenCV: it is forty lines,
    and it spares this interpreter a dependency whose only job would be this.
    Gradient strength from a Sobel pair, thinned along its own direction, and
    the strongest `keep` of the picture kept.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    grey = np.asarray(
        image.convert("L").filter(ImageFilter.GaussianBlur(1.4)), dtype=np.float32
    )
    pad = np.pad(grey, 1, mode="edge")
    gx = (pad[:-2, 2:] + 2 * pad[1:-1, 2:] + pad[2:, 2:]
          - pad[:-2, :-2] - 2 * pad[1:-1, :-2] - pad[2:, :-2])
    gy = (pad[2:, :-2] + 2 * pad[2:, 1:-1] + pad[2:, 2:]
          - pad[:-2, :-2] - 2 * pad[:-2, 1:-1] - pad[:-2, 2:])
    strength = np.hypot(gx, gy)
    angle = (np.rad2deg(np.arctan2(gy, gx)) + 180.0) % 180.0
    m = np.pad(strength, 1)
    here = m[1:-1, 1:-1]
    across = (angle < 22.5) | (angle >= 157.5)
    falling = (angle >= 22.5) & (angle < 67.5)
    down = (angle >= 67.5) & (angle < 112.5)
    ahead = np.where(across, m[1:-1, 2:], np.where(falling, m[2:, 2:],
                     np.where(down, m[2:, 1:-1], m[2:, :-2])))
    behind = np.where(across, m[1:-1, :-2], np.where(falling, m[:-2, :-2],
                      np.where(down, m[:-2, 1:-1], m[:-2, 2:])))
    thin = np.where((here >= ahead) & (here >= behind), here, 0.0)
    cut = float(np.quantile(strength, 1.0 - keep))
    lines = ((thin >= cut) & (thin > 0)).astype(np.uint8) * 255
    return Image.fromarray(lines).convert("RGB")


def generate(body: dict) -> dict:
    if LOST is not None:
        raise CardLost(
            f"this server lost the GPU ({LOST}) and cannot generate anything "
            "until it is restarted. Nothing here can recover it: the context "
            "is gone and the resident model is stuck in this process. Stop it "
            "and start it again."
        )
    model_id = str(body.get("model") or "").strip()
    # Before the lock and before anything is allocated. Reading `model_id`
    # unlocked can be a tick out of date, which only ever means asking for
    # slightly more or less headroom than the load will really need.
    guard_memory(for_a_load=RESIDENT.model_id != model_id)
    spec = spec_for(model_id)
    prompt = str(body.get("prompt") or "").strip()
    if prompt == "":
        raise ValueError("prompt is empty: there is nothing to generate.")
    if spec.emits != "image":
        # The shape is here so that a video entry is a registry addition, but
        # saying "not implemented" is honest where returning a still would not
        # be.
        raise NotImplementedError(
            f"{spec.id} emits {spec.emits}, and this server only generates images. "
            f"A video entry needs its own branch here."
        )

    width, height = parse_size(
        str(body.get("size") or f"{body.get('width', 0)}x{body.get('height', 0)}"), spec
    )
    steps = int(body.get("steps") or spec.steps)
    guidance = float(body["guidance"]) if body.get("guidance") is not None else spec.guidance

    # Starting from a picture. Absent, every line below is a no-op and this is
    # the same text-to-image call it always was.
    reference = body.get("image")
    strength = None
    # Following the picture's outlines instead of starting from it. Named, so a
    # caller that sends only `image` gets exactly what it always got.
    follow = body.get("follow")
    follow = None if follow is None or str(follow).strip() == "" else str(follow).strip()
    follow_scale = None
    control = None
    # The model that redraws a picture on instruction, when that is what was
    # asked for. A different model from `spec`: see `EDITORS`.
    editor = None
    if follow is not None:
        if follow not in ("outlines", "picture"):
            raise ValueError(
                f'follow must be "outlines" or "picture", not {follow!r}.'
            )
        if reference is None:
            raise ValueError("follow needs an image: there is nothing to follow.")
    if follow == "picture":
        editor = EDITORS.get(spec.id)
        if editor is None:
            raise ValueError(
                f"{spec.id} cannot redraw a picture on instruction: the "
                f"registry declares no editing model for it."
            )
        if body.get("negativePrompt") not in (None, ""):
            raise ValueError(
                f"{editor.id} takes no negative prompt; say what is wanted in "
                f"the instruction instead."
            )
        reference = fit_reference(decode_reference(reference), width, height)
        if body.get("steps") is None:
            steps = editor.steps
        if body.get("guidance") is None:
            guidance = editor.guidance
    elif follow is not None:
        outline = OUTLINES.get(spec.id)
        if outline is None:
            raise ValueError(
                f"{spec.id} cannot follow a picture's outlines: the registry "
                f"declares no ControlNet for it."
            )
        follow_scale = body.get("followScale")
        follow_scale = outline.scale if follow_scale is None else float(follow_scale)
        if not 0.0 < follow_scale <= 2.0:
            raise ValueError(
                f"followScale must be above 0 and at most 2, not {follow_scale}."
            )
        reference = fit_reference(decode_reference(reference), width, height)
        control = outlines_of(reference)
        if body.get("steps") is None:
            steps = outline.steps
    elif reference is not None:
        # Refused before anything is decoded: a 16 MB picture is not worth
        # reading to find out the entry could never have used it.
        if not spec.img2img:
            raise ValueError(
                f"{spec.id} cannot start from a picture: the registry declares no "
                f"img2img pipeline for it."
            )
        strength = body.get("strength")
        strength = DEFAULT_STRENGTH if strength is None else float(strength)
        if not 0.0 < strength <= 1.0:
            raise ValueError(
                f"strength must be above 0 and at most 1, not {strength}. "
                "1 ignores the picture; 0 would never leave it."
            )
        reference = fit_reference(decode_reference(reference), width, height)
        if body.get("steps") is None:
            # img2img joins the schedule partway down: diffusers runs about
            # ``steps * strength`` of them. The registry's count is what the
            # distilled model needs *in total*, so scale up to land back on it.
            # Capped at twice, because strength 0.1 would otherwise ask for 90
            # steps and fifteen minutes.
            steps = min(int(math.ceil(spec.steps / strength)), spec.steps * 2)
    seed = body.get("seed")
    # Always a real number on the way out: a picture whose seed was implicit
    # cannot be made again, and the cache key needs it.
    seed = int(seed) if seed is not None else random.randrange(0, 2**31 - 1)
    negative = body.get("negativePrompt")
    negative = None if negative is None or str(negative).strip() == "" else str(negative)

    if not cuda_ready():
        raise RuntimeError(
            "No usable CUDA device. "
            + (TORCH_IMPORT_ERROR or "torch reports no GPU; check the venv's wheel.")
        )

    # The model that will actually be on the card for this picture.
    drawing = editor or spec

    # A generation waits its turn rather than being refused: the Node side
    # queues at one in flight, so a second arrival is a caller that meant it.
    global LAST_USED
    with RESIDENT.lock:
        RESIDENT.generating = drawing.id
        try:
            # Before every generation, not just before the load: Ollama can
            # come back between two images, and the transformer needs the whole
            # card either way.
            unloaded_ollama = unload_ollama()
            loaded = None
            if RESIDENT.model_id != drawing.id:
                # Both sides of the load, so "what does a resident model
                # cost?" stops being a guess.
                commit_before_load = own_commit_mb()
                loaded = RESIDENT.load(drawing, offload_wanted(drawing))
                loaded["commitMbBefore"] = commit_before_load
                loaded["commitMbAfter"] = own_commit_mb()

            # Hand back what the last generation cached before starting this
            # one. `maybe_free_model_hooks` returns the modules to the CPU but
            # the caching allocator keeps their blocks, and the next call then
            # fails on a small allocation with gigabytes nominally free.
            torch.cuda.empty_cache()
            before = free_mb()
            torch.cuda.reset_peak_memory_stats()
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "height": height,
                "width": width,
                "num_inference_steps": steps,
                "guidance_scale": guidance,
                "generator": torch.Generator("cuda").manual_seed(seed),
            }
            if negative is not None:
                kwargs["negative_prompt"] = negative
            pipe = RESIDENT.pipe
            if editor is not None:
                # Shown the picture itself; the prompt is what to do with it.
                kwargs["image"] = reference
            elif control is not None:
                pipe = RESIDENT.following_outlines(spec)
                kwargs["control_image"] = control
                kwargs["controlnet_conditioning_scale"] = follow_scale
            elif reference is not None:
                # Built from the resident pipeline, not loaded again: same
                # weights, same offload hooks, no second 12 GB.
                pipe = RESIDENT.starting_from_image(spec)
                kwargs["image"] = reference
                kwargs["strength"] = strength
            # On the thread that loaded the weights: drawing moves them to
            # the card and back, and that is memory being freed and allocated.
            @on_the_torch_thread
            def draw() -> Any:
                if control is None:
                    return pipe(**kwargs).images[0]
                try:
                    return pipe(**kwargs).images[0]
                finally:
                    # Off the card again, the transformer with it: the two
                    # share modules, and a transformer left half on the card
                    # is exactly what `following_outlines` describes.
                    pipe.transformer.to("cpu")
                    pipe.controlnet.to("cpu")
                    torch.cuda.empty_cache()

            image = draw()
            seconds = round(time.monotonic() - started, 2)
            peak = int(torch.cuda.max_memory_allocated() // MB)

            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        except Exception as exc:
            remember_if_lost(exc)
            # A failed generation is exactly when the allocator is left
            # holding blocks nobody will read again.
            give_back_what_we_can()
            raise
        finally:
            RESIDENT.generating = None
            LAST_USED = time.monotonic()

    # Every generation, so what this costs is on the record rather than
    # reconstructed afterwards from the one that failed.
    print(
        f"[genserver] drew {width}x{height} in {seconds}s"
        + (f" from a photograph, on instruction, with {drawing.id}"
           if editor is not None
           else " along a photograph's outlines" if control is not None
           else " from a photograph" if reference is not None else "")
        + f"; peak VRAM {peak} MB, commit charge {(own_commit_mb() or 0) // 1024} GB",
        flush=True,
    )
    return {
        "model": spec.id,
        # The model that drew it, which is another one when the picture was
        # redrawn on instruction. Its repo and licence, for the credit.
        "drawnBy": drawing.id,
        "repo": drawing.repo,
        "license": drawing.license,
        "prompt": prompt,
        "negativePrompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        # So a caller (and a log) can tell a redraw from a fresh picture.
        "fromImage": reference is not None,
        "strength": strength,
        # "outlines" when the picture was held in place by its outlines.
        "follow": follow,
        "followScale": follow_scale,
        "seconds": seconds,
        "peakAllocatedMb": peak,
        "freeMbBefore": before,
        "freeMbAfter": free_mb(),
        # In every answer, so growth shows up in a log that is already kept.
        "commitMb": own_commit_mb(),
        "img2imgCommitMb": RESIDENT.image_pipe_grew_mb if reference is not None else 0,
        "offload": offload_wanted(drawing),
        "load": loaded,
        "unloadedOllama": unloaded_ollama,
        "pngBase64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


class BadRequestBody(ValueError):
    """A body this server will not even try to generate from.

    Separate from the `ValueError`s `registry.parse_size` raises because the
    OpenAI images API answers a malformed request with 422, and the callers
    that matter here — MoneyPrinterTurbo's `openai_image` source among them —
    tell 422 apart from the 400 a native `/generate` returns.
    """


def openai_size(requested: Optional[str], spec: ModelSpec) -> "tuple[int, int]":
    """The size to paint, from an OpenAI `size` string.

    Three cases, in order:

      * nothing, `""` or `"auto"` — the entry's own first declared size, which
        for Z-Image-Turbo is 1536x864: 16:9 at close to the pixel count the
        model was trained for.
      * a size the entry declares — painted exactly.
      * any other well-formed size — the declared size whose aspect ratio is
        closest. This is what makes the caller's own defaults work untouched:
        MoneyPrinterTurbo asks for 1536x1024, 1024x1536 or 1024x1024 when
        `openai_image_size` is empty, and a video resolution like 1920x1080
        arrives here too. Snapping by aspect keeps the framing and keeps the
        pixel count where the model is good, rather than refusing or painting
        a 2 megapixel image slowly.

    Anything that is not `WIDTHxHEIGHT` with positive numbers raises, and the
    route turns that into 422.
    """
    text = (requested or "").strip().lower()
    if text in {"", "auto"}:
        text = spec.sizes[0]
    width, separator, height = text.partition("x")
    if not (separator and width.isdigit() and height.isdigit()):
        raise BadRequestBody(
            f'size must be WIDTHxHEIGHT with positive integers, e.g. {spec.sizes[0]}, not "{requested}".'
        )
    want_w, want_h = int(width), int(height)
    if want_w <= 0 or want_h <= 0:
        raise BadRequestBody(
            f'size must be WIDTHxHEIGHT with positive integers, e.g. {spec.sizes[0]}, not "{requested}".'
        )

    declared = [tuple(int(v) for v in size.split("x")) for size in spec.sizes]
    if (want_w, want_h) in declared:
        return want_w, want_h
    wanted_ratio = want_w / want_h
    return min(declared, key=lambda size: abs(size[0] / size[1] - wanted_ratio))


def openai_images(body: dict) -> dict:
    """`POST /v1/images/generations`, translated onto `generate` above.

    A translation and nothing else: the card, the lock, the Ollama unload and
    the registry's step count all belong to `generate`, and this route only
    renames fields on the way in and out. `n` images are `n` calls, because one
    model is resident and each image is a full denoise.

    Z-Image-Turbo runs at guidance 0 — it is distilled, so classifier-free
    guidance is off and a `negative_prompt` would be ignored. Style belongs in
    the prompt instead; MoneyPrinterTurbo has `openai_image_prompt_template`
    for exactly that.
    """
    model_id = str(body.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        spec = spec_for(model_id)
    except KeyError as err:
        # 422 rather than the native 404: to an OpenAI client an unknown model
        # is a field it got wrong, not a missing endpoint. `args[0]` rather
        # than `str(err)`, because KeyError's str() wraps the message in the
        # repr quotes and the message already contains quotes of its own.
        raise BadRequestBody(str(err.args[0]) if err.args else str(err)) from None

    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise BadRequestBody("prompt is empty: there is nothing to generate.")

    response_format = str(body.get("response_format") or "b64_json").strip().lower()
    if response_format not in {"b64_json", ""}:
        raise BadRequestBody(
            f'response_format "{response_format}" is not available: this server returns '
            "b64_json, because it keeps no files to serve a url from."
        )

    raw_n = body.get("n", 1)
    try:
        count = int(raw_n if raw_n is not None else 1)
    except (TypeError, ValueError):
        raise BadRequestBody(f'n must be a whole number, not "{raw_n}".') from None
    if count < 1 or count > MAX_IMAGES_PER_REQUEST:
        raise BadRequestBody(
            f"n must be between 1 and {MAX_IMAGES_PER_REQUEST}; each image is a full "
            "denoise on a card that holds one model."
        )

    width, height = openai_size(body.get("size"), spec)

    # An extension to the OpenAI shape, not a translation of it: `image` and
    # `strength` are what OpenAI puts behind a multipart `/v1/images/edits`,
    # and a client that never sends them sees no difference at all. They ride
    # on this route so that a caller which already speaks it — the
    # `openai_image` source in MoneyPrinterTurbo — needs no second client,
    # no multipart encoder and no second base url.
    reference = body.get("image")
    if reference is not None and not spec.img2img:
        raise BadRequestBody(
            f"{spec.id} cannot start from a picture: the registry declares no "
            f"img2img pipeline for it."
        )

    data = []
    for index in range(count):
        # A seed per image, so `n > 1` is n different pictures rather than n
        # copies. An explicit seed still pins the first one.
        seed = body.get("seed")
        if seed is not None:
            seed = int(seed) + index
        request: dict[str, Any] = {
            "model": spec.id,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "seed": seed,
        }
        if reference is not None:
            request["image"] = reference
            if body.get("strength") is not None:
                request["strength"] = body.get("strength")
            # Same extension, one more word: redraw along the outlines.
            for key in ("follow", "followScale", "steps"):
                if body.get(key) is not None:
                    request[key] = body.get(key)
        result = generate(request)
        data.append({"b64_json": result["pngBase64"], "revised_prompt": prompt})

    return {
        "created": int(time.time()),
        "data": data,
        # Not in OpenAI's response and harmless to a client that ignores it;
        # the one number worth having in a log when the size was snapped.
        "size": f"{width}x{height}",
    }


def openai_models() -> dict:
    """The registry in the shape `GET /v1/models` is expected to have."""
    return {
        "object": "list",
        "data": [
            {
                "id": spec.id,
                "object": "model",
                "owned_by": spec.repo,
                "created": 0,
            }
            for spec in REGISTRY.values()
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "docreel-genserver/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # One line per request, on stderr, without the default's timestamp
        # noise. The Node side is what the operator reads.
        sys.stderr.write(f"  {self.command} {self.path} {fmt % args}\n")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf8"))
        except Exception as err:
            raise ValueError(f"Body is not JSON: {err}") from None
        if not isinstance(value, dict):
            raise ValueError("Body must be a JSON object.")
        return value

    def _handle(self, fn: Any) -> None:
        try:
            self._send(200, fn())
        except BadRequestBody as err:
            # Before the ValueError branch below, which this subclasses: the
            # OpenAI images API reports an unusable field as 422.
            self._send(422, {"error": {"message": str(err), "type": "invalid_request_error"}})
        except KeyError as err:
            self._send(404, {"error": str(err).strip('"')})
        except Leaking as err:
            # 503 and the words "start it again": the one action that works.
            self._send(503, {"error": str(err)})
        except NoRoom as err:
            self._send(503, {"error": str(err)})
        except CardLost as err:
            # 503 like Busy: the server is up, the card is not. A 500 here
            # reads as "that request was wrong", and it was not.
            self._send(503, {"error": str(err)})
        except Busy as err:
            # 503, not a hang. A client that waits for a lock reports "could
            # not reach the server" when it gives up, which is a lie about a
            # process that is working.
            self._send(503, {"error": str(err)})
        except (ValueError, NotImplementedError) as err:
            self._send(400, {"error": str(err)})
        except Exception as err:
            traceback.print_exc()
            self._send(500, {"error": f"{type(err).__name__}: {err}"})

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/health":
            self._handle(health)
        elif path == "/models":
            self._handle(lambda: {"models": [as_json(s) for s in REGISTRY.values()]})
        elif path == "/v1/models":
            self._handle(openai_models)
        else:
            self._send(
                404,
                {"error": f"No route {path}. Try /health, /models or /v1/models."},
            )

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/generate":
            self._handle(lambda: generate(self._read_json()))
        elif path == "/load":
            self._handle(self._load)
        elif path == "/unload":
            self._handle(self._unload)
        elif path == "/v1/images/generations":
            self._handle(lambda: openai_images(self._read_openai_json()))
        else:
            self._send(
                404,
                {
                    "error": f"No route {path}. Try /load, /generate, /unload "
                    "or /v1/images/generations."
                },
            )

    def _read_openai_json(self) -> dict:
        """The body, with a parse failure reported the way this route reports
        every other bad field."""
        try:
            return self._read_json()
        except ValueError as err:
            raise BadRequestBody(str(err)) from None

    def _load(self) -> dict:
        body = self._read_json()
        spec = spec_for(str(body.get("model") or "").strip())
        with held(f"loading {spec.id}"):
            if not cuda_ready():
                raise RuntimeError(
                    "No usable CUDA device. "
                    + (TORCH_IMPORT_ERROR or "torch reports no GPU; check the venv's wheel.")
                )
            if RESIDENT.model_id == spec.id:
                return {
                    "resident": spec.id,
                    "seconds": 0.0,
                    "freeMbBefore": free_mb(),
                    "freeMbAfter": free_mb(),
                    "alreadyResident": True,
                    "offload": offload_wanted(spec),
                }
            return RESIDENT.load(spec, offload_wanted(spec))

    def _unload(self) -> dict:
        with held('unloading'):
            was = RESIDENT.model_id
            before = free_mb()
            after = RESIDENT.unload()
            freed = None if before is None or after is None else after - before
            return {"resident": None, "unloaded": was, "freedMb": freed, "freeMb": after}


def exit_with_parent() -> None:
    """Dies when whoever started this dies.

    The launcher holds our stdin open and writes nothing to it, so a read
    returns only when that pipe closes — which happens however the launcher
    went, including a kill no signal handler could have caught. Without this,
    a hard-killed launcher leaves **23.4 GB** of interpreter running: on
    Windows `.venv/Scripts/python.exe` is a redirector stub, so the process
    holding the model is a *grandchild* and the launcher's own `kill` never
    reaches it.

    `os._exit` rather than a clean shutdown: there is nobody left to answer,
    and the only thing that matters is that the memory goes back.
    """

    def watch() -> None:
        try:
            sys.stdin.read()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="exit-with-parent").start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("GEN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GEN_PORT", "8730")))
    parser.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="shut down when stdin closes, i.e. when the launcher goes. "
        "Not the default: run by hand in a terminal, stdin is the keyboard.",
    )
    args = parser.parse_args()
    if args.exit_with_parent:
        exit_with_parent()

    state = health()
    print(f"docreel genserver on http://{args.host}:{args.port}")
    print(f"  torch   {state['torch']['version']} (cuda {state['torch']['cuda']})")
    print(f"  device  {state['device']['name']} capability {state['device']['capability']}")
    print(f"  models  {', '.join(state['models'])}")
    if TORCH_IMPORT_ERROR is not None:
        print(f"  ! torch did not import: {TORCH_IMPORT_ERROR}")
    arch = state["torch"]["archList"]
    cap = state["device"]["capability"]
    if arch and cap is not None and f"sm_{cap.replace('.', '')}" not in arch:
        # The exact failure the GPU would otherwise report 20 minutes into a
        # run. Said here, at startup, in the words the error uses.
        print(
            f"  ! this wheel was built for {', '.join(arch)} and this card is sm_{cap.replace('.', '')}:"
            " generation will fail with"
            ' "no kernel image is available for execution on the device".'
        )
    sys.stdout.flush()

    print(f"  images  POST http://{args.host}:{args.port}/v1/images/generations")
    if IDLE_SECONDS > 0:
        print(f"  idle    unloading after {IDLE_SECONDS:g}s")
    if not unload_ollama_wanted():
        print("  ollama  left alone (GEN_UNLOAD_OLLAMA=0)")
    sys.stdout.flush()

    start_idle_worker()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop_idle_worker()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
