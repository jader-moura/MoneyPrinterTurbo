"""找一张真实照片，拿来当出图的底子。

为什么要这个
------------
文生图模型没见过"那架 1952 年的彗星号客机"长什么样，它画出来的是"一架老
飞机"。而网上有那架飞机的照片。把照片当底子（img2img），画出来的东西构图、
比例、年代都对得上，而画风仍然是用户挑的那个——照片只提供"长什么样"，不
提供"画成什么样"。

这里只负责"找一张能用的照片"：搜什么由 ``llm.image_search_query`` 写，怎么
画由出图那条路（``material`` → genserver 的 ``image``/``strength``）决定。

供应商
------
``reference_image_provider`` 选一个：

``google_cse``（默认）
    Google 可编程搜索的 Custom Search JSON API，``searchType=image``。能直接
    要"只要照片、要大图"（``imgType``/``imgSize``）。免费额度 100 次/天，而且
    要先去建一个搜索引擎、把图片搜索打开。

``brave``
    Brave 的图片搜索 API，一把 key 就够，不用建引擎。代价是它没有"只要照片"
    这个开关——剪贴画和插画也可能回来，只能靠底下那几道尺寸和类型的检查挡。

配置不全时点名缺哪一项，**不会**悄悄换成别的供应商：换一个供应商等于换一批
搜索结果，用户按下那个按钮时以为搜的是他配的那个。

拿回来的东西是外部内容
----------------------
一张来自任意网站的图片。所以：限大小、限类型、必须能被 Pillow 解码，解完
再重新编码一次（EXIF、颜色描述、动画帧全部丢掉），而且永远不会被当成代码
或者路径使用。
"""

import base64
import io
from dataclasses import dataclass
from typing import Any, Optional

import requests
from loguru import logger

from app.config import config

GOOGLE_CSE = "google_cse"
GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

BRAVE = "brave"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"

PROVIDERS = (GOOGLE_CSE, BRAVE)

# 一张底图最多多大。和 genserver 的 ``MAX_REFERENCE_MB`` 对齐：它那边会拒，
# 这边先别下。
MAX_BYTES = 16 * 1024 * 1024

# 长边不到这么多像素的不要。缩略图和图标放大之后只是一团糊，而底图糊了，
# 画出来的也糊。
MIN_LONG_EDGE = 512

# 一次搜几条、最多试下载几条。搜索返回的头几条常有打不开的（防盗链、403、
# 已经删了），所以要多拿几条备用；但也不能一直试下去。
SEARCH_COUNT = 8
MAX_ATTEMPTS = 4

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

# 不带任何可以认出这台机器或者这个人的东西。
USER_AGENT = "MoneyPrinterTurbo"


class ReferenceImageError(Exception):
    """找不到能用的底图。文案是给人看的：点名供应商和当时搜的词。"""


@dataclass(frozen=True)
class Reference:
    """一张能用的底图，和它是从哪来的。"""

    data: bytes
    """重新编码过的 PNG 字节。"""

    query: str
    source_url: str
    """图片本身的地址。"""

    page_url: str
    """登载它的那个页面。界面上显示的是这个——用户要能去看一眼来源。"""

    provider: str
    width: int
    height: int

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def payload(self) -> dict:
        """给界面看的那几项。``data`` 不在里面：那是几 MB 的字节。"""
        return {
            "query": self.query,
            "provider": self.provider,
            "source_url": self.source_url,
            "page_url": self.page_url,
            "width": self.width,
            "height": self.height,
        }


def _setting(app_config: Optional[dict], key: str, default: Any = "") -> Any:
    source = app_config if app_config is not None else config.app
    try:
        value = source.get(key, default)
    except AttributeError:
        value = default
    return default if value is None else value


def strength(app_config: Optional[dict] = None) -> float:
    """底图能被改多少。1 是完全不看它，0.75 是只留构图。

    留在这里而不是留在界面上：它和"底图"是同一件事的两半，而且用户调它的
    次数远少于按那个按钮的次数。
    """
    try:
        value = float(_setting(app_config, "reference_image_strength", 0.75))
    except (TypeError, ValueError):
        return 0.75
    if not 0.0 < value <= 1.0:
        logger.warning(
            f"reference_image_strength must be above 0 and at most 1, not {value}; "
            f"using 0.75"
        )
        return 0.75
    return value


