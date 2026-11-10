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
  queue: el("queue"),
  scriptToggle: el("script-toggle"), scriptPanel: el("script-panel"),
  scriptText: el("script-text"), scriptSave: el("script-save"),
  scriptClose: el("script-close"), scriptNote: el("script-note"),
};

const state = {
  taskId: "",
  data: null,
  scenes: [],
  speed: 1,
  settings: {},
  // 可挑的画风，以及这条片子出片时用的那个。后者是重生成的默认值：
  // 以前这里什么都不知道，出图只能用 API 进程启动时读到的那份全局配置,
  // 于是漫画风的片子补出过一张油画。
  imageStyles: [],
  imageStyle: "",
  imageStylePinned: false,
  // 排队中的重生成。出一张图要几分钟，所以点"重出"只是排队：请求立刻
  // 回来，用户接着点下一张或者去改别处，不用站在那儿等。
  queue: { jobs: [], waiting: 0, running: null, busy: false },
  queueWatching: false,
  // 已经重读过时间线的那些活，免得同一张图反复触发重读。
  queueSeen: [],
  // 队列浮层收起没有。只是这台浏览器上的一点顺手，读不出来就当没收起。
  queueCollapsed: (() => {
    try { return !!localStorage.getItem("mpt_queue_collapsed"); } catch { return false; }
  })(),
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
  ui.banner.title = message ? "Click to dismiss" : "";
}

// 点一下关掉。它浮在画面上，所以得有办法让它走开——尤其是报错那一条：
// 看完了就只剩挡着。
ui.banner.addEventListener("click", () => say(""));

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
    state.imageStyles = data.image_styles || [];
    state.imageStyle = data.image_style || "";
    state.imageStylePinned = !!data.image_style_pinned;
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
    // 队列在服务端，所以上一次开着这个页面时排的活可能还在跑。刷新页面、
    // 换一个浏览器都还看得见它。已经出完的那些不算"刚完成"——时间线是
    // 刚读的，上面已经是新画面了。
    state.queueSeen = doneIds(await fetchQueue());
    draw();
    watchQueue();
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
  state.imageStyles = data.image_styles || [];
  state.imageStyle = data.image_style || "";
  state.imageStylePinned = !!data.image_style_pinned;
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

/* ---------- the regeneration queue ---------- */

function queuePath() {
  return `/tasks/${encodeURIComponent(state.taskId)}/scenes/queue`;
}

const EMPTY_QUEUE = { jobs: [], waiting: 0, running: null, busy: false };

/** 已经出完的那些活。只有"出完"才需要重读时间线：失败和撤销没有改磁盘。 */
function doneIds(queue) {
  return (queue.jobs || []).filter((job) => job.state === "done").map((job) => job.id);
}

/** 读一次队列现状。不重读时间线，只更新那条队列栏。 */
async function fetchQueue() {
  if (!state.taskId) return EMPTY_QUEUE;
  try {
    state.queue = (await call("GET", queuePath())) || EMPTY_QUEUE;
  } catch {
    // 队列查不到不该让编辑器停摆：对面可能还跑着没有这条接口的旧版 API。
    state.queue = EMPTY_QUEUE;
  }
  drawQueue();
  return state.queue;
}

/** 盯着队列，直到排空。

    每出完一张就重读一次时间线：磁盘上的画面已经换了，不重读的话用户看到的
    还是旧缩略图，会以为"重出"根本没生效。 */
