/* 时间线编辑器。没有框架、没有构建步骤：拖拽就是 pointer 事件加一点算术，
   预览就是一个 <video>，为此引入 React/Vite 工具链不值得。
   A dependency-free timeline: drag is pointer maths and preview is a <video>.

   权责划分：这个页面只描述"剪成什么样",渲染始终由 Python 那条既有管线做。
   所以它从不自己拼接画面,只提交入点/出点/顺序,然后让 /render 出片。
   This page only describes the edit; the existing Python pipeline renders it. */

"use strict";

const API = "/api/v1";
const MIN_DURATION = 0.2;

const el = (id) => document.getElementById(id);
const ui = {
  task: el("task"), load: el("load"), totals: el("totals"),
  rerecord: el("rerecord"), render: el("render"), banner: el("banner"),
  preview: el("preview"), placeholder: el("placeholder"), play: el("play"),
  clock: el("clock"), zoom: el("zoom"), inspTitle: el("insp-title"),
  inspBody: el("insp-body"), ruler: el("ruler"), videoTrack: el("video-track"),
  audioTrack: el("audio-track"), playhead: el("playhead"), narration: el("narration"),
  frame: el("frame"), fx: el("fx"), settings: el("settings"), scroll: el("scroll"),
  fit: el("fit"), gap: el("gap"), screen: document.querySelector(".screen"),
};

const state = {
  taskId: "",
  data: null,
  scenes: [],
  speed: 1,
  settings: {},
  adding: false,
  taskBusy: false,
  watching: false,
  wasBusy: false,
  selected: -1,
  pxPerSecond: 80,
  playhead: 0,
  playing: false,
  busy: false,
  loadedClip: -1,
};

