"""The local Z-Image-Turbo sidecar, over real HTTP, with no GPU.

Mirrors `test_local_image_server.py`: the actual request handler runs on a
real socket and the only things replaced are the parts that need a card — the
diffusers pipeline and torch's cuda namespace. The point is the contract
`video_source = "openai_image"` depends on, so these tests assert status
codes and the `data[].b64_json` shape rather than anything about the model.

    python -m pytest test/services/test_genserver.py
"""

import base64
import importlib.util
import io
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[2] / "local_services" / "genserver"
# The sidecar imports `registry` as a top-level module from its own folder, the
# way it does when run as a script.
sys.path.insert(0, str(SERVER_DIR))
_spec = importlib.util.spec_from_file_location("genserver_server", SERVER_DIR / "server.py")
srv = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = srv
_spec.loader.exec_module(srv)

MB = 1024 * 1024


class FakeCuda:
    """Just enough of `torch.cuda` for the server's bookkeeping."""

    def __init__(self):
        self.emptied = 0

    def is_available(self):
        return True

    def empty_cache(self):
        self.emptied += 1

    def ipc_collect(self):
        pass

    def reset_peak_memory_stats(self):
        pass

    def max_memory_allocated(self):
        return 11_000 * MB

    def mem_get_info(self):
        return 4_000 * MB, 16_311 * MB

    def get_device_capability(self, index=0):
        return 12, 0

    def get_device_name(self, index=0):
        return "Stand-in card"

    def get_arch_list(self):
        return ["sm_120"]


class FakeGenerator:
    def __init__(self, device):
        self.device = device
        self.seed = None

    def manual_seed(self, seed):
        self.seed = seed
        return self


class FakeTorchVersion:
    cuda = "12.8"


class FakeTorch:
    bfloat16 = "bfloat16"
    __version__ = "2.9.0+stand-in"
    version = FakeTorchVersion()
    Generator = FakeGenerator

    def __init__(self):
        self.cuda = FakeCuda()


class FakePipe:
    """Records what it was asked to paint and returns a tiny image."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        image = Image.new("RGB", (8, 8), (20, 30, 40))
        return type("Result", (), {"images": [image]})()


class FakeOllama:
    """Stands in for ollama's /api/ps and /api/generate."""

    def __init__(self, models=()):
        self.models = list(models)
        self.unloaded = []

    def get(self, url, timeout=None):
        assert url.endswith("/api/ps")
        models = [{"name": name} for name in self.models]
        return type("Resp", (), {"json": lambda _self: {"models": models}})()

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/api/generate") and json["keep_alive"] == 0
        self.models.remove(json["model"])
        self.unloaded.append(json["model"])


class Harness:
    def __init__(self, base_url, pipe, ollama, loads):
        self.base_url = base_url
        self.pipe = pipe
        self.ollama = ollama
        self.loads = loads

    @property
    def painted(self):
        """The sizes painted, in order."""
        return [(c["width"], c["height"]) for c in self.pipe.calls]

    def post(self, path, **body):
        return requests.post(f"{self.base_url}{path}", json=body, timeout=30)

    def get(self, path):
        return requests.get(f"{self.base_url}{path}", timeout=30)

    def image(self, **body):
        body.setdefault("prompt", "a forge at night")
        return self.post("/v1/images/generations", **body)