async function watchQueue() {
  if (state.queueWatching || !state.taskId) return;
  state.queueWatching = true;
  try {
    for (;;) {
      const queue = await fetchQueue();
      const fresh = doneIds(queue).filter((id) => !state.queueSeen.includes(id));
      if (fresh.length) {
        state.queueSeen.push(...fresh);
        await refresh();
      }
      if (!queue.busy) break;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  } finally {
    state.queueWatching = false;
    drawQueue();
  }
}

/** 这一段上排着的活（含正在出的）。 */
function pendingJobs(index) {
  return (state.queue.jobs || []).filter(
    (job) => job.index === index && (job.state === "queued" || job.state === "running")
  );
}

/** 把一次重生成排进队列。请求立刻回来，所以按钮不用禁。 */
async function queueRegeneration(index, keyword, imageStyle) {
  try {
    const job = await call(
      "POST",
      `${scenePath(index)}/material/queue`,
      { keyword, image_style: imageStyle }
    );
    say(job && job.position > 1
      ? `Scene ${index + 1} queued — ${job.position - 1} ahead of it.`
      : `Scene ${index + 1} queued. Queue another or keep editing.`);
    await fetchQueue();
    draw();
    watchQueue();
  } catch (error) {
    say(String(error.message || error), "error");
  }
}

/** 队列栏。排着的、正在出的、刚结束的都在这里，失败原因照原样显示。 */
function drawQueue() {
  const jobs = (state.queue.jobs || []).filter((job) => job.state !== "cancelled");
  ui.queue.innerHTML = "";
  ui.queue.hidden = !jobs.length;
  if (!jobs.length) return;

  const running = state.queue.running;
  const waiting = state.queue.waiting || 0;
  const failed = jobs.filter((job) => job.state === "failed").length;
  ui.queue.classList.toggle("collapsed", state.queueCollapsed);
  const head = document.createElement("div");
  head.className = "queue-head";
  const title = document.createElement("span");
  title.className = "queue-title";
  // 收起来之后这一行是唯一还看得见的东西，所以失败的条数要写在这里——
  // 否则一张图出错了而面板是收着的，用户永远不会知道。
  const summary = running
    ? `Regenerating scene ${running.index + 1} · ${fmt(running.elapsed)}`
      + (waiting ? ` · ${waiting} waiting` : "")
    : waiting
      ? `${waiting} picture${waiting === 1 ? "" : "s"} waiting`
      : "Nothing left to regenerate";
  title.textContent = (state.queueCollapsed ? "▸ " : "▾ ")
    + summary
    + (failed ? ` · ${failed} failed` : "");
  title.title = state.queueCollapsed ? "Show the queue" : "Hide the queue";
  title.addEventListener("click", () => {
    state.queueCollapsed = !state.queueCollapsed;
    try { localStorage.setItem("mpt_queue_collapsed", state.queueCollapsed ? "1" : ""); }
    catch { /* 隐身窗口里存不了，收起状态就只在这一页有效 */ }
    drawQueue();
  });
  head.appendChild(title);

  if (jobs.some((job) => job.state === "done" || job.state === "failed")) {
    const clear = button("Clear finished", "small", async () => {
      try {
        state.queue = (await call("DELETE", queuePath())) || EMPTY_QUEUE;
        drawQueue();
      } catch (error) {
        say(String(error.message || error), "error");
      }
    });
    head.appendChild(clear);
  }
  ui.queue.appendChild(head);

  for (const job of jobs) {
    const row = document.createElement("div");
    row.className = "queue-row " + job.state;

    const label = document.createElement("span");
    label.className = "queue-label";
    const term = job.keyword ? ` · ${job.keyword}` : "";
    if (job.state === "running") {
      label.textContent = `Scene ${job.index + 1}${term} · generating ${fmt(job.elapsed)}`;
    } else if (job.state === "queued") {
      label.textContent = `Scene ${job.index + 1}${term} · waiting`
        + (job.position > 1 ? ` (${job.position - 1} ahead)` : " (next)");
    } else if (job.state === "done") {
      label.textContent = `Scene ${job.index + 1}${term} · redrawn in ${fmt(job.elapsed)}`;
    } else {
      // 失败原因是服务端点名过的那一句（哪个源怎么失败的），照原样显示。
      // 只是把换行和连串空格压成一个空格：有些报错自带几行 ##### 分隔线，
      // 照原样塞进这块浮层会把它撑成半屏高。完整那段话在 title 里。
      const reason = String(job.error || "unknown error").replace(/\s+/g, " ").trim();
      label.textContent = `Scene ${job.index + 1}${term} · failed: ${reason}`;
      label.title = String(job.error || "");
    }
    row.appendChild(label);

    // 只有还排着的能撤。正在出的那一张撤不掉：出图是一次没法中断的外部
    // 调用，给个按钮假装能撤只会让用户看着它继续跑。
    if (job.state === "queued") {
      row.appendChild(button("Cancel", "small", async () => {
        try {
          await call("DELETE", `${queuePath()}/${encodeURIComponent(job.id)}`);
          await fetchQueue();
          draw();
        } catch (error) {
          say(String(error.message || error), "error");
        }
      }));
    }
    // 点一行就跳到那一段，省得自己在时间线上找。
    label.addEventListener("click", () => {
      const scene = state.scenes[job.index];
      if (!scene) return;
      state.selected = job.index;
      draw();
    });
    ui.queue.appendChild(row);
  }
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
  drawQueue();
  const ready = state.scenes.length > 0;
  ui.play.disabled = !ready;
  // 还有图没出完就不让开渲：渲染器要读的正是队列那边还在换的那几个文件，
  // 渲出来会是新旧画面混在一起的一条片子。接口那边也会拒,这里只是不让
  // 用户白点一次。
  ui.render.disabled = !ready
    || !!state.queue.busy
    || !!(state.data && state.data.audio_stale);
  ui.rerecord.disabled = !state.data || !state.data.audio_stale;
  ui.scriptToggle.disabled = !state.data;
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

    // 排着队要重出的那几段，在轨道上就标出来：用户是在时间线上挑画面的，
    // 他需要在这里看见"这段我已经点过了"，而不是去别处对一遍列表。
    const pending = pendingJobs(index);
    if (pending.length) {
      const running = pending.some((job) => job.state === "running");
      clip.classList.add(running ? "generating" : "queued");
      const badge = document.createElement("div");
      badge.className = "clip-badge";
      badge.textContent = running
        ? "redrawing…"
        : pending.length > 1
          ? `queued ×${pending.length}`
          : "queued";
      clip.appendChild(badge);
    }

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

  // 画风紧跟在关键词下面：关键词说画什么，画风说画成什么样，改完一起
  // 重生成。默认是这条片子出片时用的那个风格——以前重生成读的是 API
  // 进程"现在"的全局配置，漫画风的片子因此补出过一张油画。
  const style = state.imageStyles.length
    ? stylePicker(scene.image_style || "")
    : null;
  if (style) ui.inspBody.append(style.wrap);

  // 生成新画面是这个面板里最常用的动作：画面不对时要改的通常就是关键词，
  // 改完紧接着就要重出一张。所以它紧贴在关键词下面，而不是和锁定、删除
  // 挤在最底下那一排。
  //
  // 本机出一张图要几分钟（实测 zimage-turbo 约 3 分钟），所以点下去只是
  // **排队**：请求立刻回来，按钮也不禁用，用户接着挑下一段点"重出"，或者
  // 去改别处的台词和时长。等着出的那几张在上面那条队列栏里，出完一张
  // 时间线自己就换成新画面。
  const regenerate = button(regenerateLabel(scene), "primary", () =>
    queueRegeneration(
      scene.index,
      keyword.input.value || null,
      // 下拉框在就把它的值发出去，空字符串是"和这条片子一样"，
      // 和"没有下拉框"(null,沿用上次挑的)不是一回事。
      style ? style.input.value : null
    ));
  ui.inspBody.appendChild(regenerate);

  // 这一段已经排着了就说出来。不说的话，用户点完看不出任何变化，只会
  // 再点一次——然后同一段画面被重出两遍。
  const mine = pendingJobs(scene.index);
  if (mine.length) {
    const queued = document.createElement("p");
    queued.className = "hint pending";
    const running = mine.find((job) => job.state === "running");
    queued.textContent = running
      ? `Being redrawn now · ${fmt(running.elapsed)} so far.`
      : mine.length > 1
        ? `${mine.length} regenerations of this scene are waiting in the queue.`
        : "Waiting in the queue. Queue others or keep editing.";
    ui.inspBody.appendChild(queued);
  }

  const sourceNote = document.createElement("p");
  sourceNote.className = "hint";
  sourceNote.textContent = isGenerated(scene)
    ? "Draws a new picture from the keyword. Edit the keyword first if the "
      + "subject itself is wrong — that is usually what fixes it. Pictures are "
      + "drawn one at a time and a local image server takes a few minutes "
      + "each, so this only joins the queue — keep working while it runs."
    : "Stock searches are cached, so the same keyword returns the same clip. "
      + "Change the keyword to get a different one.";
  ui.inspBody.appendChild(sourceNote);

  // 台词按时间算出来的：这段画面在屏幕上时真正会念到的那几句。配音是一整
  // 条，字幕按那条配音的时间轴生成，所以"这张图上会念什么"只由它在成片里
  // 的起止时间决定——把图拖到别的位置，念的词不跟着走。
  //
  // 标题要说清这件事。以前写的是 "Narration"，用户会以为那是这张图自己的
  // 台词，于是重排之后看到另一段的词，以为渲染器配错了。
  const narration = field(
    scene.narration_edited ? "Spoken here (edited)" : "Spoken here",
    "textarea",
    scene.narration || ""
  );
  ui.inspBody.append(narration.wrap);

  const spokenNote = document.createElement("p");
  spokenNote.className = "hint";
  spokenNote.textContent = scene.narration_edited
    ? "You rewrote these words, so they stay as you typed them. Clear the box "
      + "and save to drop them from the script instead."
    : "Taken from the narration by time — the lines that play while this "
      + "picture is on screen. It follows the timeline, so it changes when you "
      + "move or retime pictures. Use Script for the whole text.";
  ui.inspBody.appendChild(spokenNote);

  const save = button("Save text", "small", () => run("Saving", () =>
    call("PUT", scenePath(scene.index), {
      keyword: keyword.input.value,
      narration: narration.input.value,
    })));
  ui.inspBody.appendChild(save);

  // 这一段怎么出场。放在时长前面：先决定它长什么样，再决定它停多久。
  const transitions = (state.data && state.data.transitions) || [];
  const motions = (state.data && state.data.image_motions) || [];
  if (transitions.length || motions.length) {
    const cut = transitions.length
      ? inheritPicker("Transition in", transitions, TRANSITION_LABELS,
          scene.transition || "", videoTransitionLabel())
      : null;
    const motion = motions.length
      ? inheritPicker("Picture motion", motions, MOTION_LABELS,
          scene.image_motion || "", videoMotionLabel())
      : null;
    if (cut) ui.inspBody.append(cut.wrap);
    if (motion) ui.inspBody.append(motion.wrap);

    const effectsNote = document.createElement("p");
    effectsNote.className = "hint";
    effectsNote.textContent = "The transition is added when the video is "
      + "rendered. The motion is baked into this scene's clip, so changing it "
      + "re-renders the clip from its picture — a few seconds, and instant "
      + "once you have used that setting before.";
    ui.inspBody.appendChild(effectsNote);

    ui.inspBody.appendChild(button("Apply effects", "small", () =>
      run("Applying the effects", () =>
        call("PUT", scenePath(scene.index) + "/effects", {
          transition: cut ? cut.input.value : null,
          image_motion: motion ? motion.input.value : null,
        }))));
  }

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

  // 和重生成那边同一个下拉框，默认同样是这条片子的风格：补一张画面想要的
  // 本来就是"再来一张一样的"，而不是在漫画片子里插一张油画。
  const style = state.imageStyles.length ? stylePicker("") : null;
  if (style) ui.inspBody.append(style.wrap);

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
          {
            keyword: term,
            image_style: style ? style.input.value : null,
          });
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

/** 画风下拉框。空值表示"和这条片子一样",也就是出片时用的那个风格。

    id 列表由 API 给出(服务端的预设表),这里只负责显示名。遇到不认识的新
    预设就把 id 本身排版出来,而不是漏掉一个选项。 */
function stylePicker(selected) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const caption = document.createElement("label");
  caption.textContent = "Image style";
  const input = document.createElement("select");
  for (const id of ["", ...state.imageStyles]) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = id ? styleLabel(id) : sameStyleLabel();
    input.appendChild(option);
  }
  input.value = state.imageStyles.includes(selected) ? selected : "";
  wrap.append(caption, input);
  return { wrap, input };
}

