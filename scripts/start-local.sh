#!/usr/bin/env bash
#
# Bring up the three local services, then the app (the editor, on one port).
#
#   scripts/start-local.sh            check, start what is missing, open the editor
#   scripts/start-local.sh --check    check only, report, exit non-zero if anything is down
#   scripts/start-local.sh --webui    also start the old WebUI on :8501
#
# Everything is done in the editor now — starting a video, the settings, the
# timeline — so the old WebUI is no longer started by default. It is still
# there and still works: --webui brings it up exactly as before. (--no-webui
# is accepted and does nothing; it is what the default now is.)
#
# The services:
#
#   Ollama     127.0.0.1:11434   the script and the keywords          GPU
#   genserver  127.0.0.1:8730    the pictures (Z-Image-Turbo)         GPU
#   Kokoro     127.0.0.1:8880    the narration                        CPU
#
# Chatterbox (127.0.0.1:4123, GPU) is the other narration voice. It is only
# checked, never started here: the app runs its container per narration.
#
# Whisper is a library, not a service: it loads inside the app when the
# subtitle stage runs.
#
# A service that is already up is left exactly as it is — this never restarts
# something that is working. Anything this script starts itself is stopped when
# it exits; anything that was already running is left alone.
#
# What is *not* working is cleared away first: a process of ours holding one of
# these ports while answering nothing is the reason the next start fails. Only
# this project's own processes, only when their health check does not answer.
# See reap_stale below; KEEP_STALE=1 turns it off.
#
# Each check prints what to do about a failure rather than only that it failed.
# With no config.toml, it offers to copy config.local.example.toml.

set -u

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$CURRENT_DIR" || exit 1

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
GEN_URL="${GEN_URL:-http://127.0.0.1:8730}"
KOKORO_URL="${KOKORO_URL:-http://127.0.0.1:8880}"
KOKORO_CONTAINER="${KOKORO_CONTAINER:-docreel-kokoro}"
GEN_STARTUP_TIMEOUT="${GEN_STARTUP_TIMEOUT:-120}"
# Appended to, not truncated: a crash is usually understood by comparing the
# run that died with the one before it.
GEN_LOG="${GEN_LOG:-$CURRENT_DIR/storage/logs/genserver.log}"

# Keep the model loaded. This used to default to 600, to hand back the ~16 GB
# of *host* RAM that CPU offload holds while a model is resident, and that was
# a mistake on two counts.
#
# The crash: loading the pipeline a second time in one process segfaults inside
# from_pretrained — reproduced on demand as generate, idle unload, generate,
# dying at "Loading pipeline components". So an unload does not cost the next
# video a reload, it costs it the server. The app then reports every image as
# "HTTP 500" or a reset connection and the task fails at the materials stage.
#
# The premise: the memory pressure this was meant to relieve turned out to be a
# second genserver holding 37.6 GB of commit charge, not the resident model.
#
# A video generates its images consecutively, so with no idle unload the model
# is loaded exactly once and the reload path is never taken. Measured that way:
# 8 images back to back, 8/8, server healthy afterwards.
#
# The cost is ~16 GB of host RAM held between videos. Set GEN_IDLE_SECONDS to a
# number of seconds to get it back, knowing the next video has to survive a
# reload to use it.
export GEN_IDLE_SECONDS="${GEN_IDLE_SECONDS:-0}"

CHECK_ONLY=0
START_WEBUI=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1; START_WEBUI=0 ;;
    --webui) START_WEBUI=1 ;;
    --no-webui) START_WEBUI=0 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^#\{1,2\} \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Colour only when stdout is a terminal, so a log file stays readable.
if [ -t 1 ]; then
  RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
  YELLOW=$(printf '\033[33m'); DIM=$(printf '\033[2m'); OFF=$(printf '\033[0m')
else
  RED=''; GREEN=''; YELLOW=''; DIM=''; OFF=''
fi

ok()    { printf '%s  ok  %s %s\n' "$GREEN" "$OFF" "$1"; }
warn()  { printf '%s wait %s %s\n' "$YELLOW" "$OFF" "$1"; }
fail()  { printf '%s fail %s %s\n' "$RED" "$OFF" "$1"; }
detail(){ printf '       %s%s%s\n' "$DIM" "$1" "$OFF"; }

FAILED=0
STARTED_PIDS=''

