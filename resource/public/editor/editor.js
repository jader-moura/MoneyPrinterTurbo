/* 时间线编辑器。没有框架、没有构建步骤：拖拽就是 pointer 事件加一点算术，
   预览就是一个 <video>，为此引入 React/Vite 工具链不值得。
   A dependency-free timeline: drag is pointer maths and preview is a <video>.

   权责划分：这个页面只描述"剪成什么样",渲染始终由 Python 那条既有管线做。
   所以它从不自己拼接画面,只提交入点/出点/顺序,然后让 /render 出片。
   This page only describes the edit; the existing Python pipeline renders it. */

"use strict";

const API = "/api/v1";
const MIN_DURATION = 0.2;

// 导演项的预览（见下面"导演项的预览"那一节）。放在最上面：页面一加载就会
// 画一遍，那时候这几个名字必须已经在了。
const DIRECTION_POLL = 5000;
const directionSounds = { beds: {}, hits: [], turn: 0, last: 0 };
const unit = (value) => Math.min(1, Math.max(0, value));

const el = (id) => document.getElementById(id);
const ui = {
  task: el("task"), load: el("load"), totals: el("totals"),
  limit: el("limit"), limitLine: el("limit-line"),
  limitOver: el("limit-over"),
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
  musicToggle: el("music-toggle"), musicPanel: el("music-panel"),
  musicTrack: el("music-track"), musicVolume: el("music-volume"),
  musicUpload: el("music-upload"), hold: el("hold"),
  musicPreview: el("music-preview"), musicSave: el("music-save"),
  musicClose: el("music-close"), musicNote: el("music-note"),
  musicLane: el("music-lane"), musicBed: el("music-bed"),
  subs: el("subs"), subsText: el("subs-text"),
  direction: el("direction"), dirDim: el("dir-dim"), dirTitle: el("dir-title"),
  dirTitleText: el("dir-title-text"), dirTagline: el("dir-tagline"),
  dirCard: el("dir-card"), dirCardTitle: el("dir-card-title"),
  dirCardSub: el("dir-card-sub"),
  dirTimeline: el("dir-timeline"), dirTlTicks: el("dir-tl-ticks"),
  dirTlFirst: el("dir-tl-first"), dirTlLast: el("dir-tl-last"),
  dirTlDate: el("dir-tl-date"), dirTlLabel: el("dir-tl-label"),
  directionLane: el("direction-lane"), directionCell: el("direction-cell"),
  yt: el("yt"), subsYt: el("subs-yt"), subsSafe: el("subs-safe"),
  captionPanel: el("caption-panel"), captionText: el("caption-text"),
  captionWhen: el("caption-when"), captionSave: el("caption-save"),
  captionClose: el("caption-close"), captionNote: el("caption-note"),
  subsToggle: el("subs-toggle"), subsPanel: el("subs-panel"),
  subsEnabled: el("subs-enabled"), subsFont: el("subs-font"),
  subsSize: el("subs-size"), subsPosition: el("subs-position"),
  subsColor: el("subs-color"), subsStrokeColor: el("subs-stroke-color"),
  subsStrokeWidth: el("subs-stroke-width"), subsCustom: el("subs-custom"),
  subsCustomField: el("subs-custom-field"), subsBgOn: el("subs-bg-on"),
  subsBg: el("subs-bg"), subsBgField: el("subs-bg-field"),
  subsSave: el("subs-save"), subsClose: el("subs-close"), subsNote: el("subs-note"),
  cutSound: el("cut-sound"), cutVolume: el("cut-volume"),
  cutPreview: el("cut-preview"),
  approve: el("approve"), approveCount: el("approve-count"),
  approveNote: el("approve-note"), approveDraw: el("approve-draw"),
  approveOne: el("approve-one"),
  libraryToggle: el("library-toggle"), library: el("library"),
  libraryGrid: el("library-grid"), libraryCount: el("library-count"),
  libraryClose: el("library-close"), libraryMore: el("library-more"),
  libraryMoreRow: el("library-more-row"),
  // 在这个挡板**里面**找那个盒子，不是找页面上第一个 .modal-box：页面上现在
  // 有两个挡板，谁排在前面不该决定渲染进度画在哪个盒子上。
  renderModal: el("render-modal"),
  renderBox: el("render-modal").querySelector(".modal-box"),
  renderTitle: el("render-title"), renderNote: el("render-note"),
  renderBar: el("render-bar"), renderPct: el("render-pct"),
  renderElapsed: el("render-elapsed"), renderHide: el("render-hide"),
  renderPlay: el("render-play"), renderReveal: el("render-reveal"),
  renderDone: el("render-done"),
  effectsToggle: el("effects-toggle"), effectsPanel: el("effects-panel"),
  effectsTransition: el("effects-transition"), effectsMotion: el("effects-motion"),
  effectsApply: el("effects-apply"), effectsOwn: el("effects-own"),
  effectsFinish: el("effects-finish"),
  effectsSave: el("effects-save"), effectsClose: el("effects-close"),
  effectsNote: el("effects-note"),
  publishToggle: el("publish-toggle"), publishPanel: el("publish-panel"),
  publishShape: el("publish-shape"), publishWrite: el("publish-write"),
  publishNote: el("publish-note"), publishTitle: el("publish-title"),
  publishTitleCount: el("publish-title-count"),
  publishDescription: el("publish-description"),
  publishDescriptionCount: el("publish-description-count"),
  publishTags: el("publish-tags"), publishPrompt: el("publish-prompt"),
  publishPromptCount: el("publish-prompt-count"),
  publishThumbHint: el("publish-thumb-hint"), publishDraw: el("publish-draw"),
  publishScenes: el("publish-scenes"), publishPickHint: el("publish-pick-hint"),
  publishThumbWhere: el("publish-thumb-where"), publishDrawBox: el("publish-draw-box"),
  publishDownload: el("publish-download"), publishThumbNote: el("publish-thumb-note"),
  publishThumb: el("publish-thumb"), publishThumbImg: el("publish-thumb-img"),
  publishText: el("publish-text"), publishTextCount: el("publish-text-count"),
  publishTextFont: el("publish-text-font"),
  publishTextPosition: el("publish-text-position"),
  publishTextSize: el("publish-text-size"),
  publishTextStrokeWidth: el("publish-text-stroke-width"),
  publishTextColor: el("publish-text-color"),
  publishTextStrokeColor: el("publish-text-stroke-color"),
  publishTextBand: el("publish-text-band"),
  publishTextApply: el("publish-text-apply"), publishTextNote: el("publish-text-note"),
  publishSave: el("publish-save"), publishClose: el("publish-close"),
  publishSaved: el("publish-saved"),
  shelfModal: el("shelf-modal"), shelfTitle: el("shelf-title"),
  shelfNote: el("shelf-note"), shelfFilter: el("shelf-filter"), shelfGrid: el("shelf-grid"),
  shelfStatus: el("shelf-status"), shelfClose: el("shelf-close"),
  newVideo: el("new-video"), libraryNew: el("library-new"),
  newModal: el("new-modal"), newSubject: el("new-subject"),
  newScript: el("new-script"), newWrite: el("new-write"),
  newParagraphs: el("new-paragraphs"), newLanguage: el("new-language"),
  newTabs: el("new-tabs"), newFields: el("new-fields"),
  newScriptMore: el("new-script-more"),
  newScriptMoreLabel: el("new-script-more-label"),
  newScriptFields: el("new-script-fields"),
  newStatus: el("new-status"), newSettings: el("new-settings"),
  newCancel: el("new-cancel"), newCreate: el("new-create"),
  appSettings: el("app-settings"), settingsModal: el("settings-modal"),
  settingsNav: el("settings-nav"), settingsBody: el("settings-body"),
  settingsStatus: el("settings-status"), settingsClose: el("settings-close"),
  settingsSave: el("settings-save"),
  dock: el("dock"), projectTitle: el("project-title"),
  deleteVideo: el("delete-video"), deleteModal: el("delete-modal"),
  deleteWhat: el("delete-what"), deleteNote: el("delete-note"),
  deleteCancel: el("delete-cancel"), deleteConfirm: el("delete-confirm"),
  photoModal: el("photo-modal"), photoTitle: el("photo-title"),
  photoSearch: el("photo-search"), photoFind: el("photo-find"),
  photoNote: el("photo-note"), photoGrid: el("photo-grid"),
  photoUpload: el("photo-upload"), photoAuto: el("photo-auto"),
  photoClose: el("photo-close"),
  photoLine: el("photo-line"), photoKeepSlot: el("photo-keep"),
  photoKept: el("photo-kept"), photoSkip: el("photo-skip"),
  photoGoogle: el("photo-google"), photoGoogleNote: el("photo-google-note"),
  undo: el("undo"), redo: el("redo"), split: el("split"),
  duplicate: el("duplicate"), deleteScene: el("delete-scene"),
  pace: el("pace"),
  createModal: el("create-modal"), createTitle: el("create-title"),
  createNote: el("create-note"), createBar: el("create-bar"),
  createPct: el("create-pct"), createElapsed: el("create-elapsed"),
  createHide: el("create-hide"), createDone: el("create-done"),
  videoScenes: el("video-scenes"), videoModal: el("video-modal"),
  videoBox: el("video-modal").querySelector(".modal-box"),
  videoTitle: el("video-title"), videoNote: el("video-note"),
  videoList: el("video-list"), videoProgress: el("video-progress"),
  videoBar: el("video-bar"), videoPct: el("video-pct"),
  videoElapsed: el("video-elapsed"), videoCancel: el("video-cancel"),
  animateAll: el("animate-all"), videoHide: el("video-hide"),
  videoStart: el("video-start"), videoStop: el("video-stop"),
  videoDone: el("video-done"),
};

