"""What this machine can generate, and how.

One entry per model. An entry declares *what a model is* — the weights, the
pipeline class, the step count it wants, the sizes it can do, how much of the
card it needs — and nothing about queues, caches or projects. Those live on
the Node side, shared by every entry.

This is the file that makes adding Wan 2.2 TI2V 5B an entry rather than a
rewrite: `emits="video"` is already carried through, and a 16 GB card holds
exactly one of these at a time, which is the invariant the server enforces.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Literal


@dataclass(frozen=True)
class ModelSpec:
    """One generatable model, as declared."""

    id: str
    """How the CLI and the cache key name it. Stable: it is in the cache key."""

    repo: str
    """Hugging Face repo the weights come from."""

    pipeline: str
    """A class name on the `diffusers` module, resolved with getattr."""

    emits: Literal["image", "video"]
    """What comes out. `video` is Wan's; nothing in spec 30 declares one."""

    steps: int
    """The model's own step count, which is rarely the pipeline default.

    Z-Image-Turbo is distilled to 9 steps; `ZImagePipeline.__call__` declares
    50, because that is the number for the undistilled model. A registry
    exists so that nothing outside this file has to know the difference.
    """

    guidance: float
    """Classifier-free guidance scale. 0.0 for a distilled model."""

    dtype: str
    """A torch dtype name, e.g. "bfloat16"."""

    sizes: List[str]
    """Sizes this entry supports, `WIDTHxHEIGHT`. The first is the default."""

    granularity: int
    """Width and height must both be multiples of this.

    Z-Image's VAE scale factor is 8 and its image processor doubles that, so
    16 is the real constraint. A request that is not a multiple is refused
    with the nearest legal size rather than silently rounded.
    """

    vram_floor_mb: int
    """Roughly what it needs resident. Used to warn, never to refuse."""

    license: str
    """Carried into the credit. Z-Image-Turbo is Apache-2.0."""

    img2img: str = ""
    """A `diffusers` class that starts from a picture, resolved like `pipeline`.

    Empty means this entry cannot: `/generate` then refuses an `image` and says
    which entry refused it, rather than ignoring the picture and handing back
    something that merely looks wrong.

    Z-Image has one over the very same weights, `ZImageImg2ImgPipeline`, and it
    is built with `from_pipe`. Starting from a picture therefore costs no second
    load and no second 12 GB on a card that holds exactly one model.
    """

    offload: bool = False
    """Load with `enable_model_cpu_offload()` rather than straight onto the card.

    Not a slow fallback on a 16 GB card — the only way the model runs at all.
    See `ZIMAGE_TURBO` below for the measurement. `GEN_OFFLOAD=1` forces it on
    for an entry that declares `False`.
    """

    notes: str = ""


ZIMAGE_TURBO = ModelSpec(
    id="zimage-turbo",
    repo="Tongyi-MAI/Z-Image-Turbo",
    pipeline="ZImagePipeline",
    emits="image",
    # From the model card's own example, not from the pipeline signature.
    steps=9,
    guidance=0.0,
    dtype="bfloat16",
    # 1536x864 first: 16:9 at close to the pixel count the model was trained
    # at, and the default when a caller names no size.
    #
    # The portrait entries are the landscape ones transposed, because
    # MoneyPrinterTurbo renders 9:16 as often as 16:9 and the declared set is
    # what `/v1/images/generations` snaps an incoming size to. Without
    # 864x1536 a portrait request lands on 1024x1024 — the nearest ratio it
    # could find — and a square picture in a 9:16 frame is either cropped or
    # pillarboxed. Z-Image only requires multiples of 16, so a transpose costs
    # nothing and keeps the pixel count where the model is good.
    sizes=[
        "1536x864",   # 16:9
        "864x1536",   # 9:16
        "1024x1024",  # 1:1
        "1280x720",
        "720x1280",
        "1024x576",
        "576x1024",
    ],
    granularity=16,
    # The transformer alone, as bf16. Measured rather than taken from the
    # model card, which is what spec 30 exists to do.
    vram_floor_mb=13000,
    license="Apache-2.0",
    # Verified against the diffusers in the interpreter that runs this server
    # (0.37.0.dev0): `ZImageImg2ImgPipeline.__call__` takes `prompt`, `image`
    # and `strength` over `Tongyi-MAI/Z-Image-Turbo`'s own weights.
    #
    # Z-Image-Edit, which would take an instruction ("make her smile") instead
    # of a strength, is still unreleased, and this build has no inpaint class.
    # So what is declared here is the one thing that actually works today:
    # redraw a picture, keeping its composition.
    img2img="ZImageImg2ImgPipeline",
    # Measured on an RTX 5060 Ti, 16283 MiB usable:
    #
    #   straight onto the card  15988 MiB resident, 86 SECONDS PER STEP
    #   enable_model_cpu_offload  see gen.md
    #
    # The repo ships fp32 weights -- 24.6 GB of transformer and 8.1 GB of text
    # encoder on disk -- which become 12 GB and 4 GB as bf16. 16.1 GB of
    # components on a 16.28 GB card leaves nothing for activations, so the
    # driver spills to host memory and a 7-second image takes thirteen
    # minutes. Offload keeps the text encoder off the card during denoising,
    # which is the whole difference.
    offload=True,
    notes="6B, distilled. 8 to 9 steps at guidance 0. Needs CPU offload under 24 GB.",
)


