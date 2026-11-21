"""Draws a few video clips with Wan 2.2 A14B, then exits.

    python local_services/wanvideo/worker.py <job.json> [--parent-pid <pid>]

Not a server. The app starts this process when the user asks for video scenes,
reads its progress from stdout, and the process ending is what gives the card
and the host memory back to the image server.

Both kinds of clip run the 4-step distilled recipe: image-to-video animates a
still, text-to-video draws from a prompt. Measured on a 16 GB RTX 5060 Ti at
832x480, 81 frames: about six minutes a clip, 10 GB of VRAM at the peak.

One model in memory at a time
-----------------------------
Wan 2.2 A14B is two 14B transformers: one for the noisy first half of the
steps, one for the clean second half. As Q5 GGUF each is 10 GB of host memory,
and the text encoder is another 11 GB. Loaded together, the way the stock
pipeline does it, this process held 39 GB of commit charge on a 32 GB machine
and stopped making progress.

So the work is ordered by model instead of by clip:

    1. the text encoder alone: every prompt, then it is dropped
    2. the high-noise transformer: the first half of every clip, then dropped
    3. the low-noise transformer: the second half of every clip, then dropped
    4. the VAE alone: decode and write every clip

which never holds more than one of the three. The price is that no clip is
finished until step 4, which is why the app sends only a few clips per process.

The job file:

    {"mode": "image" | "text",
     "ffmpeg": "<path to ffmpeg>",
     "clips": [{"id", "prompt", "width", "height", "frames", "seed", "out",
                "image": "<still to animate; image mode only>"}]}

Progress is one JSON object per line on stdout, each with an "event":

    loading      {"what"}                 fetching or loading weights
    clip         {"id"}                   work on this clip has begun or resumed
    step         {"id", "n", "of"}        a denoising step finished
    clip_done    {"id", "file"}
    clip_failed  {"id", "error"}          the others carry on
    fatal        {"error"}                nothing more will be drawn
    done         {}

Everything else the libraries print goes to stderr.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
# gguf, peft and ftfy, when they were installed beside this file rather than
# into the interpreter (see README.md).
DEPS = os.path.join(HERE, ".deps")
if os.path.isdir(DEPS):
    sys.path.insert(0, DEPS)

# Give freed host memory back to Windows at once; must be set before torch
# loads. Its Windows build allocates through mimalloc, which otherwise keeps
# what is freed: the dropped text encoder would stay charged to this process
# under the transformers. The image server sets the same, for the same reason.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

# Text encoder, tokenizer and VAE are the same files for both kinds of clip.
SHARED_REPO = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
SHARED_PARTS = ["tokenizer/*", "text_encoder/*", "vae/*", "scheduler/*",
                "transformer/config.json", "transformer_2/config.json", "model_index.json"]

IMAGE_REPO = "jayn7/WAN2.2-I2V_A14B-DISTILL-LIGHTX2V-4STEP-GGUF"
IMAGE_HIGH = "high_noise_260412/wan2.2_i2v_A14b_high_noise_lightx2v_4step_720p_260412-Q5_K_M.gguf"
IMAGE_LOW = "low_noise_260412/wan2.2_i2v_A14b_low_noise_lightx2v_4step_720p_260412-Q5_K_M.gguf"

TEXT_BASE_REPO = "Wan-AI/Wan2.2-T2V-A14B-Diffusers"
TEXT_BASE_PARTS = ["scheduler/*", "transformer/config.json", "transformer_2/config.json",
                   "model_index.json"]
TEXT_REPO = "QuantStack/Wan2.2-T2V-A14B-GGUF"
TEXT_HIGH = "HighNoise/Wan2.2-T2V-A14B-HighNoise-Q5_K_M.gguf"
TEXT_LOW = "LowNoise/Wan2.2-T2V-A14B-LowNoise-Q5_K_M.gguf"
# The text-to-video distillation ships as a pair of LoRAs over the base model.
LORA_REPO = "lightx2v/Wan2.2-Lightning"
LORA_DIR = "Wan2.2-T2V-A14B-4steps-lora-rank64-Seko-V2.0"

STEPS = 4
FPS = 16
FLOW_SHIFT = 5.0
# The timestep at which the high-noise transformer hands over to the low-noise
# one, as each model's own pipeline config sets it.
BOUNDARY = {"image": 900.0, "text": 875.0}
# Of a transformer's 40 blocks, how many sit on the card at once. The rest wait
# in host memory. Without this an 81-frame clip overflows 16 GB, the driver
# spills to shared memory, and one clip takes 23 minutes instead of six.
GROUP_BLOCKS = int(os.environ.get("WAN_GROUP_BLOCKS", "20"))


def say(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def exit_with(parent: int) -> None:
    """Dies when the app that started this dies.

    Without it a killed app leaves 10 GB of model running with nobody reading
    its output. Watches the app's process rather than waiting on a closed
    stdin, which is how the image server does it: on Windows a thread blocked
    reading a stdin pipe stalls `import torch` for good, so the watcher would
    have to start after every import this process will ever make.
    """

    def gone_windows() -> None:
        import ctypes

        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, parent)  # SYNCHRONIZE
        if handle:
            kernel.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)

    def gone_posix() -> None:
        import time

        while True:
            try:
                os.kill(parent, 0)
            except ProcessLookupError:
                return
            except OSError:
                pass
            time.sleep(2)

    def watch() -> None:
        try:
            gone_windows() if os.name == "nt" else gone_posix()
        finally:
            os._exit(0)

    threading.Thread(target=watch, daemon=True, name="exit-with-parent").start()


def folder(repo: str, parts: list[str]) -> str:
    """Part of a repo, from the cache when it is there, downloaded when not."""
    from huggingface_hub import snapshot_download

    def complete(path: str) -> bool:
        return all(
            os.path.exists(os.path.join(path, part.replace("/*", "")))
            for part in parts
        )

    try:
        path = snapshot_download(repo, allow_patterns=parts, local_files_only=True)
        if complete(path):
            return path
    except Exception:
        pass
    say("loading", what=f"downloading {repo}")
    return snapshot_download(repo, allow_patterns=parts)


def one_file(repo: str, name: str) -> str:
    from huggingface_hub import hf_hub_download

    try:
        return hf_hub_download(repo, name, local_files_only=True)
    except Exception:
        say("loading", what=f"downloading {name.rsplit('/', 1)[-1]}")
        return hf_hub_download(repo, name)


def release() -> None:
    import torch

    gc.collect()
    torch.cuda.empty_cache()


def encode_prompts(prompts: list[str], shared: str, device) -> dict:
    """Every prompt as embeddings, with the text encoder gone again afterwards."""
    import torch
    from diffusers import WanPipeline
    from transformers import T5TokenizerFast, UMT5EncoderModel

    say("loading", what="reading the prompts")
    encoder = UMT5EncoderModel.from_pretrained(
        os.path.join(shared, "text_encoder"), torch_dtype=torch.bfloat16)
    helper = WanPipeline(
        tokenizer=T5TokenizerFast.from_pretrained(os.path.join(shared, "tokenizer")),
        text_encoder=encoder, vae=None, scheduler=None, transformer=None)
    embeds = {}
    with torch.no_grad():
        encoder.to(device)
        for prompt in prompts:
            if prompt not in embeds:
                encoded, _ = helper.encode_prompt(
                    prompt, do_classifier_free_guidance=False,
                    device=device, max_sequence_length=512)
                embeds[prompt] = encoded.to("cpu", torch.bfloat16)
        encoder.to("cpu")
    del helper, encoder
    release()
    return embeds


def load_transformer(mode: str, half: str, base: str, device):
    """One of the two transformers, set up to stream its blocks to the card."""
    import torch
    from diffusers import GGUFQuantizationConfig, WanPipeline, WanTransformer3DModel

    high = half == "high"
    say("loading", what=f"loading the video model, part {1 if high else 2} of 2")
    if mode == "image":
        path = one_file(IMAGE_REPO, IMAGE_HIGH if high else IMAGE_LOW)
    else:
        path = one_file(TEXT_REPO, TEXT_HIGH if high else TEXT_LOW)
    model = WanTransformer3DModel.from_single_file(
        path, config=base, subfolder="transformer" if high else "transformer_2",
        torch_dtype=torch.bfloat16,
        quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
    )
    if mode == "text":
        # The LoRA goes on through a pipeline, which is what knows how to read
        # this file; the pipeline holds nothing else and is dropped at once.
        name = f"{'high' if high else 'low'}_noise_model.safetensors"
        lora = os.path.dirname(one_file(LORA_REPO, f"{LORA_DIR}/{name}"))
        shell = WanPipeline(tokenizer=None, text_encoder=None, vae=None, scheduler=None,
                            transformer=model)
        shell.load_lora_weights(lora, weight_name=name, adapter_name="fast")
        del shell
    model.enable_group_offload(
        onload_device=device, offload_device=torch.device("cpu"),
        offload_type="block_level", num_blocks_per_group=GROUP_BLOCKS,
    )
    return model


def write_mp4(frames, path: str, ffmpeg: str) -> None:
    """Frames (N, H, W, 3 uint8) to an H.264 file, whole or not at all."""
    height, width = frames.shape[1], frames.shape[2]
    part = path + ".part.mp4"
    done = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", part],
        input=frames.tobytes(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if done.returncode != 0:
        raise RuntimeError("ffmpeg: " + done.stderr.decode("utf-8", "replace")[-300:])
    os.replace(part, path)


def run(job: dict) -> None:
    import torch
    from diffusers import AutoencoderKLWan, UniPCMultistepScheduler, WanImageToVideoPipeline

    mode, clips, ffmpeg = job["mode"], job["clips"], job["ffmpeg"]
    if not torch.cuda.is_available():
        raise RuntimeError("torch reports no usable CUDA device in this interpreter")
    device = torch.device("cuda")
    shared = folder(SHARED_REPO, SHARED_PARTS)
    base = shared if mode == "image" else folder(TEXT_BASE_REPO, TEXT_BASE_PARTS)

    embeds = encode_prompts([clip["prompt"] for clip in clips], shared, device)

    vae = AutoencoderKLWan.from_pretrained(os.path.join(shared, "vae"), torch_dtype=torch.float32)
    # Tiled: a flat, small peak whatever the clip's length.
    vae.enable_tiling()
    vae.tile_sample_min_height = vae.tile_sample_min_width = 512
    vae.tile_sample_stride_height = vae.tile_sample_stride_width = 384
    vae.to(device)
    mean = torch.tensor(vae.config.latents_mean).view(1, vae.config.z_dim, 1, 1, 1).to(device)
    std = torch.tensor(vae.config.latents_std).view(1, vae.config.z_dim, 1, 1, 1).to(device)

    # Where each clip starts: noise, its own scheduler, and for an animated
    # still the encoded picture the model is conditioned on. The still is
    # prepared by the stock pipeline's own code, with no transformer attached.
    stills = WanImageToVideoPipeline(
        tokenizer=None, text_encoder=None, vae=vae, scheduler=None,
        transformer=None, transformer_2=None) if mode == "image" else None
    failed: set[str] = set()

    def attempt(clip: dict, work) -> None:
        """Run one clip's share of a phase; a failure costs only that clip."""
        if clip["id"] in failed:
            return
        try:
            with torch.no_grad():
                work(clip)
        except Exception as exc:
            failed.add(clip["id"])
            clip.pop("latents", None)
            clip.pop("condition", None)
            say("clip_failed", id=clip["id"], error=f"{type(exc).__name__}: {str(exc)[:400]}")
            release()

    def prepare(clip: dict) -> None:
        scheduler = UniPCMultistepScheduler.from_pretrained(
            os.path.join(base, "scheduler"), flow_shift=FLOW_SHIFT)
        scheduler.set_timesteps(STEPS, device=device)
        clip["scheduler"], clip["done"] = scheduler, 0
        generator = torch.Generator("cpu").manual_seed(int(clip["seed"]))
        width, height, frames = clip["width"], clip["height"], clip["frames"]
        if mode == "image":
            from PIL import Image, ImageOps

            still = ImageOps.fit(Image.open(clip["image"]).convert("RGB"), (width, height),
                                 Image.LANCZOS)
            pixels = stills.video_processor.preprocess(still, height=height, width=width).to(
                device, dtype=torch.float32)
            latents, condition = stills.prepare_latents(
                pixels, 1, vae.config.z_dim, height, width, frames, torch.float32, device,
                generator, None, None)
            clip["condition"] = condition.to("cpu")
        else:
            shape = (1, vae.config.z_dim, (frames - 1) // 4 + 1, height // 8, width // 8)
            latents = torch.randn(shape, generator=generator, dtype=torch.float32)
        clip["latents"] = latents.to("cpu")

    def denoise(clip: dict, model, high: bool) -> None:
        scheduler, latents = clip["scheduler"], clip["latents"].to(device)
        condition = clip["condition"].to(device) if mode == "image" else None
        prompt = embeds[clip["prompt"]].to(device)
        say("clip", id=clip["id"])
        timesteps = scheduler.timesteps
        while clip["done"] < len(timesteps):
            t = timesteps[clip["done"]]
            if (t >= BOUNDARY[mode]) != high:
                break
            given = latents if condition is None else torch.cat([latents, condition], dim=1)
            with model.cache_context("cond"):
                noise = model(
                    hidden_states=given.to(torch.bfloat16), timestep=t.expand(1),
                    encoder_hidden_states=prompt, return_dict=False)[0].float()
            latents = scheduler.step(noise, t, latents, return_dict=False)[0]
            clip["done"] += 1
            say("step", id=clip["id"], n=clip["done"], of=len(timesteps))
        clip["latents"] = latents.to("cpu")

    def finish(clip: dict) -> None:
        if clip["done"] < len(clip["scheduler"].timesteps):
            raise RuntimeError("the clip was not fully denoised")
        say("clip", id=clip["id"])
        video = vae.decode(clip.pop("latents").to(device) * std + mean, return_dict=False)[0]
        frames = ((video[0].clamp(-1, 1) + 1) * 127.5).permute(1, 2, 3, 0).round()
        write_mp4(frames.to(torch.uint8).cpu().numpy(), clip["out"], ffmpeg)
        say("clip_done", id=clip["id"], file=clip["out"])

    for clip in clips:
        attempt(clip, prepare)
    stills = None
    release()

    for half in ("high", "low"):
        model = load_transformer(mode, half, base, device)
        for clip in clips:
            attempt(clip, lambda one: denoise(one, model, half == "high"))
            # What a step leaves cached is sized for this clip; the next may
            # be longer, and 81 frames only just fits as it is.
            release()
        model = None
        release()
    # Decoded last, with both transformers gone. Decoding straight after a
    # clip's final step met a card still holding 5 GB of transformer blocks,
    # spilled into shared memory, and cost 8 GB of commit charge for the
    # length of the decode.
    for clip in clips:
        attempt(clip, finish)
        release()


def main() -> int:
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    if job.get("mode") not in ("image", "text"):
        say("fatal", error=f"unknown mode {job.get('mode')!r}")
        return 2
    if len(sys.argv) > 3 and sys.argv[2] == "--parent-pid":
        exit_with(int(sys.argv[3]))
    try:
        run(job)
    except Exception as exc:
        say("fatal", error=f"{type(exc).__name__}: {str(exc)[:400]}")
        return 1
    say("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