const state = {
  // 预览放多快（1、1.5、2）。只管预览，见 applyRate。
  rate: 1,
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
  // The batch of video scenes as the server last reported it: whether this
  // machine can make them at all, and the one running or just finished.
  video: {},
  videoWatching: false,
  // 已经重读过时间线的那些活，免得同一张图反复触发重读。
  queueSeen: [],
  // 成片里才加上去的片名、标题卡和声音，各在第几秒（见 loadDirection）。
  direction: null,
  directionTimer: 0,
  directionCard: "",
  musicLevel: 0.2,
  // 这条片子的配乐。时间线上要画出来，预览时要跟着响。
  music: null,
  // 屏幕上那条字幕是第几条。-1 表示此刻没有字幕。
  cueIndex: -1,
  // "做过的片子"翻到第几页了。
  libraryPage: 1,
  // 这次渲染是什么时候点下去的，以及出来的片子在哪儿。
  renderStarted: 0,
  renderedVideo: "",
  // 队列浮层收起没有。只是这台浏览器上的一点顺手，读不出来就当没收起。
  queueCollapsed: (() => {
    try { return !!localStorage.getItem("mpt_queue_collapsed"); } catch { return false; }
  })(),
  adding: false,
  taskBusy: false,
  watching: false,
  wasBusy: false,
  selected: -1,
  // 片长上限。``null`` 是没设。只是这台浏览器上的一个对齐基准,
  // 不参与渲染,所以存在 localStorage 而不是清单里。
  limit: null,
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

/** 还没画的那一段：有位置、有提示词，就是还没有画面。

    和"素材文件不见了"分开判断：那是出了问题，这是还没开始。 */
function isPending(scene) {
  return !String(scene.material || "").trim();
}

/** 这一段在成片里停留多久。没设过 duration 就是"素材剩下的全长"。 */
function screenDuration(scene) {
  if (scene.duration != null) return Math.max(MIN_DURATION, scene.duration);
  // 还没画的那一段没有素材可量，但它将来会停多久是知道的：出图就是按这个
  // 时长渲的。用它，时间轴才对得上旁白——而对得上正是这一屏要给用户看的。
  if (isPending(scene) && scene.clip_duration) {
    return Math.max(MIN_DURATION, scene.clip_duration);
  }
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
  // 上限也算进总长：上限设得比内容长时，那条线必须还在可见范围里，
  // 否则用户设了一个看不见的基准。
  return Math.max(timelineTotal(), narrationLength(), state.limit || 0);
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
  // 改了这条片子的任何东西，"能撤销几步"就可能变了。
  if (method !== "GET" && path.startsWith("/tasks/") && !path.includes("/history")) {
    scheduleHistorySync();
  }
  return payload ? payload.data : null;
}

/* ---------- loading ---------- */

async function load(taskId) {
  if (!taskId) return;
  state.taskId = taskId.trim();
  forgetDirection();
  loadLimit();
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
    state.line = -1;
    state.card = null;
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
    // 配乐单独读一次：它存在任务参数里，不在场景清单里。读不到也不挡着
    // 审片，所以不等它，画完再补上那一条轨道。
    loadMusic();
    // 导演项也是：位置可能还要在后台算几分钟，不能让它挡着时间线出来。
    loadDirection();
    // 打开时就查一次：别人(或刚才的自己)可能正在渲染这条任务。
    watchTask();
    // 队列在服务端，所以上一次开着这个页面时排的活可能还在跑。刷新页面、
    // 换一个浏览器都还看得见它。已经出完的那些不算"刚完成"——时间线是
    // 刚读的，上面已经是新画面了。
    state.queueSeen = doneIds(await fetchQueue());
    draw();
    watchQueue();
    syncHistory();
    // A batch of video scenes may be running, started from here before a
    // reload or from another window. It locks every editor, so show it.
    adoptVideoBatch();
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
  // 旁白可能刚重录过，导演项的位置跟着旁白走：重新问一次。
  loadDirection();
}

/** 盯着任务状态，直到它不再运行。

    不盯的话,用户点了"渲染成片"之后界面毫无变化,接着去改某一段就会收到
    一句"任务还在运行"——而他根本不知道有东西在跑。渲染期间确实不能改:
    渲染器正在读那几个素材文件,边读边换会得到一条谁也复现不了的片子。
    所以这里把那个状态显示出来,而不是等用户撞上去才告诉他。 */
/* 状态查不到时再试几次，一共约 10 秒。
   一次查不到不代表没在跑：接口可能刚重启，也可能提交完的那一瞬间状态还没
   落下去。但一直查不到就得说出来，不能装作还在转。 */
const STATUS_RETRIES = 5;
const STATUS_RETRY_DELAY = 2000;

/** 标签页标题，用来在页面没露脸时说话。

    渲染要好几分钟，用户会切到别的窗口去。横幅和挡板那时候一个也看不见，
    标签页标题是唯一还看得见的那一行字。 */
const PAGE_TITLE = document.title;

function setTitleNote(note) {
  document.title = note ? `${note} · ${PAGE_TITLE}` : PAGE_TITLE;
}

async function watchTask() {
  if (!state.taskId || state.watching) return;
  state.watching = true;
  let misses = 0;
  try {
    for (;;) {
      let task;
      try {
        task = await call("GET", `/tasks/${encodeURIComponent(state.taskId)}`);
        misses = 0;
      } catch (error) {
        // 以前这里一个失败就不看了，于是挡板永远停在"正在提交"——而片子在
        // 后台照样渲完，用户什么都看不到（实测 7 分 19 秒全程一个转圈）。
        misses += 1;
        if (misses < STATUS_RETRIES) {
          await new Promise((resolve) => setTimeout(resolve, STATUS_RETRY_DELAY));
          continue;
        }
        // 只有在确实有一次提交或者看到过它在跑的时候才抱怨。打开页面时查不到
        // 状态是常事（重启过接口的旧任务就查不到），那种时候闷着退出就好。
        if (state.wasBusy) loseRender(error);
        break;
      }
      // 4 = processing（见 app/models/const.py）
      const busy = task && Number(task.state) === 4;
      state.taskBusy = !!busy;
      if (!busy) {
        if (state.wasBusy) {
          state.wasBusy = false;
          say("");
          // 先刷新，这样挡板上那个"去看"按钮指向的是刚出来的那条片子。
          //
          // 但刷新成不成都必须收尾。以前这里的 ``await refresh()`` 一抛，
          // 后面的 ``finishRender`` 就永远不会跑：片子已经渲完了，而挡板还
          // 在转圈，界面上一个字都没有。收尾不能挂在刷新成功这个条件上。
          let reloaded = true;
          try {
            await refresh();
          } catch (error) {
            reloaded = false;
            console.warn("the scenes could not be reloaded", error);
          }
          finishRender(task);
          announceRender(task, reloaded);
        }
        break;
      }
      state.wasBusy = true;
      adoptRunningRender(task);
      const pct = Number(task.progress || 0);
      // 挡板开着就把数字喂给它；用户按了"隐藏"的话，还有上面那条提示在。
      updateRender(pct, (task.generation_activity || {}).phase);
      // 标签页标题也要说：渲染要好几分钟，用户早就切到别的窗口去了，而这是
      // 唯一一个在页面没露脸时还看得见的地方。
      setTitleNote(pct > 0 ? `Rendering ${pct}%` : "Rendering");
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
  // The panel of a narration line says where its retake is in the queue.
  const lines = (state.queue.jobs || []).filter((job) => job.kind === "narration")
    .map((job) => `${job.id}:${job.state}:${job.position}`).join(",");
  if (lines !== state.queueLines) {
    state.queueLines = lines;
    if (selectedLine()) drawInspector();
  }
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
        // A line recorded again: same address, new sound. Without this the
        // browser keeps playing the old take.
        const address = (state.data && state.data.audio_url) || "";
        if (address && (queue.jobs || []).some(
          (job) => job.kind === "narration" && fresh.includes(job.id))) {
          if (state.playing) stop();
          ui.narration.src = address + (address.includes("?") ? "&" : "?")
            + "take=" + Date.now();
        }
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
/** 素材文件名——"这是哪一张图"的身份。

    时间线接口发的是文件名（``1a2b….mp4``），队列里记的是整条路径
    （``C:\...\storage	asks\<id>a2b….mp4``）：同一个文件，两种写法。
    直接比字符串永远不相等，所以比到文件名为止。 */
function materialKey(path) {
  const text = String(path || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

/** 这个活现在落在第几段。

    不能用 ``job.index``：那是**排队那一刻**的下标。排着的那几分钟里用户
    完全可以去拖动画面，下标一旦不再指向同一张图，界面就会把"正在重出"
    标在另一段上——那一段用户根本没点过，看起来就是程序自己在乱改别的图。

    就地在**现在这张时间线**上按素材文件找。服务端也算了一份
    （``scene_regen_queue.snapshot`` 的 ``job.scene``），但那是上一次拉队列那
    一刻算的：拖完画面之后下一次轮询之前，它和下标一样是旧的。而这里每次
    重画都重新找一遍，拖完立刻就是对的。

    两边都认不出来（那一段被删了）就返回 null，由调用方说"没有了"，而不是
    退回一个一定属于别人的编号。 */
function jobScene(job) {
  // 身份就是服务端的 ``scene_manifest.scene_id``：有 id 用 id，老清单没有
  // 这个字段时退回素材文件名。还没出图的那几段根本没有文件，只有 id 认得
  // 出它们。
  const key = String(job.scene_id || "");
  if (key) {
    const at = (state.scenes || []).findIndex(
      (scene) => (scene.id || materialKey(scene.material)) === key
    );
    if (at >= 0) return at;
  }
  // 客户端认不出来时（刚出完、时间线还没重读）用服务端那一份。
  return Number.isInteger(job.scene) && (state.scenes || [])[job.scene]
    ? job.scene
    : null;
}

function pendingJobs(index) {
  return (state.queue.jobs || []).filter(
    (job) => jobScene(job) === index
      && (job.state === "queued" || job.state === "running")
  );
}

/** 把一次重生成排进队列。请求立刻回来，所以按钮不用禁。

    ``fromPhoto`` 打开的是另一条画法：先去搜一张真实照片，再照着它画这一
    张。排的是同一个队列——出一张图还是那几分钟，而搜图只多几秒。 */
/* 照一张照片画的时候，这一张要保留多少照片。
 *
 * 两个值，不是滑块。在 Z-Image-Turbo 上拿同一张照片、同一句提示词、同一个
 * 种子量过一整条曲线：0.35-0.60 照片的构图和机型都在，但出来的是照片，提示
 * 词里的画风不起作用；0.90-0.98 画风完全对，而照片什么都没剩——0.95 那次把
 * 一架喷气客机画成了螺旋桨。中间的 0.75（原来的默认值）两头都不是。
 *
 * 所以这里不提供中间值：摆出来等于请用户去挑一个两边都不要的结果。
 */
const KEEP_THE_PHOTO = 0.55;
const KEEP_MY_STYLE = 0.95;
/* 第三种，不在那条曲线上：把照片本身交给一个改图模型，只告诉它换成哪种
 * 画风。照片里每样东西都在原位，画出来的是我的画风——上面那两个值给不了
 * 的"两样都要"。这一段的画面提示词在这里不用：画面就是那张照片。 */
const SAME_PICTURE = "picture";
/* The fourth is not a drawing at all: the photograph itself goes on the scene,
 * fitted to the frame. No model touches it, so it takes a second. */
const AS_IT_IS = "original";
/** A choice that is a way of using the photo, not an amount of it kept. */
const photoWay = (value) =>
  (value === SAME_PICTURE || value === AS_IT_IS ? value : "");

// 上一次选的那个。整个编辑器记着：连着重画好几段的时候，用户的意图通常
// 是同一个，每一段都要重选一遍是白费的点击。
// 一开始是"同一张图，换成我的画风"：照片里的东西原样留着，画风是这条
// 片子的——挑一张照片的人多半要的就是这个。
let photoKeep = SAME_PICTURE;

/** "这一张保留多少照片"的下拉框。 */
function photoKeepPicker() {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const caption = document.createElement("label");
  caption.textContent = "How much of the photo";
  const input = document.createElement("select");
  const choices = [
    [KEEP_THE_PHOTO, "Keep the photograph — the real subject and framing"],
    [KEEP_MY_STYLE, "Loosely inspired — my style exactly"],
    [SAME_PICTURE, "Same picture — redrawn in my style"],
    [AS_IT_IS, "Use the photo itself — the original, not redrawn"],
  ];
  for (const [value, label] of choices) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = label;
    input.appendChild(option);
  }
  input.value = String(photoKeep);
  input.addEventListener("change", () => {
    photoKeep = photoWay(input.value) || Number(input.value) || KEEP_THE_PHOTO;
  });
  wrap.append(caption, input);
  return { wrap, input };
}

/** 下拉框现在选的是哪一种：一个数（保留多少），或者"同一张图"。 */
function photoKeepOf(input) {
  return photoWay(input.value) || Number(input.value) || photoKeep;
}

/** 队列行上那半句："这一张是照着照片画的，还是只借了个意思"。 */
function keptLabel(strength) {
  if (!strength) return "";
  return strength <= 0.7 ? " · keeping the photo" : " · loosely inspired";
}

async function queueRegeneration(index, keyword, imageStyle, fromPhoto, strength,
  photo) {
  try {
    const body = { keyword, image_style: imageStyle };
    if (fromPhoto) {
      body.from_photo = true;
      // 没选就不发：由服务端用配置里那个值，而不是在这里硬写一个默认。
      if (photoWay(strength)) body.follow = strength;
      else if (strength) body.strength = strength;
      // 用户自己挑的那一张（搜索结果里的地址，或者传上来的那张的名字）。
      Object.assign(body, photo || {});
    }
    const chose = !!(photo
      && (photo.photo_url || photo.photo_upload || photo.photo_kept
        || photo.photo_picked));
    const job = await call(
      "POST",
      `${scenePath(index)}/material/queue`,
      body
    );
    const how = !fromPhoto ? ""
      : chose && strength === AS_IT_IS
        ? " The photo you chose goes on it as it is."
      : chose ? " It will be drawn from the photo you chose."
        : " It will find a photo first.";
    say(job && job.position > 1
      ? `Scene ${index + 1} queued — ${job.position - 1} ahead of it.${how}`
      : `Scene ${index + 1} queued. Queue another or keep editing.${how}`);
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
  // 写提示词的活现在也在 jobs 里，所以这里数的是同一份数据。
  const writing = jobs.filter(
    (job) => job.kind === "prompt"
      && (job.state === "queued" || job.state === "running")
  );
  const writingNote = writing.length
    ? `${writing.length} prompt${writing.length === 1 ? "" : "s"} being written`
    : "";
  const summary = running
    ? (running.kind === "narration"
      ? `Recording narration line ${running.line + 1} again`
      : `Regenerating scene ${running.index + 1}`) + ` · ${fmt(running.elapsed)}`
      + (waiting ? ` · ${waiting} waiting` : "")
      + (writingNote ? ` · ${writingNote}` : "")
    : waiting
      ? `${waiting} waiting`
        + (writingNote ? ` · ${writingNote}` : "")
      : writingNote || "Nothing left to regenerate";
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
    // 这一行说的是哪一段。按现在的位置说，所以重排之后它跟着那张图走。
    // 那一段已经不在时间线上了（被删了）就别报一个编号——报出来的一定是
    // 别人的编号。
    const voiced = job.kind === "narration";
    const at = voiced ? null : jobScene(job);
    const where = voiced ? `Narration line ${job.line + 1}`
      : at === null ? "A deleted scene" : `Scene ${at + 1}`;
    // 照着照片画的那条路：搜什么词、照片来自哪个网站，都得写在这一行上。
    // 不写的话用户没法知道那张图是照着什么画的，而这正是他要能回答的问题。
    const photo = job.from_photo
      ? ` · photo: ${job.search || "…"}`
        + (job.reference && job.reference.page_url
          ? ` (${host(job.reference.page_url)})`
          : "")
        // 选的是哪一头也要写上：同一段重画两次、两次选得不一样，事后得看
        // 得出来哪一张是怎么来的。
        + keptLabel(job.strength)
        + (job.follow === AS_IT_IS ? " · the photo itself, not redrawn"
          : job.follow ? " · same picture, my style" : "")
      : "";
    // 写提示词那种活说的是另一件事。两种活排同一条队（它们抢同一块显卡），
    // 所以这一行要分得出来是哪一种，否则"正在出图"会盖在一个根本没在出图
    // 的活上。
    const writingOne = job.kind === "prompt";
    if (voiced) {
      const reason = String(job.error || "unknown error").replace(/\s+/g, " ").trim();
      const by = Number(job.added) || 0;
      label.textContent = `${where}${term} · ` + (
        job.state === "running" ? `recording ${fmt(job.elapsed)}`
          : job.state === "queued"
            ? "waiting to be recorded"
              + (job.position > 1 ? ` (${job.position - 1} ahead)` : " (next)")
            : job.state === "done"
              ? `recorded again in ${fmt(job.elapsed)}`
                + (Math.abs(by) >= 0.05
                  ? `, ${Math.abs(by).toFixed(1)}s ${by > 0 ? "longer" : "shorter"}` : "")
              : `failed: ${reason}`);
      if (job.state === "failed") label.title = String(job.error || "");
    } else if (job.state === "running") {
      label.textContent = `${where}${term}${photo} · `
        + (writingOne
          ? `writing a prompt ${fmt(job.elapsed)}`
          : job.from_photo && !job.reference
            ? `looking for a photo ${fmt(job.elapsed)}`
            : `generating ${fmt(job.elapsed)}`);
    } else if (job.state === "queued") {
      label.textContent = `${where}${term}${photo} · `
        + (writingOne ? "waiting to write the prompt" : "waiting")
        + (job.position > 1 ? ` (${job.position - 1} ahead)` : " (next)");
    } else if (job.state === "done") {
      label.textContent = writingOne
        ? `${where} · prompt written in ${fmt(job.elapsed)}`
        : `${where}${term}${photo} · redrawn in ${fmt(job.elapsed)}`;
    } else {
      // 失败原因是服务端点名过的那一句（哪个源怎么失败的），照原样显示。
      // 只是把换行和连串空格压成一个空格：有些报错自带几行 ##### 分隔线，
      // 照原样塞进这块浮层会把它撑成半屏高。完整那段话在 title 里。
      const reason = String(job.error || "unknown error").replace(/\s+/g, " ").trim();
      label.textContent = `${where}${term}${photo} · failed: ${reason}`;
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
      // 跳走之前先把框里没存的那一笔存掉。
      savePromptEdit();
      if (voiced) {
        if (!((state.data && state.data.narration_segments) || [])[job.line]) return;
        state.line = job.line;
        state.adding = false;
        drawInspector();
        markLine();
        return;
      }
      // 跳到它**现在**在的那一段，不是排队时那一段。
      if (at === null || !state.scenes[at]) return;
      state.selected = at;
      draw();
    });
    ui.queue.appendChild(row);
  }
}

/* ---------- 还没开画 ----------

   出图是整条流水线里最慢最贵的一步：本机一张要几分钟，一条 10 分钟的旁白
   是两百多张、好几个小时。以前用户第一次看到提示词是在图全画完之后——那
   时候改一个字就意味着整批重画。

   所以流水线跑到字幕就停下，把位置摆出来、提示词填进去，旁白和字幕都在，
   用户可以一边听一边改。这条横幅是"改完了，开画"那一步。 */
/** 先试哪一张。

    选中的那一段还没画就试它——用户正看着它的提示词，换成别的那一段会
    让人以为按错了；没选中或者选中的已经有图了，就挑时间线上第一张还没画的。

    只剩一张没画时返回 null：试那一张和全画完是同一件事，多一个按钮只是
    多一个要读的东西。 */
function sampleScene() {
  const waiting = (state.scenes || []).filter(isPending);
  if (waiting.length < 2) return null;
  const picked = (state.scenes || [])[state.selected];
  return picked && isPending(picked) ? picked : waiting[0];
}

function drawApprove() {
  const waiting = (state.scenes || []).filter(isPending);
  // 一张不缺就别占着地方。
  if (!waiting.length) {
    ui.approve.hidden = true;
    return;
  }
  ui.approve.hidden = false;

  const queued = (state.queue.jobs || []).filter(
    (job) => job.kind !== "narration"
      && (job.state === "queued" || job.state === "running")
  ).length;
  const left = Math.max(0, waiting.length - queued);
  // 已经有图的那几段。数它是为了把"不会重画"说出口：先试过一张的人
  // 站在"开画"前最想知道的就是那一张会不会被再画一遍。
  const drawn = Math.max(0, (state.scenes || []).length - waiting.length);

  // 先试一张。最贵的错误不是一张图画坏了，是整批两百张都照着一个不对的
  // 画风、一种不对的提示词写法画完——那要几个小时，而且看得出不对的时候
  // 已经付过钱了。队列里已经有活了就收起来：整批都开了，再说"先试一张"已经晚了。
  const sample = queued ? null : sampleScene();
  const fromPhotos = queued ? 0 : photoQuota().count;

  ui.approveCount.textContent = queued
    ? `Drawing ${queued} picture${queued === 1 ? "" : "s"}…`
    : `${waiting.length} picture${waiting.length === 1 ? "" : "s"} to draw`;
  ui.approveNote.textContent = queued
    ? "They are drawn one at a time. Keep editing the prompts further down "
      + "the timeline — anything not yet started will use what you leave here."
    : "Check the prompts first: click a slot to read the line it plays over "
      + "and change what gets drawn. Nothing is drawn until you say so, and "
      + "each picture takes a few minutes"
      + (sample
        ? ` — so try one first and look at it before you commit `
          + `to all ${waiting.length}.`
        : ".")
      + (drawn
        ? ` The ${drawn} already drawn ${drawn === 1 ? "is" : "are"} left `
          + "alone."
        : "")
      + (fromPhotos
        ? ` You will first choose a photo for up to ${fromPhotos} of them `
          + "— the ones that name a real person, place or thing; nothing is "
          + "queued until all are chosen."
        : "");

  ui.approveOne.hidden = !sample;
  if (sample) {
    ui.approveOne.disabled = !!state.busy;
    const prompt = String(sample.keyword || sample.keyword_suggestion || "").trim();
    ui.approveOne.title = `Draws scene ${sample.index + 1} on its own`
      + (prompt ? `: ${prompt}` : "")
      + ". Nothing else is drawn, so you can look at the style first.";
  }
  ui.approveDraw.disabled = !!state.busy || left === 0;
  // 有人已经画过了就写"剩下的"。写"Draw the pictures"会让人以为连已经
  // 画好的那几张也要重来——而那正是最贵的误会。
  ui.approveDraw.textContent = (drawn || queued)
    ? `Draw the remaining ${left}`
    : "Draw the pictures";
}

/** 只排一张。走的是单段重生成那条路（服务端本来就允许还没出图的那几段进
    队），所以这里不需要新接口：缺的只是一个说得出口的入口。 */
ui.approveOne.addEventListener("click", async () => {
  // 框里可能还有刚改的字（防抖那半秒）。试的就该是用户现在看到的那一笔。
  await savePromptEdit();
  const sample = sampleScene();
  if (!sample) return;
  // 跳到它：图出来了总得知道该看哪一段，而且接下来要改的就是它的提示词。
  state.selected = sample.index;
  draw();
  await queueRegeneration(sample.index, null, null);
});

ui.approveDraw.addEventListener("click", async () => {
  if (state.busy) return;
  await savePromptEdit();
  // 这条片子有几张是要照着照片画的：先把那几张的照片挑完，一张图都不排。
  // 挑完了（见 ``finishPhotoRound``）才连同其余的一起排进队列。
  if (!photoQuota().count) {
    drawPending();
    return;
  }
  // 哪几段：有真人、真地方、真东西的那几段。场景切出来的时候就算好记下
  // 了，这里只是读；那一次没算成才会在这里等模型。
  state.busy = true;
  say("Reading which scenes name a real person, place or thing…");
  let found = null;
  try {
    found = await scenesForPhotos();
  } catch (error) {
    // 评不出来就停在这里：退回"隔几段取一段"是悄悄换了一种挑法。
    say("Could not work out which scenes to draw from photos: "
      + `${String(error.message || error).replace(/[.\s]+$/, "")}. `
      + "Nothing was queued — click again to retry.",
      "error");
    return;
  } finally {
    state.busy = false;
  }
  if (!found.scenes.length) {
    drawPending("No scene left to draw names a real person, place or "
      + "thing, so none is drawn from a photo. ");
    return;
  }
  // 定过照片的不再问：照片存在这条片子里，开画多少次用的都是它。
  const todo = found.scenes.filter((scene) => !scene.photo_pick);
  if (!todo.length) {
    drawPending();
    return;
  }
  photoRound.short = found.scenes.length < found.asked
    ? `Only ${found.scenes.length} of the ${found.asked} scenes asked for name `
      + "something real. "
    : "";
  startPhotoRound(todo);
});

/** 把还没画的全排进队列。定过照片的那几段照着各自那张照片画（先排），
    其余的照提示词画——哪一段用哪张照片是服务端记着的，这里不用带。 */
function drawPending(first) {
  return run("Queueing the pictures", async () => {
  // 开画读的是清单里存着的提示词。框里还有没落盘的那一笔，现在存——
  // 两百张照旧词画完是这一屏最贵的一种错。
  await savePromptEdit();
  const result = await call(
    "POST", `/tasks/${encodeURIComponent(state.taskId)}/scenes/draw`
  );
  await fetchQueue();
  watchQueue();
  const count = (result && result.queued) || 0;
  const photos = (result && result.from_photos) || 0;
  return `${first || ""}${count} picture${count === 1 ? "" : "s"} queued`
    + (photos ? `, ${photos} of them from the photos you chose` : "")
    + ". They are drawn one at a time; you can keep editing or close this "
    + "page and come back.";
  });
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
  if (scene && scene.held > 0.05) {
    return `Scene ${index + 1} is now ${scene.held.toFixed(1)}s longer than its clip; `
      + "the clip's last frame stays on screen for that time.";
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
  // 框的大小变了，字号和位置都是按它算的，要重算一次——否则缩放窗口之后
  // 字会停在旧的位置上，甚至跑到画面外面。
  drawSubtitle();
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
    chips.push([off ? "no image motion (static)"
      : `image motion: ${(MOTION_LABELS[motion] || motion).toLowerCase()}`, off]);
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
  drawEditBar();
  drawProjectTitle();
  drawRuler();
  drawClips();
  drawVoice();
  drawMusicTrack();
  drawDirectionTrack();
  drawInspector();
  drawTotals();
  paintLimit();
  drawSettings();
  drawQueue();
  drawApprove();
  drawSubtitle();
  drawVideoButton();
  const ready = state.scenes.length > 0;
  ui.play.disabled = !ready;
  // 还有图没出完就不让开渲：渲染器要读的正是队列那边还在换的那几个文件，
  // 渲出来会是新旧画面混在一起的一条片子。接口那边也会拒,这里只是不让
  // 用户白点一次。
  // 还有位置没画就不让开渲：渲出来会是一条缺着好几段画面的片子。接口那边
  // 也会拒并点名还差几张,这里只是不让用户白点一次。
  const undrawn = (state.scenes || []).some(isPending);
  ui.render.disabled = !ready
    || undrawn
    || !!state.queue.busy
    || !!(state.data && state.data.audio_stale);
  ui.render.title = undrawn
    ? "Some scenes have no picture yet — draw them first"
    : "";
  ui.rerecord.disabled = !state.data || !state.data.audio_stale;
  ui.scriptToggle.disabled = !state.data;
  ui.musicToggle.disabled = !state.data;
  ui.subsToggle.disabled = !state.data;
  ui.effectsToggle.disabled = !state.data;
  ui.publishToggle.disabled = !state.data;
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
    // "还没画" 和 "文件没了" 是两回事。前者是正常状态——用户正在这一屏
    // 看提示词，一张图都还没开画；把它标成 missing 等于在说出了问题。
    const waiting = isPending(scene);
    clip.className = "clip"
      + (index === state.selected ? " selected" : "")
      + (scene.locked ? " locked" : "")
      + (waiting ? " pending-draw" : scene.exists ? "" : " missing");
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
    // 后半截（时长和状态）存在节点上：提示词边打边存时要就地换掉前半截，
    // 而不能重画整条时间线——重画会把正在打字的框连光标一起换掉。
    text.dataset.suffix = ` · ${span.toFixed(1)}s`
      + (waiting ? " · to draw" : scene.exists ? "" : " · missing");
    text.textContent = `${index + 1}. ${scene.keyword || scene.material || ""}`
      + text.dataset.suffix;
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

    // Marked to become a video, or already one. On the track because that is
    // where scenes are chosen: marking twenty of them should not mean
    // remembering which twenty.
    if (scene.animate || scene.video) {
      if (scene.animate) clip.classList.add("to-video");
      const tag = document.createElement("div");
      tag.className = "clip-badge video";
      tag.textContent = scene.animate ? "→ video" : "video";
      clip.appendChild(tag);
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

/* ---------- 画过的每一张图 ----------

   为什么这一屏必须存在：重生成是**换掉**，不是修改。用户看着一张"还行但不是
   最好"的图点一次重出，拿回来一张更差的——而上一张从此不在任何界面上。

   它从来没丢。重生成只是把这一段的 ``material`` 指向新文件，旧文件一直躺在
   任务目录里（服务端没有一处删除场景素材的代码）。所以这里不重画任何东西，
   只是把目录里的东西摆出来让人点回去：几秒钟，不排队，不占显卡。

   连带的好处是它对**老任务也有效**：历史记录是从今天起才记的，而目录里那些
   被换下来的图早就在了。 */

const shelf = { at: -1, pictures: [], loading: false, error: "", show: "all" };

function openPictureShelf(scene) {
  shelf.at = scene.index;
  shelf.pictures = [];
  shelf.error = "";
  shelf.show = "all";
  ui.shelfTitle.textContent = `Pictures and videos for scene ${scene.index + 1}`;
  ui.shelfNote.textContent = "Every picture and video clip this video has made. "
    + "Each one is labelled Picture or Video; point at a video to play it. Click "
    + "one to put it on this scene — nothing is made again, so it is instant, "
    + "and what you are replacing is kept here too.";
  ui.shelfStatus.textContent = "";
  ui.shelfGrid.innerHTML = "";
  ui.shelfModal.hidden = false;
  fetchPictures();
}

function closePictureShelf() {
  ui.shelfModal.hidden = true;
  shelf.at = -1;
}

async function fetchPictures() {
  if (shelf.loading) return;
  shelf.loading = true;
  shelf.error = "";
  ui.shelfStatus.textContent = "Looking through the folder…";
  try {
    const data = await call(
      "GET", `/tasks/${encodeURIComponent(state.taskId)}/pictures`);
    shelf.pictures = (data && data.pictures) || [];
  } catch (error) {
    // 读不出来和"一张都没有"是两回事。说成后者，用户会以为那些图真的没了。
    shelf.pictures = [];
    shelf.error = String(error.message || error);
  } finally {
    shelf.loading = false;
    drawShelf();
  }
}

/** All, pictures only, or videos only, with how many there are of each. The
    videos of a long video are otherwise somewhere under dozens of pictures. */
function drawShelfFilter() {
  ui.shelfFilter.innerHTML = "";
  const videos = shelf.pictures.filter(isVideoTile).length;
  ui.shelfFilter.hidden = !shelf.pictures.length;
  const choices = [
    ["all", `All (${shelf.pictures.length})`],
    ["picture", `Pictures (${shelf.pictures.length - videos})`],
    ["video", `Videos (${videos})`],
  ];
  for (const [value, label] of choices) {
    const one = document.createElement("button");
    one.className = "btn small" + (shelf.show === value ? " primary" : "");
    one.textContent = label;
    one.dataset.show = value;
    one.addEventListener("click", () => {
      shelf.show = value;
      drawShelf();
    });
    ui.shelfFilter.appendChild(one);
  }
}

function drawShelf() {
  ui.shelfGrid.innerHTML = "";
  drawShelfFilter();
  if (!shelf.pictures.length) {
    const empty = document.createElement("p");
    empty.className = "shelf-empty";
    empty.textContent = shelf.error
      ? `Could not read this video's folder: ${shelf.error}`
      : shelf.loading
        ? "Looking through the folder…"
        : "This video has no pictures or videos on disk yet.";
    ui.shelfStatus.textContent = "";
    ui.shelfGrid.appendChild(empty);
    return;
  }

  // 没人用的那几张排在前面：用户来这一屏就是为了找它们。当前这一段正用着的
  // 那一张排在最前,它是"认出别的几张"的参照物。
  const mine = shelf.pictures.filter((one) => one.in_use === shelf.at);
  const free = shelf.pictures.filter((one) => one.in_use === null
    || one.in_use === undefined);
  const elsewhere = shelf.pictures.filter(
    (one) => one.in_use !== null && one.in_use !== undefined
      && one.in_use !== shelf.at);

  const shown = [...mine, ...free, ...elsewhere].filter((tile) =>
    shelf.show === "all" || (shelf.show === "video") === isVideoTile(tile));
  for (const tile of shown) {
    ui.shelfGrid.appendChild(pictureTile(tile));
  }
  if (!shown.length) {
    const none = document.createElement("p");
    none.className = "shelf-empty";
    none.textContent = shelf.show === "video"
      ? "This video has no video clips yet. Animate a picture and it shows up here."
      : "This video has no pictures on disk.";
    ui.shelfGrid.appendChild(none);
  }

  const count = (many, word) => `${many} ${word}${many === 1 ? "" : "s"}`;
  const videos = free.filter(isVideoTile).length;
  const stills = free.length - videos;
  ui.shelfStatus.textContent = free.length
    ? [stills ? count(stills, "picture") : "", videos ? count(videos, "video") : ""]
      .filter(Boolean).join(" and ") + " not on the timeline."
    : "Everything here is on the timeline.";
}

/** How a generated clip was made, "image" or "text", or "". The server
    says; an older server that does not is read from the file name, which
    carries the same thing. */
function tileVideoMode(tile) {
  if (tile.video_mode !== undefined) return tile.video_mode || "";
  const named = /^wan-(image|text)-/i.exec(tile.material || "");
  return named ? named[1].toLowerCase() : "";
}

function isVideoTile(tile) {
  if (tile.media) return tile.media === "video";
  // An older server: a picture is stored as a clip too, named by the digest
  // of the image it was baked from, and that name is how to tell.
  const name = String(tile.material || "");
  if (/\.(png|jpe?g|webp|bmp)$/i.test(name)) return false;
  return !/^[0-9a-f]{64}\./i.test(name);
}

/** What a tile is, in words: the label on its corner and the line under it. */
function tileMedia(tile) {
  if (!isVideoTile(tile)) return { label: "Picture", detail: "" };
  const mode = tileVideoMode(tile);
  if (mode === "image") return { label: "Video", detail: "animated picture" };
  if (mode === "text") return { label: "Video", detail: "made from the prompt" };
  return { label: "Video", detail: "footage" };
}

function pictureTile(tile) {
  const here = tile.in_use === shelf.at;
  const spare = tile.in_use === null || tile.in_use === undefined;
  const node = document.createElement("button");
  const moving = isVideoTile(tile);
  const media = tileMedia(tile);
  node.className = "shelf-tile " + (here ? "in-use" : spare ? "free" : "")
    + (moving ? " is-video" : " is-picture");
  node.dataset.material = tile.material;
  node.dataset.media = moving ? "video" : "picture";

  const frame = document.createElement("div");
  frame.className = "shelf-frame";
  const shot = document.createElement("img");
  shot.className = "shelf-shot";
  shot.alt = "";
  // 大一点的那一帧：72 像素放大到一格的宽度全是糊的，而这一屏的全部意义
  // 就是让人认出画面。
  requestThumb({ url: tile.url, trim_start: 0 }, shot, 160);
  frame.appendChild(shot);

  // Said in words on the picture itself: a still frame of a video looks
  // exactly like the picture it was made from.
  const badge = document.createElement("span");
  badge.className = "shelf-media";
  badge.textContent = moving ? "▶ Video" : "Picture";
  frame.appendChild(badge);
  node.appendChild(frame);

  if (moving) {
    // Pointing at a video plays it in place, silently: the motion is the
    // thing being chosen, and one frame does not show it.
    let playing = null;
    node.addEventListener("mouseenter", () => {
      if (playing) return;
      playing = document.createElement("video");
      playing.className = "shelf-shot shelf-playing";
      playing.muted = true;
      playing.loop = true;
      playing.playsInline = true;
      playing.preload = "auto";
      playing.src = tile.url;
      frame.insertBefore(playing, badge);
      playing.play().catch(() => {});
    });
    node.addEventListener("mouseleave", () => {
      if (!playing) return;
      playing.pause();
      playing.removeAttribute("src");
      playing.load();
      playing.remove();
      playing = null;
    });
  }

  const meta = document.createElement("div");
  meta.className = "shelf-meta";

  const where = document.createElement("span");
  where.className = "shelf-where";
  where.textContent = here
    ? "On this scene now"
    : spare
      ? "Not used"
      : `On scene ${tile.in_use + 1}`;
  meta.appendChild(where);

  const when = document.createElement("span");
  when.className = "shelf-when";
  when.textContent = (media.detail ? `${media.detail} · ` : "") + madeWhen(tile)
    + (tile.variants > 1 ? ` · ${tile.variants} camera moves` : "");
  meta.appendChild(when);

  if (tile.keyword) {
    const prompt = document.createElement("span");
    prompt.className = "shelf-prompt";
    prompt.textContent = tile.keyword;
    meta.appendChild(prompt);
  }
  node.appendChild(meta);

  // 整段提示词和文件名在 tooltip 里：两张相似的图靠这个分得开，而把它排版
  // 到格子里会把这面墙撑散。
  node.title = [
    media.detail ? `${media.label}: ${media.detail}` : media.label,
    tile.keyword || "",
    tile.image_style ? `Style: ${tile.image_style}` : "",
    tile.reference && tile.reference.page_url
      ? `Drawn from a photo on ${host(tile.reference.page_url)}` : "",
    tile.material,
  ].filter(Boolean).join("\n");

  if (here) {
    node.disabled = true;
  } else {
    node.addEventListener("click", () => putPictureBack(tile));
  }
  return node;
}

/** 这一张是什么时候落地的。相对时间比时间戳好认："20 分钟前"就是刚才换掉的
    那一张,而 "2026-11-14 19:03" 要用户自己去算。 */
function madeWhen(tile) {
  const seconds = Math.max(0, Date.now() / 1000 - Number(tile.made_at || 0));
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function putPictureBack(tile) {
  const at = shelf.at;
  if (at < 0) return;
  closePictureShelf();
  const what = isVideoTile(tile) ? "video" : "picture";
  run(`Putting that ${what} back`, async () => {
    const scene = await call("POST", `${scenePath(at)}/restore`,
      { material: tile.material });
    // 这一张当时是照哪句提示词画的。不自动盖掉框里的字——那是用户正在写的
    // 东西，而他要的只是那张图。说出来，让他自己决定。
    const was = scene && scene.restored_keyword;
    if (was && was !== (scene.keyword || "")) {
      return `Scene ${at + 1} is back on that ${what}. It was made from: `
        + `"${was}" — the prompt box still has what you had.`;
    }
    return `Scene ${at + 1} is back on that ${what}.`;
  });
}

ui.shelfClose.addEventListener("click", closePictureShelf);
// 点挡板的暗处关掉，和别处一样；点盒子里面不该关。
ui.shelfModal.addEventListener("click", (event) => {
  if (event.target === ui.shelfModal) closePictureShelf();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !ui.shelfModal.hidden) closePictureShelf();
});

/* ---------- 开一条新片子 ----------

   以前这一步在另一个页面（WebUI）。现在编辑器自己能开：填主题，点创建，
   流水线跑到"该让人看一眼"的地方停下，时间线就在这里打开。

   表单不是手写的。有哪几页、每页哪些项、能挑什么、默认是什么，全是服务端
   给的（``/new-video/options``）——默认值就是上次用的那一套。页面里不留
   第二份清单：留了，迟早和服务端认的名字对不上。 */
const fresh = {
  form: null, values: {}, tab: "", aspectTouched: false, writing: false,
  voices: null, files: {}, sample: null, lastVoice: {}, more: {},
};
const CREATING_KEY = "mpt_creating_task";
const PHASES = {
  queued: "Waiting for its turn",
  script: "Writing the script",
  terms: "Writing a picture prompt for each line",
  audio: "Recording the narration",
  subtitle: "Timing the subtitles",
  materials: "Getting the pictures",
  downloading: "Getting the pictures",
};

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

/** 传文件。``call`` 只会发 JSON，文件得走 multipart。 */
async function upload(path, file) {
  const body = new FormData();
  body.append("file", file);
  const headers = {};
  const key = apiKey();
  if (key) headers["x-api-key"] = key;
  const response = await fetch(API + path, { method: "POST", headers, body });
  let payload = null;
  try { payload = await response.json(); } catch { /* 空响应体 */ }
  if (!response.ok) {
    throw new Error((payload && (payload.message || payload.detail))
      || response.statusText);
  }
  return payload ? payload.data : null;
}

/** 这一项现在该不该出现。``show`` 是 {别的项: [让它出现的那些值]}。 */
function fieldShown(field, values) {
  for (const [name, wanted] of Object.entries(field.show || {})) {
    if (!wanted.includes(values[name])) return false;
  }
  return true;
}

function fillSelect(node, options, labelOf) {
  const groups = new Map();
  for (const one of options || []) {
    const item = option(String(one.id), labelOf ? labelOf(one) : one.label);
    if (one.group) {
      if (!groups.has(one.group)) {
        const box = document.createElement("optgroup");
        box.label = one.group;
        groups.set(one.group, box);
        node.appendChild(box);
      }
      groups.get(one.group).appendChild(item);
    } else {
      node.appendChild(item);
    }
  }
}

/** 下拉框里的值都是字符串；把它还原成服务端给的那个类型（数字、布尔）。 */
function optionValue(field, text) {
  const hit = (field.options || []).find((one) => String(one.id) === text);
  return hit ? hit.id : text;
}

/** 按种类画一项。``changed(value, redraw)``：redraw 为真表示别的项的显隐
    可能跟着变，整页要重画；打字和拖滑块不重画，否则焦点会丢。 */
/** 一种画风的第 n 张样图。第一张没有后缀，早先只有它一张。 */
function styleSample(id, n) {
  return `styles/${encodeURIComponent(id)}${n > 1 ? "-" + n : ""}.jpg`;
}

const STYLE_SAMPLE_MS = 1200;
/* 随页面带着的样图，每种画风几张。 */
const STYLE_SAMPLES = 3;

/** 卡片上的样图。平时是第一张；鼠标停上去（或者键盘聚焦）就一张张换下去，
    让人多看几个例子，移开回到第一张。哪张图没有：第一张没有就只剩名字，
    后面的没有就不换了——卡片照样能点。 */
function showStyleSamples(card, frame, id, count) {
  const picture = document.createElement("img");
  picture.alt = "";
  picture.loading = "lazy";
  picture.src = styleSample(id, 1);
  frame.appendChild(picture);

  const dots = document.createElement("span");
  dots.className = "style-dots";
  for (let n = 0; n < count; n += 1) dots.appendChild(document.createElement("i"));
  if (count > 1) frame.appendChild(dots);

  let at = 1;
  let timer = null;
  const show = (n) => {
    at = n;
    picture.src = styleSample(id, n);
    [...dots.children].forEach((dot, i) => dot.classList.toggle("on", i === n - 1));
  };
  const stop = () => {
    if (timer) clearInterval(timer);
    timer = null;
    card.classList.remove("looking");
    if (at !== 1) show(1);
  };
  const start = () => {
    if (timer || count < 2) return;
    // 先取回来，换的时候才不闪。
    for (let n = 2; n <= count; n += 1) new Image().src = styleSample(id, n);
    card.classList.add("looking");
    // 一停上去就换到第二张：等上一秒多才动，人会以为没有别的例子。
    show(2);
    timer = setInterval(() => {
      // 表单重画过，这张卡片已经不在页面上了。
      if (!card.isConnected) { clearInterval(timer); timer = null; return; }
      show(at % count + 1);
    }, STYLE_SAMPLE_MS);
  };
  picture.addEventListener("error", () => {
    if (at === 1) { picture.remove(); dots.remove(); count = 1; }
    else { count = 1; dots.remove(); stop(); }
  });
  card.addEventListener("mouseenter", start);
  card.addEventListener("mouseleave", stop);
  card.addEventListener("focus", start);
  card.addEventListener("blur", stop);
}

/** 画风：一张样图一个名字，点哪张用哪张。样图是同一个画面按每种画风各画
    的一张（``styles/<id>.jpg``），所以卡片之间比的只有画风。"照设置"那张
    用设置里那个画风的图；哪张图没有，就只剩名字，照样能点。 */
function styleCards(field, value, changed) {
  const grid = document.createElement("div");
  grid.className = "style-cards";
  grid.setAttribute("role", "radiogroup");
  for (const one of field.options || []) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "style-card" + (one.id === String(value ?? "") ? " on" : "");
    card.setAttribute("role", "radio");
    card.setAttribute("aria-checked", one.id === String(value ?? "") ? "true" : "false");
    const shown = one.id || field.settings_style || "";
    const name = one.id ? styleLabel(one.id) : one.label;
    card.title = one.id || !shown ? name : `${name}: ${styleLabel(shown)}`;
    const frame = document.createElement("span");
    frame.className = "style-thumb";
    // 服务端没说有几张（还没重启的旧进程）就按随页面带的那几张算；哪张
    // 真的没有，``showStyleSamples`` 自己会停。
    if (shown) {
      showStyleSamples(card, frame, shown, Number(field.samples) || STYLE_SAMPLES);
    }
    const label = document.createElement("span");
    label.className = "style-name";
    label.textContent = name;
    card.append(frame, label);
    card.addEventListener("click", () => changed(one.id, true));
    grid.appendChild(card);
  }
  return grid;
}

function formField(field, value, changed) {
  const box = document.createElement("div");
  box.className = "field";
  const wide = ["textarea", "choice", "materials", "audio", "music", "voice",
    "styles"];
  if (wide.includes(field.kind) || field.wide) box.classList.add("wide");

  if (field.kind === "bool") {
    // 开关两个一排；只有"我知道要收费"那种要人停下来看的才独占一行。
    if (field.warn) box.classList.add("wide");
    box.classList.add("switch");
    const label = document.createElement("label");
    label.className = "check" + (field.warn ? " warn" : "");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!value;
    input.addEventListener("change", () => changed(input.checked, true));
    label.append(input, document.createTextNode(" " + field.label));
    box.appendChild(label);
    return box;
  }

  const label = document.createElement("label");
  label.textContent = field.label;
  box.appendChild(label);

  if (field.kind === "select") {
    const input = document.createElement("select");
    fillSelect(input, field.options,
      field.style_labels ? (one) => (one.id ? styleLabel(one.id) : one.label) : null);
    input.value = String(value ?? "");
    input.addEventListener("change",
      () => changed(optionValue(field, input.value), true));
    box.appendChild(input);
  } else if (field.kind === "styles") {
    box.appendChild(styleCards(field, value, changed));
  } else if (field.kind === "choice") {
    const row = document.createElement("div");
    row.className = "choice";
    for (const one of field.options || []) {
      const node = document.createElement("button");
      node.type = "button";
      node.textContent = one.label;
      node.className = one.id === value ? "on" : "";
      node.addEventListener("click", () => changed(one.id, true));
      row.appendChild(node);
    }
    box.appendChild(row);
  } else if (field.kind === "range") {
    const shown = document.createElement("span");
    shown.className = "soft";
    const say = (v) => {
      shown.textContent = field.percent
        ? ` ${Math.round(v * 100)}%` : ` ${Number(v).toFixed(2)}${field.unit || ""}`;
    };
    label.appendChild(shown);
    const input = document.createElement("input");
    input.type = "range";
    input.min = field.min; input.max = field.max; input.step = field.step;
    input.value = value ?? field.min;
    say(input.value);
    input.addEventListener("input", () => {
      say(input.value);
      changed(Number(input.value), false);
    });
    box.appendChild(input);
  } else if (field.kind === "number") {
    const input = document.createElement("input");
    input.type = "number";
    if (field.min !== undefined) input.min = field.min;
    if (field.max !== undefined) input.max = field.max;
    if (field.step !== undefined) input.step = field.step;
    input.value = value ?? "";
    input.addEventListener("input",
      () => changed(input.value === "" ? null : Number(input.value), false));
    box.appendChild(input);
  } else if (field.kind === "color") {
    const input = document.createElement("input");
    input.type = "color";
    input.value = /^#[0-9a-f]{6}$/i.test(value || "") ? value : "#000000";
    input.addEventListener("input", () => changed(input.value, false));
    box.appendChild(input);
  } else if (field.kind === "textarea") {
    const input = document.createElement("textarea");
    input.rows = 3;
    input.placeholder = field.placeholder || "";
    input.value = value ?? "";
    input.addEventListener("input", () => changed(input.value, false));
    box.appendChild(input);
  } else if (field.kind === "voice") {
    box.appendChild(voiceControl(value, changed));
  } else if (["materials", "audio", "music"].includes(field.kind)) {
    box.appendChild(uploadControl(field, value, changed));
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = field.placeholder || "";
    input.value = value ?? "";
    input.addEventListener("input", () => changed(input.value, false));
    box.appendChild(input);
  }
  if (field.help) {
    const help = document.createElement("p");
    help.className = "hint";
    help.textContent = field.help;
    box.appendChild(help);
  }
  return box;
}

/** 音色：按语言分组的下拉框，加一个"听一下"。 */
function voiceControl(value, changed) {
  const wrap = document.createElement("div");
  const row = document.createElement("div");
  row.className = "with-button";
  const input = document.createElement("select");
  const data = fresh.voices || { groups: [], notes: [] };
  for (const group of data.groups || []) {
    const box = document.createElement("optgroup");
    box.label = group.label;
    for (const voice of group.voices || []) {
      box.appendChild(option(voice.id, voice.label));
    }
    input.appendChild(box);
  }
  input.value = value || "";
  input.addEventListener("change", () => changed(input.value, false));
  const listen = document.createElement("button");
  listen.type = "button";
  listen.className = "btn small";
  listen.textContent = "Listen";
  listen.addEventListener("click", () => playVoiceSample(listen));
  row.append(input, listen);
  wrap.appendChild(row);
  for (const note of data.notes || []) {
    const line = document.createElement("p");
    line.className = "hint warn";
    line.textContent = note;
    wrap.appendChild(line);
  }
  return wrap;
}

async function playVoiceSample(button) {
  const values = fresh.values;
  if (!values.voice_name) {
    newVideoProblem("Choose a voice first.");
    return;
  }
  if (fresh.sample) { fresh.sample.pause(); fresh.sample = null; }
  button.disabled = true;
  button.textContent = "Reading…";
  try {
    const headers = { "Content-Type": "application/json" };
    const key = apiKey();
    if (key) headers["x-api-key"] = key;
    const response = await fetch(API + "/new-video/voice-preview", {
      method: "POST",
      headers,
      body: JSON.stringify({
        voice_name: values.voice_name,
        voice_rate: Number(values.voice_rate) || 1,
        voice_volume: Number(values.voice_volume) || 1,
        // 有脚本就念脚本的开头：听的是"它念我这段话"的样子。
        text: ui.newScript.value.trim().slice(0, 240),
      }),
    });
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch { /* 不是 JSON */ }
      throw new Error((payload && payload.message) || response.statusText);
    }
    const sound = new Audio(URL.createObjectURL(await response.blob()));
    fresh.sample = sound;
    await sound.play();
    ui.newStatus.textContent = "";
  } catch (error) {
    newVideoProblem(String(error.message || error));
  } finally {
    button.disabled = false;
    button.textContent = "Listen";
  }
}

/** 三种上传：自己的素材（可以好几个）、自己的配音、自己的配乐。
    文件一选就传，服务端当场验；表单里记的是它给回来的名字。 */
function uploadControl(field, value, changed) {
  const many = field.kind === "materials";
  const route = {
    materials: "/video_materials", audio: "/new-video/audio", music: "/musics",
  }[field.kind];
  const wrap = document.createElement("div");
  wrap.className = "files";
  const names = fresh.files;
  const held = many ? (value || []) : (value ? [value] : []);
  for (const stored of held) {
    const line = document.createElement("div");
    line.className = "file";
    const text = document.createElement("span");
    text.textContent = names[stored] || stored;
    const drop = document.createElement("button");
    drop.type = "button";
    drop.textContent = "×";
    drop.title = "Remove";
    drop.addEventListener("click", () =>
      changed(many ? held.filter((one) => one !== stored) : "", true));
    line.append(text, drop);
    wrap.appendChild(line);
  }
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = many;
  input.accept = field.accept
    || (many ? "video/*,image/*" : "audio/*");
  input.addEventListener("change", async () => {
    const chosen = [...input.files];
    if (!chosen.length) return;
    ui.newStatus.className = "hint pending";
    let current = many ? [...held] : "";
    try {
      for (const [at, file] of chosen.entries()) {
        ui.newStatus.textContent = chosen.length > 1
          ? `Uploading ${at + 1} of ${chosen.length}…` : `Uploading ${file.name}…`;
        const data = await upload(route, file);
        const stored = data && data.file;
        if (!stored) throw new Error("the upload was not accepted");
        names[stored] = file.name;
        current = many ? [...current, stored] : stored;
      }
      ui.newStatus.textContent = "";
    } catch (error) {
      newVideoProblem(String(error.message || error));
    }
    changed(current, true);
  });
  wrap.appendChild(input);
  return wrap;
}

async function openNewVideo() {
  ui.newModal.hidden = false;
  ui.newStatus.textContent = "";
  ui.newStatus.className = "hint";
  if (!fresh.form || fresh.stale) {
    ui.newCreate.disabled = true;
    ui.newWrite.disabled = true;
    ui.newStatus.textContent = "Reading your settings…";
    try {
      fresh.form = await call("GET", "/new-video/options");
      fresh.values = { ...(fresh.form.values || {}) };
      fresh.voices = fresh.form.voices || null;
      fresh.tab = (lookTabs()[0] || {}).id || "";
      fresh.aspectTouched = false;
      fillNewVideo();
      ui.newStatus.textContent = "";
      // 还缺东西就现在说（比如 Ollama 没起来、挑不了模型），别等跑到一半。
      const missing = fresh.form.setup_problems || [];
      // 缺的可能马上就补上了（把 Ollama 启动起来），所以下次打开重新问。
      fresh.stale = missing.length > 0;
      if (missing.length) newVideoProblem(missing.join(" "));
    } catch (error) {
      newVideoProblem(`Could not read the options: ${error.message || error}. `
        + "If you have just updated, the API needs restarting.");
      return;
    }
  }
  ui.newCreate.disabled = false;
  ui.newWrite.disabled = false;
  ui.newSubject.focus();
}

function closeNewVideo() {
  ui.newModal.hidden = true;
  if (fresh.sample) { fresh.sample.pause(); fresh.sample = null; }
}

function fillNewVideo() {
  ui.newLanguage.innerHTML = "";
  ui.newLanguage.appendChild(option("", "Auto"));
  for (const code of fresh.form.languages || []) {
    ui.newLanguage.appendChild(option(code, code));
  }
  ui.newLanguage.value = fresh.values.video_language || "";
  ui.newParagraphs.value = fresh.values.paragraph_number || 1;
  if (!fresh.tab) fresh.tab = (lookTabs()[0] || {}).id || "";
  drawNewVideo();
}

/** 右边的页签。``side`` 的那一页不在这里：它收在左边脚本框底下。 */
function lookTabs() {
  return ((fresh.form && fresh.form.tabs) || []).filter((tab) => !tab.side);
}

function newField(field) {
  return formField(field, fresh.values[field.name],
    (value, redraw) => setNewValue(field.name, value, redraw));
}

function drawNewVideo() {
  // 左边：脚本的那几项不常用的设置。
  const side = ((fresh.form && fresh.form.tabs) || []).find((tab) => tab.side);
  ui.newScriptMore.hidden = !side;
  ui.newScriptFields.innerHTML = "";
  if (side) {
    ui.newScriptMoreLabel.textContent = side.label;
    for (const field of side.fields || []) {
      if (fieldShown(field, fresh.values)) {
        ui.newScriptFields.appendChild(newField(field));
      }
    }
  }

  // 右边：页签，和当前那一页。
  ui.newTabs.innerHTML = "";
  for (const tab of lookTabs()) {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = tab.label;
    node.className = tab.id === fresh.tab ? "on" : "";
    node.addEventListener("click", () => { fresh.tab = tab.id; drawNewVideo(); });
    ui.newTabs.appendChild(node);
  }
  ui.newFields.innerHTML = "";
  const tab = lookTabs().find((one) => one.id === fresh.tab);
  if (!tab) return;
  const shown = (tab.fields || []).filter((field) => fieldShown(field, fresh.values));
  const usual = shown.filter((field) => !field.advanced);
  const extra = shown.filter((field) => field.advanced);

  const top = document.createElement("div");
  top.className = "form-fields";
  for (const field of usual) top.appendChild(newField(field));
  ui.newFields.appendChild(top);
  if (!extra.length) return;

  // 平时要动的就上面那几样；其余的收在这里。收起来的照样生效、照样发出去。
  const open = !!fresh.more[tab.id];
  const line = document.createElement("div");
  line.className = "more-line";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "more-toggle" + (open ? " open" : "");
  toggle.textContent = open ? "Fewer settings" : `More settings (${extra.length})`;
  toggle.addEventListener("click", () => {
    fresh.more[tab.id] = !open;
    drawNewVideo();
  });
  line.appendChild(toggle);
  if (open) {
    const rest = document.createElement("div");
    rest.className = "form-fields";
    for (const field of extra) rest.appendChild(newField(field));
    line.appendChild(rest);
  }
  ui.newFields.appendChild(line);
}

/** 改了一项。有几项牵着别的项走，在这里一并处理。 */
function setNewValue(name, value, redraw) {
  if (name === "tts_server") {
    // 记下在上一家挑的是谁：换过去看一眼再换回来，不该丢了原来那个音色。
    fresh.lastVoice[fresh.values.tts_server] = fresh.values.voice_name;
  }
  fresh.values[name] = value;
  if (name === "video_aspect") fresh.aspectTouched = true;
  if (name === "video_source") {
    // 画幅是按来源分别记的，换来源时跟着换——除非用户已经自己挑过。
    const saved = (fresh.form.saved_aspects || {})[value];
    if (saved && !fresh.aspectTouched) fresh.values.video_aspect = saved;
    // 换了一家收费的，上一家点的"我知道要收费"不算数。
    fresh.values.confirm_charge = false;
  }
  if (name === "tts_server") loadVoices(value);
  if (redraw) drawNewVideo();
}

/** 换了配音服务：去拿那一家的音色。拿不到就照实说，不留着上一家的清单。 */
async function loadVoices(server) {
  const language = ui.newLanguage.value || "";
  fresh.voices = { groups: [], notes: ["Reading the voices…"] };
  drawNewVideo();
  try {
    const query = `server=${encodeURIComponent(server)}`
      + `&language=${encodeURIComponent(language)}`
      + `&keep=${encodeURIComponent(fresh.lastVoice[server] || "")}`;
    fresh.voices = await call("GET", `/new-video/voices?${query}`);
  } catch (error) {
    fresh.voices = { groups: [], notes: [String(error.message || error)] };
  }
  // 还停在这一家才动：慢的那次回来时用户可能已经换到别家了。
  if (fresh.values.tts_server !== server) return;
  const all = (fresh.voices.groups || []).flatMap((group) => group.voices || []);
  if (!all.some((voice) => voice.id === fresh.values.voice_name)) {
    const before = fresh.lastVoice[server];
    fresh.values.voice_name = all.some((voice) => voice.id === before)
      ? before : (all.length ? all[0].id : "");
  }
  drawNewVideo();
}

/** 交给服务端的那一份：主题、脚本，加上此刻**该出现**的每一项。
    收起来的项不发——库存视频没有画风，发了只会被拒绝；没发的用上次的。 */
function newVideoChoices() {
  const choices = {
    video_subject: ui.newSubject.value.trim(),
    video_script: ui.newScript.value.trim(),
    video_language: ui.newLanguage.value,
    paragraph_number: Number(ui.newParagraphs.value) || 1,
  };
  for (const tab of (fresh.form && fresh.form.tabs) || []) {
    for (const field of tab.fields || []) {
      if (!fieldShown(field, fresh.values)) continue;
      const value = fresh.values[field.name];
      if (value === null || value === undefined) continue;
      choices[field.name] = value;
    }
  }
  return choices;
}

function newVideoProblem(message) {
  ui.newStatus.className = "hint warn";
  ui.newStatus.textContent = message;
}

/** 先把脚本写出来给人看。写的是模型，所以要等几秒到几十秒。 */
async function writeNewScript() {
  if (fresh.writing) return;
  const choices = newVideoChoices();
  if (!choices.video_subject) {
    newVideoProblem("Say what the video is about first.");
    ui.newSubject.focus();
    return;
  }
  if (ui.newScript.value.trim()
      && !window.confirm("Replace the script that is already in the box?")) {
    return;
  }
  fresh.writing = true;
  ui.newWrite.disabled = true;
  ui.newCreate.disabled = true;
  ui.newStatus.className = "hint pending";
  ui.newStatus.textContent = "Writing the script…";
  try {
    const data = await call("POST", "/new-video/script", choices);
    ui.newScript.value = (data && data.video_script) || "";
    ui.newStatus.className = "hint";
    ui.newStatus.textContent = "Read it through and change anything you like.";
  } catch (error) {
    newVideoProblem(String(error.message || error));
  } finally {
    fresh.writing = false;
    ui.newWrite.disabled = false;
    ui.newCreate.disabled = false;
  }
}

async function createNewVideo() {
  const choices = newVideoChoices();
  if (!choices.video_subject && !choices.video_script
      && !String(choices.scene_plan || "").trim()) {
    newVideoProblem("Say what the video is about, or paste a script.");
    ui.newSubject.focus();
    return;
  }
  ui.newCreate.disabled = true;
  ui.newStatus.className = "hint pending";
  ui.newStatus.textContent = "Starting…";
  try {
    const task = await call("POST", "/new-video", choices);
    const taskId = task && task.task_id;
    if (!taskId) throw new Error("the API did not say which video it started");
    try { localStorage.setItem(CREATING_KEY, taskId); } catch { /* 记不住也能做 */ }
    closeNewVideo();
    closeLibrary();
    // 下一条从空表开始。选项留着，连着做几条时不用再挑一遍；但只管这一条
    // 的那几样（传上来的文件、"我知道要收费"）不留。
    ui.newSubject.value = "";
    ui.newScript.value = "";
    ui.newStatus.textContent = "";
    Object.assign(fresh.values, {
      confirm_charge: false, local_materials: [], custom_audio: "", video_terms: "",
    });
    watchCreation(taskId, choices.video_subject);
  } catch (error) {
    newVideoProblem(String(error.message || error));
  } finally {
    ui.newCreate.disabled = false;
  }
}

function forgetCreation(taskId) {
  try {
    if (localStorage.getItem(CREATING_KEY) === taskId) {
      localStorage.removeItem(CREATING_KEY);
    }
  } catch { /* 无所谓 */ }
}

/** 盯着新片子，直到它停下来等人看——然后把时间线打开。

    这一段时间里还没有时间线：场景清单要到字幕做完才有。所以不能直接
    ``load``（那会得到一句"没有场景"），得先等。 */
async function watchCreation(taskId, subject) {
  const started = Date.now();
  const name = subject ? `“${subject}”` : "Your video";
  ui.createModal.hidden = false;
  ui.createModal.querySelector(".modal-box").classList.remove("failed");
  ui.createTitle.textContent = "Creating your video";
  ui.createNote.textContent = "Starting…";
  ui.createBar.classList.add("waiting");
  ui.createBar.style.width = "";
  ui.createPct.textContent = "";
  ui.createElapsed.textContent = "";
  ui.createHide.hidden = false;
  ui.createDone.hidden = true;
  setTitleNote("Creating");

  const stop = (title, note, failed) => {
    forgetCreation(taskId);
    ui.createModal.hidden = false;
    ui.createModal.querySelector(".modal-box").classList.toggle("failed", !!failed);
    ui.createBar.classList.remove("waiting");
    ui.createBar.style.width = "100%";
    ui.createTitle.textContent = title;
    ui.createNote.textContent = note;
    ui.createPct.textContent = "";
    ui.createHide.hidden = true;
    ui.createDone.hidden = false;
    setTitleNote(failed ? "Failed" : "");
  };

  let misses = 0;
  for (;;) {
    let task;
    try {
      task = await call("GET", `/tasks/${encodeURIComponent(taskId)}`);
      misses = 0;
    } catch (error) {
      misses += 1;
      if (misses < STATUS_RETRIES) {
        await new Promise((resolve) => setTimeout(resolve, STATUS_RETRY_DELAY));
        continue;
      }
      stop("Lost track of the new video",
        `Its status cannot be read any more (${error.message || error}). It `
        + "may still be running: watch the window the API is running in, then "
        + "look for it under My videos.", true);
      return;
    }
    const stateCode = Number(task && task.state);
    if (stateCode === -1) {
      const stage = task.failed_stage ? ` (while ${task.failed_stage})` : "";
      stop("The video could not be created",
        String(task.error || "No reason was reported.") + stage, true);
      return;
    }
    if (stateCode !== 4) {
      forgetCreation(taskId);
      setTitleNote("");
      // 用户把挡板收起来、又去改别的片子了：别把他正在改的那条换掉。
      if (ui.createModal.hidden && state.taskId && state.taskId !== taskId) {
        say(`${name} is ready — open it from My videos.`, "ok");
        return;
      }
      ui.createModal.hidden = true;
      ui.task.value = taskId;
      await load(taskId);
      return;
    }
    const pct = Math.max(0, Math.min(100, Number(task.progress) || 0));
    if (pct > 0) {
      ui.createBar.classList.remove("waiting");
      ui.createBar.style.width = pct + "%";
      ui.createPct.textContent = pct + "%";
    }
    const phase = String((task.generation_activity || {}).phase || "");
    if (phase) ui.createNote.textContent = (PHASES[phase] || `Stage: ${phase}`) + "…";
    ui.createElapsed.textContent = `${fmt((Date.now() - started) / 1000)} so far`;
    setTitleNote(pct > 0 ? `Creating ${pct}%` : "Creating");
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

/** 刷新页面之后接着盯：新片子做到一半时刷新，不该变成"它去哪了"。 */
async function resumeCreation() {
  let taskId = "";
  try { taskId = localStorage.getItem(CREATING_KEY) || ""; } catch { return; }
  if (!taskId) return;
  try {
    const task = await call("GET", `/tasks/${encodeURIComponent(taskId)}`);
    if (Number(task && task.state) === 4) {
      closeLibrary();
      watchCreation(taskId, "");
      return;
    }
  } catch { /* 查不到就是早结束了，或者接口重启过 */ }
  forgetCreation(taskId);
}

ui.newVideo.addEventListener("click", openNewVideo);
ui.libraryNew.addEventListener("click", openNewVideo);
ui.newCancel.addEventListener("click", closeNewVideo);
ui.newWrite.addEventListener("click", writeNewScript);
ui.newCreate.addEventListener("click", createNewVideo);
ui.newLanguage.addEventListener("change", () => {
  fresh.values.video_language = ui.newLanguage.value;
});
ui.createHide.addEventListener("click", () => { ui.createModal.hidden = true; });
ui.createDone.addEventListener("click", () => { ui.createModal.hidden = true; });
ui.newSubject.addEventListener("keydown", (event) => {
  if (event.key === "Enter") createNewVideo();
});

/* ---------- 设置 ----------

   各家服务商的地址、Key 和模型。以前只在 WebUI 里，表单底下的"更多设置"
   得把人送到另一个页面去。现在就在这里，写的是同一份 config.toml。

   这张表也是服务端给的（``/settings``）。Key 不回传：读回来只有"设过没有"，
   所以框是空的——留空等于不动，要清得点"清掉"。 */
const prefs = { data: null, group: "", changes: {}, clear: new Set(), saving: false };

async function openSettings() {
  ui.settingsModal.hidden = false;
  ui.settingsStatus.className = "hint";
  ui.settingsStatus.textContent = "Reading…";
  prefs.changes = {};
  prefs.clear = new Set();
  try {
    prefs.data = await call("GET", "/settings");
    if (!prefs.group) prefs.group = (prefs.data.groups[0] || {}).id || "";
    ui.settingsStatus.textContent = "";
    drawPrefs();
  } catch (error) {
    ui.settingsStatus.className = "hint warn";
    ui.settingsStatus.textContent = `Could not read the settings: `
      + `${error.message || error}. If you have just updated, the API needs `
      + "restarting.";
  }
}

function closeSettings() {
  ui.settingsModal.hidden = true;
  watchSignins();
  // 下次打开重新问一遍：账号可能在别处连上或断开了，缓存也可能变了。
  signins.state = {};
  upkeep.data = null;
  upkeep.note = "";
}

function settingValue(id) {
  return id in prefs.changes ? prefs.changes[id] : prefs.data.values[id];
}

function drawPrefs() {
  ui.settingsNav.innerHTML = "";
  for (const group of prefs.data.groups || []) {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = group.label;
    node.className = group.id === prefs.group ? "on" : "";
    node.addEventListener("click", () => { prefs.group = group.id; drawPrefs(); });
    ui.settingsNav.appendChild(node);
  }
  ui.settingsBody.innerHTML = "";
  const group = (prefs.data.groups || []).find((one) => one.id === prefs.group);
  if (!group) return;
  if (group.note) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = group.note;
    ui.settingsBody.appendChild(note);
  }
  if ((group.fields || []).length) {
    ui.settingsBody.appendChild(settingFields(group.fields));
  }
  // 这一组是动作不是填空，没有"保存"这回事。
  ui.settingsSave.hidden = !!group.tools;
  if (group.tools) {
    ui.settingsBody.appendChild(upkeepBox());
    return;
  }
  // 有"当前用哪一家"的那一组（语言模型四十来家）只展开被选中的那一家。
  const active = group.panel_field ? String(settingValue(group.panel_field)) : null;
  for (const panel of group.panels || []) {
    if (active !== null && panel.id !== active) continue;
    const box = document.createElement("details");
    box.open = active !== null;
    const head = document.createElement("summary");
    head.textContent = panel.label;
    const ready = (panel.fields || []).some((field) => {
      const value = prefs.data.values[field.id];
      return value && typeof value === "object" && value.set;
    });
    if (ready) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "key saved";
      head.appendChild(badge);
    }
    box.appendChild(head);
    if (panel.note) {
      const note = document.createElement("p");
      note.className = "hint";
      note.style.padding = "0 10px 8px";
      note.textContent = panel.note;
      box.appendChild(note);
    }
    if (panel.signin) box.appendChild(signinBox(panel.signin));
    box.appendChild(settingFields(panel.fields || []));
    ui.settingsBody.appendChild(box);
  }
}

function settingFields(fields) {
  const grid = document.createElement("div");
  grid.className = "form-fields";
  for (const field of fields) grid.appendChild(settingField(field));
  return grid;
}

function settingField(field) {
  const stored = prefs.data.values[field.id];
  const secret = stored && typeof stored === "object";
  if (!secret) {
    // 普通的项直接借新片子表单的那一套画法。
    const kind = field.kind === "keys" ? "text" : field.kind;
    return formField({ ...field, kind, name: field.id }, settingValue(field.id),
      (value, redraw) => {
        prefs.changes[field.id] = value;
        if (redraw) drawPrefs();
      });
  }
  const box = document.createElement("div");
  box.className = "field wide";
  const label = document.createElement("label");
  label.textContent = field.label;
  box.appendChild(label);
  const row = document.createElement("div");
  row.className = "with-button";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "new-password";
  const clearing = prefs.clear.has(field.id);
  input.placeholder = clearing ? "Will be removed when you save"
    : stored.set
      ? (stored.count > 1 ? `Saved (${stored.count} keys) — type to replace`
        : "Saved — type to replace")
      : (field.kind === "keys" ? "Not set — several keys can be separated by commas"
        : "Not set");
  input.value = prefs.changes[field.id] || "";
  input.addEventListener("input", () => {
    prefs.clear.delete(field.id);
    if (input.value) prefs.changes[field.id] = input.value;
    else delete prefs.changes[field.id];
  });
  row.appendChild(input);
  if (stored.set) {
    const drop = document.createElement("button");
    drop.type = "button";
    drop.className = "btn small";
    drop.textContent = clearing ? "Keep it" : "Remove";
    drop.addEventListener("click", () => {
      if (clearing) prefs.clear.delete(field.id);
      else { prefs.clear.add(field.id); delete prefs.changes[field.id]; }
      drawPrefs();
    });
    row.appendChild(drop);
  }
  box.appendChild(row);
  return box;
}

/* ---------- 设置里的两样特别的东西 ----------

   登录：三家语言模型不用 Key，而是去官网点一下授权。
   备份和维护：不是填空，是几个动作——所以不走上面那套"改了再保存"。 */
const signins = { state: {}, timer: 0 };

function signinPath(provider, step) {
  return `/settings/signin/${encodeURIComponent(provider)}${step ? "/" + step : ""}`;
}

async function signinStep(provider, step) {
  try {
    signins.state[provider] = await call(step ? "POST" : "GET",
      signinPath(provider, step));
  } catch (error) {
    signins.state[provider] = {
      provider, problem: String(error.message || error), failed: true,
    };
  }
  if (!ui.settingsModal.hidden) drawPrefs();
  watchSignins();
}

/** 有登录走到一半就每两秒问一次，没有就停。设置窗关了也停。 */
function watchSignins() {
  const waiting = Object.values(signins.state).filter((one) => one && one.pending);
  if (!waiting.length || ui.settingsModal.hidden) {
    clearTimeout(signins.timer);
    signins.timer = 0;
    return;
  }
  if (signins.timer) return;
  signins.timer = setTimeout(async () => {
    signins.timer = 0;
    for (const one of waiting) await signinStep(one.provider, "poll");
  }, 2000);
}

function signinBox(provider) {
  const box = document.createElement("div");
  box.className = "signin";
  const state = signins.state[provider];
  if (!state) {
    box.textContent = "Checking the account…";
    signinStep(provider, "");
    return box;
  }
  const line = (text, kind) => {
    const node = document.createElement("p");
    node.className = "hint" + (kind ? " " + kind : "");
    node.textContent = text;
    box.appendChild(node);
  };
  const act = (label, step, primary) => {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "btn small" + (primary ? " primary" : "");
    node.textContent = label;
    node.addEventListener("click", () => {
      node.disabled = true;
      signinStep(provider, step);
    });
    return node;
  };
  if (state.connected) {
    line(`Connected${state.account ? " as " + state.account : ""}.`, "ok");
  } else if (state.signed_in) {
    line(`Signed in${state.account ? " as " + state.account : ""}.`);
  } else if (!state.pending) {
    line("Not connected.");
  }
  if (state.problem) line(state.problem, "warn");
  if (state.note) line(state.note.message, state.note.kind === "ok" ? "ok" : "warn");
  const row = document.createElement("div");
  row.className = "row-left";
  if (state.pending) {
    line(state.pending.message, "pending");
    if (state.pending.code) {
      const code = document.createElement("code");
      code.className = "signin-code";
      code.textContent = state.pending.code;
      box.appendChild(code);
    }
    const open = document.createElement("a");
    open.className = "btn small primary";
    open.href = state.pending.url;
    open.target = "_blank";
    open.rel = "noopener";
    open.textContent = "Open the sign-in page";
    row.append(open, act("Cancel", "cancel"));
  } else if (!state.connected) {
    row.appendChild(act(state.signed_in ? "Allow plan usage" : "Sign in",
      "begin", true));
  }
  if (state.connected || state.signed_in || state.broken) {
    row.appendChild(act("Disconnect", "disconnect"));
  }
  box.appendChild(row);
  if (state.detail) line(state.detail);
  return box;
}

const upkeep = { data: null, days: 30, busy: false, note: "", noteKind: "" };

function sizeText(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1 << 30) return `${(value / (1 << 30)).toFixed(1)} GB`;
  if (value >= 1 << 20) return `${(value / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(0, Math.round(value / 1024))} KB`;
}

/** 把服务端给的一份 JSON 存成文件。 */
function saveAsFile(filename, payload) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(
    new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

/** 做一件维护的事，把结果（或者为什么没做成）写在那一组底下。 */
async function upkeepRun(work) {
  if (upkeep.busy) return;
  upkeep.busy = true;
  upkeep.note = "Working…";
  upkeep.noteKind = "pending";
  drawPrefs();
  try {
    upkeep.note = (await work()) || "";
    upkeep.noteKind = "ok";
  } catch (error) {
    upkeep.note = String(error.message || error);
    upkeep.noteKind = "warn";
  } finally {
    upkeep.busy = false;
    drawPrefs();
  }
}

function pickFile(accept, onFile) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.addEventListener("change", () => {
    if (input.files.length) onFile(input.files[0]);
  });
  input.click();
}

function upkeepBox() {
  const wrap = document.createElement("div");
  wrap.className = "upkeep";
  if (!upkeep.data) {
    wrap.textContent = "Reading…";
    call("GET", "/settings/upkeep").then((data) => {
      upkeep.data = data;
      if (!ui.settingsModal.hidden) drawPrefs();
    }).catch((error) => {
      upkeep.note = String(error.message || error);
      upkeep.noteKind = "warn";
      upkeep.data = { cache: null, logging: null };
      if (!ui.settingsModal.hidden) drawPrefs();
    });
    return wrap;
  }
  const section = (title, text) => {
    const box = document.createElement("div");
    box.className = "tool";
    const head = document.createElement("strong");
    head.textContent = title;
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = text;
    const row = document.createElement("div");
    row.className = "row-left";
    box.append(head, note, row);
    wrap.appendChild(box);
    return row;
  };
  const press = (label, onClick, extra) => {
    const node = button(label, "small" + (extra ? " " + extra : ""), onClick);
    node.disabled = upkeep.busy;
    return node;
  };

  // 密钥
  const keys = section("Your keys",
    "A backup file holds every saved key in plain text. Keep it somewhere "
    + "safe; anyone who has it can use your accounts.");
  keys.append(
    press("Download key backup", () => {
      if (!window.confirm("Write every saved key into a file on this computer?")) {
        return;
      }
      upkeepRun(async () => {
        const data = await call("POST", "/settings/keys/export", { confirm: true });
        saveAsFile(data.filename, data.payload);
        return "Key backup downloaded.";
      });
    }),
    press("Restore from a backup…", () => pickFile(".json,application/json",
      (file) => upkeepRun(async () => {
        const data = await upload("/settings/keys/restore", file);
        prefs.data = await call("GET", "/settings");
        fresh.form = null;
        return `Restored ${data.restored} key${data.restored === 1 ? "" : "s"}.`
          + (data.deferred ? " A video is being made, so they take effect "
            + "when that finishes." : "");
      }))),
  );

  // 预设
  const preset = section("Video settings preset",
    "The choices the New video form starts from, without any keys. Load one "
    + "and the next new video starts from it.");
  preset.append(
    press("Download preset", () => upkeepRun(async () => {
      const data = await call("POST", "/settings/preset/export", {});
      saveAsFile(data.filename, data.payload);
      return "Preset downloaded.";
    })),
    press("Load a preset…", () => pickFile(".json,application/json",
      (file) => upkeepRun(async () => {
        await upload("/settings/preset/import", file);
        fresh.form = null;
        return "Preset loaded. The next new video starts from it.";
      }))),
  );

  // 素材缓存
  const cache = upkeep.data.cache;
  if (cache) {
    const row = section("Downloaded stock material",
      `${cache.files} file${cache.files === 1 ? "" : "s"}, ${sizeText(cache.bytes)}, in `
      + `${cache.folder}. These are clips downloaded from stock sites; they `
      + "are downloaded again when needed. Your videos are not touched.");
    const range = document.createElement("select");
    for (const days of cache.ranges || []) {
      range.appendChild(option(days === null ? "all" : String(days),
        days === null ? "Everything" : `Older than ${days} days`));
    }
    range.value = upkeep.days === null ? "all" : String(upkeep.days);
    range.disabled = upkeep.busy;
    range.addEventListener("change", () => {
      upkeep.days = range.value === "all" ? null : Number(range.value);
      upkeepRun(async () => {
        const query = upkeep.days === null ? "" : `?days=${upkeep.days}`;
        upkeep.data.cache = await call("GET", `/settings/cache${query}`);
        return "";
      });
    });
    const would = cache.would_delete || { files: 0, bytes: 0 };
    const clean = press(
      `Delete ${would.files} file${would.files === 1 ? "" : "s"} (${sizeText(would.bytes)})`,
      () => {
        if (!window.confirm(`Delete ${would.files} downloaded file(s)? This `
            + "cannot be undone.")) return;
        upkeepRun(async () => {
          const data = await call("POST", "/settings/cache/clean",
            { days: upkeep.days, confirm: true });
          upkeep.data.cache = data;
          const done = data.deleted || {};
          return `Deleted ${done.files} file(s), ${sizeText(done.bytes)} freed.`
            + (done.failed ? ` ${done.failed} could not be deleted.` : "");
        });
      }, "danger");
    clean.disabled = upkeep.busy || !would.files;
    row.append(range, clean);
  }

  // 日志
  const logging = upkeep.data.logging;
  if (logging) {
    const row = section("Diagnostic logging",
      "More detail in the window the app runs in. Takes effect at once.");
    const level = document.createElement("select");
    for (const one of logging.levels || []) level.appendChild(option(one, one));
    level.value = logging.level;
    const trace = document.createElement("label");
    trace.className = "check";
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !!logging.log_requests;
    trace.append(tick, document.createTextNode(" Trace API requests"));
    row.append(level, trace, press("Apply", () => upkeepRun(async () => {
      upkeep.data.logging = await call("PUT", "/settings/logging",
        { level: level.value, log_requests: tick.checked });
      return "Logging updated.";
    })));
  }

  if (upkeep.note) {
    const note = document.createElement("p");
    note.className = "hint " + upkeep.noteKind;
    note.textContent = upkeep.note;
    wrap.appendChild(note);
  }
  return wrap;
}

async function saveSettings() {
  if (prefs.saving || !prefs.data) return;
  const changes = { ...prefs.changes };
  const clear = [...prefs.clear];
  if (!Object.keys(changes).length && !clear.length) {
    ui.settingsStatus.className = "hint";
    ui.settingsStatus.textContent = "Nothing was changed.";
    return;
  }
  prefs.saving = true;
  ui.settingsSave.disabled = true;
  ui.settingsStatus.className = "hint pending";
  ui.settingsStatus.textContent = "Saving…";
  try {
    const data = await call("PUT", "/settings", { changes, clear });
    prefs.data = data;
    prefs.changes = {};
    prefs.clear = new Set();
    ui.settingsStatus.className = "hint";
    ui.settingsStatus.textContent = data.deferred
      ? "Saved. A video is being made, so it takes effect when that finishes."
      : "Saved.";
    // 新片子表单的默认值和音色清单是照旧设置读的，下次打开重读。
    fresh.form = null;
    drawPrefs();
  } catch (error) {
    ui.settingsStatus.className = "hint warn";
    ui.settingsStatus.textContent = String(error.message || error);
  } finally {
    prefs.saving = false;
    ui.settingsSave.disabled = false;
  }
}

ui.appSettings.addEventListener("click", openSettings);
ui.newSettings.addEventListener("click", () => {
  // 设置盖在表单上面打开；表单里填了一半的东西留着。
  closeNewVideo();
  openSettings();
});
ui.settingsClose.addEventListener("click", closeSettings);
ui.settingsSave.addEventListener("click", saveSettings);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!ui.settingsModal.hidden && !prefs.saving) closeSettings();
  else if (!ui.newModal.hidden && !fresh.writing) closeNewVideo();
});

/* ---------- 剪辑工具条和快捷键 ----------

   撤销、重做、分割、复制、删除，加上空格播放、方向键走位。剪辑软件里这些
   是肌肉记忆；没有它们，每一下都得先找按钮，而且删错了没有回头路。

   撤销的历史在服务端（任务目录里），所以刷新页面、换个浏览器都还在。 */
const edits = {
  history: { undo: 0, redo: 0, undo_label: "", redo_label: "" },
  timer: 0,
};

/** 播放头底下是哪一段，以及播放头在这一段里的第几秒。 */
function sceneUnderPlayhead() {
  const offsets = sceneOffsets();
  for (let at = state.scenes.length - 1; at >= 0; at -= 1) {
    if (state.playhead >= offsets[at] - 1e-6) {
      const inside = state.playhead - offsets[at];
      if (inside < screenDuration(state.scenes[at])) {
        return { index: at, inside };
      }
      return null;
    }
  }
  return null;
}

function canEdit() {
  return !!state.taskId && state.scenes.length > 0 && !state.taskBusy;
}

function drawEditBar() {
  const ready = canEdit();
  const picked = state.selected >= 0 && state.selected < state.scenes.length;
  ui.undo.disabled = !ready || !edits.history.undo;
  ui.redo.disabled = !ready || !edits.history.redo;
  ui.undo.title = edits.history.undo_label
    ? `Undo: ${edits.history.undo_label} (Ctrl+Z)` : "Undo (Ctrl+Z)";
  ui.redo.title = edits.history.redo_label
    ? `Redo: ${edits.history.redo_label} (Ctrl+Y)` : "Redo (Ctrl+Y)";
  ui.split.disabled = !ready;
  ui.duplicate.disabled = !ready || !picked;
  // 最后一段不能删：一条片子至少要有一段。
  ui.deleteScene.disabled = !ready || !picked || state.scenes.length < 2;
  ui.pace.disabled = !ready || !state.scenes.length;
}

async function syncHistory() {
  if (!state.taskId) return;
  try {
    edits.history = await call("GET",
      `/tasks/${encodeURIComponent(state.taskId)}/history`) || edits.history;
  } catch {
    // 老任务、或者接口还没重启：撤销用不了，但不该为这个报错。
    edits.history = { undo: 0, redo: 0, undo_label: "", redo_label: "" };
  }
  drawEditBar();
}

/** 任何一次改动之后都重新问一遍"能撤销几步"。挂在 ``call`` 上而不是每个
    操作各调一次：新加的操作不用记得来这里登记。 */
function scheduleHistorySync() {
  clearTimeout(edits.timer);
  edits.timer = setTimeout(syncHistory, 250);
}

function splitAtPlayhead() {
  if (!canEdit()) return;
  const hit = sceneUnderPlayhead();
  if (!hit) {
    say("Move the playhead onto a scene to split it.", "warn");
    return;
  }
  const length = screenDuration(state.scenes[hit.index]);
  if (hit.inside < 0.2 || hit.inside > length - 0.2) {
    say("Move the playhead further inside the scene — a cut this close to "
      + "the edge would leave a piece too short to see.", "warn");
    return;
  }
  run("Splitting", async () => {
    await call("POST", `${scenePath(hit.index)}/split`, { at: hit.inside });
    // 选中后一段：切完之后手上要处理的通常是它（拖走、换图、删掉）。
    state.selected = hit.index + 1;
  });
}

function duplicateSelected() {
  if (!canEdit() || state.selected < 0) return;
  const at = state.selected;
  run("Duplicating", async () => {
    await call("POST", `${scenePath(at)}/duplicate`);
    state.selected = at + 1;
  });
}

function deleteSelected() {
  if (!canEdit() || state.selected < 0) return;
  if (state.scenes.length < 2) {
    say("A video needs at least one scene.", "warn");
    return;
  }
  // 不再问"确定吗"：删错了按 Ctrl+Z 就回来了，图也还在盘上。
  run("Deleting", () => call("DELETE", scenePath(state.selected)));
}

async function stepHistory(direction) {
  if (!canEdit() || state.busy) return;
  if (!edits.history[direction]) return;
  state.busy = true;
  try {
    const data = await call("POST",
      `/tasks/${encodeURIComponent(state.taskId)}/history/${direction}`);
    edits.history = (data && data.history) || edits.history;
    await refresh();
    const what = (data && data.label) || "the last change";
    say(direction === "undo" ? `Undone: ${what}` : `Redone: ${what}`, "ok");
  } catch (error) {
    say(String(error.message || error), "error");
  } finally {
    state.busy = false;
    drawEditBar();
  }
}

/** 选中上一段或下一段，并把播放头放到它开头。 */
function stepScene(by) {
  if (!state.scenes.length) return;
  const from = state.selected >= 0 ? state.selected
    : ((sceneUnderPlayhead() || { index: 0 }).index);
  const to = Math.max(0, Math.min(state.scenes.length - 1, from + by));
  state.selected = to;
  scrubTo(sceneOffsets()[to]);
  draw();
}

ui.undo.addEventListener("click", () => stepHistory("undo"));
ui.redo.addEventListener("click", () => stepHistory("redo"));
ui.split.addEventListener("click", splitAtPlayhead);
ui.duplicate.addEventListener("click", duplicateSelected);
// 每张画面跟着它那句旁白走。新片子打开时已经是这样；挪过、删过之后用它
// 再对一次。对不上的时候服务端会点名是哪一段。
ui.pace.addEventListener("click", () => run("Matching the pictures to the narration", async () => {
  await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/timeline/pace`);
  return "";
}));
ui.deleteScene.addEventListener("click", deleteSelected);

/** 这一下键盘是打给时间线的，还是打给一个输入框的。
    在框里打字时空格就是空格、Delete 就是删字——抢过来会毁掉用户正在写的
    东西。开着的窗口（新片子、设置）同理。 */
function typingSomewhere(event) {
  const node = event.target;
  if (node && (node.isContentEditable
      || ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName))) {
    return true;
  }
  return !!document.querySelector(".modal:not([hidden])");
}

document.addEventListener("keydown", (event) => {
  if (event.altKey || typingSomewhere(event) || !state.taskId) return;
  const ctrl = event.ctrlKey || event.metaKey;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  let handled = true;
  if (ctrl && key === "z" && !event.shiftKey) stepHistory("undo");
  else if (ctrl && (key === "y" || (key === "z" && event.shiftKey))) {
    stepHistory("redo");
  } else if (ctrl && key === "d") duplicateSelected();
  else if (ctrl && key === "b") splitAtPlayhead();
  else if (ctrl) handled = false;
  else if (key === " ") {
    if (state.scenes.length) (state.playing ? stop() : play());
  } else if (key === "s") splitAtPlayhead();
  else if (key === "Delete" || key === "Backspace") deleteSelected();
  // 一下 0.1 秒，按住 Shift 一下 1 秒。
  else if (key === "ArrowLeft") scrubTo(state.playhead - (event.shiftKey ? 1 : 0.1));
  else if (key === "ArrowRight") scrubTo(state.playhead + (event.shiftKey ? 1 : 0.1));
  else if (key === "ArrowUp") stepScene(-1);
  else if (key === "ArrowDown") stepScene(1);
  else if (key === "Home") scrubTo(0);
  else if (key === "End") scrubTo(projectTotal());
  else handled = false;
  // 处理了的才拦：空格不拦会把页面往下滚，Ctrl+D 不拦会弹出"加书签"。
  if (handled) event.preventDefault();
});

/* ---------- 左边的工具栏和停靠面板 ----------

   脚本、字幕、声音、效果、发布这五个面板以前是浮在画面上的窗，各有各的
   开关。现在它们停靠在左边，一次只开一个，像 CapCut 那样。

   面板自己的代码一行没动：这里只做三件事——把它们搬进 dock，开一个时关掉
   别的，以及让页签亮起来。所以哪个面板将来改了自己的开关逻辑，这里不用
   跟着改。 */
const DOCK_TABS = [
  [ui.scriptToggle, ui.scriptPanel, closeScript],
  [ui.subsToggle, ui.subsPanel, closeSubtitles],
  [ui.musicToggle, ui.musicPanel, closeMusic],
  [ui.effectsToggle, ui.effectsPanel, closeEffects],
  [ui.publishToggle, ui.publishPanel, closePublish],
];

function syncDock() {
  let open = false;
  for (const [toggle, panel] of DOCK_TABS) {
    toggle.classList.toggle("on", !panel.hidden);
    open = open || !panel.hidden;
  }
  // 只在变了的时候写：写同一个值也会惊动下面那个观察者。
  if (ui.dock.hidden === open) ui.dock.hidden = !open;
}

for (const [toggle, panel] of DOCK_TABS) {
  ui.dock.appendChild(panel);
  toggle.addEventListener("click", () => {
    // 开这一个之前把别的关掉。走各自的关闭函数，不直接藏起来：字幕面板
    // 关的时候要把没保存的预览还原，直接藏它就漏了这一步。
    for (const [, other, close] of DOCK_TABS) {
      if (other !== panel && !other.hidden) close();
    }
  });
}
// 面板怎么开、怎么关不归这里管（它们有自己的"关闭"按钮，保存之后也会自己
// 关），所以不去每个地方都补一句，而是看着 hidden 这个属性。
new MutationObserver(syncDock).observe(ui.dock, {
  attributes: true, attributeFilter: ["hidden"], subtree: true,
});
syncDock();

function drawProjectTitle() {
  const name = (state.data && state.data.subject) || "";
  ui.projectTitle.textContent = name;
  ui.projectTitle.title = name;
  // 开着一条片子时，"我的视频"是往回走的那个按钮，删除也才有东西可删。
  const inVideo = !!state.taskId && !EMBEDDED;
  ui.libraryToggle.textContent = inVideo ? "\u2190 My videos" : "My videos";
  ui.libraryToggle.title = inVideo
    ? "Leave this video and go back to the list of your videos" : "";
  ui.deleteVideo.hidden = !inVideo;
}

// "按 id 打开"现在在"我的视频"那一屏里：打开之后那一屏得让开。
ui.load.addEventListener("click", closeLibrary);
ui.task.addEventListener("keydown", (event) => {
  if (event.key === "Enter") closeLibrary();
});

/* ---------- "?" 说明 ----------

   面板里原来到处是整段的说明文字：提示词怎么用、AI 写提示词是怎么回事、
   效果什么时候生效。第一次用的时候它们有用，之后每一次都只是把按钮往下
   挤——右边这一栏本来就窄，一屏里一半是字。

   所以说明收进一个 "?"：鼠标移上去（或者用键盘聚焦）才出来。字一个没删，
   只是不再占地方。

   浮层是挂在 body 上的同一个节点，不是每个 "?" 各带一个：右边那一栏会
   滚动、会裁剪溢出，带在里面的浮层会被裁掉一半。 */
let helpPop = null;

function showHelp(anchor, text) {
  if (!helpPop) {
    helpPop = document.createElement("div");
    helpPop.className = "help-pop";
    helpPop.setAttribute("role", "tooltip");
    document.body.appendChild(helpPop);
  }
  helpPop.textContent = text;
  helpPop.hidden = false;
  // 先摆出来量尺寸，再决定放哪：优先放在 "?" 下面，放不下就翻到上面；
  // 横向夹在屏幕里——右边那一栏的 "?" 紧贴着屏幕边。
  const at = anchor.getBoundingClientRect();
  const box = helpPop.getBoundingClientRect();
  const left = Math.max(8, Math.min(window.innerWidth - box.width - 8,
    at.left + at.width / 2 - box.width / 2));
  const below = at.bottom + 8;
  const top = below + box.height > window.innerHeight - 8
    ? Math.max(8, at.top - box.height - 8) : below;
  helpPop.style.left = `${left}px`;
  helpPop.style.top = `${top}px`;
}

function hideHelp() {
  if (helpPop) helpPop.hidden = true;
}

/** 一个 "?"。鼠标移上去、或者键盘聚焦，就显示 ``text``。 */
function helpTip(text) {
  const node = document.createElement("span");
  node.className = "help";
  node.textContent = "?";
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.setAttribute("aria-label", text);
  node.addEventListener("mouseenter", () => showHelp(node, text));
  node.addEventListener("focus", () => showHelp(node, text));
  node.addEventListener("mouseleave", hideHelp);
  node.addEventListener("blur", hideHelp);
  // 它常常坐在一个 <label> 或 <summary> 里。点它不该顺便聚焦输入框、
  // 也不该把抽屉收起来。
  node.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    showHelp(node, text);
  });
  return node;
}

/** 给一个字段的标题后面挂一个 "?"。 */
function addHelp(wrap, text) {
  const label = wrap && wrap.querySelector("label, summary");
  if (label) label.appendChild(helpTip(text));
  return wrap;
}

// 浮层是固定在屏幕上的，面板一滚它就对不上了；滚了就收起来。
document.addEventListener("scroll", hideHelp, true);

/* ---------- 右边那一栏分成几节 ----------

   画面、效果、时长——三件不相干的事原来从上到下排成一条长卷，要改时长得
   先滚过整个提示词区。现在一节一个抽屉，默认只开"画面"：那是来这一栏
   十次里九次要做的事。

   开没开是按节记的，不按场景记：把"时长"打开之后一段一段往下点，它应该
   一直开着。 */
const inspectorOpen = { Picture: true, Effects: false, Timing: false };

/** 在右边那一栏里做一个记号："从这里开始是下一节"。空名字表示"分节到此
    为止"，后面的东西（锁定、删除）不属于任何一节。
    记号本身是看不见的：某条路提前返回、没走到分组那一步，也不会露出来。 */
function inspectorSection(name) {
  const mark = document.createElement("i");
  mark.hidden = true;
  mark.dataset.section = name;
  ui.inspBody.appendChild(mark);
}

/** 按记号把右边那一栏的内容收进一个个抽屉里。 */
function groupInspector() {
  let drawer = null;
  for (const node of [...ui.inspBody.children]) {
    if (node.dataset && "section" in node.dataset) {
      const name = node.dataset.section;
      drawer = null;
      if (name) {
        const box = document.createElement("details");
        box.className = "insp-section";
        box.open = inspectorOpen[name] !== false;
        box.addEventListener("toggle", () => { inspectorOpen[name] = box.open; });
        const head = document.createElement("summary");
        head.textContent = name;
        box.appendChild(head);
        // 抽屉放在记号原来的位置上，然后才把记号摘掉。
        ui.inspBody.insertBefore(box, node);
        drawer = box;
      }
      node.remove();
      continue;
    }
    if (drawer) drawer.appendChild(node);
  }
}

/* ---------- 左边面板里的说明也收进 "?" ----------

   和右边那一栏同一个道理：每个面板顶上那段话第一次有用，之后只是把控件
   往下挤。带 id 的不动——那些是会变的状态（"已保存"、"这条片子是竖版"），
   不是说明。 */
function tuckPanelHints() {
  for (const [, panel] of DOCK_TABS) {
    for (const hint of [...panel.querySelectorAll("p.hint")]) {
      if (hint.id || hint.querySelector("[id]")) continue;
      const text = hint.textContent.replace(/\s+/g, " ").trim();
      if (text.length < 60) continue;
      // 往上找它在讲的是谁：最近的那个标题，或者最近的那个字段。
      let owner = hint.previousElementSibling;
      while (owner && !owner.matches("h2, h3, .field")) {
        owner = owner.previousElementSibling;
      }
      if (!owner) continue;
      const label = owner.matches(".field") ? owner.querySelector("label") : owner;
      if (!label) continue;
      label.appendChild(helpTip(text));
      hint.remove();
    }
  }
}
tuckPanelHints();

/* ---------- 挑一张照片来重画 ----------

   搜到的照片先摆出来，点哪张就照哪张画；也可以改搜索词再搜，或者传一张
   自己的。以前这一步是自动的，画完才知道它拿了哪一张。 */
const photoPick = {
  at: -1, keyword: null, style: null, photos: [], busy: false,
  // 这一段现在那张图是照着哪张存下来的照片画的（没有就是空的）。
  kept: "",
  // 没照着画过，但开画之前给它定下过一张（从 Google 挑的、传的）。
  saved: false,
  // 每一段上一次在这里搜的是什么。再打开时框里就是它。
  words: {},
};

// "这一张保留多少照片"就在这扇窗里选：它和照片是同一个决定，而且一口气
// 给十几段挑照片的时候，每一张都可以不一样。
const photoKeepChoice = photoKeepPicker();
photoKeepChoice.wrap.title = "Measured on this model: keeping the photograph gives you "
  + "the real subject and framing but a photographic picture, and loosely "
  + "inspired gives your style exactly while the photograph only hints at "
  + "the subject. Same picture gives both; it ignores the prompt. "
  + "The photo itself is not drawn at all: the original goes on the scene, "
  + "fitted to the frame, in about a second.";
ui.photoKeepSlot.appendChild(photoKeepChoice.wrap);

/* ---------- 手里的那张照片，摆进画框里 ----------

   照着照片画之前，照片先摆在这扇窗里的一个画框里（画框就是这条片子的
   比例）。一开始整张照片都在框里，一点不裁；拖滑块改大小，拖照片改位置。
   放大到出框的那一部分是用户自己决定不要的——程序自己从不裁。

   这一段已经有一张照片的（照着画过的那一张，或者开画之前从 Google 挑下来
   存着的那一张），开窗就摆着它：点"照这张画"就用，不用再去搜。 */
const photoInHand = { extra: null, zoom: 1, x: 0, y: 0, drag: null };
const PHOTO_ZOOM = { least: 0.3, most: 3 };

const photoStage = (() => {
  const box = document.createElement("div");
  box.className = "photo-have";
  box.hidden = true;
  const view = document.createElement("div");
  view.className = "photo-frame";
  view.title = "Drag the photo to move it. Scroll to resize it.";
  const behind = document.createElement("img");
  behind.className = "photo-frame-behind";
  const shot = document.createElement("img");
  shot.className = "photo-frame-shot";
  for (const one of [behind, shot]) {
    one.alt = "";
    one.draggable = false;
    // 不把"我从哪个页面来的"告诉图床。
    one.referrerPolicy = "no-referrer";
  }
  view.append(behind, shot);
  const side = document.createElement("div");
  side.className = "photo-have-side";
  const head = document.createElement("b");
  const line = document.createElement("span");
  line.textContent = "It starts with the whole photo in the frame, nothing "
    + "cropped. Drag the slider to resize it and drag the photo to move it; "
    + "what you push outside the frame is left out.";
  const sizeRow = document.createElement("label");
  sizeRow.className = "photo-size";
  const sizeName = document.createElement("span");
  sizeName.textContent = "Photo size";
  const size = document.createElement("input");
  size.type = "range";
  size.min = String(PHOTO_ZOOM.least * 100);
  size.max = String(PHOTO_ZOOM.most * 100);
  size.step = "1";
  const sizeSays = document.createElement("span");
  sizeRow.append(sizeName, size, sizeSays);
  const buttons = document.createElement("div");
  buttons.className = "row";
  const whole = button("Whole photo", "small", () => setPhotoFrame(1, 0, 0));
  whole.title = "Back to the whole photo, centred, nothing cropped.";
  const go = button("Draw from this photo", "primary small",
    () => redrawFromPhoto(photoInHand.extra));
  buttons.append(go, whole);
  side.append(head, line, sizeRow, buttons);
  box.append(view, side);
  ui.photoGrid.before(box);
  return { box, view, behind, shot, head, size, sizeSays };
})();

/** 按现在的大小和位置把照片摆进画框。 */
function placePhotoInFrame() {
  const { view, shot, size, sizeSays } = photoStage;
  size.value = String(Math.round(photoInHand.zoom * 100));
  sizeSays.textContent = `${Math.round(photoInHand.zoom * 100)}%`;
  const wide = view.clientWidth;
  const high = view.clientHeight;
  const w = shot.naturalWidth;
  const h = shot.naturalHeight;
  if (!wide || !high || !w || !h) return;
  // 和服务端同一条式子：1 是"整张照片刚好放进画框"。
  const fit = Math.min(wide / w, high / h) * photoInHand.zoom;
  shot.style.width = `${w * fit}px`;
  shot.style.height = `${h * fit}px`;
  shot.style.left = `${wide / 2 + photoInHand.x * wide - (w * fit) / 2}px`;
  shot.style.top = `${high / 2 + photoInHand.y * high - (h * fit) / 2}px`;
}

function setPhotoFrame(zoom, x, y) {
  const within = (value, least, most) => Math.min(most, Math.max(least, value));
  photoInHand.zoom = within(zoom, PHOTO_ZOOM.least, PHOTO_ZOOM.most);
  photoInHand.x = within(x, -1, 1);
  photoInHand.y = within(y, -1, 1);
  placePhotoInFrame();
}

/** 用户摆的大小和位置；没动过就是 null（整张照片，什么都不用说）。 */
function photoFrameChosen() {
  const { zoom, x, y } = photoInHand;
  if (zoom === 1 && !x && !y) return null;
  const short = (value) => Math.round(value * 1000) / 1000;
  return { zoom: short(zoom), x: short(x), y: short(y) };
}

/** 这一种挑法的那张照片，在哪能看到。看不到的（"替我挑一张"）是空的。 */
function photoAddress(extra) {
  const task = `${API}/tasks/${encodeURIComponent(state.taskId)}`;
  if (!extra) return "";
  if (extra.photo_kept) return `${task}/photos/used/${encodeURIComponent(extra.photo_kept)}`;
  if (extra.photo_picked) return `${API}${scenePath(photoPick.at)}/photo-pick`;
  if (extra.photo_upload) {
    return `${task}/photos/uploaded/${encodeURIComponent(extra.photo_upload)}`;
  }
  return extra.photo_url || "";
}

/** 把一张照片拿在手里：摆进画框，等用户点"照这张画"。 */
function holdPhoto(extra, title) {
  const { box, view, behind, shot, head } = photoStage;
  Object.assign(photoInHand, { extra, zoom: 1, x: 0, y: 0, drag: null });
  const ratio = ASPECT_NUMBER[state.settings.aspect] || 9 / 16;
  view.style.aspectRatio = String(ratio);
  // 竖的片子按高算，横的按宽算：画框不该比窗还高。
  view.style.width = ratio < 1 ? "auto" : "340px";
  view.style.height = ratio < 1 ? "300px" : "auto";
  head.textContent = title;
  box.hidden = false;
  const address = photoAddress(extra);
  shot.src = address;
  behind.src = address;
  placePhotoInFrame();
}

function dropPhotoInHand() {
  photoInHand.extra = null;
  photoStage.box.hidden = true;
  photoStage.shot.removeAttribute("src");
  photoStage.behind.removeAttribute("src");
}

function samePhotoChoice(one, other) {
  return !!one && !!other && JSON.stringify(one) === JSON.stringify(other);
}

photoStage.shot.addEventListener("load", placePhotoInFrame);
// 照片看不了（文件不在了、图床不给）：这一块收起来，不摆一个空画框。
photoStage.shot.addEventListener("error", () => {
  if (photoInHand.extra) dropPhotoInHand();
});
photoStage.size.addEventListener("input", () => {
  setPhotoFrame(Number(photoStage.size.value) / 100, photoInHand.x, photoInHand.y);
});
photoStage.view.addEventListener("pointerdown", (event) => {
  photoInHand.drag = { x: event.clientX, y: event.clientY };
  photoStage.view.setPointerCapture(event.pointerId);
});
photoStage.view.addEventListener("pointermove", (event) => {
  const from = photoInHand.drag;
  if (!from) return;
  const { view } = photoStage;
  setPhotoFrame(photoInHand.zoom,
    photoInHand.x + (event.clientX - from.x) / (view.clientWidth || 1),
    photoInHand.y + (event.clientY - from.y) / (view.clientHeight || 1));
  photoInHand.drag = { x: event.clientX, y: event.clientY };
});
for (const done of ["pointerup", "pointercancel"]) {
  photoStage.view.addEventListener(done, () => { photoInHand.drag = null; });
}
photoStage.view.addEventListener("wheel", (event) => {
  event.preventDefault();
  setPhotoFrame(photoInHand.zoom * (event.deltaY < 0 ? 1.06 : 1 / 1.06),
    photoInHand.x, photoInHand.y);
}, { passive: false });

// 挑了一张之后先摆进画框再画，还是点了就画。记在这台浏览器里：一口气挑
// 几十张的人会想关掉它。
const photoFrameFirst = (() => {
  const wrap = document.createElement("label");
  wrap.className = "photo-frame-first";
  const input = document.createElement("input");
  input.type = "checkbox";
  let on = true;
  try { on = localStorage.getItem("editor.photoFrameFirst") !== "no"; } catch { /* 隐私模式 */ }
  input.checked = on;
  input.addEventListener("change", () => {
    try { localStorage.setItem("editor.photoFrameFirst", input.checked ? "yes" : "no"); } catch { /* 同上 */ }
  });
  const words = document.createElement("span");
  words.textContent = "Let me set the photo's size and position before it is drawn";
  wrap.title = "On: a photo you choose is shown in the frame first, and is "
    + "drawn when you click Draw from this photo. Off: it is drawn as soon as "
    + "you choose it, whole and uncropped.";
  wrap.append(input, words);
  ui.photoKeepSlot.appendChild(wrap);
  return input;
})();

/* ---------- 开画之前，一口气把照片挑完 ----------

   开片时可以定"百分之多少的画面照着照片画"。点"开画"的时候，这几段一段
   一段地过这扇窗：挑一张，下一段。全挑完之前一张图都不排——排了队就开始
   占显卡，而挑照片要用的那个写搜索词的模型也要显卡。

   每挑一张就存进这条片子里（服务端，连照片带选择）：中途关了窗、刷新了
   页面、出图失败了重来，都不用再挑一遍，从没挑的那一段接着来。 */
const photoRound = { on: false, scenes: [], at: -1, short: "" };

// 模型给一段打的分到这个数才去找照片：6 是"具体、真实、多半有照片"。
const PHOTO_WORTH = 6;

/** 最多几段要照着照片画，以及能从哪几段里挑（还没画、也没在排队的）。

    份额是按整条片子算的，已经照着照片画过（或者正排着照片的活）的算在
    里面——否则每点一次"开画"都会在剩下的里面再要一遍。 */
function photoQuota() {
  const percent = Number((state.data || {}).photo_scene_percent) || 0;
  const all = state.scenes || [];
  if (percent <= 0 || !all.length) return { count: 0, waiting: [] };
  const active = (state.queue.jobs || []).filter(
    (job) => job.state === "queued" || job.state === "running");
  const busy = new Set(active.map(jobScene));
  const done = all.filter((scene) => scene.reference).length
    + active.filter((job) => job.from_photo).length;
  const waiting = all.filter(
    (scene) => isPending(scene) && !busy.has(scene.index));
  const count = Math.max(0, Math.min(waiting.length,
    Math.round(all.length * percent / 100) - done));
  return { count, waiting };
}

/** 哪几段要照着照片画：说到真实的人、地方、东西的那几段，人排最前。

    不是隔几段取一段。够格的不到份额就少挑几段——拿一段"通货膨胀上升了"
    去凑数，找来的只会是一张不相干的图。``asked`` 是份额本来要几段。 */
async function scenesForPhotos() {
  const { count, waiting } = photoQuota();
  if (!count) return { scenes: [], asked: 0 };
  const data = await call(
    "GET", `/tasks/${encodeURIComponent(state.taskId)}/photos/candidates`);
  const rated = new Map();
  for (const one of (data && data.scenes) || []) rated.set(one.id, one);
  const worth = waiting
    .map((scene) => ({ scene, rating: rated.get(scene.id || materialKey(scene.material)) }))
    .filter((one) => one.rating && one.rating.kind !== "none"
      && one.rating.score >= PHOTO_WORTH);
  worth.sort((a, b) =>
    (b.rating.kind === "person") - (a.rating.kind === "person")
    || b.rating.score - a.rating.score
    || a.scene.index - b.scene.index);
  // 挑出来之后按时间线的顺序过，不按分数：一段一段往后走才不乱。
  const scenes = worth.slice(0, count).map((one) => one.scene)
    .sort((a, b) => a.index - b.index);
  return { scenes, asked: count };
}

function startPhotoRound(scenes) {
  Object.assign(photoRound, { on: true, scenes, at: -1 });
  nextRoundPhoto();
}

/** 下一段还没定照片的；没有了就开始排队。 */
function nextRoundPhoto() {
  const { scenes } = photoRound;
  let at = photoRound.at + 1;
  while (at < scenes.length && scenes[at].photo_pick) at += 1;
  photoRound.at = at;
  if (at >= scenes.length) {
    finishPhotoRound();
    return;
  }
  // 时间线跟着走：窗后面看得见现在在给哪一段找照片。
  state.selected = scenes[at].index;
  draw();
  openPhotoPicker(scenes[at], null, null);
}

/** 这一段的照片定了（``pick.skip`` 是"这一段不要照片"）：存进这条片子里，
    下一段。存不进去（照片太小、下载不了）就停在这一段上说清楚。 */
async function keepRoundPhoto(pick) {
  const at = photoPick.at;
  const scene = photoRound.scenes[photoRound.at];
  const body = pick.skip ? { skip: true } : { ...(pick.photo || {}) };
  if (!pick.skip) {
    if (photoWay(pick.strength)) body.follow = pick.strength;
    else if (pick.strength) body.strength = pick.strength;
  }
  photoNote(pick.skip ? "Skipping this scene…" : "Keeping this photo…", "pending");
  try {
    await call("POST", `${scenePath(at)}/photo-pick`, body);
  } catch (error) {
    if (photoPick.at !== at) return;
    photoNote(`${String(error.message || error).replace(/[.\s]+$/, "")}. `
      + "Choose another photo for this scene.", "warn");
    googleNote("Click Search on Google to open the window again.");
    return;
  }
  if (scene) scene.photo_pick = pick.skip ? "skip" : "photo";
  if (photoPick.at !== at || !photoRound.on) return;
  // 先等那个小窗口关掉再开下一个：关和开撞在一起，关掉的会是新开的那个。
  await closePhotoBrowser();
  nextRoundPhoto();
}

function finishPhotoRound() {
  photoRound.on = false;
  ui.photoModal.hidden = true;
  photoPick.at = -1;
  drawPending();
}

/** 挑照片的那一轮里一直挂着的那句话。 */
function photoRoundNote() {
  return photoRound.on
    ? `${photoRound.short}Nothing is drawn until a photo is chosen for every `
      + "one of these scenes."
    : "";
}

/** 这一段说的是什么，摆在窗顶上。 */
function photoSceneLine(scene) {
  const line = String(scene.narration || "").replace(/\s+/g, " ").trim();
  if (line) return `“${line}”`;
  return String(scene.keyword || scene.keyword_suggestion || "").trim();
}

function photoWordsKey(scene) {
  return scene.id || materialKey(scene.material) || `at-${scene.index}`;
}

function openPhotoPicker(scene, keyword, style, again) {
  Object.assign(photoPick, {
    at: scene.index, keyword, style, photos: [], busy: false,
    kept: (scene.reference && scene.reference.file) || "",
    saved: scene.photo_pick === "photo",
  });
  const round = photoRound.on;
  ui.photoTitle.textContent = round
    ? `Photo ${photoRound.at + 1} of ${photoRound.scenes.length} — `
      + `scene ${scene.index + 1}`
    : `Redraw scene ${scene.index + 1} from a photo`;
  ui.photoLine.textContent = photoSceneLine(scene);
  // 上一次选的那一档；连着挑好几段时不用每一段重选。
  photoKeepChoice.input.value = String(photoKeep);
  ui.photoKept.hidden = round || !(photoPick.kept || photoPick.saved);
  ui.photoKept.textContent = photoPick.kept
    ? "Use this scene's current photo" : "Use the photo saved for this scene";
  ui.photoSkip.hidden = !round;
  // 这一段已经有一张照片的，开窗就把它拿在手里。
  dropPhotoInHand();
  if (!round && photoPick.kept) {
    holdPhoto({ photo_kept: photoPick.kept }, "The photo this scene was drawn from");
  } else if (!round && photoPick.saved) {
    holdPhoto({ photo_picked: true }, "The photo already saved for this scene");
  }
  ui.photoGrid.innerHTML = "";
  ui.photoModal.hidden = false;
  photoPick.key = photoWordsKey(scene);
  // 框里一打开就得有词，而且是"之前搜的那个"：
  //   1. 这一段刚才在这里搜过的词（改过的也算）；
  //   2. 没有的话，上次照着照片画这一段时用的词（记在这一段上）；
  //   3. 都没有，才让模型从台词和提示词里写一条。
  const before = photoPick.words[photoPick.key]
    || ((scene.reference && scene.reference.query) || "").trim();
  ui.photoSearch.value = before;
  // 有词就马上开 Google 图片的小窗口；没词等模型写出来再开（见下）。
  // 图库那一遍不再自己搜：用户是在 Google 的窗口里挑的，再搜一遍只是多
  // 一次会失败的请求。想用图库，"Search" 按钮还在。
  googleNote("");
  photoNote(photoRoundNote());
  // "还是这张照片，换一档再画"：照片已经有了，不用去开 Google 的窗口。
  if (again) {
    photoNote("Choose how much of the photo to keep, then click "
      + "“Draw from this photo”. Or search for another one.");
    return;
  }
  if (before) browseGoogle(before);
  else writePhotoWords();
}

/** 让模型写搜索词，写完填进框里，再开 Google 图片的窗口去搜。词先落框、
    后搜索：窗口开不了的时候，框里不能是空的。 */
async function writePhotoWords() {
  const at = photoPick.at;
  ui.photoFind.disabled = true;
  photoNote("Working out what to search for…", "pending");
  let words = "";
  try {
    const data = await call("GET", `${scenePath(at)}/photos/words`);
    words = ((data && data.query) || "").trim();
  } catch (error) {
    // 单独写词的那一步没成（比如接口还没重启、还不认识这条路由）。不能
    // 就此停在一个空框上：退回原来那一步——让搜索自己写词、自己搜，搜到
    // 之后词照样填进框里。点一下按钮就开始搜，这件事不能因为加了一步而没了。
    console.warn("could not get the search words on their own", error);
  }
  if (photoPick.at !== at) return;
  ui.photoFind.disabled = false;
  ui.photoSearch.value = words;
  if (words) {
    photoNote(photoRoundNote());
    browseGoogle(words);
  } else {
    // 词没写出来（模型没应答）。不停在一个空框和一句"正在想"上。
    photoNote("Could not work out what to search for. Type it above and "
      + "click Search on Google.", "warn");
    ui.photoSearch.focus();
  }
}

function closePhotoPicker() {
  closePhotoBrowser();
  dropPhotoInHand();
  ui.photoModal.hidden = true;
  photoPick.at = -1;
  // 挑到一半关了窗：什么都没排。挑过的那几张记着，下次接着挑。
  if (photoRound.on) {
    photoRound.on = false;
    const kept = photoRound.scenes.filter(
      (scene) => scene.photo_pick === "photo" || scene.photo_pick === "auto").length;
    say("Nothing was queued."
      + (kept ? ` The ${kept} photo${kept === 1 ? "" : "s"} you chose `
        + `${kept === 1 ? "is" : "are"} kept — click the draw button to carry on.`
        : ""), "warn");
  }
}

function photoNote(text, kind) {
  ui.photoNote.className = "hint" + (kind ? " " + kind : "");
  ui.photoNote.textContent = text;
}

async function findPhotos(search) {
  if (photoPick.busy || photoPick.at < 0) return;
  const at = photoPick.at;
  photoPick.busy = true;
  ui.photoFind.disabled = true;
  ui.photoGrid.innerHTML = "";
  // 搜的词留在框里：搜不到的时候要改的就是它，不该让人重打一遍。而且
  // 记下来——关了窗再打开，框里还是它。
  if (search) {
    ui.photoSearch.value = search;
    if (photoPick.key) photoPick.words[photoPick.key] = search;
  }
  photoNote(search ? "Searching…" : "Working out what to search for…", "pending");
  try {
    const data = await call("GET",
      `${scenePath(at)}/photos?search=${encodeURIComponent(search)}`);
    // 等结果的时候用户可能已经关了窗、或者换了一段。
    if (photoPick.at !== at) return;
    photoPick.photos = (data && data.photos) || [];
    ui.photoSearch.value = (data && data.query) || search;
    photoNote(`${photoPick.photos.length} photo`
      + `${photoPick.photos.length === 1 ? "" : "s"} for “${data.query}”. `
      + "Click one to draw this scene from it.");
    drawPhotos();
  } catch (error) {
    if (photoPick.at !== at) return;
    photoPick.photos = [];
    photoNote(String(error.message || error), "warn");
  } finally {
    photoPick.busy = false;
    ui.photoFind.disabled = false;
  }
}

function drawPhotos() {
  ui.photoGrid.innerHTML = "";
  for (const photo of photoPick.photos) {
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = "shelf-tile free" + (photo.used ? " used" : "");
    const shot = document.createElement("img");
    shot.className = "shelf-shot";
    shot.loading = "lazy";
    // 不把"我从哪个页面来的"告诉图床。
    shot.referrerPolicy = "no-referrer";
    shot.alt = "";
    shot.src = photo.thumb || photo.url;
    // 缩略图坏了的那一张多半原图也下不来；摆一个点了必失败的格子没有用。
    shot.addEventListener("error", () => tile.remove());
    tile.appendChild(shot);
    const meta = document.createElement("div");
    meta.className = "shelf-meta";
    const where = document.createElement("span");
    where.className = "shelf-where";
    where.textContent = photo.used ? "Used in another scene" : (photo.host || "");
    meta.appendChild(where);
    const credit = [photo.license, photo.creator].filter(Boolean).join(" · ");
    if (credit) {
      const line = document.createElement("span");
      line.className = "shelf-when";
      line.textContent = credit;
      meta.appendChild(line);
    }
    tile.appendChild(meta);
    tile.title = photo.page_url || photo.url;
    tile.addEventListener("click", () => redrawFromPhoto({ photo_url: photo.url }));
    ui.photoGrid.appendChild(tile);
  }
}

/** 照着挑好的那一张（或者传上来的那一张）排一次重画。``extra`` 空着就是
    原来那条全自动的路：服务端自己搜、自己挑。 */
function redrawFromPhoto(extra) {
  const { at, keyword, style } = photoPick;
  if (at < 0) return;
  // 刚挑的一张先摆进画框里，等用户看过、摆好再画。已经在手里的那一张
  // （点的是"照这张画"）就带着摆好的大小和位置去画。
  const held = samePhotoChoice(extra, photoInHand.extra);
  if (extra && !held && photoFrameFirst.checked && photoAddress(extra)) {
    holdPhoto(extra, "The photo you chose");
    photoNote("Set its size and position if you like, then click "
      + "“Draw from this photo”.");
    return;
  }
  const frame = held ? photoFrameChosen() : null;
  const strength = photoKeepOf(photoKeepChoice.input);
  const search = ui.photoSearch.value.trim();
  const photo = {
    ...(extra || {}), ...(search ? { search } : {}), ...(frame ? { frame } : {}),
  };
  // 开画之前挑照片的那一轮：记下来，下一段。全挑完才排队。
  if (photoRound.on) {
    keepRoundPhoto({ strength, photo });
    return;
  }
  closePhotoPicker();
  queueRegeneration(at, keyword, style, true, strength, photo);
}

/** 按框里的词搜。框空着就先让模型写一条，而不是拿空词去搜。 */
function searchPhotosFromBox() {
  const words = ui.photoSearch.value.trim();
  if (words) findPhotos(words);
  else writePhotoWords();
}
ui.photoFind.addEventListener("click", searchPhotosFromBox);
ui.photoSearch.addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchPhotosFromBox();
});
// 打了字还没搜就关窗，下次打开也该是这几个字。
ui.photoSearch.addEventListener("input", () => {
  if (photoPick.key) photoPick.words[photoPick.key] = ui.photoSearch.value;
});
ui.photoClose.addEventListener("click", closePhotoPicker);
ui.photoAuto.addEventListener("click", () => redrawFromPhoto(null));
ui.photoKept.addEventListener("click", () => {
  // 照着画过的那一张优先；没有就用开画之前定下、存着的那一张。
  if (photoPick.kept) redrawFromPhoto({ photo_kept: photoPick.kept });
  else if (photoPick.saved) redrawFromPhoto({ photo_picked: true });
});
ui.photoSkip.addEventListener("click", () => {
  if (photoRound.on && photoPick.at >= 0) keepRoundPhoto({ skip: true });
});
ui.photoUpload.addEventListener("click", () => pickFile("image/*", async (file) => {
  photoNote(`Uploading ${file.name}…`, "pending");
  try {
    const data = await upload(
      `/tasks/${encodeURIComponent(state.taskId)}/photos`, file);
    redrawFromPhoto({ photo_upload: data.file });
  } catch (error) {
    photoNote(String(error.message || error), "warn");
  }
}));
ui.photoModal.addEventListener("click", (event) => {
  if (event.target === ui.photoModal) closePhotoPicker();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !ui.photoModal.hidden) closePhotoPicker();
});

/* ---------- 在 Google 图片里自己挑 ----------

   点"照着照片重画"，这个程序就在本机上开一个小浏览器窗口，里面是 Google
   图片搜这一段的词。用户在那里点一张图把它放大，再点那张大图——就是它了，
   这边直接开画。不需要任何搜索 API、不需要密钥，也没有额度。

   窗口是服务端开的（一张网页读不到别的网站的窗口里点了什么），所以这里
   做的只有三件事：叫它开、每秒问一次"点了没有"、关窗时叫它关。

   粘贴和拖进来也收：从别处复制来的一张图，按 Ctrl+V 就用它。 */
const browsing = { on: false, timer: 0, session: "" };

const GOOGLE_HOW = ui.photoGoogleNote.innerHTML;

function googleNote(text, kind) {
  ui.photoGoogleNote.className = "hint" + (kind ? " " + kind : "");
  if (text) ui.photoGoogleNote.textContent = text;
  else ui.photoGoogleNote.innerHTML = GOOGLE_HOW;
}

function photoBrowserPath() {
  return `/tasks/${encodeURIComponent(state.taskId)}/photos/browse`;
}

function stopWatchingPhotoBrowser() {
  browsing.on = false;
  clearTimeout(browsing.timer);
}

/** 开那个窗口去搜 ``words``。开不了就说是什么开不了——不悄悄换成别的。 */
async function browseGoogle(words) {
  if (photoPick.at < 0 || !words) return;
  const at = photoPick.at;
  stopWatchingPhotoBrowser();
  ui.photoGoogle.disabled = true;
  googleNote("Opening a browser window on Google Images…", "pending");
  try {
    const data = await call("POST",
      `${scenePath(at)}/photos/browse?search=${encodeURIComponent(words)}`);
    if (photoPick.at !== at) { closePhotoBrowser(); return; }
    browsing.session = (data && data.session) || "";
    browsing.on = true;
    googleNote("");
    watchPhotoBrowser(at);
  } catch (error) {
    if (photoPick.at !== at) return;
    googleNote(String(error.message || error), "warn");
  } finally {
    ui.photoGoogle.disabled = false;
  }
}

function watchPhotoBrowser(at) {
  clearTimeout(browsing.timer);
  browsing.timer = setTimeout(async () => {
    if (!browsing.on || photoPick.at !== at) return;
    let data = null;
    try {
      data = await call("GET", photoBrowserPath());
    } catch {
      // 问不到一次不算什么，下一秒再问。
    }
    if (!browsing.on || photoPick.at !== at) return;
    if (data && data.session === browsing.session) {
      if (data.state === "picked" && data.file) {
        stopWatchingPhotoBrowser();
        redrawFromPhoto({ photo_upload: data.file });
        return;
      }
      if (data.state === "closed" || data.state === "none") {
        stopWatchingPhotoBrowser();
        googleNote("The browser window was closed without choosing a picture. "
          + "Click Search on Google to open it again.");
        return;
      }
    }
    watchPhotoBrowser(at);
  }, 1000);
}

function closePhotoBrowser() {
  const was = browsing.on;
  stopWatchingPhotoBrowser();
  if (!was || !state.taskId) return Promise.resolve();
  return call("DELETE", photoBrowserPath()).catch(() => {});
}

ui.photoGoogle.addEventListener("click", () => {
  const words = ui.photoSearch.value.trim();
  if (!words) {
    googleNote("Type what to search for first.", "warn");
    return;
  }
  if (photoPick.key) photoPick.words[photoPick.key] = words;
  browseGoogle(words);
});

/** 粘贴或拖进来的一张图：和"传一张自己的"一样，收下就开画。 */
async function takeCopiedPhoto(blob) {
  if (photoPick.at < 0) return;
  const at = photoPick.at;
  googleNote("Getting that picture…", "pending");
  try {
    const kind = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg")
      .replace(/[^a-z0-9]/g, "") || "png";
    const file = blob instanceof File && blob.name
      ? blob : new File([blob], `copied-photo.${kind}`, { type: blob.type });
    const data = await upload(
      `/tasks/${encodeURIComponent(state.taskId)}/photos`, file);
    if (photoPick.at !== at) return;
    redrawFromPhoto({ photo_upload: data.file });
  } catch (error) {
    if (photoPick.at !== at) return;
    const why = String(error.message || error);
    // 服务端不收太小的图。从网页上带回来的小图几乎总是缩略图。
    googleNote(/long edge/.test(why)
      ? "That picture is too small to draw from — it is a small preview. Open "
        + "the picture large first and copy that one."
      : why, "warn");
  }
}

document.addEventListener("paste", (event) => {
  if (ui.photoModal.hidden || !event.clipboardData) return;
  const file = [...event.clipboardData.files].find((one) => one.type.startsWith("image/"));
  if (!file) return;
  event.preventDefault();
  takeCopiedPhoto(file);
});

const photoBox = ui.photoModal.querySelector(".modal-box");
ui.photoModal.addEventListener("dragover", (event) => {
  event.preventDefault();
  photoBox.classList.add("dropping");
});
ui.photoModal.addEventListener("dragleave", () => photoBox.classList.remove("dropping"));
ui.photoModal.addEventListener("drop", (event) => {
  event.preventDefault();
  photoBox.classList.remove("dropping");
  const file = event.dataTransfer
    && [...event.dataTransfer.files].find((one) => one.type.startsWith("image/"));
  if (file) takeCopiedPhoto(file);
  else googleNote("Only a picture file can be dropped here.", "warn");
});

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

/** ``height`` 是存下来的那一帧有多高。

    轨道上一格只有几十像素，72 就够；而"画过的每一张图"那一屏是用来认画面
    的，72 像素放大到 150 宽全是糊的，挑不出来。尺寸进缓存键：同一个地址的
    两种尺寸是两张图,共用一个键会让其中一边拿到错的那张。 */
function requestThumb(scene, img, height = 72) {
  const at = Number(scene.trim_start || 0);
  const key = `${scene.url}@${at.toFixed(2)}@${height}`;
  const cached = thumbCache.get(key);
  if (cached) { img.src = cached; return; }
  thumbQueue.push({ key, url: scene.url, at, img, height });
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
        const dataUrl = await grabFrame(job.url, job.at, job.height || 72);
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

function grabFrame(url, at, height = 72) {
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
        // 按用处存，不按原分辨率存：轨道上一格几十像素，存一张 1080p 的
        // data URL 是白占内存。
        canvas.height = height;
        canvas.width = Math.max(
          8, Math.round(height * (video.videoWidth / video.videoHeight || 0.5625))
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
    bar.dataset.line = index;
    bar.classList.toggle("selected", index === state.line);
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
      // The line is also what the right-hand panel is about now: that is
      // where it can be heard on its own and recorded again.
      savePromptEdit();
      state.line = index;
      state.card = null;
      state.adding = false;
      drawInspector();
      markLine();
    });
    // 双击直接改这一条的文字。画面上那行字也能点开同一个面板，但旁白比
    // 画面长的时候后面那几句没有画面可点，这里是它们唯一的入口。
    bar.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      openCaption(index);
    });
    bar.title += "\nDouble-click to edit this caption";
    ui.audioTrack.appendChild(bar);
  });
}

/* ---------- one narration line ----------
   Click a line of the narration and the right-hand panel is about that line:
   hear it alone, or have the voice say it again. Only that line is recorded;
   it is put back where the old take was and everything after it moves by the
   difference in length. */

/** The narration line the panel is about, or null. */
function selectedLine() {
  const segments = (state.data && state.data.narration_segments) || [];
  const index = Number.isInteger(state.line) ? state.line : -1;
  return index >= 0 && index < segments.length
    ? { index, ...segments[index] }
    : null;
}

/** Only the highlight; the bars are not rebuilt. */
function markLine() {
  for (const bar of ui.audioTrack.querySelectorAll(".voice-bar.line")) {
    bar.classList.toggle("selected", Number(bar.dataset.line) === state.line);
  }
}

/* The bars are drawn at the caption times, which are estimates and can be a
   second off. The server knows when the words were really heard, and how the
   script writes the line. Asked for once per line and take. */
const lineHeard = { key: "", data: null };

async function heardLine(line) {
  const key = `${state.taskId}:${line.index}:${ui.narration.src}`;
  if (lineHeard.key === key) return lineHeard.data;
  let data = null;
  try {
    data = await call(
      "GET", `/tasks/${encodeURIComponent(state.taskId)}/narration/${line.index}`);
  } catch { /* an older server: the caption's own times and text */ }
  lineHeard.key = key;
  lineHeard.data = data;
  return data;
}

/** Play from the start of a line and stop where it ends. */
async function playLine(line) {
  if (state.playing) stop();
  const heard = await heardLine(line);
  const from = heard ? Math.max(0, heard.start - 0.1) : line.start;
  const until = heard ? heard.end + 0.2 : line.end;
  state.playhead = from;
  state.loadedClip = -1;
  syncPreview(true);
  movePlayhead();
  play();
  const watch = setInterval(() => {
    if (!state.playing) { clearInterval(watch); return; }
    if (state.playhead >= until) { clearInterval(watch); stop(); }
  }, 40);
}

/** Why this line cannot be recorded again right now; "" when it can. */
function retakeRefusal() {
  if (state.data && state.data.audio_stale) {
    return "The script was changed since the narration was recorded, so the "
      + "lines no longer match it. Re-record the whole narration first.";
  }
  if (state.taskBusy) return "This video is busy. Wait for it to finish.";
  if ((state.video || {}).running) {
    return "Video scenes are being made and have the whole graphics card. "
      + "Record the line when they are done.";
  }
  return "";
}

/** The retake of this line that is waiting or being recorded, if any. */
function lineJob(line) {
  return (state.queue.jobs || []).find(
    (job) => job.kind === "narration" && job.line === line.index
      && (job.state === "queued" || job.state === "running")) || null;
}

/** Puts the retake in the queue. The request comes back at once: the line
    waits its turn behind the pictures, and is never recorded while one is
    drawn. */
async function retakeLine(line) {
  try {
    const job = await call(
      "POST",
      `/tasks/${encodeURIComponent(state.taskId)}/narration/${line.index}/retake`);
    say(job && job.position > 1
      ? `Line ${line.index + 1} queued to be recorded again — ${job.position - 1} ahead of it.`
      : `Line ${line.index + 1} queued to be recorded again. Queue another or keep editing.`);
    await fetchQueue();
    drawInspector();
    watchQueue();
  } catch (error) {
    say(String(error.message || error), "error");
  }
}

function deleteLine(line) {
  if (!window.confirm(
    `Delete narration line ${line.index + 1}?\n\n"${line.text}"\n\n`
    + "Its sound and its caption are taken out, and everything after it comes "
    + "earlier. This cannot be undone.")) return undefined;
  return run(`Deleting line ${line.index + 1}`, async () => {
    if (state.playing) stop();
    const made = await call(
      "DELETE", `/tasks/${encodeURIComponent(state.taskId)}/narration/${line.index}`);
    const address = (state.data && state.data.audio_url) || "";
    if (address) {
      ui.narration.src = address + (address.includes("?") ? "&" : "?") + "take=" + Date.now();
    }
    // The line after it now has this number; the panel goes back to the scene.
    state.line = -1;
    const by = made && made.removed ? Number(made.removed) : 0;
    return `Line ${line.index + 1} was deleted; the narration is ${by.toFixed(1)}s shorter.`;
  });
}

function drawLinePanel(line) {
  ui.inspTitle.textContent = `Narration line ${line.index + 1}`;

  const said = inspectorGroup("What is said");
  const text = document.createElement("p");
  text.className = "line-text";
  text.textContent = line.text;
  const when = document.createElement("p");
  when.className = "hint";
  when.textContent = `${line.start.toFixed(2)}s – ${line.end.toFixed(2)}s · `
    + `${(line.end - line.start).toFixed(1)}s long`;
  const listen = document.createElement("div");
  listen.className = "row";
  listen.append(button("Play this line", "small", () => playLine(line)));
  said.append(text, when, listen);
  ui.inspBody.appendChild(said);
  // The line as the script writes it, which is what the voice is given.
  heardLine(line).then((heard) => {
    if (heard && heard.text && text.isConnected) text.textContent = heard.text;
  });

  const again = inspectorGroup("Record it again");
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = "The same voice says this line again, with the same "
    + "words, and the new take replaces the old one. Nothing else is recorded. "
    + "A new take is never exactly as long as the old one, so the picture on "
    + "screen and everything after this line move by the difference.";
  const refusal = retakeRefusal();
  const record = button("Record this line again", "small", () => retakeLine(line));
  const lineWaiting = lineJob(line);
  record.disabled = !!refusal || !!state.busy || !!lineWaiting;
  const row = document.createElement("div");
  row.className = "row";
  row.append(record);
  again.append(note, row);
  if (refusal) {
    const why = document.createElement("p");
    why.className = "hint warn";
    why.textContent = refusal;
    again.appendChild(why);
  } else {
    const wait = document.createElement("p");
    wait.className = "hint";
    wait.textContent = lineWaiting
      ? (lineWaiting.state === "running"
        ? "This line is being recorded now."
        : "This line is in the queue"
          + (lineWaiting.position > 1 ? `, ${lineWaiting.position - 1} ahead of it.` : ", next."))
        + " The new take appears here when it is done."
      : "It goes in the queue with the pictures, so it never runs while one "
        + "is drawn, and takes about a minute once its turn comes. To change "
        + "the words, edit the script and re-record the narration instead.";
    again.appendChild(wait);
  }
  ui.inspBody.appendChild(again);

  const out = inspectorGroup("Delete it");
  const outNote = document.createElement("p");
  outNote.className = "hint";
  outNote.textContent = "Takes this line out of the narration: its sound, its "
    + "caption and its place in the script. The picture on screen gets "
    + "shorter and everything after it comes earlier. It cannot be undone.";
  const remove = button("Delete this line", "small", () => deleteLine(line));
  remove.disabled = !!refusal || !!state.busy
    || ((state.data && state.data.narration_segments) || []).length < 2;
  const outRow = document.createElement("div");
  outRow.className = "row";
  outRow.append(remove);
  out.append(outNote, outRow);
  ui.inspBody.appendChild(out);

  const more = document.createElement("div");
  more.className = "row";
  more.append(
    button("Edit the caption…", "small", () => openCaption(line.index)),
    button("Add a title card here", "small", async () => {
      // On the words of this line, at the time they are really said.
      const heard = await heardLine(line);
      openCard(null, heard ? heard.start : line.start);
    }),
    button("Back to the scene", "small", () => {
      state.line = -1;
      markLine();
      drawInspector();
    }),
  );
  ui.inspBody.appendChild(more);
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
  // 上限是用户自己设的那条线，所以状态栏里也要给出"离它还差多少"——
  // 那个数就是他要挤掉或者补上的量。
  let overLimit = false;
  if (state.limit) {
    const over = total - state.limit;
    label += ` · limit ${fmt(state.limit)}`;
    if (Math.abs(over) > 0.05) {
      overLimit = over > 0;
      label += over > 0
        ? ` (${over.toFixed(1)}s over)`
        : ` (${(-over).toFixed(1)}s to fill)`;
    }
  }

  ui.totals.textContent = label;
  ui.totals.className = "totals"
    + (drift ? " drift" : "")
    + (overLimit ? " over-limit" : "");

  // 对不齐的时候才给这个按钮：素材张数由关键词个数决定，画面比旁白短是
  // 常态，而这是"同样的画面每段停更久"那个答案。
  ui.fit.hidden = !drift || !narration;
  ui.fit.title = `Stretch these ${state.scenes.length} clips to cover the `
    + `${narration.toFixed(1)}s narration (about `
    + `${(narration / state.scenes.length).toFixed(1)}s each). `
    + "Generated images are re-rendered at the new length.";
}

/* ---------- 片长上限 ----------

   CapCut 上那条线。用户要的不是"帮我剪到 60 秒"，而是"给我一条线，我自己
   对着它挤"。所以这里设了之后什么都不会自动改：画面、配音、配乐对着同一
   条线去调，比盯着状态栏里三个数字互相对要容易得多。

   存在 localStorage 而不是清单里：它不参与渲染，改它不该让任务变脏，也不
   该要求后端多一个字段。读不出来就当没设。 */

/** "60"、"1:30"、"90s" 都认，读不出来就是没设上限。

    用户脑子里的片长可能是秒也可能是分秒，两种都认比强迫他换算要好。 */
function parseLimit(text) {
  const raw = String(text == null ? "" : text).trim().replace(/s$/i, "");
  if (!raw) return null;
  const parts = raw.split(":");
  let seconds = NaN;
  if (parts.length === 2) {
    const minutes = parseFloat(parts[0]);
    const rest = parseFloat(parts[1]);
    if (isFinite(minutes) && isFinite(rest)) seconds = minutes * 60 + rest;
  } else if (parts.length === 1) {
    seconds = parseFloat(raw);
  }
  if (!isFinite(seconds) || seconds <= 0) return null;
  return Math.round(seconds * 10) / 10;
}

/** 输入框里显示成什么样：一分钟以内就写秒数，过了就写 m:ss。 */
function limitText(seconds) {
  if (!seconds) return "";
  return seconds >= 60 ? fmt(seconds) : String(seconds).replace(/\.0$/, "");
}

function limitKey() {
  return state.taskId ? `mpt_limit_${state.taskId}` : "";
}

/** 换任务时读回这条任务自己的上限。 */
function loadLimit() {
  state.limit = null;
  const key = limitKey();
  if (key) {
    try { state.limit = parseLimit(localStorage.getItem(key)); } catch { }
  }
  ui.limit.value = limitText(state.limit);
}

function saveLimit() {
  const key = limitKey();
  if (!key) return;
  // 隐私模式下存不进去。这只是个顺手的基准，不该因为存不上就报错。
  try {
    if (state.limit) localStorage.setItem(key, String(state.limit));
    else localStorage.removeItem(key);
  } catch { }
}

/** 上限那条线，以及它右边超出去的那一截。

    拖动时每帧都要走这里，所以它只动这几个节点，不重画整条时间线——
    重画会把每段画面连缩略图一起重建，拖起来就卡住了。 */
function paintLimit() {
  const limit = state.limit;
  if (!limit) {
    ui.limitLine.hidden = true;
    ui.limitOver.hidden = true;
    return;
  }
  ui.limitLine.hidden = false;
  ui.limitLine.style.left = (limit * state.pxPerSecond) + "px";
  ui.limitLine.dataset.label = fmt(limit);
  ui.limitLine.title = `Limit ${fmt(limit)} — drag to move, Alt to ignore the cuts`;

  // 超出去的那一截铺成斜纹，用户一眼看到的是"还要挤掉这么多"。
  const over = Math.max(0, projectTotal() - limit);
  ui.limitOver.hidden = over <= 0.05;
  ui.limitOver.style.left = (limit * state.pxPerSecond) + "px";
  ui.limitOver.style.width = (over * state.pxPerSecond) + "px";
  ui.limitOver.title = `${over.toFixed(1)}s past the ${fmt(limit)} limit`;

  // 拖的时候输入框要跟着走，否则线到了位置、框里的数字还停在原处。
  if (document.activeElement !== ui.limit) ui.limit.value = limitText(limit);

  // 拖过内容末尾时把轨道撑长，否则线会跑到可视区外面去。
  const needed = (limit * state.pxPerSecond) + 60;
  for (const node of [ui.ruler, ui.videoTrack, ui.audioTrack]) {
    if (needed > parseFloat(node.style.width || "0")) node.style.width = needed + "px";
  }
}

/** 这条线可以吸附到哪些位置。

    "对齐"要的是**正好**落在某个切点上，不是差 0.2 秒。所以拖的时候给每个
    画面的出入点、旁白结尾一个吸附：松手的位置就是真正的切点,而不是用户
    手抖之后的那个数。按住 Alt 可以不吸附。 */
function limitSnapPoints() {
  const points = [];
  const offsets = sceneOffsets();
  offsets.forEach((start, index) => {
    points.push(start);
    points.push(start + screenDuration(state.scenes[index]));
  });
  const narration = narrationLength();
  if (narration) points.push(narration);
  return points;
}

/** 拖动上限那条线。

    输入框是用来打一个准数的，这条线是用来"对着看"的——而对齐这件事手比
    眼睛准：把线拖到那一刀上，比读出那一刀是 12.4 秒再去框里打一遍快得多。 */
function startLimitDrag(event) {
  if (!state.limit) return;
  event.preventDefault();
  event.stopPropagation();

  const startX = event.clientX;
  const startLimit = state.limit;
  let next = startLimit;
  ui.limitLine.classList.add("dragging");

  const onMove = (move) => {
    const delta = (move.clientX - startX) / state.pxPerSecond;
    next = Math.max(0.1, startLimit + delta);

    // 吸附在屏幕上量，不在秒上量：缩放变了，手感不该跟着变。
    if (!move.altKey) {
      const reach = 6 / state.pxPerSecond;
      let best = null;
      for (const point of limitSnapPoints()) {
        const gap = Math.abs(point - next);
        if (gap <= reach && (best === null || gap < Math.abs(best - next))) {
          best = point;
        }
      }
      if (best !== null) next = best;
    }
    next = Math.round(next * 10) / 10;
    state.limit = next;
    paintLimit();
    drawTotals();
  };

  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    ui.limitLine.classList.remove("dragging");
    saveLimit();
    // 松手之后整条重画一次：上限拖过了内容末尾时，标尺要跟着长出来。
    draw();
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

ui.limitLine.addEventListener("pointerdown", startLimitDrag);

ui.limit.addEventListener("change", () => {
  state.limit = parseLimit(ui.limit.value);
  // 回写一遍：用户打的是 "90"，线上写的是 1:30，两边要说同一件事。
  ui.limit.value = limitText(state.limit);
  saveLimit();
  draw();
});

/* ---------- 提示词随改随存 ----------

   这一屏的全部意义是"先把提示词改对，再开画"。而"开画"和"先试一张"
   读的是**清单里存着的**那个词，不是框里的字：改完没点保存就开画，等于
   照着旧词画两百张。最坏的是这事根本看不出来：框里明明写着新的。

   所以改完就存：停手半秒存一次，焦点离开再存一次，开画之前还要再确认
   一次没有落下的那一笔。

   这里不走 ``run()``：那会禁掉整屏按钮、弹一条 "Saving…"、再重读整条
   时间线。改一个词不该有这么大动静，所以只在没存上的时候说话。 */

const PROMPT_SAVE_DELAY = 600;

/** 正在编辑的那个提示词框：``{ id, input, saved, timer }``。

    记的是场景**身份**而不是下标。存一次要等半秒，而这半秒里用户完全
    可以去拖动画面；下标一旦不再指向同一段，这一笔就存到别人头上了。 */
let promptEdit = null;

/** "用 AI 写这条提示词"那一小块的状态，按段落身份一段一份：
    ``id -> { id, open, direction, undo, note, kind, show }``。

    留在模块级而不是闭包里，因为整个 inspector 会被重画（存完一次、队列
    轮询回来一次），闭包里的东西活不过那一次重画。按身份分开存是因为写一条
    要等模型几十秒，这中间用户会去改别的段落——回来时这一段的撤销内容、那
    句小字、还有"正在写"都得还在。

    ``show`` 是这一段那一小块此刻挂在屏上的那个重画函数。面板重画之后它会
    被换成新的那个，旧的那个自己认得出来（见 ``drawPromptAssist``）。 */
const promptAssists = new Map();

function assistState(id) {
  let found = promptAssists.get(id);
  if (!found) {
    found = {
      id, open: false, direction: "", undo: null, note: "", kind: "", show: null,
    };
    promptAssists.set(id, found);
  }
  return found;
}

/** 这一段有没有在排队写提示词（排着的和正在写的都算）。

    写这件事现在是服务端队列里的一个活。写提示词要让 ollama 把模型装进
    显存，而出图那边正占着同一块显卡——两件事排同一条队才不会撞上，这也是
    它从这个页面搬到服务端去的全部理由。

    顺带的好处：进度不再只活在这一个标签页里。关掉页面再打开，看到的还是
    同一份队列。 */
function promptJobFor(id) {
  const at = sceneIndexById(id);
  if (at < 0) return null;
  return (state.queue.jobs || []).find(
    (job) => job.kind === "prompt"
      && jobScene(job) === at
      && (job.state === "queued" || job.state === "running")
  ) || null;
}

/** 输入框下面那一行小字：``{ id, text, kind }``，``null`` 是不说话。

    自动保存最大的毛病是看不见：存了没存都是同一副样子，用户只能赌。
    没存上的时候尤其要命：他以为存下了，然后照旧词画了两百张。 */
let promptStatus = null;
let promptStatusTimer = 0;

/** 就地改那一行小字，不重画整个面板——重画会把正在打字的框换掉。

    只在屏上真的停在那一段时才动 DOM：存的是上一段而人已经看到别处时，
    把 "Saved" 写到别人的框下面比什么都不写还坏。 */
function setPromptStatus(id, message, kind) {
  clearTimeout(promptStatusTimer);
  promptStatus = message ? { id, text: message, kind: kind || "" } : null;
  const scene = (state.scenes || [])[state.selected];
  const showing = scene && (scene.id || materialKey(scene.material)) === id;
  const node = ui.inspBody.querySelector(".prompt-status");
  if (!node || !showing) return;
  node.textContent = message || "";
  node.className = "hint prompt-status" + (kind ? " " + kind : "");
}

/** 按身份找它现在落在第几段。和 ``jobScene`` 用的是同一把尺子。 */
function sceneIndexById(id) {
  if (!id) return -1;
  return (state.scenes || []).findIndex(
    (scene) => (scene.id || materialKey(scene.material)) === id
  );
}

/** 把框里的字存下去。没改过、或者那一段已经不在了，就什么都不做。

    不传参就是"把现在这个框里没存的那一笔存掉"——开画之前谁都可以调一下。 */
async function savePromptEdit(edit) {
  const target = edit || promptEdit;
  if (!target) return;
  clearTimeout(target.timer);
  const value = String(target.input.value);
  if (value === target.saved) return;
  // 没有身份可认时（早于 scene_id 的老清单）退回接管时的那个下标——和
  // 以前那个 "Save prompt" 按钮一样，不更坏。有身份就一律按身份找，认不
  // 出来就说明那一段已经没了，这一笔没有归宿，宁可不存。
  const at = target.id ? sceneIndexById(target.id) : target.index;
  if (!Number.isInteger(at) || at < 0 || !(state.scenes || [])[at]) return;
  // 先记下来再发：发的过程里用户还在打字，回头不能把更新的字当成旧的。
  target.saved = value;
  setPromptStatus(target.id, "Saving…");
  try {
    await call("PUT", scenePath(at), { keyword: value });
  } catch (error) {
    // 没存上就要说，而且下一次还要再试。默默失败比根本没有自动保存
    // 更坏：用户以为存下了，然后照旧词画了两百张。
    target.saved = null;
    setPromptStatus(target.id, `Not saved: ${error.message || error}`, "warn");
    say(`The prompt was not saved: ${error.message || error}`, "error");
    return;
  }
  setPromptStatus(target.id, "Saved");
  promptStatusTimer = setTimeout(() => {
    if (promptStatus && promptStatus.id === target.id
        && promptStatus.text === "Saved") {
      setPromptStatus(target.id, "");
    }
  }, 2500);
  const scene = (state.scenes || [])[at];
  if (scene) scene.keyword = value;
  // 时间线上每一段写的就是它的提示词，存完要跟着变。但框还在手里时
  // 不能重画整屏（会把框连光标一起换掉），所以只把那一行的前半截换了。
  // 不换的话时间线上会一直写着旧词，看上去就是"根本没存下"。
  if (document.activeElement === target.input) {
    relabelClip(at);
  } else {
    draw();
  }
}

/** 时间线上每一段写的就是它的提示词，存完要跟着变。

    只换这一行的字，不整屏重画：重画会把正在打字的框连光标一起换掉。不换的
    话时间线上会一直写着旧词，看上去就是"根本没存下"。 */
function relabelClip(at) {
  const scene = (state.scenes || [])[at];
  const label = ui.videoTrack.querySelector(
    `.clip[data-index="${at}"] .clip-text`
  );
  if (!label || !scene) return;
  label.textContent = `${at + 1}. ${scene.keyword || scene.material || ""}`
    + (label.dataset.suffix || "");
}

/** 接管一个提示词框。换到别的场景时先把上一笔存掉（不等它）。 */
function trackPromptEdits(scene, input) {
  if (promptEdit && promptEdit.input !== input) savePromptEdit(promptEdit);
  const edit = {
    id: scene.id || materialKey(scene.material),
    index: scene.index,
    input,
    saved: String(scene.keyword || ""),
    timer: 0,
  };
  promptEdit = edit;
  input.addEventListener("input", () => {
    clearTimeout(edit.timer);
    edit.timer = setTimeout(() => savePromptEdit(edit), PROMPT_SAVE_DELAY);
  });
  // 焦点离开就别再等那半秒了。点别的按钮也会先经过这里。
  input.addEventListener("change", () => savePromptEdit(edit));
}

/** 请队列去写这一段的提示词。

    不在这里写，也不在这里存：这个活跑的时候这个页面可能已经关了，而写好的
    提示词不该跟着没了。服务端写完会把它存进场景，然后**接着**排一个出图的
    活——写提示词几乎永远是为了换一张图。

    撤销的底在按下去的这一刻留：那一刻框里是什么，撤销就该还原成什么。 */
async function startPromptWrite(id, direction, current) {
  if (promptJobFor(id)) return;
  const at = sceneIndexById(id);
  const scene = at >= 0 ? state.scenes[at] : null;
  const assist = assistState(id);
  if (!scene) {
    assist.note = "that scene is no longer in the timeline";
    assist.kind = "warn";
    if (assist.show) assist.show();
    return;
  }
  assist.undo = String(scene.keyword || "");
  try {
    const job = await call(
      "POST", `${scenePath(at)}/prompt/queue`, { direction, current });
    assist.note = job && job.position > 1
      ? `Queued — ${job.position - 1} ahead of it. The picture is redrawn `
        + "straight after, and both are in the queue below."
      : "Queued. The picture is redrawn straight after, and both are in the "
        + "queue below.";
    assist.kind = "";
    await fetchQueue();
    draw();
    watchQueue();
  } catch (error) {
    assist.note = String(error.message || error);
    assist.kind = "warn";
    if (assist.show) assist.show();
  }
}

/** "用 AI 写这条提示词"那一小块。

    为什么写出来的直接填回上面那个框
    --------------------------------
    框里那几个字**就是**拿去出图的提示词。模型写好的东西如果存在别处，用户
    看到的就不再是真正会被画出来的那一条。填回去之后接着走的还是原来那条
    自动保存，和手打一个字完全一样——所以"保存"、"重出一张"全都不用改。

    为什么多一个撤销
    ----------------
    手打是一个字一个字来的，顶掉的是自己刚写的半句；这里是一下子换掉一整
    条，被顶掉的可能是用户认真写了很久的那条。默认收起来也是同一个道理：
    自己写才是主路，这是旁边那条。 */
function drawPromptAssist(scene, input) {
  const id = scene.id || materialKey(scene.material);
  // 一段一份，按身份取：上一段的撤销内容按到这一段上就是张冠李戴，而换回来
  // 时这一段写到哪儿了又得还在。
  const state_ = assistState(id);

  const box = document.createElement("details");
  box.className = "drawer prompt-assist";
  box.open = state_.open;
  box.addEventListener("toggle", () => { state_.open = box.open; });

  const title = document.createElement("summary");
  title.textContent = "Write this prompt with AI";
  box.appendChild(title);

  const assistHelp =
    "Say in a few words what this picture should show, or what to change "
    + "about it, then press Write the prompt — it rewrites what is in the "
    + "box above. Suggest another ignores the box and thinks of a fresh "
    + "picture for this scene's narration, for when what is there is not "
    + "working and you want a different idea rather than a tidier version "
    + "of the same one. Either way the people, the period and the setting "
    + "of the rest of the video are kept and Undo puts your own words back. "
    + "The picture is redrawn as soon as the prompt is written — that lands "
    + "in the queue below, so cancel it there if you would rather read the "
    + "words first. Writing takes a while, so you can move to another scene "
    + "while it writes — it finishes on its own.";

  const direction = field("What it should show", "input", state_.direction);
  addHelp(direction.wrap, assistHelp);
  direction.input.placeholder = "at night · have her smiling · not in an office";
  direction.input.addEventListener("input", () => {
    state_.direction = direction.input.value;
  });
  box.appendChild(direction.wrap);

  const note = document.createElement("p");
  const undo = button("Undo", "small", async () => {
    if (state_.undo === null || promptJobFor(id)) return;
    input.value = state_.undo;
    state_.undo = null;
    state_.note = "Put back the prompt that was there before.";
    state_.kind = "";
    input.focus();
    await savePromptEdit();
    show();
  });

  // 这一小块现在的样子全部从 ``state_`` 和"正在写哪几段"里读出来，所以写的
  // 过程中换段再换回来，看到的还是同一个进度。
  //
  // 面板一重画就会有新的一块，旧的那个 ``show`` 指向的节点已经被摘下来了：
  // 认出自己不是当家的那个（``state_.show`` 已经换人）就什么都不做，免得把
  // "写好了"写到一块早就不在屏上的 DOM 上。
  function show() {
    if (state_.show !== show) return;
    const busy = !!promptJobFor(id);
    write.disabled = busy;
    write.textContent = busy ? "In the queue…" : "Write the prompt";
    // 一段一次只写一条，所以两个按钮共用同一个"忙"。
    suggest.disabled = busy;
    undo.disabled = busy;
    note.className = "hint" + (state_.kind ? " " + state_.kind : "");
    note.textContent = state_.note;
    undo.hidden = state_.undo === null;
  }

  // 点下去只是**开始写**：请求交给 ``startPromptWrite`` 带着这一段的身份自
  // 己跑完，用户接着去改别的段落都不打断它。以前这件事在这个闭包里等结果，
  // 换一段就把框和闭包一起换掉了，写好的那条无处可落。
  const write = button("Write the prompt", "small", () => {
    if (promptJobFor(id)) return;
    startPromptWrite(id, direction.input.value, input.value);
    show();
  });

  // 同一条路，只是**不把框里那条递过去**：模型于是从这一段的旁白重新想一
  // 条，而不是把现有的润色一遍。"我不要这个想法"和"这句话写得不好"是两件
  // 事，而原来只有后面那个按钮——想换个想法得先自己把框清空，谁也猜不到。
  const suggest = button("Suggest another", "small", () => {
    if (promptJobFor(id)) return;
    startPromptWrite(id, direction.input.value, "");
    show();
  });

  const row = document.createElement("div");
  row.className = "row";
  row.append(write, suggest, undo);
  box.append(row, note);
  // 从这一刻起这一块才是这一段当家的那一块（上面那个 ``show`` 的规矩）。
  state_.show = show;
  show();
  return box;
}


/** 检查器里的一块：一个小标题，下面是这条路自己的选项和按钮。 */
function inspectorGroup(title) {
  const box = document.createElement("div");
  box.className = "insp-group";
  const head = document.createElement("div");
  head.className = "insp-group-title";
  head.textContent = title;
  box.appendChild(head);
  return box;
}

/** 这一段现在这张图是照着哪张照片画的。照片本身存在任务目录里，所以这里
    摆的是它，不是一个可能已经失效的地址：画得好不好，对着照片一眼就知道
    是照片的事还是画的事。 */
/** 给这一段存着、还没照着画的那张照片。 */
function savedPhotoNote(scene) {
  const box = document.createElement("div");
  box.className = "used-photo";
  const address = `${API}${scenePath(scene.index)}/photo-pick`;
  const shot = document.createElement("img");
  shot.alt = "The photo saved for this scene";
  shot.loading = "lazy";
  shot.src = address;
  shot.title = "Open the photo full size";
  shot.addEventListener("click", () => window.open(address, "_blank", "noopener"));
  // 文件不在了就不摆一个破图标。
  shot.addEventListener("error", () => shot.remove());
  box.appendChild(shot);
  const words = document.createElement("div");
  const head = document.createElement("b");
  head.textContent = "Photo saved for this scene";
  words.appendChild(head);
  words.appendChild(document.createTextNode("chosen before drawing"));
  box.appendChild(words);
  return box;
}

function usedPhotoNote(scene) {
  const used = scene.reference;
  if (!used || typeof used !== "object") return null;
  const box = document.createElement("div");
  box.className = "used-photo";
  const words = document.createElement("div");
  const head = document.createElement("b");
  words.appendChild(head);
  const lines = [];
  if (used.width && used.height) lines.push(`${used.width} × ${used.height}`);
  if (used.follow === AS_IT_IS) {
    lines.push("the photo itself, not redrawn");
  } else if (used.follow) {
    lines.push("same picture, in your style");
  } else if (used.strength) {
    lines.push(used.strength <= 0.7 ? "kept the photograph" : "loosely inspired by it");
  }
  if (used.query) lines.push(`searched: ${used.query}`);
  if (used.file) {
    head.textContent = used.follow === AS_IT_IS
      ? "This photo, as it is" : "Drawn from this photo";
    const address =`${API}/tasks/${encodeURIComponent(state.taskId)}`
      + `/photos/used/${encodeURIComponent(used.file)}`;
    const shot = document.createElement("img");
    shot.alt = "The photo this scene was drawn from";
    shot.loading = "lazy";
    shot.src = address;
    shot.title = "Open the photo full size";
    shot.addEventListener("click", () => window.open(address, "_blank", "noopener"));
    box.appendChild(shot);
  } else {
    // 存照片这件事是后来才有的：以前画的那些只记了地址。
    head.textContent = "Drawn from a photo";
    // 没存下来的那些，照片还在它原来的网站上就从那里显示。那边撤了图、或者
    // 不许别的页面引用，这一格就摘掉，只剩下面那个链接。
    const remote = String(used.source_url || "");
    if (/^https?:\/\//i.test(remote)) {
      head.textContent = "Drawn from this photo";
      const shot = document.createElement("img");
      shot.alt = "The photo this scene was drawn from";
      shot.loading = "lazy";
      // 不把"我从哪个页面来的"告诉图床。
      shot.referrerPolicy = "no-referrer";
      shot.src = remote;
      shot.title = "Open the photo full size";
      shot.addEventListener("click", () => window.open(remote, "_blank", "noopener"));
      shot.addEventListener("error", () => {
        shot.remove();
        head.textContent = "Drawn from a photo";
      });
      box.appendChild(shot);
      lines.push("Shown from its website; no copy was kept.");
    } else {
      lines.push("The photo itself was not kept — redraw from a photo to keep it.");
    }
  }
  for (const line of lines) {
    const row = document.createElement("span");
    row.textContent = line;
    words.append(row, document.createElement("br"));
  }
  const source = String(used.page_url || used.source_url || "");
  if (/^https?:\/\//i.test(source)) {
    const link = document.createElement("a");
    link.href = source;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `from ${host(source)}`;
    words.appendChild(link);
  }
  box.appendChild(words);
  return box;
}

/* ---------- inspector ---------- */

function drawInspector() {
  ui.inspBody.innerHTML = "";
  if (state.adding) { drawAddForm(); return; }
  if (state.card) { drawCardPanel(state.card); return; }
  const line = selectedLine();
  if (line) { drawLinePanel(line); return; }

  const scene = state.scenes[state.selected];
  if (!scene) {
    ui.inspTitle.textContent = "Nothing selected";
    return;
  }
  ui.inspTitle.textContent = `Scene ${scene.index + 1}${scene.locked ? " · locked" : ""}`;
  inspectorSection("Picture");

  // 这个框在两种源下根本不是一回事，所以名字也不能是同一个。
  //
  // 文生图：这几个字**就是**提示词，原样交给出图模型（后面接画风模板），
  // 不改写、不补充。叫它 "Keyword" 会让人以为只是个检索词，于是用户往里
  // 塞两三个名词,而它其实能写一整句画面描述——那正是出图模型要的。
  // 库存素材：它真的是一个检索词，拿去库里搜。
  const keyword = field(
    isGenerated(scene) ? "Picture prompt" : "Search keyword",
    "textarea",
    scene.keyword || ""
  );
  keyword.input.rows = 2;
  // 空着的那几段（关键词常常比场景少）会拿主题加这一段的台词去画。把它当
  // 占位文字摆出来，用户看得见要画什么，改一笔就是自己的。
  if (!scene.keyword && scene.keyword_suggestion) {
    keyword.input.placeholder = scene.keyword_suggestion;
  }
  ui.inspBody.append(keyword.wrap);
  // 存没存下去要看得见。重画之后也要还在，所以话存在 ``promptStatus``
  // 里，不存在这个节点上。
  const promptNote = document.createElement("p");
  promptNote.className = "hint prompt-status";
  const sceneKey = scene.id || materialKey(scene.material);
  if (promptStatus && promptStatus.id === sceneKey) {
    promptNote.textContent = promptStatus.text;
    if (promptStatus.kind) promptNote.classList.add(promptStatus.kind);
  }
  ui.inspBody.appendChild(promptNote);
  // 改完就存。下面那个 "Save prompt" 按钮留着：它现在是"我现在就要存"，
  // 而不再是"不点就丢"。
  trackPromptEdits(scene, keyword.input);

  // 第二条路：写一句短话，让模型把它补成一条完整的提示词。上面那个框照旧
  // 可以自己从头写——这里加的是"不想记那十来条规矩时"的入口，不是替换。
  // 只对文生图的段落出现：库存源那个框是检索词,补成一整句反而搜不到。
  if (isGenerated(scene)) {
    ui.inspBody.appendChild(drawPromptAssist(scene, keyword.input));
  }

  // 画风紧跟在关键词下面：关键词说画什么，画风说画成什么样，改完一起
  // 重生成。默认是这条片子出片时用的那个风格——以前重生成读的是 API
  // 进程"现在"的全局配置，漫画风的片子因此补出过一张油画。
  const style = state.imageStyles.length
    ? stylePicker(scene.image_style || "")
    : null;
  // 画一张的两条路各占一块，各自的选项紧挨着各自的按钮。
  const drawBox = inspectorGroup(isGenerated(scene) ? "Draw it from the prompt" : "Find another clip");
  if (style) drawBox.append(style.wrap);

  // 生成新画面是这个面板里最常用的动作：画面不对时要改的通常就是关键词，
  // 改完紧接着就要重出一张。所以它紧贴在关键词下面，而不是和锁定、删除
  // 挤在最底下那一排。
  //
  // 本机出一张图要几分钟（实测 zimage-turbo 约 3 分钟），所以点下去只是
  // **排队**：请求立刻回来，按钮也不禁用，用户接着挑下一段点"重出"，或者
  // 去改别处的台词和时长。等着出的那几张在上面那条队列栏里，出完一张
  // 时间线自己就换成新画面。
  const regenerate = button(regenerateLabel(scene), "primary", async () => {
    // 框里的字先落盘再排队。这一张本来就是照这几个字画的，但清单里
    // 存着的也得是它——不然待会儿"开画"还会照旧词把这一段再画一遍。
    await savePromptEdit();
    queueRegeneration(
      scene.index,
      keyword.input.value || null,
      // 下拉框在就把它的值发出去，空字符串是"和这条片子一样"，
      // 和"没有下拉框"(null,沿用上次挑的)不是一回事。
      style ? style.input.value : null
    );
  });
  drawBox.appendChild(regenerate);
  ui.inspBody.appendChild(drawBox);

  // 第二条画法：先去网上搜一张真实照片，再照着它画。
  //
  // 为什么需要它：文生图模型没见过"那架 1952 年的彗星号客机"，它画出来的
  // 是"一架老飞机"——形状、窗户、年代全是编的。而网上有那架飞机的照片。
  // 照着照片画，构图和样子对得上，画风仍然是上面那个下拉框里挑的那个。
  //
  // 只对生成源出现：库存素材源是去库里搜一个现成文件，没有"照着这张画"
  // 这回事（接口那边也会拒）。
  if (isGenerated(scene)) {
    // "保留多少照片"不在这里选了：它在挑照片的那扇窗里，和照片一起定。
    const photoBox = inspectorGroup("Draw it from a photo");

    const fromPhoto = button("Redraw from a photo", "", async () => {
      await savePromptEdit();
      // 不再直接开画：先把搜到的照片摆出来给人挑（或者传一张自己的）。
      openPhotoPicker(
        scene,
        keyword.input.value || null,
        style ? style.input.value : null
      );
    });
    fromPhoto.title = "Search the web for photographs of this and choose the "
      + "one to draw this scene from, or upload your own. The search words "
      + "are written from this scene's narration and prompt, and you can "
      + "change them.";
    photoBox.appendChild(fromPhoto);
    // 现在这张图是照着哪张照片画的，就摆在这条路下面。
    const used = usedPhotoNote(scene);
    if (used) photoBox.appendChild(used);
    // 同一张照片，换一档"留多少"再画一次。看了结果才知道该留多少是常事，
    // 不该为此重新去搜、去点。只有存下来的照片才行：服务端手里得有它。
    const kept = scene.reference && scene.reference.file;
    // 没照着画过、但从 Google 挑过（或者传过）一张存着的，也算"同一张"：
    // 照片已经在这条片子里了，不该为了用它再去搜一遍。
    const saved = !kept && scene.photo_pick === "photo";
    if (saved) photoBox.appendChild(savedPhotoNote(scene));
    if (kept || saved) {
      const again = button("Redraw from this same photo", "small", async () => {
        await savePromptEdit();
        // 开的是同一扇窗，但不去搜：在里面选留多少，再点"用现在这张"。
        openPhotoPicker(
          scene,
          keyword.input.value || null,
          style ? style.input.value : null,
          true
        );
      });
      again.title = (kept
        ? "Draw this scene again from the photo shown here."
        : "Draw this scene from the photo already saved for it.")
        + " A window opens to choose how much of the photo to keep — nothing "
        + "is searched for again.";
      photoBox.appendChild(again);
    }
    ui.inspBody.appendChild(photoBox);
  }

  ui.inspBody.appendChild(videoSceneBox(scene));

  // 第三条路，而且是最该先试的那一条：这张图以前画过的某一个版本。
  //
  // 为什么它紧跟着那两个"重出"按钮：用户来这块面板最常做的事就是重出，而
  // 重出一次就盖掉一张。看过新的才知道旧的更好是常事，所以"回到旧的"必须
  // 和"再出一张"摆在一起——藏在别处的话，用户只会再点一次重出去赌。
  const earlier = button("Earlier media…", "small",
    () => openPictureShelf(scene));
  earlier.title = "Every picture and video clip this video has made, including "
    + "the ones no scene is using any more. Putting one back is instant — the "
    + "file is already on disk, so nothing is made again.";
  // 不画图的两个小按钮并排成一行，和上面两块分开。
  const pair = document.createElement("div");
  pair.className = "insp-pair";
  pair.appendChild(earlier);
  // The scene's own file gets a row to itself, under the first: bring one in,
  // or find the one that is there. Four buttons do not fit one row.
  const files = document.createElement("div");
  files.className = "insp-pair";
  const own = button("Upload…", "small", () => uploadSceneFile(scene));
  own.title = "Put your own picture or video on this scene: a clip made in "
    + "another program, or a photo. What the scene shows now is kept under "
    + "“Earlier media”.";
  files.appendChild(own);
  if (!isPending(scene) && !EMBEDDED) {
    const show = button("Show in folder", "small", () => showSceneFile(scene, show));
    show.title = "Opens this video's folder with this scene's picture selected: "
      + "the image file itself, ready to drag into another program. For a "
      + "video scene that is the picture it was made from.";
    files.appendChild(show);
  }
  ui.inspBody.appendChild(pair);
  ui.inspBody.appendChild(files);
  const misfit = clipMisfit(scene);
  if (misfit) {
    const row = document.createElement("div");
    row.className = "insp-pair";
    const fit = button(
      misfit === "short" ? "Slow the clip to fit this scene"
        : "Speed the clip up to fit this scene",
      "small", () => fitSceneClip(scene));
    fit.title = misfit === "short"
      ? "This clip ends before the scene does. Slows it, by up to half speed, "
        + "with the frames in between filled in, so it covers the scene. The "
        + "original stays under “Earlier media”."
      : "This clip is longer than the scene, so its end is cut off. Plays it "
        + "faster, by up to double speed, so all of it fits. The original "
        + "stays under “Earlier media”.";
    row.appendChild(fit);
    ui.inspBody.appendChild(row);
  }

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

  // 这一段以前是摆在按钮下面的一整段字。现在收在提示词标题旁边的 "?" 里。
  addHelp(keyword.wrap, isGenerated(scene)
    ? "The text in this box is the prompt, used exactly as written with the "
      + "style added after it — nothing is rewritten and nothing else is sent. "
      + "A full sentence works better than a few nouns: say who is in shot, "
      + "what they are doing, and when and where. Pictures are drawn one at a "
      + "time and a local image server takes a few minutes each, so "
      + "generating only joins the queue — keep working while it runs."
    : "Stock searches are cached, so the same keyword returns the same clip. "
      + "Change the keyword to get a different one.");

  /* 这里不再显示"这段画面上会念到什么"。

     配音是一整条，字幕按那条配音的时间轴生成，所以某张图上念的是什么，只由
     它在成片里的起止时间决定。把第 9 张拖到第 2 位，念的词不跟着走——第 2 位
     念的还是第 2 位那几句。

     原来的那个框标题写着 "Spoken here"，重排之后它显示的是新位置上的词,
     看起来却像是"这张图的台词被换掉了"。它说的其实是对的,但没人会这么读：
     用户看到的是一个贴在这张图上的文本框,里面的字自己变了。

     而这件事根本不需要一个文本框来回答：按播放，听见的就是这一刻念的词，
     预览上还压着那一刻的字幕。整段正文要改就开 Script，那里改的是"念什么",
     本来就该在整段文字上做,不是在某一张图上做。 */

  const save = button(
    isGenerated(scene) ? "Save prompt" : "Save keyword",
    "small",
    () => run("Saving", () =>
      call("PUT", scenePath(scene.index), { keyword: keyword.input.value })),
  );
  pair.appendChild(save);

  // 这一段怎么出场。放在时长前面：先决定它长什么样，再决定它停多久。
  inspectorSection("Effects");
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
    // 切到这一段时响的那一声。音效库是本机目录，所以列表来自接口。
    const sounds = (state.data && state.data.cut_sounds) || [];
    const soundLabels = {};
    for (const name of sounds) soundLabels[name] = name.replace(/\.[^.]+$/, "");
    soundLabels.none = "Silent";
    const sound = sounds.length
      ? inheritPicker("Sound on the cut", ["none", ...sounds], soundLabels,
          scene.cut_sound || "", videoCutSoundLabel())
      : null;

    if (cut) ui.inspBody.append(cut.wrap);
    if (motion) ui.inspBody.append(motion.wrap);
    if (sound) ui.inspBody.append(sound.wrap);

    // 挂在这一组的第一项上：三项讲的是同一件事（什么时候生效）。
    const firstEffect = cut || motion || sound;
    if (firstEffect) {
      addHelp(firstEffect.wrap, "The transition and the sound are added when "
        + "the video is rendered. The motion is baked into this scene's clip, so "
        + "changing it re-renders the clip from its picture — a few seconds, and "
        + "instant once you have used that setting before. The sound plays on "
        + "the cut into this scene, at the video's transition-sound volume.");
    }

    ui.inspBody.appendChild(button("Apply effects", "small", () =>
      run("Applying the effects", () =>
        call("PUT", scenePath(scene.index) + "/effects", {
          transition: cut ? cut.input.value : null,
          image_motion: motion ? motion.input.value : null,
          cut_sound: sound ? sound.input.value : null,
        }))));
  }

  inspectorSection("Timing");
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
      + (scene.holds_last_frame
        ? " The scene can be any length: past the end of the clip, its last frame stays on screen."
        : "")
    : "Source length unknown.";
  ui.inspBody.appendChild(note);

  if (state.speed !== 1) {
    const speedNote = document.createElement("p");
    speedNote.className = "hint warn";
    speedNote.textContent = `Clip speed is ${state.speed}x, so 1s on screen uses `
      + `${state.speed.toFixed(2)}s of source.`;
    ui.inspBody.appendChild(speedNote);
  }

  // 分节到此为止：锁定和删除是对整段说的，不属于哪一节。
  inspectorSection("");
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
  groupInspector();
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

/** 一个地址属于哪个网站。照片来自哪里要说出来，但整条 URL 占满一行。 */
function host(url) {
  try {
    return new URL(String(url)).hostname.replace(/^www\./, "");
  } catch {
    return String(url || "");
  }
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
  if (!isGenerated(scene)) return "Find another clip";
  // 还没画过的那一段不是"重出",是第一次出。按钮写着"Generate new image"
  // 时没人会去点它:这一屏在等"开画",用户以为得先让整批跑起来才轮得到这
  // 一张。而这里恰恰是最该点的地方——只排这一张,看过画风再决定整批。
  return isPending(scene) ? "Draw this one now" : "Generate new image";
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
  zoom_out: "Zoom out", shuffle: "Random each time", cross_fade: "Crossfade",
};

const MOTION_LABELS = {
  zoom: "Slow zoom", static: "Hold still",
  varied: "Varied (a different move per picture)", zoom_out: "Pull back",
  pan_right: "Pan right", pan_left: "Pan left",
  zoom_top: "Push in on the upper third",
};

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

/* 整条片子现在用的那一声。音量是整条片子一个值，每一段挑的是"响哪一声"。 */
function videoCutSoundLabel() {
  const settings = (state.data && state.data.render_settings) || {};
  const raw = String(settings.transition_sound || "").trim();
  if (!raw) return "Same as the video (silent)";
  return `Same as the video (${raw.replace(/\.[^.]+$/, "")})`;
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
  pencil_sketch: "Pencil sketch", pixel_art: "Pixel art",
  claymation: "Claymation", paper_cutout: "Paper cut-out",
  engraving: "Old engraving", low_poly: "Low-poly", noir: "Film noir",
  cyberpunk: "Cyberpunk neon",
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
  // 拖之前先把没存的那一笔存掉：拖完了下标全变，那一笔就难找归宿了。
  savePromptEdit();
  const index = Number(event.currentTarget.dataset.index);
  state.selected = index;
  state.adding = false;
  // Clicking a picture takes the panel back from a narration line or a card.
  state.line = -1;
  state.card = null;
  markLine();
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
  // 拉边框也会把面板换到别的段上去，和点别的画面一样要先存。
  savePromptEdit();
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
      // 想拖多长就多长，不按素材长度夹。图片会按新时长重渲；视频没有更多
      // 的帧，多出来的那一截定在最后一帧上——出片时如此，预览里播放器走到
      // 文件末尾也正是这样停的。
      //
      // As long as the user drags it: no cap at the material's length. A
      // picture is drawn again at the new length. A clip has no more frames,
      // so its last one is held for the rest, in the finished video and in
      // the preview alike (a player stops on the last frame of a file).
      nextSpan = Math.max(MIN_DURATION, startSpan + deltaSeconds);
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

/** 第几次换片段。晚到的回调拿它认出自己已经过期。 */
let clipSwap = 0;

/** 把 <video> 眼下这一帧画到盖在它上面的画布上，换片段的空当里看到的是它。 */
function holdCurrentFrame() {
  const video = ui.preview;
  // 还没有一帧可画（刚打开、上一段也没加载完）：没有东西可留。
  if (!ui.hold || !video.videoWidth || video.readyState < 2) return;
  try {
    ui.hold.width = video.videoWidth;
    ui.hold.height = video.videoHeight;
    ui.hold.getContext("2d").drawImage(video, 0, 0);
    ui.hold.style.objectFit = video.style.objectFit || "contain";
    ui.hold.hidden = false;
  } catch { /* 画不了就照旧：黑一下，不挡播放 */ }
}

function releaseHeldFrame() {
  if (ui.hold) ui.hold.hidden = true;
}

/** 放着的时候是不是旁白在走时间（和 ``tick`` 里的判断是同一个）。 */
function narrationIsTheClock() {
  return !!ui.narration.src && !ui.narration.paused;
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
    releaseHeldFrame();
    state.loadedClip = -1;
    return;
  }
  ui.gap.hidden = true;
  ui.preview.style.visibility = "visible";

  if (!scene || !scene.exists) {
    ui.frame.classList.remove("ready");
    releaseHeldFrame();
    return;
  }
  ui.frame.classList.add("ready");
  if (index !== state.loadedClip) {
    const advancing = state.loadedClip >= 0;
    state.loadedClip = index;
    // 换 src 的那一刻 <video> 是空的，新片段的第一帧到之前露出黑底：每换一段
    // 就黑一下。所以先把眼下这一帧留在上面，新的一帧出来了再撤。
    holdCurrentFrame();
    const swap = ++clipSwap;
    ui.preview.muted = true;
    ui.preview.src = scene.url;
    // 元数据到位之前 currentTime 写不进去，所以等一次 loadedmetadata。
    ui.preview.addEventListener("loadedmetadata", () => {
      // 等的这一会儿里又换了一段：这一次的定位已经不作数了。
      if (swap !== clipSwap) return;
      seekWithin(scene, offset);
      if (state.playing) ui.preview.play().catch(() => {});
      const shown = () => { if (swap === clipSwap) releaseHeldFrame(); };
      if (typeof ui.preview.requestVideoFrameCallback === "function") {
        ui.preview.requestVideoFrameCallback(shown);
      } else {
        ui.preview.addEventListener("seeked", shown, { once: true });
      }
    }, { once: true });
    // 片段加载不出来也不能一直盖着上一段的画面。
    setTimeout(() => { if (swap === clipSwap) releaseHeldFrame(); }, 2000);
    if (advancing && state.playing) playTransition();
    return;
  }
  if (force) {
    seekWithin(scene, offset);
    // 片段停在了自己的最后一帧（等旁白走完这一段）之后又被拖回段内：接着放。
    if (state.playing && ui.preview.paused) ui.preview.play().catch(() => {});
  }
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
  // 字幕跟着播放头走：播放时每帧都会走到这里，拖动时也一样。
  drawSubtitle();
  drawDirection();
  soundDirection();
}

function tick() {
  if (!state.playing) return;
  const now = performance.now();
  // 旁白是成片的时间基准，所以有配音时以它为准；没有就用画面自己走。
  if (ui.narration.src && !ui.narration.paused) {
    state.playhead = ui.narration.currentTime;
  } else {
    const { index, offset } = clipAt(state.playhead);
    const scene = state.scenes[index];
    // ``ended`` as well as ``paused``: a clip that has run out while its scene
    // has not is the held last frame, and the clock has to keep walking.
    if (scene && !ui.preview.paused && !ui.preview.ended) {
      const within = ui.preview.currentTime - (scene.trim_start || 0);
      state.playhead = sceneOffsets()[index] + Math.max(0, within / state.speed);
    } else {
      // 照真的过了多久走，不是"一帧六十分之一秒"：120Hz 的屏幕上那样就是
      // 两倍速。切到别的标签页再回来那一下不算（封顶十分之一秒）。
      state.playhead += Math.min(0.1, (now - (state.tickAt || now)) / 1000) * state.rate;
    }
    void offset;
  }
  state.tickAt = now;

  // 走到画面和旁白里更长的那个才算放完。只按画面长度收尾的话，画面比
  // 旁白短时后面那段旁白永远听不到。
  if (state.playhead >= projectTotal()) {
    stop();
    state.playhead = 0;
    state.loadedClip = -1;
    syncPreview(true);
    movePlayhead();
    followPlayhead(true);
    return;
  }
  const { index } = clipAt(state.playhead);
  if (index !== state.loadedClip) syncPreview(false);
  movePlayhead();
  followPlayhead();
  requestAnimationFrame(tick);
}

/* ---------- 预览的播放速度 ----------
   十几分钟的片子按原速审一遍就是十几分钟。这里只让预览放快：旁白是时间基准，
   它快了播放头就快了，画面、配乐、环境声和重音都跟着同一个倍数走。出片不受
   影响（那是 state.speed，片段本身的速度，另一回事）。 */
const RATES = [1, 1.5, 2];
const RATE_KEY = "mpt_preview_rate";

function savedRate() {
  let saved = 1;
  try { saved = Number(localStorage.getItem(RATE_KEY)); } catch { /* 记不住就原速 */ }
  return RATES.includes(saved) ? saved : 1;
}

/** 预览里出声、出画面的每一个元素。 */
function pacedMedia() {
  return [
    ui.narration, ui.preview, ui.musicBed,
    ...Object.values(directionSounds.beds), ...directionSounds.hits,
  ];
}

/** 把倍数写到每个元素上。换 src 会把 playbackRate 重置成 defaultPlaybackRate，
    所以两个都写。 */
function applyRate() {
  for (const media of pacedMedia()) {
    if (!media) continue;
    media.defaultPlaybackRate = state.rate;
    media.playbackRate = state.rate;
  }
  const button = document.getElementById("rate");
  if (button) {
    button.textContent = `${state.rate}×`;
    button.classList.toggle("fast", state.rate !== 1);
  }
}

function nextRate() {
  state.rate = RATES[(RATES.indexOf(state.rate) + 1) % RATES.length];
  try { localStorage.setItem(RATE_KEY, String(state.rate)); } catch { /* 下次从原速开始 */ }
  applyRate();
}

function play() {
  if (!state.scenes.length) return;
  state.playing = true;
  ui.play.textContent = "Pause";
  applyRate();
  state.tickAt = 0;
  // 一按播放，红线就该在眼前——哪怕刚才滚到别处去了。
  followPlayhead(true);
  if (ui.narration.src) {
    try { ui.narration.currentTime = state.playhead; } catch { /* 元数据未就绪 */ }
    ui.narration.play().catch(() => {});
  }
  if (ui.musicBed.src) {
    // 曲子通常比片子短，所以是循环的；播放头落在哪里，就从曲子里对应的
    // 那一点接着放，这样来回拖动时听到的和成片里的那一刻一致。
    try {
      const length = ui.musicBed.duration;
      ui.musicBed.currentTime = Number.isFinite(length) && length > 0
        ? state.playhead % length
        : 0;
    } catch { /* 元数据未就绪 */ }
    ui.musicBed.play().catch(() => {});
  }
  ui.preview.play().catch(() => {});
  // 重音：正响到一半的那一记从中途接上；环境声由 soundDirection 每帧管。
  directionSounds.last = state.playhead;
  startHitsUnder(state.playhead);
  requestAnimationFrame(tick);
}

function stop() {
  state.playing = false;
  ui.play.textContent = "Play";
  ui.preview.pause();
  ui.narration.pause();
  ui.musicBed.pause();
  quietDirection();
}

/* ---------- wiring ---------- */

ui.load.addEventListener("click", () => load(ui.task.value));
ui.task.addEventListener("keydown", (event) => {
  if (event.key === "Enter") load(ui.task.value);
});

ui.play.addEventListener("click", () => (state.playing ? stop() : play()));
document.getElementById("rate").addEventListener("click", nextRate);
state.rate = savedRate();
applyRate();

ui.zoom.addEventListener("input", () => {
  state.pxPerSecond = Number(ui.zoom.value);
  draw();
  centerOnPlayhead();
});

/** 缩放之后把红线摆回可视区正中。
 *
 * 缩放改的是每秒多少像素，滚动条的像素位置却不变，于是同一个 scrollLeft 换了
 * 一个时间：人正看着的那一段一下子跑到别处去了。红线是"我在看这里"，所以
 * 以它为中心；靠近开头或结尾时浏览器自己把滚动量收在边界上。
 */
function centerOnPlayhead() {
  const at = state.playhead * state.pxPerSecond;
  ui.scroll.scrollLeft = Math.max(0, at - ui.scroll.clientWidth / 2);
}

/* ---------- 放的时候时间线跟着红线走 ----------
   不跟的话，红线几秒钟就走出可视区，人得一边看一边自己横向滚，一松手就又
   找不到它了。做法是"平滑滚动"：红线先自己往右走，走到可视区正中就停在那里，
   之后是时间线从它底下往左流过去——红线一直在同一个地方，前后各看得见半屏。
   （试过"翻页"：红线到右边缘、时间线一下子跳一页。跳的那一下人会跟丢。）

   人自己滚走了（想看看后面、去改别的段）就先不跟，过几秒再接着跟；不然
   每一帧都被拽回来，放着的时候就哪儿也去不了。 */
const FOLLOW_HOLD_MS = 4000;
const follow = { expected: -1, holdUntil: 0, lag: 0, at: 0 };
/** 落后了多少，每秒追回去的比例（指数）：大约半秒追上大半。 */
const FOLLOW_CATCH_UP = 5;
/** 一帧里红线多走出这么多像素，就不是在"放"，是被点到前面去了。 */
const FOLLOW_JUMP_PX = 24;

function followPlayhead(force = false) {
  const view = ui.scroll;
  const width = view.clientWidth;
  if (!width) return;
  const now = performance.now();
  if (!force && now < follow.holdUntil) { follow.lag = 0; return; }
  const elapsed = Math.min(0.1, (now - (follow.at || now)) / 1000);
  follow.at = now;
  const at = state.playhead * state.pxPerSecond;
  const left = view.scrollLeft;
  // 红线在正中时时间线该在的位置。
  const target = Math.max(0, at - width / 2);
  const put = (where) => {
    view.scrollLeft = where;
    follow.expected = view.scrollLeft;
  };
  // 红线不在眼前（滚走过）：没有什么可"滑"的，直接摆到正中。
  if (at < left || at > left + width) {
    follow.lag = 0;
    put(target);
    return;
  }
  // 时间线比"红线在正中"落后多少。还没走到正中就是不落后：让红线自己走。
  const behind = target - left;
  if (behind <= 0) { follow.lag = 0; return; }
  // 放着的时候往前面点了一下：红线一下子到了正中右边。这时候把时间线直接
  // 摆过去就是"跳"——人刚点的那个地方从手底下跑了。改成追：红线先留在点的
  // 地方，时间线加快流过去，半秒左右红线回到正中。
  if (behind > follow.lag + FOLLOW_JUMP_PX) follow.lag = behind;
  follow.lag *= Math.exp(-FOLLOW_CATCH_UP * elapsed);
  if (follow.lag < 0.5) follow.lag = 0;
  put(target - follow.lag);
}

ui.scroll.addEventListener("scroll", () => {
  // 自己翻的页不算"人滚走了"。
  if (Math.abs(ui.scroll.scrollLeft - follow.expected) < 2) return;
  follow.expected = -1;
  if (state.playing) follow.holdUntil = performance.now() + FOLLOW_HOLD_MS;
}, { passive: true });

/** 把播放头放到某一秒——放着的时候也能放。
 *
 * 为什么要动旁白：放着的时候旁白才是时钟（见 ``tick``，它每一帧都把播放头
 * 读成 ``narration.currentTime``）。只改 ``state.playhead`` 的话，下一帧就
 * 被旁白拽回原处，看起来就是"播放时拖不动，必须先暂停"。
 *
 * 背景音乐是循环的，所以按余数对位，拖到哪儿听到的就是成片里那一刻的那一
 * 段，而不是从头开始的另一段。
 */
function scrubTo(seconds) {
  state.playhead = Math.max(0, Math.min(projectTotal(), seconds));
  if (ui.narration.src) {
    try { ui.narration.currentTime = state.playhead; } catch { /* 元数据未就绪 */ }
  }
  if (ui.musicBed.src) {
    try {
      const length = ui.musicBed.duration;
      ui.musicBed.currentTime = Number.isFinite(length) && length > 0
        ? state.playhead % length
        : 0;
    } catch { /* 元数据未就绪 */ }
  }
  // 这里不重置 loadedClip：同一段之内 syncPreview(true) 只是改 currentTime，
  // 很便宜。清掉它等于每挪一个像素都把同一个文件重新加载一遍，拖动会卡成
  // 幻灯片，而且每次重载都算一次"切片段"，转场动画会一直闪。
  syncPreview(true);
  movePlayhead();
}

// 在时间线上点一下就把播放头移到那里，按住还能拖着走——这是剪辑软件最基本
// 的期待，而且放着的时候也该管用。
for (const track of [ui.ruler, ui.audioTrack]) {
  track.addEventListener("pointerdown", (event) => {
    const rect = track.getBoundingClientRect();
    const secondsAt = (clientX) => (clientX - rect.left) / state.pxPerSecond;
    scrubTo(secondsAt(event.clientX));

    // 指针捕获：拖出轨道范围（往上拖到素材区、往下拖出窗口）也继续跟手，
    // 松开才停。没有它的话指针一离开轨道就不再收到事件，播放头卡在半路。
    try { track.setPointerCapture(event.pointerId); } catch { /* 老浏览器 */ }
    const onMove = (move) => scrubTo(secondsAt(move.clientX));
    const onUp = () => {
      track.removeEventListener("pointermove", onMove);
      track.removeEventListener("pointerup", onUp);
      track.removeEventListener("pointercancel", onUp);
      try { track.releasePointerCapture(event.pointerId); } catch { /* 同上 */ }
    };
    track.addEventListener("pointermove", onMove);
    track.addEventListener("pointerup", onUp);
    track.addEventListener("pointercancel", onUp);
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
  // 旁白在走时间的时候，片段比它该占的时间早放完就停在最后一帧等着。在这里
  // 把播放头推到下一段的话，下一帧 tick 又照旁白把它拉回来：两段来回重新
  // 加载，一直闪到旁白真的走过这条边界。
  if (narrationIsTheClock()) return;
  const { index } = clipAt(state.playhead);
  const offsets = sceneOffsets();
  // A scene set longer than its clip keeps the last frame on screen for the
  // rest of its time, as the finished video does. The player has already
  // stopped on that frame; tick walks the playhead to the end of the scene.
  if (index >= 0
    && state.playhead < offsets[index] + screenDuration(state.scenes[index]) - 0.1) return;
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
    // 同上：旁白在走时间时停在出点这一帧，换段的事交给 tick。
    if (narrationIsTheClock()) { ui.preview.pause(); return; }
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

/* 打开编辑器时这条任务已经在渲染（别人点的，或者刚才刷新过页面）：挡板
   同样要放出来，否则用户会对着一个什么都点不动的界面发呆。 */
function adoptRunningRender(task) {
  if (!task || Number(task.state) !== 4) return;
  if (!ui.renderModal.hidden) return;
  showRender("This video is already rendering.");
  state.renderStarted = Number(task.generation_started_at || 0) * 1000
    || Date.now();
  updateRender(task.progress, (task.generation_activity || {}).phase);
}

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

/* ---------- 字幕预览 ----------

   把成片里那一刻会压上去的字，按成片的字号、颜色、描边和位置画在预览框上。
   审片时真正要判断的是"这行字会不会盖住主体、在手机上够不够大"，而这两件
   事光看一份 SRT 文本是看不出来的。

   句子的时间点就是字幕文件里的时间点（``narration_segments``），所以逐词
   模式不需要特别处理：那种模式下字幕本身就是一词一条，这里照画即可。 */
function activeCue() {
  const segments = (state.data && state.data.narration_segments) || [];
  const at = state.playhead;
  // 从后往前找：时间轴是有序的，而播放时命中的多半是靠后的那几条。
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const cue = segments[index];
    if (at >= cue.start && at < cue.end) return { ...cue, index };
  }
  return null;
}

function subtitleSettings() {
  const settings = (state.data && state.data.render_settings) || {};
  return settings.subtitle || null;
}

function drawSubtitle() {
  drawYouTube();
  const style = subtitleSettings();
  const cue = style && style.enabled ? activeCue() : null;
  if (!cue || !String(cue.text || "").trim()) {
    ui.subs.hidden = true;
    state.cueIndex = -1;
    return;
  }

  const settings = (state.data && state.data.render_settings) || {};
  const videoHeight = Number(settings.frame_height) || 1920;
  const frameHeight = ui.frame.clientHeight || 0;
  if (!frameHeight) { ui.subs.hidden = true; return; }
  // 预览框比成片小得多，所以字号、描边都要按同一个比例缩。
  const scale = frameHeight / videoHeight;

  ui.subs.hidden = false;
  ui.subsText.textContent = String(cue.text);
  // 哪一条正在屏幕上。点它就是改这一条。
  state.cueIndex = cue.index;
  ui.subsText.title = "Click to edit this caption";
  const text = ui.subsText.style;
  text.fontSize = Math.max(8, (Number(style.font_size) || 60) * scale) + "px";
  text.color = style.color || "#FFFFFF";
  const stroke = (Number(style.stroke_width) || 0) * scale;
  text.webkitTextStrokeWidth = stroke > 0 ? stroke + "px" : "0";
  text.webkitTextStrokeColor = style.stroke_color || "#000000";
  // text_background_color 是 false 或一个颜色，和渲染器那边一个含义。
  const background = style.background;
  text.background = typeof background === "string" && background
    ? background
    : "transparent";
  text.padding = typeof background === "string" && background
    ? Math.max(2, 6 * scale * (videoHeight / 1920)) + "px " + Math.max(4, 14 * scale) + "px"
    : "0";

  placeSubtitle(style, frameHeight);
  noteCaptionClash();
}

/** 字幕的纵向位置，照抄渲染器那几条规则（见 video.py 的 subtitle_position）。 */
function placeSubtitle(style, frameHeight) {
  // 先量一次它自己有多高：除了 top，其余几种位置都要用到。
  ui.subs.style.top = "0px";
  const height = ui.subs.offsetHeight || 0;
  const position = String(style.position || "bottom");
  let top;
  if (position === "bottom") {
    // 渲染器把字的**底边**放在 95% 处。
    top = frameHeight * 0.95 - height;
  } else if (position === "top") {
    top = frameHeight * 0.05;
  } else if (["two_thirds_bottom", "two_thirds", "2/3_bottom"].includes(position)) {
    top = (frameHeight - height) / 3;
  } else if (position === "custom") {
    const margin = 10 * (frameHeight / (Number(
      (state.data.render_settings || {}).frame_height) || 1920));
    const wanted = (frameHeight - height) * (Number(style.custom_position) || 0) / 100;
    top = Math.max(margin, Math.min(wanted, frameHeight - height - margin));
  } else {
    top = (frameHeight - height) / 2;
  }
  ui.subs.style.top = Math.max(0, top) + "px";
}

/* ---------- 导演项的预览 ----------

   片名、标题卡、重音、环境声、配乐让开的那几段，都是精修那一遍出片时才加
   上去的：预览放的只是每一段的素材和旁白。所以以前一张摆错位置的标题卡，
   要等整条片子渲完才看得见。

   这里把它们照成片的位置和时间在预览上做一遍。各在第几秒是服务端算的
   （``GET /tasks/{id}/direction``，和出片走同一套），这里只负责画和放：

   * 片名、标题卡——画在预览框上（样式在 editor.css 的 .direction 里）；
   * 配乐让开——预览那条配乐的音量照同一条线走；
   * 环境声、重音——服务端合成的那几个文件，跟着播放头放；
   * 时间线上多一条轨道，标出它们各在哪里。

   这是照着做的，不是成片本身：调色、压低配乐让人声、整体响度这些只有渲出来
   才有，这里的音量比例只是接近。 */
/** 摆好了位置、可以照着画的那一份；没有就是 null。 */
function directionReady() {
  const found = state.direction;
  return found && found.state === "ready" ? found : null;
}

/** 读回这条片子的导演项。位置还在算（服务端在后台听配音）就过一会儿再问。 */
async function loadDirection() {
  const taskId = state.taskId;
  if (!taskId) return;
  clearTimeout(state.directionTimer);
  let found = null;
  try {
    found = await call("GET", `/tasks/${encodeURIComponent(taskId)}/direction`);
  } catch (error) {
    // 读不到不该挡住审片，但也不能一声不吭：轨道上说一句，不然看起来就像
    // 这条片子没有导演项（服务端是旧的、还没重启时就是这样）。
    found = {
      state: "off",
      note: "The direction could not be loaded"
        + (error && error.message ? ` (${error.message})` : "")
        + ". If the app was just updated, restart it.",
    };
  }
  if (taskId !== state.taskId) return;
  showDirection(found);
}

/** Take a direction as the server gave it and draw it everywhere. */
function showDirection(found) {
  clearTimeout(state.directionTimer);
  state.direction = found;
  if (found && found.state === "listening") {
    state.directionTimer = setTimeout(loadDirection, DIRECTION_POLL);
  }
  state.directionCard = "";
  prepareDirectionSounds();
  drawDirectionTrack();
  drawDirection();
  soundDirection();
}

/* ---------- title cards, by hand ----------

   A card is tied to the words the narration says when it appears, not to a
   number of seconds, so it stays on them when the narration changes. The
   server answers every change with the whole direction, placed again. */
const CARD_TITLE_CHARS = 40;
const CARD_SUBTITLE_CHARS = 60;

function cardsPath() {
  return `/tasks/${encodeURIComponent(state.taskId)}/direction/cards`;
}

/** Open the panel on a card of the direction, or on a new one at `time`. */
function openCard(card, time) {
  savePromptEdit();
  state.card = card
    ? { start: card.start, time: card.start, title: card.title || "",
        subtitle: card.subtitle || "", moved: false }
    : { start: null, time: Math.max(0, Number(time) || 0), title: "",
        subtitle: "", moved: false };
  state.line = -1;
  state.adding = false;
  markLine();
  drawInspector();
  drawDirectionTrack();
}

function closeCard() {
  state.card = null;
  drawInspector();
  drawDirectionTrack();
}

async function saveCard(card) {
  const fresh = card.start === null;
  const body = { title: card.title, subtitle: card.subtitle };
  if (fresh || card.moved) body.time = card.time;
  if (!fresh) body.start = card.start;
  try {
    const found = await call(fresh ? "POST" : "PUT", cardsPath(), body);
    showDirection(found);
    // The card as the server placed it: it starts on a word, not on the click.
    const placed = ((found && found.cards) || [])
      .filter((one) => one.title === card.title.trim().replace(/\s+/g, " "))
      .sort((a, b) => Math.abs(a.start - card.time) - Math.abs(b.start - card.time))[0];
    state.card = null;
    if (placed) openCard(placed); else closeCard();
    say(fresh ? "The card was added. It is in the finished video when it is rendered."
      : "The card was changed.");
  } catch (error) {
    say(String(error.message || error), "error");
  }
}

async function deleteCard(card) {
  try {
    const found = await call(
      "DELETE", `${cardsPath()}?start=${encodeURIComponent(card.start)}`);
    showDirection(found);
    closeCard();
    say("The card was deleted.");
  } catch (error) {
    say(String(error.message || error), "error");
  }
}

function drawCardPanel(card) {
  const fresh = card.start === null;
  ui.inspTitle.textContent = fresh ? "New title card" : "Title card";

  const says = inspectorGroup("What it says");
  const field = (label, key, most, hint) => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const caption = document.createElement("label");
    caption.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = most;
    input.value = card[key];
    input.placeholder = hint;
    input.addEventListener("input", () => {
      card[key] = input.value;
      save.disabled = !card.title.trim();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && card.title.trim()) saveCard(card);
    });
    wrap.append(caption, input);
    says.appendChild(wrap);
    return input;
  };
  const title = field("Title", "title", CARD_TITLE_CHARS, "December 1978");
  field("Second line (optional)", "subtitle", CARD_SUBTITLE_CHARS,
    "West Summerdale Avenue");
  ui.inspBody.appendChild(says);

  const when = inspectorGroup("When it appears");
  const at = document.createElement("p");
  at.className = "hint";
  at.textContent = `At ${fmt(card.time)}`
    + (card.moved && !fresh ? ` (was ${fmt(card.start)}; not saved yet)` : "")
    + ". It starts on the word being said there, stays about six seconds, "
    + "and follows those words if the narration changes.";
  const move = button("Put it at the playhead", "small", () => {
    card.time = Math.max(0, state.playhead);
    card.moved = true;
    drawInspector();
  });
  const moveRow = document.createElement("div");
  moveRow.className = "row";
  moveRow.append(move);
  when.append(at, moveRow);
  ui.inspBody.appendChild(when);

  const save = button(fresh ? "Add the card" : "Save", "small", () => saveCard(card));
  save.disabled = !card.title.trim();
  const row = document.createElement("div");
  row.className = "row";
  row.append(save);
  if (!fresh) row.append(button("Delete the card", "small", () => deleteCard(card)));
  row.append(button("Back to the scene", "small", closeCard));
  ui.inspBody.appendChild(row);
  if (fresh) title.focus();
}

/** 换任务时把上一条片子的导演项清掉，声音也停掉。 */
function forgetDirection() {
  clearTimeout(state.directionTimer);
  state.direction = null;
  state.directionCard = "";
  prepareDirectionSounds();
  drawDirectionTrack();
  drawDirection();
}

/** 几段斜线加起来的那条 0..1 的线，和 ffmpeg 那边的 ``gate`` 是同一个式子。 */
function envelopeAt(shape, at) {
  let level = 0;
  for (const [up, down] of (shape && shape.ramps) || []) {
    level += unit((at - up) / Math.max(shape.rise, 0.01))
      - unit((at - down) / Math.max(shape.fall, 0.01));
  }
  return unit(level);
}

/** 配乐在这一刻要乘上多少：让开的那几段里一路压到听不见。 */
function musicDip(at) {
  const found = directionReady();
  if (!found || !found.music || !(found.music.ramps || []).length) return 1;
  return Math.pow(10, (found.music.floor_db / 20) * envelopeAt(found.music, at));
}

function applyMusicLevel() {
  const level = Number.isFinite(state.musicLevel) ? state.musicLevel : 0.2;
  ui.musicBed.volume = unit(level * musicDip(state.playhead));
}

/** 准备好这条片子用得上的那几种声音；用不上的停掉、放掉。 */
function prepareDirectionSounds() {
  const found = directionReady();
  const sounds = directionSounds;
  const kinds = new Set(((found && found.beds) || []).map((bed) => bed.kind));
  for (const kind of Object.keys(sounds.beds)) {
    if (kinds.has(kind)) continue;
    sounds.beds[kind].pause();
    delete sounds.beds[kind];
  }
  for (const kind of kinds) {
    if (sounds.beds[kind]) continue;
    const bed = new Audio(`${API}/direction/sounds/${encodeURIComponent(kind)}`);
    bed.loop = true;
    bed.preload = "auto";
    bed.volume = 0;
    sounds.beds[kind] = bed;
  }
  const hits = ((found && found.hits) || []).length;
  if (!hits) {
    sounds.hits.forEach((hit) => hit.pause());
    sounds.hits = [];
  } else if (!sounds.hits.length) {
    // 两个：前一记的尾音还没完，后一记就来了的时候各放各的。
    for (let count = 0; count < 2; count += 1) {
      const hit = new Audio(`${API}/direction/sounds/hit`);
      hit.preload = "auto";
      sounds.hits.push(hit);
    }
  }
  // 新做出来的这几个也照预览的倍数放。
  applyRate();
}

function strike(offset) {
  const sounds = directionSounds;
  if (!sounds.hits.length) return;
  const hit = sounds.hits[sounds.turn % sounds.hits.length];
  sounds.turn += 1;
  try { hit.currentTime = Math.max(0, offset); } catch { /* 元数据未就绪 */ }
  hit.play().catch(() => {});
}

/** 从这一秒开始放：正响到一半的那一记，从它的中途接上。 */
function startHitsUnder(at) {
  const found = directionReady();
  directionSounds.hits.forEach((hit) => hit.pause());
  if (!found) return;
  const length = Number(found.hit_seconds) || 5;
  for (const hit of found.hits || []) {
    if (at >= hit && at - hit < length - 0.2) strike(at - hit);
  }
}

function quietDirection() {
  Object.values(directionSounds.beds).forEach((bed) => bed.pause());
  directionSounds.hits.forEach((hit) => hit.pause());
}

/** 每一帧：配乐、环境声的音量照线走，播放头越过哪一记重音就放哪一记。 */
function soundDirection() {
  applyMusicLevel();
  const found = directionReady();
  const sounds = directionSounds;
  const at = state.playhead;
  for (const bed of (found && found.beds) || []) {
    const audio = sounds.beds[bed.kind];
    if (!audio) continue;
    const level = envelopeAt(bed, at);
    audio.volume = level;
    if (state.playing && level > 0) {
      if (audio.paused) audio.play().catch(() => {});
    } else if (!audio.paused) {
      audio.pause();
    }
  }
  const before = sounds.last;
  sounds.last = at;
  if (!state.playing || !found || !(found.hits || []).length) return;
  // 往回走了、或者一下子跳了一截，就是拖动过：按新位置重新接上。
  if (at < before || at - before > 0.5) { startHitsUnder(at); return; }
  for (const hit of found.hits) {
    if (hit > before && hit <= at) strike(0);
  }
}

/** 这一刻该在画面上的片名和标题卡。位置、字号、淡入淡出都照成片的数。 */
function drawDirection() {
  const found = directionReady();
  const cards = (found && found.cards) || [];
  const title = found && found.title;
  const unitPx = Math.min(ui.frame.clientWidth || 0, ui.frame.clientHeight || 0) / 1080;
  const lines = (found && found.timelines) || [];
  if (!found || !unitPx || (!title && !cards.length && !lines.length)) {
    ui.direction.hidden = true;
    return;
  }
  ui.direction.hidden = false;
  const at = state.playhead;
  const squeeze = Math.min(100, Math.max(50, Number(found.card_squeeze) || 100)) / 100;
  const box = ui.direction.style;
  box.setProperty("--u", unitPx + "px");
  box.setProperty("--squeeze", String(squeeze));
  if (found.card_font) {
    box.setProperty("--card-font",
      `"${String(found.card_font).replace(/"/g, "")}", "Arial Narrow", sans-serif`);
  }

  // 片名：整个画面压暗，正中一行大字。长的缩到放得下（字宽照字数估，和
  // 成片是同一个估法，所以缩的比例也一样）。
  let shown = 0;
  if (title && at >= title.start && at < title.end) {
    shown = unit((at - title.start) / 0.5) * unit((title.end - at) / 0.7);
    const text = String(title.text || "");
    ui.dirTitleText.textContent = text;
    ui.dirTagline.textContent = String(title.tagline || "");
    ui.dirTagline.hidden = !String(title.tagline || "").trim();
    const advance = 170 * 0.62 * squeeze + 12;
    const wide = ui.frame.clientWidth / unitPx;
    const shrink = Math.min(1, 0.86 * wide / Math.max(1, text.length * advance));
    const grow = 0.96 + 0.04 * unit((at - title.start) / Math.max(0.1, title.end - title.start));
    box.setProperty("--shrink", String(shrink));
    box.setProperty("--grow", String(grow));
  }
  ui.dirTitle.style.opacity = String(shown);
  ui.dirDim.style.opacity = String(0.64 * shown);
  drawDirectionTimeline(found, at, unitPx);

  // 标题卡：左上角，从左边滑进来 20 个单位。
  const index = cards.findIndex((card) => at >= card.start && at < card.end);
  if (index < 0) {
    ui.dirCard.style.opacity = "0";
    return;
  }
  const card = cards[index];
  const key = `${index}:${unitPx}:${card.title}:${card.subtitle}`;
  if (state.directionCard !== key) {
    state.directionCard = key;
    ui.dirCardTitle.textContent = String(card.title || "");
    ui.dirCardSub.textContent = String(card.subtitle || "");
    ui.dirCard.classList.toggle("with-sub", !!String(card.subtitle || "").trim());
    // 暗底跟着字的宽度走（成片里是估的，这里量得到）。
    const text = Math.max(
      ui.dirCardTitle.getBoundingClientRect().width,
      ui.dirCardSub.getBoundingClientRect().width);
    const most = ui.frame.clientWidth - 140 * unitPx;
    box.setProperty("--wide", Math.min(text + 150 * unitPx, most) + "px");
  }
  const since = at - card.start;
  ui.dirCard.style.opacity = String(unit(since / 0.45) * unit((card.end - at) / 0.45));
  box.setProperty("--slide", (112 + 20 * unit(since / 0.6)) * unitPx + "px");
  box.setProperty("--slide-sub", (112 + 20 * unit(since / 0.75)) * unitPx + "px");
}