/** 宽 / 高。与 VideoAspect.to_resolution() 的三种比例一致。 */
const ASPECT_NUMBER = { "16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1 };

/** 是不是被嵌在 WebUI 里。

    嵌着的时候要把"重新配音"和"渲染成片"交给外面的 Streamlit 按钮，自己
    藏起来。原因不是重复难看，而是两者并不等价：编辑器这两个按钮提交到
    API 进程，而 ``enable_redis`` 关闭时任务状态是进程内的，WebUI 根本看不到
    那次渲染——既没有进度，渲染完也不会在页面上出现成片。外面那两个按钮
    走 WebUI 自己的队列，这两件事都正常。 */
const EMBEDDED = (() => {
  try { return window.self !== window.top; } catch { return true; }
})();

/** 渲染器给每个转场的时长是 1 秒；这里只是给预览一个近似的视觉提示。 */
const TRANSITION_CLASS = {
  FadeIn: "fade", FadeOut: "fade", Shuffle: "fade",
  SlideIn: "slide", SlideOut: "slide",
  ZoomIn: "zoom", ZoomOut: "zoom",
};

/* ---------- helpers ---------- */

/** 这一段在成片里停留多久。没设过 duration 就是"素材剩下的全长"。 */
function screenDuration(scene) {
  if (scene.duration != null) return Math.max(MIN_DURATION, scene.duration);
  const source = scene.source_duration;
  if (source == null) return 5; // 探测不到时给个占位宽度，免得整条轨道塌掉
  return Math.max(MIN_DURATION, (source - (scene.trim_start || 0)) / state.speed);
}

function timelineTotal() {
  return state.scenes.reduce((sum, scene) => sum + screenDuration(scene), 0);
}

/** 这条片子真正的长度：画面和旁白里更长的那个。

    画面比旁白短是常态(素材张数由关键词个数决定)。如果播放和标尺都只到
    画面结束就截止,用户既听不到后面那段旁白,也看不出到底缺了多少——而
    "缺多少"正是他要判断的事。所以时间轴按两者里更长的那个铺开,画面用完
    之后继续走,只是没有画面。 */
function narrationLength() {
  return (state.data && state.data.audio_duration) || 0;
}

function projectTotal() {
  return Math.max(timelineTotal(), narrationLength());
}

/** 每一段的起点（成片时间轴上的秒数）。 */
function sceneOffsets() {
  const offsets = [];
  let at = 0;
  for (const scene of state.scenes) {
    offsets.push(at);
    at += screenDuration(scene);
  }
  return offsets;
}

function fmt(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function say(message, kind) {
  ui.banner.hidden = !message;
  ui.banner.textContent = message || "";
  ui.banner.className = "banner" + (kind ? " " + kind : "");
}

/** API key 存在 localStorage，只有管理员配置过 api_key 的部署才需要。
    注意 <video src> 没法带请求头，所以配了 key 的部署里预览画面会取不到。 */
function apiKey() {
  try { return localStorage.getItem("mpt_api_key") || ""; } catch { return ""; }
}

async function call(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const key = apiKey();
  if (key) headers["x-api-key"] = key;
  const response = await fetch(API + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try { payload = await response.json(); } catch { /* 空响应体 */ }
  if (!response.ok) {
    // 服务端的错误文案是点名的（哪个源失败、越界多少），照原样显示比
    // 翻译成"操作失败"有用得多。
    const detail = (payload && (payload.message || payload.detail)) || response.statusText;
    throw new Error(detail);
  }
  return payload ? payload.data : null;
}

/* ---------- loading ---------- */

async function load(taskId) {
  if (!taskId) return;
  state.taskId = taskId.trim();
  say("Loading…");
  try {
    const data = await call("GET", `/tasks/${encodeURIComponent(state.taskId)}/timeline`);
    state.data = data;
    state.scenes = data.scenes || [];
    state.speed = data.clip_speed || 1;
    state.settings = data.render_settings || {};
    state.selected = state.scenes.length ? 0 : -1;
    state.playhead = 0;
    state.loadedClip = -1;
    ui.narration.src = data.audio_url || "";
    applyAspect();
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("task", state.taskId);
      history.replaceState(null, "", url);
    } catch { /* 地址栏同步失败无关紧要 */ }
    say(data.audio_stale
      ? "The narration no longer matches the edited script. Re-record before rendering."
      : "", data.audio_stale ? "warn" : "");
    draw();
    // 打开时就查一次：别人(或刚才的自己)可能正在渲染这条任务。
    watchTask();
  } catch (error) {
    state.data = null;
    state.scenes = [];
    draw();
    say(String(error.message || error), "error");
  }
}

async function refresh() {
  if (!state.taskId) return;
  const keep = state.selected;
  const data = await call("GET", `/tasks/${encodeURIComponent(state.taskId)}/timeline`);
  state.data = data;
  state.scenes = data.scenes || [];
  state.speed = data.clip_speed || 1;
  state.settings = data.render_settings || {};
  state.selected = Math.min(keep, state.scenes.length - 1);
  state.loadedClip = -1;
  applyAspect();
  if (data.audio_stale) {
    say("The narration no longer matches the edited script. Re-record before rendering.", "warn");
  } else {
    say("");
  }
  draw();
}

/** 盯着任务状态，直到它不再运行。

    不盯的话,用户点了"渲染成片"之后界面毫无变化,接着去改某一段就会收到
    一句"任务还在运行"——而他根本不知道有东西在跑。渲染期间确实不能改:
    渲染器正在读那几个素材文件,边读边换会得到一条谁也复现不了的片子。
    所以这里把那个状态显示出来,而不是等用户撞上去才告诉他。 */
async function watchTask() {
  if (!state.taskId || state.watching) return;
  state.watching = true;
  try {
    for (;;) {
      let task;
      try {
        task = await call("GET", `/tasks/${encodeURIComponent(state.taskId)}`);
      } catch {
        break; // 查不到状态不该让编辑器停摆
      }
      // 4 = processing（见 app/models/const.py）
      const busy = task && Number(task.state) === 4;
      state.taskBusy = !!busy;
      if (!busy) {
        if (state.wasBusy) {
          state.wasBusy = false;
          say("The render finished. Reloading the scenes.");
          await refresh();
        }
        break;
      }
      state.wasBusy = true;
      const pct = Number(task.progress || 0);
      say(`This task is rendering (${pct}%). Scene edits are locked until it finishes.`,
        "warn");
      setControlsEnabled(false);
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  } finally {
    state.watching = false;
    state.taskBusy = false;
    setControlsEnabled(true);
  }
}

/** 渲染期间把会改数据的控件禁掉，省得用户点了才发现不让改。 */
function setControlsEnabled(enabled) {
  for (const node of ui.inspBody.querySelectorAll("button, input, textarea")) {
    node.disabled = !enabled;
  }
  ui.render.disabled = !enabled
    || !state.scenes.length
    || !!(state.data && state.data.audio_stale);
  ui.fit.disabled = !enabled;
}

/** 所有改数据的操作都走这里：禁用按钮、显示真实错误、完成后重读一次。

    操作返回字符串时就把它当作完成后要显示的话。``refresh`` 会把提示清空,
    所以这句必须在重读之后再说。 */
async function run(label, operation) {
  if (state.busy) return;
  state.busy = true;
  say(label + "…");
  try {
    const note = await operation();
    await refresh();
    // 只有字符串才是"要显示的话"。多数操作直接返回接口载荷(一个对象)，
    // 不判类型就会把 "[object Object]" 糊到提示条上。
    if (typeof note === "string" && note) say(note, "warn");
  } catch (error) {
    say(String(error.message || error), "error");
  } finally {
    state.busy = false;
  }
}

/** 时长设置的共用提交路径：被素材长度收住时要说出来，不能悄悄改掉输入。 */
async function applyTiming(index, body) {
  const scene = await call("PUT", scenePath(index) + "/timing", body);
  if (scene && scene.clamped) {
    return `Scene ${index + 1} was pulled back to the length of its material`
      + (scene.duration != null ? ` (${scene.duration.toFixed(2)}s).` : ".");
  }
  return "";
}

/* ---------- drawing ---------- */

/** 画面框按成片宽高比留出，并复刻渲染器的适配方式。

    竖版素材正是"图片溢出画面区"的来源：框先定下比例，视频再用
    object-fit 填进去，就不存在靠百分比高度去约束一个 flex 子元素的问题。 */
function applyAspect() {
  // 渲染器的 cover 会裁掉多余边缘，contain 会留黑边。预览照搬，
  // 这样这里看到的构图就是成片的构图。
  ui.preview.style.objectFit = state.settings.fit === "cover" ? "cover" : "contain";
  fitFrame();
}

/** 按成片比例，算出画面框在当前可视区里能占到的最大尺寸。

    尺寸直接写成像素而不是交给 CSS 自动布局：框里的 <video> 是绝对定位的，
    对布局没有尺寸贡献，任何依赖"内容撑开"的写法都会把框压成 0——这正是
    改版后画面整个看不见的原因。 */
function fitFrame() {
  const ratio = ASPECT_NUMBER[state.settings.aspect] || 9 / 16;
  const box = ui.screen.getBoundingClientRect();

  // 量到一个退化的尺寸时什么都不做。标签页在后台、或者布局还没跑完时,
  // getBoundingClientRect 会返回 0；此时若按下限写死,画面框会被永久钉在
  // 那个最小值上(实测会变成 22x40),而且之后没有任何东西会再去量它。
  // 宁可这一帧不画,也不要把一个错的尺寸固化下来。
  if (box.width < 50 || box.height < 50) return;

  // .screen 有 10px 内边距，两边各留出来。
  const availableWidth = box.width - 20;
  const availableHeight = box.height - 20;

  let width = availableHeight * ratio;
  let height = availableHeight;
  if (width > availableWidth) {
    width = availableWidth;
    height = availableWidth / ratio;
  }
  ui.frame.style.width = Math.floor(width) + "px";
  ui.frame.style.height = Math.floor(height) + "px";
}

/** 把这次生成的渲染设置摆出来。

    "看不到画面在动"和"看不到转场"最常见的原因不是编辑器没显示，而是这次
    生成压根没配：image_motion=static 的素材在生成时就烘成了静止片段，
    transition=None 则是渲染阶段不加转场。所以这两项要明确标出来。 */
function drawSettings() {
  const s = state.settings;
  if (!s || !Object.keys(s).length) { ui.settings.innerHTML = ""; return; }
  ui.settings.innerHTML = "";

  const chips = [];
  if (s.aspect) chips.push([s.aspect, false]);
  if (state.speed !== 1) chips.push([`${state.speed}x speed`, false]);

  const motion = String(s.image_motion || "");
  if (motion) {
    const off = motion === "static";
    chips.push([off ? "no image motion (static)" : "image motion: zoom", off]);
  }

  const transition = String(s.transition || "");
  const off = !transition || transition === "None";
  chips.push([off ? "no transitions" : `transition: ${transition}`, off]);

  for (const [label, isOff] of chips) {
    const chip = document.createElement("span");
    chip.className = "chip" + (isOff ? " off" : "");
    chip.textContent = label;
    chips.length && ui.settings.appendChild(chip);
    if (isOff) {
      chip.title = label.indexOf("motion") >= 0
        ? "Clips were baked without the zoom at generation time, so there is no "
          + "motion to preview. Set image_motion = \"zoom\" and regenerate."
        : "No transition is configured for this task, so none is applied at render.";
    } else if (label.indexOf("transition") === 0) {
      chip.title = "Transitions are applied per clip at render time and are not "
        + "baked into the source clips, so the preview only approximates them.";
    }
  }
}

function draw() {
  drawRuler();
  drawClips();
  drawVoice();
  drawInspector();
  drawTotals();
  drawSettings();
  const ready = state.scenes.length > 0;
  ui.play.disabled = !ready;
  ui.render.disabled = !ready || !!(state.data && state.data.audio_stale);
  ui.rerecord.disabled = !state.data || !state.data.audio_stale;
  ui.placeholder.hidden = ready;
  ui.playhead.hidden = !ready;
  ui.frame.classList.toggle("ready", ready);
  // 框显示出来之后再量一次：嵌在 iframe 里时，首次布局可能还没稳定。
  if (ready) { fitFrame(); syncPreview(true); }
  movePlayhead();
}

function drawRuler() {
  const total = Math.max(projectTotal(), 1);
  // 轨道比可视区窄时也撑满，否则刻度后面会露出一条空背景。
  const width = Math.max(total * state.pxPerSecond, ui.scroll.clientWidth);
  for (const node of [ui.ruler, ui.videoTrack, ui.audioTrack]) {
    node.style.width = width + "px";
  }
  ui.ruler.innerHTML = "";
  // 刻度密度跟着缩放走，免得放大后挤成一片黑。
  const step = state.pxPerSecond < 40 ? 10 : state.pxPerSecond < 100 ? 5 : 1;
  for (let second = 0; second <= Math.ceil(total); second += step) {
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = second * state.pxPerSecond + "px";
    tick.textContent = fmt(second);
    ui.ruler.appendChild(tick);
  }
}

function drawClips() {
  const offsets = sceneOffsets();
  ui.videoTrack.innerHTML = "";

  state.scenes.forEach((scene, index) => {
    const span = screenDuration(scene);
    const clip = document.createElement("div");
    clip.className = "clip"
      + (index === state.selected ? " selected" : "")
      + (scene.locked ? " locked" : "")
      + (scene.exists ? "" : " missing");
    clip.style.left = offsets[index] * state.pxPerSecond + "px";
    clip.style.width = Math.max(14, span * state.pxPerSecond - 2) + "px";
    clip.dataset.index = String(index);

    if (scene.exists) {
      const thumb = document.createElement("img");
      thumb.className = "clip-thumb";
      clip.appendChild(thumb);
      requestThumb(scene, thumb);
    }

    const text = document.createElement("div");
    text.className = "clip-text";
    text.textContent = `${index + 1}. ${scene.keyword || scene.material || ""}`
      + ` · ${span.toFixed(1)}s`
      + (scene.exists ? "" : " · missing");
    clip.appendChild(text);

    for (const side of ["left", "right"]) {
      const handle = document.createElement("div");
      handle.className = "handle " + side;
      handle.dataset.side = side;
      handle.dataset.index = String(index);
      handle.addEventListener("pointerdown", startTrim);
      clip.appendChild(handle);
    }

    clip.addEventListener("pointerdown", startDrag);
    ui.videoTrack.appendChild(clip);
  });

  drawGap(offsets);
  drawAddTile(offsets);
}

/** 画面用完、旁白还在的那一段，在轨道上画出来。

    这是"还缺多少画面"最直接的表达：一条看得见、量得出的空档，比标题栏里
    一个 "-40.0s" 的数字清楚得多。 */
function drawGap(offsets) {
  const covered = state.scenes.length
    ? offsets[offsets.length - 1] + screenDuration(state.scenes[state.scenes.length - 1])
    : 0;
  const narration = narrationLength();
  if (narration - covered <= 0.5) return;

  const gap = document.createElement("div");
  gap.className = "gap-block";
  gap.style.left = covered * state.pxPerSecond + "px";
  gap.style.width = (narration - covered) * state.pxPerSecond + "px";
  gap.textContent = `no picture · ${(narration - covered).toFixed(1)}s`;
  gap.title = "The narration keeps going here but there are no pictures left. "
    + "Add scenes with +, or use Fit to narration to hold the existing ones longer.";
  ui.videoTrack.appendChild(gap);
}

/** 时间线末尾的加号。

    放在最后一段画面右边,是因为"再补一张"本来就是沿着时间线往后做的事;
    藏进检视面板里就得先选中某一段才找得到它。 */
function drawAddTile(offsets) {
  const end = state.scenes.length
    ? offsets[offsets.length - 1] + screenDuration(state.scenes[state.scenes.length - 1])
    : 0;
  const tile = document.createElement("div");
  tile.className = "clip add" + (state.adding ? " selected" : "");
  tile.style.left = end * state.pxPerSecond + 2 + "px";
  tile.style.width = "44px";
  tile.title = "Generate an extra picture and add it here";
  tile.textContent = "+";
  tile.addEventListener("click", () => {
    state.adding = true;
    state.selected = -1;
    drawInspector();
    markSelection();
  });
  ui.videoTrack.appendChild(tile);
  // 轨道要留得下这个加号，否则它会被裁在滚动区外面。
  const needed = (end * state.pxPerSecond) + 60;
  if (needed > parseFloat(ui.videoTrack.style.width || "0")) {
    for (const node of [ui.ruler, ui.videoTrack, ui.audioTrack]) {
      node.style.width = needed + "px";
    }
  }
}

/* ---------- thumbnails ---------- */

/* 每段画面抓一帧存成静态图。

   以前这里每段挂一个 <video>。13 段就是 13 个解码器同时启动，页面会直接
   失去响应(实测渲染进程连 30 秒的截图请求都答不上来)；而且每次重绘轨道
   都会把它们全部重挂一遍。服务端不是瓶颈——13 个完整片段并发取回只要
   0.18 秒——瓶颈是浏览器里同时活着的解码器数量。

   所以改成：用**一个**离屏 <video> 按队列逐个解码，抓到的帧画进 canvas
   转成静态图，解码器随即释放。抓过的帧按 (地址, 时间点) 缓存，后续重绘
   直接命中，不会再解一次。 */
const thumbCache = new Map();
const thumbQueue = [];
let thumbWorking = false;

function requestThumb(scene, img) {
  const at = Number(scene.trim_start || 0);
  const key = `${scene.url}@${at.toFixed(2)}`;
  const cached = thumbCache.get(key);
  if (cached) { img.src = cached; return; }
  thumbQueue.push({ key, url: scene.url, at, img });
  if (!thumbWorking) drainThumbs();
}

async function drainThumbs() {
  thumbWorking = true;
  try {
    while (thumbQueue.length) {
      const job = thumbQueue.shift();
      const cached = thumbCache.get(job.key);
      if (cached) { job.img.src = cached; continue; }
      try {
        const dataUrl = await grabFrame(job.url, job.at);
        thumbCache.set(job.key, dataUrl);
        // 轨道可能已经重绘过，这个 img 未必还在页面上，赋值无害。
        job.img.src = dataUrl;
      } catch {
        // 抓不到缩略图不影响剪辑，这一格留空即可。
      }
    }
  } finally {
    thumbWorking = false;
  }
}

function grabFrame(url, at) {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    video.crossOrigin = "anonymous";
    const done = (fn, arg) => {
      video.removeAttribute("src");
      video.load();
      fn(arg);
    };
    const timer = setTimeout(() => done(reject, new Error("thumbnail timeout")), 8000);
    video.addEventListener("error", () => {
      clearTimeout(timer);
      done(reject, new Error("thumbnail decode failed"));
    }, { once: true });
    video.addEventListener("seeked", () => {
      clearTimeout(timer);
      try {
        const canvas = document.createElement("canvas");
        // 轨道上只有几十像素高，按这个尺寸存就够了，也省内存。
        canvas.height = 72;
        canvas.width = Math.max(
          8, Math.round(72 * (video.videoWidth / video.videoHeight || 0.5625))
        );
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        done(resolve, canvas.toDataURL("image/jpeg", 0.6));
      } catch (error) {
        done(reject, error);
      }
    }, { once: true });
    video.addEventListener("loadedmetadata", () => {
      // 入点正好落在最后一帧时 seek 可能不会触发，往回收一点。
      video.currentTime = Math.min(at, Math.max(0, (video.duration || 0) - 0.05));
    }, { once: true });
    video.src = url;
  });
}

/** 旁白轨。

    配音是逐句合成的，所以这里按句画，而不是画成一整条。一整条看不出
    哪句话落在哪一段画面上——而"这句话配的是哪张图"正是审片时要判断的事。
    句子的时间点来自任务的字幕文件；没有字幕时退回整条显示。 */
function drawVoice() {
  ui.audioTrack.innerHTML = "";
  const narration = narrationLength();
  if (!narration) return;

  const segments = (state.data && state.data.narration_segments) || [];
  if (!segments.length) {
    const bar = document.createElement("div");
    bar.className = "voice-bar";
    bar.style.left = "0px";
    bar.style.width = Math.max(14, narration * state.pxPerSecond - 2) + "px";
    bar.textContent = `narration · ${narration.toFixed(1)}s`;
    bar.title = "No subtitle file for this task, so the narration cannot be "
      + "split into lines here.";
    ui.audioTrack.appendChild(bar);
    return;
  }

  segments.forEach((segment, index) => {
    const bar = document.createElement("div");
    bar.className = "voice-bar line";
    bar.style.left = segment.start * state.pxPerSecond + "px";
    bar.style.width = Math.max(
      3, (segment.end - segment.start) * state.pxPerSecond - 2
    ) + "px";
    bar.textContent = segment.text;
    bar.title = `${index + 1}. ${segment.text}\n`
      + `${segment.start.toFixed(2)}s – ${segment.end.toFixed(2)}s `
      + `(${(segment.end - segment.start).toFixed(2)}s)`;
    // 点一句就把播放头移到那句开头，方便直接听这一句配的是哪张图。
    bar.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      state.playhead = segment.start;
      state.loadedClip = -1;
      syncPreview(true);
      movePlayhead();
    });
    ui.audioTrack.appendChild(bar);
  });
}