# `kill` on a native Windows process from Git Bash can report success and
# leave the process running, because the MSYS pid is not the Windows pid. A
# genserver that survives that way is not merely untidy: it holds its share of
# the ~16 GB of host RAM that CPU offload needs, and a later load then fails
# with "the paging file is too small" (os error 1455), which arrives at the app
# as "HTTP 500: MemoryError".
stop_pid() {
  kill "$1" 2>/dev/null
  if command -v taskkill >/dev/null 2>&1; then
    winpid=$(ps -p "$1" -o winpid= 2>/dev/null | tr -d ' ')
    if [ -n "$winpid" ]; then
      taskkill //F //T //PID "$winpid" >/dev/null 2>&1
    fi
  fi
  return 0
}

cleanup() {
  for pid in $STARTED_PIDS; do
    stop_pid "$pid"
  done
}
trap cleanup EXIT INT TERM

port_owned() {
  # port_owned PORT -> 0 when something already accepts connections there.
  # "/health answers" is a different question: a genserver still importing
  # torch owns the port for a minute before it answers anything, and so does a
  # wedged one. Starting a second server in either window is what produced two
  # processes competing for the same host memory.
  if [ -z "$PYTHON_BIN" ]; then
    return 1
  fi
  "$PYTHON_BIN" - "$1" <<'PY' 2>/dev/null
import socket, sys
probe = socket.socket()
probe.settimeout(1.5)
try:
    probe.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    probe.close()
PY
}

# --- a portable GET, without assuming curl -----------------------------------

PYTHON_BIN=''
for candidate in "$CURRENT_DIR/.venv/Scripts/python.exe" "$CURRENT_DIR/.venv/bin/python" python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
    PYTHON_BIN="$candidate"
    break
  fi
done

http_ok() {
  # http_ok URL [TIMEOUT] -> 0 when the request returned a 2xx
  url=$1
  timeout=${2:-3}
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m "$timeout" -o /dev/null "$url" 2>/dev/null
    return $?
  fi
  if [ -n "$PYTHON_BIN" ]; then
    "$PYTHON_BIN" - "$url" "$timeout" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2])) as r:
        sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception:
    sys.exit(1)
PY
    return $?
  fi
  echo "neither curl nor python is available to check $url" >&2
  return 1
}

wait_for() {
  # wait_for URL SECONDS
  url=$1
  deadline=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if http_ok "$url" 2; then return 0; fi
    sleep 1
  done
  return 1
}


# --- leftovers from a previous run -------------------------------------------
#
# The rule above - "a service that is already up is left exactly as it is" -
# is about services that *work*. A process that owns the port and answers
# nothing is not a running service, it is the reason the next start fails, and
# until now the script only printed the taskkill line for you to copy.
#
# What may be stopped here is deliberately narrow. All three have to hold:
#
#   1. it owns a port this script manages;
#   2. its own health check does not answer, or answers "not ok";
#   3. its command line belongs to *this* project.
#
# (3) is the one that matters. Something else on :8080 is somebody else's
# program, and a start script that kills unknown processes to free a port it
# wants is a worse problem than the one it solves. Those are reported, never
# killed. Ollama and Kokoro are not touched either way: this script does not
# own them, it only checks whether they answer.
#
# KEEP_STALE=1 turns all of this off.

KEEP_STALE="${KEEP_STALE:-0}"

pid_on_port() {
  # pid_on_port PORT -> the OS pid listening there, if any.
  if [ "${OS:-}" = "Windows_NT" ] && command -v netstat >/dev/null 2>&1; then
    netstat -ano 2>/dev/null | tr -d '\r' \
      | awk -v want=":$1" '$1 == "TCP" && $4 == "LISTENING" {
          n = split($2, a, ":"); if (":" a[n] == want) { print $5; exit }
        }'
    return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1
  fi
}

command_of() {
  # command_of PID -> its command line, as one line.
  if [ "${OS:-}" = "Windows_NT" ] && command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -Command \
      "(Get-CimInstance Win32_Process -Filter \"ProcessId=$1\").CommandLine" \
      2>/dev/null | tr -d '\r' | tr '\n' ' '
    return 0
  fi
  ps -o args= -p "$1" 2>/dev/null | tr '\n' ' '
}

ours() {
  # ours PID -> 0 when that command line is one of this project's own.
  # Matched on the script paths this repo starts, not on "python": the point
  # is to be sure before stopping anything.
  case "$(command_of "$1")" in
    *local_services/genserver/server.py*|*local_services\\genserver\\server.py*) return 0 ;;
    *webui/Main.py*|*webui\\Main.py*) return 0 ;;
    *"$CURRENT_DIR"*main.py*) return 0 ;;
    *) return 1 ;;
  esac
}

