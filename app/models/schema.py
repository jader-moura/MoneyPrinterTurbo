import warnings
from enum import Enum
from typing import Any, List, Literal, Optional, Union

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from app.config import config
from app.models.const import DEFAULT_VIDEO_SOURCE, DEFAULT_VOICE_NAME

# 忽略 Pydantic 的特定警告
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message="Field name.*shadows an attribute in parent.*",
)


class VideoConcatMode(str, Enum):
    random = "random"
    sequential = "sequential"


class VideoTransitionMode(str, Enum):
    none = None
    shuffle = "Shuffle"
    fade_in = "FadeIn"
    fade_out = "FadeOut"
    slide_in = "SlideIn"
    slide_out = "SlideOut"
    zoom_in = "ZoomIn"
    zoom_out = "ZoomOut"


class VideoAspect(str, Enum):
    landscape = "16:9"
    portrait = "9:16"
    square = "1:1"

    def to_resolution(self):
        if self == VideoAspect.landscape:
            return 1920, 1080
        elif self == VideoAspect.portrait:
            return 1080, 1920
        elif self == VideoAspect.square:
            return 1080, 1080
        raise ValueError(f"unsupported video aspect: {self}")


class VideoFitMode(str, Enum):
    """How source clips with a different aspect ratio fill the output canvas."""

    cover = "cover"
    contain = "contain"


class ImageMotion(str, Enum):
    static = "static"
    zoom = "zoom"


SubtitleDisplayMode = Literal["sentence", "word_by_word"]
SubtitleAnimation = Literal["none", "pop_spring"]
_SUBTITLE_DISPLAY_MODES = ("sentence", "word_by_word")
_SUBTITLE_ANIMATIONS = ("none", "pop_spring")


def _get_valid_ui_choice(key: str, allowed_values: tuple[str, ...], default: str) -> str:
    """
    读取经过校验的 WebUI 枚举配置，兼容旧用户可能残留的无效值。

    请求体由 Pydantic 的 Literal 严格校验，拼写错误会返回明确的字段校验错误；
    配置文件则需要宽容处理，避免用户升级后因为历史手工配置错误导致整个服务
    无法启动。HTTP 状态码由应用统一的校验异常处理器决定，这里不绑定具体数值。
    """
    configured_value = config.ui.get(key, default)
    return configured_value if configured_value in allowed_values else default


_Config = ConfigDict(
    arbitrary_types_allowed=True,
    # Note: ensure your key names match renamed V2 parameters if needed
)


@pydantic.dataclasses.dataclass(config=_Config)
class MaterialInfo:
    provider: str = "pexels"
    url: str = ""
    duration: int = 0
    # 在线素材搜索会附带经过筛选的公开来源信息，供搜索缓存和任务记录复用。
    # 本地上传素材不需要填写；写入任务文件前仍会按字段白名单重新构造，
    # 避免外部请求传入的签名 URL、凭据或无关字段进入持久化数据。
    source_info: Optional[dict[str, Any]] = None