function drawTotals() {
  if (!state.scenes.length) {
    ui.totals.textContent = "";
    ui.fit.hidden = true;
    return;
  }
  const total = timelineTotal();
  // 这里比的是"画面总长 vs 旁白长度"，所以用 timelineTotal 而不是
  // projectTotal——后者已经把两者取过最大值，比出来永远是 0。
  const narration = narrationLength();
  let label = `${state.scenes.length} clips · ${total.toFixed(1)}s`;
  let drift = false;
  if (narration) {
    const gap = total - narration;
    label += ` · narration ${narration.toFixed(1)}s`;
    if (Math.abs(gap) > 0.5) {
      drift = true;
      label += gap > 0 ? ` (+${gap.toFixed(1)}s silent tail)` : ` (${gap.toFixed(1)}s, voice cut off)`;
    }
  }
  ui.totals.textContent = label;
  ui.totals.className = "totals" + (drift ? " drift" : "");

  // 对不齐的时候才给这个按钮：素材张数由关键词个数决定，画面比旁白短是
  // 常态，而这是"同样的画面每段停更久"那个答案。
  ui.fit.hidden = !drift || !narration;
  ui.fit.title = `Stretch these ${state.scenes.length} clips to cover the `
    + `${narration.toFixed(1)}s narration (about `
    + `${(narration / state.scenes.length).toFixed(1)}s each). `
    + "Generated images are re-rendered at the new length.";
}

