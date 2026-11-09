"""审片面板：渲染之前逐个场景检查、替换、重生成，确认后再出片。

Why a separate module
---------------------
``webui/Main.py`` 已经 9500 行以上。这个面板只在用户显式勾选"渲染前审片"后
才出现,把它放进 Main.py 只会让那个文件更难读。这里只依赖 ``tr`` 作为参数
传入,不反向导入 Main,避免循环导入。

``webui/Main.py`` is already past 9,500 lines and this panel only appears when
the user ticks "review before rendering". ``tr`` is passed in rather than
imported, so there is no circular import back into Main.

Why每个场景都包在 st.fragment 里
--------------------------------
Streamlit 的默认行为是任何一次控件交互都重跑整个脚本。一条 20 场景的时间线
里改一个关键词就会重跑 9500 行、并重新挂载 20 个播放器,慢到不能用。
``st.fragment`` 把重跑范围限制在这一行之内。只有会影响其它场景的操作
(删除、重排)才整页重跑。

Streamlit reruns the whole script on any widget interaction. On a 20-scene
timeline, editing one keyword would re-execute 9,500 lines and re-mount 20
players. ``st.fragment`` scopes the rerun to one row; only operations that
affect other rows (delete, reorder) rerun the page.
"""

from __future__ import annotations

import os
import urllib.request

import streamlit as st
import streamlit.components.v1 as components
from loguru import logger

from app.config import config
from app.services import material_upload, scene_editor, scene_manifest, webui_task


def has_scenes(task_id: str) -> bool:
    """这个任务是否已经审过片（即跑到素材阶段并留下了场景清单）。"""
    try:
        return bool(scene_manifest.load(task_id)["scenes"])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"failed to read scenes for task {task_id}: {exc}")
        return False


def render_panel(task_id: str, tr) -> bool:
    """渲染审片面板。

    返回 ``True`` 表示这个任务由审片面板接管显示；``False`` 表示它没有场景
    清单,调用方应该继续走原本的结果或失败展示。这个返回值很重要：停在素材
    阶段的任务没有成片,原本的分支会把它显示成"生成失败"。
    """
    if not has_scenes(task_id):
        return False

    described = scene_editor.describe(task_id)
    scenes = described["scenes"]

    st.success(tr("Review Scenes Ready").format(count=len(scenes)))

    # 画面张数由关键词个数决定，旁白句数由脚本决定，两者几乎从不相等。
    # 不说出来的话，"28 句旁白只有 12 段画面"看起来像是列表被截断了。
    lines = scene_manifest.split_narration(described.get("script"))
    covered = described.get("planned_total") or 0.0
    narration = described.get("narration_total") or 0.0
    if scenes and lines:
        st.caption(
            tr("Review Scene Coverage").format(
                scenes=len(scenes),
                covered=f"{covered:.1f}",
                total=f"{narration or covered:.1f}",
                lines=len(lines),
                each=f"{covered / len(scenes):.1f}" if covered else "0.0",
            )
        )
        # 画面在"一张图最多停多久"的上限之内铺不满旁白时会整组轮播。不说
        # 出来的话，下面每行写的时间段只是它第一次出现的位置，用户会以为
        # 渲染器又换了图。
        if narration > covered + 0.05 and covered > 0:
            st.warning(
                tr("Review Scene Repeats").format(
                    times=f"{narration / covered:.1f}",
                )
            )

    if described["audio_stale"]:
        st.warning(tr("Narration Out Of Date"))

    # 时间线编辑器已经包含逐段预览、重生成、替换、重排和裁剪，下面那份
    # 逐段列表就成了同一件事的第二套界面。两套并排既占地方又会让人以为
    # 它们是两个不同的东西，所以编辑器嵌上来之后就不再画它。
    #
    # API 没起来时编辑器嵌不进来，那份列表就是唯一能用的审片界面——它直接
    # 调 scene_editor，不经过 API，所以那种情况下必须保留。
    embedded = _render_timeline(task_id, tr)
    _render_actions(task_id, described, tr)

    if embedded:
        # 上传替换是编辑器里唯一没有的能力（那个页面没有上传入口），
        # 所以留一个折叠起来的紧凑表单，而不是把整份逐段列表再画一遍。
        _render_replace_form(task_id, scenes, tr)
    else:
        st.caption(tr("Review Scenes Hint"))
        for scene in scenes:
            _scene_row(task_id, scene, len(scenes), tr)

    return True