@pytest.fixture
def server(monkeypatch):
    pipe = FakePipe()
    ollama = FakeOllama()
    loads = []

    def fake_load(spec, offload):
        loads.append((spec.id, offload))
        srv.RESIDENT.pipe = pipe
        srv.RESIDENT.model_id = spec.id
        return {"resident": spec.id, "seconds": 0.0, "offload": offload}

    monkeypatch.setattr(srv, "torch", FakeTorch())
    monkeypatch.setattr(srv, "TORCH_IMPORT_ERROR", None)
    monkeypatch.setattr(srv, "requests", ollama)
    monkeypatch.setattr(srv, "OLLAMA_UNLOAD_TIMEOUT", 1.0)
    monkeypatch.setattr(srv, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(srv.RESIDENT, "load", fake_load)
    monkeypatch.setattr(srv.RESIDENT, "pipe", None)
    monkeypatch.setattr(srv.RESIDENT, "model_id", None)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Harness(f"http://127.0.0.1:{httpd.server_address[1]}", pipe, ollama, loads)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# --- the size contract -----------------------------------------------------


@pytest.mark.parametrize(
    "size", ["1024x0", "0x1024", "-1x1024", "1024", "axb", "1024x1536x2", "x", "1024 x 768"]
)
def test_invalid_size_is_rejected(server, size):
    response = server.image(size=size)
    assert response.status_code == 422
    assert "WIDTHxHEIGHT" in response.json()["error"]["message"]
    assert server.painted == []


@pytest.mark.parametrize(
    "size, expected",
    [
        # MoneyPrinterTurbo's own defaults, when openai_image_size is empty.
        ("1536x1024", (1536, 864)),
        ("1024x1536", (864, 1536)),
        ("1024x1024", (1024, 1024)),
        # Raw video resolutions, which is what a hand-written config tends to
        # carry. Both are exactly 16:9 and 9:16.
        ("1920x1080", (1536, 864)),
        ("1080x1920", (864, 1536)),
        # Sizes the registry declares are painted as asked.
        ("1536x864", (1536, 864)),
        ("864x1536", (864, 1536)),
        ("720x1280", (720, 1280)),
        # No size at all: the entry's first declared size.
        ("auto", (1536, 864)),
        ("", (1536, 864)),
        (None, (1536, 864)),
    ],
)
def test_size_snaps_to_a_declared_size(server, size, expected):
    response = server.image(size=size)
    assert response.status_code == 200
    assert server.painted == [expected]
    assert response.json()["size"] == f"{expected[0]}x{expected[1]}"


def test_a_portrait_request_is_not_painted_square(server):
    """The reason the registry gained transposed sizes.

    Without them the nearest declared ratio to 9:16 was 1:1, and a 9:16 video
    got square pictures.
    """
    assert server.image(size="1080x1920").status_code == 200
    width, height = server.painted[0]
    assert width < height
    assert abs((width / height) - (9 / 16)) < 0.05


# --- the response contract -------------------------------------------------


def test_response_carries_a_decodable_png(server):
    response = server.image(size="1024x1024")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["created"], int)
    assert len(body["data"]) == 1

    raw = base64.b64decode(body["data"][0]["b64_json"])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "PNG"


def test_registry_settings_reach_the_pipeline(server):
    """Z-Image-Turbo is distilled: 9 steps at guidance 0, from the registry."""
    assert server.image().status_code == 200
    call = server.pipe.calls[0]
    assert call["num_inference_steps"] == 9
    assert call["guidance_scale"] == 0.0
    # Distilled means no classifier-free guidance, so a negative prompt would
    # be ignored; the route must not invent one.
    assert "negative_prompt" not in call
    assert call["prompt"] == "a forge at night"


def test_the_declared_model_is_loaded_with_offload(server):
    assert server.image().status_code == 200
    # offload=True is the only way Z-Image runs on a 16 GB card.
    assert server.loads == [("zimage-turbo", True)]


def test_a_second_request_reuses_the_resident_model(server):
    assert server.image().status_code == 200
    assert server.image().status_code == 200
    assert len(server.loads) == 1
    assert len(server.pipe.calls) == 2


@pytest.mark.parametrize("count", [2, 3])
def test_n_paints_that_many_distinct_images(server, count):
    response = server.image(n=count, seed=7)
    assert response.status_code == 200
    assert len(response.json()["data"]) == count
    assert len(server.pipe.calls) == count
    seeds = [call["generator"].seed for call in server.pipe.calls]
    assert seeds == [7 + i for i in range(count)]


@pytest.mark.parametrize("count", [0, -1, 5, "many"])
def test_n_out_of_range_is_rejected(server, count):
    assert server.image(n=count).status_code == 422
    assert server.painted == []


def test_empty_prompt_is_rejected(server):
    assert server.image(prompt="   ").status_code == 422
    assert server.painted == []


def test_unknown_model_is_rejected_as_a_bad_field(server):
    response = server.image(model="stable-diffusion-xl")
    # 422, not the native 404: to an OpenAI client this is a field it got wrong.
    assert response.status_code == 422
    assert "zimage-turbo" in response.json()["error"]["message"]
    assert server.painted == []


def test_a_url_response_format_is_refused_rather_than_faked(server):
    response = server.image(response_format="url")
    assert response.status_code == 422
    assert "b64_json" in response.json()["error"]["message"]


