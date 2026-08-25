#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="${AI_TUTOR_LOG_DIR:-/kaggle/working/ai-tutor-logs}"
RUNTIME_DIR="${AI_TUTOR_RUNTIME_DIR:-/kaggle/working/ai-tutor-runtime}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
PUBLIC_PORT="${PUBLIC_PORT:-7860}"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
OLLAMA_CHAT_MODEL="${OLLAMA_CHAT_MODEL:-hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M}"
OLLAMA_GENERATION_MODELS="${OLLAMA_GENERATION_MODELS:-$OLLAMA_CHAT_MODEL}"
DEFAULT_MODEL="${OLLAMA_DEFAULT_GENERATION_MODEL:-qwen-2.5-7b}"
AVAILABLE_MODELS="${AVAILABLE_MODELS:-$OLLAMA_GENERATION_MODELS}"
PRELOAD_ALL_MODELS="${PRELOAD_ALL_MODELS:-false}"
OLLAMA_EMBEDDING_MODEL="${OLLAMA_EMBEDDING_MODEL:-bge-m3}"
GGUF_MODEL_PATH="${GGUF_MODEL_PATH:-}"
RECREATE_OLLAMA_MODEL="${RECREATE_OLLAMA_MODEL:-0}"
REBUILD_CHROMA_ON_EMBEDDING_CHANGE="${REBUILD_CHROMA_ON_EMBEDDING_CHANGE:-1}"

export PROJECT_ROOT BACKEND_PORT FRONTEND_PORT PUBLIC_PORT OLLAMA_HOST
export OLLAMA_CHAT_MODEL OLLAMA_GENERATION_MODELS OLLAMA_DEFAULT_GENERATION_MODEL="$DEFAULT_MODEL" OLLAMA_EMBEDDING_MODEL
export AI_TUTOR_DATA_DIR="${AI_TUTOR_DATA_DIR:-$PROJECT_ROOT/data}"
export AI_TUTOR_VECTORSTORE_DIR="${AI_TUTOR_VECTORSTORE_DIR:-$PROJECT_ROOT/vectorstore}"
export AI_TUTOR_DATABASE_PATH="${AI_TUTOR_DATABASE_PATH:-$AI_TUTOR_DATA_DIR/conversations.db}"
export QUIZ_GENERATION_RETRY_LIMIT="${QUIZ_GENERATION_RETRY_LIMIT:-3}"

mkdir -p "$LOG_DIR" "$RUNTIME_DIR" "$AI_TUTOR_DATA_DIR" "$AI_TUTOR_VECTORSTORE_DIR"