def _render_replace_form(task_id: str, scenes: list, tr) -> None:
    """用自己的文件替换某一段画面。默认折叠，不占地方。"""
    with st.expander(tr("Replace Scene"), expanded=False):
        labels = [
            f"{index + 1}. {scene.get('keyword') or scene.get('material') or ''}"
            for index, scene in enumerate(scenes)
        ]
        if not labels:
            return
        choice = st.selectbox(
            tr("Pick Scene To Replace"),
            options=list(range(len(labels))),
            format_func=lambda i: labels[i],
            key=f"review_replace_pick_{task_id}",
        )
        uploaded = st.file_uploader(
            tr("Replace Scene"),
            key=f"review_replace_upload_{task_id}",
            type=["mp4", "mov", "mkv", "webm", "png", "jpg", "jpeg", "webp"],
            help=tr("Replace Scene Help"),
        )
        if uploaded is not None and st.button(
            tr("Use Uploaded Material"),
            key=f"review_replace_apply_{task_id}",
            use_container_width=True,
        ):
            _replace_from_upload(task_id, int(choice), uploaded, tr)


def api_base() -> str:
    """时间线编辑器所在的地址。``endpoint`` 没配时按默认监听端口推断。"""
    endpoint = str(config.app.get("endpoint", "") or "").rstrip("/")
    if endpoint:
        return endpoint
    port = config.listen_port if hasattr(config, "listen_port") else 8080
    return f"http://127.0.0.1:{port}"


@st.cache_data(ttl=15, show_spinner=False)
def _api_is_up(base: str) -> bool:
    """API 进程在不在。

    带 15 秒缓存：这个判断每次重跑都要做一次，而用户在页面开着的时候
    把 API 起起来是很正常的事，所以既不能每次都探测、也不能一次缓存到底。
    """
    try:
        with urllib.request.urlopen(f"{base}/docs", timeout=1.5) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def _render_timeline(task_id: str, tr) -> bool:
    """把时间线编辑器直接嵌在审片面板里，返回是否真的嵌上了。

    为什么是 iframe 而不是 Streamlit 组件：Streamlit 每次控件交互都重跑整个
    脚本,做不了拖拽,也没法让播放头和画面逐帧对上。所以时间线是一个独立页面。

    为什么跨端口的 iframe 没问题：浏览器只是不允许父页面**读**跨源 iframe 的
    内容,显示和交互都不受影响。而编辑器自己的 fetch 和 <video> 请求都是
    发往它自身的源(8080),在 iframe 里看就是同源,所以既不需要 CORS,
    也不需要父页面传任何东西进去。

    A cross-origin iframe is fine here: the browser only stops the parent from
    *reading* inside it. The editor's own fetches and media requests go to its
    own origin, which is same-origin from within the frame, so no CORS is
    involved and nothing has to be passed in from Streamlit.
    """
    base = api_base()
    url = f"{base}/editor/?task={task_id}"

    if not _api_is_up(base):
        st.warning(tr("Timeline Editor Unavailable").format(base=base))
        return False

    # 高度写死而不是自适应：iframe 的内容高度跨源读不到,而编辑器本身是
    # 100vh 的 flex 布局,在固定高度的 iframe 里会填满它。这里是主界面，
    # 给够高度，否则时间线会被挤在一条缝里。
    components.iframe(url, height=820, scrolling=False)
    st.caption(tr("Timeline Editor Hint"))
    st.markdown(f"[{tr('Open Timeline Editor')}]({url})", unsafe_allow_html=False)
    return True


def _render_actions(task_id: str, described: dict, tr) -> None:
    """顶部的两个动作：重新配音、渲染成片。"""
    left, right = st.columns(2)
    with left:
        if st.button(
            tr("Rerecord Narration"),
            key=f"review_rerecord_{task_id}",
            use_container_width=True,
            disabled=not described["audio_stale"],
            help=tr("Rerecord Narration Help"),
        ):
            _submit(
                task_id,
                tr,
                prepare=scene_editor.prepare_audio_rerun,
                stop_at="subtitle",
                start_from="audio",
            )
    with right:
        if st.button(
            tr("Render Reviewed Video"),
            key=f"review_render_{task_id}",
            use_container_width=True,
            type="primary",
            disabled=described["audio_stale"],
        ):
            _submit(
                task_id,
                tr,
                prepare=scene_editor.prepare_render,
                stop_at="video",
                start_from="video",
            )


