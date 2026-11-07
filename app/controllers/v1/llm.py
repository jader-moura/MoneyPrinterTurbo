from fastapi import Depends, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    VideoScriptRequest,
    VideoScriptResponse,
    VideoSocialMetadataRequest,
    VideoSocialMetadataResponse,
    VideoTermsRequest,
    VideoTermsResponse,
)
from app.services import llm
from app.utils import utils

# LLM 接口与视频接口共用同一鉴权规则，避免新增端点时遗漏保护。
# api_key 为空时 verify_token 直接放行，不改变默认本地使用体验。
router = new_router(dependencies=[Depends(base.verify_token)])


def _llm_override(request: Request, body) -> dict:
    """``{"app_config": snapshot}`` when the body names its own provider.

    `llm_provider` / `llm_model` choose the model for this request alone;
    leaving them out means "whatever config.toml says", and then the call is
    made exactly as it always was. An unknown provider id is the caller's
    mistake, so it is a 400 — not the 503 these endpoints return when a
    configured provider is simply unavailable.
    """
    provider = str(getattr(body, "llm_provider", "") or "").strip()
    model = str(getattr(body, "llm_model", "") or "").strip()
    if not provider and not model:
        return {}
    try:
        return {
            "app_config": llm.resolve_llm_app_config(
                provider, model, require_credentials=True
            )
        }
    except ValueError as exc:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=400, message=str(exc)
        ) from None


@router.post(
    "/scripts",
    response_model=VideoScriptResponse,
    summary="Create a script for the video",
)
def generate_video_script(request: Request, body: VideoScriptRequest):
    override = _llm_override(request, body)
    video_script = llm.generate_script(
        video_subject=body.video_subject,
        language=body.video_language,
        paragraph_number=body.paragraph_number,
        video_script_prompt=body.video_script_prompt,
        custom_system_prompt=body.custom_system_prompt,
        **override,
    )
    if not video_script or video_script.startswith("Error: "):
        raise HttpException(
            task_id=base.get_task_id(request), status_code=503,
            message="script generation is unavailable",
        )
    response = {"video_script": video_script}
    return utils.get_response(200, response)


@router.post(
    "/terms",
    response_model=VideoTermsResponse,
    summary="Generate video terms based on the video script",
)
def generate_video_terms(request: Request, body: VideoTermsRequest):
    override = _llm_override(request, body)
    video_terms = llm.generate_terms(
        video_subject=body.video_subject,
        video_script=body.video_script,
        amount=body.amount,
        match_script_order=body.match_materials_to_script,
        **override,
    )
    if not video_terms:
        raise HttpException(
            task_id=base.get_task_id(request), status_code=503,
            message="search term generation is unavailable",
        )
    response = {"video_terms": video_terms}
    return utils.get_response(200, response)


@router.post(
    "/social-metadata",
    response_model=VideoSocialMetadataResponse,
    summary="Generate social publishing metadata",
)
def generate_video_social_metadata(
    request: Request, body: VideoSocialMetadataRequest
):
    metadata = llm.generate_social_metadata(
        video_subject=body.video_subject,
        video_script=body.video_script,
        language=body.language,
        platform=body.platform,
    )
    return utils.get_response(200, metadata)
