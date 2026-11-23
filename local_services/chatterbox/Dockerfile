# Chatterbox narration server for this project, for RTX 50-series cards.
#
# This is travisvn/chatterbox-tts-api's docker/Dockerfile.blackwell with one
# change: Python comes from uv instead of the deadsnakes PPA. The PPA answered
# 503 for a whole afternoon and the upstream build cannot finish without it.
#
# The build context is the upstream repository, not this folder, so the file
# is fed on stdin (run from the repository root):
#
#   docker build -t docreel-chatterbox -f- \
#     "https://github.com/travisvn/chatterbox-tts-api.git#a5f466128e4baa8e4cceb3bba9b7ca9de6f7ec6b" \
#     < local_services/chatterbox/Dockerfile
#
# Then create the container without starting it; the app starts it for each
# narration and stops it after (app/services/chatterbox_server.py):
#
#   docker create --name docreel-chatterbox --gpus all -p 127.0.0.1:4123:4123 \
#     -v docreel-chatterbox-models:/cache -v docreel-chatterbox-voices:/voices \
#     docreel-chatterbox
#
# Older cards: torch 2.7 / cu128 runs on them too, so this image still works.

FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    curl \
    build-essential \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# uv downloads its own Python 3.11 build; nothing comes from a PPA.
RUN uv venv --python 3.11
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# One layer and no uv cache: as separate steps this was three 6.5 GB layers,
# two of them copies of torch that the last step replaces.
# torch goes in again after chatterbox, whose dependencies swap the CUDA build
# for another one.
# setuptools is held below 81: later ones drop pkg_resources, without which
# resemble-perth (the watermarker) imports as None and the model fails to load
# with "'NoneType' object is not callable".
RUN uv pip install --no-cache "setuptools<81" fastapi uvicorn[standard] python-dotenv python-multipart requests psutil pydub sse-starlette \
    && uv pip install --no-cache git+https://github.com/travisvn/chatterbox-multilingual.git@exp \
    && uv pip install --no-cache torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 --index-url https://download.pytorch.org/whl/cu128 \
    && uv pip install --no-cache "setuptools<81" \
    && python -c "import perth; assert perth.PerthImplicitWatermarker is not None, 'watermarker missing'"

COPY app/ ./app/
COPY main.py ./
COPY voice-sample.mp3 ./voice-sample.mp3

RUN mkdir -p /cache /voices /data/long_text_jobs

ENV PORT=4123
ENV HOST=0.0.0.0
ENV DEVICE=cuda
ENV MODEL_CACHE_DIR=/cache
# The weights come from Hugging Face, which ignores MODEL_CACHE_DIR and would
# put 3 GB in the container's own layer, lost whenever it is recreated.
ENV HF_HOME=/cache/huggingface
ENV VOICE_LIBRARY_DIR=/voices
ENV VOICE_SAMPLE_PATH=/app/voice-sample.mp3
ENV LONG_TEXT_DATA_DIR=/data/long_text_jobs
# English only: the multilingual model is larger and reads English less well.
ENV USE_MULTILINGUAL_MODEL=false
ENV EXAGGERATION=0.5
ENV CFG_WEIGHT=0.5
ENV TEMPERATURE=0.8
ENV MAX_CHUNK_LENGTH=280
ENV MAX_TOTAL_LENGTH=3000
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

EXPOSE 4123

CMD ["python", "main.py"]
