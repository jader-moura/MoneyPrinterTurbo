"""用账号登录的那三家语言模型：Codex、ChatGPT、Kimi Code。

它们不用 API Key，而是"去官网点一下授权"。这一步以前只在 WebUI 的设置
对话框里，于是设置搬进编辑器之后，这三家还得回 :8501 去登录。

三家的流程不一样（两家是设备码，一家是本机浏览器回调），但界面要问的只有
四件事：现在连着没有、开始登录、登录到哪一步了、断开。这里把三家收成同
一个形状，界面只画一种面板。

登录到一半的状态放在本进程的内存里，一家一份。WebUI 把它放在浏览器会话里；
这里没有会话，而且登录是"这台机器连上这个账号"，本来就是全局的事。API
重启会丢掉走到一半的登录——重新点一次就好，已经连上的账号不受影响（凭据
由各家自己的模块存在盘上）。

Sign-in for the three language-model providers that use an account instead of
an API key. Their flows differ, but the page only needs four things: status,
begin, poll, disconnect. A half-finished sign-in lives in this process's
memory, one per provider; a finished one is stored on disk by the provider's
own module, as before.
"""

import io
import json
import os
import threading
import time
from typing import Optional

import requests
from loguru import logger

from app.config import config
from app.services import chatgpt_oauth, codex_oauth, kimi_code_oauth
from app.utils import utils


class SignInError(ValueError):
    """这一步做不了。文案直接给用户看。"""


PROVIDERS = ("openai_codex", "openai_chatgpt", "kimi_code")
LABELS = {
    "openai_codex": "ChatGPT (Codex)",
    "openai_chatgpt": "ChatGPT",
    "kimi_code": "Kimi",
}

_lock = threading.RLock()
_flows: dict = {}
_notes: dict = {}
_texts: Optional[dict] = None


def _text(key: str, **params) -> str:
    """各家模块抛的是文案的键（``codex.oauth.expired``）；这里换成英文句子。"""
    global _texts
    if _texts is None:
        try:
            path = os.path.join(utils.root_dir(), "webui", "i18n", "en.json")
            with io.open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            _texts = data.get("Translation", data)
        except Exception as exc:  # pragma: no cover - 读不到就显示键本身
            logger.warning(f"cannot read the sign-in messages: {exc}")
            _texts = {}
    text = str(_texts.get(key, key))
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def _known(provider: str) -> str:
    if provider not in PROVIDERS:
        raise SignInError(f"{provider!r} does not sign in with an account")
    return provider


def _kimi_region() -> str:
    return kimi_code_oauth.resolve_region(config.app.get("kimi_code_base_url"))


def _finish(provider: str, message: str = "", kind: str = "") -> None:
    _flows.pop(provider, None)
    if message:
        _notes[provider] = {"message": message, "kind": kind or "warn"}
    else:
        _notes.pop(provider, None)


# ---------------------------------------------------------------------------
# 每一家：现在连着没有
# ---------------------------------------------------------------------------

def _connected(provider: str) -> dict:
    if provider == "openai_codex":
        try:
            return {"connected": bool(codex_oauth.account_status()),
                    "detail": _text("codex.oauth.shared")}
        except codex_oauth.CodexError as error:
            # 凭据读不出来：既不算连着，也得让人能点"断开"把它清掉。
            return {"connected": False, "broken": True,
                    "problem": _text(error.key, **error.params)}
    if provider == "openai_chatgpt":
        try:
            details = chatgpt_oauth.account_details()
        except chatgpt_oauth.ChatGPTError as error:
            return {"connected": False, "broken": error.key != "chatgpt.oauth.busy",
                    "problem": _text(error.key, **error.params)}
        state = {
            "connected": bool(details.get("authorized")),
            "account": str(details.get("email") or ""),
            "detail": _text("chatgpt.oauth.eligibility"),
        }
        if details.get("connected") and not details.get("authorized"):
            # 登录了，但没有开通"用套餐额度"的许可：这不是没连上，得照实说。
            state["signed_in"] = True
            state["problem"] = _text("chatgpt.oauth.plan_required")
        return state
    connected = bool(kimi_code_oauth.is_authorized())
    state = {"connected": connected}
    if connected:
        region = kimi_code_oauth.authorized_region() or _kimi_region()
        if region != _kimi_region():
            state["problem"] = _text("kimi_code.oauth.region_mismatch",
                                     authorized=region, selected=_kimi_region())
        else:
            state["detail"] = _text("kimi_code.oauth.authorized", region=region)
    return state


def status(provider: str) -> dict:
    """这一家现在的样子。只读，不发任何登录请求。"""
    provider = _known(provider)
    with _lock:
        state = {"provider": provider, "label": LABELS[provider]}
        state.update(_connected(provider))
        flow = _flows.get(provider)
        if flow:
            state["pending"] = {
                "url": flow["url"],
                "code": flow.get("user_code", ""),
                "message": flow["message"],
            }
        note = _notes.get(provider)
        if note:
            state["note"] = dict(note)
        return state


# ---------------------------------------------------------------------------
# 开始、轮询、取消、断开
# ---------------------------------------------------------------------------