stop_os_pid() {
  # stop_os_pid PID — an OS pid, not the MSYS one that stop_pid takes.
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //F //T //PID "$1" >/dev/null 2>&1 && return 0
  fi
  kill -9 "$1" 2>/dev/null
}

answers_well() {
  # answers_well URL [JSON_OK_FIELD] -> 0 when it replies, and when the field
  # is given, when that field is true. The genserver reports ok:false after it
  # has lost the GPU or outgrown its memory ceiling; it answers every request
  # in that state and generates nothing, so "it replied" is not enough.
  http_ok "$1" 3 || return 1
  [ -z "${2:-}" ] && return 0
  [ -z "$PYTHON_BIN" ] && return 0
  "$PYTHON_BIN" - "$1" "$2" <<'PY' 2>/dev/null
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as answer:
        sys.exit(0 if json.load(answer).get(sys.argv[2]) else 1)
except Exception:
    sys.exit(0)  # unreadable is not the same as unhealthy; leave it alone
PY
}

reap_stale() {
  # reap_stale PORT URL LABEL [JSON_OK_FIELD]
  [ "$KEEP_STALE" = "1" ] && return 0
  pid=$(pid_on_port "$1")
  [ -z "$pid" ] && return 0
  answers_well "$2" "${4:-}" && return 0

  if ! ours "$pid"; then
    warn "something that is not ours owns :$1 and is not answering (pid $pid)"
    detail "this script will not kill a process it does not recognise"
    detail "  $(command_of "$pid" | cut -c1-70)"
    return 0
  fi
  warn "a $3 from an earlier run is on :$1 and not answering — stopping it"
  detail "pid $pid"
  stop_os_pid "$pid"
  # Give the port a moment to come back before the caller tests it again.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(pid_on_port "$1")" ] && break
    sleep 1
  done
  if [ -n "$(pid_on_port "$1")" ]; then
    fail "could not free :$1 (pid $pid is still there)"
    detail "stop it by hand, then run this script again"
    FAILED=1
  else
    ok "freed :$1"
  fi
}

echo
echo "MoneyPrinterTurbo — local services"
echo

# --- config ------------------------------------------------------------------

if [ -f config.toml ]; then
  ok "config.toml"
else
  fail "config.toml is missing"
  detail "cp config.local.example.toml config.toml   # the all-local setup"
  detail "cp config.example.toml config.toml         # the stock defaults"
  FAILED=1
fi

# --- 1. Ollama ---------------------------------------------------------------

if http_ok "$OLLAMA_URL/api/tags"; then
  ok "ollama        $OLLAMA_URL"
elif ! command -v ollama >/dev/null 2>&1; then
  fail "ollama is not installed"
  detail "install it from https://ollama.com/download, then: ollama pull qwen3:14b"
  FAILED=1
elif [ "$CHECK_ONLY" = "1" ]; then
  fail "ollama is installed but not answering on $OLLAMA_URL"
  detail "start it with: ollama serve"
  FAILED=1
else
  warn "starting ollama"
  ollama serve >/dev/null 2>&1 &
  STARTED_PIDS="$STARTED_PIDS $!"
  if wait_for "$OLLAMA_URL/api/tags" 30; then
    ok "ollama        $OLLAMA_URL"
  else
    fail "ollama did not come up within 30s"
    detail "run 'ollama serve' in another terminal to see why"
    FAILED=1
  fi
fi

# The daemon being up is not the same as a model being installed.
if http_ok "$OLLAMA_URL/api/tags" && [ -n "$PYTHON_BIN" ]; then
  configured_model=$("$PYTHON_BIN" - <<'PY' 2>/dev/null
import re
try:
    text = open("config.toml", encoding="utf8").read()
except OSError:
    text = ""
match = re.search(r'^\s*ollama_model_name\s*=\s*"([^"]*)"', text, re.M)
print(match.group(1) if match else "")
PY
)
  if [ -n "$configured_model" ]; then
    if http_ok "$OLLAMA_URL/api/tags" && \
       "$PYTHON_BIN" - "$OLLAMA_URL" "$configured_model" <<'PY' 2>/dev/null
import json, sys, urllib.request
url, want = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
    names = [m.get("name", "") for m in json.load(r).get("models", [])]
