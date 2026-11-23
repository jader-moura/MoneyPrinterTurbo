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
    # 真正的叠化：前一段的尾巴和后一段的开头叠在一起半秒。上面那几种都是
    # 在一段自己的时间里做的（从黑里淡出来、滑进来），这一种是两段之间的。
    # A real dissolve between two clips, where the others above happen inside
    # one clip's own time.
    cross_fade = "CrossFade"


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
    # 下面这几种是后加的。每一张图都用同一种慢推，一条八分钟的片子看下来
    # 像幻灯片；"varied" 让每张图在五种运镜里按图的内容挑一种（同一张图每次
    # 挑到的是同一种），其余几个是把某一种单独点出来。值会写进片段的文件名，
    # 所以里面不能有 "." 和 "-"。
    #
    # Added later. "varied" gives each picture one of the five moves below,
    # chosen from the picture's own content so it is the same every time.
    # The value goes into the clip's file name: no "." and no "-".
    varied = "varied"
    zoom_out = "zoom_out"
    pan_right = "pan_right"
    pan_left = "pan_left"
    zoom_top = "zoom_top"


# "highlight"：几个词一组，念到哪个词哪个词变色。只有成片精修那条路
# （``finish_look`` 不为空）画得出来；原来的渲染器遇到它按 "sentence" 画。
SubtitleDisplayMode = Literal["sentence", "word_by_word", "highlight"]
SubtitleAnimation = Literal["none", "pop_spring"]
_SUBTITLE_DISPLAY_MODES = ("sentence", "word_by_word", "highlight")
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