def begin(provider: str) -> dict:
    """开始登录：拿到要去打开的那个网址（和要输的码，如果有）。"""
    provider = _known(provider)
    with _lock:
        _notes.pop(provider, None)
        if provider in _flows:
            return status(provider)
        now = time.time()
        try:
            if provider == "openai_codex":
                raw = codex_oauth.request_device_code()
                flow = {"raw": raw, "url": codex_oauth.VERIFY_URL,
                        "user_code": raw["user_code"],
                        "interval": raw["interval"],
                        "message": _text("codex.oauth.pending")}
            elif provider == "openai_chatgpt":
                details = chatgpt_oauth.account_details()
                raw = chatgpt_oauth.begin_login(
                    enable_plan=bool(details.get("connected"))
                )
                flow = {"raw": raw, "url": raw["authorize_url"], "interval": 2,
                        "message": _text("chatgpt.oauth.pending")}
            else:
                region = _kimi_region()
                raw = kimi_code_oauth.request_device_code(region)
                flow = {
                    "raw": raw, "region": region,
                    "url": raw["verification_uri_complete"],
                    "user_code": raw.get("user_code", ""),
                    "interval": max(int(raw.get("interval", 5)), 2),
                    "expires_at": now + float(raw.get("expires_in", 600)),
                    "generation": kimi_code_oauth.authorization_generation(),
                    "network_errors": 0,
                    "message": _text("kimi_code.oauth.pending"),
                }
        except (codex_oauth.CodexError, chatgpt_oauth.ChatGPTError) as error:
            raise SignInError(_text(error.key, **error.params)) from error
        except requests.RequestException as exc:
            raise SignInError(_text("kimi_code.oauth.network_error")) from exc
        except ValueError as exc:
            raise SignInError(str(exc)) from exc
        flow["next_poll_at"] = now + flow["interval"]
        _flows[provider] = flow
        return status(provider)


def _poll_codex(flow: dict) -> None:
    raw = flow["raw"]
    try:
        if raw["generation"] != codex_oauth.authorization_generation():
            raise codex_oauth.CodexError("codex.oauth.changed")
        if time.time() >= raw["expires_at"]:
            raise codex_oauth.CodexError("codex.oauth.expired")
        if codex_oauth.poll_login(raw):
            _finish("openai_codex", _text("codex.oauth.connected"), "ok")
    except codex_oauth.CodexError as error:
        _finish("openai_codex", _text(error.key, **error.params))


def _poll_chatgpt(flow: dict) -> None:
    try:
        if chatgpt_oauth.poll_login(flow["raw"]):
            _finish("openai_chatgpt", _text("chatgpt.oauth.connected"), "ok")
    except chatgpt_oauth.ChatGPTError as error:
        _finish("openai_chatgpt", _text(error.key, **error.params))


def _poll_kimi(flow: dict) -> None:
    if flow["generation"] != kimi_code_oauth.authorization_generation():
        return _finish("kimi_code", _text("kimi_code.oauth.changed"))
    if time.time() >= flow["expires_at"]:
        return _finish("kimi_code", _text("kimi_code.oauth.expired"))
    try:
        payload = kimi_code_oauth.poll_token(flow["region"], flow["raw"]["device_code"])
    except requests.RequestException:
        # 网络抖一下不等于登录失败；连着五次才放弃。
        flow["network_errors"] += 1
        if flow["network_errors"] >= 5:
            _finish("kimi_code", _text("kimi_code.oauth.network_error"))
        return None
    except ValueError as error:
        return _finish("kimi_code", str(error))
    flow["network_errors"] = 0
    if payload.get("expired"):
        return _finish("kimi_code", _text("kimi_code.oauth.expired"))
    if payload.get("denied"):
        return _finish("kimi_code", _text("kimi_code.oauth.denied"))
    if payload.get("pending"):
        if payload.get("slow_down"):
            flow["interval"] += 5
        return None
    try:
        kimi_code_oauth.store_token_bundle(
            flow["region"], payload, expected_generation=flow["generation"]
        )
    except ValueError:
        return _finish("kimi_code", _text("kimi_code.oauth.changed"))
    return _finish("kimi_code", _text("kimi_code.oauth.success"), "ok")


def poll(provider: str) -> dict:
    """看一眼登录走到哪了。界面每隔两秒问一次；问得再勤，向对方发请求的
    节奏也由对方给的间隔决定。"""
    provider = _known(provider)
    with _lock:
        flow = _flows.get(provider)
        if flow and time.time() >= flow["next_poll_at"]:
            {"openai_codex": _poll_codex, "openai_chatgpt": _poll_chatgpt,
             "kimi_code": _poll_kimi}[provider](flow)
            if provider in _flows:
                flow["next_poll_at"] = time.time() + flow["interval"]
        return status(provider)


def cancel(provider: str) -> dict:
    """不登了。已经连着的账号不动。"""
    provider = _known(provider)
    with _lock:
        flow = _flows.pop(provider, None)
        if flow and provider == "openai_chatgpt":
            try:
                chatgpt_oauth.cancel_login(flow["raw"])
            except Exception as exc:  # pragma: no cover - 取消失败也当取消了
                logger.warning(f"chatgpt sign-in cancel failed: {exc}")
        _notes.pop(provider, None)
        return status(provider)


def disconnect(provider: str) -> dict:
    """断开这个账号，把这台机器上存的凭据清掉。"""
    provider = _known(provider)
    with _lock:
        cancel(provider)
        if provider == "openai_codex":
            codex_oauth.disconnect()
        elif provider == "openai_chatgpt":
            confirmed = chatgpt_oauth.disconnect()
            if not confirmed:
                # 本机清掉了，但对方没确认收回授权——得说，不能当成断干净了。
                _notes[provider] = {
                    "message": _text("chatgpt.oauth.revocation_unconfirmed"),
                    "kind": "warn",
                }
        else:
            kimi_code_oauth.clear_credentials()
        return status(provider)
