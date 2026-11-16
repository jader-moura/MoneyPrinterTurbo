"""一个小浏览器窗口：用户在里面点哪张图，就拿到哪张图。

为什么要自己开一个浏览器
------------------------
"照着照片重画"要先有一张照片。走搜索 API 要密钥、有额度，搜出来的也常常
不是要的那一张。让用户自己在 Google 图片里挑最直接——可是编辑器是一张网
页，它读不到另一个网站的窗口里点了什么（浏览器不让，也不该让）。

所以这里由**服务端**开一个窗口：就是这台电脑上装的 Chrome（或 Edge），用
它自己的一份资料目录，和用户平时用的那个互不相干。服务端通过调试接口
（DevTools Protocol）往页面里放一小段脚本：鼠标移到一张够大的图上会描边，
点下去，这张图就交回来。图的字节也是从这个浏览器里取的——它已经下载过了，
服务端不用自己再去访问那个地址。

没有搜索 API，没有密钥。也没有新的依赖要装浏览器驱动：用的是机器上现成的
浏览器。

It opens the Chrome or Edge already installed on this computer, with its own
profile folder, on a Google Images search. A small script injected over the
DevTools Protocol outlines every picture large enough to draw from and hands
back the one that is clicked; the bytes come out of that browser's own cache.
No search API and no key are involved.

只在跑着这个程序的那台电脑上有用：窗口开在服务端那一边。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Optional

from loguru import logger

from app.config import config
from app.utils import utils


class PhotoBrowserError(Exception):
    """开不了那个窗口，或者它出了事。信息是给用户看的。"""


PROFILE_DIR = "photo-browser-profile"
BINDING = "mptPickPhoto"
# 浏览器起来、把调试端口写进资料目录，一般一两秒。
START_TIMEOUT = 20.0
CALL_TIMEOUT = 20.0
MAX_BYTES = 16 * 1024 * 1024

# 放进每一张页面里的脚本。它不认 Google 的页面结构（那个三天两头变），只认
# 一件事：鼠标底下有没有一张**原图够大**的 <img>。搜索结果格子里的缩略图
# 原图只有两三百像素，点它还是 Google 自己的行为——打开右边的大图；大图够
# 大，点它就是"用这一张"。
INJECTED = r"""
(() => {
  if (window.__mptPhotoPick) return;
  window.__mptPhotoPick = true;
  const MIN = __MIN__;
  const HOW = "Click a picture to open it large, then click the large picture to use it.";
  let bar = null;
  let lit = null;

  function say(text, bad) {
    if (!document.body) return;
    if (!bar) {
      bar = document.createElement("div");
      bar.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);"
        + "z-index:2147483647;max-width:86vw;padding:10px 18px;border-radius:22px;"
        + "font:600 14px/1.35 Arial,sans-serif;color:#fff;box-shadow:0 4px 18px rgba(0,0,0,.45);"
        + "pointer-events:none;text-align:center";
      document.body.appendChild(bar);
    }
    bar.textContent = text;
    bar.style.background = bad ? "#b3261e" : "#1a57c9";
  }
  window.__mptPhotoSay = say;

  function usable(img) {
    return img && img.tagName === "IMG"
      && Math.max(img.naturalWidth, img.naturalHeight) >= MIN
      && img.getBoundingClientRect().width >= 160;
  }

  // 图上面常常盖着别的东西（链接、透明的层），所以不只看事件落在谁身上，
  // 还看这个位置底下所有的东西。
  function pictureAt(event) {
    const seen = [...(event.composedPath ? event.composedPath() : []),
      ...document.elementsFromPoint(event.clientX, event.clientY)];
    for (const node of seen) if (usable(node)) return node;
    return null;
  }

  function light(img) {
    if (lit === img) return;
    if (lit) { lit.style.outline = lit.__mptOutline || ""; lit.style.cursor = ""; }
    lit = img;
    if (img) {
      img.__mptOutline = img.style.outline;
      img.style.outline = "5px solid #1a73e8";
      img.style.outlineOffset = "-5px";
      img.style.cursor = "copy";
    }
  }

  document.addEventListener("mousemove", (event) => {
    const img = pictureAt(event);
    light(img);
    if (img) say("Click to use this picture (" + img.naturalWidth + " × " + img.naturalHeight + ")");
    else say(HOW);
  }, true);

  document.addEventListener("click", (event) => {
    if (event.button !== 0) return;
    const img = pictureAt(event);
    if (!img) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    say("Sending this picture to the editor…");
    try {
      window.__BINDING__(JSON.stringify({
        src: img.currentSrc || img.src, page: location.href,
        width: img.naturalWidth, height: img.naturalHeight,
      }));
    } catch (error) {
      say("Could not send it: " + error, true);
    }
  }, true);

  const start = () => say(HOW);
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
"""


GOOGLE_IMAGES = "https://www.google.com/search?udm=2&q={words}"


def search_url(words: str) -> str:
    """搜这几个词的地址。默认是 Google 图片。

    ``photo_browser_search_url`` 可以换成别的图片搜索（地址里用 ``{words}``
    占位）：页面里那段脚本不认哪一家的页面结构，只认"够大的图"。写了就用
    写的那个，写错了就报错，不悄悄退回 Google。
    """
    template = str(config.app.get("photo_browser_search_url", "") or "").strip()
    if not template:
        template = GOOGLE_IMAGES
    elif "{words}" not in template or not template.lower().startswith(
        ("http://", "https://")
    ):
        raise PhotoBrowserError(
            "photo_browser_search_url in config.toml has to be a web address "
            "with {words} where the search words go"
        )
    return template.replace("{words}", urllib.parse.quote_plus(
        " ".join(str(words or "").split())
    ))


def find_browser() -> str:
    """这台电脑上的 Chrome 或 Edge。配置里写了就用配置里的，不猜别的。"""
    wanted = str(config.app.get("photo_browser_path", "") or "").strip()
    if wanted:
        if os.path.isfile(wanted):
            return wanted
        raise PhotoBrowserError(
            f"photo_browser_path in config.toml points at {wanted!r}, and there "
            "is no such file"
        )
    places = []
    roots = [os.environ.get(name) for name in
             ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    for tail in (("Google", "Chrome", "Application", "chrome.exe"),
                 ("Microsoft", "Edge", "Application", "msedge.exe")):
        places += [os.path.join(root, *tail) for root in roots if root]
    places += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for place in places:
        if os.path.isfile(place):
            return place
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise PhotoBrowserError(
        "the photo browser needs Chrome or Edge on the computer that runs this "
        "app, and neither was found; install one, or set photo_browser_path in "
        "config.toml to the browser's program file"
    )


def _open_local(url: str, timeout: float = 5.0) -> bytes:
    """读调试接口。它在本机上，不该绕去系统代理。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return response.read()


def _raise_window(pid: int) -> None:
    """把那个进程的窗口提到最前面（Windows）。

    Windows 不让后台进程抢焦点，所以浏览器自己的"到前面来"不管用：窗口开
    在了用户正看着的那个窗口后面。把它设成"总在最前"再取消，它就留在最
    上面一层了；焦点给不给由系统定，看得见就够了。别的系统上窗口本来就开
    在前面。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(handle, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(handle):
                found.append(handle)
            return True

        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.EnumWindows(each, 0)
        flags = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
        for handle in found:
            user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetWindowPos(handle, wintypes.HWND(-1), 0, 0, 0, 0, flags)
            user32.SetWindowPos(handle, wintypes.HWND(-2), 0, 0, 0, 0, flags)
            user32.SetForegroundWindow(handle)
    except Exception as exc:  # pragma: no cover - 只是锦上添花
        logger.debug(f"could not raise the photo browser window: {exc}")


class _Session:
    """一次"开窗口 → 用户点一张 → 关窗口"。"""

    def __init__(self, task_id: str, words: str,
                 on_pick: Callable[[bytes, dict], dict], min_edge: int):
        self.id = uuid.uuid4().hex
        self.task_id = str(task_id)
        self.words = words
        self.on_pick = on_pick
        self.min_edge = int(min_edge)
        self.state = "opening"
        self.error = ""
        self.result: dict = {}
        self.process: Optional[subprocess.Popen] = None
        self._socket: Any = None
        self._next = 0
        self._events: list = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 给外面看的 ----------------------------------------------------
    def describe(self) -> dict:
        return {
            "session": self.id, "state": self.state, "error": self.error,
            "words": self.words, **self.result,
        }

    def stop(self) -> None:
        self._stop.set()

    # -- 浏览器 --------------------------------------------------------
    def launch(self) -> None:
        program = find_browser()
        profile = utils.storage_dir(PROFILE_DIR, create=True)
        port_file = os.path.join(profile, "DevToolsActivePort")
        try:
            os.remove(port_file)
        except OSError:
            pass
        try:
            self.process = subprocess.Popen(
                [
                    program,
                    f"--user-data-dir={profile}",
                    # 0 = 让浏览器自己挑一个空着的端口，写进资料目录。
                    "--remote-debugging-port=0",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1040,840",
                    # 一个没有地址栏和标签页的小窗口。先开空白页：脚本要
                    # 赶在 Google 的页面之前放进去。
                    "--app=about:blank",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise PhotoBrowserError(
                f"could not start the browser at {program}: {exc}"
            ) from exc

        deadline = time.monotonic() + START_TIMEOUT
        port = 0
        while time.monotonic() < deadline and not port:
            try:
                with open(port_file, encoding="utf-8") as handle:
                    port = int((handle.readline() or "0").strip() or 0)
            except (OSError, ValueError):
                port = 0
            if not port:
                time.sleep(0.15)
        if not port:
            self._kill()
            raise PhotoBrowserError(
                "the browser started but never opened its control port; if a "
                "photo browser window from before is still open, close it and "
                "try again"
            )

        target = ""
        while time.monotonic() < deadline and not target:
            try:
                pages = json.loads(_open_local(f"http://127.0.0.1:{port}/json/list"))
            except Exception:
                pages = []
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    target = page["webSocketDebuggerUrl"]
                    break
            if not target:
                time.sleep(0.15)
        if not target:
            self._kill()
            raise PhotoBrowserError("the browser opened no window to search in")

        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - 环境问题
            self._kill()
            raise PhotoBrowserError(
                "the photo browser needs the 'websockets' Python package, "
                "which is not installed"
            ) from exc
        try:
            try:
                self._socket = connect(target, max_size=64 * 1024 * 1024, proxy=None)
            except TypeError:  # 老版本没有 proxy 这个参数
                self._socket = connect(target, max_size=64 * 1024 * 1024)
        except Exception as exc:
            self._kill()
            raise PhotoBrowserError(
                f"could not connect to the browser that was just opened: {exc}"
            ) from exc

        script = INJECTED.replace("__MIN__", str(self.min_edge)).replace(
            "__BINDING__", BINDING
        )
        self._call("Page.enable")
        self._call("Runtime.enable")
        self._call("Runtime.addBinding", {"name": BINDING})
        self._call("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        self._call("Page.navigate", {"url": search_url(self.words)})
        # 从后台进程开出来的窗口常常落在别的窗口后面。用户点了按钮却什么
        # 都没看见，那就等于没开。
        try:
            self._call("Page.bringToFront", timeout=5.0)
        except PhotoBrowserError:
            pass
        _raise_window(self.process.pid)
        self.state = "open"
        self._thread = threading.Thread(
            target=self._watch, name=f"photo-browser-{self.id[:8]}", daemon=True
        )
        self._thread.start()

    def _call(self, method: str, params: Optional[dict] = None,
              timeout: float = CALL_TIMEOUT) -> dict:
        """发一条命令，等它自己的那条回答。等的时候收到的事件先存着。"""
        self._next += 1
        wanted = self._next
        self._socket.send(json.dumps(
            {"id": wanted, "method": method, "params": params or {}}
        ))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PhotoBrowserError(f"the browser did not answer {method}")
            message = json.loads(self._socket.recv(timeout=left))
            if message.get("id") == wanted:
                if "error" in message:
                    raise PhotoBrowserError(
                        str(message["error"].get("message") or message["error"])
                    )
                return message.get("result") or {}
            if "method" in message:
                self._events.append(message)

    def _watch(self) -> None:
        try:
            while not self._stop.is_set():
                if self.process is not None and self.process.poll() is not None:
                    break
                if self._events:
                    message = self._events.pop(0)
                else:
                    try:
                        message = json.loads(self._socket.recv(timeout=0.5))
                    except TimeoutError:
                        continue
                if (message.get("method") == "Runtime.bindingCalled"
                        and (message.get("params") or {}).get("name") == BINDING):
                    if self._picked(message["params"].get("payload") or ""):
                        break
        except Exception as exc:
            # 用户把窗口关了，连接也就断了：那不是错。
            if not self._stop.is_set() and self.state == "open":
                logger.debug(f"photo browser connection ended: {exc}")
        finally:
            if self.state in ("opening", "open"):
                self.state = "closed"
            self._shut()

    def _picked(self, payload: str) -> bool:
        """用户点了一张。收得下就结束，收不下就在那个窗口里告诉他为什么，
        让他接着挑。"""
        try:
            chosen = json.loads(payload)
            raw = self._bytes_of(str(chosen.get("src") or ""))
            self.result = dict(self.on_pick(raw, chosen) or {})
        except Exception as exc:
            logger.info(f"photo browser: that picture was not usable: {exc}")
            self._say(f"That picture cannot be used: {exc}. Choose another.", True)
            return False
        self.state = "picked"
        self._say("Done. You can go back to the editor.", False)
        return True

    def _bytes_of(self, address: str) -> bytes:
        """点中的那张图的字节。从浏览器里取：它已经下载过了。"""
        if address.startswith("data:"):
            head, _, body = address.partition(",")
            if ";base64" in head:
                return base64.b64decode(body)
            return urllib.parse.unquote_to_bytes(body)
        if not address.lower().startswith(("http://", "https://")):
            raise PhotoBrowserError("it has no address the browser can read it from")
        frame = self._call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        try:
            got = self._call("Page.getResourceContent", {"frameId": frame, "url": address})
            body = got.get("content") or ""
            raw = base64.b64decode(body) if got.get("base64Encoded") else body.encode("utf-8")
            if raw:
                return raw
        except PhotoBrowserError:
            pass
        # 缓存里没有（比如图在一个内嵌的框里）：让浏览器再取一次。
        loaded = self._call("Network.loadNetworkResource", {
            "frameId": frame, "url": address,
            "options": {"disableCache": False, "includeCredentials": False},
        })["resource"]
        if not loaded.get("success") or not loaded.get("stream"):
            raise PhotoBrowserError("the browser could not read it from its site")
        parts = []
        size = 0
        try:
            while True:
                piece = self._call("IO.read", {"handle": loaded["stream"]})
                data = piece.get("data") or ""
                chunk = base64.b64decode(data) if piece.get("base64Encoded") else data.encode("utf-8")
                parts.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PhotoBrowserError("it is too large a file")
                if piece.get("eof"):
                    break
        finally:
            try:
                self._call("IO.close", {"handle": loaded["stream"]})
            except Exception:
                pass
        return b"".join(parts)

    def _say(self, text: str, bad: bool) -> None:
        try:
            self._call("Runtime.evaluate", {
                "expression": "window.__mptPhotoSay && window.__mptPhotoSay("
                              f"{json.dumps(text)}, {json.dumps(bool(bad))})",
            }, timeout=5.0)
        except Exception:
            pass

    def _shut(self) -> None:
        """关掉那个窗口。先好好说，不听再结束进程。"""
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.send(json.dumps({"id": 10 ** 9, "method": "Browser.close"}))
            except Exception:
                pass
            try:
                socket.close()
            except Exception:
                pass
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=3)
            except Exception:
                self._kill()

    def _kill(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass


_LOCK = threading.Lock()
_CURRENT: Optional[_Session] = None


def start(task_id: str, words: str, on_pick: Callable[[bytes, dict], dict],
          *, min_edge: int = 512) -> dict:
    """开窗口去搜 ``words``。同一时间只有一个：再开就把上一个关掉。

    ``on_pick(raw, chosen)`` 在用户点了一张之后被叫到；它抛出的错会显示在
    那个窗口里（"这张用不了，换一张"），返回的字典并进状态里。
    """
    global _CURRENT
    wanted = " ".join(str(words or "").split())
    if not wanted:
        raise PhotoBrowserError("there are no words to search for")
    with _LOCK:
        before, _CURRENT = _CURRENT, None
    if before is not None:
        # 看着它的那个线程自己会把窗口关掉；等它关完再开新的，两个窗口用
        # 的是同一份资料目录。
        before.stop()
        if before._thread is not None:
            before._thread.join(timeout=8)
        else:
            before._shut()
    session = _Session(task_id, wanted, on_pick, min_edge)
    session.launch()
    with _LOCK:
        _CURRENT = session
    logger.info(f"photo browser opened for {task_id}: {wanted!r}")
    return session.describe()


def status(task_id: str) -> dict:
    """那个窗口现在怎么样了。没有开过就是 ``none``。"""
    with _LOCK:
        session = _CURRENT
    if session is None or session.task_id != str(task_id):
        return {"state": "none", "error": "", "words": ""}
    return session.describe()


def close(task_id: str) -> dict:
    """关掉这条片子开的那个窗口（如果有）。"""
    global _CURRENT
    with _LOCK:
        session = _CURRENT
        if session is None or session.task_id != str(task_id):
            return {"state": "none", "error": "", "words": ""}
        _CURRENT = None
    session.stop()
    return {"state": "closed", "error": "", "words": session.words}