/* ---------- inspector ---------- */

function drawInspector() {
  ui.inspBody.innerHTML = "";
  if (state.adding) { drawAddForm(); return; }

  const scene = state.scenes[state.selected];
  if (!scene) {
    ui.inspTitle.textContent = "Nothing selected";
    return;
  }
  ui.inspTitle.textContent = `Scene ${scene.index + 1}${scene.locked ? " · locked" : ""}`;

  const keyword = field("Keyword", "input", scene.keyword || "");
  ui.inspBody.append(keyword.wrap);

  // 生成新画面是这个面板里最常用的动作：画面不对时要改的通常就是关键词，
  // 改完紧接着就要重出一张。所以它紧贴在关键词下面，而不是和锁定、删除
  // 挤在最底下那一排。
  // 本机出一张图可能要几分钟（实测 zimage-turbo 约 3 分钟）。按钮必须禁用
  // 并且**显示已经等了多久**：一个几分钟不变的"生成中…"和卡死没有区别，
  // 用户只会以为它挂了然后反复点。
  const regenerate = button(regenerateLabel(scene), "primary", async () => {
    regenerate.disabled = true;
    const started = Date.now();
    const tick = setInterval(() => {
      regenerate.textContent = `Generating… ${fmt((Date.now() - started) / 1000)}`;
    }, 1000);
    regenerate.textContent = "Generating… 0:00";
    try {
      await run("Generating a new picture for this scene", () =>
        call("POST", scenePath(scene.index) + "/material",
          { keyword: keyword.input.value || null }));
    } finally {
      clearInterval(tick);
      regenerate.disabled = false;
      regenerate.textContent = regenerateLabel(scene);
    }
  });
  ui.inspBody.appendChild(regenerate);

  const sourceNote = document.createElement("p");
  sourceNote.className = "hint";
  sourceNote.textContent = isGenerated(scene)
    ? "Draws a new picture from the keyword. Edit the keyword first if the "
      + "subject itself is wrong — that is usually what fixes it. A local "
      + "image server can take a few minutes per picture."
    : "Stock searches are cached, so the same keyword returns the same clip. "
      + "Change the keyword to get a different one.";
  ui.inspBody.appendChild(sourceNote);

  const narration = field("Narration", "textarea", scene.narration || "");
  ui.inspBody.append(narration.wrap);

  const save = button("Save text", "small", () => run("Saving", () =>
    call("PUT", scenePath(scene.index), {
      keyword: keyword.input.value,
      narration: narration.input.value,
    })));
  ui.inspBody.appendChild(save);

  const source = scene.source_duration;
  const inPoint = field("In-point (s)", "input", String(scene.trim_start ?? 0), "number");
  const hold = field("Duration on screen (s)", "input",
    scene.duration != null ? String(scene.duration) : "", "number");
  hold.input.placeholder = "auto";
  ui.inspBody.append(inPoint.wrap, hold.wrap);

  const timingRow = document.createElement("div");
  timingRow.className = "row";
  timingRow.append(
    button("Apply timing", "small", () => run("Applying timing", () =>
      applyTiming(scene.index, {
        trim_start: Number(inPoint.input.value) || 0,
        duration: hold.input.value === "" ? null : Number(hold.input.value),
      }))),
    button("Auto", "small", () => run("Resetting timing", () =>
      call("PUT", scenePath(scene.index) + "/timing", { reset: true }))),
  );
  ui.inspBody.appendChild(timingRow);

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = source != null
    ? `Source material is ${source.toFixed(2)}s long.`
      + (scene.kind === "image" ? " Images are re-rendered when held longer." : "")
    : "Source length unknown.";
  ui.inspBody.appendChild(note);

  if (state.speed !== 1) {
    const speedNote = document.createElement("p");
    speedNote.className = "hint warn";
    speedNote.textContent = `Clip speed is ${state.speed}x, so 1s on screen uses `
      + `${state.speed.toFixed(2)}s of source.`;
    ui.inspBody.appendChild(speedNote);
  }

  const actions = document.createElement("div");
  actions.className = "row";
  actions.append(
    button(scene.locked ? "Unlock" : "Lock", "small", () => run("Saving", () =>
      call("PUT", scenePath(scene.index), { locked: !scene.locked }))),
  );
  ui.inspBody.appendChild(actions);

  ui.inspBody.appendChild(
    button("Delete scene", "small danger", () => run("Deleting", () =>
      call("DELETE", scenePath(scene.index)))));
}

