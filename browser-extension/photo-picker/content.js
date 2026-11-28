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
  // A window opened to get footage: a bar on YouTube marks where the wanted
  // part of the video starts and ends, and sends the two times to the editor.
  if (to.kind === "footage") { footageBar(); return; }

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

  /* ---- footage: mark a part of the video that is playing ---- */
  function footageBar() {
    const marks = { video: "", start: null, end: null };
    let panel = null;
    let text = null;
    let busy = false;

    const clock = (seconds) => {
      const whole = Math.floor(seconds);
      const tenth = Math.floor((seconds - whole) * 10);
      return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0")
        + (tenth ? "." + tenth : "");
    };
    const player = () => document.querySelector("video.html5-main-video")
      || document.querySelector("video");
    const videoId = () => {
      const found = new URL(location.href).searchParams.get("v");
      if (found) return found;
      const parts = location.pathname.split("/").filter(Boolean);
      return ["shorts", "embed", "live"].includes(parts[0]) ? (parts[1] || "") : "";
    };

    function tell(message, bad) {
      if (!text) return;
      text.textContent = message;
      panel.style.background = bad ? "#b3261e" : "#1a57c9";
    }

    function describe() {
      if (!videoId() || !player()) {
        tell("Open a video and watch it. Mark the part you want here.");
        return;
      }
      if (marks.video !== videoId()) { marks.start = null; marks.end = null; }
      const from = marks.start == null ? "–" : clock(marks.start);
      const to = marks.end == null ? "–" : clock(marks.end);
      const length = marks.start != null && marks.end != null && marks.end > marks.start
        ? " (" + (marks.end - marks.start).toFixed(1) + " s)" : "";
      tell("Part to use: " + from + " to " + to + length);
    }

    function mark(which) {
      const video = player();
      if (!videoId() || !video) { describe(); return; }
      if (marks.video !== videoId()) {
        marks.video = videoId(); marks.start = null; marks.end = null;
      }
      marks[which] = Math.round(video.currentTime * 10) / 10;
      describe();
    }

    async function use() {
      if (busy) return;
      describe();
      if (marks.video !== videoId() || marks.start == null || marks.end == null) {
        tell("Mark where the part starts and where it ends first.", true);
        return;
      }
      if (marks.end <= marks.start) {
        tell("The end has to be after the start. Mark them again.", true);
        return;
      }
      busy = true;
      tell("Sending this part to the editor…");
      let answer = null;
      try {
        answer = await chrome.runtime.sendMessage({
          type: "footage",
          chosen: {
            url: "https://www.youtube.com/watch?v=" + marks.video,
            start: marks.start, end: marks.end,
          },
        });
      } catch (error) {
        answer = { error: String(error.message || error) };
      }
      if (answer && answer.ok) {
        tell("Done. Go back to the editor — it is fetching this part.");
        return;
      }
      busy = false;
      tell("That part cannot be used: " + ((answer && answer.error) || "no answer"), true);
    }

    function build() {
      if (panel || !document.body) return;
      panel = document.createElement("div");
      panel.style.cssText = "position:fixed;left:50%;bottom:14px;transform:translateX(-50%);"
        + "z-index:2147483647;display:flex;gap:8px;align-items:center;max-width:94vw;"
        + "padding:8px 12px;border-radius:14px;background:#1a57c9;color:#fff;"
        + "font:600 13px/1.3 Arial,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.5)";
      text = document.createElement("span");
      text.style.cssText = "margin-right:6px";
      panel.appendChild(text);
      for (const [label, act] of [
        ["Start here", () => mark("start")],
        ["End here", () => mark("end")],
        ["Use this part", use],
      ]) {
        const press = document.createElement("button");
        press.type = "button";
        press.textContent = label;
        press.style.cssText = "all:unset;cursor:pointer;padding:6px 12px;border-radius:9px;"
          + "background:#fff;color:#123a86;font:700 13px/1.2 Arial,sans-serif;white-space:nowrap";
        press.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          act();
        });
        panel.appendChild(press);
      }
      document.body.appendChild(panel);
      describe();
      // YouTube changes video without loading a new page.
      setInterval(() => { if (!busy) describe(); }, 1500);
    }

    if (document.body) build();
    else document.addEventListener("DOMContentLoaded", build);
  }
})();