REGISTRY: Dict[str, ModelSpec] = {ZIMAGE_TURBO.id: ZIMAGE_TURBO}


@dataclass(frozen=True)
class OutlineSpec:
    """How one model redraws a picture while *following its outlines*.

    Starting from a picture with a strength (`ModelSpec.img2img`) is one dial:
    turn it down and the photograph survives, photographic look included; turn
    it up and the style arrives but the model redraws the scene its own way.
    No position gives "this exact picture, in that style".

    A ControlNet does. The photograph is reduced to its outlines, the picture
    is drawn from noise in whatever style the prompt names, and the outlines
    hold every shape where it was.

    Kept out of `ModelSpec` on purpose: `GET /models` is compared field by
    field by its other reader, and this is nothing that reader asks about.
    """

    pipeline: str
    """A `diffusers` class taking `control_image`, built with `from_pipe`."""

    model: str
    """The `diffusers` ControlNet class, loaded with `from_single_file`."""

    repo: str
    filename: str
    """Where the ControlNet weights are. One file, fetched on first use."""

    steps: int
    """Steps when the caller names none."""

    scale: float
    """How hard the outlines hold, when the caller names none."""

    license: str


OUTLINES: Dict[str, OutlineSpec] = {
    ZIMAGE_TURBO.id: OutlineSpec(
        # Both verified against the diffusers in the interpreter that runs this
        # server (0.41.0), whose own example loads exactly this file.
        pipeline="ZImageControlNetPipeline",
        model="ZImageControlNetModel",
        # The first Union release: 3.1 GB on disk, 1.55 GB as bf16. The 2.x
        # files are 6.7 GB, which does not fit beside a 12 GB transformer on a
        # 16 GB card, and this diffusers cannot read the small "lite" ones.
        repo="alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union",
        filename="Z-Image-Turbo-Fun-Controlnet-Union.safetensors",
        steps=9,
        scale=0.75,
        license="Apache-2.0",
    ),
}


FLUX2_KLEIN = ModelSpec(
    id="flux2-klein-4b",
    repo="black-forest-labs/FLUX.2-klein-4B",
    # Verified against the diffusers in the interpreter that runs this server
    # (0.41.0): `Flux2KleinPipeline.__call__` takes `image` and `prompt`, and
    # the prompt is an instruction about that image ("redraw this as ...").
    pipeline="Flux2KleinPipeline",
    emits="image",
    # The distilled 4B: four steps at guidance 1, from its model card.
    steps=4,
    guidance=1.0,
    dtype="bfloat16",
    sizes=list(ZIMAGE_TURBO.sizes),
    granularity=16,
    # Measured on an RTX 5060 Ti with CPU offload: 9266 MiB peak at 1280x720,
    # 16 to 24 seconds a picture.
    vram_floor_mb=9500,
    license="Apache-2.0",
    offload=True,
    notes="4B, distilled. Takes a picture and an instruction about it.",
)

#: Which model redraws a picture *on instruction* for callers of each entry.
#:
#: `OUTLINES` holds a picture's shapes and still needs the prompt to say what
#: the shapes are. An editing model is shown the picture itself, so "redraw
#: this as comic book art" is the whole prompt.
#:
#: Not in `REGISTRY`, for the reason `OutlineSpec` is not in `ModelSpec`:
#: `GET /models` is compared against its other reader's own list. It is a
#: second model all the same, and the card holds one: asking for it swaps the
#: resident model out, and the next plain generation swaps it back.
EDITORS: Dict[str, ModelSpec] = {ZIMAGE_TURBO.id: FLUX2_KLEIN}


def spec_for(model_id: str) -> ModelSpec:
    """The entry for `model_id`, or an error naming every id there is."""
    spec = REGISTRY.get(model_id)
    if spec is None:
        known = ", ".join(sorted(REGISTRY)) or "none"
        raise KeyError(f'No model "{model_id}" in the registry. Known: {known}.')
    return spec


def parse_size(size: str, spec: ModelSpec) -> "tuple[int, int]":
    """`WIDTHxHEIGHT` as numbers, checked against the entry's granularity."""
    parts = size.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f'Size must look like 1536x864, not "{size}".')
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f'Size must look like 1536x864, not "{size}".') from None
    if width <= 0 or height <= 0:
        raise ValueError(f'Size must be positive, not "{size}".')
    step = spec.granularity
    if width % step != 0 or height % step != 0:
        near = f"{max(step, round(width / step) * step)}x{max(step, round(height / step) * step)}"
        raise ValueError(
            f"{spec.id} needs width and height to be multiples of {step}. "
            f'"{size}" is not; the nearest is {near}.'
        )
    return width, height


def as_json(spec: ModelSpec) -> dict:
    """The entry as `GET /models` reports it.

    camelCase on purpose: this is the wire format, and the TypeScript side
    compares it field by field against its own descriptors. Renaming on
    arrival would put the mapping in two places and let them drift.
    """
    raw = asdict(spec)
    renamed = {"vram_floor_mb": "vramFloorMb"}
    return {renamed.get(key, key): value for key, value in raw.items()}