/** The timeline strip as the finished video has it at `at`: the axis, a tick
    per event, the fill and the marker on the event being told, its date and
    what happened. The geometry and the timings are the render's
    (ass_subtitles.py, `_timelines`), sent with the direction. */
function drawDirectionTimeline(found, at, unitPx) {
  const box = ui.dirTimeline;
  const line = ((found && found.timelines) || [])
    .find((one) => at >= one.start && at < one.end && (one.events || []).length);
  if (!line) { box.style.opacity = "0"; return; }
  const how = found.timeline || {};
  const fade = Number(how.fade) || 0.5;
  const move = Number(how.move) || 0.6;

  // It gives way to the title (and to cards, when it is at the top with them).
  let open = unit((at - line.start) / fade) * unit((line.end - at) / fade);
  for (const [from, to] of how.holes || []) {
    if (at >= from && at < to) open = 0;
    else open = Math.min(open, unit((at < from ? from - at : at - to) / fade));
  }
  box.style.opacity = String(open);
  if (!open) return;

  const events = line.events;
  let now = 0;
  for (let i = 1; i < events.length; i += 1) if (at >= events[i].at) now = i;
  const event = events[now];
  const before = events[Math.max(0, now - 1)];
  const slid = now ? unit((at - event.at) / move) : 1;
  const place = before.place + (event.place - before.place) * slid;

  const edge = (Number(how.edge) || 150) * unitPx;
  const span = ui.frame.clientWidth - 2 * edge;
  const fromEdge = (Number(how.from_edge) || 112) * unitPx;
  box.style.setProperty("--tl-edge", edge + "px");
  box.style.setProperty("--tl-y",
    (how.top ? fromEdge : ui.frame.clientHeight - fromEdge) + "px");
  box.style.setProperty("--tl-x", edge + span * place + "px");
  box.classList.toggle("right", event.place > 0.5);

  const key = `${line.start}:${events.length}:${unitPx}:${span}`;
  if (state.directionTimeline !== key) {
    state.directionTimeline = key;
    ui.dirTlFirst.textContent = String(line.first || "");
    ui.dirTlLast.textContent = String(line.last || "");
    ui.dirTlTicks.innerHTML = "";
    for (const one of events) {
      const tick = document.createElement("i");
      tick.className = "dir-tl-tick";
      tick.style.left = edge + span * one.place + "px";
      ui.dirTlTicks.appendChild(tick);
    }
  }
  ui.dirTlDate.textContent = String(event.date || "");
  ui.dirTlLabel.textContent = String(event.label || "");
  const since = now ? event.at : line.start;
  const next = now + 1 < events.length ? events[now + 1].at : line.end;
  const until = Math.min(next, since + (Number(how.label_seconds) || 8));
  ui.dirTlDate.style.opacity = String(unit((at - since) / 0.2));
  ui.dirTlLabel.style.opacity = String(
    at < until ? unit((at - since) / 0.2) * unit((until - at) / 0.4) : 0);
}

