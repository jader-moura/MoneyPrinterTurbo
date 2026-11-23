# genserver — Z-Image-Turbo for `openai_image`

A local image server for `video_source = "openai_image"`. It generates one picture per
script keyword on this machine's GPU, so there is nothing to pay per image and nothing
leaves the computer.

It speaks the OpenAI images API that `openai_image` already calls:

```
POST /v1/images/generations   {"model", "prompt", "size", "n"}  ->  data[].b64_json
GET  /v1/models
```

and keeps its own five native routes, which are the ones a non-OpenAI client uses:

```
GET  /health     resident model, VRAM, torch version, arch list, capability
GET  /models     the registry, as declared
POST /load       {"model": id}
POST /generate   {"model", "prompt", "size"|"width"/"height", "seed", ...}  ->  pngBase64
POST /unload
```

Both generate routes also take a **starting picture**:

```
"image":    base64 of a PNG/JPEG (a data: URL is fine), up to 16 MB
"strength": 0 < s <= 1, default 0.75 — how far the result may move from it
```

`image` on `/v1/images/generations` is an extension, not OpenAI's shape: OpenAI puts this
behind a multipart `/v1/images/edits`, and carrying the two fields here means a client that
already speaks this route — MoneyPrinterTurbo's `openai_image` source — needs no second
client, no multipart encoder and no second base url. A client that never sends them sees
no difference whatsoever.

The picture is never cropped. All of it is scaled to fit inside the size being painted, and
what is left over at the sides is filled with a blurred copy of the same picture (black bars
would be handed to the model to denoise, and they come back as black bars). With
`follow: "picture"` the editing model is given the whole picture in its own proportions
instead. Either way it is re-encoded without EXIF, and
refused with a 400 if it is not a picture. `strength` is how much noise is added before
denoising: at `1.0` the picture is ignored, at `0.75` the composition survives and
everything else is repainted, at `0.3` you get the same picture again in a slightly
different hand. Because img2img joins the schedule partway down, the step count is scaled
up so the distilled model still gets its nine — `strength: 0.75` runs 12 steps — and an
explicit `steps` is left alone.

Which entries can do it is declared, not assumed: `img2img` in `registry.py` names the
`diffusers` class, and an entry with none answers 400 (or 422 on the `/v1` route) instead
of ignoring the picture. Z-Image-Turbo's is `ZImageImg2ImgPipeline`, over the same weights,
built with `from_pipe` — so starting from a picture costs no second load and no second
12 GB on a card that holds exactly one model.

The `/v1` pair is a translation and nothing more. One model is resident at a time, one
lock guards the card, and both route families go through the same `generate`.

The model is [Z-Image-Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) (Apache-2.0,
6B, distilled): **9 steps at guidance 0**. Because it is distilled, classifier-free
guidance is off, so a negative prompt would be ignored — put style in the prompt instead,
with `openai_image_prompt_template` (see below).

`docs/local-image-server/` is the other local image server in this repo, running SDXL
Lightning. It is smaller, faster and needs about 6.6 GB of weights; this one is slower
but needs no `negative_prompt` tuning and produces a different look. Either works with
`openai_image`; pick one and point `openai_image_base_url` at it.

## Setup

torch is not in `requirements.txt`, because the right wheel depends on the card. Install
it first, from PyTorch's own index — PyPI will not serve a Blackwell-capable build:

```bash
cd /path/to/MoneyPrinterTurbo
python -m venv local_services/genserver/.venv
local_services/genserver/.venv/Scripts/python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
local_services/genserver/.venv/Scripts/python -m pip install -r local_services/genserver/requirements.txt
```

Use `cu128` or later for Blackwell (RTX 50-series, `sm_120`); `cu121` or `cu124` for
older cards. On Linux the interpreter is `.venv/bin/python`.

A wheel built for the wrong architecture is the failure worth knowing about: it imports,
`torch.cuda.is_available()` returns `True`, and then every kernel dies with *"no kernel
image is available for execution on the device"*. The server checks for exactly that at
startup and says so before you wait on a generation.

## Running it

```bash
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  local_services/genserver/.venv/Scripts/python local_services/genserver/server.py
```

`scripts/start-local.sh` does this for you, along with the other two local services.

It listens on `http://127.0.0.1:8730`. `PYTORCH_CUDA_ALLOC_CONF` matters because a model
that is offloaded and reloaded every call leaves the allocator holding blocks of the
wrong shape, and the next generation dies asking for 76 MiB with gigabytes free.

### On Windows, that setting does nothing

Measured here — torch prints it at the first generation:

```
UserWarning: expandable_segments not supported on this platform
```

