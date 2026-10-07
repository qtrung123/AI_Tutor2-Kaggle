#!/usr/bin/env bash
# Start Qwen 2.5 7B on a local vLLM OpenAI-compatible server for LLM_BACKEND=vllm (Kaggle T4 x2).
# Called by deployment/start_kaggle.sh only when LLM_BACKEND=vllm; never with the Ollama default.
# The model is downloaded (first run) and loaded once here, then served at $VLLM_BASE_URL.
# T4 (compute capability 7.5) has no bfloat16, hence --dtype half.
set -Eeuo pipefail

LOG_DIR="${AI_TUTOR_LOG_DIR:-/kaggle/working/ai-tutor-logs}"
RUNTIME_DIR="${AI_TUTOR_RUNTIME_DIR:-/kaggle/working/ai-tutor-runtime}"
VLLM_MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
VLLM_HOST="${VLLM_HOST:-127.0.0.1}"
VLLM_PORT="${VLLM_PORT:-8001}"
VLLM_TENSOR_PARALLEL_SIZE="${VLLM_TENSOR_PARALLEL_SIZE:-2}"
VLLM_DTYPE="${VLLM_DTYPE:-half}"
# 0.85 leaves room on each T4 for Ollama's bge-m3 embedding model, which still runs alongside vLLM.
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.85}"
# Qwen 2.5 7B's full native window: the largest request (Summary, num_ctx 32768) must still fit.
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
VLLM_PIP_SPEC="${VLLM_PIP_SPEC:-vllm}"
VLLM_STARTUP_TIMEOUT_S="${VLLM_STARTUP_TIMEOUT_S:-1800}"

mkdir -p "$LOG_DIR" "$RUNTIME_DIR"
log() { printf '[ai-tutor] %s\n' "$*"; }

# Environment evidence for T4 compatibility issues, logged before and after any vLLM install
# (installing vLLM can replace Kaggle's preinstalled torch).
log_versions() {
  log "vLLM environment ($1):"
  python - <<'PY' 2>&1 | sed 's/^/[ai-tutor]   /'
import platform
print(f"python={platform.python_version()}")
try:
    import torch
    print(f"torch={torch.__version__} torch_cuda={torch.version.cuda}")
except Exception as error:  # torch missing or broken
    print(f"torch=unavailable ({error})")
try:
    from importlib.metadata import version
    print(f"vllm={version('vllm')}")
except Exception:
    print("vllm=not installed")
PY
  nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader 2>/dev/null \
    | sed 's/^/[ai-tutor]   gpu=/' || log "  gpu=unavailable (nvidia-smi failed)"
}

log_versions "before startup"
if ! python -c "import vllm" >/dev/null 2>&1; then
  log "vLLM is missing; installing $VLLM_PIP_SPEC once"
  python -m pip install --quiet "$VLLM_PIP_SPEC" >>"$LOG_DIR/vllm.log" 2>&1
  log_versions "after vLLM install"
fi

pid_file="$RUNTIME_DIR/vllm.pid"
if [[ -s "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  log "Stopping old vLLM process ($(cat "$pid_file"))"
  kill "$(cat "$pid_file")" 2>/dev/null || true
  for _ in {1..40}; do kill -0 "$(cat "$pid_file")" 2>/dev/null || break; sleep 0.5; done
  kill -9 "$(cat "$pid_file")" 2>/dev/null || true
fi

log "Starting vLLM: model=$VLLM_MODEL tp=$VLLM_TENSOR_PARALLEL_SIZE dtype=$VLLM_DTYPE gpu_mem=$VLLM_GPU_MEMORY_UTILIZATION max_len=$VLLM_MAX_MODEL_LEN port=$VLLM_PORT"
python -m vllm.entrypoints.openai.api_server \
  --model "$VLLM_MODEL" \
  --host "$VLLM_HOST" --port "$VLLM_PORT" \
  --tensor-parallel-size "$VLLM_TENSOR_PARALLEL_SIZE" \
  --dtype "$VLLM_DTYPE" \
  --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
  --max-model-len "$VLLM_MAX_MODEL_LEN" \
  --api-key "$VLLM_API_KEY" \
  >>"$LOG_DIR/vllm.log" 2>&1 &
echo $! >"$pid_file"

start="$(date +%s)"
until curl --fail --silent --max-time 3 -H "Authorization: Bearer $VLLM_API_KEY" \
    "http://$VLLM_HOST:$VLLM_PORT/v1/models" 2>/dev/null | grep -Fq "\"$VLLM_MODEL\""; do
  if ! kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    log "ERROR: vLLM exited during startup"; tail -n 80 "$LOG_DIR/vllm.log" >&2; exit 1
  fi
  if (( $(date +%s) - start >= VLLM_STARTUP_TIMEOUT_S )); then
    log "ERROR: vLLM did not become ready in ${VLLM_STARTUP_TIMEOUT_S}s"; tail -n 80 "$LOG_DIR/vllm.log" >&2; exit 1
  fi
  sleep 2
done
log "vLLM is ready at http://$VLLM_HOST:$VLLM_PORT/v1 ($VLLM_MODEL)"