class VideoParams(BaseModel):
    """
    {
      "video_subject": "",
      "video_aspect": "横屏 16:9（西瓜视频）",
      "voice_name": "女生-晓晓",
      "bgm_name": "random",
      "font_name": "STHeitiMedium 黑体-中",
      "text_color": "#FFFFFF",
      "font_size": 60,
      "stroke_color": "#000000",
      "stroke_width": 1.5
    }
    """

    video_subject: str
    video_script: str = ""  # Script used to generate the video
    video_terms: Optional[str | List[str]] = None  # Keywords used to generate the video
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait
    video_fit_mode: VideoFitMode = VideoFitMode.cover
    image_motion: ImageMotion = ImageMotion.zoom

    # 这次生成用的图片风格提示词模板（``{term}`` 会替换成脚本关键词）。空
    # 字符串表示"按 config.toml 当前的设置来"，与本字段上线前完全一致，所以
    # 历史任务和不带它的 API 请求行为都不变。
    #
    # 为什么要钉在任务里：风格原本只有 config.toml 一个来源，而那是"现在"
    # 的设置。审片时在时间线编辑器里重生成一张图，跑的是 API 进程、读的是
    # 它自己那份早已载入内存的配置——于是用漫画风出的片子，补拍出来的是
    # 油画。任务开始时把当时真正用的模板记下来，重生成就不再依赖"现在"。
    #
    # The image-style prompt template this run used. Empty means "whatever
    # config.toml says now", which is exactly the behaviour from before this
    # field existed. Pinning it per task is the whole point: the style used to
    # live only in config.toml, which is the *current* setting, so
    # regenerating one scene later -- in another process, holding its own
    # loaded copy of that config -- could draw an oil painting into a
    # comic-book video.
    image_prompt_template: Optional[str] = ""
    video_concat_mode: Optional[VideoConcatMode] = VideoConcatMode.random
    video_transition_mode: Optional[VideoTransitionMode] = None
    video_clip_duration: int = Field(default=5, ge=1)
    video_clip_speed: Optional[float] = 1.0
    match_materials_to_script: bool = False
    # 逐句生成画面关键词：每一句旁白各要一个"这句话该配什么画面"的词，
    # 而不是给整篇脚本要固定几个主题词再按下标配对。关掉时行为与以前完全
    # 一致。默认打开：固定条数的词在下游按下标配给旁白,条数不等就必然错配
    # ——12 个主题词配 28 句旁白时,用户听到的是"早餐",看到的是超市货架。
    # 代价是按句子数出图、付费素材源花费上升,但错配的画面本来就等于白花。
    #
    # One search term per narration line instead of a fixed number of topics
    # for the whole script. On by default: a fixed count is paired with the
    # narration by index downstream, so unequal counts mismatch by
    # construction — 12 topics against 28 lines put the supermarket picture
    # under the sentence about breakfast.
    #
    # 只在"第一次生成画面"时起作用：它属于 terms 阶段,而审片之后的渲染
    # (start_from="materials") 根本不跑那个阶段,所以重新出片不会再问一次
    # 模型,也不会重新生成任何图片。
    align_terms_to_narration: bool = True
    video_count: int = Field(default=1, ge=1)

    # Pictures generated on this machine rather than stock footage searched for
    # with somebody's API key. Every other source still works when chosen.
    video_source: Optional[str] = DEFAULT_VIDEO_SOURCE
    video_materials: Optional[List[MaterialInfo]] = (
        None  # Materials used to generate the video
    )

    custom_audio_file: Optional[str] = (
        None  # Custom audio file path, will ignore TTS and can still use Whisper subtitles
    )
    video_language: Optional[str] = ""  # auto detect

    # A local Kokoro voice, so a request that names none still narrates without
    # a cloud account. Any provider's voice name is still accepted here.
    voice_name: Optional[str] = DEFAULT_VOICE_NAME
    voice_volume: Optional[float] = Field(default=1.0, allow_inf_nan=False)
    voice_rate: Optional[float] = Field(default=1.0, allow_inf_nan=False)
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = Field(default=0.2, allow_inf_nan=False)
    # 视频配乐供应商共用提示词，WebUI 新任务统一写入该字段。保留下面的
    # Sonilo 专用字段以兼容旧任务记录和现有 CLI 参数。
    video_music_prompt: str = Field(default="", max_length=2000)
    sonilo_bgm_prompt: str = Field(default="", max_length=2000)

    # 转场音效：每两段画面的交界处响一下的那个 whoosh/pop。空字符串表示不放，
    # 成片与本功能上线前完全一致，所以历史任务和不带这两个字段的 API 请求
    # 都不会突然多出声音。文件名由 app/services/transition_sound.py 在音效库
    # 内解析，这里不接受路径。
    transition_sound: Optional[str] = ""
    transition_sound_volume: Optional[float] = Field(default=0.5, allow_inf_nan=False)

    subtitle_enabled: Optional[bool] = True
    subtitle_position: Optional[str] = config.ui.get(
        "subtitle_position", "bottom"
    )  # top, bottom, center, custom, two_thirds_bottom
    subtitle_display_mode: SubtitleDisplayMode = _get_valid_ui_choice(
        "subtitle_display_mode", _SUBTITLE_DISPLAY_MODES, "sentence"
    )
    subtitle_animation: SubtitleAnimation = _get_valid_ui_choice(
        "subtitle_animation", _SUBTITLE_ANIMATIONS, "none"
    )
    custom_position: float = Field(
        default=config.ui.get("custom_position", 70.0), ge=0, le=100, allow_inf_nan=False
    )
    font_name: Optional[str] = "STHeitiMedium.ttc"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = False
    rounded_subtitle_background: bool = False

    font_size: int = Field(default=60, ge=1)
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = Field(default=1.5, ge=0, allow_inf_nan=False)
    n_threads: Optional[int] = 2
    paragraph_number: int = Field(default=1, ge=1, le=10)
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)

    # Which model writes the script and the keywords, for this task only.
    #
    # `[app] llm_provider` is one global setting, which makes "draft this one
    # on the Claude API and the rest on Ollama" a config edit between runs —
    # and a race, when two tasks overlap. These two fields carry the choice
    # with the task instead: empty means "whatever the config says", and a
    # provider id from app/models/llm_provider.py overrides it for this run
    # without touching config or affecting any other task.
    #
    # `llm_model` overrides that provider's model name the same way, so
    # ollama + a model that is not the configured one needs no config edit.
    llm_provider: Optional[str] = ""
    llm_model: Optional[str] = ""


class SubtitleRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    # Local Kokoro, like VideoParams: these endpoints should not reach a cloud
    # voice service for a caller who named no voice. Pass any provider's voice
    # name to use that provider instead.
    voice_name: Optional[str] = DEFAULT_VOICE_NAME
    voice_volume: Optional[float] = Field(default=1.0, allow_inf_nan=False)
    voice_rate: Optional[float] = Field(default=1.2, allow_inf_nan=False)
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = Field(default=0.2, allow_inf_nan=False)
    subtitle_position: Optional[str] = config.ui.get("subtitle_position", "bottom")
    subtitle_display_mode: SubtitleDisplayMode = _get_valid_ui_choice(
        "subtitle_display_mode", _SUBTITLE_DISPLAY_MODES, "sentence"
    )
    subtitle_animation: SubtitleAnimation = _get_valid_ui_choice(
        "subtitle_animation", _SUBTITLE_ANIMATIONS, "none"
    )
    font_name: Optional[str] = "STHeitiMedium.ttc"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = False
    rounded_subtitle_background: bool = False
    font_size: int = Field(default=60, ge=1)
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = Field(default=1.5, ge=0, allow_inf_nan=False)
    video_source: Optional[str] = "local"
    subtitle_enabled: Optional[bool] = True


class AudioRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    # Local Kokoro, like VideoParams: these endpoints should not reach a cloud
    # voice service for a caller who named no voice. Pass any provider's voice
    # name to use that provider instead.
    voice_name: Optional[str] = DEFAULT_VOICE_NAME
    voice_volume: Optional[float] = Field(default=1.0, allow_inf_nan=False)
    voice_rate: Optional[float] = Field(default=1.2, allow_inf_nan=False)
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = Field(default=0.2, allow_inf_nan=False)
    video_source: Optional[str] = "local"


