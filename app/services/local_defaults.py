"""没设过的时候，默认用这台机器上的那几个服务。

为什么要有这个模块
------------------
这个仓库的默认搭配是三样本机服务：Ollama 写脚本，genserver 出图，Kokoro
配音。可"默认"以前只写在 ``config.local.example.toml`` 里——也就是说，只有
照着那个文件抄过一遍的 config.toml 才真的指向它们。一份缺了这几行的
config.toml（重装过、从别的示例抄的、被别的工具改过）开不了片子：出图
那一步说"没配置"，写脚本那一步说"没设模型"，而用户明明什么都装好了。

所以这里做一件很小的事：**只在缺的时候**，把这几项补成本机的值。

只补缺的
--------
* 已经写了值的，一个字不动——哪怕它指向别处、哪怕它看起来不对。那是用户
  自己的选择。
* 出图地址和模型要么一起补，要么都不补：地址已经指向别处（比如一个计费
  中转）而模型空着，那不是"缺默认值"，是用户还没配完，不替他猜。
* Ollama 的模型不写死一个名字：问 Ollama 它装了哪些，有示例里推荐的那个
  就用它，没有就用第一个。Ollama 没应答就什么都不补，照实说。

补上的值写进 config.toml，所以它只发生一次，之后在设置里看得见、改得了。

When a setting is missing, default to the services on this machine. Only
missing values are filled — a value that is already there is never touched,
whatever it points at. The image address and model are filled together or not
at all, and the Ollama model is chosen from what Ollama says is installed
rather than hard-coded. What is filled is written to config.toml, so it happens
once and is visible and editable in Settings afterwards.
"""

from typing import Mapping, Optional

import requests
from loguru import logger

from app.config import config
from app.models import const
from app.models.llm_provider import DEFAULT_LLM_PROVIDER_ID

# config.local.example.toml 里推荐的那个。装了就优先用它。
PREFERRED_OLLAMA_MODEL = "qwen3:14b"
OLLAMA_TIMEOUT_SECONDS = 3


def _text(app: Mapping, key: str) -> str:
    return str(app.get(key) or "").strip()


def _provider(app: Mapping) -> str:
    return _text(app, "llm_provider").lower() or DEFAULT_LLM_PROVIDER_ID


def ollama_base_url(app: Optional[Mapping] = None) -> str:
    app = config.app if app is None else app
    return _text(app, "ollama_base_url") or config.get_default_ollama_base_url()


def installed_ollama_models(app: Optional[Mapping] = None) -> list:
    """Ollama 装了哪些能写字的模型。连不上就是空列表。"""
    base = ollama_base_url(app).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        response = requests.get(f"{base}/api/tags", timeout=OLLAMA_TIMEOUT_SECONDS)
        response.raise_for_status()
        listed = response.json().get("models") or []
    except Exception as exc:
        logger.info(f"ollama did not list its models ({type(exc).__name__})")
        return []
    names = [str(one.get("name") or "").strip() for one in listed
             if isinstance(one, dict)]
    # 向量模型不会写脚本。
    return [name for name in names if name and "embed" not in name.lower()]


def _pick_model(models: list) -> str:
    if PREFERRED_OLLAMA_MODEL in models:
        return PREFERRED_OLLAMA_MODEL
    return models[0] if models else ""


def missing(app: Optional[Mapping] = None) -> dict:
    """缺了哪几项、该补成什么。只算，不写。"""
    app = config.app if app is None else app
    fills: dict = {}

    address = _text(app, "openai_image_base_url")
    model = _text(app, "openai_image_model")
    if not address and not model:
        fills["openai_image_base_url"] = const.DEFAULT_OPENAI_IMAGE_BASE_URL
        fills["openai_image_model"] = const.DEFAULT_OPENAI_IMAGE_MODEL
    elif address == const.DEFAULT_OPENAI_IMAGE_BASE_URL and not model:
        fills["openai_image_model"] = const.DEFAULT_OPENAI_IMAGE_MODEL

    if _provider(app) == "ollama" and not _text(app, "ollama_model_name"):
        chosen = _pick_model(installed_ollama_models(app))
        if chosen:
            fills["ollama_model_name"] = chosen
    return fills


def apply() -> dict:
    """把缺的补上并存盘。返回补了什么；什么都不缺就是空的，也不碰文件。"""
    fills = missing()
    if not fills:
        return {}
    config.update_config_values_nonblocking(config.app, fills)
    config.try_save_config()
    logger.info(
        "filled missing settings with this machine's local services: "
        + ", ".join(f"{key}={value}" for key, value in sorted(fills.items()))
    )
    return fills


def problems(app: Optional[Mapping] = None) -> list:
    """补完之后还缺什么。给开片子的表单看：现在说，好过跑到一半才失败。"""
    app = config.snapshot_config_with_pending(config.app) if app is None else app
    found = []
    if _provider(app) == "ollama" and not _text(app, "ollama_model_name"):
        found.append(
            "No Ollama model is chosen, and Ollama did not answer at "
            f"{ollama_base_url(app)} to pick one. Start Ollama, or choose a "
            "model under Settings → Language model."
        )
    source = _text(app, "video_source") or const.DEFAULT_VIDEO_SOURCE
    if source == "openai_image" and not (
        _text(app, "openai_image_base_url") and _text(app, "openai_image_model")
    ):
        found.append(
            "The image server has an address but no model (or the other way "
            "round). Finish it under Settings → Pictures and video."
        )
    return found