def test_malformed_json_is_rejected(server):
    response = requests.post(
        f"{server.base_url}/v1/images/generations",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    assert response.status_code == 422


# --- sharing the card with ollama ------------------------------------------


def test_ollama_is_unloaded_before_every_image(server):
    server.ollama.models = ["qwen3:14b"]
    assert server.image().status_code == 200

    # Ollama loads again between two requests.
    server.ollama.models = ["qwen3:14b"]
    assert server.image().status_code == 200

    assert server.ollama.unloaded == ["qwen3:14b", "qwen3:14b"]


def test_image_painted_when_ollama_is_not_running(server, monkeypatch):
    def refuse(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(server.ollama, "get", refuse)
    assert server.image().status_code == 200
    assert len(server.painted) == 1


def test_image_painted_when_ollama_will_not_unload(server, monkeypatch):
    server.ollama.models = ["qwen3:14b"]
    monkeypatch.setattr(server.ollama, "post", lambda url, json=None, timeout=None: None)
    monkeypatch.setattr(srv, "OLLAMA_UNLOAD_TIMEOUT", 0.3)
    assert server.image().status_code == 200
    assert len(server.painted) == 1


def test_ollama_is_left_alone_when_turned_off(server, monkeypatch):
    monkeypatch.setenv("GEN_UNLOAD_OLLAMA", "0")
    server.ollama.models = ["qwen3:14b"]
    assert server.image().status_code == 200
    assert server.ollama.unloaded == []


# --- the idle unload -------------------------------------------------------


def test_idle_unload_is_off_by_default():
    """Reloading Z-Image re-reads 24.6 GB from disk; opt in, do not inherit."""
    assert srv.IDLE_SECONDS == 0.0


def test_idle_unload_drops_the_resident_model(server, monkeypatch):
    assert server.image().status_code == 200
    assert srv.RESIDENT.model_id == "zimage-turbo"

    monkeypatch.setattr(srv, "IDLE_SECONDS", 0.0)
    assert srv.unload_if_idle() is False  # 0 means off, not "immediately"

    monkeypatch.setattr(srv, "IDLE_SECONDS", 0.01)
    monkeypatch.setattr(srv, "LAST_USED", 0.0)
    assert srv.unload_if_idle() is True
    assert srv.RESIDENT.model_id is None

    # And the next request loads it again.
    assert server.image().status_code == 200
    assert len(server.loads) == 2


def test_idle_unload_leaves_an_idle_server_alone(server, monkeypatch):
    monkeypatch.setattr(srv, "IDLE_SECONDS", 3600.0)
    assert srv.unload_if_idle() is False


# --- the native routes, unchanged ------------------------------------------


def test_health_still_answers(server):
    body = server.get("/health").json()
    assert body["ok"] is True
    assert body["models"] == ["zimage-turbo"]
    assert body["device"]["totalMb"] == 16_311
    assert body["torch"]["available"] is True


def test_native_models_route_still_answers(server):
    body = server.get("/models").json()
    entry = body["models"][0]
    assert entry["id"] == "zimage-turbo"
    assert entry["steps"] == 9
    assert entry["vramFloorMb"] == 13_000


def test_native_generate_still_returns_png_base64(server):
    response = server.post("/generate", model="zimage-turbo", prompt="iron", size="1536x864")
    assert response.status_code == 200
    body = response.json()
    # The native shape, which the TypeScript caller in video-generator reads.
    assert base64.b64decode(body["pngBase64"])
    assert "b64_json" not in body
    assert body["steps"] == 9 and body["guidance"] == 0.0
    assert body["seed"] is not None


def test_native_generate_keeps_its_own_400_for_an_illegal_size(server):
    """The native route still refuses rather than snapping.

    `/generate` is a direct request for one size and says so when the model
    cannot do it; only the OpenAI route, whose callers send sizes meant for
    somebody else's model, snaps to the nearest declared one.
    """
    response = server.post("/generate", model="zimage-turbo", prompt="iron", size="1080x1920")
    assert response.status_code == 400
    assert "multiples of 16" in response.json()["error"]
    assert server.painted == []


def test_native_unknown_model_is_still_404(server):
    response = server.post("/generate", model="nope", prompt="iron")
    assert response.status_code == 404


def test_native_load_and_unload_still_work(server):
    assert server.post("/load", model="zimage-turbo").status_code == 200
    assert srv.RESIDENT.model_id == "zimage-turbo"
    body = server.post("/unload").json()
    assert body["unloaded"] == "zimage-turbo"
    assert srv.RESIDENT.model_id is None


def test_v1_models_lists_the_registry(server):
    body = server.get("/v1/models").json()
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == ["zimage-turbo"]
    assert body["data"][0]["owned_by"] == "Tongyi-MAI/Z-Image-Turbo"


def test_an_unknown_route_names_the_ones_there_are(server):
    response = server.get("/v1/chat/completions")
    assert response.status_code == 404
    assert "/v1/models" in response.json()["error"]


# --- starting from a picture -----------------------------------------------
#
# The point of these: a reference picture reaches the img2img pipeline with a
# strength, a *fitted* picture reaches it, and a request without one is the
# text-to-image call it always was, byte for byte.


class FakeImagePipe(FakePipe):
    """The img2img pipeline. Records the same way, built from the resident one."""


def draw(server, **body):
    """A native `/generate` call. The route has no default size, so this does."""
    body.setdefault("model", "zimage-turbo")
    body.setdefault("prompt", "a forge")
    body.setdefault("size", "1536x864")
    return server.post("/generate", **body)


def a_png(width=40, height=10, colour=(200, 40, 40)):
    """Base64 of a real PNG, which is what a caller sends."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture
def redraw(server, monkeypatch):
    """`server`, with an img2img pipeline that needs no diffusers and no card."""
    image_pipe = FakeImagePipe()
    monkeypatch.setattr(srv.RESIDENT, "starting_from_image", lambda spec: image_pipe)
    server.image_pipe = image_pipe
    return server


def test_the_picture_and_the_strength_reach_the_img2img_pipeline(redraw):
    response = draw(redraw, image=a_png(), strength=0.5)
    assert response.status_code == 200, response.text
    assert redraw.pipe.calls == [], "the text-to-image pipeline must not be used"
    call = redraw.image_pipe.calls[0]
    assert call["strength"] == 0.5
    assert call["image"].size == (1536, 864)


def test_the_picture_is_fitted_to_the_size_that_was_asked_for(redraw):
    """A 4:1 photo into a 9:16 frame: cover-crop, never squashed or letterboxed."""
    body = draw(
        redraw, size="864x1536", image=a_png(width=400, height=100)
    ).json()
    assert (body["width"], body["height"]) == (864, 1536)
    assert redraw.image_pipe.calls[0]["image"].size == (864, 1536)


def test_the_default_strength_redraws_but_keeps_the_composition(redraw):
    body = draw(redraw, image=a_png()).json()
    assert body["strength"] == srv.DEFAULT_STRENGTH == 0.75
    assert body["fromImage"] is True


def test_the_step_count_is_scaled_so_the_model_still_gets_its_nine(redraw):
    """img2img runs about ``steps * strength`` of them; 9 is what it needs."""
    body = draw(redraw, image=a_png(), strength=0.75).json()
    assert body["steps"] == 12
    assert redraw.image_pipe.calls[0]["num_inference_steps"] == 12


def test_a_tiny_strength_does_not_ask_for_ninety_steps(redraw):
    body = draw(redraw, image=a_png(), strength=0.05).json()
    assert body["steps"] == 18


def test_an_explicit_step_count_is_left_alone(redraw):
    body = draw(redraw, image=a_png(), steps=4).json()
    assert body["steps"] == 4


@pytest.mark.parametrize("strength", [0, 0.0, -0.5, 1.5, 2])
def test_an_impossible_strength_is_refused(redraw, strength):
    response = draw(redraw, image=a_png(), strength=strength)
    assert response.status_code == 400
    assert "strength" in response.json()["error"]


@pytest.mark.parametrize("value", ["", "   ", "not base64!!", 7, None])
def test_something_that_is_not_a_picture_is_refused(redraw, value):
    response = draw(redraw, image=value)
    if value is None:
        # No picture at all is the plain text-to-image call, not an error.
        assert response.status_code == 200
        assert response.json()["fromImage"] is False
        return
    assert response.status_code == 400
    assert "image" in response.json()["error"]


def test_a_base64_blob_that_is_not_an_image_is_refused(redraw):
    blob = base64.b64encode(b"\x00\x01\x02 this is not a picture").decode("ascii")
    response = draw(redraw, image=blob)
    assert response.status_code == 400
    assert "picture" in response.json()["error"]


def test_a_picture_over_the_limit_is_refused(redraw, monkeypatch):
    monkeypatch.setattr(srv, "MAX_REFERENCE_MB", 1)
    import os

    noise = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    assert buffer.tell() > srv.MB, "the fixture must exceed the limit it tests"
    response = draw(
        redraw, image=base64.b64encode(buffer.getvalue()).decode("ascii")
    )
    assert response.status_code == 400
    assert "MB" in response.json()["error"]


def test_a_data_url_is_accepted(redraw):
    response = draw(redraw, image="data:image/png;base64," + a_png())
    assert response.status_code == 200


def test_the_openai_route_carries_the_picture_through(redraw):
    response = redraw.image(image=a_png(), strength=0.4, size="864x1536")
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"]
    call = redraw.image_pipe.calls[0]
    assert call["strength"] == 0.4
    assert call["image"].size == (864, 1536)


def test_text_to_image_is_untouched_when_no_picture_is_sent(redraw):
    body = draw(redraw).json()
    assert redraw.image_pipe.calls == []
    call = redraw.pipe.calls[0]
    assert "image" not in call and "strength" not in call
    assert body["steps"] == 9
    assert body["fromImage"] is False
    assert body["strength"] is None


def test_an_entry_that_cannot_do_it_says_so(redraw, monkeypatch):
    import dataclasses

    spec = dataclasses.replace(srv.REGISTRY["zimage-turbo"], img2img="")
    monkeypatch.setitem(srv.REGISTRY, "zimage-turbo", spec)
    native = draw(redraw, image=a_png())
    assert native.status_code == 400
    assert "img2img" in native.json()["error"]
    openai = redraw.image(image=a_png())
    assert openai.status_code == 422


def test_the_registry_advertises_the_capability(server):
    entry = server.get("/models").json()["models"][0]
    assert entry["img2img"] == "ZImageImg2ImgPipeline"


# --- the second pipeline over the same weights -----------------------------


class FakeDiffusers:
    """Stands in for the `diffusers` module `starting_from_image` imports."""

    __version__ = "0.37.0+stand-in"

    def __init__(self, with_class=True):
        self.built_from = []
        if with_class:
            outer = self

            class ZImageImg2ImgPipeline:
                @staticmethod
                def from_pipe(pipe):
                    outer.built_from.append(pipe)
                    return FakeImagePipe()

            self.ZImageImg2ImgPipeline = ZImageImg2ImgPipeline


@pytest.fixture
def resident(monkeypatch):
    """A resident text-to-image pipeline, with `diffusers` stood in for."""
    fake = FakeDiffusers()
    monkeypatch.setitem(sys.modules, "diffusers", fake)
    monkeypatch.setattr(srv.RESIDENT, "pipe", FakePipe())
    monkeypatch.setattr(srv.RESIDENT, "model_id", "zimage-turbo")
    monkeypatch.setattr(srv.RESIDENT, "image_pipe", None)
    return fake


def test_it_is_built_from_the_resident_pipeline_not_loaded_again(resident):
    """A second load would ask a 16 GB card for a second 12 GB transformer."""
    spec = srv.REGISTRY["zimage-turbo"]
    built = srv.RESIDENT.starting_from_image(spec)
    assert resident.built_from == [srv.RESIDENT.pipe]
    assert isinstance(built, FakeImagePipe)


def test_it_is_built_once_and_then_reused(resident):
    spec = srv.REGISTRY["zimage-turbo"]
    first = srv.RESIDENT.starting_from_image(spec)
    assert srv.RESIDENT.starting_from_image(spec) is first
    assert len(resident.built_from) == 1


def test_unloading_lets_go_of_it_too(resident, monkeypatch):
    """It holds the same transformer; left behind, the card stays full."""
    monkeypatch.setattr(srv, "torch", FakeTorch())
    srv.RESIDENT.starting_from_image(srv.REGISTRY["zimage-turbo"])
    assert srv.RESIDENT.image_pipe is not None
    srv.RESIDENT.unload()
    assert srv.RESIDENT.image_pipe is None


def test_it_refuses_when_nothing_is_resident(resident, monkeypatch):
    monkeypatch.setattr(srv.RESIDENT, "model_id", None)
    monkeypatch.setattr(srv.RESIDENT, "pipe", None)
    with pytest.raises(RuntimeError, match="not resident"):
        srv.RESIDENT.starting_from_image(srv.REGISTRY["zimage-turbo"])


def test_it_names_the_class_when_diffusers_is_too_old(monkeypatch):
    monkeypatch.setitem(sys.modules, "diffusers", FakeDiffusers(with_class=False))
    monkeypatch.setattr(srv.RESIDENT, "pipe", FakePipe())
    monkeypatch.setattr(srv.RESIDENT, "model_id", "zimage-turbo")
    monkeypatch.setattr(srv.RESIDENT, "image_pipe", None)
    with pytest.raises(RuntimeError, match="ZImageImg2ImgPipeline"):
        srv.RESIDENT.starting_from_image(srv.REGISTRY["zimage-turbo"])


def test_an_entry_without_one_says_so(resident):
    import dataclasses

    spec = dataclasses.replace(srv.REGISTRY["zimage-turbo"], img2img="")
    with pytest.raises(ValueError, match="img2img"):
        srv.RESIDENT.starting_from_image(spec)