class VideoScriptParams:
    """
    {
      "video_subject": "春天的花海",
      "video_language": "",
      "paragraph_number": 1,
      "video_script_prompt": "",
      "custom_system_prompt": ""
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_language: Optional[str] = ""
    paragraph_number: int = Field(default=1, ge=1, le=10)
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)
    # Per-request provider override; see VideoParams above.
    llm_provider: Optional[str] = ""
    llm_model: Optional[str] = ""


class VideoTermsParams:
    """
    {
      "video_subject": "",
      "video_script": "",
      "amount": 5,
      "match_materials_to_script": false
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_script: Optional[str] = (
        "春天的花海，如诗如画般展现在眼前。万物复苏的季节里，大地披上了一袭绚丽多彩的盛装。金黄的迎春、粉嫩的樱花、洁白的梨花、艳丽的郁金香……"
    )
    amount: Optional[int] = 5
    match_materials_to_script: bool = False
    # Per-request provider override; see VideoParams above.
    llm_provider: Optional[str] = ""
    llm_model: Optional[str] = ""


class VideoSocialMetadataParams:
    """
    {
      "video_subject": "A day in Shanghai",
      "video_script": "",
      "language": "auto",
      "platform": "tiktok"
    }
    """

    video_subject: Optional[str] = Field(default="A day in Shanghai", max_length=500)
    video_script: Optional[str] = Field(default="", max_length=8000)
    language: Optional[str] = Field(default="auto", max_length=64)
    platform: Optional[str] = Field(default="tiktok", max_length=64)


class TaskVideoRequest(VideoParams, BaseModel):
    # Bound API resource requests while leaving the CLI's explicit batch and
    # FFmpeg thread controls in VideoParams unchanged.
    video_count: int = Field(default=1, ge=1, le=5)
    video_clip_duration: int = Field(default=5, ge=1, le=15)
    n_threads: Optional[int] = Field(default=2, ge=1, le=16)


class TaskQueryRequest(BaseModel):
    pass


class VideoScriptRequest(VideoScriptParams, BaseModel):
    pass


class VideoTermsRequest(VideoTermsParams, BaseModel):
    # Ordered term generation allocates an example list proportional to amount
    # before contacting the model. Reject malformed or excessive API requests.
    amount: int = Field(default=5, ge=1, le=50)


class VideoSocialMetadataRequest(VideoSocialMetadataParams, BaseModel):
    pass


# ---------------------------
# ----- RESPONSE MODELS -----
# ---------------------------
class BaseResponse(BaseModel):
    status: int = 200
    message: Optional[str] = "success"
    data: Any = None


# ---- DATA MODELS ----
class TaskResponseData(BaseModel):
    task_id: str


class TaskStatusData(BaseModel):
    """任务查询对外保证的稳定字段；历史和扩展字段继续原样透传。"""

    model_config = ConfigDict(extra="allow")

    task_id: str
    state: int
    progress: int = 0
    videos: Optional[List[str]] = None
    combined_videos: Optional[List[str]] = None
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    cross_post_state: Optional[
        Literal["pending", "processing", "complete", "failed"]
    ] = None
    cross_post_results: Optional[List[dict[str, Any]]] = None
    cross_post_error: Optional[str] = None


class TaskListData(BaseModel):
    """分页任务列表结构。"""

    tasks: List[TaskStatusData]
    total: int
    page: int
    page_size: int


class VideoScriptData(BaseModel):
    video_script: str


class VideoTermsData(BaseModel):
    video_terms: List[str]


class VideoSocialMetadataData(BaseModel):
    title: str
    caption: str
    hashtags: List[str]


class FileData(BaseModel):
    name: str
    size: int
    file: str


class BgmRetrieveData(BaseModel):
    files: List[FileData]


class BgmUploadData(BaseModel):
    file: str


class VideoMaterialRetrieveData(BaseModel):
    files: List[FileData]


class VideoMaterialUploadData(BaseModel):
    file: str


# ---- RESPONSE MODELS ----
class TaskResponse(BaseResponse):
    data: TaskResponseData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                },
            },
        }
    )


class TaskQueryResponse(BaseResponse):
    """
    任务查询会返回生成状态和可选的跨平台发布状态。

    生成失败时包含 `failed_stage` 和 `error`；生成完成后如果启用了自动发布，
    `cross_post_state` 会依次进入 pending、processing、complete 或 failed。
    """

    data: TaskStatusData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": 200,
                    "message": "success",
                    "data": {
                        "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                        "state": 1,
                        "progress": 100,
                        "videos": ["/tasks/example/final-1.mp4"],
                        "cross_post_state": "complete",
                        "cross_post_results": [{"success": True}],
                    },
                },
                {
                    "status": 200,
                    "message": "success",
                    "data": {
                        "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                        "state": -1,
                        "progress": 30,
                        "failed_stage": "audio",
                        "error": "TTS request timed out",
                    },
                },
            ],
        }
    )


class TaskListResponse(BaseResponse):
    """任务列表使用独立响应模型，避免与单任务查询混用文档结构。"""

    data: TaskListData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "tasks": [
                        {
                            "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                            "state": 4,
                            "progress": 50,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                },
            }
        }
    )


class TaskDeletionResponse(BaseResponse):
    data: None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": None,
            },
        }
    )


class VideoScriptResponse(BaseResponse):
    data: VideoScriptData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "video_script": "春天的花海，是大自然的一幅美丽画卷。在这个季节里，大地复苏，万物生长，花朵争相绽放，形成了一片五彩斑斓的花海..."
                },
            },
        }
    )


class VideoTermsResponse(BaseResponse):
    data: VideoTermsData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {"video_terms": ["sky", "tree"]},
            },
        }
    )