# ollama list shows "name:tag"; a bare name means the "latest" tag.
sys.exit(0 if want in names or f"{want}:latest" in names else 1)
PY
    then
      ok "  model       $configured_model"
    else
      fail "  ollama_model_name \"$configured_model\" is not installed"
      detail "ollama pull $configured_model     # or set another from 'ollama list'"
      FAILED=1
    fi
  else
    warn "  ollama_model_name is not set in config.toml"
    detail "pick one from 'ollama list' and set [app] ollama_model_name"
  fi
fi

# --- 2. genserver ------------------------------------------------------------

reap_stale 8730 "$GEN_URL/health" "genserver" "ok"
if http_ok "$GEN_URL/health" && answers_well "$GEN_URL/health" "ok"; then
  ok "genserver     $GEN_URL"
else
  # In order: an interpreter named outright in GEN_PYTHON, this repo's own
  # venv, then the venv of the video-generator checkout genserver was copied
  # from, which is where it already exists if that project was set up first.
  # Never a bare `python`: a system interpreter usually carries a torch built
  # for a different GPU architecture, and that one imports cleanly, reports
  # CUDA available, loads 16 GB of weights into host RAM, and only then dies on
  # every kernel it tries to run.
  for candidate in \
    "${GEN_PYTHON:-}" \
    "local_services/genserver/.venv/Scripts/python.exe" \
    "local_services/genserver/.venv/bin/python" \
    "$CURRENT_DIR/../video-generator/scripts/genserver/.venv/Scripts/python.exe" \
    "$CURRENT_DIR/../video-generator/scripts/genserver/.venv/bin/python"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      GEN_PYTHON="$candidate"
      break
    fi
  done

  if [ -z "$GEN_PYTHON" ]; then
    fail "genserver has no virtual environment"
    detail "python -m venv local_services/genserver/.venv"
    detail "local_services/genserver/.venv/Scripts/python -m pip install torch --index-url https://download.pytorch.org/whl/cu128"
    detail "local_services/genserver/.venv/Scripts/python -m pip install -r local_services/genserver/requirements.txt"
    detail "see local_services/genserver/README.md (use the cu128 index on Blackwell)"
    FAILED=1
  elif port_owned 8730; then
    # Deliberately not a start. Two genservers is the worst outcome available
    # here, worse than none: the second one's commit charge is what makes the
    # first one's model load fail, and the error surfaces much later, during
    # the materials stage, as MemoryError on every image.
    fail "something already owns 127.0.0.1:8730 but is not answering /health"
    detail "it may still be importing torch — give it a minute and check again"
    detail "otherwise find and stop it, then run this script again:"
    detail "  netstat -ano | grep 8730      # the last column is the Windows pid"
    detail "  taskkill //F //PID <pid>"
    FAILED=1
  elif [ "$CHECK_ONLY" = "1" ]; then
    fail "genserver is not answering on $GEN_URL"
    detail "start it with: PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True $GEN_PYTHON local_services/genserver/server.py"
    FAILED=1
  else
    warn "starting genserver (importing torch takes a moment)"
    # Keep the output. This used to go to /dev/null, and when the server died
    # mid-request there was no traceback, no exit code and no last line to
    # work from -- for a process whose failure modes so far are a segfault and
    # an out-of-memory during the model load, that is the one thing worth
    # having. storage/ is gitignored, so the log stays out of commits.
    mkdir -p storage/logs
    printf '\n===== started %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" \
      >> "$GEN_LOG"
    # expandable_segments is not optional: a model that is offloaded and
    # reloaded every call otherwise leaves the allocator holding blocks of the
    # wrong shape, and a later generation dies on a small allocation.
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
      "$GEN_PYTHON" local_services/genserver/server.py >> "$GEN_LOG" 2>&1 &
    GEN_PID=$!
    STARTED_PIDS="$STARTED_PIDS $GEN_PID"
    if wait_for "$GEN_URL/health" "$GEN_STARTUP_TIMEOUT"; then
      ok "genserver     $GEN_URL"
      detail "log: $GEN_LOG"
    else
      # Stop it here rather than leaving it to the exit trap: a half-started
      # genserver may already hold gigabytes, and when the WebUI starts below
      # this script stays alive for as long as the session lasts.
      stop_pid "$GEN_PID"
      fail "genserver did not come up within ${GEN_STARTUP_TIMEOUT}s"
      detail "the last lines it wrote:"
      tail -n 15 "$GEN_LOG" 2>/dev/null | sed "s/^/       ${DIM}/;s/$/${OFF}/"
      detail "full log: $GEN_LOG"
      FAILED=1
    fi
  fi
