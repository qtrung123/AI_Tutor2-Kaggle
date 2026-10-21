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
# Configured but lazy: pulled on demand (see /api/models/{id}/prepare) the first time a user
# actually selects and uses it, never pulled or warmed here at startup. An official Ollama library
# reference, not Hugging Face GGUF. (DeepSeek and GLM are temporarily inactive: not configured here.)
OLLAMA_GEMMA3_12B_MODEL="${OLLAMA_GEMMA3_12B_MODEL:-gemma3:12b-it-q4_K_M}"
OLLAMA_GENERATION_MODELS="${OLLAMA_GENERATION_MODELS:-}"
# Chat backend: "ollama" (default) or "vllm" (Qwen 2.5 7B on a local vLLM server, see
# deployment/start_vllm.sh). With vllm, Ollama only serves embeddings and Gemma is not offered.
LLM_BACKEND="${LLM_BACKEND:-ollama}"
VLLM_PORT="${VLLM_PORT:-8001}"
VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:$VLLM_PORT/v1}"
if [[ "$LLM_BACKEND" == "vllm" ]]; then
  OLLAMA_GENERATION_MODELS="qwen-2.5-7b"
else
  # Repairs the app-level model allowlist only (which models the Study Session selector offers and
  # resolves) - it does not affect what gets pulled or warmed at startup, which is Qwen alone below.
  for required_model_id in qwen-2.5-7b gemma3-12b; do
    case ",$OLLAMA_GENERATION_MODELS," in
      *",$required_model_id,"*) ;;
      *) OLLAMA_GENERATION_MODELS="${OLLAMA_GENERATION_MODELS:+$OLLAMA_GENERATION_MODELS,}$required_model_id" ;;
    esac
  done
fi
DEFAULT_MODEL="${OLLAMA_DEFAULT_GENERATION_MODEL:-qwen-2.5-7b}"
QUIZ_DEFAULT_MODEL="${OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL:-qwen-2.5-7b}"
# Only consulted when PRELOAD_ALL_MODELS=true (benchmarking): every configured model, pulled once
# up front instead of lazily. Startup itself only ever pulls/warms $OLLAMA_CHAT_MODEL.
AVAILABLE_MODELS="${AVAILABLE_MODELS:-$OLLAMA_CHAT_MODEL,$OLLAMA_GEMMA3_12B_MODEL}"
PRELOAD_ALL_MODELS="${PRELOAD_ALL_MODELS:-false}"
OLLAMA_EMBEDDING_MODEL="${OLLAMA_EMBEDDING_MODEL:-bge-m3}"
GGUF_MODEL_PATH="${GGUF_MODEL_PATH:-}"
RECREATE_OLLAMA_MODEL="${RECREATE_OLLAMA_MODEL:-0}"
REBUILD_CHROMA_ON_EMBEDDING_CHANGE="${REBUILD_CHROMA_ON_EMBEDDING_CHANGE:-1}"

export PROJECT_ROOT BACKEND_PORT FRONTEND_PORT PUBLIC_PORT OLLAMA_HOST
export LLM_BACKEND VLLM_PORT VLLM_BASE_URL
export OLLAMA_CHAT_MODEL OLLAMA_GEMMA3_12B_MODEL
export OLLAMA_GENERATION_MODELS OLLAMA_DEFAULT_GENERATION_MODEL="$DEFAULT_MODEL"
export OLLAMA_QUIZ_DEFAULT_GENERATION_MODEL="$QUIZ_DEFAULT_MODEL" OLLAMA_EMBEDDING_MODEL
export AI_TUTOR_DATA_DIR="${AI_TUTOR_DATA_DIR:-$PROJECT_ROOT/data}"
export AI_TUTOR_VECTORSTORE_DIR="${AI_TUTOR_VECTORSTORE_DIR:-$PROJECT_ROOT/vectorstore}"
export AI_TUTOR_DATABASE_PATH="${AI_TUTOR_DATABASE_PATH:-$AI_TUTOR_DATA_DIR/conversations.db}"
export QUIZ_GENERATION_RETRY_LIMIT="${QUIZ_GENERATION_RETRY_LIMIT:-3}"
# Chat context window and model residency (defaults mirror config.py). The warmup below loads Qwen
# with exactly this num_ctx and keep_alive, so the first Chat/Quiz request finds it loaded instead of
# paying a cold load. OLLAMA_KEEP_ALIVE makes the same residency Ollama's default for calls that
# set none (Summary, Flashcards, embeddings). On a 15 GB T4, Qwen 2.5 7B Q4_K_M with a 16k window
# (~4.7 GB weights + ~0.9 GB KV cache + buffers) and bge-m3 (~1.2 GB) stay well within VRAM; an
# idle resident model is still evicted by Ollama when a larger one (e.g. Gemma 12B) needs the room.
export AI_TUTOR_CHAT_NUM_CTX="${AI_TUTOR_CHAT_NUM_CTX:-16384}"
export AI_TUTOR_GENERATION_KEEP_ALIVE="${AI_TUTOR_GENERATION_KEEP_ALIVE:-30m}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-$AI_TUTOR_GENERATION_KEEP_ALIVE}"