/* 时间线上的那条导演轨道。上排：片名、标题卡、重音；下排：配乐让开、环境声。
   没有导演项的片子这条轨道不出现，不占地方。 */
function drawDirectionTrack() {
  const lane = ui.directionLane;
  lane.innerHTML = "";
  const found = state.direction;
  const total = projectTotal();
  const ready = directionReady();
  const show = !!found && total > 0 && (!!ready || !!found.note);
  lane.hidden = !show;
  ui.directionCell.hidden = !show;
  if (!show) return;
  const px = state.pxPerSecond;

  if (!ready) {
    const note = document.createElement("div");
    note.className = "dir-note" + (found.state === "off" ? " warn" : "");
    note.textContent = found.note;
    note.title = found.note;
    note.style.width = Math.max(14, total * px - 2) + "px";
    lane.appendChild(note);
    ui.directionCell.textContent = "Direction";
    return;
  }
  // 位置是照字幕估的时候在轨道名上说一声：估的能差到两秒。
  ui.directionCell.textContent = ready.note ? "Direction ≈" : "Direction";
  ui.directionCell.title = ready.note
    || "What the finished video adds: the title, title cards, impacts, "
      + "ambience and where the music drops out.";

  const span = (kind, row, start, end, label, tip, chosen) => {
    const mark = document.createElement("div");
    mark.className = `dir-mark ${kind} ${row}`;
    if (chosen) {
      mark.addEventListener("click", chosen);
      mark.title = "Click to change this card";
    }
    mark.style.left = start * px + "px";
    mark.style.width = Math.max(6, (end - start) * px) + "px";
    mark.textContent = label;
    mark.title = `${tip} · ${fmt(start)}–${fmt(end)}`
      + (chosen ? "\nClick to change this card" : "");
    mark.addEventListener("click", () => scrubTo(start));
    lane.appendChild(mark);
    return mark;
  };
  for (const line of ready.timelines || []) {
    const run = document.createElement("div");
    run.className = "dir-run";
    run.style.left = line.start * px + "px";
    run.style.width = Math.max(6, (line.end - line.start) * px) + "px";
    run.title = `Timeline ${line.first || ""}${line.last ? "–" + line.last : ""}`
      + ` · ${fmt(line.start)}–${fmt(line.end)}`;
    lane.appendChild(run);
    for (const event of line.events || []) {
      const date = document.createElement("div");
      date.className = "dir-run-date";
      date.style.left = event.at * px + "px";
      date.title = `Timeline: ${event.date}${event.label ? " — " + event.label : ""}`
        + ` · ${fmt(event.at)}`;
      date.addEventListener("click", () => scrubTo(event.at + 1));
      lane.appendChild(date);
    }
  }
  if (ready.title) {
    span("title", "row-a", ready.title.start, ready.title.end,
      ready.title.text, `Title: ${ready.title.text}`);
  }
  for (const card of ready.cards || []) {
    const mark = span("card", "row-a", card.start, card.end, card.title,
      `Title card: ${card.title}${card.subtitle ? " — " + card.subtitle : ""}`,
      () => openCard(card));
    mark.classList.toggle("selected",
      !!state.card && state.card.start !== null
        && Math.abs(state.card.start - card.start) < 0.01);
  }
  for (const [start, end] of ready.music_off || []) {
    span("music-off", "row-b", start, end, "music off",
      "The music fades out before this and comes back after it");
  }
  for (const bed of ready.beds || []) {
    for (const [start, end] of bed.windows || []) {
      span("bed", "row-b", start, end, bed.kind, `Ambience: ${bed.kind}`);
    }
  }
  for (const at of ready.hits || []) {
    const hit = document.createElement("div");
    hit.className = "dir-hit";
    hit.style.left = at * px + "px";
    hit.title = `Impact · ${fmt(at)}`;
    hit.addEventListener("click", () => scrubTo(Math.max(0, at - 1)));
    lane.appendChild(hit);
  }
}