def _submit(task_id: str, tr, *, prepare, stop_at: str, start_from: str) -> None:
    """共用的提交路径：先准备参数，再交给既有的 WebUI 任务队列。

    准备失败就地显示原因并停住。素材被删、台词全空这类问题必须在排队之前
    说清楚,而不是让用户看着一个任务跑到一半再失败。
    """
    try:
        params = prepare(task_id)
    except scene_editor.SceneError as exc:
        st.error(str(exc))
        return

    try:
        webui_task.submit_generation(
            task_id=task_id,
            params=params,
            capture_logs=True,
            stop_at=stop_at,
            start_from=start_from,
        )
    except Exception as exc:
        logger.exception(f"failed to submit reviewed task {task_id}: {exc}")
        st.error(tr("Video Generation Failed"))
        return

    st.session_state["current_generation_task_id"] = task_id
    st.rerun()


@st.fragment
def _scene_row(task_id: str, scene: dict, total: int, tr) -> None:
    """一个场景一行。

    包成 fragment 是为了让"在关键词或台词框里打字"只重跑这一行，而不是整个
    9500 行的页面和另外十几个播放器。真正改动数据的操作走整页重跑
    (见 ``_run``)：fragment 重跑会沿用传进来的那一份 ``scene`` 快照，改完
    之后这一行仍会显示旧值。
    """
    index = scene["index"]
    with st.container(border=True):
        heading = tr("Scene Number").format(index=index + 1)
        # 成片里的起止时间。没有它，用户无法判断界面上这一段对应成片的哪一
        # 段——关键词个数和旁白句数不等时，"第 7 段画面"并不播在第 7 句上。
        window = _scene_window(scene)
        if window:
            heading = f"{heading} · {window}"
        if scene["locked"]:
            heading = f"{heading} · {tr('Scene Locked')}"
        st.write(heading)

        preview, controls = st.columns([1, 2])
        with preview:
            _render_preview(task_id, scene, tr)
        with controls:
            _render_controls(task_id, scene, total, tr)


def _scene_window(scene: dict) -> str:
    """这一段在成片里的起止时间，算不出来时返回空串。"""
    start, end = scene.get("starts_at"), scene.get("ends_at")
    if start is None or end is None:
        return ""
    return f"{_stamp(start)}–{_stamp(end)} ({end - start:.1f}s)"


