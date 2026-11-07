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