/* 时间线上的配乐轨。一条从头铺到尾的横条：配乐属于整条片子，没有"哪一段"
   的概念，所以它不分块，只说明"这里一直有音乐,是哪一首,多响"。 */
function drawMusicTrack() {
  ui.musicLane.innerHTML = "";
  const total = projectTotal();
  if (!total) return;

  const music = state.music;
  const bar = document.createElement("div");
  bar.style.left = "0px";
  bar.style.width = Math.max(14, total * state.pxPerSecond - 2) + "px";

  if (!music || (!music.track && !music.external)) {
    bar.className = "music-bar empty";
    bar.textContent = "no music";
    bar.title = "Click to choose background music for this video.";
  } else if (music.external) {
    bar.className = "music-bar";
    bar.textContent = `music · from ${music.kind}`;
    bar.title = `This video gets its music from ${music.kind}.`;
  } else {
    bar.className = "music-bar";
    const name = music.track === RANDOM_MUSIC
      ? "a different one each render"
      : music.track.replace(/\.[^.]+$/, "");
    const volume = Math.round((music.volume || 0) * 100);
    bar.textContent = `music · ${name} · ${volume}%`;
    bar.title = music.track === RANDOM_MUSIC
      ? "A track is picked at random when the video is rendered, so there is "
        + "nothing to play here yet."
      : `${music.track} at ${volume}% — click to change it.`;
  }
  bar.addEventListener("click", openMusic);
  ui.musicLane.appendChild(bar);
}