/* ---------- 这一段怎么出场：转场和运镜 ----------

   两者在片子里的位置完全不同——运镜在出图那一刻就烘进 mp4 了,转场是渲染时
   加的——但在用户眼里是同一件事,所以并排放在一起。 */
const TRANSITION_LABELS = {
  none: "No transition", fade_in: "Fade in", fade_out: "Fade out",
  slide_in: "Slide in", slide_out: "Slide out", zoom_in: "Zoom in",
  zoom_out: "Zoom out", shuffle: "Random each time",
};

const MOTION_LABELS = { zoom: "Slow zoom", static: "Hold still" };

/** 选择框：第一项永远是"跟整条片子一样"。 */
function inheritPicker(label, ids, labels, selected, inheritText) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const caption = document.createElement("label");
  caption.textContent = label;
  const input = document.createElement("select");
  for (const id of ["", ...ids]) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = id ? (labels[id] || id) : inheritText;
    input.appendChild(option);
  }
  input.value = ids.includes(selected) ? selected : "";
  wrap.append(caption, input);
  return { wrap, input };
}

/** 整条片子现在用的那个转场，用来写"跟片子一样（淡入）"。 */
function videoTransitionLabel() {
  const settings = (state.data && state.data.render_settings) || {};
  const raw = String(settings.transition || "").trim();
  if (!raw || raw === "None") return "Same as the video (none)";
  const id = Object.keys(TRANSITION_LABELS).find(
    (key) => TRANSITION_LABELS[key] && labelOfRenderTransition(key) === raw);
  return `Same as the video (${id ? TRANSITION_LABELS[id] : raw})`;
}