fi

# A genserver that is up can still be a genserver that cannot run a kernel.
if http_ok "$GEN_URL/health" && [ -n "$PYTHON_BIN" ]; then
  "$PYTHON_BIN" - "$GEN_URL" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f"{sys.argv[1]}/health", timeout=5) as r:
    health = json.load(r)
torch_info, device = health.get("torch", {}), health.get("device", {})
if torch_info.get("importError"):
    print(f"       torch did not import: {torch_info['importError']}")
    sys.exit(0)
if not torch_info.get("available"):
    print("       ! torch reports no usable GPU; generation will fail")
    sys.exit(0)
arch, capability = torch_info.get("archList") or [], device.get("capability")
if capability and arch and f"sm_{capability.replace('.', '')}" not in arch:
    print(f"       ! this torch wheel is built for {', '.join(arch)} and the card is "
          f"sm_{capability.replace('.', '')}: every kernel will fail")
    sys.exit(0)
free, total = device.get("freeMb"), device.get("totalMb")
print(f"       {device.get('name')}, {free} of {total} MB free, "
      f"resident: {health.get('resident') or 'nothing'}")
PY
fi

# A running genserver that config.toml does not point at is the same as no
# genserver, and it fails much later: the run spends an LLM call and a full
# TTS pass before the materials stage says "openai_image_base_url is not set".
# There is deliberately no default for it in the code (the endpoint may be a
# paid relay, so it has to be stated), and config.example.toml ships these two
# lines -- but a config.toml the WebUI has rewritten keeps only the keys that
# were in memory, so a config predating the image source silently loses them.
if [ -n "$PYTHON_BIN" ]; then
  missing_image_keys=$("$PYTHON_BIN" - <<'PY' 2>/dev/null
import re
try:
    text = open("config.toml", encoding="utf8").read()
except OSError:
    text = ""


def value(key):
    match = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', text, re.M)
    return (match.group(1) if match else "").strip()


if value("video_source") == "openai_image":
    for key in ("openai_image_base_url", "openai_image_model"):
        if not value(key):
            print(key)
PY
)
  if [ -n "$missing_image_keys" ]; then
    warn "config.toml does not say where to generate images"
    detail "video_source is \"openai_image\" but these are unset in [app]:"
    for key in $missing_image_keys; do
      detail "  $key"
    done
    detail "for the local genserver, add to [app] in config.toml:"
    detail "  openai_image_base_url = \"$GEN_URL/v1\""
    detail "  openai_image_model = \"zimage-turbo\""
  fi
fi

# --- 3. Kokoro ---------------------------------------------------------------

if http_ok "$KOKORO_URL/v1/audio/voices" 10; then
  ok "kokoro        $KOKORO_URL"
elif ! command -v docker >/dev/null 2>&1; then
  fail "kokoro is not answering and docker is not installed"
  detail "install Docker, or run any OpenAI-compatible Kokoro server on port 8880"
  detail "then point [kokoro] base_url at it (it must end in /v1)"
  FAILED=1
elif [ "$CHECK_ONLY" = "1" ]; then
  fail "kokoro is not answering on $KOKORO_URL"
  detail "docker start $KOKORO_CONTAINER"
  FAILED=1
elif docker inspect "$KOKORO_CONTAINER" >/dev/null 2>&1; then
  warn "starting the $KOKORO_CONTAINER container"
  docker start "$KOKORO_CONTAINER" >/dev/null 2>&1
  if wait_for "$KOKORO_URL/v1/audio/voices" 60; then
    ok "kokoro        $KOKORO_URL"
  else
    fail "the $KOKORO_CONTAINER container did not start serving within 60s"
    detail "docker logs --tail 50 $KOKORO_CONTAINER"
    FAILED=1
  fi
else
  fail "no container named $KOKORO_CONTAINER"
  detail "create one (CPU build; it never competes for the GPU):"
  detail "  docker run -d --name $KOKORO_CONTAINER -p 8880:8880 \\"
  detail "    ghcr.io/remsky/kokoro-fastapi-cpu:latest"
  FAILED=1
fi

if http_ok "$KOKORO_URL/v1/audio/voices" 10 && [ -n "$PYTHON_BIN" ]; then
  "$PYTHON_BIN" - "$KOKORO_URL" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f"{sys.argv[1]}/v1/audio/voices", timeout=10) as r:
    data = json.load(r)