/* 预览时跟着旁白一起放的那一条配乐。

   音量照搬渲染时的那个值，所以在编辑器里听到的比例和成片一致——这正是用户
   要在这里判断的事："这首曲子会不会盖住人声"。

   "每次渲染随机挑"没有具体文件，放不了：这时安静,时间线上那一条也写明了
   原因,而不是随便挑一首给用户一个会变的印象。 */
function syncMusicBed() {
  const music = state.music;
  const name = music && !music.external ? String(music.track || "") : "";
  const playable = name && name !== RANDOM_MUSIC;
  const wanted = playable ? `${API}/music/${encodeURIComponent(name)}` : "";
  if (!wanted) {
    try { ui.musicBed.pause(); } catch { /* 还没加载 */ }
    ui.musicBed.removeAttribute("src");
    return;
  }
  // 同一首就别重设 src：重设会把它拉回开头，而用户可能正在播放中途。
  if (!ui.musicBed.src.endsWith(encodeURIComponent(name))) {
    ui.musicBed.src = wanted;
  }
  const level = Number(music.volume);
  // 配乐让开的那几段要在这个音量上再往下压，所以记下来，由 applyMusicLevel 设。
  state.musicLevel = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0.2;
  applyMusicLevel();
}

/** 读回这条片子的配乐，画到时间线上，并准备好预览用的那一条。 */
async function loadMusic() {
  if (!state.taskId) return;
  try {
    state.music = (await call("GET", musicPath())) || null;
  } catch {
    // 读不到配乐不该挡住审片，轨道上就显示"没有音乐"。
    state.music = null;
  }
  syncMusicBed();
  drawMusicTrack();
}

/* ---------- 渲染中 ----------

   渲染要跑好几分钟，而且期间任何编辑都不作数（接口也会拒）。所以点下去立刻
   把这块挡板放出来：一是让用户知道"点到了"，二是把路挡上，省得点了半天才
   发现不让改。进度就摆在正中间。 */
function showRender(stage) {
  ui.renderModal.hidden = false;
  ui.renderBox.classList.remove("failed");
  ui.renderPlay.hidden = true;
  ui.renderReveal.hidden = true;
  ui.renderDone.hidden = true;
  ui.renderHide.hidden = false;
  ui.renderTitle.textContent = "Rendering your video";
  ui.renderNote.textContent = stage || "Putting the pictures, the narration "
    + "and the music together. This takes a few minutes.";
  // 还没有数字时用来回跑的条，别让用户盯着一个不动的 0%。
  ui.renderBar.classList.add("waiting");
  ui.renderPct.textContent = "";
  ui.renderElapsed.textContent = "";
  // 上一条片子的"好了"不能留在标签页标题上：这一条才刚开始。
  setTitleNote("Rendering");
  state.renderStarted = Date.now();
}

function updateRender(percent, phase) {
  if (ui.renderModal.hidden) return;
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  if (value > 0) {
    ui.renderBar.classList.remove("waiting");
    ui.renderBar.style.width = value + "%";
    ui.renderPct.textContent = value + "%";
  }
  if (phase) ui.renderNote.textContent = `Stage: ${phase}.`;
  if (state.renderStarted) {
    const seconds = (Date.now() - state.renderStarted) / 1000;
    ui.renderElapsed.textContent = `${fmt(seconds)} so far`;
  }
}

/** 渲染结束了，在横幅和标签页标题上说一声。

    ``finishRender`` 只画那块挡板，而挡板常常是收起来的：用户按过"隐藏"（渲
    染要好几分钟，他当然会去干别的），或者这个页面是渲染中途打开的、从来没
    开过挡板。那种时候以前界面上一个字都没有——七分钟过去，什么动静都没有，
    用户只能自己去翻文件夹看有没有新片子。 */
function announceRender(task, reloaded) {
  const failed = task && Number(task.state) === -1;
  if (failed) {
    const why = (task && (task.error || task.failed_stage))
      || "no reason was reported";
    say(`The render failed: ${why}`, "error");
    setTitleNote("Render failed");
    return;
  }
  setTitleNote("Video ready");
  if (!reloaded) {
    // 片子出来了，只是这个页面没能把场景重新读回来。两件事都要说清楚，
    // 别让"刷新失败"听起来像"渲染失败"。
    say("Your video is ready. The scenes could not be reloaded here — "
      + "reload the page to see them.", "warn");
    if (!ui.renderModal.hidden) {
      // 挡板上那句"场景已经重新读好了"这时候是假话。
      ui.renderNote.textContent = "Your video is ready. The scenes could not "
        + "be reloaded here — reload the page to see them.";
    }
    return;
  }
  // 挡板开着的话 ``finishRender`` 已经在上面写了同样的意思，横幅就不用再
  // 挤一遍；收起来了才靠横幅说。
  if (ui.renderModal.hidden) say("Your video is ready.", "done");
}

/** 状态真的查不到了。渲染可能还在跑，所以既不能说"好了"，也不能一直转。

    这块挡板上一直转圈是最坏的那种"没反应"：后台在干活，而用户以为点了没用，
    于是再点一次，于是同一条片子渲两遍。 */
function loseRender(error) {
  const why = (error && (error.message || error)) || "no reason given";
  say(
    `Cannot read the render status (${why}). The render may still be running — `
    + "look at the window the API is running in, then reload this page.",
    "warn"
  );
  if (ui.renderModal.hidden) return;
  ui.renderBar.classList.remove("waiting");
  ui.renderHide.hidden = true;
  ui.renderDone.hidden = false;
  ui.renderPlay.hidden = true;
  ui.renderReveal.hidden = true;
  setTitleNote("Render status lost");
  ui.renderTitle.textContent = "Lost track of the render";
  ui.renderNote.textContent = "It was submitted, but this page cannot read its "
    + "status any more. It is probably still running: watch the API window, "
    + "then reload this page to pick it up again.";
  ui.renderPct.textContent = "";
}