/* 渲染器认的名字是 "FadeIn" 这种驼峰，界面用的是 "fade_in"。这里只做
   一次朴素的转换，省得为这点事再加一张表。 */
function labelOfRenderTransition(id) {
  return id === "shuffle" ? "Shuffle"
    : id.split("_").map((part) => part[0].toUpperCase() + part.slice(1)).join("");
}

function videoMotionLabel() {
  const settings = (state.data && state.data.render_settings) || {};
  const raw = String(settings.image_motion || "").trim();
  return raw && MOTION_LABELS[raw]
    ? `Same as the video (${MOTION_LABELS[raw].toLowerCase()})`
    : "Same as the video";
}

const STYLE_LABELS = {
  photorealistic: "Photorealistic", cinematic: "Cinematic",
  monochrome: "Black and white", vintage_film: "Vintage film",
  anime: "Anime", digital_art: "Digital art", oil_painting: "Oil painting",
  watercolour: "Watercolour", three_d: "3D render", flat_vector: "Flat vector",
  stickman: "Stickman explainer", comic: "Comic book",
};

function styleLabel(id) {
  return STYLE_LABELS[id]
    || String(id).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function sameStyleLabel() {
  // 说出名字，用户才知道"一样"是哪一样。自定义模板配不上任何预设名，
  // 那就只说"一样"。
  //
  // 这个功能之前出的片子没有记下画风，对它们只能按设置里现在那个来，
  // 所以话也得这么说——否则就是对着一条漫画风的片子说"和这条片子一样"，
  // 而实际画出来的是设置里那个风格。
  const name = state.imageStyle ? styleLabel(state.imageStyle) : "";
  if (state.imageStylePinned) {
    return name ? `Same as this video (${name})` : "Same as this video";
  }
  return name ? `From settings (${name})` : "From settings";
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

/* ---------- 旁白正文 ----------

   打开时才从载荷里灌一次文字，之后不再碰：每次刷新都重灌会把用户正在打的
   字抹掉——而这个面板是开着的时候图还在一张张出，刷新随时会来。 */
function openScript() {
  ui.scriptText.value = (state.data && state.data.script) || "";
  ui.scriptNote.textContent = state.data && state.data.audio_stale
    ? "The recorded voice is older than these words."
    : "";
  ui.scriptPanel.hidden = false;
  ui.scriptText.focus();
}

function closeScript() {
  ui.scriptPanel.hidden = true;
}

ui.scriptToggle.addEventListener("click", () => {
  if (ui.scriptPanel.hidden) openScript(); else closeScript();
});

ui.scriptClose.addEventListener("click", closeScript);

ui.scriptSave.addEventListener("click", () => run("Saving the script", async () => {
  await call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/script`, {
    script: ui.scriptText.value,
  });
  closeScript();
  // 存完配音就不作数了，但不替用户按"重新配音"：那要跑一次 TTS。
  return "Script saved. Re-record the narration when you want it spoken.";
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