class VideoSocialMetadataResponse(BaseResponse):
    data: VideoSocialMetadataData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "title": "A Day in Shanghai You Should Not Miss",
                    "caption": "Save this quick Shanghai inspiration and follow for more short travel ideas.",
                    "hashtags": ["#shorts", "#travel", "#shanghai", "#viral", "#fyp"],
                },
            },
        }
    )


class BgmRetrieveResponse(BaseResponse):
    data: BgmRetrieveData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "4fca18fce7344f3aa824777a40d45c8c.mp3",
                            "size": 1891269,
                            "file": "4fca18fce7344f3aa824777a40d45c8c.mp3",
                        }
                    ]
                },
            },
        }
    )


class BgmUploadResponse(BaseResponse):
    data: BgmUploadData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {"file": "4fca18fce7344f3aa824777a40d45c8c.mp3"},
            },
        }
    )


class VideoMaterialRetrieveResponse(BaseResponse):
    data: VideoMaterialRetrieveData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "example.mp4",
                            "size": 12345678,
                            "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                        }
                    ]
                },
            },
        }
    )


class VideoMaterialUploadResponse(BaseResponse):
    data: VideoMaterialUploadData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                },
            },
        }
    )


# ---- SCENE REVIEW ----
# 审片是渲染前的可选步骤：先跑到素材阶段，逐个场景检查、替换、重生成，
# 再提交渲染。这些模型只描述"对某一个场景做什么"，场景本身存在任务清单里。
# Reviewing is the optional step before rendering: run up to materials, check
# and fix each scene, then render. These models describe what to do to one
# scene; the scenes themselves live in the task manifest.
class SceneUpdateRequest(BaseModel):
    """改台词、关键词或锁定状态。只提交想改的字段。"""

    narration: Optional[str] = None
    keyword: Optional[str] = None
    locked: Optional[bool] = None


class SceneRegenerateRequest(BaseModel):
    """重新生成这一个场景的素材。

    ``keyword`` 给了就同时改写场景关键词——画面不对时要改的通常正是它。

    ``image_style`` 给了就只给这一张换个画风（预设 id 见
    ``app/models/image_style.py``）。不给就沿用任务自己记下的风格,也就是
    出片时真正用的那个。
    """

    keyword: Optional[str] = None
    image_style: Optional[str] = None


class SceneReplaceRequest(BaseModel):
    """用已上传的素材替换场景画面。``material`` 是上传接口返回的文件名。"""

    material: str


class SceneAddRequest(BaseModel):
    """按关键词生成一段新画面并加进时间线。

    ``after`` 是插入位置（插在该下标之后）；不给就追加到末尾。
    ``narration`` 默认留空：补画面不该顺手改旁白，否则会悄悄让已录好的
    配音失效。

    ``image_style`` 给了就用这个画风生成（预设 id 见
    ``app/models/image_style.py``）；不给就用任务自己记下的那个，补出来的
    画面和片子里原有的画面是同一种风格。
    """

    keyword: str
    narration: Optional[str] = None
    after: Optional[int] = Field(default=None, ge=0)
    image_style: Optional[str] = None


class SceneTimingRequest(BaseModel):
    """设置这一段的入点和在成片里的停留时长。

    ``trim_start`` 是**源素材上**的秒数，``duration`` 是**画面在成片里**停留
    的秒数；变速不为 1 时两者差一个倍率，换算由服务端做。
    ``reset`` 为真时清掉这一段的时间线设置，让它回到自动时长。
    """

    trim_start: Optional[float] = Field(default=None, ge=0)
    duration: Optional[float] = Field(default=None, gt=0)
    reset: bool = False


class SceneReorderRequest(BaseModel):
    """``order`` 必须是 0..N-1 的完整排列，不接受部分顺序。"""

    order: List[int]


class SceneData(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    keyword: str = ""
    narration: str = ""
    material: str = ""
    kind: str = ""
    source: str = ""
    locked: bool = False
    url: str = ""
    exists: bool = False
    # 时间线字段。``duration``/``source_duration`` 为 None 表示这一段没被
    # 编辑过，按素材全长自动决定。
    trim_start: float = 0.0
    duration: Optional[float] = None
    source_duration: Optional[float] = None


class SceneListData(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    scenes: List[SceneData] = []
    audio_stale: bool = False
    audio_url: str = ""
    script: str = ""


class SceneListResponse(BaseResponse):
    data: SceneListData


class SceneResponse(BaseResponse):
    data: SceneData