/** 渲染结束：成了就给一个"去看"的按钮，败了就把原因留在这块挡板上。 */
function finishRender(task) {
  if (ui.renderModal.hidden) return;
  const failed = task && Number(task.state) === -1;
  ui.renderBar.classList.remove("waiting");
  ui.renderHide.hidden = true;
  ui.renderDone.hidden = false;
  // 失败那条路上不能留着"去看成片"：没有成片可看。
  ui.renderPlay.hidden = true;
  ui.renderReveal.hidden = true;
  ui.renderBox.classList.toggle("failed", !!failed);
  if (failed) {
    ui.renderBar.style.width = "100%";
    ui.renderTitle.textContent = "The render failed";
    ui.renderNote.textContent = String(
      (task && (task.error || task.failed_stage)) || "No reason was reported."
    );
    ui.renderPct.textContent = "";
    return;
  }
  ui.renderBar.style.width = "100%";
  ui.renderPct.textContent = "100%";
  ui.renderTitle.textContent = "Your video is ready";
  ui.renderNote.textContent = "The scenes have been reloaded. Render again any "
    + "time you change something.";
  const videos = (task && task.videos) || [];
  if (videos.length) {
    ui.renderPlay.hidden = false;
    ui.renderReveal.hidden = false;
    state.renderedVideo = String(videos[videos.length - 1]);
  }
}

ui.renderHide.addEventListener("click", () => { ui.renderModal.hidden = true; });
ui.renderDone.addEventListener("click", () => {
  ui.renderModal.hidden = true;
  // 看过了就把标签页标题还原。"隐藏"不还原：那条片子还在渲，标题正是用来
  // 在页面没露脸时报进度的。
  setTitleNote("");
});
ui.renderPlay.addEventListener("click", () => {
  if (!state.renderedVideo) return;
  // 成片由 /tasks 那个静态挂载直接提供，所以这就是一个能打开的地址。
  window.open(state.renderedVideo, "_blank", "noopener");
});

/** What fitting a clip to its scene came to, as a sentence (or ""). */
function fitNote(fit) {
  if (!fit) return "";
  if (!fit.changed) {
    return fit.spare > 0.4
      ? ` The clip is ${fit.spare.toFixed(1)}s longer than the scene, so its end is `
        + "cut off; “Speed the clip up to fit” plays all of it."
      : "";
  }
  const pace = Math.round(100 / fit.factor);
  if (fit.factor < 1) {
    return ` It was sped up to ${pace}% speed so the whole clip plays in the scene`
      + (fit.cut > 0.05
        ? `, which is as fast as it goes; the last ${fit.cut.toFixed(1)}s are still cut off.`
        : ".");
  }
  return ` It was slowed to ${pace}% speed to cover the scene`
    + (fit.short > 0.05
      ? `, which is as slow as it goes; the scene is still ${fit.short.toFixed(1)}s `
        + "longer than the clip, and its last frame is held for that time."
      : ".");
}

/** How an uploaded clip misses its scene's length: "short" (it ends before
    the scene does), "long" (its end is cut off), or "" when it fits. Only for
    clips the user brought in: stock footage is always longer than its scene,
    and which part of it shows is chosen by trimming. */
function clipMisfit(scene) {
  if (scene.kind !== "video" || scene.video || scene.duration == null) return "";
  if (scene.source !== "local") return "";
  if (/^(wan-(image|text)-|[0-9a-f]{64}\.)/i.test(String(scene.material || "")
    .split(/[\\/]/).pop())) return "";
  const have = Number(scene.source_duration || 0) - Number(scene.trim_start || 0);
  if (have <= 0) return "";
  const need = scene.duration * state.speed;
  if (need > have + 0.05) return "short";
  return have > need + 0.4 ? "long" : "";
}

function fitSceneClip(scene) {
  const at = scene.index;
  run("Fitting the clip to the scene", async () => {
    const fitted = await call("POST", scenePath(at) + "/fit");
    return fitNote(fitted && fitted.fit).trim()
      || `Scene ${at + 1}'s clip already fits the scene.`;
  });
}

/** Put the user's own picture or video on a scene. */
function uploadSceneFile(scene) {
  const at = scene.index;
  pickFile("image/png,image/jpeg,image/bmp,video/*", (file) => {
    run(`Uploading ${file.name}`, async () => {
      const placed = await upload(scenePath(at) + "/upload", file);
      return `Scene ${at + 1} now shows ${file.name}.${fitNote(placed && placed.fit)} `
        + "What it showed before is under “Earlier media”.";
    });
  });
}

/** Point at a scene's picture in the file manager. Only the server can: a
    page cannot open a folder, and the API runs on the same machine. */
async function showSceneFile(scene, control) {
  control.disabled = true;
  try {
    const answer = await call("POST", scenePath(scene.index) + "/reveal");
    const name = String((answer && answer.file) || "");
    if (answer && answer.opened) {
      say(`The folder is open with scene ${scene.index + 1}'s `
        + `${answer.what === "picture" ? "picture" : "video"} selected: ${name}`);
      return;
    }
    // No desktop, or the page is open on another machine: say where the file
    // is and put the path on the clipboard, which is the next best thing.
    const where = String((answer && answer.path) || "");
    let copied = false;
    if (where && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(where);
        copied = true;
      } catch { /* not allowed here; the path is still shown */ }
    }
    say(`The file is at ${where}` + (copied ? " (path copied)" : "")
      + (answer && answer.reason ? ` — ${answer.reason}.` : "."), "warn");
  } catch (error) {
    say(String(error.message || error), "error");
  } finally {
    control.disabled = false;
  }
}

// 在资源管理器/访达里把成片指出来。
//
// 这件事只能由服务端做：网页打不开本机的文件管理器，而这个 API 就跑在同一
// 台机器上。递过去的是**文件名**，不是路径——能开哪个由服务端在任务目录里
// 自己解析。
ui.renderReveal.addEventListener("click", async () => {
  if (!state.renderedVideo) return;
  const name = decodeURIComponent(
    String(state.renderedVideo).split("?")[0].split("/").pop() || ""
  );
  ui.renderReveal.disabled = true;
  try {
    const answer = await call(
      "POST", `/tasks/${encodeURIComponent(state.taskId)}/reveal`, { file: name });
    if (answer && answer.opened) return;
    // 开不了窗（没桌面、或者这个页面开在另一台机器上）时不报错：把路径摆
    // 出来，用户自己复制就行，而一句"失败了"帮不上任何忙。
    const where = String((answer && answer.path) || "");
    ui.renderNote.textContent = `The video is at ${where}`
      + (answer && answer.reason ? ` — ${answer.reason}.` : ".");
    if (where && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(where);
        ui.renderNote.textContent += " Copied the path.";
      } catch { /* 没有剪贴板权限就算了，路径已经显示出来了 */ }
    }
  } catch (error) {
    ui.renderNote.textContent = String(error.message || error);
  } finally {
    ui.renderReveal.disabled = false;
  }
});

/* ---------- video scenes ----------

   A scene can be turned into a generated video clip: either its picture is
   animated, or a clip is drawn from its prompt. One clip takes minutes and
   needs the whole graphics card, so nothing is made on the click. Scenes are
   marked one by one, and one button then makes all of them in a single batch.

   While the batch runs the editor is covered by a window that cannot be
   hidden. Nothing else would work anyway: the image server has given up the
   card, and the API refuses every scene edit until the batch ends. */

function videoModeLabel(mode) {
  return mode === "image" ? "animate the picture" : "video from the prompt";
}

/** "about 6 min", "about 1 h 20 min". Rounded up: a wait that ends early is
    better news than one that overruns. */
function aboutTime(seconds) {
  const minutes = Math.max(1, Math.ceil((Number(seconds) || 0) / 60));
  if (minutes < 60) return `about ${minutes} min`;
  const rest = minutes % 60;
  return `about ${Math.floor(minutes / 60)} h` + (rest ? ` ${rest} min` : "");
}

/** How long one scene's clip takes here. A full clip is 81 frames at 16 fps;
    a shorter scene needs fewer and is quicker in proportion. */
function videoClipSeconds(scene) {
  const typical = (state.video && state.video.typical_clip_seconds) || 360;
  const frames = Math.min(81, Math.max(17, Math.ceil(screenDuration(scene) * state.speed * 16)));
  return typical * frames / 81;
}

function markedScenes() {
  return (state.scenes || []).filter((scene) => scene.animate);
}

async function fetchVideo() {
  try {
    state.video = (await call("GET", "/video-scenes")) || {};
  } catch {
    // An older API without the route: video scenes are simply not offered.
    state.video = { available: false, reason: "this server cannot make video scenes" };
  }
  return state.video;
}

/* One video at a time has the graphics card. A batch asked for while another
   video's is running waits in a queue and starts by itself. */

/** This video's place in that queue, if it is waiting. */
function videoQueued(video = state.video || {}) {
  return (video.queue || []).find((one) => one.task_id === state.taskId) || null;
}

/** How this video's batch ended, when another video has the card since. */
function videoEnded(video = state.video || {}) {
  return (video.ended || []).find((one) => one.task_id === state.taskId) || null;
}

/** Another video's batch is running, not this one's. */
function videoElsewhere(video = state.video || {}) {
  return !!video.running && video.task_id !== state.taskId;
}

/** "in about 2 h", or "next" when nothing measurable is ahead. */
function startsIn(seconds) {
  return seconds > 0 ? `in ${aboutTime(seconds)}` : "next";
}

/** The button in the top bar. Only there once something is marked. */
function drawVideoButton() {
  const marked = markedScenes().length;
  const video = state.video || {};
  const queued = videoQueued(video);
  if (queued) {
    ui.videoScenes.hidden = EMBEDDED;
    ui.videoScenes.disabled = false;
    ui.videoScenes.textContent = `Video scenes queued · starts ${startsIn(queued.starts_in)}…`;
    ui.videoScenes.title = "Another video is making its video scenes. This "
      + "video's start by themselves when it is done. Click to see, or to "
      + "take this video out of the queue.";
    ui.animateAll.hidden = true;
    return;
  }
  // Another video's batch, and scenes marked here: the button queues them.
  const elsewhere = videoElsewhere(video);
  if (video.running && !(elsewhere && marked)) {
    // A batch is running, for this video or another. The button is its
    // progress, and opens the window again after it was hidden.
    const finished = (video.done || 0) + (video.failed || 0);
    ui.videoScenes.hidden = EMBEDDED;
    ui.videoScenes.disabled = false;
    ui.videoScenes.textContent = `Video scenes ${finished}/${video.total}`
      + (video.remaining ? ` · ${aboutTime(video.remaining)} left` : "") + "…";
    ui.videoScenes.title = "Video scenes are being made. Click to see how far "
      + "they are, or to stop them. Pictures cannot be drawn until they finish.";
    // In another video, every picture of this one can still be queued.
    ui.animateAll.hidden = !elsewhere || EMBEDDED || !state.taskId
      || !(state.scenes || []).length;
    ui.animateAll.disabled = state.taskBusy;
    ui.animateAll.title = "Turn every still picture of this video into a moving "
      + "clip. Another video is making its scenes now, so these wait their turn.";
    return;
  }
  ui.videoScenes.hidden = !marked || EMBEDDED;
  ui.videoScenes.textContent = `${elsewhere ? "Queue" : "Make"} ${marked} `
    + `video scene${marked === 1 ? "" : "s"}…`;
  ui.videoScenes.disabled = !!state.queue.busy || state.taskBusy;
  ui.videoScenes.title = state.queue.busy
    ? "Pictures are still being drawn or narration lines recorded. Video "
      + "scenes need the whole graphics card, so wait for the queue to finish."
    : elsewhere
      ? "Another video is making its video scenes. These are queued and "
        + "start by themselves when it is done."
      : "Make a video clip for every scene you marked, one after another.";

  ui.animateAll.hidden = EMBEDDED || !state.taskId || !(state.scenes || []).length
    || video.available === false;
  ui.animateAll.disabled = ui.videoScenes.disabled;
  ui.animateAll.title = state.queue.busy
    ? ui.videoScenes.title
    : "Turn every still picture of this video into a moving clip, in one go. "
      + "It asks first and says how long it takes.";
}

/** The box in a scene's panel where it is marked. Marking only saves the
    choice; nothing is made until the batch is started from the top bar. */
function videoSceneBox(scene) {
  const box = inspectorGroup("Video scene");
  const video = state.video || {};
  const select = document.createElement("select");
  const isVideo = !!scene.video;
  // The other form of this scene, already on disk: the picture a clip was
  // made from, or the clip made from this picture. Switching is instant.
  const other = isVideo ? pictureBehind(scene) : clipMadeFrom(scene);
  const choices = isVideo
    ? [
      ["", "Video clip (showing now)"],
      ["swap", "Still picture (the original)", !other],
      ["image", "Animate the picture again", !scene.video.still],
      ["text", "Make a new video from the prompt"],
    ]
    : [
      ["", "Still picture (showing now)"],
      ...(other ? [["swap", "Video clip (already made)"]] : []),
      ["image", other ? "Animate this picture again" : "Animate this picture",
        isPending(scene)],
      ["text", "Make a video from the prompt"],
    ];
  for (const [value, label, off] of choices) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.disabled = !!off;
    select.appendChild(option);
  }
  select.value = scene.animate || "";
  box.appendChild(select);

  const note = document.createElement("p");
  note.className = "hint";
  if (scene.animate) {
    note.classList.add("pending");
    note.textContent = `Marked: ${videoModeLabel(scene.animate)}. It is made when `
      + "you press the “Make video scenes” button at the top, together with "
      + "every other scene you mark. Until then the scene stays as it is.";
  } else if (isVideo) {
    note.textContent = other
      ? "This scene is a video clip. Choose “Still picture” to go back to the "
        + "picture it was made from; the clip is kept and can be chosen again."
      : "This scene is a video clip made from the prompt, so there is no "
        + "picture behind it. “Earlier media” has what the scene showed before.";
  } else if (other) {
    note.textContent = "This scene is a still picture. The video clip made from "
      + "it is kept: choose “Video clip” to put it back, no waiting.";
  } else if (video.available === false) {
    note.textContent = `Not available on this computer: ${video.reason}.`;
  } else {
    note.textContent = "Mark every scene that should move, then make them all "
      + `at once from the top bar. This one takes ${aboutTime(videoClipSeconds(scene))}.`;
  }
  if (video.available === false) {
    // Making clips is off here, but going back and forth between a picture
    // and a clip that already exists needs no video model.
    for (const option of select.options) {
      if (option.value === "image" || option.value === "text") option.disabled = true;
    }
  }
  box.appendChild(note);

  select.addEventListener("change", async () => {
    const chosen = select.value;
    await run(chosen === "swap" ? "Switching" : "Saving", async () => {
      if (chosen !== "swap") {
        await call("PUT", scenePath(scene.index) + "/video", { mode: chosen });
        return "";
      }
      // A mark for the next batch would act on what the scene no longer is.
      if (scene.animate) {
        await call("PUT", scenePath(scene.index) + "/video", { mode: "" });
      }
      await call("POST", `${scenePath(scene.index)}/restore`, { material: other });
      return isVideo
        ? `Scene ${scene.index + 1} is a still picture again. Its video clip is kept.`
        : `Scene ${scene.index + 1} is the video clip again.`;
    });
    // A refused choice (no picture to animate, scene too long) leaves the list
    // on something that was not saved. Put it back to what is true.
    drawInspector();
    drawClips();
    drawVideoButton();
  });
  return box;
}

/** A generated clip, by its name: clips made before their record was kept in
    the scene's history are still known this way. */
function isGeneratedClip(name) {
  return /^wan-(image|text)-/i.test(materialKey(name));
}

/** The picture a video scene goes back to: the one its clip was made from,
    or, for a clip made from the prompt, the last picture the scene showed. */
function pictureBehind(scene) {
  if (scene.video && scene.video.still) return scene.video.still;
  const earlier = (scene.history || []).filter(
    (entry) => entry && entry.material && !entry.video && !isGeneratedClip(entry.material));
  return earlier.length ? earlier[earlier.length - 1].material : "";
}

/** The clip already made from this scene's picture, newest first choice; or
    any clip the scene has had, when none is recorded against this picture. */
function clipMadeFrom(scene) {
  const mine = materialKey(scene.material);
  const clips = (scene.history || []).filter(
    (entry) => entry && entry.material
      && (entry.video || isGeneratedClip(entry.material)));
  if (!clips.length || !mine) return "";
  const exact = clips.filter((entry) => entry.video && entry.video.still === mine);
  return (exact.length ? exact : clips).slice(-1)[0].material;
}

/** The lines of the batch window: one per scene. */
function drawVideoList(rows) {
  ui.videoList.innerHTML = "";
  for (const row of rows) {
    const item = document.createElement("li");
    if (row.kind) item.className = row.kind;
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = row.what;
    what.title = row.what;
    const stateText = document.createElement("span");
    stateText.className = "state";
    stateText.textContent = row.state;
    item.append(what, stateText);
    ui.videoList.appendChild(item);
  }
}

function videoButtons(shown) {
  for (const name of ["Cancel", "Start", "Hide", "Stop", "Done"]) {
    ui["video" + name].hidden = !shown.includes(name);
  }
}

/** Step one: say what is about to happen and how long it takes, and ask. */
function askForVideoScenes(pictures) {
  // `pictures` is the server's answer for "animate all": the scenes to add to
  // the marked ones, and the ones it has to leave as pictures. Nothing is
  // marked until Start, so "Not now" leaves the video exactly as it was.
  const everything = !!(pictures && pictures.scenes);
  state.videoEverything = everything;
  const extra = new Set(everything ? pictures.scenes : []);
  const marked = (state.scenes || [])
    .filter((scene) => scene.animate || extra.has(scene.index))
    .map((scene) => (scene.animate ? scene : { ...scene, animate: "image" }));
  if (!marked.length) return;
  const skipped = (everything ? pictures.skipped || [] : []).map((one) => one.scene + 1);
  // The model is loaded once for every few clips of a kind (the server says
  // how many), and that takes a couple of minutes each time.
  const perGroup = (state.video && state.video.clips_per_group) || 4;
  const groups = ["image", "text"].reduce((sum, mode) => sum + Math.ceil(
    marked.filter((scene) => scene.animate === mode).length / perGroup), 0);
  const total = marked.reduce((sum, scene) => sum + videoClipSeconds(scene), 0)
    + groups * ((state.video && state.video.typical_group_seconds) || 150);
  ui.videoModal.hidden = false;
  ui.videoBox.classList.remove("failed");
  // Another video has the card: these are queued behind it, and behind
  // every video already waiting.
  const elsewhere = videoElsewhere();
  const ahead = ((state.video || {}).remaining || 0)
    + ((state.video || {}).queue || []).reduce((sum, one) => sum + (one.seconds || 0), 0);
  ui.videoTitle.textContent =
    `${elsewhere ? "Queue" : "Make"} ${marked.length} video scene${marked.length === 1 ? "" : "s"}?`;
  ui.videoNote.textContent = (elsewhere
    ? "Another video is making its video scenes, so these wait their turn: "
      + `they start by themselves ${startsIn(ahead)}. `
    : "")
    + `This takes ${aboutTime(total)}. Until it is done `
    + "the editor is locked and no pictures can be drawn: the video model needs "
    + "the whole graphics card. Each scene keeps its picture, so any of them "
    + "can be put back afterwards."
    + (skipped.length
      ? ` Scene${skipped.length === 1 ? "" : "s"} ${skipped.join(", ")} `
        + `${skipped.length === 1 ? "stays a picture: it is" : "stay pictures: they are"} `
        + "longer than a video scene can cover "
        + `(${((state.video || {}).max_scene_seconds || 9.7).toFixed(1)}s).`
      : "");
  drawVideoList(marked.map((scene) => ({
    what: `${scene.index + 1}. ${scene.keyword || ""}`,
    state: `${videoModeLabel(scene.animate)} · ${aboutTime(videoClipSeconds(scene))}`,
  })));
  ui.videoProgress.hidden = true;
  ui.videoPct.textContent = "";
  ui.videoElapsed.textContent = "";
  videoButtons(["Cancel", "Start"]);
  ui.videoStart.disabled = false;
}

function videoSceneName(clip) {
  const scene = state.scenes.find((one) => one.id === clip.id) || state.scenes[clip.scene];
  return `${clip.scene + 1}. ${(scene && scene.keyword) || ""}`;
}

/** Step two and three: the batch running, then what came of it. */
function drawVideoBatch(video) {
  // Hidden by the user while it runs: keep it hidden, and bring it back when
  // the batch ends, because how it ended is news.
  if (!video.running) state.videoHidden = false;
  ui.videoModal.hidden = !!state.videoHidden;
  drawVideoButton();
  ui.videoStop.textContent = "Stop";
  const queued = video.running ? videoQueued(video) : null;
  if (queued) {
    // This video is waiting behind another one's batch.
    const others = queued.position - 1;
    drawVideoList((queued.clips || []).map((clip) => ({
      what: videoSceneName(clip), kind: "waiting", state: "waiting",
    })));
    ui.videoBox.classList.remove("failed");
    ui.videoTitle.textContent = `Waiting its turn · ${queued.total} video `
      + `scene${queued.total === 1 ? "" : "s"}`;
    ui.videoNote.textContent = "Another video is making its video scenes"
      + (others ? `, and ${others} more ${others === 1 ? "is" : "are"} ahead of this one` : "")
      + `. These start by themselves ${startsIn(queued.starts_in)} and take `
      + `${aboutTime(queued.seconds)}. You can hide this and keep working, here `
      + "or in another video. What is marked when their turn comes is what is made.";
    ui.videoProgress.hidden = true;
    ui.videoPct.textContent = "";
    ui.videoElapsed.textContent = "";
    videoButtons(["Hide", "Stop"]);
    ui.videoStop.textContent = "Leave the queue";
    ui.videoStop.disabled = false;
    setTitleNote("Video scenes queued");
    return;
  }
  const clips = video.clips || [];
  const finished = (video.done || 0) + (video.failed || 0);
  const here = video.task_id === state.taskId;
  drawVideoList(clips.map((clip) => ({
    what: here ? videoSceneName(clip) : `Scene ${clip.scene + 1}`,
    kind: clip.state,
    // Each clip is drawn in two halves with the others' in between, so a
    // clip can sit at "2 of 4" while the next one is worked on.
    state: clip.state === "running"
      ? (clip.id !== video.current ? `${clip.step} of ${video.steps} steps done`
        : clip.step >= video.steps ? "finishing"
          : `step ${clip.step + 1} of ${video.steps}`)
      : clip.state === "done" ? "done"
        : clip.state === "failed" ? clip.error || "failed"
          : clip.state === "stopped" ? "not made" : "waiting",
  })));

  if (video.running) {
    ui.videoBox.classList.remove("failed");
    ui.videoTitle.textContent = here
      ? `Making video scenes · ${finished} of ${video.total} done`
      : "Another video is making its video scenes";
    ui.videoNote.textContent = (video.stopping ? "Stopping" : (video.note || "Working"))
      + ". You can hide this and keep working: edit, render, open other "
      + "videos. Only drawing pictures has to wait until this is done.";
    ui.videoProgress.hidden = false;
    // Decoding counts as one more step, so the last step is not "100%".
    const share = clips.reduce((sum, clip) => sum + (
      clip.state === "done" || clip.state === "failed" ? 1
        : Math.min(clip.step || 0, video.steps) / (video.steps + 1)), 0) / (clips.length || 1);
    // Nothing to measure while the model loads: sweep instead of sitting at 0%.
    ui.videoBar.classList.toggle("waiting", share <= 0);
    ui.videoBar.style.width = share > 0 ? `${Math.round(share * 100)}%` : "";
    ui.videoPct.textContent = share > 0 ? `${Math.round(share * 100)}%` : "";
    ui.videoElapsed.textContent = `${fmt(video.elapsed)} so far`
      + (video.remaining ? ` · ${aboutTime(video.remaining)} left` : "");
    videoButtons(["Hide", "Stop"]);
    ui.videoStop.disabled = !!video.stopping;
    setTitleNote(`Video scenes ${finished}/${video.total}`);
    return;
  }

  const made = video.done || 0;
  const missed = (video.total || 0) - made;
  ui.videoBox.classList.toggle("failed", !made);
  ui.videoBar.classList.remove("waiting");
  ui.videoBar.style.width = "100%";
  ui.videoPct.textContent = "";
  ui.videoElapsed.textContent = `${fmt(video.elapsed)} in all`;
  ui.videoTitle.textContent = video.state === "stopped"
    ? `Stopped · ${made} of ${video.total} made`
    : made === video.total
      ? `${made} video scene${made === 1 ? " is" : "s are"} ready`
      : `${made} of ${video.total} video scenes were made`;
  ui.videoNote.textContent = video.error
    || (missed
      ? "The scenes that were not made are still marked, so they can be tried again."
      : "They are on the timeline now. Pictures can be drawn again.");
  videoButtons(["Done"]);
  setTitleNote(made ? "Video scenes ready" : "Video scenes failed");
}

/** Follow the batch until it ends, reloading the timeline as clips land. */
async function watchVideoBatch() {
  if (state.videoWatching) return;
  state.videoWatching = true;
  let seen = -1;
  try {
    for (;;) {
      const video = await fetchVideo();
      const ended = videoEnded(video);
      if (ended) {
        // This video's batch is over and the next video has the card. Say how
        // it went; Close picks the watching up again.
        try { await refresh(); } catch { /* the timeline on screen is still valid */ }
        drawVideoBatch(ended);
        break;
      }
      if (!video.state) { ui.videoModal.hidden = true; break; }
      if (!video.running && video.task_id !== state.taskId) {
        // The last batch was another video's: its result is told there.
        ui.videoModal.hidden = true;
        setTitleNote("");
        drawVideoButton();
        break;
      }
      if (video.task_id === state.taskId && video.done !== seen) {
        // A clip just replaced a picture on disk; show it under the window.
        if (seen >= 0 || !video.running) {
          try { await refresh(); } catch { /* the next round tries again */ }
        }
        seen = video.done;
      }
      drawVideoBatch(video);
      if (!video.running) break;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  } finally {
    state.videoWatching = false;
  }
}

/** On opening a video: is a batch running, or did one end while away? */
async function adoptVideoBatch() {
  const video = await fetchVideo();
  drawInspector();
  if (video.running || videoEnded(video)
    || (video.state && video.task_id === state.taskId)) watchVideoBatch();
}

ui.videoScenes.addEventListener("click", () => {
  const video = state.video || {};
  // Scenes marked here while another video has the card: ask, then queue.
  const toQueue = videoElsewhere(video) && !videoQueued(video) && markedScenes().length;
  if (video.running && !toQueue) {
    state.videoHidden = false;
    drawVideoBatch(state.video);
    return;
  }
  askForVideoScenes();
});
ui.videoHide.addEventListener("click", () => {
  state.videoHidden = true;
  ui.videoModal.hidden = true;
});
ui.animateAll.addEventListener("click", async () => {
  try {
    await savePromptEdit();
    const pictures = await call(
      "GET", `/tasks/${encodeURIComponent(state.taskId)}/video-scenes/pictures`);
    if (!pictures.scenes.length && !markedScenes().length) {
      say((pictures.skipped || []).length
        ? "No picture can be animated: every still scene is longer than a video scene can cover."
        : "Every picture in this video is already a video scene.", "warn");
      return;
    }
    askForVideoScenes(pictures);
  } catch (error) {
    say(String(error.message || error), "error");
  }
});
ui.videoCancel.addEventListener("click", () => { ui.videoModal.hidden = true; });
ui.videoStart.addEventListener("click", async () => {
  ui.videoStart.disabled = true;
  try {
    // A prompt still being typed belongs to the clip made from it.
    await savePromptEdit();
    state.videoHidden = false;
    state.video = await call(
      "POST", `/tasks/${encodeURIComponent(state.taskId)}/video-scenes`
        + (state.videoEverything ? "?everything=true" : ""));
    drawVideoBatch(state.video);
    watchVideoBatch();
  } catch (error) {
    ui.videoBox.classList.add("failed");
    ui.videoNote.textContent = String(error.message || error);
    ui.videoStart.disabled = false;
  }
});
ui.videoStop.addEventListener("click", async () => {
  ui.videoStop.disabled = true;
  try {
    if (videoQueued()) {
      // Waiting, not running: leave the queue. The video that has the card
      // is not touched.
      state.video = await call(
        "DELETE", `/tasks/${encodeURIComponent(state.taskId)}/video-scenes`);
      ui.videoModal.hidden = true;
      setTitleNote("");
      drawVideoButton();
      say("This video is out of the queue. Its scenes are still marked.");
      return;
    }
    await call("DELETE", "/video-scenes");
  } catch (error) {
    ui.videoNote.textContent = String(error.message || error);
    ui.videoStop.disabled = false;
  }
});
ui.videoDone.addEventListener("click", async () => {
  ui.videoModal.hidden = true;
  setTitleNote("");
  // Forget the finished batch, or the window would come back on reload.
  // By this video's name first: the plain route would stop a batch another
  // video has started since.
  try {
    await call("DELETE", `/tasks/${encodeURIComponent(state.taskId)}/video-scenes`);
  } catch { /* an older server; or nothing to forget */ }
  let video = await fetchVideo();
  if (!video.running && video.state && video.task_id === state.taskId) {
    try { await call("DELETE", "/video-scenes"); } catch { /* nothing to forget */ }
    video = await fetchVideo();
  }
  try { await refresh(); } catch { /* the timeline on screen is still valid */ }
  drawVideoButton();
  // Another video is still at it: keep the button's count moving.
  if (video.running) watchVideoBatch();
});

/* ---------- 做过的片子 ----------

   编辑器原来只能靠粘一个 uuid 进来。第二天回来想接着改前天那条片子时，那
   串 id 早就找不着了——而磁盘上明明记着每一条。

   列表来自 ``/library``（磁盘），不是 ``/tasks``（进程内存）：后者 API 一
   重启就空了，正好是"隔了一天回来"这种情况下必然为空。 */
const LIBRARY_PAGE = 24;

function libraryCard(row) {
  const card = document.createElement("div");
  card.className = "card";
  card.title = row.task_id;

  if (row.thumb_url) {
    // 封面用第一段画面。<video> 比 <img> 麻烦，但这些素材本来就是 mp4。
    const shot = document.createElement("video");
    shot.className = "shot";
    shot.muted = true;
    shot.preload = "metadata";
    // #t=0.1 让浏览器真的去解一帧出来，否则多半是一块黑。
    shot.src = row.thumb_url + "#t=0.1";
    card.appendChild(shot);
  } else {
    const blank = document.createElement("div");
    blank.className = "shot";
    card.appendChild(blank);
  }

  const body = document.createElement("div");
  body.className = "body";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = row.subject || row.task_id;
  body.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [`${row.scenes} scene${row.scenes === 1 ? "" : "s"}`];
  if (row.narration) bits.push(fmt(row.narration));
  if (row.updated_at) bits.push(whenShort(row.updated_at));
  meta.textContent = bits.join(" · ");
  body.appendChild(meta);

  const unfinished = row.unfinished || {};

  const tags = document.createElement("div");
  tags.className = "tags";
  if (row.rendered) tags.appendChild(tag("rendered", "done"));
  if (row.audio_stale) tags.appendChild(tag("needs re-record", "warn"));
  if (unfinished.resumable) {
    tags.appendChild(
      tag(`${unfinished.pictures} of ${unfinished.needed} pictures`, "warn")
    );
  } else if (!row.scenes) {
    tags.appendChild(tag("no scenes", "warn"));
  }
  if (tags.children.length) body.appendChild(tags);

  /* 画面出了一半就断掉的那种任务。
     文生图一张一张买，二十九张之后连接一掐——那二十九张的钱已经付了，文件
     也在盘上，可清单里没有场景，于是这张卡片以前写着 "0 scenes"，点进去
     一片空白，用户只能从头再来一遍、把那二十九张重新买一次。
     这个按钮从断的那张接着买，已经出完的不再买。 */
  if (unfinished.resumable) {
    const left = Math.max(0, unfinished.needed - unfinished.pictures);
    const resume = document.createElement("button");
    resume.className = "resume";
    resume.textContent = `Finish the pictures (${left} to go)`;
    resume.title =
      `${unfinished.pictures} picture(s) are already generated and will not ` +
      `be generated or paid for again; ${left} more are needed to cover ` +
      `${fmt(unfinished.narration)} of narration.`;
    resume.addEventListener("click", async (event) => {
      // 卡片本身是"点开这条任务"，别让这一下顺带把编辑器也切过去。
      event.stopPropagation();
      resume.disabled = true;
      resume.textContent = "Starting…";
      try {
        await call("POST", `/tasks/${row.task_id}/materials`, {});
        closeLibrary();
        ui.task.value = row.task_id;
        say(
          `Finishing the pictures for this task: ${left} to go, keeping the ` +
          `${unfinished.pictures} already generated. This screen will show ` +
          `the scenes once they are done.`,
          "ok"
        );
        load(row.task_id);
      } catch (error) {
        resume.disabled = false;
        resume.textContent = `Finish the pictures (${left} to go)`;
        say(error.message, "bad");
      }
    });
    body.appendChild(resume);
  }

  card.appendChild(body);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "card-delete";
  remove.textContent = "Delete";
  remove.title = "Delete this video permanently";
  remove.addEventListener("click", (event) => {
    // 卡片本身是"打开这条片子"；删除不该顺带把它打开。
    event.stopPropagation();
    askDeleteVideo(row.task_id, row.subject || row.task_id, card);
  });
  card.appendChild(remove);

  card.addEventListener("click", () => {
    closeLibrary();
    ui.task.value = row.task_id;
    load(row.task_id);
  });
  return card;
}

function tag(text, kind) {
  const node = document.createElement("span");
  node.className = "tag" + (kind ? " " + kind : "");
  node.textContent = text;
  return node;
}

/** "3 天前" 这种。精确到分钟没有意义，用户要的是"哪一条比较新"。 */
function whenShort(epochSeconds) {
  const seconds = Math.max(0, Date.now() / 1000 - Number(epochSeconds));
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  const days = Math.round(seconds / 86400);
  return days <= 30 ? `${days}d ago` : new Date(epochSeconds * 1000)
    .toISOString().slice(0, 10);
}

async function loadLibrary(page) {
  const wanted = page || 1;
  ui.library.hidden = false;
  if (wanted === 1) {
    ui.libraryGrid.innerHTML = "";
    ui.libraryCount.textContent = "Loading…";
  }
  try {
    const data = await call("GET",
      `/library?page=${wanted}&page_size=${LIBRARY_PAGE}`) || {};
    const rows = data.tasks || [];
    for (const row of rows) ui.libraryGrid.appendChild(libraryCard(row));
    const shown = ui.libraryGrid.children.length;
    ui.libraryCount.textContent = data.total
      ? `${shown} of ${data.total}`
      : "";
    if (!data.total) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "No videos yet. Press “+ New video” to make "
        + "your first one.";
      ui.libraryGrid.appendChild(empty);
    }
    state.libraryPage = wanted;
    ui.libraryMoreRow.hidden = shown >= (data.total || 0);
  } catch (error) {
    ui.libraryCount.textContent = "";
    // 失败要说在该出现视频的那块地方。只在角落里写一行小字，看起来就是
    // "一条片子都没有"——而那是完全不同的一件事。
    const failed = document.createElement("p");
    failed.className = "hint";
    failed.textContent = `Could not read your videos: ${error.message || error}. `
      + "If you have just updated, the API needs restarting to serve this.";
    ui.libraryGrid.appendChild(failed);
  }
}

function closeLibrary() {
  ui.library.hidden = true;
}

/** 回到"我的视频"那一页：编辑器里不再开着任何一条片子。

    是真的换页，不是在这一条上面盖一层：盖一层的话地址栏里还是这条片子，
    刷新一下又回到它里面，"关闭"也还是它——那不叫回到列表。没存完的提示词
    由 pagehide 那一下发出去。 */
function backToLibrary() {
  window.location.assign(window.location.pathname);
}

ui.libraryToggle.addEventListener("click", () => {
  // 嵌在 WebUI 里时这一页就是为那一条片子开的，没有"列表页"可回。
  if (state.taskId && !EMBEDDED) { backToLibrary(); return; }
  if (ui.library.hidden) loadLibrary(1); else closeLibrary();
});

/* ---------- 永久删除一条片子 ----------

   编辑器里别的改动都能撤销，这一件不能，所以它先问。列表里每张卡片上有
   一个，打开的那条片子顶栏里也有一个。 */
const deleting = { id: "", card: null, busy: false };

function askDeleteVideo(taskId, name, card) {
  Object.assign(deleting, { id: taskId, card: card || null, busy: false });
  ui.deleteWhat.textContent = name || taskId;
  ui.deleteNote.textContent = "";
  ui.deleteNote.className = "hint";
  ui.deleteConfirm.disabled = false;
  ui.deleteModal.hidden = false;
  // 焦点落在"取消"上：手快按了回车，什么都不会没。
  ui.deleteCancel.focus();
}

function closeDeleteVideo() {
  if (deleting.busy) return;
  ui.deleteModal.hidden = true;
  deleting.id = "";
}

async function deleteVideoForGood() {
  if (deleting.busy || !deleting.id) return;
  const { id, card } = deleting;
  deleting.busy = true;
  ui.deleteConfirm.disabled = true;
  ui.deleteNote.className = "hint pending";
  ui.deleteNote.textContent = "Deleting…";
  const open = id === state.taskId;
  if (open) {
    // 正在播的预览占着文件，Windows 上占着就删不掉。先放手。
    for (const player of document.querySelectorAll("video, audio")) {
      try { player.pause(); player.removeAttribute("src"); player.load(); }
      catch { /* 放不掉就让服务端说 */ }
    }
  }
  try {
    await call("DELETE", `/library/${encodeURIComponent(id)}`);
  } catch (error) {
    deleting.busy = false;
    ui.deleteConfirm.disabled = false;
    ui.deleteNote.className = "hint warn";
    ui.deleteNote.textContent = String(error.message || error);
    return;
  }
  deleting.busy = false;
  ui.deleteModal.hidden = true;
  deleting.id = "";
  if (open) {
    // 这条片子没了，编辑器里不能还开着它。
    promptEdit = null;
    backToLibrary();
    return;
  }
  if (card) card.remove();
  // 数字和"再来一页"都按少了一条重算。
  if (!ui.library.hidden) loadLibrary(1);
}

ui.deleteCancel.addEventListener("click", closeDeleteVideo);
ui.deleteConfirm.addEventListener("click", deleteVideoForGood);
ui.deleteModal.addEventListener("click", (event) => {
  if (event.target === ui.deleteModal) closeDeleteVideo();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !ui.deleteModal.hidden) closeDeleteVideo();
});
ui.deleteVideo.addEventListener("click", () => {
  if (!state.taskId) return;
  askDeleteVideo(state.taskId, (state.data && state.data.subject) || state.taskId);
});
ui.libraryClose.addEventListener("click", closeLibrary);
ui.libraryMore.addEventListener("click", () =>
  loadLibrary((state.libraryPage || 1) + 1));