def search(query: str, *, app_config: Optional[dict] = None) -> Reference:
    """搜一张照片回来。搜不到、下不来、解不开都抛 ``ReferenceImageError``。"""
    wanted = " ".join(str(query or "").split())
    if not wanted:
        raise ReferenceImageError("there is nothing to search for: the query is empty")

    provider = str(_setting(app_config, "reference_image_provider", GOOGLE_CSE)).strip()
    finder = {GOOGLE_CSE: _google_cse, BRAVE: _brave}.get(provider)
    if finder is None:
        # 不认识的供应商不能退回默认那个：用户以为搜的是他写的那个。
        raise ReferenceImageError(
            f"reference_image_provider {provider!r} is not one this build knows; "
            "the ones it knows are "
            + " and ".join(repr(name) for name in PROVIDERS)
        )
    return finder(wanted, app_config)


def _safe_on(app_config: Optional[dict]) -> bool:
    """要不要开 SafeSearch。两家的参数名和取值不一样，这里只管开关。"""
    value = str(_setting(app_config, "reference_image_safe", "active")).strip().lower()
    return value not in ("off", "none", "false", "0", "no")


def _google_cse(query: str, app_config: Optional[dict]) -> Reference:
    key = str(_setting(app_config, "google_search_api_key", "")).strip()
    engine = str(_setting(app_config, "google_search_engine_id", "")).strip()
    missing = [
        name
        for name, value in (
            ("google_search_api_key", key),
            ("google_search_engine_id", engine),
        )
        if not value
    ]
    if missing:
        raise ReferenceImageError(
            "google_cse is not configured: set "
            + " and ".join(missing)
            + " in [app] of config.toml. The key comes from "
            "https://developers.google.com/custom-search/v1/introduction and the "
            "engine id from https://programmablesearchengine.google.com/ "
            "(switch image search on for it). Brave is the other way to search: "
            'set reference_image_provider = "brave" and brave_search_api_key, '
            "which needs no engine to be built first."
        )

    params = {
        "key": key,
        "cx": engine,
        "q": query,
        "searchType": "image",
        "num": SEARCH_COUNT,
        # 要照片，不要剪贴画和线稿：底图的作用是"这东西长什么样"。
        "imgType": "photo",
        "imgSize": "large",
        "safe": str(_setting(app_config, "reference_image_safe", "active")).strip()
        or "active",
    }
    logger.info(f"searching google for a reference photo: query={query!r}")
    try:
        response = requests.get(
            GOOGLE_CSE_ENDPOINT,
            params=params,
            headers={"User-Agent": USER_AGENT},
            proxies=config.proxy,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except Exception as exc:
        # 错误里可能带着整个请求 URL，而 URL 里有那把 key。
        raise ReferenceImageError(
            f"google_cse could not be reached: {type(exc).__name__}"
        ) from None

    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise ReferenceImageError(
            f"google_cse answered {status}: {_google_reason(response)}"
        )
    try:
        payload = response.json()
    except Exception:
        raise ReferenceImageError(
            "google_cse answered 200 with something that is not JSON"
        ) from None

    items = payload.get("items") if isinstance(payload, dict) else None
    return _first_usable(query, GOOGLE_CSE, _google_candidates(items))


def _google_candidates(items: Any) -> "list[tuple[str, str]]":
    out: list[tuple[str, str]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("image") if isinstance(item.get("image"), dict) else {}
        out.append((str(item.get("link") or ""), str(meta.get("contextLink") or "")))
    return out


def _brave(query: str, app_config: Optional[dict]) -> Reference:
    key = str(_setting(app_config, "brave_search_api_key", "")).strip()
    if not key:
        raise ReferenceImageError(
            "brave is not configured: set brave_search_api_key in [app] of "
            "config.toml. The key comes from "
            "https://api-dashboard.search.brave.com/ - subscribe to an image "
            "search plan and the key works straight away, with no search engine "
            "to build first."
        )

    params = {
        "q": query,
        "count": SEARCH_COUNT,
        # Brave 的图片搜索只认 "strict" 和 "off"。
        "safesearch": "strict" if _safe_on(app_config) else "off",
    }
    logger.info(f"searching brave for a reference photo: query={query!r}")
    try:
        response = requests.get(
            BRAVE_ENDPOINT,
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                # key 走请求头，所以它不会出现在任何一条带 URL 的日志里。
                "X-Subscription-Token": key,
            },
            proxies=config.proxy,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except Exception as exc:
        raise ReferenceImageError(
            f"brave could not be reached: {type(exc).__name__}"
        ) from None

    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise ReferenceImageError(f"brave answered {status}: {_brave_reason(response)}")
    try:
        payload = response.json()
    except Exception:
        raise ReferenceImageError(
            "brave answered 200 with something that is not JSON"
        ) from None

    results = payload.get("results") if isinstance(payload, dict) else None
    return _first_usable(query, BRAVE, _brave_candidates(results))


def _brave_candidates(results: Any) -> "list[tuple[str, str]]":
    """Brave 的一条结果里，``properties.url`` 是图，``url`` 是登载它的页面。"""
    out: list[tuple[str, str]] = []
    if not isinstance(results, list):
        return out
    for item in results:
        if not isinstance(item, dict):
            continue
        where = item.get("properties")
        picture = where.get("url") if isinstance(where, dict) else ""
        out.append((str(picture or ""), str(item.get("url") or "")))
    return out


def _brave_reason(response: Any) -> str:
    """Brave 的错误体里那句人话。"""
    try:
        body = response.json()
    except Exception:
        return "no reason given"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        said = " ".join(
            part
            for part in (
                str(error.get("detail") or "").strip(),
                str(error.get("code") or "").strip(),
            )
            if part
        )
        if said:
            return said
    return "no reason given"


def _first_usable(
    query: str, provider: str, candidates: "list[tuple[str, str]]"
) -> Reference:
    """一条条试下来，返回第一张真能下得来、看得清的照片。

    ``candidates`` 是 ``(图片地址, 登载页面)``，顺序就是搜索引擎给的顺序。两
    家供应商共用这一段：下载、限大小、限类型、重新编码的规矩必须一样，不然
    换个供应商就等于换一套安全检查。
    """
    refused: list[str] = []
    tried: set[str] = set()
    for url, page in candidates:
        # 试过几条就够了。头几条打不开是常事（防盗链、403、已经删了），但
        # 一直试下去会把一次点击拖成半分钟。
        if len(refused) >= MAX_ATTEMPTS:
            break
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")) or url in tried:
            continue
        tried.add(url)
        try:
            data, width, height = _fetch(url)
        except ReferenceImageError as exc:
            refused.append(f"{_host(url)}: {exc}")
            continue
        logger.info(
            f"reference photo: {width}x{height} from {_host(url)} "
            f"for query={query!r}"
        )
        return Reference(
            data=data,
            query=query,
            source_url=url,
            page_url=str(page or url),
            provider=provider,
            width=width,
            height=height,
        )

    if not refused:
        raise ReferenceImageError(f"{provider} found no pictures for {query!r}")
    raise ReferenceImageError(
        f"{provider} found pictures for {query!r} but none could be used: "
        + "; ".join(refused[:MAX_ATTEMPTS])
    )


def _google_reason(response: Any) -> str:
    """Google 的错误体里那句人话，没有就退回状态码的文本。"""
    try:
        body = response.json()
    except Exception:
        return "no reason given"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        reasons = error.get("errors")
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
            reason = str(reasons[0].get("reason") or "").strip()
            if reason and reason not in message:
                message = f"{message} ({reason})"
        if message:
            return message
    return "no reason given"


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _fetch(url: str) -> "tuple[bytes, int, int]":
    """下一张图回来，验过、重新编码过的 PNG 字节。

    流式读 + 边读边数：先信 ``Content-Length`` 再读完的话，一个撒谎的服务器
    就能让这个进程吃下几个 G。
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
            proxies=config.proxy,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            stream=True,
            allow_redirects=True,
        )
    except Exception as exc:
        raise ReferenceImageError(f"{type(exc).__name__}") from None

    with response:
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise ReferenceImageError(f"answered {status}")
        kind = str(response.headers.get("Content-Type") or "").split(";")[0].strip()
        if kind and not kind.lower().startswith("image/"):
            raise ReferenceImageError(f"is {kind or 'of unknown type'}, not an image")
        declared = response.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > MAX_BYTES:
            raise ReferenceImageError(
                f"is {int(declared) // (1024 * 1024)} MB, over the "
                f"{MAX_BYTES // (1024 * 1024)} MB limit"
            )
        buffer = io.BytesIO()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buffer.write(chunk)
            if buffer.tell() > MAX_BYTES:
                raise ReferenceImageError(
                    f"is over the {MAX_BYTES // (1024 * 1024)} MB limit"
                )
    raw = buffer.getvalue()
    if not raw:
        raise ReferenceImageError("came back empty")
    return _decode(raw)


def _decode(raw: bytes) -> "tuple[bytes, int, int]":
    """解码、检查尺寸、重新编码成 PNG。

    重新编码是故意的：丢掉 EXIF（里头可能有拍摄地点）、丢掉颜色描述文件、
    丢掉除第一帧以外的所有帧。出图模型要的只是像素。
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            picture = opened.convert("RGB")
    except UnidentifiedImageError:
        raise ReferenceImageError("is not a picture") from None
    except Exception as exc:
        raise ReferenceImageError(f"could not be read: {type(exc).__name__}") from None

    if max(picture.width, picture.height) < MIN_LONG_EDGE:
        raise ReferenceImageError(
            f"is only {picture.width}x{picture.height}; the long edge has to be "
            f"at least {MIN_LONG_EDGE}px"
        )
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue(), picture.width, picture.height