def _get_valid_ui_int(key: str, default: int, *, minimum: int = 0) -> int:
    """读取一个整数型 WebUI 配置，写坏了就当没写。

    和 ``_get_valid_ui_choice`` 同一个理由：配置文件里一个手打错的值不该让整
    个服务起不来。Pydantic 不校验字段默认值，所以这里自己兜住。
    """
    try:
        value = int(config.ui.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


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

    # 一张图配一整句话，而不是配逗号切出来的半句。
    #
    # 断句用的是 ``split_string_by_punctuations``，它连逗号一起断——配音和
    # 字幕要的就是这么细。可画面不是：一条真片子里 13 句话被切成 29 段，
    # 其中 16 段是 "in Texas"、"Your laptop" 这样的碎片。为每个碎片单画一
    # 张图，是按念 0.7 秒的半句话去烧几分钟显卡，而且那张图短到看不清。
    #
    # 关掉它就回到一段一张图。
    #
    # One picture per whole sentence instead of per comma-clause. Splitting
    # on commas is right for the voice and the subtitles; for pictures it
    # means drawing a separate image for "in Texas", spoken in 0.7s.
    one_picture_per_sentence: bool = True

    # 这条片子里有百分之多少的画面要照着用户自己挑的照片画（0 = 一张都不）。
    # 流水线本身不读它：它只是记在任务上，编辑器点"开画"时据此先请用户把
    # 那几张照片挑好，再开始排队。
    # Share of this video's scenes drawn from photos the user chooses. The
    # pipeline does not read it; the editor does, when the pictures are drawn.
    photo_scene_percent: int = Field(default=0, ge=0, le=100)
    # 在外面写好的分镜（见 app/services/scene_plan.py）：每段的旁白，和照着
    # 照片画的那几段要找什么。有它的时候场景照它切，不照脚本重新断句。
    # A scene plan written outside the app: each scene's narration and, for
    # the scenes drawn from a photo, what to look for.
    scene_plan: Optional[List[dict]] = None

    # 一张图最多停多久（秒）。
    #
    # 画面是按它配的那句话有多长就停多长的，这正是同步的来源；可实测用户那条
    # 499 秒的片子里，最长的一句要念 18.6 秒——一张静图干停这么久。超过这个数
    # 的句子会被拆回分句，每段各配一张图。
    #
    # 8 秒是按那条真片子选的：96 句里 15 句超过，多画约 16 张图（96 → 112），
    # 最长的定格从 18.6 秒降到 13 秒。剩下那 13 秒是一个 30 词、中间没有任何
    # 标点的分句，再拆就得把一句话从中间切开，所以不拆。
    #
    # 填 0 就是不设上限，回到按整句停。
    #
    # How long one picture may hold. Pictures last as long as the line they
    # illustrate, which is what keeps them in sync, but the longest line in
    # the measured task ran 18.6 seconds. Lines over this are split back into
    # their clauses, one picture each. A single clause with no internal
    # punctuation cannot be split further and still goes over. 0 disables it.
    max_image_scene_duration: float = 8.0
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

    # A local Chatterbox voice, so a request that names none still narrates without
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

    # 成片精修（``app/services/finishing.py``）。空串 = 不精修，成片由原来的
    # 渲染器出，和这个字段出现之前逐字节一致——所以历史任务和不带这个字段的
    # API 请求都不受影响。不为空时是调色的名字（``finishing.LOOKS`` 的键，
    # "clean" 是不调色），成片改由 ffmpeg 出：旁白均衡压缩、配乐让位、整体
    # 响度 -14 LUFS、调色、暗角、颗粒、淡入淡出、libass 烧字幕。
    #
    # Empty keeps the classic renderer, byte for byte. A look name from
    # ``finishing.LOOKS`` sends the final render through the ffmpeg finishing
    # pass instead.
    finish_look: Optional[str] = Field(default="", max_length=40)

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
    # 同一时刻屏幕上最多几行字幕。
    #
    # 字幕文件里的一条是旁白的一个分句，可以念上六七秒；大字号竖屏里那就是
    # 四五行一堵字墙，而且整句话从第一个字起就全摊在那儿了。超过这个行数的
    # 那一条在渲染时被切成几条，按字数分时间；文件本身不动——它同时是每段
    # 画面停多久的唯一依据。
    #
    # 填 0 就是不切，和这个设置出现之前完全一样。
    #
    # How many lines of subtitle may be on screen at once. One cue is one
    # spoken clause, which can run six or seven seconds and fill four or five
    # lines at a large font size. Longer cues are cut into several on-screen
    # pieces at render time, sharing the cue's duration by character count.
    # The file itself is untouched: it is also the only source of per-picture
    # timing. 0 keeps the old behaviour.
    subtitle_max_lines: int = Field(
        default=_get_valid_ui_int("subtitle_max_lines", 2), ge=0, le=20
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
    # Local Chatterbox, like VideoParams: these endpoints should not reach a cloud
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
    # 和 VideoParams 同一个意思：一次最多几行。0 是不切。
    subtitle_max_lines: int = Field(
        default=_get_valid_ui_int("subtitle_max_lines", 2), ge=0, le=20
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
    # Local Chatterbox, like VideoParams: these endpoints should not reach a cloud
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


class PhotoFrame(BaseModel):
    """照片在画框里摆多大、摆在哪。

    ``zoom`` 是相对于"整张照片刚好放进画框"的倍数：1 就是整张都在、一点不
    裁；大于 1 是放大（出框的部分是用户自己决定不要的）；小于 1 是缩小。
    ``x``、``y`` 是照片中心离画框中心多远，按画框的宽、高算（0.5 就是半个
    画框）。三个都是默认值等于没有这个框。
    """

    model_config = ConfigDict(extra="forbid")

    zoom: float = Field(default=1.0, ge=0.25, le=4)
    x: float = Field(default=0.0, ge=-1, le=1)
    y: float = Field(default=0.0, ge=-1, le=1)


class SceneRegenerateRequest(BaseModel):
    """重新生成这一个场景的素材。

    ``keyword`` 给了就同时改写场景关键词——画面不对时要改的通常正是它。

    ``image_style`` 给了就只给这一张换个画风（预设 id 见
    ``app/models/image_style.py``）。不给就沿用任务自己记下的风格,也就是
    出片时真正用的那个。

    ``from_photo`` 打开的是另一条画法：先去网上搜一张真实照片，再照着它画
    这一张。构图、比例、年代跟着照片走，画风仍然跟着提示词走——文生图模型
    没见过"那架 1952 年的彗星号客机"，而网上有它的照片。默认关着，所以老
    调用方的行为一个字都没变。

    ``search`` 是搜图用的词。留空就让模型从这一段的旁白和提示词里写一个
    （提示词本身带着画风，直接拿去搜图会搜回别人画的漫画）。只在
    ``from_photo`` 打开时有意义。

    ``strength`` 决定这一张保留多少照片：0 到 1，越大越不看照片。这是**每
    一张**都要选的，不是一个全局设置——"这一张要照片里那架飞机，那一张只要
    画风"是两次不同的判断。不给就用 ``reference_image_strength``。

    量过的分界（Z-Image-Turbo，同一张照片、同一句提示词、同一个种子）：

    ==========  ====================================================
    0.35-0.60   照片的构图、比例、机型都在，但出来的是一张照片，提示
                词里的画风基本不起作用。
    0.75        两边都不要：构图丢了，画风也没上去。
    0.90-0.98   画风完全对，照片一点没剩——连"那是什么飞机"都没剩，0.95
                那次画出了一架螺旋桨客机。
    ==========  ====================================================

    所以这两件事在这个模型上换不来：没有哪个值能同时给到。界面上给的是
    两个说实话的选项，而不是一个假装中间值有用的滑块。

    ``from_photo`` turns on the other way of drawing: find a real photograph
    first, then redraw it in the chosen style. Default off, so existing
    callers behave exactly as before. ``search`` overrides the query the model
    would have written. ``strength`` is how much of the photograph to keep,
    chosen per picture, because keeping the subject and keeping a stylised
    template are mutually exclusive on this model.
    """

    keyword: Optional[str] = None
    image_style: Optional[str] = None
    from_photo: bool = False
    search: Optional[str] = Field(default="", max_length=200)
    strength: Optional[float] = Field(default=None, gt=0, le=1)
    # 用户自己挑的那一张：搜索结果里的一个地址，或者刚传上来的一张的名字。
    # 给了其中一个就照它画，不再自动搜。
    photo_url: Optional[str] = Field(default=None, max_length=2000)
    photo_upload: Optional[str] = Field(default=None, max_length=80)
    # 这条片子里已经照着画过、存下来的那一张（场景 ``reference.file``）。
    # 同一张照片换一种"留多少"再画一次，不用重新去找它。
    photo_kept: Optional[str] = Field(default=None, max_length=40)
    # 开画之前给这一段定下、存在这条片子里的那一张（见 ``photo-pick``）。
    # 这一段还没照着它画过、或者后来改成照提示词画了，也能拿它来画。
    photo_picked: bool = False
    # "outlines"：同一张照片，换成我的画风——只跟着照片的轮廓，按画风从头
    # 画。给了它就不看 ``strength``。
    # "original": the photo itself goes on the scene; nothing is drawn.
    follow: Optional[str] = Field(default=None, pattern="^(outlines|picture|original)$")
    # 用户在窗里把照片摆好的大小和位置。只对挑好的那一张有意义。
    frame: Optional[PhotoFrame] = None


class ScenePhotoPickRequest(BaseModel):
    """开画之前给一段定下的照片：挑的那一张、传的那一张，或者"这一段不要"。

    和 ``SceneRegenerateRequest`` 里照片那几项是同一个意思，只是这里不排队：
    照片先存在这条片子里，点"开画"时才用。三项照片都不给、也不是 ``skip``，
    就是"替我挑一张"。
    """

    model_config = ConfigDict(extra="forbid")

    skip: bool = False
    search: Optional[str] = Field(default="", max_length=200)
    strength: Optional[float] = Field(default=None, gt=0, le=1)
    photo_url: Optional[str] = Field(default=None, max_length=2000)
    photo_upload: Optional[str] = Field(default=None, max_length=80)
    photo_kept: Optional[str] = Field(default=None, max_length=40)
    follow: Optional[str] = Field(default=None, pattern="^(outlines|picture|original)$")
    # 用户在窗里把照片摆好的大小和位置。只对挑好的那一张有意义。
    frame: Optional[PhotoFrame] = None


class ScenePromptSuggestRequest(BaseModel):
    """用一句短话，让模型把这一段的画面提示词写完整。

    ``direction`` 是用户自己那句话——"换成晚上"、"让她在笑"。``current`` 是
    框里**当前**的内容（可能有还没保存的改动）；不给就用清单里存着的那条。

    两个都可以空：只给 ``direction`` 就是从头写一条，只给 ``current`` 就是
    照规矩把现在这条写好。什么都没有、这一段又没有旁白时会报错，不瞎编。

    这个接口**不写盘**。返回的是草稿，存不存由调用方决定——模型写歪的时候，
    一次点击不该已经把用户手写的那条覆盖掉。
    """

    direction: Optional[str] = Field(default="", max_length=1000)
    current: Optional[str] = Field(default="", max_length=4000)


class ScenePromptSuggestResponse(BaseResponse):
    data: dict = {"prompt": "an exhausted nurse rubbing her eyes at 3am"}


class NewVideoRequest(BaseModel):
    """在编辑器里开一条新片子时表单交上来的东西。

    每一项都可以不给：没给的用上次存下来的设置，见 ``app.services.new_video``。
    表外的键一律拒绝——拼错一个名字就该当场知道，而不是悄悄用了默认值。
    """

    model_config = ConfigDict(extra="forbid")

    # 说什么
    video_subject: str = Field(default="", max_length=2000)
    video_script: str = Field(default="", max_length=20000)
    video_terms: Optional[str] = Field(default=None, max_length=4000)
    scene_plan: Optional[str] = Field(default=None, max_length=400000)
    video_language: Optional[str] = Field(default=None, max_length=20)
    paragraph_number: Optional[int] = Field(default=None, ge=1, le=10)
    video_script_prompt: Optional[str] = Field(default=None, max_length=2000)
    custom_system_prompt: Optional[str] = Field(default=None, max_length=8000)
    llm_provider: Optional[str] = Field(default=None, max_length=60)
    llm_model: Optional[str] = Field(default=None, max_length=200)
    # 画面
    video_source: Optional[str] = Field(default=None, max_length=40)
    confirm_charge: Optional[bool] = None
    local_materials: Optional[list[str]] = Field(default=None, max_length=200)
    video_aspect: Optional[str] = Field(default=None, max_length=10)
    image_style: Optional[str] = Field(default=None, max_length=40)
    image_motion: Optional[str] = Field(default=None, max_length=20)
    finish_look: Optional[str] = Field(default=None, max_length=40)
    video_clip_duration: Optional[int] = Field(default=None, ge=1, le=15)
    one_picture_per_sentence: Optional[bool] = None
    photo_scene_percent: Optional[int] = Field(default=None, ge=0, le=100)
    max_image_scene_duration: Optional[float] = Field(default=None, ge=0, le=60)
    align_terms_to_narration: Optional[bool] = None
    review_prompts: Optional[bool] = None
    video_concat_mode: Optional[str] = Field(default=None, max_length=20)
    match_materials_to_script: Optional[bool] = None
    video_transition_mode: Optional[str] = Field(default=None, max_length=20)
    video_fit_mode: Optional[str] = Field(default=None, max_length=20)
    video_clip_speed: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    video_count: Optional[int] = Field(default=None, ge=1, le=5)
    # 配音
    voice_mode: Optional[str] = Field(default=None, max_length=10)
    tts_server: Optional[str] = Field(default=None, max_length=40)
    voice_name: Optional[str] = Field(default=None, max_length=300)
    voice_rate: Optional[float] = Field(default=None, ge=0.25, le=4.0)
    voice_volume: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    custom_audio: Optional[str] = Field(default=None, max_length=80)
    # 字幕
    subtitle_enabled: Optional[bool] = None
    font_name: Optional[str] = Field(default=None, max_length=200)
    font_size: Optional[int] = Field(default=None, ge=8, le=300)
    text_fore_color: Optional[str] = Field(default=None, max_length=9)
    stroke_color: Optional[str] = Field(default=None, max_length=9)
    stroke_width: Optional[float] = Field(default=None, ge=0, le=20)
    subtitle_position: Optional[str] = Field(default=None, max_length=30)
    custom_position: Optional[float] = Field(default=None, ge=0, le=100)
    subtitle_display_mode: Optional[str] = Field(default=None, max_length=20)
    subtitle_animation: Optional[str] = Field(default=None, max_length=20)
    subtitle_max_lines: Optional[int] = Field(default=None, ge=1, le=4)
    subtitle_background_enabled: Optional[bool] = None
    subtitle_background_color: Optional[str] = Field(default=None, max_length=9)
    rounded_subtitle_background: Optional[bool] = None
    # 配乐
    music_type: Optional[str] = Field(default=None, max_length=20)
    music_preset: Optional[str] = Field(default=None, max_length=300)
    music_custom: Optional[str] = Field(default=None, max_length=300)
    music_prompt: Optional[str] = Field(default=None, max_length=2000)
    bgm_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    transition_sound: Optional[str] = Field(default=None, max_length=300)
    transition_sound_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class VoicePreviewRequest(BaseModel):
    """念一小段给人听。``text`` 不给就念一句固定的样例。"""

    voice_name: str = Field(max_length=300)
    voice_rate: float = Field(default=1.0, ge=0.25, le=4.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=5.0)
    text: str = Field(default="", max_length=4000)


class SettingsUpdateRequest(BaseModel):
    """改设置。``changes`` 是 ``{"分区.键": 新值}``；凭据留空等于不动，
    要清空得在 ``clear`` 里点名。"""

    changes: dict[str, Any] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list, max_length=200)


class SceneSplitRequest(BaseModel):
    """在场景的第 ``at`` 秒切一刀。``at`` 是这一段在成片里的时间。"""

    at: float = Field(gt=0, le=36000)


class CacheCleanRequest(BaseModel):
    """清素材缓存。``days`` 是"比这更旧的"，不给就是全部；删了就没了，所以
    要 ``confirm``。"""

    days: Optional[int] = None
    confirm: bool = False


class LoggingOptionsRequest(BaseModel):
    level: str = Field(max_length=20)
    log_requests: bool = False


class ConfirmRequest(BaseModel):
    """要用户明说一次"是"的那种请求。"""

    confirm: bool = False


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


class SceneVideoMarkRequest(BaseModel):
    """Mark a scene to become a video clip in the next batch.

    ``image`` animates the scene's picture, ``text`` draws a clip from its
    prompt, and an empty string takes the mark off again.
    """

    mode: Literal["", "image", "text"] = ""


class SceneTimingRequest(BaseModel):
    """设置这一段的入点和在成片里的停留时长。

    ``trim_start`` 是**源素材上**的秒数，``duration`` 是**画面在成片里**停留
    的秒数；变速不为 1 时两者差一个倍率，换算由服务端做。
    ``reset`` 为真时清掉这一段的时间线设置，让它回到自动时长。
    """

    trim_start: Optional[float] = Field(default=None, ge=0)
    duration: Optional[float] = Field(default=None, gt=0)
    reset: bool = False


class RevealRequest(BaseModel):
    """在文件管理器里指出这条任务的某个文件。

    ``file`` 是**文件名**，不是路径：这个接口能让服务端在桌面上开一个窗口，
    所以能开哪个由服务端自己在任务目录里解析，不接受调用方递过来的路径。
    留空就打开任务目录本身。
    """

    file: Optional[str] = Field(default="", max_length=255)


class SceneReorderRequest(BaseModel):
    """``order`` 必须是 0..N-1 的完整排列，不接受部分顺序。"""

    order: List[int]


class SceneEffectsRequest(BaseModel):
    """这一段画面怎么出场：转场和运镜。只提交想改的字段。

    两者都用空字符串表示"跟整条片子一样"，这和显式选 ``none``（这一刀不要
    转场）不是一回事。
    """

    transition: Optional[str] = None
    image_motion: Optional[str] = None
    cut_sound: Optional[str] = None


class SubtitleStyleRequest(BaseModel):
    """字幕的样子。只提交想改的字段。

    颜色一律 ``#RRGGBB``。``background`` 给 ``false`` 或空字符串表示不要底色。
    """

    enabled: Optional[bool] = None
    font_name: Optional[str] = None
    font_size: Optional[int] = None
    color: Optional[str] = None
    stroke_color: Optional[str] = None
    stroke_width: Optional[float] = None
    background: Union[bool, str, None] = None
    position: Optional[str] = None
    custom_position: Optional[float] = None


class VideoEffectsRequest(BaseModel):
    """整条片子的转场和运镜。

    ``apply_to_scenes`` 打开时顺手清掉每一段自己挑过的转场，否则"全部改掉"
    之后那几段仍然我行我素。
    """

    transition: Optional[str] = None
    image_motion: Optional[str] = None
    # 成片精修用哪种调色；空串是关掉（用原来的渲染器），不给就是不改。
    finish_look: Optional[str] = Field(default=None, max_length=40)
    apply_to_scenes: bool = False


class CutSoundRequest(BaseModel):
    """整条片子在每一刀上响的那一声。``sound`` 空字符串表示一刀都不响。"""

    sound: Optional[str] = None
    volume: Optional[float] = None


class SubtitleEditRequest(BaseModel):
    """改一条字幕的文字。时间轴和配音都不动。"""

    text: str


class BackgroundMusicRequest(BaseModel):
    """这条片子的背景音乐。只提交想改的字段。

    ``track`` 空字符串是"不要背景音乐"，``"random"`` 是每次渲染随机挑一首，
    其余是音乐库里的文件名。音量是 0 到 1。
    """

    track: Optional[str] = None
    volume: Optional[float] = None


class BackgroundMusicData(BaseModel):
    model_config = ConfigDict(extra="allow")

    track: str = ""
    kind: str = ""
    volume: float = 0.0
    tracks: List[str] = []
    external: bool = False


class BackgroundMusicResponse(BaseResponse):
    data: BackgroundMusicData


class NarrationScriptRequest(BaseModel):
    """整条旁白的文字，一次改完。

    台词按时间归到画面上，所以改句子的先后、把一句拆成两句、或者整段重写，
    都没有哪一段画面是它的归属——那些要改的是这里。场景上那个输入框只是这段
    文字的局部视图。
    """

    script: str



class PublishMetadataRequest(BaseModel):
    """发到 YouTube 时要填的文案。只提交想改的字段。

    超长一律报 400 而不是截断：标题被砍掉半句而没人说，用户会把它当成自己
    写的那一句发出去。
    """

    title: Optional[str] = None
    description: Optional[str] = None
    tags: Union[List[str], str, None] = None
    thumbnail_prompt: Optional[str] = None


class PublishTextRequest(BaseModel):
    """让模型按这条片子的画幅写一份标题、简介、标签和封面说明。

    ``language`` 的 ``auto`` 是"跟着主题和脚本的语言"。
    """

    language: Optional[str] = Field(default="auto", max_length=64)


class PublishThumbnailTextRequest(BaseModel):
    """压在封面上的那行字。

    尺寸和画幅不在这里：字号是画面高度的百分比，描边是字号的百分比，所以同
    一份设置在 9:16 和 16:9 上看起来一样大。只提交想改的字段。
    """

    text: Optional[str] = Field(default=None, max_length=120)
    font: Optional[str] = None
    position: Optional[str] = None
    size: Optional[float] = Field(default=None, ge=3, le=30)
    color: Optional[str] = None
    stroke_color: Optional[str] = None
    stroke_width: Optional[float] = Field(default=None, ge=0, le=40)
    band: Optional[bool] = None


class PublishSceneThumbnailRequest(BaseModel):
    """拿片子里已经有的一张画面当封面。

    ``index`` 是时间线上那一段的下标。``at`` 是从那个片段的第几秒取帧：素材
    多半是带运镜的 mp4，默认取第 0 秒，因为 zoom 是从 1.0 倍往里推的，第一帧
    是这张图最完整的样子。尺寸同样不在这里——它由这条片子的画幅决定。
    """

    index: int = Field(..., ge=0)
    at: Optional[float] = Field(default=0.0, ge=0)


class PublishThumbnailRequest(BaseModel):
    """画一张封面。

    ``prompt`` 不给就用已经存下来的那一段封面说明。图片的尺寸不在这里：它由
    这条片子的画幅决定，9:16 出竖图，16:9 出横图——让调用方指定它只会制造出
    一张会被 YouTube 裁掉的封面。
    """

    prompt: Optional[str] = Field(default=None, max_length=600)


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


class SceneRegenJobData(BaseModel):
    """排队中的一次重生成。``extra="allow"`` 的理由和场景一样：队列以后多带
    一个字段，不该让界面整个打不开。"""

    model_config = ConfigDict(extra="allow")

    id: str
    task_id: str = ""
    index: int = 0
    keyword: str = ""
    image_style: Optional[str] = None
    state: str = ""
    error: str = ""
    # 排在第几个，1 表示下一个就是它；正在出图的和已经结束的是 0。
    position: int = 0
    elapsed: float = 0.0
    waited: float = 0.0


class SceneRegenQueueData(BaseModel):
    model_config = ConfigDict(extra="allow")

    jobs: List[SceneRegenJobData] = []
    waiting: int = 0
    running: Optional[SceneRegenJobData] = None
    busy: bool = False


class SceneRegenJobResponse(BaseResponse):
    data: SceneRegenJobData


class SceneRegenQueueResponse(BaseResponse):
    data: SceneRegenQueueData


class SceneListResponse(BaseResponse):
    data: SceneListData


class SceneResponse(BaseResponse):
    data: SceneData
