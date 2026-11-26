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

import os
from typing import Optional

from fastapi import Depends, File, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from loguru import logger
from starlette.background import BackgroundTask

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.controllers.v1.video import create_task, task_manager
from app.models.exception import HttpException
from app.models.schema import (
    BackgroundMusicRequest,
    BackgroundMusicResponse,
    CutSoundRequest,
    CacheCleanRequest,
    ConfirmRequest,
    LoggingOptionsRequest,
    NewVideoRequest,
    SettingsUpdateRequest,
    VoicePreviewRequest,
    RevealRequest,
    VideoEffectsRequest,
    SubtitleEditRequest,
    SubtitleStyleRequest,
    TitleCardRequest,
    NarrationScriptRequest,
    PublishMetadataRequest,
    PublishSceneThumbnailRequest,
    PublishTextRequest,
    PublishThumbnailRequest,
    PublishThumbnailTextRequest,
    ScenePromptSuggestRequest,
    ScenePromptSuggestResponse,
    SceneListResponse,
    SceneAddRequest,
    SceneEffectsRequest,
    SceneRegenJobResponse,
    SceneRegenQueueResponse,
    ScenePhotoPickRequest,
    SceneRegenerateRequest,
    SceneReorderRequest,
    SceneReplaceRequest,
    SceneResponse,
    SceneSplitRequest,
    SceneTimingRequest,
    SceneUpdateRequest,
    SceneVideoMarkRequest,
    TaskResponse,
    TaskVideoRequest,
)
from app.controllers.manager.base_manager import TaskQueueFullError
from app.services import bgm as bgm_service
from app.services import transition_sound as transition_sound_service
from app.services import app_settings
from app.services import direction_preview
from app.services import narration_retake
from app.services import title_cards
from app.services import new_video
from app.services import picture_shelf
from app.services import reference_image
from app.services import publish_metadata
from app.services import reveal
from app.services import scene_editor, scene_manifest, scene_regen_queue
from app.services import scene_video
from app.services import signin, upkeep
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
        manifest = scene_manifest.load(task_id)
        # 有场景就是"跑完素材、可以审片"的任务。
        #
        # 没有场景也可能是一条真实存在的任务：素材阶段半路死掉的那种。文生图
        # 一张一张买，二十九张之后连接被掐断——场景清单是在素材阶段全部成功
        # 之后才写的，所以那条任务盘上有脚本、有关键词、有配音、有二十九段
        # 已经付过钱的画面，却一个场景都没有。以前它在这里就是 404,"接着把
        # 画面出完"这件事连门都进不去。
        #
        # 判据退到"这个任务跑到过脚本阶段"，和列表页认任务的口径一致。
        #
        # A task with no scenes can still be a real one: materials dying
        # halfway leaves the script, the keywords, the narration and the
        # pictures already paid for on disk, but no scene list, because that
        # is only written once materials fully succeed. Such a task used to
        # 404 on every editor route, so there was no way in to finish it.
        if manifest["scenes"] or manifest["script"]:
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
    "/new-video/options",
    summary="The editor's New video form: every field, its choices and defaults",
)
def new_video_options(request: Request):
    """表单长什么样、每一项能挑什么、默认是什么。"""
    base.get_task_id(request)
    return utils.get_response(200, new_video.form())


@router.get(
    "/new-video/voices",
    summary="The voices of one voice service, grouped by language",
)
def new_video_voices(
    request: Request,
    server: str = Query(..., max_length=40),
    language: str = Query("", max_length=20),
    keep: str = Query("", max_length=300),
):
    request_id = base.get_task_id(request)
    try:
        data = new_video.voices(server, prefer=language, keep=keep)
    except new_video.NewVideoError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    return utils.get_response(200, data)


@router.post(
    "/new-video/voice-preview",
    summary="Hear a voice read a short sample",
)
def new_video_voice_preview(request: Request, body: VoicePreviewRequest):
    request_id = base.get_task_id(request)
    try:
        path = new_video.preview_voice(
            body.voice_name, body.voice_rate, body.voice_volume, body.text
        )
    except new_video.NewVideoError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    # 播完就删：试听不是任务产物，不该在 temp 里越攒越多。
    return FileResponse(
        path, media_type="audio/mpeg", filename="voice-preview.mp3",
        background=BackgroundTask(_remove_quietly, path),
    )


@router.post(
    "/new-video/audio",
    summary="Upload your own narration for a new video",
)
def new_video_audio(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        token = new_video.stage_audio(file.filename or "", file.file)
    except new_video.NewVideoError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    return utils.get_response(200, {"file": token, "name": file.filename or token})


@router.post(
    "/new-video/script",
    summary="Write the script for a new video, to read before starting it",
)
def write_new_video_script(request: Request, body: NewVideoRequest):
    request_id = base.get_task_id(request)
    try:
        script = new_video.write_script(body.model_dump())
    except new_video.NewVideoError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    return utils.get_response(200, {"video_script": script})


@router.post(
    "/new-video",
    response_model=TaskResponse,
    summary="Start a video from the editor; it stops for review, never renders",
)
def start_new_video(request: Request, body: NewVideoRequest):
    """从编辑器开一条片子。

    和 ``POST /materials`` 走同一条流水线，区别只在请求体：这里收的是表单
    那些项，其余用存下来的设置补齐。文生图停在字幕（先看提示词），别的
    停在素材——成片永远由编辑器里的"渲染"决定。
    """
    request_id = base.get_task_id(request)
    # A new video draws its pictures; that has to wait for video scenes.
    _handle("", request_id, scene_video.refuse_while_running)
    choices = body.model_dump()
    task_id = utils.get_uuid()
    try:
        params = new_video.build_params(choices)
        if choices.get("voice_mode") == new_video.VOICE_UPLOAD:
            params.custom_audio_file = new_video.place_audio(
                task_id, choices.get("custom_audio") or ""
            )
    except new_video.NewVideoError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    response = create_task(
        request, params,
        stop_at=new_video.stop_stage(params, choices.get("review_prompts")),
        task_id=task_id,
    )
    # 开成了才记：没开成的那一次不算"上次用的"。
    new_video.remember(choices, params)
    return response


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.get(
    "/settings",
    summary="Provider addresses, keys and models; keys are never sent back",
)
def read_settings(request: Request):
    base.get_task_id(request)
    return utils.get_response(200, app_settings.describe())


@router.put(
    "/settings",
    summary="Save provider settings to config.toml",
)
def save_settings(request: Request, body: SettingsUpdateRequest):
    request_id = base.get_task_id(request)
    try:
        data = app_settings.apply(body.changes, body.clear)
    except app_settings.SettingsError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/scenes/{index}/split",
    summary="Split one scene in two at a point in time",
)
def split_scene(
    request: Request,
    body: SceneSplitRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id,
            lambda: scene_editor.split_scene(task_id, index, body.at))
    return utils.get_response(200, {"selected": index + 1})


