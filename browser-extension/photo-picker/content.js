/* Runs in every page, and does nothing in almost all of them.

   1. It leaves a mark on the page so the MoneyPrinterTurbo editor can tell the
      extension is installed.
   2. In a window the editor opened to pick a photo (its address carries
      "#mpt-pick=…"), every picture large enough to draw from is outlined under
      the mouse, and a click sends it to the editor. Small pictures — the
      search-result thumbnails — are left alone, so clicking one still opens it
      large, as Google does. */
(async () => {
  const MARK = "#mpt-pick=";
  try { document.documentElement.dataset.mptPhotoPicker = chrome.runtime.getManifest().version; }
  catch { /* no document yet */ }

  let to = null;
  try {
    if (location.hash.startsWith(MARK)) {
      const marker = JSON.parse(decodeURIComponent(location.hash.slice(MARK.length)));
      to = await chrome.runtime.sendMessage({ type: "arm", marker });
      try { history.replaceState(history.state, "", location.pathname + location.search); }
      catch { /* the address keeps its marker; harmless */ }
    } else {
      to = await chrome.runtime.sendMessage({ type: "state" });
    }
  } catch { return; }
  if (!to || to.error || window.top !== window) return;

  const MIN = Number(to.min) || 512;
  const HOW = "Click a picture to open it large, then click the large picture to use it.";
  let bar = null;
  let lit = null;
  let sending = false;

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

  function usable(img) {
    return img && img.tagName === "IMG"
      && Math.max(img.naturalWidth, img.naturalHeight) >= MIN
      && img.getBoundingClientRect().width >= 160;
  }

  // A picture is often covered by something else (a link, a transparent
  // layer), so look at everything under the pointer, not only the target.
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
    if (sending) return;
    const img = pictureAt(event);
    light(img);
    if (img) say("Click to use this picture (" + img.naturalWidth + " × " + img.naturalHeight + ")");
    else say(HOW);
  }, true);

  document.addEventListener("click", async (event) => {
    if (event.button !== 0) return;
    const img = pictureAt(event);
    if (!img) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (sending) return;
    sending = true;
    say("Sending this picture to the editor…");
    let answer = null;
    try {
      answer = await chrome.runtime.sendMessage({
        type: "pick",
        chosen: {
          src: img.currentSrc || img.src, page: location.href,
          width: img.naturalWidth, height: img.naturalHeight,
        },
      });
    } catch (error) {
      answer = { error: String(error.message || error) };
    }
    if (answer && answer.ok) {
      say("Done. Go back to the editor.");
      return;
    }
    sending = false;
    say("That picture cannot be used: " + ((answer && answer.error) || "no answer")
      + ". Choose another.", true);
  }, true);

  const start = () => say(HOW);
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