def _stamp(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    return f"{total // 60}:{total % 60:02d}"


def _render_preview(task_id: str, scene: dict, tr) -> None:
    """显示这一段的画面。文件不在了就说清楚，不显示一个空白占位。"""
    if not scene["exists"]:
        st.error(tr("Scene Material Missing"))
        return

    try:
        path = scene_manifest.resolve_material(task_id, scene["material"])
    except scene_manifest.SceneError:
        st.error(tr("Scene Material Missing"))
        return

    if scene["kind"] == "video":
        st.video(path)
    elif scene["kind"] == "image":
        st.image(path, use_container_width=True)
    else:
        # 认不出的格式不猜，让用户看到文件名自己判断。
        st.caption(os.path.basename(path))


def _render_controls(task_id: str, scene: dict, total: int, tr) -> None:
    index = scene["index"]
    # 文生图那条路里这段文字就是发给模型的提示词原文，一句完整的画面描述
    # 塞不进单行输入框。用多行框显示，用户读到的就是真正生成这张图的那段
    # 文字，改完重生成也不会有"我改的和它用的不是一回事"。
    keyword = st.text_area(
        tr("Scene Keyword"),
        value=scene["keyword"],
        key=f"review_keyword_{task_id}_{index}",
        height=68,
        help=tr("Scene Keyword Help"),
    )
    narration = st.text_area(
        tr("Scene Narration"),
        value=scene["narration"],
        key=f"review_narration_{task_id}_{index}",
        height=80,
    )

    if keyword != scene["keyword"] or narration != scene["narration"]:
        if st.button(
            tr("Save Scene Text"),
            key=f"review_save_{task_id}_{index}",
            use_container_width=True,
        ):
            _run(
                tr,
                lambda: scene_editor.update_scene(
                    task_id, index, keyword=keyword, narration=narration
                ),
            )

    regenerate, lock, delete = st.columns(3)
    with regenerate:
        if st.button(
            tr("Regenerate Scene"),
            key=f"review_regen_{task_id}_{index}",
            use_container_width=True,
            help=tr("Regenerate Scene Help"),
        ):
            with st.spinner(tr("Regenerating Scene")):
                _run(
                    tr,
                    lambda: scene_editor.regenerate_material(
                        task_id, index, keyword=keyword or None
                    ),
                )
    with lock:
        if st.button(
            tr("Unlock Scene") if scene["locked"] else tr("Lock Scene"),
            key=f"review_lock_{task_id}_{index}",
            use_container_width=True,
        ):
            _run(
                tr,
                lambda: scene_editor.set_locked(task_id, index, not scene["locked"]),
            )
    with delete:
        if st.button(
            tr("Delete Scene"),
            key=f"review_delete_{task_id}_{index}",
            use_container_width=True,
        ):
            _run(tr, lambda: scene_editor.delete_scene(task_id, index))

    _render_reorder(task_id, index, total, tr)

    uploaded = st.file_uploader(
        tr("Replace Scene"),
        key=f"review_upload_{task_id}_{index}",
        type=["mp4", "mov", "mkv", "webm", "png", "jpg", "jpeg", "webp"],
        help=tr("Replace Scene Help"),
    )
    if uploaded is not None:
        if st.button(
            tr("Use Uploaded Material"),
            key=f"review_use_upload_{task_id}_{index}",
            use_container_width=True,
        ):
            _replace_from_upload(task_id, index, uploaded, tr)


def _render_reorder(task_id: str, index: int, total: int, tr) -> None:
    """上移/下移。``total`` 由调用方传入，避免每一行都去读一次清单。"""
    up, down = st.columns(2)
    with up:
        if st.button(
            tr("Move Scene Up"),
            key=f"review_up_{task_id}_{index}",
            use_container_width=True,
            disabled=index == 0,
        ):
            _run(tr, lambda: _swap(task_id, index, index - 1))
    with down:
        if st.button(
            tr("Move Scene Down"),
            key=f"review_down_{task_id}_{index}",
            use_container_width=True,
            disabled=index >= total - 1,
        ):
            _run(tr, lambda: _swap(task_id, index, index + 1))


def _swap(task_id: str, first: int, second: int):
    """重排前重新读一次清单：下标必须对应提交那一刻的真实场景数。"""
    order = list(range(len(scene_manifest.load(task_id)["scenes"])))
    if not (0 <= first < len(order) and 0 <= second < len(order)):
        raise scene_editor.SceneError(
            "the scene list changed; reopen the review and try again"
        )
    order[first], order[second] = order[second], order[first]
    return scene_editor.reorder_scenes(task_id, order)


def _replace_from_upload(task_id: str, index: int, uploaded, tr) -> None:
    """上传的文件先过既有的格式与内容校验，再写进场景。"""
    try:
        saved = material_upload.save_material_upload(uploaded.name, uploaded)
    except material_upload.MaterialUploadError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        logger.exception(f"failed to save replacement material: {exc}")
        st.error(tr("Video Generation Failed"))
        return

    _run(tr, lambda: scene_editor.replace_material(task_id, index, saved))


def _run(tr, operation) -> None:
    """执行一次场景编辑，并把失败原因原样显示出来。

    成功后整页重跑,不用 ``scope="fragment"``：fragment 重跑会沿用当初传进来
    的场景快照,这一行会继续显示改之前的值。而且删除和重排会改变其它行的
    下标,顶部的"配音已失效"提示也在 fragment 之外。

    错误不翻译成通用文案：供应商失败、素材越界、任务正忙,三者的处理方式
    完全不同,把它们都显示成"操作失败"等于让用户自己猜。
    """
    try:
        operation()
    except scene_editor.SceneBusyError as exc:
        st.warning(str(exc))
        return
    except scene_editor.SceneError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        logger.exception(f"unexpected scene edit failure: {exc}")
        st.error(f"{type(exc).__name__}: {exc}")
        return
    st.rerun()