So the allocator mitigation this server leans on is **unavailable on Windows**, and
fragmentation accumulates across generations instead of being absorbed. Observed: a run
of eight images died after the sixth, taking the process with it (exit 139) and resetting
the HTTP connection mid-response; the caller reported it as
`unconfirmed image request ... ConnectionResetError(10054)`. VRAM peaked at 15069 MiB of
16283 on that run, which is the headroom the fragmentation ate.

What helps on Windows, cheapest first:

1. **Ask for a smaller size.** `openai_image_size = "1280x720"` is 0.92 MP against
   1536x864's 1.33 MP, so roughly 30% less activation memory per image, and faster.
2. **Fewer images per video** — longer `video_clip_duration`, or
   `match_materials_to_script = false`, which asks for 5 keywords instead of 8.
3. **Restart the server between videos.** A fresh process starts from an
   unfragmented allocator. Restart it, rather than setting `GEN_IDLE_SECONDS`
   to have a running one drop the model: reloading in-process segfaults, as
   "The reload segfault" below explains.

Linux does support `expandable_segments`, so none of this applies there.

### Model download

The first generation downloads the weights to the Hugging Face cache
(`~/.cache/huggingface`, or `HF_HOME` if set) — **about 33 GB**, because the repo ships
fp32: 24.6 GB of transformer and 8.1 GB of text encoder. They load as bfloat16, which is
12 GB and 4 GB in memory. Nothing is written into this folder, and the weights are not in
the repository.

### Environment

| Variable | Default | What it does |
| --- | --- | --- |
| `GEN_HOST` / `GEN_PORT` | `127.0.0.1` / `8730` | Where to listen. |
| `HF_HOME` | Hugging Face default | Moves the 33 GB of weights off the system drive. |
| `PYTORCH_CUDA_ALLOC_CONF` | unset | Set it to `expandable_segments:True`; see above. |
| `GEN_OFFLOAD` | registry's value | Force CPU offload on for an entry that declares it off. |
| `GEN_UNLOAD_OLLAMA` | `1` | Ask Ollama to drop its model before each generation. |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Where to ask. |
| `OLLAMA_UNLOAD_TIMEOUT` | `30` | Seconds to wait for Ollama's VRAM to come back. |
| `GEN_IDLE_SECONDS` | `0` (off) | Unload the model after this long idle. |
| `GEN_IDLE_CHECK_SECONDS` | `10` | How often the idle worker looks, and how often the commit charge is checked. |
| `GEN_MAX_COMMIT_GB` | `44` | Bigger than working costs past here. On a crowded machine it drops the model, and if that does not help it refuses further work. |
| `GEN_MIN_FREE_COMMIT_GB` | `0.5` | Refuse to generate when the machine has less commit charge left. Nearly off on purpose: the commit limit is elastic, so this is only the last half gigabyte before an allocation really fails. |
| `GEN_LOAD_COMMIT_GB` | `0` | Extra commit charge demanded before a load. Off by default, since the system grows its pagefile; set it where the pagefile is fixed. |
| `GEN_CRITICAL_FREE_COMMIT_GB` | `2` | When the machine counts as out of room, for the runaway backstop. |
| `GEN_DEFAULT_MODEL` | `zimage-turbo` | What `/v1/images/generations` loads with no `model`. |

## config.toml

```toml
[app]
video_source = "openai_image"
openai_image_base_url = "http://127.0.0.1:8730/v1"
openai_image_api_keys = []          # no auth; no Authorization header is sent
openai_image_model = "zimage-turbo"
openai_image_size = ""              # leave empty — see below
openai_image_prompt_template = "{term}, candid cinematic photograph, natural light, documentary style, 35mm film, highly detailed, sharp focus"
```

**Leave `openai_image_size` empty.** MoneyPrinterTurbo then sends its own per-aspect
default — `1536x1024` for landscape, `1024x1536` for portrait, `1024x1024` for square —
and this server snaps each to the nearest size the model actually declares:

| MoneyPrinterTurbo asks | painted | |
| --- | --- | --- |
| `1536x1024` (landscape) | `1536x864` | 16:9 |
| `1024x1536` (portrait) | `864x1536` | 9:16 |
| `1024x1024` (square) | `1024x1024` | 1:1 |

A raw video resolution works the same way: `1920x1080` → `1536x864`, `1080x1920` →
`864x1536`. Setting `openai_image_size` to a fixed value would pin *every* aspect to it,
which is why it is better left alone unless all your videos are one shape.

