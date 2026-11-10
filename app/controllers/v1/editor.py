"""审片接口：渲染之前逐个场景检查、替换、重生成，确认后再提交渲染。

这些接口全部是新增的可选入口。``POST /videos`` 的行为完全没有改变：不调用
这里的任何一个接口，一次点击仍然从脚本一路跑到成片。

Every route here is additive and optional. ``POST /videos`` is unchanged: call
none of these and one click still runs from script to finished video.

两条贯穿全部接口的规则
----------------------
1. 任务正在跑的时候不能改场景。渲染正在读那些文件，边读边换会得到一段
   谁也无法复现的成片。``is_task_busy`` 已经是删除任务用的同一把判断。
2. 失败一律点名。某个素材源取不到图就说是哪个源失败了，不静默换源、也不
   静默跳过那个场景——审片的全部意义就是让用户看到真实结果。

Two rules run through all of them: scenes cannot be edited while the task is
running, because the render is reading those files; and a failure always names
what failed, with no silent provider substitution and no silently skipped
scene, since seeing the real result is the entire point of a review.
"""

from fastapi import Depends, Path, Request
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.controllers.v1.video import create_task, task_manager
from app.models.exception import HttpException
from app.models.schema import (
    NarrationScriptRequest,
    SceneListResponse,
    SceneAddRequest,
    SceneEffectsRequest,
    SceneRegenJobResponse,
    SceneRegenQueueResponse,
    SceneRegenerateRequest,
    SceneReorderRequest,
    SceneReplaceRequest,
    SceneResponse,
    SceneTimingRequest,
    SceneUpdateRequest,
    TaskResponse,
    TaskVideoRequest,
)
from app.controllers.manager.base_manager import TaskQueueFullError
from app.services import scene_editor, scene_manifest, scene_regen_queue
from app.services import state as sm
from app.services import task as tm
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


def _endpoint() -> str:
    return config.app.get("endpoint", "").rstrip("/")


def _require_task(task_id: str, request_id: str) -> dict:
    """确认这个任务存在。state 里没有时，以磁盘上的任务清单为准。

    为什么必须回落到清单：``enable_redis`` 关闭时 state 是**进程内**的。
    WebUI(Streamlit)和 API 是两个进程,所以在 WebUI 里生成的任务在 API 进程
    的 state 里根本不存在——只认 state 的话,审片接口和时间线对 WebUI 生成的
    每一个任务都会返回 404,整个功能等于不可用。

    而清单本来就是这个功能的事实来源(重启后仍能恢复渲染正是靠它),所以
    "磁盘上有场景清单"就是"这个任务存在"的充分证据。

    Task state is per-process while ``enable_redis`` is off, and the WebUI and
    the API are different processes, so a task created in the WebUI is absent
    from the API's state. The manifest is this feature's source of truth
    anyway, so its presence on disk is sufficient proof the task exists.
    """
    task = sm.state.get_task(task_id)
    if task:
        return task
    try:
        if scene_manifest.load(task_id)["scenes"]:
            # state 里没有，说明这个进程没在跑它；按空字典返回，
            # 让 ``_require_idle`` 自己再去判断忙碌状态。
            return {}
    except scene_manifest.SceneError:
        pass
    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


def _handle(task_id: str, request_id: str, operation):
    """把场景层的错误翻译成 HTTP 状态码。

    409 专门留给"任务还在跑"，因为那是唯一一个"等一下再试就会成功"的失败；
    其它都是 400，调用方改请求才有用。
    """
    try:
        return operation()
    except scene_editor.SceneBusyError as exc:
        logger.warning(f"refuse scene edit on busy task {task_id}: {exc}")
        raise HttpException(
            task_id=task_id, status_code=409, message=f"{request_id}: {exc}"
        ) from exc
    except scene_editor.SceneError as exc:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc


@router.post(
    "/materials",
    response_model=TaskResponse,
    summary="Generate everything except the video, ready for review",
)
def create_review_task(request: Request, body: TaskVideoRequest):
    """跑完脚本、配音、字幕和素材就停下，等审片后再调 ``/render``。

    这条路由是 ``POST /videos`` 的审片版本：同样的请求体，只是停在素材阶段。
    没有它的话，HTTP 调用方根本无法开始一次可审片的生成——只有 WebUI 能。
    """
    return create_task(request, body, stop_at="materials")


