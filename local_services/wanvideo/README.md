# wanvideo — video scenes with Wan 2.2

Turns scenes into short video clips on this machine's GPU, for the timeline
editor's **Video scene** choice. Two ways, both the 14B model with a 4-step
distillation:

| In the editor | Model | What it does |
| --- | --- | --- |
| Animate this picture | Wan 2.2 I2V A14B | Starts on the scene's picture and moves it. |
| Make a video from the prompt | Wan 2.2 T2V A14B | Draws a clip from the scene's prompt alone. |

It is not a server. `app/services/scene_video.py` starts `worker.py` for a few
clips at a time and reads its progress; the process ending is what hands the
graphics card back to the image server.

## Setup

It runs on the image server's interpreter (the one with a torch built for the
card — see `../genserver/README.md`) and needs three more packages. Install
them beside the worker, which leaves that interpreter as the image server
expects it:

```bash
<genserver python> -m pip install --no-deps --target local_services/wanvideo/.deps \
    -r local_services/wanvideo/requirements.txt
```

The app looks for that interpreter where `scripts/start-local.sh` does. To name
it outright, in `config.toml`:

```toml
[app]
wan_video_python = "C:\\path\\to\\genserver\\.venv\\Scripts\\python.exe"
```

### Weights

Fetched from Hugging Face into its cache (`HF_HOME`) the first time each kind
of clip is made — about 50 GB in all:

| Repo | What | Size |
| --- | --- | --- |
| `Wan-AI/Wan2.2-I2V-A14B-Diffusers` | text encoder, VAE, configs (not its transformers) | 12 GB |
| `jayn7/WAN2.2-I2V_A14B-DISTILL-LIGHTX2V-4STEP-GGUF` | image-to-video, two Q5_K_M files | 21 GB |
| `QuantStack/Wan2.2-T2V-A14B-GGUF` | text-to-video, two Q5_K_M files | 21 GB |
| `lightx2v/Wan2.2-Lightning` | the 4-step LoRAs for text-to-video | 1.2 GB |
| `Wan-AI/Wan2.2-T2V-A14B-Diffusers` | configs only | — |

## What it costs

Measured on an RTX 5060 Ti (16 GB) with 32 GB of RAM, 832x480:

| | |
| --- | --- |
| One 5-second clip (81 frames) | about 6 minutes |
| Loading, per group of up to 4 clips | 2–3 minutes |
| Peak VRAM | about 10 GB |
| Host memory | about 10 GB |

Clips are 16 fps and at most 81 frames. The app slows a clip to cover a longer
scene (up to 2x, so scenes up to 10 s) and interpolates it to 30 fps.

## Why one model at a time

Wan 2.2 A14B is two transformers, one for the noisy half of the steps and one
for the clean half, plus an 11 GB text encoder. Loaded together, as the stock
`diffusers` pipeline does, the worker held 39 GB of commit charge on a 32 GB
machine and stalled. So it works by model, not by clip: every prompt through
the text encoder, then the first half of every clip, then the second half,
then decoding. Nothing is finished until the last phase, which is why a process is given only
a few clips.

`WAN_GROUP_BLOCKS` (default 20) is how many of a transformer's 40 blocks are on
the card at once. Lower it if a clip overflows VRAM: the driver then spills to
shared memory instead of failing, and a clip takes 20 minutes instead of six.

## Running it by hand

```bash
<genserver python> local_services/wanvideo/worker.py job.json
```

`worker.py`'s docstring has the job file and the progress lines it prints.
The app's own runs append the worker's stderr to `storage/logs/wanvideo.log`.