voices = data.get("voices", data) if isinstance(data, dict) else data
names = [v.get("id") if isinstance(v, dict) else v for v in voices]
names = [n for n in names if n]
print(f"       {len(names)} voices, including {', '.join(names[:4])}")
PY
fi

# --- 3b. Chatterbox (only when config.toml names its container) --------------
# Not started here, on purpose. It runs on the graphics card and cannot drop
# its model, so the app starts the container for each narration and stops it
# straight after (app/services/chatterbox_server.py). All this checks is that
# the container exists to be started.

CHATTERBOX_CONTAINER=""
if [ -n "$PYTHON_BIN" ]; then
  CHATTERBOX_CONTAINER=$("$PYTHON_BIN" - <<'PY' 2>/dev/null
from app.config import config
print(str(config.chatterbox.get("container", "") or "").strip())
PY
)
fi

if [ -n "$CHATTERBOX_CONTAINER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    fail "[chatterbox] container is set but docker is not installed"
    FAILED=1
  elif docker inspect "$CHATTERBOX_CONTAINER" >/dev/null 2>&1; then
    ok "chatterbox    container $CHATTERBOX_CONTAINER (started per narration)"
  else
    fail "no container named $CHATTERBOX_CONTAINER"
    detail "build and create it — the commands are at the top of"
    detail "local_services/chatterbox/Dockerfile — or clear [chatterbox] container"
    FAILED=1
  fi
fi

echo

if [ "$FAILED" != "0" ]; then
  fail "something above needs attention; the WebUI was not started"
  echo
  exit 1
fi

ok "all local services are up — nothing in this path calls a paid API"
echo

if [ "$CHECK_ONLY" = "1" ]; then
  exit 0
fi

# --- 4. the API server -------------------------------------------------------
# The WebUI runs the pipeline in its own process and does not need this. The
# timeline editor does: it is a page served by the API, talking to the API's
# routes, so without this step its link from the review panel goes nowhere.
#
# Started before the WebUI because the next step `exec`s into webui.sh and
# never comes back here.

API_PORT=$("$PYTHON_BIN" - <<'PY' 2>/dev/null || echo 8080
from app.config import config
print(config.listen_port)
PY
)
API_PORT=${API_PORT:-8080}

reap_stale "$API_PORT" "http://127.0.0.1:$API_PORT/docs" "API server"
if port_owned "$API_PORT"; then
  ok "API server already on :$API_PORT"
elif [ -z "$PYTHON_BIN" ]; then
  fail "no Python found; cannot start the app"
  API_FAILED=1
else
  mkdir -p storage/logs
  "$PYTHON_BIN" main.py >> storage/logs/api.log 2>&1 &
  STARTED_PIDS="$STARTED_PIDS $!"
  if wait_for "http://127.0.0.1:$API_PORT/docs" 40; then
    ok "API server on :$API_PORT"
  else
    fail "the app did not answer on :$API_PORT"
    detail "see storage/logs/api.log"
    API_FAILED=1
  fi
fi
EDITOR_URL="http://127.0.0.1:$API_PORT/editor/"
echo

if [ "$START_WEBUI" != "1" ]; then
  # The editor is the app. If it is not up there is nothing to open, and
  # saying "ready" would be a lie: say what failed and stop.
  if [ "${API_FAILED:-0}" = "1" ]; then
    fail "the app is not running, so there is nothing to open"
    detail "the old WebUI does not need it: scripts/start-local.sh --webui"
    exit 1
  fi
  echo "Ready: $EDITOR_URL"
  echo
  # Open it, the way the WebUI used to open itself. NO_BROWSER=1 to skip.
  if [ "${NO_BROWSER:-0}" != "1" ]; then
    if command -v cmd.exe >/dev/null 2>&1; then
      cmd.exe //c start "" "$EDITOR_URL" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
      open "$EDITOR_URL" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$EDITOR_URL" >/dev/null 2>&1 || true
    fi
  fi
  if [ -n "$STARTED_PIDS" ]; then
    echo "Services this script started will stop when it exits. Ctrl-C when done."
    echo
    wait
  fi
  exit 0
fi

# With --webui a missing API is not fatal: one-click generation in the WebUI
# never touches it, so refusing to start the WebUI over this would take away
# the part that does work.
if [ "${API_FAILED:-0}" = "1" ]; then
  detail "the WebUI still works; the editor will not"
else
  detail "editor: $EDITOR_URL"
fi
echo

# --- 5. the WebUI ------------------------------------------------------------

echo "starting the WebUI..."
echo
exec ./webui.sh
