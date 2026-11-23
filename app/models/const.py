PUNCTUATIONS = [
    "?",
    ",",
    ".",
    "、",
    ";",
    ":",
    "!",
    "…",
    "？",
    "，",
    "。",
    "、",
    "；",
    "：",
    "！",
    "...",
    # 阿拉伯语常用标点也应作为自然断句点，避免脚本文本和 edge-tts
    # 返回的字幕停顿边界不一致，导致后续逐行匹配失败。
    "،",
    "؛",
    "؟",
    # Full stops of scripts that do not use ".": Devanagari and Bengali danda,
    # Urdu, Burmese, Khmer and Ethiopic. Edge TTS word boundaries leave these
    # out or attach them to the last word, so they must end script lines too.
    "।",
    "॥",
    "۔",
    "။",
    "។",
    "።",
]

TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1
TASK_STATE_PROCESSING = 4

CROSS_POST_STATE_PENDING = "pending"
CROSS_POST_STATE_PROCESSING = "processing"
CROSS_POST_STATE_COMPLETE = "complete"
CROSS_POST_STATE_FAILED = "failed"

FILE_TYPE_VIDEOS = ["mp4", "mov", "mkv", "webm"]
FILE_TYPE_IMAGES = ["jpg", "jpeg", "png", "bmp"]


# -----------------------------------------------------------------------------
# Local-first defaults / 本地优先默认值
# -----------------------------------------------------------------------------
# What a fresh install does before anybody has typed in a key: generate the
# whole video on this machine. Scripts come from Ollama, pictures from a local
# OpenAI-compatible image server (see local_services/genserver), narration from
# a local Chatterbox server, subtitles from local Whisper. None of it reaches a
# paid API, so the first run works with no account anywhere.
#
# These are *defaults*, not a policy: every hosted provider is still there and
# still works the moment it is chosen. An existing config.toml is never
# rewritten — these only fill in a value that is missing.
#
# config.local.example.toml is the same setup as a ready-made file, and
# scripts/start-local.sh checks that the three services are up.
DEFAULT_VIDEO_SOURCE = "openai_image"
DEFAULT_VOICE_NAME = "chatterbox:default-Female"
# The image endpoint local_services/genserver listens on, and its one model.
DEFAULT_OPENAI_IMAGE_BASE_URL = "http://127.0.0.1:8730/v1"
DEFAULT_OPENAI_IMAGE_MODEL = "zimage-turbo"
# WebUI 的默认配音服务。Chatterbox 跑在本机容器里，不需要密钥；它用显卡，
# 所以容器只在配音期间运行（见 app/services/chatterbox_server.py）。
# The WebUI's default voiceover service. Chatterbox runs in a local container
# and needs no key. It does use the graphics card, so its container only runs
# while a narration records (app/services/chatterbox_server.py). Every hosted
# service in the selector still works when picked.
DEFAULT_TTS_SERVER = "chatterbox"