@router.post(
    "/tasks/{task_id}/scenes/{index}/duplicate",
    summary="Put a copy of one scene right after it",
)
def duplicate_scene(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id,
            lambda: scene_editor.duplicate_scene(task_id, index))
    return utils.get_response(200, {"selected": index + 1})


@router.get(
    "/tasks/{task_id}/history",
    summary="How many timeline edits can be undone and redone",
)
def read_history(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(200, scene_editor.history_state(task_id))


@router.post(
    "/tasks/{task_id}/history/{direction}",
    summary="Undo or redo the last timeline edit",
)
def step_history(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    direction: str = Path(..., max_length=10),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    if direction not in ("undo", "redo"):
        raise HttpException(
            task_id=task_id, status_code=404,
            message=f"{request_id}: unknown history step {direction!r}",
        )
    step = scene_editor.undo if direction == "undo" else scene_editor.redo
    data = _handle(task_id, request_id, lambda: step(task_id))
    return utils.get_response(
        200, {"label": data["label"], "history": data["history"]}
    )


@router.get(
    "/settings/signin/{provider}",
    summary="Whether an account-based provider is connected",
)
def signin_status(request: Request, provider: str = Path(..., max_length=40)):
    request_id = base.get_task_id(request)
    return utils.get_response(
        200, _signin(request_id, lambda: signin.status(provider))
    )


@router.post(
    "/settings/signin/{provider}/{step}",
    summary="Begin, poll, cancel or disconnect an account sign-in",
)
def signin_step(
    request: Request,
    provider: str = Path(..., max_length=40),
    step: str = Path(..., max_length=20),
):
    request_id = base.get_task_id(request)
    steps = {"begin": signin.begin, "poll": signin.poll,
             "cancel": signin.cancel, "disconnect": signin.disconnect}
    if step not in steps:
        raise HttpException(
            task_id=request_id, status_code=404,
            message=f"{request_id}: unknown sign-in step {step!r}",
        )
    return utils.get_response(
        200, _signin(request_id, lambda: steps[step](provider))
    )


def _signin(request_id: str, operation):
    try:
        return operation()
    except signin.SignInError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc


def _upkeep(request_id: str, operation):
    try:
        return operation()
    except upkeep.UpkeepError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc


@router.get(
    "/settings/upkeep",
    summary="The material cache size and the logging options",
)
def read_upkeep(request: Request):
    base.get_task_id(request)
    return utils.get_response(200, upkeep.describe())


@router.get(
    "/settings/cache",
    summary="What a cache clean-up of this range would delete; deletes nothing",
)
def preview_cache(request: Request, days: Optional[int] = Query(None)):
    request_id = base.get_task_id(request)
    return utils.get_response(
        200, _upkeep(request_id, lambda: upkeep.cache_stats(days))
    )


@router.post(
    "/settings/cache/clean",
    summary="Delete downloaded stock material older than a number of days",
)
def clean_cache(request: Request, body: CacheCleanRequest):
    request_id = base.get_task_id(request)
    return utils.get_response(
        200, _upkeep(request_id, lambda: upkeep.clean_cache(body.days, body.confirm))
    )


@router.put(
    "/settings/logging",
    summary="Change the log level; takes effect at once",
)
def set_logging(request: Request, body: LoggingOptionsRequest):
    request_id = base.get_task_id(request)
    return utils.get_response(
        200,
        _upkeep(request_id, lambda: upkeep.set_logging(body.level, body.log_requests)),
    )


@router.post(
    "/settings/keys/export",
    summary="Download every saved key as a backup file; must be confirmed",
)
def export_keys(request: Request, body: ConfirmRequest):
    """密钥备份。设置页别的接口都不回传 Key，这一条是唯一的例外，所以它
    不是 GET，而且要请求体里明说 ``confirm``。"""
    request_id = base.get_task_id(request)
    if body.confirm is not True:
        raise HttpException(
            task_id=request_id, status_code=400,
            message=f"{request_id}: confirm that the keys may be written to a file",
        )
    logger.warning("the saved keys were exported to a backup file from the editor")
    return utils.get_response(200, {
        "filename": upkeep.KEY_BACKUP_FILE_NAME, "payload": upkeep.key_backup(),
    })


@router.post(
    "/settings/keys/restore",
    summary="Restore keys from a backup file",
)
def restore_keys(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    raw = file.file.read(upkeep.MAX_TRANSFER_BYTES + 1)
    return utils.get_response(
        200, _upkeep(request_id, lambda: upkeep.restore_keys(raw))
    )


@router.post(
    "/settings/preset/export",
    summary="Download the last-used video settings as a preset file",
)
def export_preset(request: Request):
    base.get_task_id(request)
    return utils.get_response(200, {
        "filename": upkeep.SETTINGS_PRESET_FILE_NAME,
        "payload": upkeep.settings_preset(),
    })


@router.post(
    "/settings/preset/import",
    summary="Load a settings preset; the next new video starts from it",
)
def import_preset(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    raw = file.file.read(upkeep.MAX_TRANSFER_BYTES + 1)
    return utils.get_response(
        200, _upkeep(request_id, lambda: upkeep.apply_preset(raw))
    )


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
@router.get(
    "/tasks/{task_id}/subtitles/style",
    summary="How the captions look, and the fonts available",
)
def read_subtitle_style(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(task_id, request_id, lambda: scene_editor.subtitle_style(task_id))
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/subtitles/style",
    summary="Set the caption font, size, colour, outline and position",
)
def update_subtitle_style(
    request: Request,
    body: SubtitleStyleRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """改字幕的样子。只影响这一条片子，不动字幕文件，也不动配音。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: scene_editor.set_subtitle_style(
            task_id, **body.model_dump(exclude_none=True)
        ),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/effects",
    summary="The transition and motion used by the whole video",
)
def read_video_effects(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(task_id, request_id, lambda: scene_editor.video_effects(task_id))
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/effects",
    summary="Change the transition for the whole video at once",
)
def update_video_effects(
    request: Request,
    body: VideoEffectsRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """一次改掉整条片子的转场。

    ``apply_to_scenes`` 打开时顺手清掉每一段自己挑过的那个，否则改完之后
    那几段仍然我行我素。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: scene_editor.set_video_effects(
            task_id,
            transition=body.transition,
            image_motion=body.image_motion,
            apply_to_scenes=body.apply_to_scenes,
            finish_look=body.finish_look,
        ),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/cut-sound",
    summary="The sound played at every cut, and what else is available",
)
def read_cut_sound(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(task_id, request_id, lambda: scene_editor.cut_sound_settings(task_id))
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/cut-sound",
    summary="Change the sound at every cut, or turn it off entirely",
)
def update_cut_sound(
    request: Request,
    body: CutSoundRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """换整条片子的转场音效，或者把它整个关掉（``sound`` 给空字符串）。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: scene_editor.set_cut_sound(
            task_id, sound=body.sound, volume=body.volume
        ),
    )
    return utils.get_response(200, data)



# ---------------------------------------------------------------------------
# 发到 YouTube 之前要准备的那三样：标题、简介、封面。
#
# 和 ``/subtitles/style``、``/effects`` 一样，这几条也必须声明在
# ``/scenes/{index}`` 之前——不过它们在 ``/publish`` 下，和那条路由不冲突。
#
# 这几条不检查"任务是不是在跑"。``scene_editor`` 的编辑操作必须拒绝，因为
# 渲染器正在读那些素材文件；而文案和封面都不是渲染器的输入，写文案恰恰是
# 等渲染的那几分钟里最自然的事。
# ---------------------------------------------------------------------------


@router.get(
    "/tasks/{task_id}/publish",
    summary="The YouTube title, description, tags and thumbnail of this video",
)
def read_publish_metadata(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    """已经准备好的发布信息，连这条片子的画幅一起。

    画幅无条件带上：封面的形状由它决定，所以界面要在用户按下"生成封面"之前
    就说清楚会得到一张竖图还是横图。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: publish_metadata.describe(task_id, _endpoint())
    )
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/publish",
    summary="Save the YouTube title, description, tags and thumbnail brief",
)
def update_publish_metadata(
    request: Request,
    body: PublishMetadataRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """存下用户改过的文案。超长报 400，不悄悄截断。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: publish_metadata.set_metadata(
            task_id,
            title=body.title,
            description=body.description,
            tags=body.tags,
            thumbnail_prompt=body.thumbnail_prompt,
            endpoint=_endpoint(),
        ),
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/publish/text",
    summary="Write the YouTube title, description and tags for this video",
)
def generate_publish_text(
    request: Request,
    body: PublishTextRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """按这条片子的画幅写一版文案，顺带写出封面要画什么。

    竖版进 Shorts，横版进普通视频页，两者该写的东西不一样——所以画幅是
    模型的输入，不是事后的一个细节。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: publish_metadata.generate_text(
            task_id, language=body.language or "auto", endpoint=_endpoint()
        ),
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/publish/thumbnail",
    summary="Draw the YouTube thumbnail at this video's aspect ratio",
)
def generate_publish_thumbnail(
    request: Request,
    body: PublishThumbnailRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """画一张封面，尺寸由这条片子的画幅决定。

    调用方不能指定尺寸：9:16 的片子配一张横封面，传到 YouTube 会被两边
    裁掉，而主体通常正在那里。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: publish_metadata.generate_thumbnail(
            task_id, prompt=body.prompt, endpoint=_endpoint()
        ),
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/publish/thumbnail/scene",
    summary="Use one of the video's own pictures as the thumbnail",
)
def use_scene_as_thumbnail(
    request: Request,
    body: PublishSceneThumbnailRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """拿第 ``index`` 段画面当封面，裁到这条片子的画幅。

    封面本该是片子里的一个画面，所以这条路由是常走的那一条：那些图已经在
    磁盘上了，不用再跑一次文生图，也不会出现一张和成片对不上的封面。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: publish_metadata.use_scene(
            task_id, body.index, at=body.at, endpoint=_endpoint()
        ),
    )
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/publish/thumbnail/text",
    summary="Put words on the thumbnail",
)
def set_thumbnail_text(
    request: Request,
    body: PublishThumbnailTextRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """改压在封面上的那行字，并立刻重新合成成品。

    只重画文字，不碰那张图：改一个词不该重跑一次文生图，也不该重新从片段里
    取一次帧。反过来，换一张图也会保留这几个字。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: publish_metadata.set_text(
            task_id,
            body.model_dump(exclude_none=True),
            endpoint=_endpoint(),
        ),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/publish/thumbnail",
    summary="Download the thumbnail file",
)
def download_publish_thumbnail(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    """把封面作为附件交出去，带一个认得出来的文件名。

    ``/stream`` 也能取到这个文件，但它是给 ``<img>`` 用的：浏览器会直接显示
    出来，另存时文件名是 ``youtube-thumbnail.png``,和是哪条片子无关。上传
    封面时要在文件选择框里认出它,所以这里带上任务 id。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    resolved = _handle(
        task_id, request_id, lambda: publish_metadata.thumbnail_file(task_id)
    )
    return FileResponse(
        resolved,
        media_type="image/png",
        filename=f"thumbnail-{task_id}.png",
    )


@router.get("/library", summary="Every task on disk, newest first")
def list_library(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
):
    """做过的片子，用来在编辑器里直接挑一条接着改。

    不用 ``GET /tasks``：那一份是进程内存里的任务表，API 一重启就空了，而
    "我做过哪些片子"的真正记录在磁盘上。
    """
    base.get_task_id(request)
    data = scene_editor.library(_endpoint(), page=page, page_size=page_size)
    return utils.get_response(200, data)


@router.delete(
    "/library/{task_id}",
    summary="Delete one video and everything made for it, permanently",
)
def delete_library_video(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
):
    """永久删除一条片子。以磁盘为准，所以 API 重启之前做的片子也删得掉
    （``DELETE /tasks/{id}`` 只认内存里的任务表）。正在跑的返回 409。"""
    request_id = base.get_task_id(request)
    data = _handle(
        task_id, request_id, lambda: scene_editor.delete_video(task_id)
    )
    return utils.get_response(200, data)


@router.get("/cut-sound/{name}", summary="Play one transition sound")
def stream_cut_sound(request: Request, name: str = Path(..., description="Sound file name")):
    """让编辑器能先听一下那一声再决定用不用。

    和背景音乐那个口子一个道理：``/stream`` 只认任务目录，而音效库在别处。
    解析仍然走 ``transition_sound.resolve_transition_sound`` 那张白名单。
    """
    request_id = base.get_task_id(request)
    try:
        resolved = transition_sound_service.resolve_transition_sound(name)
    except ValueError as exc:
        raise HttpException(
            task_id="", status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    if not os.path.isfile(resolved):
        raise HttpException(
            task_id="", status_code=404, message=f"{request_id}: no such sound file"
        )
    return FileResponse(resolved)


@router.get("/fonts/{name:path}", summary="Serve one subtitle font to the editor")
def stream_font(request: Request, name: str = Path(..., description="Font file name")):
    """把字体文件交给编辑器，让预览用真正要烧进成片的那一款字。

    只认字体目录里列得出来的那几个名字,别的一律 400——这个口子不能变成
    "按路径下载服务器上任意文件"。
    """
    request_id = base.get_task_id(request)
    if name not in scene_editor.available_fonts():
        raise HttpException(
            task_id="", status_code=400, message=f"{request_id}: unknown font"
        )
    resolved = os.path.join(utils.font_dir(), *name.split("/"))
    if not os.path.isfile(resolved):
        raise HttpException(
            task_id="", status_code=404, message=f"{request_id}: no such font file"
        )
    return FileResponse(resolved)


@router.put(
    "/tasks/{task_id}/subtitles/{index}",
    summary="Edit one caption without re-recording",
)
def update_subtitle(
    request: Request,
    body: SubtitleEditRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Caption index"),
):
    """改屏幕上那行字，不碰配音，也不碰时间轴。

    听写把一个词认错时要改的就是这里：为一个字重跑一遍 TTS 并作废全部字幕
    时间点，代价完全不成比例。想改**念出来**的词请走 ``PUT /script``。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: scene_editor.edit_subtitle(task_id, index, body.text)
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/music",
    response_model=BackgroundMusicResponse,
    summary="The background music of this video, and what else is available",
)
def read_background_music(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: scene_editor.background_music(task_id)
    )
    return utils.get_response(200, data)


@router.post(
    "/music",
    summary="Upload a song to the music library from the editor",
)
def upload_background_music(request: Request, file: UploadFile = File(...)):
    """往曲库里加一首。加进去的是整个曲库的，不只是这一条片子的：传完它出现
    在每条片子的下拉框里，选不选、存不存仍然是 ``PUT /tasks/{id}/music`` 的事。

    存、验都走 ``bgm.save_bgm_upload``（和 ``POST /musics`` 同一条路）；这里多
    做的一件事是记下原来的文件名——存下来的是一串 UUID，下拉框里认不出。
    """
    request_id = base.get_task_id(request)
    try:
        stored = bgm_service.save_bgm_upload(
            file.filename, file.file,
            max_bytes=bgm_service.MAX_EDITOR_BGM_UPLOAD_BYTES,
            validate_seconds=bgm_service._EDITOR_BGM_VALIDATE_SECONDS,
        )
    except bgm_service.BgmUploadError as exc:
        raise HttpException(
            task_id=request_id, status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    except bgm_service.BgmServiceError as exc:
        logger.error(
            f"song upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id, status_code=500,
            message=f"{request_id}: the song could not be checked or stored",
        ) from exc
    name = bgm_service.remember_uploaded_bgm_name(stored, file.filename or "")
    return utils.get_response(200, {"file": stored, "name": name})


@router.get(
    "/music/{name}",
    summary="Play one background music file",
)
def stream_background_music(request: Request, name: str = Path(..., description="File name")):
    """让编辑器能先听一下再选。

    ``/stream`` 那条路由只认任务目录里的文件，而曲库在 resource/songs 和
    storage/bgm，所以这里单开一个口子——解析仍然走 ``bgm.resolve_bgm_file``
    那张白名单,它只认这两个目录。
    """
    request_id = base.get_task_id(request)
    try:
        resolved = bgm_service.resolve_bgm_file(name)
    except ValueError as exc:
        raise HttpException(
            task_id="", status_code=400, message=f"{request_id}: {exc}"
        ) from exc
    if not os.path.isfile(resolved):
        raise HttpException(
            task_id="", status_code=404, message=f"{request_id}: no such music file"
        )
    return FileResponse(resolved)


@router.get(
    "/tasks/{task_id}/direction",
    summary="What the finished video adds over the preview, and when",
)
def read_direction(request: Request, task_id: str = Path(..., description="Task ID")):
    """片名、标题卡、重音、环境声、配乐让开——成片里才有的那些，各在第几秒。

    编辑器照着它在预览上画、放。位置要靠把配音听一遍才准，那要几分钟：没听
    过的时候这里答 ``listening`` 并在后台去听，编辑器过一会儿再问。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: direction_preview.describe(task_id)
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/direction/cards",
    summary="Add a title card on the words said at a time",
)
def add_title_card(
    request: Request,
    body: TitleCardRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """The card is tied to the words the narration says at ``time``, so it
    stays on them when the narration around it changes. Answers with the
    direction as the editor shows it."""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id,
        lambda: title_cards.add(task_id, body.time, body.title, body.subtitle),
    )
    return utils.get_response(200, data)


@router.put(
    "/tasks/{task_id}/direction/cards",
    summary="Reword a title card, or move it",
)
def change_title_card(
    request: Request,
    body: TitleCardRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """``start`` says which card (when it starts, in seconds). ``title`` and
    ``subtitle`` reword it; ``time`` moves it onto the words said then."""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id,
        lambda: title_cards.change(
            task_id, body.start, title=body.title, subtitle=body.subtitle,
            time=body.time),
    )
    return utils.get_response(200, data)


@router.delete(
    "/tasks/{task_id}/direction/cards",
    summary="Take a title card out",
)
def delete_title_card(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    start: float = Query(..., ge=0, description="When the card starts, in seconds"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: title_cards.remove(task_id, start)
    )
    return utils.get_response(200, data)


@router.get(
    "/direction/sounds/{name}",
    summary="One of the synthesised sounds the finishing pass mixes in",
)
def stream_direction_sound(request: Request, name: str = Path(..., description="hit, wind or room")):
    """重音和两种环境声。合成出来的，和成片里混进去的是同一份波形。"""
    request_id = base.get_task_id(request)
    try:
        resolved = direction_preview.sound_file(name)
    except ValueError as exc:
        raise HttpException(
            task_id="", status_code=404, message=f"{request_id}: {exc}"
        ) from exc
    return FileResponse(resolved, media_type="audio/wav")


@router.put(
    "/tasks/{task_id}/music",
    response_model=BackgroundMusicResponse,
    summary="Choose the background music and its volume",
)
def update_background_music(
    request: Request,
    body: BackgroundMusicRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """换这条片子的配乐。空的 ``track`` 是不要音乐，``random`` 是随机挑。

    改的是这条任务自己的参数，不是全局设置，所以别的片子不受影响。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id,
        request_id,
        lambda: scene_editor.set_background_music(
            task_id, track=body.track, volume=body.volume
        ),
    )
    return utils.get_response(200, data)


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
@router.get("/video-scenes", summary="The batch of video scenes, running or last finished")
def read_video_scenes(request: Request):
    """Not under a task: one batch runs at a time on this machine, whichever
    video it is for, and every editor window has to be able to see it."""
    return utils.get_response(200, scene_video.snapshot())


@router.delete("/video-scenes", summary="Stop the running batch, or forget a finished one")
def end_video_scenes(request: Request):
    request_id = base.get_task_id(request)
    if scene_video.running():
        return utils.get_response(200, _handle("", request_id, scene_video.stop))
    return utils.get_response(200, scene_video.dismiss())


@router.post(
    "/tasks/{task_id}/video-scenes",
    summary="Make a video clip for every scene marked for one",
)
def start_video_scenes(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    everything: bool = Query(
        False, description="Animate every still picture, not only the marked scenes"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(
        200,
        _handle(task_id, request_id, lambda: scene_video.start(task_id, everything=everything)),
    )


@router.delete(
    "/tasks/{task_id}/video-scenes",
    summary="Take this video out of the video scene queue, or forget its ended batch",
)
def leave_video_scenes(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    """Never stops the batch that is running: that is ``DELETE /video-scenes``."""
    return utils.get_response(200, scene_video.forget(task_id))


@router.get(
    "/tasks/{task_id}/video-scenes/pictures",
    summary="Which scenes animating every picture would animate",
)
def read_video_pictures(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(
        200, _handle(task_id, request_id, lambda: scene_video.pictures(task_id))
    )


@router.put(
    "/tasks/{task_id}/scenes/{index}/video",
    response_model=SceneResponse,
    summary="Mark a scene to become a video clip, or take the mark off",
)
def mark_scene_video(
    request: Request,
    body: SceneVideoMarkRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_video.mark(task_id, index, body.mode))
    return utils.get_response(200, _scene_payload(task_id, index))


@router.post(
    "/tasks/{task_id}/scenes/draw",
    summary="Approve the prompts and draw every picture still missing",
)
def draw_pending_scenes(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    """用户在提示词那一屏点的"开画"。

    路由放在 ``/scenes/{index}`` 之前：``draw`` 会被那条带下标的路由吃掉。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(
        200, _handle(task_id, request_id, lambda: scene_regen_queue.enqueue_pending(task_id))
    )


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
    "/tasks/{task_id}/scenes/{index}/prompt",
    response_model=ScenePromptSuggestResponse,
    summary="Write this scene's picture prompt from a short description",
)
def suggest_scene_prompt(
    request: Request,
    body: ScenePromptSuggestRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """用户写一句短话，模型把它补成这一段完整的画面提示词。

    手写那条路一个字没动：``PUT /scenes/{index}`` 仍然把框里的内容原样存下
    去，出图时也仍然原样使用。这里只是多一条路——要记着"画面里得有人在做
    事、情绪落在脸上、年代地点每条都重写、不能出现文字"这十来条规矩才能写
    好一条提示词，而用户敲进去的往往只有"让她在笑"。

    **不写盘。** 返回的是草稿，存不存由调用方决定：模型写歪的时候，一次点击
    不该已经把用户手写的那条覆盖掉。

    任务正在渲染时也可以调——它只是读旁白和参数，不碰任何文件。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    prompt = _handle(
        task_id,
        request_id,
        lambda: scene_editor.suggest_image_prompt(
            task_id, index, direction=body.direction or "", current=body.current or ""
        ),
    )
    return utils.get_response(200, {"prompt": prompt})


@router.post(
    "/tasks/{task_id}/scenes/{index}/prompt/queue",
    response_model=SceneRegenJobResponse,
    summary="Queue the model writing this scene's picture prompt",
)
def queue_scene_prompt(
    request: Request,
    body: ScenePromptSuggestRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """排队写提示词，而不是当场写。

    为什么排队：写提示词用的是本机那个语言模型，出图用的是本机那块显卡，
    是同一块。一边在出图、一边让 ollama 把 14B 装进显存，轻则两边都慢，
    重则谁都装不下——这台机器上两件事同时发生过，代价是整条队列都卡住。

    写完之后这一段的出图会**接着**排进同一条队，所以"写词 → 出图"是两个
    看得见的活，而不是一次说不清在干什么的等待。

    同步那条路（``POST .../prompt``）一个字没动：它不写盘，调用方自己决定
    要不要留下来。界面走的是这一条。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    job = _handle(
        task_id,
        request_id,
        lambda: scene_regen_queue.enqueue_prompt(
            task_id,
            index,
            direction=str(body.direction or ""),
            current=str(body.current or ""),
        ),
    )
    return utils.get_response(200, job)


@router.get(
    "/tasks/{task_id}/scenes/{index}/photos",
    summary="Search for photographs to redraw one scene from; nothing is drawn",
)
def search_scene_photos(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
    search: str = Query("", max_length=200),
):
    """把搜到的照片列出来给人挑。``search`` 空着就由模型来写搜索词。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id,
        lambda: scene_editor.find_reference_photos(task_id, index, search=search),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/scenes/{index}/photos/words",
    summary="The words to search photographs with for one scene; searches nothing",
)
def scene_photo_words(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id,
        lambda: scene_editor.reference_photo_words(task_id, index),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/photos/candidates",
    summary="Which scenes name something real that a photograph exists of",
)
def scene_photo_candidates(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: scene_editor.photo_candidates(task_id)
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/photos/uploaded/{token}",
    summary="A photograph that was just uploaded or chosen, to look at",
)
def uploaded_scene_photo(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    token: str = Path(..., max_length=80),
):
    """刚传上来（或者刚在 Google 里点中）的那一张：窗里要把它摆出来。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    photo = _handle(
        task_id, request_id, lambda: scene_editor.uploaded_reference_photo(token)
    )
    return Response(content=photo.data, media_type="image/png")


@router.get(
    "/tasks/{task_id}/photos/used/{name}",
    summary="The photograph a scene was drawn from",
)
def used_scene_photo(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    name: str = Path(..., max_length=40),
):
    """照着画的那张照片本身，存在任务目录里的那一份。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    path = _handle(
        task_id, request_id, lambda: scene_editor.used_photo_path(task_id, name)
    )
    return FileResponse(path, media_type="image/png")


@router.post(
    "/tasks/{task_id}/scenes/{index}/photos/browse",
    summary="Open a small browser window on Google Images to click a photograph in",
)
def browse_scene_photos(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
    search: str = Query("", max_length=200),
):
    """在跑着这个程序的电脑上开一个浏览器窗口。用户点中的那一张之后用
    ``photo_upload`` 来排重画，和自己传的一样。不用任何搜索 API。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id,
        lambda: scene_editor.browse_for_photo(task_id, index, search=search),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/photos/browse",
    summary="What happened in the photo browser window so far",
)
def scene_photo_browser(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(200, scene_editor.photo_browser_state(task_id))


@router.delete(
    "/tasks/{task_id}/photos/browse",
    summary="Close the photo browser window",
)
def close_scene_photo_browser(
    request: Request, task_id: str = Path(..., description="Task ID")
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    return utils.get_response(200, scene_editor.close_photo_browser(task_id))


@router.post(
    "/tasks/{task_id}/photos",
    summary="Upload a photograph to redraw a scene from",
)
def upload_scene_photo(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    file: UploadFile = File(...),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    raw = file.file.read(reference_image.MAX_BYTES + 1)
    data = _handle(
        task_id, request_id,
        lambda: scene_editor.keep_uploaded_photo(raw, file.filename or ""),
    )
    return utils.get_response(200, data)


@router.get(
    "/tasks/{task_id}/scenes/{index}/photo-pick",
    summary="The photograph saved for a scene before it was drawn",
)
def scene_photo_pick(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    path = _handle(
        task_id, request_id, lambda: scene_editor.picked_photo_path(task_id, index)
    )
    return FileResponse(path, media_type="image/png")


@router.post(
    "/tasks/{task_id}/scenes/{index}/photo-pick",
    summary="Keep the photo chosen for a scene; nothing is drawn yet",
)
def keep_scene_photo_pick(
    request: Request,
    body: ScenePhotoPickRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """开画之前给一段定下照片。照片现在就下载、验过、存进这条片子里：
    用不了的话当场说，而不是十几张都挑完了才说。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)

    def keep():
        photo = None
        if not body.skip:
            if body.photo_upload:
                photo = scene_editor.uploaded_reference_photo(body.photo_upload)
            elif body.photo_kept:
                photo = scene_editor.kept_reference_photo(task_id, body.photo_kept)
            elif body.photo_url:
                photo = scene_editor.chosen_reference_photo(task_id, body.photo_url)
        if body.frame is not None:
            if photo is None:
                raise scene_editor.SceneError(
                    "a size and position were given, but no photo was chosen "
                    "to place"
                )
            photo = scene_editor.framed_photo(task_id, photo, body.frame.model_dump())
        return scene_editor.keep_photo_pick(
            task_id, index,
            photo=photo, skip=body.skip, search=str(body.search or ""),
            strength=body.strength, follow=str(body.follow or ""),
        )

    return utils.get_response(200, _handle(task_id, request_id, keep))


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

    def chosen_photo():
        # 挑好的那张在排队之前就下载、验过：这张用不了的话现在就说，而不是
        # 排三分钟队之后才说。
        if body.photo_upload:
            return scene_editor.uploaded_reference_photo(body.photo_upload)
        if body.photo_kept:
            return scene_editor.kept_reference_photo(task_id, body.photo_kept)
        if body.photo_picked:
            return scene_editor.picked_reference_photo(task_id, index)
        if body.photo_url:
            return scene_editor.chosen_reference_photo(task_id, body.photo_url)
        return None

    def in_its_frame(photo):
        # 用户在窗里摆好的大小和位置。没摆就是整张照片，一点不裁。
        if body.frame is None:
            return photo
        if photo is None:
            raise scene_editor.SceneError(
                "a size and position were given, but no photo was chosen to place"
            )
        return scene_editor.framed_photo(task_id, photo, body.frame.model_dump())

    job = _handle(
        task_id,
        request_id,
        lambda: scene_regen_queue.enqueue(
            task_id,
            index,
            keyword=body.keyword,
            image_style=body.image_style,
            photo=in_its_frame(chosen_photo()),
            # 照着一张真实照片重画。排进队列里而不是同步做：搜图加出图是
            # 分钟级的，而队列那边本来就一张一张地画、一张一张地报进度。
            from_photo=bool(body.from_photo),
            search=str(body.search or ""),
            # 每一张自己选保留多少照片。不给就用配置里那个。
            strength=body.strength,
            follow=str(body.follow or ""),
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
        # 同步那条也认 ``from_photo``：两条路由共用同一个请求体，字段的意思
        # 必须是同一个，否则"body 完全一样"就是假话。搜图在出图之前，搜不到
        # 就不出图。
        lambda: scene_editor.regenerate_material(
            task_id,
            index,
            keyword=body.keyword,
            image_style=body.image_style,
            strength=body.strength,
            follow=str(body.follow or ""),
            reference=(
                scene_editor.find_reference_photo(
                    task_id,
                    index,
                    search=str(body.search or ""),
                    keyword=body.keyword,
                )
                if body.from_photo
                else None
            ),
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


@router.post(
    "/tasks/{task_id}/scenes/{index}/upload",
    response_model=SceneResponse,
    summary="Put your own picture or video file on a scene",
)
def upload_scene_material(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
    file: UploadFile = File(...),
):
    """A clip made somewhere else, or a picture of your own, straight onto a
    scene. What the scene showed before is kept and can be put back."""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    placed = _handle(
        task_id,
        request_id,
        lambda: scene_editor.upload_material(
            task_id, index, file.filename or "", file.file),
    )
    answer = dict(_scene_payload(task_id, index))
    answer["fit"] = placed.get("fit")
    return utils.get_response(200, answer)


@router.post(
    "/tasks/{task_id}/scenes/{index}/fit",
    response_model=SceneResponse,
    summary="Slow a scene's own video clip so that it covers the scene",
)
def fit_scene_clip(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    fitted = _handle(
        task_id, request_id, lambda: scene_video.fit_own_clip(task_id, index))
    answer = dict(_scene_payload(task_id, index))
    answer["fit"] = fitted.get("fit")
    return utils.get_response(200, answer)


@router.get(
    "/tasks/{task_id}/pictures",
    summary="Every picture this video has drawn, newest first",
)
def list_pictures(request: Request, task_id: str = Path(..., description="Task ID")):
    """这条片子画过的每一张图，包括已经不在时间线上的那些。

    为什么需要它：重生成是换掉，不是修改。用户点一次重出拿回一张更差的图，
    上一张就不在任何界面上了——而那个文件一直在任务目录里。这个接口把目录
    里的东西摆出来，顺便说清每一张现在有没有人用、当时是凭什么画的。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    pictures = _handle(
        task_id,
        request_id,
        lambda: picture_shelf.shelf(task_id, _endpoint()),
    )
    return utils.get_response(200, {"pictures": pictures})


@router.post(
    "/tasks/{task_id}/scenes/{index}/restore",
    response_model=SceneResponse,
    summary="Put one of this video's earlier pictures back on a scene",
)
def restore_scene_material(
    request: Request,
    body: SceneReplaceRequest,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """把这一段换回它画过的某一张图。

    这不是重画：那张图就在磁盘上，所以这里只改一个文件名——几秒钟，不排队，
    不占显卡，不花钱。换下来的那一张同时记进 history，所以改主意还能再换
    回来。``material`` 只收文件名，而且必须是这条片子自己画过的东西。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    scene = _handle(
        task_id,
        request_id,
        lambda: scene_editor.restore_material(task_id, index, body.material),
    )
    payload = _scene_payload(task_id, index)
    # 这一张当时是照哪句提示词画的。不自动盖掉用户正在写的那一句——界面把它
    # 摆出来，由他决定要不要用。
    if isinstance(scene, dict) and scene.get("restored_keyword"):
        payload["restored_keyword"] = scene["restored_keyword"]
    return utils.get_response(200, payload)


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
    "/tasks/{task_id}/scenes/{index}/reveal",
    summary="Show a scene's picture in the file manager, selected",
)
def reveal_scene_file(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Scene index"),
):
    """Open the task's folder with this scene's image selected.

    The folder holds dozens of files named by digests, so finding one picture
    by eye is hopeless; the server knows which file it is. For a picture or a
    video scene made from one, that is the image itself. The same two gates as
    the route below apply: the file is this task's, and the request is local.
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)

    def show():
        found = scene_video.file_to_show(task_id, index)
        answer = reveal.reveal(
            task_id, found["file"],
            client=(request.client.host if request.client else None),
        )
        return {**answer, **found}

    return utils.get_response(200, _handle(task_id, request_id, show))


@router.post(
    "/tasks/{task_id}/reveal",
    summary="Show one of this task's files in the file manager",
)
def reveal_file(
    request: Request,
    body: RevealRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """把成片在资源管理器/访达里指出来。

    网页自己做不到这件事（也不该做到），但这个 API 和那个文件管理器在同一
    台机器上。两道闸在 ``reveal`` 里：文件必须是这条任务的，请求必须来自
    本机。开不了的时候不报错，返回路径让界面显示出来——用户复制一下就行，
    而一个红字的失败提示帮不上任何忙。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    answer = _handle(
        task_id,
        request_id,
        lambda: reveal.reveal(
            task_id,
            str(body.file or ""),
            client=(request.client.host if request.client else None),
        ),
    )
    return utils.get_response(200, answer)


@router.post(
    "/tasks/{task_id}/timeline/pace",
    response_model=SceneListResponse,
    summary="Hold each scene for as long as its narration line is spoken",
)
def pace_timeline(request: Request, task_id: str = Path(..., description="Task ID")):
    """每张画面跟着它那句旁白走：那句话开始时出现，下一段的话开始时换走。"""
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    _handle(task_id, request_id, lambda: scene_editor.pace_to_narration(task_id))
    return utils.get_response(
        200, scene_editor.describe(task_id, _endpoint(), with_timing=True)
    )


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
    # One kind of generation at a time: the voice shares the graphics card
    # with the pictures and the video scenes.
    _handle(task_id, request_id, scene_video.refuse_while_running)
    _handle(task_id, request_id,
            lambda: scene_regen_queue.refuse_while_busy("recording the narration"))
    params = _handle(task_id, request_id, lambda: scene_editor.prepare_audio_rerun(task_id))
    return _submit(task_id, request_id, params, stop_at="subtitle", start_from="audio")


@router.get(
    "/tasks/{task_id}/narration/{index}",
    summary="One narration line: what the voice says for it, and when",
)
def read_narration_line(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Narration line, counted like the captions"),
):
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: narration_retake.line(task_id, index)
    )
    return utils.get_response(200, data)


@router.post(
    "/tasks/{task_id}/narration/{index}/retake",
    summary="Queue one narration line to be recorded again",
)
def retake_narration_line(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Narration line, counted like the captions"),
):
    """One line, same words, same voice. Everything timed to the voice after
    it (subtitles, word times, the picture on screen) moves by the difference
    in length. Answers at once with the queued job: the line waits its turn
    in the same queue as the pictures, so the two never run together.
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: scene_regen_queue.enqueue_retake(task_id, index)
    )
    return utils.get_response(200, data)


@router.delete(
    "/tasks/{task_id}/narration/{index}",
    summary="Take one narration line out: its sound, its caption, its place in the script",
)
def delete_narration_line(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    index: int = Path(..., ge=0, description="Narration line, counted like the captions"),
):
    """Nothing is recorded. Everything timed to the voice after the line comes
    earlier by its length, and the picture on screen gets shorter by the same.
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    data = _handle(
        task_id, request_id, lambda: narration_retake.remove(task_id, index)
    )
    return utils.get_response(200, data)


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
                f"{request_id}: {pending} picture(s) or narration line(s) are still being regenerated; "
                "wait for them to finish or cancel them before rendering"
            ),
        )
    params = _handle(task_id, request_id, lambda: scene_editor.prepare_render(task_id))
    return _submit(task_id, request_id, params, stop_at="video", start_from="video")


@router.post(
    "/tasks/{task_id}/materials",
    response_model=TaskResponse,
    summary="Finish the pictures a failed run left unfinished",
)
def resume_materials(request: Request, task_id: str = Path(..., description="Task ID")):
    """把素材阶段半路断掉的任务接着跑完，不重新买已经出完的画面。

    停在 materials：出完画面先回审片界面。配音不重配——重配一次字幕时间轴
    全废,而这条任务的配音本来就是好的。
    """
    request_id = base.get_task_id(request)
    _require_task(task_id, request_id)
    params = _handle(
        task_id, request_id, lambda: scene_editor.prepare_materials_rerun(task_id)
    )
    return _submit(task_id, request_id, params, stop_at="materials", start_from="materials")


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


def _restore_state(task_id: str, snapshot: dict | None) -> None:
    """调度器没接住时把任务状态放回原样。

    放不回去的话这条任务会永远显示"在跑"：编辑器会一直锁着控件等一个根本
    没有在跑的渲染。重启过接口、状态里本来就没有这条任务时就把它删掉，和
    ``create_task`` 回滚的做法一致。
    """
    if not snapshot:
        sm.state.delete_task(task_id)
        return
    fields = {key: value for key, value in snapshot.items() if key != "task_id"}
    sm.state.update_task(task_id, **fields)


def _submit(
    task_id: str, request_id: str, params, *, stop_at: str, start_from: str
) -> dict:
    """把恢复任务交给既有的任务调度器。

    复用 ``task_manager`` 而不是直接起线程，是为了让审片触发的渲染和
    ``POST /videos`` 共用同一个并发上限：两条路径都会占满 GPU，各自计数
    等于没有上限。

    为什么要先把"在跑"写进状态里
    ----------------------------
    编辑器提交完立刻就去查任务状态（``watchTask``），而那一刻流水线线程往往
    还没开始写状态——重启过接口的话状态里更是连这条任务都没有。查到的就是
    404，编辑器那边一个查不到就不再跟着看了：渲染照样跑完，挡板却一直停在
    "正在提交"。实测 11:25:45 提交、11:33:04 渲完，用户全程只看到一个转圈。

    一次性生成那条路（``create_task``）本来就是先写状态再入队，这里补上同一
    个顺序。交不出去时把状态放回原样，不留一个永远"在跑"的任务。
    """
    before = sm.state.get_task(task_id)
    sm.state.update_task(task_id)
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
        _restore_state(task_id, before)
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {exc}"
        ) from exc
    except Exception:
        _restore_state(task_id, before)
        raise
    logger.success(
        f"task resumed: task_id={task_id}, start_from={start_from}, stop_at={stop_at}"
    )
    return utils.get_response(200, {"task_id": task_id})