The snapping is by aspect ratio, to the sizes `registry.py` declares. It keeps the pixel
count near what the model was trained for; painting 2 megapixels instead would be slower
with nothing to show for it. A `size` that is not `WIDTHxHEIGHT` with positive numbers is
a **422**. The native `/generate` does not snap — it is a direct request for one size and
returns 400 when the model cannot do it.

## The GPU, and what competes for it

Measured on this machine: **RTX 5060 Ti, 16283 MiB usable**, torch 2.11.0+cu128, sm_120.

| | |
| --- | --- |
| First request (includes loading the model) | **210 s** |
| Warm `1536x864`, allocator fresh | **26–33 s** (2.5–3.7 s/step) |
| Warm `1536x864`, allocator fragmented | **60–112 s** |
| Peak VRAM during a generation | **12.2 GiB** (`peakAllocatedMb` 12461) |
| VRAM held between generations | **~1.5 GiB** |
| **Host RAM held while loaded** | **~16 GiB** |

The spread on the warm row is the Windows allocator problem below, not noise: the first
few images after a load take under 35 s, and the same request later in the same run takes
two to three times that as fragmentation builds. On Linux, where `expandable_segments`
works, the fast number should hold.

The host-RAM row is the one that surprises people, and it is a direct consequence
of the thing that makes the model fit on the card at all. `enable_model_cpu_offload`
keeps the weights in *system* memory and streams each component to the GPU as
it is needed, so the 12 GB transformer and 4 GB text encoder sit in RAM for as
long as the model is resident. Measured: on a 32 GB machine, genserver resident
plus Ollama plus the app itself was enough to exhaust system memory and get the
server killed by the OS. VRAM was never the limit — RAM was.

### "HTTP 500: MemoryError" on every image

This is the failure the host-RAM number above predicts, and it is worth
recognising on sight, because nothing in the message mentions memory pressure
or the GPU:

```
material.py: openai image request failed, retrying: detail=HTTP 500: MemoryError:
material.py: openai image generation failed: term='...', detail=HTTP 500: MemoryError:
task.py:     task failed, stage: materials, error: failed to download video materials
```

Every term fails, each after three attempts, and the task dies at the materials
stage having generated nothing. In genserver's own log the cause is one line
further down than the `MemoryError`:

```
OSError: The paging file is too small for this operation to complete. (os error 1455)
```

That is Windows `ERROR_COMMITMENT_LIMIT`, raised inside `from_pretrained` while
safetensors reserves memory for a shard. diffusers catches it, tries to read the
file to produce a better message, and *that* read is what raises the
`MemoryError` you see. So the reported exception is a side effect; the real
error is the one above it.

It means the system commit charge could not grow by the ~16 GB this model needs.
Diagnose it by commit charge, not by free RAM — the number that matters is the
commit limit, which is physical memory plus the pagefile:

```powershell
$c = Get-Counter '\Memory\Committed Bytes','\Memory\Commit Limit'
$c.CounterSamples | ForEach-Object { "{0}: {1:N1} GB" -f $_.Path, ($_.CookedValue/1GB) }
```

Under about 20 GB of headroom, expect the load to fail. The causes, in the order
worth checking:

1. **A second genserver.** Measured here: two were running, one of them on a
   system interpreter whose torch was built for another GPU architecture. It
   lost the race for port 8730 but kept loading weights anyway and held
   **37.6 GB of commit charge** with a 0.3 GB working set — invisible in Task
   Manager's memory column, fatal to the next load. Find them with
   `netstat -ano | grep 8730` and by listing python processes with their command
   lines; `scripts/start-local.sh` now refuses to start one when the port is
   already owned, and kills its own if it fails to come up.
2. **Ollama still resident**, holding ~10 GB. Both `app/services/task.py` and
   this server ask it to unload, so this should be rare; `GEN_UNLOAD_OLLAMA=0`
   disables the second of those.
3. **A small pagefile.** A system-managed pagefile shrinks when idle, so a run
   can meet a smaller commit limit than the one measured an hour earlier. A
   fixed pagefile of 32 GB or more removes that variable. This is a Windows
   setting, not a project one: System Properties -> Advanced -> Performance ->
   Advanced -> Virtual memory.

### 61 GB of commit charge, and every allocation failing

Worth recognising on sight, because none of the errors mention memory. On
2026-11-13 a redraw failed like this, three times in a row, for two hours:

```
HTTP 500: OutOfMemoryError: CUDA out of memory. Tried to allocate 40.00 MiB.
          GPU 0 has a total capacity of 15.90 GiB of which 5.76 GiB is free
HTTP 500: AcceleratorError: CUDA error: unknown error
numpy._core._exceptions._ArrayMemoryError: Unable to allocate 15.2 MiB
```