/** 新增一段画面的表单。

    用面板里的一个表单而不是 window.prompt：浏览器的模态框会挡住整页，
    嵌在 WebUI 的 iframe 里尤其糟糕。 */
function drawAddForm() {
  ui.inspTitle.textContent = `Add scene ${state.scenes.length + 1}`;

  const keyword = field("Keyword for the new picture", "input", "");
  keyword.input.placeholder = "a hand dropping a coin into a jar";
  ui.inspBody.appendChild(keyword.wrap);

  const create = button("Generate and add", "primary", async () => {
    const term = keyword.input.value.trim();
    if (!term) { say("Enter a keyword for the new picture first.", "warn"); return; }
    create.disabled = true;
    const started = Date.now();
    const tick = setInterval(() => {
      create.textContent = `Generating… ${fmt((Date.now() - started) / 1000)}`;
    }, 1000);
    create.textContent = "Generating… 0:00";
    try {
      await run("Generating an extra picture", async () => {
        await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/scenes`,
          { keyword: term });
        state.adding = false;
        // 新的一段在末尾，选中它，这样生成完就能直接接着改。
        state.selected = state.scenes.length;
      });
    } finally {
      clearInterval(tick);
      create.disabled = false;
      create.textContent = "Generate and add";
    }
  });
  ui.inspBody.appendChild(create);

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = "Adds a picture at the end of the timeline. Its narration "
    + "is left empty, so the recorded voice stays valid.";
  ui.inspBody.appendChild(note);

  ui.inspBody.appendChild(button("Cancel", "small", () => {
    state.adding = false;
    state.selected = state.scenes.length ? 0 : -1;
    drawInspector();
    markSelection();
  }));
}

function scenePath(index) {
  return `/tasks/${encodeURIComponent(state.taskId)}/scenes/${index}`;
}

/** 这一段的画面是生成出来的，还是从素材库取回来的。

    两者的重出行为完全不同：生成源每次都会画一张新的，而库存源的搜索带
    缓存,同一个关键词拿回来的还是同一段。按钮文案必须讲清这件事,否则
    用户会反复点一个"什么都没变"的按钮。 */
const GENERATED_SOURCES = new Set([
  "openai_image", "codex_image", "infistar", "loomloom", "jalapeno", "muapi",
]);

function isGenerated(scene) {
  return GENERATED_SOURCES.has(String(scene.source || ""));
}

function regenerateLabel(scene) {
  return isGenerated(scene) ? "Generate new image" : "Find another clip";
}

function field(label, tag, value, type) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const caption = document.createElement("label");
  caption.textContent = label;
  const input = document.createElement(tag);
  if (type) input.type = type;
  if (type === "number") { input.step = "0.1"; input.min = "0"; }
  input.value = value;
  wrap.append(caption, input);
  return { wrap, input };
}

function button(label, extra, onClick) {
  const node = document.createElement("button");
  node.className = "btn " + (extra || "");
  node.textContent = label;
  node.addEventListener("click", onClick);
  return node;
}

/* ---------- drag to reorder ---------- */

/** 只切换选中样式，不重建轨道。

    ``drawClips`` 会把每一段的缩略图 <video> 全部重新挂载一遍。选中另一段
    就重挂十几个解码器,页面会直接卡住(实测 13 段时渲染进程失去响应)。
    选中状态只是个 class,改 class 就够了。 */
function markSelection() {
  for (const node of ui.videoTrack.querySelectorAll(".clip[data-index]")) {
    node.classList.toggle("selected", Number(node.dataset.index) === state.selected);
  }
  const add = ui.videoTrack.querySelector(".clip.add");
  if (add) add.classList.toggle("selected", !!state.adding);
}

function startDrag(event) {
  if (event.target.classList.contains("handle")) return;
  const index = Number(event.currentTarget.dataset.index);
  state.selected = index;
  state.adding = false;
  drawInspector();
  markSelection();

  const clip = ui.videoTrack.querySelector(`.clip[data-index="${index}"]`);
  if (!clip) return;
  clip.classList.add("dragging");
  const marker = document.createElement("div");
  marker.className = "drop-marker";
  ui.videoTrack.appendChild(marker);

  const startX = event.clientX;
  let target = index;
  let moved = false;

  const onMove = (move) => {
    if (Math.abs(move.clientX - startX) > 3) moved = true;
    const rect = ui.videoTrack.getBoundingClientRect();
    const at = (move.clientX - rect.left) / state.pxPerSecond;
    const offsets = sceneOffsets();
    // 落点取"指针越过了哪些片段的中点"，这比按边界判断更贴手感。
    target = state.scenes.length - 1;
    for (let i = 0; i < state.scenes.length; i += 1) {
      const middle = offsets[i] + screenDuration(state.scenes[i]) / 2;
      if (at < middle) { target = i; break; }
    }
    marker.style.left = (offsets[target] || 0) * state.pxPerSecond + "px";
  };

  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    clip.classList.remove("dragging");
    marker.remove();
    if (!moved || target === index) return;
    const order = state.scenes.map((_, i) => i);
    order.splice(target, 0, order.splice(index, 1)[0]);
    state.selected = target;
    run("Reordering", () =>
      call("POST", `/tasks/${encodeURIComponent(state.taskId)}/scenes/reorder`, { order }));
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

/* ---------- trim handles ---------- */

function startTrim(event) {
  event.stopPropagation();
  const index = Number(event.currentTarget.dataset.index);
  const side = event.currentTarget.dataset.side;
  const scene = state.scenes[index];
  if (!scene) return;

  state.selected = index;
  const startX = event.clientX;
  const startTrimStart = scene.trim_start || 0;
  const startSpan = screenDuration(scene);
  const source = scene.source_duration;
  const clip = ui.videoTrack.querySelector(`.clip[data-index="${index}"]`);
  let nextTrimStart = startTrimStart;
  let nextSpan = startSpan;

  const onMove = (move) => {
    const deltaSeconds = (move.clientX - startX) / state.pxPerSecond;
    if (side === "right") {
      nextSpan = Math.max(MIN_DURATION, startSpan + deltaSeconds);
      if (source != null && scene.kind === "video") {
        // 视频素材拉不过源长度；图片会按新时长重渲，所以不在这里夹。
        nextSpan = Math.min(nextSpan, (source - nextTrimStart) / state.speed);
      }
    } else {
      // 拖左把手是"改入点同时保持出点不动"，所以时长要反向跟着变。
      const shift = deltaSeconds * state.speed;
      nextTrimStart = Math.max(0, startTrimStart + shift);
      if (source != null) nextTrimStart = Math.min(nextTrimStart, source - MIN_DURATION);
      nextSpan = Math.max(MIN_DURATION, startSpan - (nextTrimStart - startTrimStart) / state.speed);
    }
    if (clip) clip.style.width = Math.max(14, nextSpan * state.pxPerSecond - 2) + "px";
  };

  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    if (Math.abs(nextSpan - startSpan) < 0.05
      && Math.abs(nextTrimStart - startTrimStart) < 0.05) {
      drawClips();
      return;
    }
    run("Trimming", () =>
      applyTiming(index, {
        trim_start: Number(nextTrimStart.toFixed(3)),
        duration: Number(nextSpan.toFixed(3)),
      }));
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

/* ---------- preview playback ---------- */

/** 播放头落在哪一段，以及落在这一段里的第几秒。 */
/** 播放头落在哪一段。走到最后一段画面之后返回 ``-1``，表示"这里没有画面"。

    以前这里对任何超出的时间都返回最后一段，于是画面用完之后预览会定格在
    最后一帧，看起来和"卡住了"一模一样。明确区分出"没有画面"，界面才能把
    这件事说出来。 */
function clipAt(seconds) {
  const offsets = sceneOffsets();
  for (let i = state.scenes.length - 1; i >= 0; i -= 1) {
    if (seconds >= offsets[i] - 1e-6) {
      const end = offsets[i] + screenDuration(state.scenes[i]);
      if (seconds < end - 1e-6) return { index: i, offset: seconds - offsets[i] };
      return { index: -1, offset: 0 };
    }
  }
  return { index: -1, offset: 0 };
}

/** 把 <video> 对到播放头所在的位置。``force`` 时即使还是同一段也重新定位。 */
function syncPreview(force) {
  const { index, offset } = clipAt(state.playhead);
  const scene = state.scenes[index];

  // 画面用完但旁白还在：保持画面区可见（黑屏 + 一句说明），让旁白继续放。
  // 停下来等于把"还缺多少画面"这个信息藏起来。
  if (index < 0 && state.playhead < narrationLength()) {
    ui.frame.classList.add("ready");
    ui.gap.hidden = false;
    ui.preview.pause();
    ui.preview.style.visibility = "hidden";
    state.loadedClip = -1;
    return;
  }
  ui.gap.hidden = true;
  ui.preview.style.visibility = "visible";

  if (!scene || !scene.exists) {
    ui.frame.classList.remove("ready");
    return;
  }
  ui.frame.classList.add("ready");
  if (index !== state.loadedClip) {
    const advancing = state.loadedClip >= 0;
    state.loadedClip = index;
    ui.preview.src = scene.url;
    // 元数据到位之前 currentTime 写不进去，所以等一次 loadedmetadata。
    ui.preview.addEventListener("loadedmetadata", () => {
      seekWithin(scene, offset);
      if (state.playing) ui.preview.play().catch(() => {});
    }, { once: true });
    if (advancing && state.playing) playTransition();
    return;
  }
  if (force) seekWithin(scene, offset);
}

/** 切片段时放一下转场的近似效果。

    真正的转场是渲染阶段逐片段加的(见 video.process_one_clip),没有烘进
    素材,所以预览没法播出真东西。这里按配置的模式给一个同类的视觉提示,
    并在设置区标明它只是近似——什么都不显示会让用户以为转场没生效。 */
function playTransition() {
  const mode = String(state.settings.transition || "");
  const kind = TRANSITION_CLASS[mode];
  if (!kind) return;
  ui.fx.className = "fx";
  // 强制重排，否则同一个动画类连续两次不会重新播放。
  void ui.fx.offsetWidth;
  ui.fx.className = "fx " + kind;
}

function seekWithin(scene, offset) {
  const within = (scene.trim_start || 0) + offset * state.speed;
  try { ui.preview.currentTime = Math.max(0, within); } catch { /* 还没 ready */ }
}

function movePlayhead() {
  // 轨道名在滚动区外面，所以这里不再需要那个 70px 偏移。
  ui.playhead.style.left = state.playhead * state.pxPerSecond + "px";
  ui.clock.textContent = `${fmt(state.playhead)} / ${fmt(projectTotal())}`;
}

function tick() {
  if (!state.playing) return;
  // 旁白是成片的时间基准，所以有配音时以它为准；没有就用画面自己走。
  if (ui.narration.src && !ui.narration.paused) {
    state.playhead = ui.narration.currentTime;
  } else {
    const { index, offset } = clipAt(state.playhead);
    const scene = state.scenes[index];
    if (scene && !ui.preview.paused) {
      const within = ui.preview.currentTime - (scene.trim_start || 0);
      state.playhead = sceneOffsets()[index] + Math.max(0, within / state.speed);
    } else {
      state.playhead += 1 / 60;
    }
    void offset;
  }

  // 走到画面和旁白里更长的那个才算放完。只按画面长度收尾的话，画面比
  // 旁白短时后面那段旁白永远听不到。
  if (state.playhead >= projectTotal()) {
    stop();
    state.playhead = 0;
    state.loadedClip = -1;
    syncPreview(true);
    movePlayhead();
    return;
  }
  const { index } = clipAt(state.playhead);
  if (index !== state.loadedClip) syncPreview(false);
  movePlayhead();
  requestAnimationFrame(tick);
}

function play() {
  if (!state.scenes.length) return;
  state.playing = true;
  ui.play.textContent = "Pause";
  if (ui.narration.src) {
    try { ui.narration.currentTime = state.playhead; } catch { /* 元数据未就绪 */ }
    ui.narration.play().catch(() => {});
  }
  ui.preview.play().catch(() => {});
  requestAnimationFrame(tick);
}

function stop() {
  state.playing = false;
  ui.play.textContent = "Play";
  ui.preview.pause();
  ui.narration.pause();
}

/* ---------- wiring ---------- */

ui.load.addEventListener("click", () => load(ui.task.value));
ui.task.addEventListener("keydown", (event) => {
  if (event.key === "Enter") load(ui.task.value);
});

ui.play.addEventListener("click", () => (state.playing ? stop() : play()));

ui.zoom.addEventListener("input", () => {
  state.pxPerSecond = Number(ui.zoom.value);
  draw();
});

// 在时间线上点一下就把播放头移到那里，这是剪辑软件最基本的期待。
for (const track of [ui.ruler, ui.audioTrack]) {
  track.addEventListener("pointerdown", (event) => {
    const rect = track.getBoundingClientRect();
    state.playhead = Math.max(0, Math.min(projectTotal(),
      (event.clientX - rect.left) / state.pxPerSecond));
    state.loadedClip = -1;
    syncPreview(true);
    movePlayhead();
  });
}

// 缩放改变后轨道宽度要跟着重算，否则放大时右侧会露出空背景。
window.addEventListener("resize", () => {
  fitFrame();
  if (state.scenes.length) { drawRuler(); movePlayhead(); }
});

// 画面区的尺寸不止受窗口影响：嵌进 WebUI 时它由那一列决定，检视面板换行、
// 提示条出现都会改变它。ResizeObserver 覆盖这些，resize 事件覆盖不到。
if (typeof ResizeObserver !== "undefined" && ui.screen) {
  new ResizeObserver(() => fitFrame()).observe(ui.screen);
}

// 标签页重新可见时再量一次：在后台时量到的是 0，那一次会被上面的退化检查
// 跳过，所以必须有人在可见之后补上这一次。
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) fitFrame();
});

// 一段播完就接下一段。这里只推进一帧的量，剩下的交给 tick。
ui.preview.addEventListener("ended", () => {
  if (!state.playing) return;
  const { index } = clipAt(state.playhead);
  const offsets = sceneOffsets();
  if (index >= 0 && index + 1 < state.scenes.length) {
    state.playhead = offsets[index + 1];
    syncPreview(false);
  }
});

// 片段被裁短时 <video> 不会自己停在出点，得手动跨段。
ui.preview.addEventListener("timeupdate", () => {
  if (!state.playing) return;
  const { index } = clipAt(state.playhead);
  const scene = state.scenes[index];
  if (!scene) return;
  const limit = (scene.trim_start || 0) + screenDuration(scene) * state.speed;
  if (ui.preview.currentTime >= limit - 0.03) {
    const offsets = sceneOffsets();
    if (index + 1 < state.scenes.length) {
      state.playhead = offsets[index + 1];
      syncPreview(false);
    } else {
      stop();
    }
  }
});

ui.fit.addEventListener("click", () => run("Fitting the scenes to the narration", async () => {
  await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/timeline/fit`);
  // 视频素材拉不过自身长度，会被收住。刷新后再比一次，差多少就说多少。
  return "";
}));

ui.rerecord.addEventListener("click", () => run("Re-recording narration", async () => {
  await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/audio`);
  state.wasBusy = true;
  watchTask();
  return "";
}));

ui.render.addEventListener("click", () => run("Submitting the render", async () => {
  await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/render`);
  // 提交完立刻开始盯状态，这样渲染期间界面是"锁住并显示进度"，
  // 而不是看起来什么都没发生、改一下才被拒绝。
  state.wasBusy = true;
  watchTask();
  return "";
}));

// 嵌在 WebUI 里时，提交类操作交给外面的 Streamlit 按钮（见 EMBEDDED）。
// 任务 id 也由链接带过来，不需要再给一个输入框。
if (EMBEDDED) {
  for (const node of [ui.rerecord, ui.render, ui.task, ui.load]) node.hidden = true;
  const hint = document.createElement("span");
  hint.className = "totals";
  hint.textContent = "Use the buttons below this editor to re-record or render.";
  ui.render.parentNode.appendChild(hint);
}

// 深链接：WebUI 的审片面板就是用 ?task=<id> 跳到这里的。
const initial = new URLSearchParams(window.location.search).get("task");
if (initial) {
  ui.task.value = initial;
  load(initial);
}
