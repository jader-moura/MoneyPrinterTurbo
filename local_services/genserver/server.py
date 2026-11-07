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
import gc
import io
import json
import os
import random
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from registry import REGISTRY, ModelSpec, as_json, parse_size, spec_for  # noqa: E402

# Importing torch must not take the server down: `/health` reporting a broken
# install is the whole point of `doctor` asking this process rather than
# guessing. The error is carried and reported, not raised at startup.
TORCH_IMPORT_ERROR: Optional[str] = None
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
IDLE_SECONDS = float(os.environ.get("GEN_IDLE_SECONDS", "0"))
IDLE_CHECK_SECONDS = float(os.environ.get("GEN_IDLE_CHECK_SECONDS", "10"))

# What `/v1/images/generations` loads when a caller names no model. OpenAI's
# API makes `model` optional in practice and MoneyPrinterTurbo always sends
# one, but a default keeps curl honest.
DEFAULT_MODEL = os.environ.get("GEN_DEFAULT_MODEL", "zimage-turbo")

# OpenAI's images API caps `n`; so does this, because each image is a full
# denoise on a card that holds one model.
MAX_IMAGES_PER_REQUEST = 4


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
        self.loaded_at: float = 0.0
        # Readable without the lock, so /health answers during a generation.
        self.generating: Optional[str] = None

    def unload(self) -> float:
        """Drops whatever is resident and returns the free VRAM in MB after."""
        self.pipe = None
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
        return free_mb()

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
        self.model_id = spec.id
        self.loaded_at = time.time()
        return {
            "resident": spec.id,
            "seconds": round(time.monotonic() - started, 2),
            "freeMbBefore": before,
            "freeMbAfter": free_mb(),
            "offload": offload,
        }


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
        "ok": TORCH_IMPORT_ERROR is None,
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


def start_idle_worker() -> None:
    global IDLE_THREAD
    if IDLE_SECONDS <= 0:
        return
    STOP_IDLE.clear()
    IDLE_THREAD = threading.Thread(target=idle_worker, name="idle-unload", daemon=True)
    IDLE_THREAD.start()


def stop_idle_worker() -> None:
    STOP_IDLE.set()
    if IDLE_THREAD is not None:
        IDLE_THREAD.join(timeout=5)


def generate(body: dict) -> dict:
    model_id = str(body.get("model") or "").strip()
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

    # A generation waits its turn rather than being refused: the Node side
    # queues at one in flight, so a second arrival is a caller that meant it.
    global LAST_USED
    with RESIDENT.lock:
        RESIDENT.generating = spec.id
        try:
            # Before every generation, not just before the load: Ollama can
            # come back between two images, and the transformer needs the whole
            # card either way.
            unloaded_ollama = unload_ollama()
            loaded = None
            if RESIDENT.model_id != spec.id:
                loaded = RESIDENT.load(spec, offload_wanted(spec))

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
            image = RESIDENT.pipe(**kwargs).images[0]
            seconds = round(time.monotonic() - started, 2)
            peak = int(torch.cuda.max_memory_allocated() // MB)

            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        finally:
            RESIDENT.generating = None
            LAST_USED = time.monotonic()

    return {
        "model": spec.id,
        "repo": spec.repo,
        "license": spec.license,
        "prompt": prompt,
        "negativePrompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        "seconds": seconds,
        "peakAllocatedMb": peak,
        "freeMbBefore": before,
        "freeMbAfter": free_mb(),
        "offload": offload_wanted(spec),
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

    data = []
    for index in range(count):
        # A seed per image, so `n > 1` is n different pictures rather than n
        # copies. An explicit seed still pins the first one.
        seed = body.get("seed")
        if seed is not None:
            seed = int(seed) + index
        result = generate(
            {
                "model": spec.id,
                "prompt": prompt,
                "size": f"{width}x{height}",
                "seed": seed,
            }
        )
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