A 40 MB allocation failing with 5.76 GB of VRAM free is not a GPU problem, and
the numpy line is the giveaway: that is host memory. The process looked
harmless in Task Manager — a working set of 0.03 GB — while holding **61.25 GB
of commit charge**, on a machine whose commit limit is 79.92 GB with 78.19 GB
already used. Past that point every allocation fails whatever its size, the
CUDA context is lost, and the resident model cannot be given back, because
commit charge is only returned when a process ends.

So the number to look at is commit charge, not working set:

```
GET /health   ->  "commit": {"ownMb": 14230, "ceilingMb": 28672,
                             "machineUsedMb": 41203, "machineLimitMb": 81840}
```

and every `/generate` answer carries `commitMb`, so growth shows up in
`storage/logs/genserver.log` without anyone having to go looking.

What the server does about it now:

| it sees | it answers |
| --- | --- |
| the machine has less than `GEN_MIN_FREE_COMMIT_GB` left (plus `GEN_LOAD_COMMIT_GB` when a load is needed) | `503`, naming how much is left and how much was wanted. Both default low: a machine simply running close to its elastic commit limit is normal, and refusing there blocked every redraw after the first. |
| its own commit charge past `GEN_MAX_COMMIT_GB`, machine still roomy | says so once and carries on — being large is not the harm |
| past the ceiling **and** the machine out of room | drops the resident model, and carries on if that gave it back |
| still past it after dropping the model | `503` on everything, and "stop this server and start it again" — `/health` stops saying `ok` |
| a CUDA error meaning the context is gone | the same, naming the error |

An ordinary out-of-memory is deliberately none of these: refusing all work for
ever because one picture did not fit would be worse than the error it avoids.

#### What it turned out to be

Not a leak that built up over an afternoon. One redraw, on a freshly started
server:

```
[genserver] img2img pipeline built: commit charge 23 GB -> 52 GB (+29786 MB)
```

`from_pipe` ends in `new_pipeline.to(dtype=dtype)`, and with no dtype passed
that is `torch.float32`:

```python
torch_dtype = kwargs.pop("torch_dtype", None)
dtype = _resolve_dtype(kwargs.pop("dtype", None), torch_dtype) or torch.float32
```

The resident pipeline is bfloat16, so the img2img pipeline was casting every
weight to float32 - twice the size, every parameter a new tensor, while the
originals stayed referenced by the resident pipeline. Hence +29.8 GB for one
picture, a machine at 79 of 79 GB committed, and a 40 MB CUDA allocation
reporting "out of memory" with 5.76 GB of VRAM free. Had it ever succeeded it
would also have been generating in float32.

The fix is naming the dtype it was loaded with, so the cast is a no-op. The
measurement stays, because that is the line that found it:

```
[genserver] img2img pipeline built: commit charge 23 GB -> 23 GB (+0 MB)
POST /v1/images/generations "POST /v1/images/generations HTTP/1.1" 200
```

#### And then the ceiling itself was wrong

The redraw after that one was refused — by this guard, not by the machine:

```
[genserver] this process holds 34 GB of commit charge, past the 28 GB ceiling,
            and dropping the model gave back only 3 GB
```

So here is what the thing actually costs, measured rather than guessed:

| | commit charge |
| --- | --- |
| model resident, img2img pipeline built | 23 GB |
| after one redraw | 34 GB |
| after dropping the model again | 31 GB |

The 11 GB is CPU offload materialising its host-side copy of the transformer
the first time a generation touches it. That is how the design works, it does
not come back while the process lives, and 28 GB was below it — so the guard
refused the request right after the first redraw that had ever succeeded.

The ceiling is 44 GB now, above what working costs and below the 52 GB
runaway. More to the point, being large is no longer by itself a reason to
refuse: it takes the ceiling **and** a machine with no room left, because the
harm was always the machine running out, never the size of this process.

Worth knowing on a 32 GB machine: one genserver drawing redraws sits at ~34 GB
of commit charge, so the commit limit — RAM plus the pagefile, which Windows
resizes on its own — is what decides whether anything else can run. Here it
moved from 79.9 GB to 66.6 GB between two restarts, and with everything else
running the machine was left with 3 GB.

The watcher runs on the idle worker's tick, which is why that thread now
starts even with `GEN_IDLE_SECONDS=0` — before this there was no background
thread at all, and nothing was watching the process grow.

### The reload segfault, and why the idle unload is off

Do not reach for `GEN_IDLE_SECONDS` to get that 16 GB back without reading
this. **Loading the pipeline a second time in one process crashes the
process.** Reproduced on demand, with a short idle timer:

```
POST /v1/images/generations  200      generate
idle: unloaded zimage-turbo           the unload itself is fine
GET  /health                 200      still answering
Loading pipeline components... 40%    the next request triggers a reload
<Segmentation fault, exit 139>
```

The unload is not what kills it and neither is the generation; it is the second
`from_pretrained` in the same interpreter. Detaching accelerate's offload hooks
before dropping the pipeline does not help — this is torch/diffusers teardown
state on Windows with CPU offload, below where this server can reach.

What the caller sees is not a crash report. The app reports `HTTP 500` or
`ConnectionResetError(10054)` for every term and fails at the materials stage,
which looks like an image problem and is not one.

`load()` takes the same path when it swaps one model for another, so a registry
with two models in rotation hits this too.

So `GEN_IDLE_SECONDS` defaults to `0` and `scripts/start-local.sh` no longer
overrides it. A video generates its images consecutively, so the model is
loaded once and the reload path is never taken. Measured that way: **8 images
back to back, 8/8, server healthy afterwards**, 44–149 s each.

The price is ~16 GB of host RAM held between videos. On 32 GB that is
affordable once there is only one genserver — commit charge measured 28 GB of a
60 GB limit with nothing loaded. To get the RAM back, stop the server and start
a fresh one rather than asking a running one to unload:

```bash
taskkill //F //PID <pid>     # or Ctrl-C
bash scripts/start-local.sh  # starts a clean one
```

A supervised restart — the idle timer exits the process, a wrapper starts
another — would give back the RAM without ever reloading in-process. That is
the fix this server does not yet have.

Two things in the local pipeline want the card, and on 16 GB they do not fit together:

| Service | GPU | Roughly |
| --- | --- | --- |
| Ollama (script, keywords) | yes | 10 GB for a 14B model at 4k context |
| **genserver (this)** | yes | **12.2 GiB peak, ~1.5 GiB idle** |
| Chatterbox (narration) | yes | 3.2 GiB loaded, 4.4 GiB peak; its container runs only while a narration records |
| Whisper (subtitles) | no | CPU; see below |

Whisper is on that list as a *non*-consumer, which is worth saying because the
obvious configuration does not work. Subtitles run through faster-whisper,
which is built on CTranslate2 rather than torch, and its CUDA build wants
`cublas64_12.dll` and cuDNN on the PATH. Those are not among this project's
dependencies, so `[whisper] device = "cuda"` fails the task with:

```
RuntimeError: Library cublas64_12.dll is not found or cannot be loaded
```

`config.local.example.toml` therefore uses `device = "cpu"` with the `small`
model, which transcribes a one-minute narration in well under a minute and
leaves the card entirely to the image model.

So the pipeline has to run Ollama and genserver one after another, and the repo
does that in two places:

- `app/services/task.py` asks Ollama to unload (`keep_alive: 0`) once the script and the
  keywords are written, before the pictures start. Only for Ollama — a Claude run has
  nothing resident to drop.
- this server asks Ollama to unload again before **every** generation, because Ollama can
  come back between two images. `GEN_UNLOAD_OLLAMA=0` turns that off.

Between generations CPU offload returns the weights to host memory by itself, so only
about 1.5 GiB of VRAM is held and Whisper has room without anything further.

Host RAM is the one that needs managing, and the honest answer today is that
this server has no safe way to hand it back while running. `GEN_IDLE_SECONDS`
drops the pipeline and does return the ~16 GiB, but the next generation has to
load it again and that is the path that segfaults, so it defaults to 0 and
`scripts/start-local.sh` leaves it there. Stop and restart the process to
reclaim the memory. See "The reload segfault" above.

## Tests

From the repo root, no GPU needed:

```bash
python -m pytest test/services/test_genserver.py
```

49 tests. They run the real request handler on a real socket with a stand-in pipeline and
a fake Ollama, and cover the `/v1` contract (sizes, 422s, `b64_json`, `n`), the Ollama
unload, the idle unload, and that the five native routes still behave as they did.

## Where this came from

Copied from `scripts/genserver/` in the `video-generator` project, which is where it is
maintained. The two differences here:

1. `server.py` gained the `/v1` routes, the Ollama unload and the idle unload.
2. `registry.py` gained the transposed portrait sizes (`864x1536`, `720x1280`,
   `576x1024`). MoneyPrinterTurbo renders 9:16 as often as 16:9, and without a portrait
   entry the nearest declared ratio to 9:16 was 1:1 — a square picture in a tall frame.
   Z-Image only requires multiples of 16, so the transpose costs nothing.

Adding another model is an entry in `registry.py`, not a change here; `emits="video"` is
already carried through for a video model, and the one-resident-at-a-time rule is the
invariant this server exists to enforce.