log() { printf '[ai-tutor] %s\n' "$*"; }
tail_log() { [[ -f "$1" ]] && tail -n 80 "$1" >&2 || true; }
fail() { log "ERROR: $1"; [[ $# -lt 2 ]] || tail_log "$2"; exit 1; }

on_error() {
  local exit_code=$?
  log "Command failed at line $1 (exit $exit_code)."
  for file in ollama backend frontend nginx; do tail_log "$LOG_DIR/$file.log"; done
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

if ! command -v ollama >/dev/null 2>&1; then
  log "Ollama is missing; installing it once"
  curl -fsSL https://ollama.com/install.sh | sh
else
  log "Ollama is already installed"
fi

stop_pid() {
  local name="$1" pid_file="$RUNTIME_DIR/$1.pid"
  if [[ -s "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      log "Stopping old $name process ($pid)"
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi
}

wait_http() {
  local name="$1" url="$2" timeout="$3" log_file="$4" start
  start="$(date +%s)"
  until curl --fail --silent --show-error --max-time 3 "$url" >/dev/null 2>&1; do
    if (( $(date +%s) - start >= timeout )); then fail "$name did not become ready: $url" "$log_file"; fi
    sleep 1
  done
  log "$name is ready"
}

ollama_has_model() {
  ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$1" ||
    ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$1:latest"
}

prepare_kaggle_chroma() {
  local chroma_path="$AI_TUTOR_VECTORSTORE_DIR" marker current_model timestamp backup_path
  marker="$chroma_path/.embedding_model"

  case "$(realpath -m "$chroma_path")" in
    /kaggle/working/*) ;;
    *) fail "Refusing to manage Chroma outside /kaggle/working: $chroma_path" ;;
  esac
  mkdir -p "$chroma_path"
  if [[ -f "$marker" ]]; then
    current_model="$(tr -d '\r\n' <"$marker")"
    if [[ "$current_model" == "$OLLAMA_EMBEDDING_MODEL" ]]; then
      log "Chroma embedding marker matches: $current_model"
      return
    fi
  elif [[ -n "$(find "$chroma_path" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    current_model="<missing marker; existing model unknown>"
  else
    printf '%s\n' "$OLLAMA_EMBEDDING_MODEL" >"$marker"
    log "Initialized Chroma embedding marker: $OLLAMA_EMBEDDING_MODEL"
    return
  fi

  log "WARNING: Embedding model changed or cannot be verified"
  log "old: $current_model"
  log "new: $OLLAMA_EMBEDDING_MODEL"
  [[ "$REBUILD_CHROMA_ON_EMBEDDING_CHANGE" == "1" ]] ||
    fail "Chroma rebuild is disabled. Set REBUILD_CHROMA_ON_EMBEDDING_CHANGE=1 or provide a compatible database."

  timestamp="$(date +%Y%m%d-%H%M%S)"
  backup_path="${chroma_path}.backup-${timestamp}"
  [[ ! -e "$backup_path" ]] || backup_path="${backup_path}-$$"
  mv "$chroma_path" "$backup_path"
  mkdir -p "$chroma_path"
  printf '%s\n' "$OLLAMA_EMBEDDING_MODEL" >"$marker"
  log "Old ChromaDB moved to: $backup_path"

  # SQLite indexed_documents is authoritative. Clear only that derived registry
  # so incremental ingestion repopulates the rebuilt vectorstore.
  if [[ -f "$AI_TUTOR_DATABASE_PATH" ]]; then
    python - "$AI_TUTOR_DATABASE_PATH" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as connection:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='indexed_documents'"
    ).fetchone()
    if exists:
        connection.execute("DELETE FROM indexed_documents")
PY
    log "Cleared SQLite indexed_documents for embedding-model reindex"
  fi
}

warm_models_once() {
  local key marker
  key="$(printf '%s\n%s\n' "$OLLAMA_CHAT_MODEL" "$OLLAMA_EMBEDDING_MODEL" | sha256sum | awk '{print $1}')"
  marker="$RUNTIME_DIR/models-warmed-$key"
  if [[ -f "$marker" ]]; then
    log "Model warmup already completed in this runtime"
    return
  fi
  log "Warming chat model"
  curl --fail --silent --show-error --max-time 600 \
    -H 'Content-Type: application/json' "$OLLAMA_HOST/api/generate" \
    -d "$(python -c 'import json, os; print(json.dumps({"model": os.environ["OLLAMA_CHAT_MODEL"], "prompt": "Reply with OK.", "stream": False, "keep_alive": 0}))')" >/dev/null
  log "Warming embedding model"
  curl --fail --silent --show-error --max-time 180 \
    -H 'Content-Type: application/json' "$OLLAMA_HOST/api/embed" \
    -d "$(python -c 'import json, os; print(json.dumps({"model": os.environ["OLLAMA_EMBEDDING_MODEL"], "input": "AI Tutor embedding health check.", "keep_alive": "10m"}))')" \
    | python -c 'import json, sys; data=json.load(sys.stdin); assert data.get("embeddings"), "Embedding warmup returned no vector"'
  touch "$marker"
}

assert_health_models() {
  local url="$1" payload
  payload="$(curl --fail --silent --show-error --max-time 5 "$url")"
  printf '%s\n' "$payload"
  HEALTH_PAYLOAD="$payload" python -c 'import json, os, sys; data=json.loads(os.environ["HEALTH_PAYLOAD"]); expected=(os.environ["OLLAMA_CHAT_MODEL"], os.environ["OLLAMA_EMBEDDING_MODEL"]); actual=(data.get("chat_model"), data.get("embedding_model")); sys.exit(0 if data.get("status") == "ok" and actual == expected else f"Health model mismatch or degraded: expected={expected}, actual={actual}, response={data}")'
}

for service in nginx frontend backend ollama; do stop_pid "$service"; done
nginx -s stop -c "$PROJECT_ROOT/deployment/nginx.kaggle.conf" 2>/dev/null || true

log "Starting Ollama"
ollama serve >"$LOG_DIR/ollama.log" 2>&1 & echo $! >"$RUNTIME_DIR/ollama.pid"
wait_http "Ollama" "$OLLAMA_HOST/api/tags" 120 "$LOG_DIR/ollama.log"

if [[ "$OLLAMA_CHAT_MODEL" == hf.co/* ]] && ! ollama_has_model "$OLLAMA_CHAT_MODEL"; then
  log "Pulling Hugging Face chat model $OLLAMA_CHAT_MODEL"
  ollama pull "$OLLAMA_CHAT_MODEL" >>"$LOG_DIR/ollama.log" 2>&1
elif [[ "$RECREATE_OLLAMA_MODEL" == "1" ]] || ! ollama_has_model "$OLLAMA_CHAT_MODEL"; then
  [[ -n "$GGUF_MODEL_PATH" ]] || fail "GGUF_MODEL_PATH must be set because chat model '$OLLAMA_CHAT_MODEL' is absent."
  [[ -f "$GGUF_MODEL_PATH" ]] || fail "GGUF file does not exist: $GGUF_MODEL_PATH"
  MODELFILE="$RUNTIME_DIR/Modelfile"
  printf 'FROM "%s"\n\nPARAMETER temperature 0.1\nPARAMETER num_ctx 4096\n' "$GGUF_MODEL_PATH" >"$MODELFILE"
  log "Creating Ollama model $OLLAMA_CHAT_MODEL from the configured GGUF"
  ollama create "$OLLAMA_CHAT_MODEL" -f "$MODELFILE" >>"$LOG_DIR/ollama.log" 2>&1
fi

if [[ "$PRELOAD_ALL_MODELS" == "true" || "$PRELOAD_ALL_MODELS" == "1" ]]; then
  IFS=',' read -r -a generation_models <<< "$AVAILABLE_MODELS"
  for generation_model in "${generation_models[@]}"; do
    generation_model="${generation_model//[[:space:]]/}"
    [[ -z "$generation_model" || "$generation_model" == "$OLLAMA_CHAT_MODEL" ]] && continue
    if ollama_has_model "$generation_model"; then log "Generation model already exists: $generation_model"
    elif [[ "$generation_model" == hf.co/* ]]; then log "Preloading optional generation model $generation_model"; ollama pull "$generation_model" >>"$LOG_DIR/ollama.log" 2>&1
    else fail "Optional model '$generation_model' is absent and cannot be pulled automatically."
    fi
  done
else
  log "Optional generation models are lazy; set PRELOAD_ALL_MODELS=true only for benchmarking."
fi

if ! ollama_has_model "$OLLAMA_EMBEDDING_MODEL"; then
  log "Pulling embedding model $OLLAMA_EMBEDDING_MODEL"
  ollama pull "$OLLAMA_EMBEDDING_MODEL" >>"$LOG_DIR/ollama.log" 2>&1
else
  log "Embedding model already exists: $OLLAMA_EMBEDDING_MODEL"
fi

warm_models_once

prepare_kaggle_chroma

if [[ "${AI_TUTOR_AUTO_INDEX_DATA:-1}" == "1" ]]; then
  log "Incrementally indexing PDF/TXT files in $AI_TUTOR_DATA_DIR"
  cd "$PROJECT_ROOT"
  python -m backend.ingest >>"$LOG_DIR/backend.log" 2>&1
fi

log "Starting FastAPI"
cd "$PROJECT_ROOT"
python -m uvicorn backend.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$LOG_DIR/backend.log" 2>&1 &
echo $! >"$RUNTIME_DIR/backend.pid"
wait_http "FastAPI" "http://127.0.0.1:$BACKEND_PORT/api/health" 120 "$LOG_DIR/backend.log"
log "Backend health:"
assert_health_models "http://127.0.0.1:$BACKEND_PORT/api/health"

log "Starting frontend"
cd "$PROJECT_ROOT/frontend"
API_BASE_URL="" FRONTEND_HOST=127.0.0.1 node server.js >"$LOG_DIR/frontend.log" 2>&1 &
echo $! >"$RUNTIME_DIR/frontend.pid"
wait_http "Frontend" "http://127.0.0.1:$FRONTEND_PORT/" 60 "$LOG_DIR/frontend.log"

log "Starting Nginx"
nginx -c "$PROJECT_ROOT/deployment/nginx.kaggle.conf" >"$LOG_DIR/nginx.log" 2>&1
cp /kaggle/working/ai-tutor-runtime/nginx.pid "$RUNTIME_DIR/nginx.pid" 2>/dev/null || true
wait_http "Public gateway" "http://127.0.0.1:$PUBLIC_PORT/" 30 "$LOG_DIR/nginx.log"
wait_http "Proxied health endpoint" "http://127.0.0.1:$PUBLIC_PORT/api/health" 30 "$LOG_DIR/nginx.log"
log "Proxied health:"
assert_health_models "http://127.0.0.1:$PUBLIC_PORT/api/health"

log "All services are running"
for service in ollama backend frontend nginx; do
  pid="$(cat "$RUNTIME_DIR/$service.pid" 2>/dev/null || true)"
  printf '%-10s PID=%s status=%s\n' "$service" "${pid:-unknown}" "$([[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && echo running || echo unknown)"
done
ollama list
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null || log "GPU is not available"