mkdir -p "$LOG_DIR" "$RUNTIME_DIR" "$AI_TUTOR_DATA_DIR" "$AI_TUTOR_VECTORSTORE_DIR"

log() { printf '[ai-tutor] %s\n' "$*"; }
tail_log() { [[ -f "$1" ]] && tail -n 80 "$1" >&2 || true; }
# PID of the background chat warmup (see start_model_warmup); every failure path stops it, so no
# request outlives a failed startup.
CHAT_WARMUP_PID=""
stop_chat_warmup() {
  [[ -n "$CHAT_WARMUP_PID" ]] || return 0
  pkill -TERM -P "$CHAT_WARMUP_PID" 2>/dev/null || true
  kill "$CHAT_WARMUP_PID" 2>/dev/null || true
  CHAT_WARMUP_PID=""
}
fail() { log "ERROR: $1"; [[ $# -lt 2 ]] || tail_log "$2"; stop_chat_warmup; exit 1; }
# "[timing] <stage>: <seconds>s" lines are parsed by the notebook into its startup timing table.
now() { date +%s.%N; }
log_timing() { awk -v stage="$1" -v start="$2" -v end="$(now)" 'BEGIN { printf "[timing] %s: %.1fs\n", stage, end - start }'; }

stage_started="$(now)"

# Kaggle is compute only. Accounts and app data live in external PostgreSQL (DATABASE_URL), document
# vectors in the same database (pgvector) and original uploads in a private Supabase Storage bucket
# (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY). Each value comes from the environment or, on Kaggle,
# from the Kaggle Secret of the same name. Values are never printed.
resolve_secret() {
  local name="$1" value
  if [[ -n "${!name:-}" ]]; then
    log "$name provided by the environment"
    return
  fi
  value="$(python - "$name" 2>/dev/null <<'PY' || true
import sys
try:
    from kaggle_secrets import UserSecretsClient
    print(UserSecretsClient().get_secret(sys.argv[1]) or "", end="")
except Exception:
    pass
PY
)"
  if [[ -n "$value" ]]; then
    export "$name=$value"
    log "$name loaded from Kaggle Secrets"
  fi
}

# AI_TUTOR_ALLOW_SQLITE=1 is SQLite mode: local SQLite only, no PostgreSQL/Supabase secrets needed,
# even when a DATABASE_URL secret is attached to the notebook.
if [[ "${AI_TUTOR_ALLOW_SQLITE:-0}" == "1" ]]; then
  unset DATABASE_URL
else
  resolve_secret DATABASE_URL
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  [[ "${AI_TUTOR_ALLOW_SQLITE:-0}" == "1" ]] ||
    fail "DATABASE_URL is not set. Add the Kaggle Secret DATABASE_URL and attach it to this notebook (or set AI_TUTOR_ALLOW_SQLITE=1 to run on temporary local SQLite)."
  log "SQLite mode: using local SQLite at $AI_TUTOR_DATABASE_PATH (PostgreSQL/Supabase not used)."
  log "WARNING: accounts, uploads and saved data are lost when this Kaggle session ends unless the notebook's state Dataset snapshot saves data/ and vectorstore/."
else
  resolve_secret SUPABASE_URL
  resolve_secret SUPABASE_SERVICE_ROLE_KEY
  if [[ "${AI_TUTOR_DOCUMENT_STORAGE:-supabase}" != "local" ]] &&
     [[ -z "${SUPABASE_URL:-}" || -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]]; then
    fail "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required so uploaded documents survive a Kaggle reset. Add both Kaggle Secrets and attach them to this notebook."
  fi
fi
# Connection, schema (incl. pgvector), private bucket and Data API lock-down; fails clearly.
db_status="$(cd "$PROJECT_ROOT" && python -m backend.durable_setup 2>&1)" || fail "Durable storage check failed: $db_status"
while IFS= read -r line; do log "$line"; done <<<"$db_status"
log_timing "secrets + durable storage check" "$stage_started"

on_error() {
  local exit_code=$?
  log "Command failed at line $1 (exit $exit_code)."
  for file in ollama vllm backend frontend nginx; do tail_log "$LOG_DIR/$file.log"; done
  stop_chat_warmup
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
  # Read the list once into a variable: `awk | grep -q` under `set -o pipefail` can report failure
  # (SIGPIPE) when grep exits at the first match, which would wrongly trigger a second pull.
  local names
  names="$(ollama list 2>/dev/null | awk 'NR > 1 {print $1}')" || return 1
  grep -Fxq "$1" <<<"$names" || grep -Fxq "$1:latest" <<<"$names"
}

# Pull one model into Ollama (callers only pull when ollama_has_model is false).
# Ollama 0.34.x refuses the redirect from the Hugging Face registry to its CDN
# ("blocked redirect to a different host", e.g. us.aws.cdn.hf.co), so hf.co/* models are pulled
# with --insecure first and, if that also fails, with a plain pull. --insecure is used only for
# hf.co/* models, never for the registry.ollama.ai models (e.g. the embedding model).
# The optional second argument gives a parallel pull its own log instead of the shared ollama.log.
pull_model() {
  local model="$1" pull_log="${2:-$LOG_DIR/ollama.log}" started
  started="$(now)"
  if [[ "$model" == hf.co/* ]]; then
    if ! ollama pull --insecure "$model" >>"$pull_log" 2>&1; then
      log "ollama pull --insecure failed for $model; retrying without --insecure"
      ollama pull "$model" >>"$pull_log" 2>&1 ||
        fail "Could not pull Hugging Face model '$model' (tried --insecure and a plain pull)." "$pull_log"
    fi
  else
    ollama pull "$model" >>"$pull_log" 2>&1 || fail "Could not pull model '$model'." "$pull_log"
  fi
  ollama_has_model "$model" || fail "Model '$model' is still absent after pulling it." "$pull_log"
  log_timing "pull $model" "$started"
}

prepare_kaggle_chroma() {
  local chroma_path="$AI_TUTOR_VECTORSTORE_DIR" marker current_model timestamp backup_path
  if [[ -n "${DATABASE_URL:-}" ]]; then
    log "Document vectors are stored in PostgreSQL (pgvector); no local Chroma to prepare"
    return
  fi
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
  # so incremental ingestion repopulates the rebuilt vectorstore. (Never reached with PostgreSQL:
  # this function returns early, so the shared registry is never cleared from here.)
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

# Ollama's own breakdown of one warmup response (nanoseconds in the JSON): load vs prompt vs generation,
# so a slow warmup in the log can be attributed to the model load itself or to inference.
log_ollama_durations() {
  python - "$1" "$2" <<'PY' || true
import json, sys
label, path = sys.argv[1], sys.argv[2]
data = json.load(open(path))
parts = [f"{key}={data[key] / 1e9:.1f}s" for key in ("load_duration", "prompt_eval_duration", "eval_duration", "total_duration") if key in data]
parts += [f"{key}={data[key]}" for key in ("prompt_eval_count", "eval_count") if key in data]
print(f"[ai-tutor] Ollama {label} warmup: {' '.join(parts)}")
PY
}

CHAT_WARMUP_LOG="$LOG_DIR/warmup-chat.log"
CHAT_WARMUP_RESPONSE="$RUNTIME_DIR/warmup-chat.json"
MODELS_WARMED_MARKER=""
WARMUP_STARTED=""

# Phase 1 of the once-per-runtime warmup. Only the startup default (Qwen) and the embedding model are
# warmed: Gemma is lazy (see /api/models/{id}/prepare) and is never pulled or warmed at startup.
# The embedding request is sent first and awaited here (indexing needs it); the chat request runs in the
# background meanwhile, so its load overlaps the embedding warmup, the Chroma check, indexing and the
# FastAPI/frontend/nginx startup. finish_model_warmup awaits it before startup reports success, so the
# first Chat/Quiz request still finds Qwen loaded with the Chat num_ctx and keep_alive. Both models fit
# in VRAM together (see the residency note above); if Ollama serializes the two loads, nothing is lost.
start_model_warmup() {
  local key started embedding_request chat_request
  key="$(printf '%s\n%s\n%s\n%s\n' "$OLLAMA_CHAT_MODEL" "$OLLAMA_EMBEDDING_MODEL" "$AI_TUTOR_CHAT_NUM_CTX" \
    "$AI_TUTOR_GENERATION_KEEP_ALIVE" | sha256sum | awk '{print $1}')"
  MODELS_WARMED_MARKER="$RUNTIME_DIR/models-warmed-$key"
  if [[ -f "$MODELS_WARMED_MARKER" ]]; then
    log "Model warmup already completed in this runtime"
    MODELS_WARMED_MARKER=""
    return
  fi
  WARMUP_STARTED="$(now)"
  embedding_request="$(python -c 'import json, os; print(json.dumps({"model": os.environ["OLLAMA_EMBEDDING_MODEL"], "input": "AI Tutor embedding health check.", "keep_alive": os.environ["AI_TUTOR_GENERATION_KEEP_ALIVE"]}))')"
  log "Warming embedding model"
  started="$(now)"
  # Sent before the chat request so that, if Ollama loads one model at a time, the embedding model
  # (needed by indexing) goes first and the app startup overlaps the longer Qwen load.
  curl --fail --silent --show-error --max-time 180 -o "$RUNTIME_DIR/warmup-embedding.json" \
    -H 'Content-Type: application/json' "$OLLAMA_HOST/api/embed" -d "$embedding_request" &
  local embedding_pid=$!
  if [[ "$LLM_BACKEND" == "vllm" ]]; then
    log "Chat model is served by vLLM (loaded at its startup); skipping the Ollama chat warmup"
  else
    chat_request="$(python -c 'import json, os; print(json.dumps({"model": os.environ["OLLAMA_CHAT_MODEL"], "prompt": "Reply with OK.", "stream": False, "keep_alive": os.environ["AI_TUTOR_GENERATION_KEEP_ALIVE"], "options": {"num_ctx": int(os.environ["AI_TUTOR_CHAT_NUM_CTX"])}}))')"
    log "Warming chat model in the background (num_ctx=$AI_TUTOR_CHAT_NUM_CTX keep_alive=$AI_TUTOR_GENERATION_KEEP_ALIVE; log: $CHAT_WARMUP_LOG)"
    # Own log, not the notebook's pipe; no ERR trap inside (its status is checked by finish_model_warmup).
    (
      trap - ERR
      chat_started="$(now)"
      curl --fail --silent --show-error --max-time 600 -o "$CHAT_WARMUP_RESPONSE" \
        -H 'Content-Type: application/json' "$OLLAMA_HOST/api/generate" -d "$chat_request"
      log_timing "warmup chat model" "$chat_started"
    ) >"$CHAT_WARMUP_LOG" 2>&1 &
    CHAT_WARMUP_PID=$!
  fi
  local embedding_status=0
  wait "$embedding_pid" || embedding_status=$?
  (( embedding_status == 0 )) || fail "Embedding model warmup failed (exit $embedding_status): $OLLAMA_EMBEDDING_MODEL" "$LOG_DIR/ollama.log"
  python -c 'import json, sys; data=json.load(open(sys.argv[1])); assert data.get("embeddings"), "Embedding warmup returned no vector"' \
    "$RUNTIME_DIR/warmup-embedding.json"
  log_timing "warmup embedding model" "$started"
  log_ollama_durations embedding "$RUNTIME_DIR/warmup-embedding.json"
}

# Phase 2: awaits the background chat warmup (a failure stops startup like before), then records the
# residency evidence and the once-per-runtime marker.
finish_model_warmup() {
  [[ -n "$MODELS_WARMED_MARKER" ]] || return 0
  local started status=0
  started="$(now)"
  if [[ -n "$CHAT_WARMUP_PID" ]]; then
    wait "$CHAT_WARMUP_PID" || status=$?
    CHAT_WARMUP_PID=""
    cat "$CHAT_WARMUP_LOG"
    (( status == 0 )) || fail "Chat model warmup failed (exit $status): $OLLAMA_CHAT_MODEL" "$CHAT_WARMUP_LOG"
    log_ollama_durations chat "$CHAT_WARMUP_RESPONSE"
  fi
  log_timing "wait for chat warmup" "$started"
  log_timing "model warmup (total)" "$WARMUP_STARTED"
  # Evidence for the log: the chat model (Ollama backend) and the embedding model stay resident;
  # `ollama ps` shows their size, CONTEXT and GPU/CPU split, and nvidia-smi the VRAM actually used.
  log "Models resident after warmup (expected: $OLLAMA_CHAT_MODEL with context $AI_TUTOR_CHAT_NUM_CTX, and $OLLAMA_EMBEDDING_MODEL):"
  ollama ps || true
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv || true
  touch "$MODELS_WARMED_MARKER"
}

assert_health_models() {
  local url="$1" payload
  payload="$(curl --fail --silent --show-error --max-time 5 "$url")"
  printf '%s\n' "$payload"
  HEALTH_PAYLOAD="$payload" python -c 'import json, os, sys; data=json.loads(os.environ["HEALTH_PAYLOAD"]); expected=(os.environ["OLLAMA_CHAT_MODEL"], os.environ["OLLAMA_EMBEDDING_MODEL"]); actual=(data.get("chat_model"), data.get("embedding_model")); sys.exit(0 if data.get("status") == "ok" and actual == expected else f"Health model mismatch or degraded: expected={expected}, actual={actual}, response={data}")'
}

for service in nginx frontend backend vllm ollama; do stop_pid "$service"; done
nginx -s stop -c "$PROJECT_ROOT/deployment/nginx.kaggle.conf" 2>/dev/null || true

log "Starting Ollama"
stage_started="$(now)"
ollama serve >"$LOG_DIR/ollama.log" 2>&1 & echo $! >"$RUNTIME_DIR/ollama.pid"
wait_http "Ollama" "$OLLAMA_HOST/api/tags" 120 "$LOG_DIR/ollama.log"
log_timing "ollama serve ready" "$stage_started"

models_started="$(now)"
if [[ "$LLM_BACKEND" != "vllm" && "$OLLAMA_CHAT_MODEL" == hf.co/* ]] && ! ollama_has_model "$OLLAMA_CHAT_MODEL" &&
   ! ollama_has_model "$OLLAMA_EMBEDDING_MODEL"; then
  # Both required models are missing: pull them concurrently, each into its own log. Both pulls are
  # always awaited (no orphan keeps the notebook's output pipe open) and startup stops if either fails.
  log "Pulling Hugging Face chat model $OLLAMA_CHAT_MODEL and embedding model $OLLAMA_EMBEDDING_MODEL in parallel"
  pull_model "$OLLAMA_CHAT_MODEL" "$LOG_DIR/ollama-pull-chat.log" & chat_pull_pid=$!
  pull_model "$OLLAMA_EMBEDDING_MODEL" "$LOG_DIR/ollama-pull-embedding.log" & embedding_pull_pid=$!
  chat_pull_status=0; wait "$chat_pull_pid" || chat_pull_status=$?
  embedding_pull_status=0; wait "$embedding_pull_pid" || embedding_pull_status=$?
  (( chat_pull_status == 0 )) || fail "Chat model pull failed (exit $chat_pull_status): $OLLAMA_CHAT_MODEL" "$LOG_DIR/ollama-pull-chat.log"
  (( embedding_pull_status == 0 )) || fail "Embedding model pull failed (exit $embedding_pull_status): $OLLAMA_EMBEDDING_MODEL" "$LOG_DIR/ollama-pull-embedding.log"
elif [[ "$LLM_BACKEND" == "vllm" ]]; then
  # Started before the embedding model is warmed so vLLM can reserve its GPU memory share first.
  log "LLM_BACKEND=vllm: starting vLLM instead of pulling $OLLAMA_CHAT_MODEL into Ollama"
  VLLM_PORT="$VLLM_PORT" bash "$PROJECT_ROOT/deployment/start_vllm.sh"
elif [[ "$OLLAMA_CHAT_MODEL" == hf.co/* ]] && ! ollama_has_model "$OLLAMA_CHAT_MODEL"; then
  log "Pulling Hugging Face chat model $OLLAMA_CHAT_MODEL"
  pull_model "$OLLAMA_CHAT_MODEL"
elif [[ "$RECREATE_OLLAMA_MODEL" == "1" ]] || ! ollama_has_model "$OLLAMA_CHAT_MODEL"; then
  [[ -n "$GGUF_MODEL_PATH" ]] || fail "GGUF_MODEL_PATH must be set because chat model '$OLLAMA_CHAT_MODEL' is absent."
  [[ -f "$GGUF_MODEL_PATH" ]] || fail "GGUF file does not exist: $GGUF_MODEL_PATH"
  MODELFILE="$RUNTIME_DIR/Modelfile"
  printf 'FROM "%s"\n\nPARAMETER temperature 0.1\nPARAMETER num_ctx 4096\n' "$GGUF_MODEL_PATH" >"$MODELFILE"
  log "Creating Ollama model $OLLAMA_CHAT_MODEL from the configured GGUF"
  ollama create "$OLLAMA_CHAT_MODEL" -f "$MODELFILE" >>"$LOG_DIR/ollama.log" 2>&1
fi

# Gemma is lazy: only Qwen (above) is pulled at startup. Every other configured
# model is pulled on demand the first time a user actually selects and uses it (see
# backend.model_registry.prepare_generation_model, reached through /api/models/{id}/prepare).
if [[ "$LLM_BACKEND" == "vllm" ]]; then
  log "LLM_BACKEND=vllm: no optional Ollama generation models are preloaded."
elif [[ "$PRELOAD_ALL_MODELS" == "true" || "$PRELOAD_ALL_MODELS" == "1" ]]; then
  IFS=',' read -r -a generation_models <<< "$AVAILABLE_MODELS"
  for generation_model in "${generation_models[@]}"; do
    generation_model="${generation_model//[[:space:]]/}"
    [[ -z "$generation_model" || "$generation_model" == "$OLLAMA_CHAT_MODEL" ]] && continue
    if ollama_has_model "$generation_model"; then log "Generation model already exists: $generation_model"
    else log "Preloading optional generation model $generation_model"; pull_model "$generation_model"
    fi
  done
else
  log "Optional generation models are lazy; set PRELOAD_ALL_MODELS=true only for benchmarking."
fi

if ! ollama_has_model "$OLLAMA_EMBEDDING_MODEL"; then
  log "Pulling embedding model $OLLAMA_EMBEDDING_MODEL"
  pull_model "$OLLAMA_EMBEDDING_MODEL"
else
  log "Embedding model already exists: $OLLAMA_EMBEDDING_MODEL"
fi
log_timing "model pulls (total)" "$models_started"

start_model_warmup

stage_started="$(now)"
prepare_kaggle_chroma
log_timing "chroma check" "$stage_started"

if [[ "${AI_TUTOR_AUTO_INDEX_DATA:-1}" == "1" ]]; then
  log "Incrementally indexing PDF/TXT files in $AI_TUTOR_DATA_DIR"
  stage_started="$(now)"
  cd "$PROJECT_ROOT"
  python -m backend.ingest >>"$LOG_DIR/backend.log" 2>&1
  log_timing "indexing" "$stage_started"
fi

log "Starting FastAPI"
stage_started="$(now)"
cd "$PROJECT_ROOT"
python -m uvicorn backend.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >>"$LOG_DIR/backend.log" 2>&1 &
echo $! >"$RUNTIME_DIR/backend.pid"
wait_http "FastAPI" "http://127.0.0.1:$BACKEND_PORT/api/health" 120 "$LOG_DIR/backend.log"
log "Backend health:"
assert_health_models "http://127.0.0.1:$BACKEND_PORT/api/health"
log_timing "fastapi ready" "$stage_started"

log "Starting frontend"
stage_started="$(now)"
cd "$PROJECT_ROOT/frontend"
API_BASE_URL="" FRONTEND_HOST=127.0.0.1 node server.js >"$LOG_DIR/frontend.log" 2>&1 &
echo $! >"$RUNTIME_DIR/frontend.pid"
wait_http "Frontend" "http://127.0.0.1:$FRONTEND_PORT/" 60 "$LOG_DIR/frontend.log"
log_timing "frontend ready" "$stage_started"

log "Starting Nginx"
stage_started="$(now)"
nginx -c "$PROJECT_ROOT/deployment/nginx.kaggle.conf" >"$LOG_DIR/nginx.log" 2>&1
cp /kaggle/working/ai-tutor-runtime/nginx.pid "$RUNTIME_DIR/nginx.pid" 2>/dev/null || true
wait_http "Public gateway" "http://127.0.0.1:$PUBLIC_PORT/" 30 "$LOG_DIR/nginx.log"
wait_http "Proxied health endpoint" "http://127.0.0.1:$PUBLIC_PORT/api/health" 30 "$LOG_DIR/nginx.log"
log "Proxied health:"
assert_health_models "http://127.0.0.1:$PUBLIC_PORT/api/health"
log_timing "nginx ready" "$stage_started"

# Startup is only reported successful once the background Qwen warmup has finished.
finish_model_warmup

log "All services are running"
for service in ollama vllm backend frontend nginx; do
  pid="$(cat "$RUNTIME_DIR/$service.pid" 2>/dev/null || true)"
  printf '%-10s PID=%s status=%s\n' "$service" "${pid:-unknown}" "$([[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && echo running || echo unknown)"
done
ollama list
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>/dev/null || log "GPU is not available"