/* ---------- 整条片子的转场 ----------

   单段那个下拉框只管一段。"全部换成淡入"是另一件事，而且必须说清楚那几个
   自己挑过的段落会怎样——否则用户改完整条，看到几段纹丝不动，只会以为没存
   上。 */
function fillEffectsPanel(data) {
  const fill = (select, ids, labels, chosen) => {
    select.innerHTML = "";
    for (const id of ids || []) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = labels[id] || id;
      select.appendChild(option);
    }
    if (chosen) select.value = chosen;
  };
  fill(ui.effectsTransition, data.transitions, TRANSITION_LABELS,
    data.transition || "none");
  fill(ui.effectsMotion, data.image_motions, MOTION_LABELS,
    data.image_motion || "zoom");
  // 成片精修。第一项是"关掉"：成片照旧由原来的渲染器出。
  const looks = data.finish_looks || [];
  const lookLabels = { "": "Off — the classic render" };
  for (const look of looks) lookLabels[look.id] = look.label;
  fill(ui.effectsFinish, [""].concat(looks.map((look) => look.id)), lookLabels, "");
  ui.effectsFinish.value = data.finish_look || "";

  const own = Number(data.scenes_with_own_transition || 0);
  ui.effectsOwn.textContent = own
    ? `${own} scene${own === 1 ? "" : "s"} chose ${own === 1 ? "its" : "their"} `
      + "own transition and will keep it unless you tick the box."
    : "No scene has chosen its own, so this applies to all of them.";
  ui.effectsApply.checked = false;
  ui.effectsNote.textContent = "";
}

async function openEffects() {
  ui.effectsPanel.hidden = false;
  ui.effectsNote.textContent = "Loading…";
  try {
    fillEffectsPanel((await call("GET",
      `/tasks/${encodeURIComponent(state.taskId)}/effects`)) || {});
  } catch (error) {
    ui.effectsNote.textContent = String(error.message || error);
  }
}

function closeEffects() { ui.effectsPanel.hidden = true; }

ui.effectsToggle.addEventListener("click", () => {
  if (ui.effectsPanel.hidden) openEffects(); else closeEffects();
});
ui.effectsClose.addEventListener("click", closeEffects);

ui.effectsSave.addEventListener("click", () => run("Saving the effects", async () => {
  await call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/effects`, {
    transition: ui.effectsTransition.value,
    image_motion: ui.effectsMotion.value,
    finish_look: ui.effectsFinish.value,
    apply_to_scenes: ui.effectsApply.checked,
  });
  closeEffects();
  return "";
}));

/* ---------- 发到 YouTube 的文案和封面 ----------

   这个面板不改成片，一个字节都不改：它是上传时要粘进 YouTube 的那段文字和
   那张图，跟着这条片子一起存着，第二天回来还在。

   面板第一句话先说这条片子是竖的还是横的。封面的形状完全由画幅决定——9:16
   的片子配一张横封面，传上去会被两边裁掉，而主体通常正在那里。所以这件事
   必须在"画封面"那个按钮之前说清楚,而不是等图出来了让用户自己发现。 */

/** 面板里当前这一份发布信息。上限（标题 100 字等）由服务端给，不写死在这里。 */
let publish = null;

/** "还能写多少字"。超了就标红——超长服务端会拒，提前说比保存时报错有用。 */
function countLabel(node, used, limit) {
  if (!limit) { node.textContent = ""; return; }
  node.textContent = `${used} / ${limit}`;
  node.classList.toggle("over", used > limit);
}

function drawPublishCounts() {
  if (!publish) return;
  countLabel(ui.publishTitleCount, ui.publishTitle.value.length, publish.title_max);
  countLabel(ui.publishDescriptionCount, ui.publishDescription.value.length,
    publish.description_max);
  countLabel(ui.publishPromptCount, ui.publishPrompt.value.length, publish.prompt_max);
  countLabel(ui.publishTextCount, ui.publishText.value.length, publish.text_max);
}

/** 这份草稿是不是降级拼出来的，是的话为什么。不是就返回空字符串。 */
function fallbackReason(data) {
  if (!data || data.text_source !== "fallback") return "";
  // 供应商那句话自己带句号，照原样接上去会得到 "config.toml file.."。
  const why = String(data.text_error || "").trim().replace(/\.+$/, "");
  return "This draft was not written by a language model"
    + (why ? ` — ${why}` : " because the model was unavailable")
    + ". The title and tags below are a plain split of the subject. Fix the "
    + "model, then press the button again.";
}

/** 这条片子在 YouTube 上是哪一种东西，以及封面会是什么形状、多大。 */
function describeShape(data) {
  const vertical = data.aspect === "9:16";
  const shape = vertical ? "portrait" : data.aspect === "1:1" ? "square" : "landscape";
  const where = data.kind === "short"
    ? "This video is vertical, so it goes up as a Short."
    : data.kind === "square"
      ? "This video is square."
      : "This video is widescreen, so it goes up as a normal video.";
  return `${where} The thumbnail will be drawn ${shape}, `
    + `${data.thumbnail_width}×${data.thumbnail_height} — the shape YouTube `
    + `shows next to a ${data.aspect} video.`;
}

function fillPublishPanel(data) {
  publish = data || {};
  ui.publishShape.textContent = publish.aspect ? describeShape(publish) : "";
  ui.publishTitle.value = publish.title || "";
  ui.publishDescription.value = publish.description || "";
  ui.publishTags.value = (publish.tags || []).join(", ");
  ui.publishPrompt.value = publish.thumbnail_prompt || "";
  drawPublishCounts();

  // 没有脚本时文案会很单薄，说出来——否则用户只会觉得模型写得差。
  if (!publish.has_script && !publish.has_subject) {
    ui.publishNote.textContent =
      "This video has neither a subject nor a script, so there is nothing to "
      + "write a title from.";
    ui.publishNote.className = "hint";
    ui.publishWrite.disabled = true;
  } else {
    ui.publishNote.textContent = publish.has_script ? ""
      : "This video has no saved script, so the text will be written from the "
        + "subject alone.";
    ui.publishNote.className = "hint";
    ui.publishWrite.disabled = false;
  }
  // 降级写出来的那一份要署名。一个被拆成几个词的标题和一行分词标签是
  // 很显眼的差，而"模型压根没跑"和"模型就这水平"是两件完全不同的事——
  // 前者改一行配置就好了，所以原因也一并说出来。
  const fallbackNote = fallbackReason(publish);
  if (fallbackNote) {
    ui.publishNote.textContent = fallbackNote;
    ui.publishNote.className = "hint warn";
  }

  // 文生图没配就别让用户按下那个按钮再看报错。
  ui.publishDraw.disabled = !publish.can_draw_thumbnail;
  // 说出它要花多久，以及那个服务得开着。"配置填了"不等于"它在应答"——
  // can_draw_thumbnail 只知道前者，所以这里不能说得像按下去一定成。
  ui.publishThumbHint.textContent = publish.can_draw_thumbnail
    ? "Drawn by the same image service that makes the pictures, at the shape "
      + "above, so that service has to be running. One picture takes a few "
      + "minutes on a local machine — the button counts the seconds while it "
      + "works. Edit the description below first if it is not what you want."
    : "No image service is configured, so the thumbnail cannot be drawn here. "
      + "Set openai_image_base_url and openai_image_model in config.toml.";

  drawScenePicker(publish);
  fillTextPanel(publish);
  showThumbnail(publish);
}

/** 压在封面上的那行字。字体和位置的选项由服务端给——装着哪些字体只有它
    知道，界面写死一份迟早对不上。 */
function fillTextPanel(data) {
  const style = data.text || {};

  ui.publishTextFont.innerHTML = "";
  for (const name of data.text_fonts || []) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name.replace(/\.[^.]+$/, "");
    ui.publishTextFont.appendChild(option);
  }
  if (style.font) ui.publishTextFont.value = style.font;

  ui.publishTextPosition.innerHTML = "";
  for (const id of data.text_positions || []) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = TEXT_POSITION_LABELS[id] || id;
    ui.publishTextPosition.appendChild(option);
  }
  ui.publishTextPosition.value = style.position || "bottom";

  ui.publishText.value = style.text || "";
  ui.publishTextSize.value = String(style.size ?? 9);
  ui.publishTextStrokeWidth.value = String(style.stroke_width ?? 10);
  ui.publishTextColor.value = style.color || "#FFFFFF";
  ui.publishTextStrokeColor.value = style.stroke_color || "#000000";
  ui.publishTextBand.checked = style.band !== false;

  // 还没有图就先别让按：字要压在一张图上，没有图时按下去只会收到一句
  // "先挑一张图"。
  ui.publishTextApply.disabled = !data.has_picture;
  ui.publishTextNote.textContent = data.has_picture
    ? "" : "Choose a picture above first.";
  // 数一遍字数。必须在这里数，不能只靠 fillPublishPanel 里那一次：那一次
  // 跑在这个函数之前，那时文字框还是空的，计数器会一直显示 0。
  drawPublishCounts();
}

function textBody() {
  return {
    text: ui.publishText.value,
    font: ui.publishTextFont.value,
    position: ui.publishTextPosition.value,
    size: Number(ui.publishTextSize.value),
    color: ui.publishTextColor.value,
    stroke_color: ui.publishTextStrokeColor.value,
    stroke_width: Number(ui.publishTextStrokeWidth.value),
    band: ui.publishTextBand.checked,
  };
}

/** 可以拿来当封面的那些画面，排成一条可点的小图。

    素材本来就是 mp4（图片在出图时就烘成了带运镜的片段），所以这里和时间线、
    "我做过的片子"一样用 <video>：``#t=0.1`` 是为了让浏览器真的去解一帧出来，
    否则多半是一块黑。 */
function drawScenePicker(data) {
  ui.publishScenes.innerHTML = "";
  const scenes = data.scenes || [];
  if (!scenes.length) {
    ui.publishPickHint.textContent =
      "This video has no pictures on disk to choose from.";
    return;
  }
  ui.publishPickHint.textContent =
    `Click one of this video's own ${scenes.length} pictures to use it. `
    + "It is cropped to the shape above.";

  for (const scene of scenes) {
    const pick = document.createElement("button");
    pick.className = "pick";
    pick.type = "button";
    pick.title = scene.keyword || `Scene ${scene.index + 1}`;
    if (data.thumbnail_from === "scene" && data.thumbnail_scene === scene.index) {
      pick.classList.add("current");
    }
    const shot = scene.kind === "image"
      ? document.createElement("img")
      : document.createElement("video");
    if (shot.tagName === "VIDEO") {
      shot.muted = true;
      shot.preload = "metadata";
      shot.src = scene.url + "#t=0.1";
    } else {
      shot.src = scene.url;
      shot.alt = "";
    }
    pick.appendChild(shot);
    const label = document.createElement("span");
    label.className = "n";
    label.textContent = String(scene.index + 1);
    pick.appendChild(label);
    pick.addEventListener("click", () => useScene(scene.index));
    ui.publishScenes.appendChild(pick);
  }
}

const TEXT_POSITION_LABELS = {
  top: "Across the top", middle: "Across the middle", bottom: "Across the bottom",
};

/** 这张封面是从哪儿来的。用户下一步想换的时候，这是唯一要知道的事。 */
function thumbnailOrigin(data) {
  if (!data.thumbnail) return "";
  if (data.thumbnail_from === "scene" && data.thumbnail_scene != null) {
    return `Using the picture from scene ${data.thumbnail_scene + 1}.`;
  }
  if (data.thumbnail_from === "drawn") return "Using a picture drawn for it.";
  return "";
}

function useScene(index) {
  return publishAction("Cropping", ui.publishThumbNote, "Done.", async () => {
    // 先把面板里那几个字存下来再换图。不存的话，"先写字、再挑图"会用上一次
    // 存着的旧文字去合成——用户刚打的那一行就白打了。
    if (publish && publish.has_picture !== undefined) {
      await call("PUT",
        `/tasks/${encodeURIComponent(state.taskId)}/publish/thumbnail/text`,
        textBody());
    }
    return call("POST",
      `/tasks/${encodeURIComponent(state.taskId)}/publish/thumbnail/scene`,
      { index, at: 0 });
  });
}

function showThumbnail(data) {
  ui.publishThumbWhere.textContent = thumbnailOrigin(data);
  const url = data.thumbnail_url || "";
  ui.publishThumb.hidden = !url;
  ui.publishDownload.hidden = !url;
  if (!url) { ui.publishThumbImg.removeAttribute("src"); return; }
  ui.publishThumbImg.src = url;
  // 下载走专门那条路由：它带 Content-Disposition 和一个认得出来的文件名，
  // 而 /stream 另存下来的每一张都叫 youtube-thumbnail.png。
  ui.publishDownload.href =
    `${API}/tasks/${encodeURIComponent(state.taskId)}/publish/thumbnail`;
  ui.publishDownload.setAttribute("download", `thumbnail-${state.taskId}.png`);
  ui.publishThumb.style.aspectRatio =
    `${data.thumbnail_width || 16} / ${data.thumbnail_height || 9}`;
}

async function openPublish() {
  ui.publishPanel.hidden = false;
  ui.publishSaved.textContent = "Loading…";
  try {
    const data = await call("GET",
      `/tasks/${encodeURIComponent(state.taskId)}/publish`);
    ui.publishSaved.textContent = "";
    fillPublishPanel(data || {});
  } catch (error) {
    ui.publishSaved.textContent = String(error.message || error);
  }
}

function closePublish() { ui.publishPanel.hidden = true; }

ui.publishToggle.addEventListener("click", () => {
  if (ui.publishPanel.hidden) openPublish(); else closePublish();
});
ui.publishClose.addEventListener("click", closePublish);
for (const node of [ui.publishTitle, ui.publishDescription, ui.publishPrompt]) {
  node.addEventListener("input", drawPublishCounts);
}

/** 面板里的四个框，一次提交。 */
function publishBody() {
  return {
    title: ui.publishTitle.value,
    description: ui.publishDescription.value,
    tags: ui.publishTags.value,
    thumbnail_prompt: ui.publishPrompt.value,
  };
}

/* 这三个按钮不走 run()：run() 做完会 refresh() 整条时间线并把提示写到顶上
   那条横幅上。而这里改的东西和时间线无关，面板又是开着的——提示应该留在
   面板里,而且面板上没保存的输入不能被一次重读刷掉。 */

/** 一边等一边报时，报在刚按下的那个按钮旁边。

    出一张图在本机要好几分钟,而"按下去之后什么都没变"和"这个按钮是坏的"
    长得一模一样。所以这里每秒把已经等了多久写出来——一个会走的秒数是
    "它在跑"的唯一证据,而一句静止的"Drawing…"不是。

    报在按钮旁边而不是面板最底下：那一行要滚到底才看得见,用户的眼睛在
    他刚点的那个按钮上。 */
function publishTicker(node, label) {
  const started = Date.now();
  const elapsed = () => Math.round((Date.now() - started) / 1000);
  const tick = () => {
    node.className = "hint pending";
    node.textContent = `${label}… ${elapsed()}s`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  return (message, kind) => {
    clearInterval(timer);
    node.className = kind ? `hint ${kind}` : "hint";
    // 花了多久一并留着：下一次按之前，用户据此知道该等多久。
    node.textContent = message ? `${message} (${elapsed()}s)` : "";
  };
}

async function publishAction(label, note, success, operation) {
  if (state.busy) return;
  state.busy = true;
  // 另外两处的提示先清掉，否则上一次那句"Saved."留在旁边，看起来像是
  // 这一次的结果。
  for (const other of [ui.publishNote, ui.publishThumbNote, ui.publishSaved,
    ui.publishTextNote]) {
    if (other !== note) { other.textContent = ""; other.className = "hint"; }
  }
  const done = publishTicker(note, label);
  for (const node of [ui.publishWrite, ui.publishDraw, ui.publishSave]) {
    node.disabled = true;
  }
  try {
    const data = await operation();
    fillPublishPanel(data);
    // 成功文案可以取决于结果：降级拼出来的那一份不该报成一句干净的
    // "Written."，否则面板上那条橙色警告和这里的绿灯自相矛盾。
    const message = typeof success === "function" ? success(data) : success;
    done(message, message && /not written by/.test(message) ? "warn" : "");
  } catch (error) {
    done(String(error.message || error), "warn");
  } finally {
    state.busy = false;
    // 失败时 fillPublishPanel 没跑过，这三个按钮得自己解锁，否则一次网络
    // 故障就把面板永久锁住了。
    ui.publishSave.disabled = false;
    if (publish) {
      ui.publishWrite.disabled = !(publish.has_subject || publish.has_script);
      ui.publishDraw.disabled = !publish.can_draw_thumbnail;
    } else {
      ui.publishWrite.disabled = false;
      ui.publishDraw.disabled = false;
    }
  }
}

ui.publishSave.addEventListener("click", () =>
  publishAction("Saving", ui.publishSaved, "Saved.", () =>
    call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/publish`,
      publishBody())));

ui.publishWrite.addEventListener("click", () =>
  publishAction("Writing", ui.publishNote,
    (data) => fallbackReason(data) || "Written.", () =>
    // 先存一次：模型写出来的那一份会盖掉面板里的内容，而用户可能刚刚改过
    // 封面说明。存下来至少让"上一版"留在磁盘上。
    call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/publish`, publishBody())
      .then(() => call("POST",
        `/tasks/${encodeURIComponent(state.taskId)}/publish/text`, {}))));

ui.publishText.addEventListener("input", drawPublishCounts);

ui.publishTextApply.addEventListener("click", () =>
  publishAction("Lettering", ui.publishTextNote, "Done.", () =>
    call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/publish/thumbnail/text`,
      textBody())));

ui.publishDraw.addEventListener("click", () =>
  publishAction("Drawing", ui.publishThumbNote, "Drawn.", () =>
    call("POST", `/tasks/${encodeURIComponent(state.taskId)}/publish/thumbnail`,
      { prompt: ui.publishPrompt.value })));

/* ---------- YouTube 会盖住哪里 ----------

   字幕"放对了没有"光看成片的画面是看不出来的：片子传上去以后，YouTube 还
   要在上面压自己的东西——Shorts 的频道名、标题、右边那一列按钮，正好压在
   默认的"底部"字幕上。所以调字幕时把那一层的样子画出来，字幕压在它底下
   就直说。

   它只是一张示意图：尺寸是照着手机上的样子估的，不同手机、不同版本会差一
   点。它不进成片。 */
const YT_PREF = "mpt-editor-youtube-guide";

function drawYouTube() {
  const open = !ui.subsPanel.hidden;
  ui.subs.classList.toggle("movable", open);
  const show = open && ui.subsYt.checked;
  ui.yt.hidden = !show;
  if (!show) {
    ui.subs.classList.remove("clash");
    return;
  }
  const settings = (state.data && state.data.render_settings) || {};
  const width = Number(settings.frame_width) || 1080;
  const height = Number(settings.frame_height) || 1920;
  // 竖的按 Shorts 画，横的和方的按普通播放器画。
  const shape = height > width ? "shorts" : "wide";
  if (ui.yt.dataset.shape !== shape) ui.yt.dataset.shape = shape;
  const title = (state.data && state.data.subject) || "Your video title";
  for (const node of ui.yt.querySelectorAll(".yt-title")) {
    if (node.textContent !== title) node.textContent = title;
  }
}

/** 此刻这行字压在 YouTube 的哪几样东西底下。量的就是画出来的那几块，所以
    说的和看到的是一回事。 */
function captionClashes() {
  if (ui.yt.hidden || ui.subs.hidden) return [];
  const box = ui.subsText.getBoundingClientRect();
  const under = [];
  for (const zone of ui.yt.querySelectorAll(".yt-zone")) {
    const area = zone.getBoundingClientRect();
    if (!area.width || !area.height) continue;
    if (box.left < area.right && box.right > area.left
        && box.top < area.bottom && box.bottom > area.top) {
      under.push(zone.dataset.covers);
    }
  }
  return under;
}

function noteCaptionClash() {
  if (ui.subsPanel.hidden) return;
  if (ui.yt.hidden) {
    ui.subsSafe.textContent = "";
    return;
  }
  if (ui.subs.hidden) return;
  const under = captionClashes();
  ui.subs.classList.toggle("clash", under.length > 0);
  let text;
  if (!under.length) {
    text = "Clear: nothing of YouTube's covers the caption here.";
  } else if (ui.yt.dataset.shape === "wide") {
    text = `Here the caption is under ${under.join(" and ")} whenever the `
      + "viewer pauses or moves the mouse. Drag it clear if that matters to you.";
  } else {
    text = `Here the caption is under ${under.join(" and ")} on YouTube Shorts, `
      + "so viewers cannot read it. Drag it clear, or pick another position.";
  }
  if (ui.subsSafe.textContent !== text) ui.subsSafe.textContent = text;
  ui.subsSafe.className = under.length ? "hint warn" : "hint";
}

try { ui.subsYt.checked = localStorage.getItem(YT_PREF) !== "off"; }
catch { /* 记不住就每次都开着 */ }
ui.subsYt.addEventListener("change", () => {
  try { localStorage.setItem(YT_PREF, ui.subsYt.checked ? "on" : "off"); }
  catch { /* 同上 */ }
  drawSubtitle();
  drawYouTube();
  noteCaptionClash();
});

/* 把画面上那行字上下拖到想要的地方。

   拖出来的就是"离顶部百分之几"那一种位置，和面板里那个数是同一个值，算法
   也和渲染器一样：百分比乘的是"画面高减去字幕高"。只在字幕面板开着时能
   拖；平时点它还是改这一条的字。 */
const captionDrag = { on: false, moved: false, startY: 0, startTop: 0, justMoved: false };

ui.subsText.addEventListener("pointerdown", (event) => {
  if (ui.subsPanel.hidden || event.button !== 0) return;
  if (![...ui.subsPosition.options].some((option) => option.value === "custom")) return;
  Object.assign(captionDrag, {
    on: true, moved: false, startY: event.clientY, startTop: ui.subs.offsetTop,
  });
  try { ui.subsText.setPointerCapture(event.pointerId); } catch { /* 不要紧 */ }
  event.preventDefault();
});

ui.subsText.addEventListener("pointermove", (event) => {
  if (!captionDrag.on) return;
  const moved = event.clientY - captionDrag.startY;
  if (!captionDrag.moved && Math.abs(moved) < 4) return;
  captionDrag.moved = true;
  const room = (ui.frame.clientHeight || 0) - (ui.subs.offsetHeight || 0);
  if (room <= 0) return;
  const top = Math.max(0, Math.min(room, captionDrag.startTop + moved));
  ui.subsPosition.value = "custom";
  ui.subsCustom.value = String(Math.round((top / room) * 1000) / 10);
  previewSubtitleStyle();
});

function endCaptionDrag() {
  if (!captionDrag.on) return;
  captionDrag.on = false;
  if (!captionDrag.moved) return;
  // 松手会跟着来一次 click；那一下不是"我要改这条字幕的字"。
  captionDrag.justMoved = true;
  setTimeout(() => { captionDrag.justMoved = false; }, 300);
}
ui.subsText.addEventListener("pointerup", endCaptionDrag);
ui.subsText.addEventListener("pointercancel", endCaptionDrag);
ui.subs.addEventListener("click", (event) => {
  if (!captionDrag.justMoved) return;
  captionDrag.justMoved = false;
  event.stopPropagation();
  event.preventDefault();
}, true);

/* ---------- 字幕的样子 ----------

   改的时候画面上那行字立刻跟着变："这个颜色压在这张图上看不看得清"只能这样
   判断,光看一个 #RRGGBB 没有任何意义。所以这个面板每动一下就把当前值写进
   render_settings 再重画一次,保存只是把同一份值落盘。 */
const POSITION_LABELS = {
  top: "Top", center: "Centre", bottom: "Bottom",
  two_thirds_bottom: "Two thirds down", custom: "A set distance down",
};

/** 把真正要烧进成片的那款字加载进浏览器，失败就退回系统字体。 */
async function useFont(name) {
  const family = "mpt-subtitle";
  if (!name) { ui.subsText.style.fontFamily = ""; return; }
  try {
    const face = new FontFace(family, `url("${API}/fonts/${encodeURI(name)}")`);
    await face.load();
    document.fonts.add(face);
    ui.subsText.style.fontFamily = `"${family}"`;
    ui.subsNote.textContent = "";
  } catch {
    // .ttc（字体合集）浏览器基本都不认。预览用系统字体，但字号、颜色、
    // 描边和位置仍然是真的——所以要说清楚哪一项不作数。
    ui.subsText.style.fontFamily = "";
    ui.subsNote.textContent =
      "This font cannot be shown in the browser, so the preview uses a "
      + "stand-in typeface. Everything else here is accurate.";
  }
}

/** 把面板里的值写进预览用的那份设置，并立刻重画。 */
function previewSubtitleStyle() {
  const settings = (state.data && state.data.render_settings) || {};
  const style = settings.subtitle || (settings.subtitle = {});
  style.enabled = ui.subsEnabled.checked;
  style.font_size = Number(ui.subsSize.value) || 60;
  style.color = ui.subsColor.value;
  style.stroke_color = ui.subsStrokeColor.value;
  style.stroke_width = Number(ui.subsStrokeWidth.value) || 0;
  style.position = ui.subsPosition.value;
  style.custom_position = Number(ui.subsCustom.value) || 0;
  style.background = ui.subsBgOn.checked ? ui.subsBg.value : false;
  ui.subsCustomField.hidden = style.position !== "custom";
  ui.subsBgField.hidden = !ui.subsBgOn.checked;
  // 此刻没有字幕在屏幕上就挪到第一条上去，否则这个面板改了半天看不见效果。
  const segments = (state.data && state.data.narration_segments) || [];
  if (!activeCue() && segments.length) {
    state.playhead = (segments[0].start + segments[0].end) / 2;
    syncPreview(true);
    movePlayhead();
  }
  drawSubtitle();
}

function fillSubtitlePanel(style) {
  ui.subsFont.innerHTML = "";
  for (const name of style.fonts || []) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name.replace(/\.[^.]+$/, "");
    ui.subsFont.appendChild(option);
  }
  if (style.font_name) ui.subsFont.value = style.font_name;

  ui.subsPosition.innerHTML = "";
  for (const id of style.positions || []) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = POSITION_LABELS[id] || id;
    ui.subsPosition.appendChild(option);
  }
  ui.subsPosition.value = style.position || "bottom";

  ui.subsEnabled.checked = style.enabled !== false;
  ui.subsSize.value = String(style.font_size ?? 60);
  ui.subsColor.value = style.color || "#FFFFFF";
  ui.subsStrokeColor.value = style.stroke_color || "#000000";
  ui.subsStrokeWidth.value = String(style.stroke_width ?? 0);
  ui.subsCustom.value = String(style.custom_position ?? 70);
  const background = typeof style.background === "string" ? style.background : "";
  ui.subsBgOn.checked = !!background;
  ui.subsBg.value = background || "#000000";
  useFont(ui.subsFont.value);
  previewSubtitleStyle();
}

async function openSubtitles() {
  ui.subsPanel.hidden = false;
  drawYouTube();
  ui.subsNote.textContent = "Loading…";
  try {
    const style = await call("GET",
      `/tasks/${encodeURIComponent(state.taskId)}/subtitles/style`);
    ui.subsNote.textContent = "";
    fillSubtitlePanel(style || {});
  } catch (error) {
    ui.subsNote.textContent = String(error.message || error);
  }
}

function closeSubtitles() {
  ui.subsPanel.hidden = true;
  drawYouTube();
  // 面板关了就把预览恢复成磁盘上那一份，免得没保存的改动留在画面上骗人。
  refresh().catch(() => {});
}

ui.subsToggle.addEventListener("click", () => {
  if (ui.subsPanel.hidden) openSubtitles(); else closeSubtitles();
});
ui.subsClose.addEventListener("click", closeSubtitles);
ui.subsFont.addEventListener("change", () => useFont(ui.subsFont.value));
for (const node of [ui.subsEnabled, ui.subsSize, ui.subsPosition, ui.subsColor,
  ui.subsStrokeColor, ui.subsStrokeWidth, ui.subsCustom, ui.subsBgOn, ui.subsBg]) {
  node.addEventListener("input", previewSubtitleStyle);
  node.addEventListener("change", previewSubtitleStyle);
}

ui.subsSave.addEventListener("click", () => run("Saving the subtitles", async () => {
  await call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/subtitles/style`, {
    enabled: ui.subsEnabled.checked,
    font_name: ui.subsFont.value,
    font_size: Number(ui.subsSize.value),
    color: ui.subsColor.value,
    stroke_color: ui.subsStrokeColor.value,
    stroke_width: Number(ui.subsStrokeWidth.value),
    background: ui.subsBgOn.checked ? ui.subsBg.value : false,
    position: ui.subsPosition.value,
    custom_position: Number(ui.subsCustom.value),
  });
  ui.subsPanel.hidden = true;
  return "";
}));

/* ---------- 改一条字幕 ----------

   改的只是屏幕上那行字：配音和时间点都不动。听写把一个词认错时要改的就是
   这里——为一个字重跑一遍 TTS 并作废全部时间点，代价完全不成比例。

   想改**念出来**的词是另一件事，走 Script 那个面板，然后重新配音。 */
function openCaption(index) {
  const segments = (state.data && state.data.narration_segments) || [];
  const cue = segments[index];
  if (!cue) return;
  state.cueIndex = index;
  ui.captionText.value = String(cue.text || "");
  ui.captionWhen.textContent = `${fmt(cue.start)} – ${fmt(cue.end)}`;
  ui.captionNote.textContent = "";
  ui.captionPanel.hidden = false;
  ui.captionText.focus();
  ui.captionText.select();
}

function closeCaption() {
  ui.captionPanel.hidden = true;
}

ui.subsText.addEventListener("click", () => {
  if (state.cueIndex >= 0) openCaption(state.cueIndex);
});
ui.captionClose.addEventListener("click", closeCaption);

ui.captionSave.addEventListener("click", () => run("Saving the caption", async () => {
  const index = state.cueIndex;
  if (index < 0) return "";
  await call("PUT",
    `/tasks/${encodeURIComponent(state.taskId)}/subtitles/${index}`,
    { text: ui.captionText.value });
  closeCaption();
  return "";
}));

/* ---------- 背景音乐 ----------

   配乐属于整条片子，所以它和场景面板无关。曲库在服务端（内置歌曲加用户自己
   丢进去的），所以列表和试听都要走接口。 */
const NO_MUSIC = "";
const RANDOM_MUSIC = "random";

function musicPath() {
  return `/tasks/${encodeURIComponent(state.taskId)}/music`;
}

/** 面板上现在摆着的那一份：传了一首新的之后照它重画，不用再问服务端。 */
let shownMusic = {};

function drawMusic(music) {
  shownMusic = music || {};
  ui.musicTrack.innerHTML = "";
  // 上传的曲子存成一串 UUID，显示它原来的名字。
  const names = music.names || {};
  const entries = [
    [NO_MUSIC, "No music"],
    [RANDOM_MUSIC, "A different one each render"],
    ...(music.tracks || []).map(
      (name) => [name, (names[name] || name).replace(/\.[^.]+$/, "")]),
  ];
  for (const [value, label] of entries) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    ui.musicTrack.appendChild(option);
  }
  const known = entries.some(([value]) => value === music.track);
  ui.musicTrack.value = known ? music.track : NO_MUSIC;
  ui.musicVolume.value = String(music.volume ?? 0.2);
  // 这条片子的配乐交给了外部服务（Sonilo / ElevenLabs）时别假装能在这里换：
  // 换了也只是被那边生成的曲子盖掉。
  ui.musicTrack.disabled = !!music.external;
  ui.musicUpload.disabled = !!music.external;
  ui.musicSave.disabled = !!music.external;
  ui.musicNote.textContent = music.external
    ? `This video gets its music from ${music.kind}; change that in the main `
      + "settings."
    : (!known && music.track ? `Currently: ${music.track}` : "");
  syncMusicPreview();
}

/** 试听选中的那一首。"不要音乐"和"随机"没有具体文件，就不给播放器。 */
function syncMusicPreview() {
  const name = ui.musicTrack.value;
  const playable = name && name !== RANDOM_MUSIC;
  ui.musicPreview.hidden = !playable;
  if (!playable) { ui.musicPreview.removeAttribute("src"); return; }
  ui.musicPreview.src = `${API}/music/${encodeURIComponent(name)}`;
}

/** 整条片子在每一刀上响的那一声。空选项就是"一刀都不要响"。 */
function drawCutSound(settings) {
  ui.cutSound.innerHTML = "";
  const entries = [["", "None"],
    ...(settings.sounds || []).map((name) => [name, name.replace(/\.[^.]+$/, "")])];
  for (const [value, label] of entries) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    ui.cutSound.appendChild(option);
  }
  const known = entries.some(([value]) => value === (settings.sound || ""));
  ui.cutSound.value = known ? (settings.sound || "") : "";
  ui.cutVolume.value = String(settings.volume ?? 0.5);
  syncCutPreview();
}

/** 试听那一声。"None" 没有文件可放，就不给播放器。

    音量照搬要存的那个值，和背景音乐那边一个道理：在这里听到的轻重和成片
    里一致，否则试听只能回答"是哪一声",回答不了"会不会太吵"。 */
function syncCutPreview() {
  const name = ui.cutSound.value;
  ui.cutPreview.hidden = !name;
  if (!name) { ui.cutPreview.removeAttribute("src"); return; }
  ui.cutPreview.src = `${API}/cut-sound/${encodeURIComponent(name)}`;
  const level = Number(ui.cutVolume.value);
  ui.cutPreview.volume = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0.5;
}

ui.cutSound.addEventListener("change", syncCutPreview);

/* 取不到音频文件时说出来。一个按了没反应的播放条是最难查的那种故障：
   用户只能看到"没声音"，看不到那是一个 404。 */
function sayPreviewFailed(player, note, what) {
  player.addEventListener("error", () => {
    if (!player.getAttribute("src")) return;
    note.textContent = `${what} could not be loaded. If you have just `
      + "updated, the API needs restarting to serve it.";
  });
}
sayPreviewFailed(ui.cutPreview, ui.musicNote, "That sound");
sayPreviewFailed(ui.musicPreview, ui.musicNote, "That track");
ui.cutVolume.addEventListener("input", () => {
  const level = Number(ui.cutVolume.value);
  if (Number.isFinite(level)) {
    ui.cutPreview.volume = Math.min(1, Math.max(0, level));
  }
});

async function openMusic() {
  ui.musicPanel.hidden = false;
  ui.musicNote.textContent = "Loading…";
  try {
    state.music = (await call("GET", musicPath())) || null;
    drawMusic(state.music || {});
    drawMusicTrack();
    drawCutSound((await call("GET",
      `/tasks/${encodeURIComponent(state.taskId)}/cut-sound`)) || {});
  } catch (error) {
    ui.musicNote.textContent = String(error.message || error);
  }
}

function closeMusic() {
  ui.musicPanel.hidden = true;
  // 关掉面板就停声，否则试听会一直放到用户去找哪里在响。
  try { ui.musicPreview.pause(); } catch { /* 还没加载 */ }
  try { ui.cutPreview.pause(); } catch { /* 还没加载 */ }
}

ui.musicToggle.addEventListener("click", () => {
  if (ui.musicPanel.hidden) openMusic(); else closeMusic();
});
ui.musicClose.addEventListener("click", closeMusic);
ui.musicTrack.addEventListener("change", syncMusicPreview);

/* 往曲库里加一首。文件一选就传，服务端当场验（格式、大小、是不是真的音频）；
   传完选中它、能试听，但还没用到这条片子上——那是"Save sound"的事，和在
   下拉框里换一首是同一个动作。 */
ui.musicUpload.addEventListener("change", async () => {
  const file = ui.musicUpload.files[0];
  if (!file) return;
  ui.musicUpload.disabled = true;
  ui.musicNote.textContent = `Uploading ${file.name}…`;
  try {
    const data = await upload("/music", file);
    if (!data || !data.file) throw new Error("the upload was not accepted");
    const tracks = [...(shownMusic.tracks || [])];
    if (!tracks.includes(data.file)) tracks.push(data.file);
    drawMusic({
      ...shownMusic,
      tracks,
      names: { ...(shownMusic.names || {}), [data.file]: data.name || file.name },
      track: data.file,
      // 音量框里刚改过、还没存的数别冲掉。
      volume: Number(ui.musicVolume.value) || shownMusic.volume,
    });
    ui.musicNote.textContent =
      `Added ${data.name || file.name}. Save sound to use it in this video.`;
  } catch (error) {
    ui.musicNote.textContent = `Could not add that song: ${error.message || error}`;
  } finally {
    ui.musicUpload.value = "";
    ui.musicUpload.disabled = !!shownMusic.external;
  }
});

ui.musicSave.addEventListener("click", () => run("Saving the sound", async () => {
  // 两项分开提交：一项被拒时另一项不该跟着丢掉。
  await call("PUT", `/tasks/${encodeURIComponent(state.taskId)}/cut-sound`, {
    sound: ui.cutSound.value,
    volume: Number(ui.cutVolume.value),
  });
  const music = await call("PUT", musicPath(), {
    track: ui.musicTrack.value,
    volume: Number(ui.musicVolume.value),
  });
  state.music = music || null;
  drawMusic(music || {});
  syncMusicBed();
  drawMusicTrack();
  closeMusic();
  return "";
}));

ui.render.addEventListener("click", () => run("Submitting the render", async () => {
  // 先把挡板放出来，再发请求：提交本身也要一两秒，而这一两秒里用户只想知道
  // "我到底点到没有"。
  showRender("Submitting the render…");
  try {
    await call("POST", `/tasks/${encodeURIComponent(state.taskId)}/render`);
  } catch (error) {
    // 提交就被拒了（还有图排着队、配音过期之类）。挡板不能留在那里转圈。
    ui.renderModal.hidden = true;
    throw error;
  }
  // 提交完立刻开始盯状态，这样渲染期间界面是"锁住并显示进度"，
  // 而不是看起来什么都没发生、改一下才被拒绝。
  state.wasBusy = true;
  watchTask();
  return "";
}));

// 嵌在 WebUI 里时，提交类操作交给外面的 Streamlit 按钮（见 EMBEDDED）。
// 任务 id 也由链接带过来，不需要再给一个输入框。
if (EMBEDDED) {
  for (const node of [ui.rerecord, ui.render, ui.task, ui.load,
    ui.newVideo, ui.libraryNew, ui.appSettings]) node.hidden = true;
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
} else {
  // 没带 task 进来就先给一张"做过的片子"：空编辑器加一个要粘 uuid 的输入框
  // 不是一个能开始工作的地方。
  loadLibrary(1);
  resumeCreation();
  adoptVideoBatch();
}

/* 关页前的最后一次机会。``keepalive`` 让这一条在页面没了之后继续发完；
   普通 fetch 会跟着页面一起被取消，那一笔就没了。 */
window.addEventListener("pagehide", () => {
  if (!promptEdit || !state.taskId) return;
  const value = String(promptEdit.input.value);
  if (value === promptEdit.saved) return;
  const at = promptEdit.id ? sceneIndexById(promptEdit.id) : promptEdit.index;
  if (!Number.isInteger(at) || at < 0) return;
  const headers = { "Content-Type": "application/json" };
  const key = apiKey();
  if (key) headers["x-api-key"] = key;
  try {
    fetch(API + scenePath(at), {
      method: "PUT",
      headers,
      body: JSON.stringify({ keyword: value }),
      keepalive: true,
    });
  } catch { /* 走都走了，没人可以告诉 */ }
});