@router.get(
    "/tasks/{task_id}/scenes",
    response_model=SceneListResponse,
    summary="List the reviewable scenes of a task",
)
def list_scenes(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: scene_editor.describe(task_id, _endpoint())
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/scenes",
    response_model=SceneResponse,
    summary="Generate an extra scene and add it to the timeline",
)
def add_scene(
    request: Request,
    body: SceneAddRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """按关键词生成一段新画面并插进时间线，``after`` 不给就追加到末尾。

    素材张数由关键词个数决定，不由旁白长度决定，所以画面比旁白短很常见。
    "每段停得更久"(``/timeline/fit``)是一个答案，"多补几张画面"是另一个，
    这条路由提供后者。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    scene = _handle(
        task_id,
        request_id,
        lambda: scene_editor.add_scene(
            task_id,
            keyword=body.keyword,
            narration=body.narration or "",
            after=body.after,
            image_style=body.image_style,
        ),
    )
    return utils.get_response(200, _scene_payload(task_id, scene["index"]))


# 必须声明在 ``/scenes/{index}`` 之前：否则 "reorder" 会先去匹配那条带
# 整数参数的路由并直接返回 422，而不是落到这里。
# Declared before ``/scenes/{index}``: otherwise "reorder" matches that
# integer-typed route first and returns 422 instead of reaching this one.
@router.put(
    "/tasks/{task_id}/script",
    response_model=SceneListResponse,
    summary="Rewrite the whole narration script",
)
def rewrite_script(
    request: Request,
    body: NarrationScriptRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """改整条旁白的文字。

    改完配音就不作数了，要再按一次"重新配音"才会用上新词——这里不自动重配：
    重配要跑一次 TTS 并作废全部字幕时间，那应该是用户按下按钮的结果。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_editor.rewrite_script(task_id, body.script))
    data = _handle(
        task_id, request_id, lambda: scene_editor.describe(task_id, _endpoint())
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/scenes/reorder",
    response_model=SceneListResponse,
    summary="Reorder the scenes of a task",
)
def reorder_scenes(
    request: Request,
    body: SceneReorderRequest,
    task_id: str = Path(..., description="Task ID"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_editor.reorder_scenes(task_id, body.order))
    return utils.get_response(200, scene_editor.describe(task_id, _endpoint()))


#: 和 "reorder" 同一个道理，必须声明在 ``/scenes/{index}`` 那几条之前。
@router.get(
    "/tasks/{task_id}/scenes/queue",
    response_model=SceneRegenQueueResponse,
    summary="List the queued image regenerations of a task",
)
def list_regen_queue(request: Request, task_id: str = Path(..., description="Task ID")):
    """这个任务排着的、正在出的和刚结束的重生成。

    跑完的也要带上：界面靠它显示失败原因。一张图失败了而队列里已经没有它
    的记录，用户就只能看到画面没变，不知道为什么。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(200, scene_regen_queue.snapshot(task_id))


@router.delete(
    "/tasks/{task_id}/scenes/queue",
    response_model=SceneRegenQueueResponse,
    summary="Forget the finished image regenerations of a task",
)
def clear_regen_queue(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    """清掉已经结束的记录。排着的和正在出图的不动。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    scene_regen_queue.dismiss(task_id)
    return utils.get_response(200, scene_regen_queue.snapshot(task_id))


@router.delete(
    "/tasks/{task_id}/scenes/queue/{job_id}",
    response_model=SceneRegenJobResponse,
    summary="Cancel one queued image regeneration",
)
def cancel_regen_job(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    job_id: str = Path(..., description="Queued job id"),
):
    """撤掉一个还排着的活。已经在出图的那一张撤不掉（见队列模块的说明）。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    job = _handle(task_id, request_id, lambda: scene_regen_queue.cancel(job_id))
    return utils.get_response(200, job)


@router.post(
    "/tasks/{task_id}/scenes/{index}/material/queue",
    response_model=SceneRegenJobResponse,
    summary="Queue a regeneration of one scene's material",
)
def queue_scene_material(
    request: Request,
    body: SceneRegenerateRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """和下面那条接口做同一件事，但是立刻返回，出图排进队列。

    为什么要分成两条路由而不是改那一条：那条同步接口的调用方（CLI、脚本、
    现有的集成）要的就是"这个请求回来的时候图已经换好了"。把它改成异步会
    让那些调用方在图还没出来的时候就以为做完了。所以这里新增一条,原来那
    条一个字没动。

    请求体和同步那条完全一样,``keyword`` 和 ``image_style`` 的含义也一样。
    改请求才能解决的错误（任务在渲染、下标越界、风格 id 不认识）在这里就
    直接报出来,不会排进队列几分钟之后才冒出来。

    A second route rather than a change to the synchronous one: its callers
    expect the picture to be in place when the request returns, and making it
    asynchronous would have them believe the work was done. The body and the
    meaning of its fields are identical.
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    job = _handle(
        task_id,
        request_id,
        lambda: scene_regen_queue.enqueue(
            task_id, index, keyword=body.keyword, image_style=body.image_style
        ),
    )
    return utils.get_response(200, job)


@router.post(
    "/tasks/{task_id}/scenes/{index}/material",
    response_model=SceneResponse,
    summary="Regenerate the material of one scene",
)
def regenerate_scene_material(
    request: Request,
    body: SceneRegenerateRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(
        task_id,
        request_id,
        lambda: scene_editor.regenerate_material(
            task_id, index, keyword=body.keyword, image_style=body.image_style
        ),
    )
    return utils.get_response(200, _scene_payload(task_id, index))


@router.post(
    "/tasks/{task_id}/scenes/{index}/replace",
    response_model=SceneResponse,
    summary="Replace one scene with an uploaded material",
)
def replace_scene_material(
    request: Request,
    body: SceneReplaceRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(
        task_id,
        request_id,
        lambda: scene_editor.replace_material(task_id, index, body.material),
    )
    return utils.get_response(200, _scene_payload(task_id, index))


@router.put(
    "/tasks/{task_id}/scenes/{index}",
    response_model=SceneResponse,
    summary="Edit one scene's narration, keyword or lock",
)
def update_scene(
    request: Request,
    body: SceneUpdateRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(
        task_id,
        request_id,
        lambda: scene_editor.update_scene(
            task_id,
            index,
            narration=body.narration,
            keyword=body.keyword,
            locked=body.locked,
        ),
    )
    return utils.get_response(200, _scene_payload(task_id, index))


@router.put(
    "/tasks/{task_id}/scenes/{index}/effects",
    response_model=SceneResponse,
    summary="Set one scene's transition and picture motion",
)
def update_scene_effects(
    request: Request,
    body: SceneEffectsRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """改这一段的转场和运镜。两者都用空字符串表示"跟整条片子一样"。

    改运镜要从原图重烘一段（运镜是出图时烘进 mp4 的，不是渲染期的效果），
    所以这个请求可能要几秒钟；zoom/static 来回切换时第二次起直接命中磁盘上
    已有的文件。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(
        task_id,
        request_id,
        lambda: scene_editor.set_effects(
            task_id,
            index,
            transition=body.transition,
            image_motion=body.image_motion,
            cut_sound=body.cut_sound,
        ),
    )
    return utils.get_response(200, _scene_payload(task_id, index))


@router.get(
    "/tasks/{task_id}/timeline",
    response_model=SceneListResponse,
    summary="The scene list with source durations, for the timeline editor",
)
def timeline_state(request: Request, task_id: str = Path(..., description="Task ID")):
    """时间线界面的读模型：比 ``/scenes`` 多出每段素材的真实时长。

    源长度要逐个打开素材才能拿到，所以单独一条路由：Phase 1 的审片列表
    不需要它，不该为此多付十几次解码器启动。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: scene_editor.describe(task_id, _endpoint(), with_timing=True),
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/timeline/fit",
    response_model=SceneListResponse,
    summary="Stretch the scenes to fill the narration",
)
def fit_timeline(request: Request, task_id: str = Path(..., description="Task ID")):
    """把现有场景按比例拉长，铺满整段旁白。

    素材张数由关键词个数决定，不由旁白长度决定，所以"画面只有 24 秒、旁白
    有 64 秒"是常见结果。自动出片会循环播放这些片段来凑时长；时间线不替用户
    循环，这条路由则是另一种答案：同样的画面，每段停得更久。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_editor.fit_to_narration(task_id))
    # 重新 describe 一次，好让返回的地址带上配置的 endpoint，与其它路由一致。
    return utils.get_response(
        200, scene_editor.describe(task_id, _endpoint(), with_timing=True)
    )


@router.put(
    "/tasks/{task_id}/scenes/{index}/timing",
    response_model=SceneResponse,
    summary="Set one scene's in-point and on-screen duration",
)
def update_scene_timing(
    request: Request,
    body: SceneTimingRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """设置入点与停留时长，或用 ``reset`` 退回自动时长。

    超出素材长度不报错，而是按素材长度收住并在返回里带上 ``clamped``：
    用户在时间线上拖过了头是常事，为此失败一次毫无意义。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    scene = _handle(
        task_id,
        request_id,
        lambda: scene_editor.update_timing(
            task_id,
            index,
            trim_start=body.trim_start,
            duration=body.duration,
            reset=body.reset,
        ),
    )
    payload = _scene_payload(task_id, index)
    # clamped 只存在于这次调用的返回值里，清单上不会留这个字段。
    payload["clamped"] = bool(scene.get("clamped"))
    return utils.get_response(200, payload)


@router.delete(
    "/tasks/{task_id}/scenes/{index}",
    response_model=SceneListResponse,
    summary="Remove one scene from a task",
)
def delete_scene(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_editor.delete_scene(task_id, index))
    return utils.get_response(200, scene_editor.describe(task_id, _endpoint()))


@router.post(
    "/tasks/{task_id}/audio",
    response_model=TaskResponse,
    summary="Re-record the narration from the edited scene text",
)
def rerecord_audio(request: Request, task_id: str = Path(..., description="Task ID")):
    """用编辑后的台词重新配音并重新生成字幕。

    stop_at 固定为 "subtitle" 而不是 "audio"：换了配音，旧字幕的每一条时间
    都对不上了，只重配音会留下一个保证错位的字幕文件。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    params = _handle(task_id, request_id, lambda: scene_editor.prepare_audio_rerun(task_id))
    return _submit(task_id, request_id, params, stop_at="subtitle", start_from="audio")


@router.post(
    "/tasks/{task_id}/render",
    response_model=TaskResponse,
    summary="Render the reviewed scenes into the final video",
)
def render_task(request: Request, task_id: str = Path(..., description="Task ID")):
    """按审片后的场景顺序渲染成片，不重新生成任何素材。

    还有图排着队没出完的时候不开渲：渲染器正要去读那几个素材文件，而队列
    那边还在往清单里换文件。渲出来的片子会混着新旧两批画面,而且谁也说不清
    到底混进了哪几张。这里点名还剩几张,让用户自己决定是等还是撤。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    queue = scene_regen_queue.snapshot(task_id)
    if queue["busy"]:
        pending = queue["waiting"] + (1 if queue["running"] else 0)
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=(
                f"{request_id}: {pending} picture(s) are still being regenerated; "
                "wait for them to finish or cancel them before rendering"
            ),
        )
    params = _handle(task_id, request_id, lambda: scene_editor.prepare_render(task_id))
    return _submit(task_id, request_id, params, stop_at="video", start_from="video")


def _scene_payload(task_id: str, index: int) -> dict:
    scenes = scene_editor.describe(task_id, _endpoint())["scenes"]
    # 删除是独立接口，这里的下标在同一请求内不会失效；越界只可能是并发编辑。
    for scene in scenes:
        if scene["index"] == index:
            return scene
    raise HttpException(
        task_id=task_id,
        status_code=409,
        message="the scene list changed while this request was running",
    )


def _submit(
    task_id: str, request_id: str, params, *, stop_at: str, start_from: str
) -> dict:
    """把恢复任务交给既有的任务调度器。

    复用 ``task_manager`` 而不是直接起线程，是为了让审片触发的渲染和
    ``POST /videos`` 共用同一个并发上限：两条路径都会占满 GPU，各自计数
    等于没有上限。
    """
    try:
        task_manager.add_task(
            tm.start,
            task_id=task_id,
            params=params,
            stop_at=stop_at,
            start_from=start_from,
        )
    except TaskQueueFullError as exc:
        logger.warning(f"reject resumed task because queue is full: {task_id}")
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {exc}"
        ) from exc
    logger.success(
        f"task resumed: task_id={task_id}, start_from={start_from}, stop_at={stop_at}"
    )
    return utils.get_response(200, {"task_id": task_id})
