"""Kaggle startup critical path: overlapped model warmup in start_kaggle.sh, parallel dependency
installs, and the early Cloudflare tunnel / gateway update in kaggle_run_ollama_cloudflare.ipynb.

The shell tests run the script's own warmup functions under bash with fake curl/ollama/nvidia-smi
binaries; the notebook tests execute the real cell sources with faked processes and HTTP."""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
STARTUP = (ROOT / "deployment" / "start_kaggle.sh").read_text(encoding="utf-8")
NOTEBOOK = json.loads((ROOT / "kaggle_run_ollama_cloudflare.ipynb").read_text(encoding="utf-8"))
QWEN = "hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"
BASH = shutil.which("bash")


def cell(cell_id: str) -> str:
    [match] = [c for c in NOTEBOOK["cells"] if c.get("id") == cell_id]
    return "".join(match["source"])


DEPENDENCIES_CELL, EARLY_TUNNEL_CELL, SERVICES_CELL, CLOUDFLARE_CELL = (
    cell("d9adf282"), cell("3f1c2a7e"), cell("b57020be"), cell("57b57f91"))

FAKE_CURL = r"""#!/usr/bin/env bash
out="" url="" payload=""
while (( $# )); do
  case "$1" in
    -o) out="$2"; shift ;;
    -d) payload="$2"; shift ;;
    -H|--max-time) shift ;;
    http*) url="$1" ;;
  esac
  shift
done
case "$url" in
  */api/embed)
    echo "embed $(date +%s.%N)" >>"$FAKE_DIR/calls"
    [[ "${FAKE_EMBED_EXIT:-0}" == 0 ]] || exit "$FAKE_EMBED_EXIT"
    printf '{"embeddings": [[0.1, 0.2]], "load_duration": 2000000000, "total_duration": 3000000000}' >"$out" ;;
  */api/generate)
    echo "generate-start $(date +%s.%N)" >>"$FAKE_DIR/calls"
    printf '%s' "$payload" >"$FAKE_DIR/generate-request.json"
    echo $$ >"$FAKE_DIR/generate.pid"
    sleep "${FAKE_CHAT_SLEEP:-0.5}"
    [[ "${FAKE_CHAT_EXIT:-0}" == 0 ]] || exit "$FAKE_CHAT_EXIT"
    printf '{"response": "OK", "load_duration": 9000000000, "prompt_eval_duration": 100000000, "eval_duration": 50000000, "total_duration": 9200000000, "eval_count": 2}' >"$out" ;;
  *) exit 7 ;;
esac
"""


def warmup_harness() -> str:
    """The script's helpers and warmup functions, then the same call order as the script."""
    helpers = STARTUP[STARTUP.index("log() {"):STARTUP.index('stage_started="$(now)"')]
    warmup = STARTUP[STARTUP.index("# Ollama's own breakdown"):STARTUP.index("assert_health_models() {")]
    return "\n".join([
        "set -Eeuo pipefail", helpers, warmup,
        "start_model_warmup",
        'echo "APP_STARTUP $(date +%s.%N)"',   # Chroma check, indexing, FastAPI, frontend, nginx
        "finish_model_warmup",
        "echo HARNESS_DONE",
    ])


@unittest.skipUnless(BASH, "bash is required to run the startup script functions")
class OverlappedModelWarmupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in {"curl": FAKE_CURL, "ollama": "#!/usr/bin/env bash\necho NAME\n",
                           "nvidia-smi": "#!/usr/bin/env bash\necho gpu\n"}.items():
            (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
            (bin_dir / name).chmod(0o755)
        (self.tmp / "logs").mkdir()
        (self.tmp / "runtime").mkdir()
        (self.tmp / "harness.sh").write_text(warmup_harness(), encoding="utf-8", newline="\n")
        self.bin_dir = bin_dir

    def run_harness(self, **env):
        variables = {
            "FAKE_DIR": self.tmp.as_posix(), "LOG_DIR": (self.tmp / "logs").as_posix(),
            "RUNTIME_DIR": (self.tmp / "runtime").as_posix(), "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OLLAMA_CHAT_MODEL": QWEN, "OLLAMA_EMBEDDING_MODEL": "bge-m3", "AI_TUTOR_CHAT_NUM_CTX": "16384",
            "AI_TUTOR_GENERATION_KEEP_ALIVE": "30m", "LLM_BACKEND": "ollama", **env,
        }
        exports = "".join(f"export {key}='{value}'\n" for key, value in variables.items())
        # PATH entries must be POSIX paths (Git Bash on Windows would split "C:/..." at the colon).
        command = (f"{exports}bin_dir='{self.bin_dir.as_posix()}'\n"
                   'if command -v cygpath >/dev/null; then bin_dir="$(cygpath -u "$bin_dir")"; fi\n'
                   'export PATH="$bin_dir:$PATH"\n'
                   f'source "{(self.tmp / "harness.sh").as_posix()}"\n')
        started = time.time()
        result = subprocess.run([BASH, "-c", command], capture_output=True, text=True, timeout=60)
        return result, time.time() - started

    def calls(self):
        path = self.tmp / "calls"
        return path.read_text().split("\n") if path.exists() else []

    def markers(self):
        return list((self.tmp / "runtime").glob("models-warmed-*"))

    def test_chat_warmup_overlaps_app_startup_and_is_awaited(self):
        result, _ = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        # The chat request was sent before the app startup steps ran, and finished before success.
        generate_started = float(next(c for c in self.calls() if c.startswith("generate-start")).split()[1])
        app_startup = float(next(line for line in out.splitlines() if line.startswith("APP_STARTUP")).split()[1])
        self.assertLess(generate_started, app_startup)
        self.assertLess(out.index("APP_STARTUP"), out.index("[timing] warmup chat model:"))
        self.assertLess(out.index("[timing] warmup chat model:"), out.index("HARNESS_DONE"))
        for line in ("[timing] warmup embedding model:", "[timing] wait for chat warmup:", "[timing] model warmup (total):"):
            self.assertIn(line, out)
        # Ollama's own load/inference split is logged for both models.
        self.assertIn("Ollama embedding warmup: load_duration=2.0s total_duration=3.0s", out)
        self.assertIn("Ollama chat warmup: load_duration=9.0s prompt_eval_duration=0.1s eval_duration=0.1s", out)
        # Same request as before: the Chat window and residency, so the first real request needs no reload.
        request = json.loads((self.tmp / "generate-request.json").read_text())
        self.assertEqual(request, {"model": QWEN, "prompt": "Reply with OK.", "stream": False, "keep_alive": "30m",
                                   "options": {"num_ctx": 16384}})
        self.assertEqual(len(self.markers()), 1)

    def test_second_run_in_the_same_runtime_skips_the_warmup(self):
        self.assertEqual(self.run_harness()[0].returncode, 0)
        calls_after_first = len(self.calls())
        result, _ = self.run_harness()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Model warmup already completed in this runtime", result.stdout)
        self.assertEqual(len(self.calls()), calls_after_first)
        self.assertNotIn("[timing] wait for chat warmup", result.stdout)

    def test_failed_chat_warmup_fails_startup_without_a_marker(self):
        result, _ = self.run_harness(FAKE_CHAT_EXIT="22")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: Chat model warmup failed (exit 22): {QWEN}", result.stdout)
        self.assertNotIn("HARNESS_DONE", result.stdout)
        self.assertEqual(self.markers(), [])

    def test_failed_embedding_warmup_stops_the_background_chat_warmup(self):
        result, elapsed = self.run_harness(FAKE_EMBED_EXIT="22", FAKE_CHAT_SLEEP="30")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: Embedding model warmup failed (exit 22): bge-m3", result.stdout)
        self.assertNotIn("APP_STARTUP", result.stdout)
        # The background job writes only to its own log, so it never holds the caller's pipe open.
        self.assertLess(elapsed, 20)
        self.assertEqual(self.markers(), [])
        pid_file = self.tmp / "generate.pid"
        if shutil.which("pkill") and pid_file.exists():   # Linux/Kaggle: the in-flight request is killed too
            alive = subprocess.run(["kill", "-0", pid_file.read_text().strip()], capture_output=True).returncode == 0
            self.assertFalse(alive)

    def test_vllm_backend_warms_only_the_embedding_model(self):
        result, _ = self.run_harness(LLM_BACKEND="vllm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipping the Ollama chat warmup", result.stdout)
        self.assertFalse(any(c.startswith("generate-start") for c in self.calls()))
        self.assertEqual(len(self.markers()), 1)


class StartupScriptOrderTests(unittest.TestCase):
    def test_chat_warmup_is_awaited_after_the_public_gateway_check_and_before_the_summary(self):
        start = STARTUP.index("\nstart_model_warmup\n")
        self.assertLess(STARTUP.index('log_timing "model pulls (total)"'), start)
        self.assertLess(start, STARTUP.index("prepare_kaggle_chroma\n", start))
        finish = STARTUP.index("\nfinish_model_warmup\n")
        self.assertLess(STARTUP.index('assert_health_models "http://127.0.0.1:$PUBLIC_PORT/api/health"'), finish)
        self.assertLess(finish, STARTUP.index('log "All services are running"'))

    def test_every_failure_path_stops_the_background_warmup(self):
        self.assertIn("stop_chat_warmup; exit 1; }", STARTUP)
        on_error = STARTUP[STARTUP.index("on_error() {"):STARTUP.index("trap 'on_error $LINENO' ERR")]
        self.assertIn("stop_chat_warmup", on_error)


# ---------------------------------------------------------------- notebook cells


@contextmanager
def capture():
    out = io.StringIO()
    with redirect_stdout(out):
        yield out


def notebook_namespace(**extra):
    timings = {}

    @contextmanager
    def timed(stage):
        started = time.time()
        try:
            yield
        finally:
            timings[stage] = time.time() - started

    return {"timed": timed, "STARTUP_TIMINGS": timings, "PUBLIC_PORT": 7860, "OLLAMA_CHAT_MODEL": QWEN,
            "OLLAMA_EMBEDDING_MODEL": "bge-m3", "EARLY_GATEWAY_UPDATE": True, **extra}


class ParallelDependenciesCellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        project = self.tmp / "project"
        (project / "frontend").mkdir(parents=True)
        (project / "requirements.txt").write_text("fastapi\n")
        (project / "frontend" / "package-lock.json").write_text("{}")
        (self.tmp / "markers").mkdir()
        self.source = (DEPENDENCIES_CELL
                       .replace('Path("/kaggle/working/ai-tutor-logs/dependencies")', f'Path({str(self.tmp / "logs")!r})')
                       .replace('Path("/usr/local/bin/cloudflared")', f'Path({str(self.tmp / "cloudflared")!r})')
                       .replace('Path("/etc/apt/sources.list.d")', f'Path({str(self.tmp / "no-apt-sources")!r})'))
        self.namespace = notebook_namespace(RUNTIME_MARKERS=self.tmp / "markers", PROJECT_ROOT=str(project), QUICK_START=True)

    def run_cell(self, run, installer_installs_ollama=True):
        installed = set()

        def which(binary):
            return f"/usr/bin/{binary}" if binary in installed else None

        def fake_run(command, **kwargs):
            text = command if isinstance(command, str) else " ".join(map(str, command))
            result = run(text)
            if "install.sh" in text and result.returncode == 0 and installer_installs_ollama:
                installed.add("ollama")
            return result

        def retrieve(url, target):
            run("cloudflared download")
            Path(target).write_text("binary")

        with patch("subprocess.run", side_effect=fake_run), patch("shutil.which", side_effect=which), \
                patch("urllib.request.urlretrieve", side_effect=retrieve), capture() as out:
            try:
                exec(compile(self.source, "dependencies-cell", "exec"), self.namespace)
            finally:
                self.output = out.getvalue()

    def test_independent_install_chains_run_concurrently(self):
        # pip, npm, apt and the cloudflared download all wait for each other: only a parallel run passes.
        barrier = threading.Barrier(4, timeout=10)
        order = []

        def run(text):
            order.append(text)
            if text.startswith(("apt-get update", "cloudflared download")) or " pip install " in text or text.startswith("npm ci"):
                barrier.wait()
            return subprocess.CompletedProcess(text, 0)

        self.run_cell(run)
        # apt finishes before the Ollama installer (it provides zstd), which runs under pipefail.
        installer = next(i for i, text in enumerate(order) if "install.sh" in text)
        self.assertLess(max(i for i, text in enumerate(order) if text.startswith("apt-get")), installer)
        self.assertIn("bash -o pipefail -c curl -fsSL https://ollama.com/install.sh | sh", order[installer])
        for stage in ("dependencies", "dependencies > apt", "dependencies > pip", "dependencies > npm",
                      "dependencies > ollama install", "dependencies > cloudflared"):
            self.assertIn(stage, self.namespace["STARTUP_TIMINGS"])

    def test_a_failed_step_stops_the_cell_after_every_step_finished(self):
        finished = []

        def run(text):
            if text.startswith("npm ci"):
                time.sleep(0.3)
                finished.append("npm")
            if " pip install " in text:
                return subprocess.CompletedProcess(text, 1)
            return subprocess.CompletedProcess(text, 0)

        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cell(run)
        self.assertEqual(finished, ["npm"])   # the slower npm step was awaited, not orphaned
        self.assertIn("Dependency step failed: pip", self.output)
        self.assertFalse(any(path.name.startswith("pip-") for path in (self.tmp / "markers" / "setup").iterdir()))

    def test_a_silent_ollama_installer_failure_is_reported(self):
        def run(text):
            return subprocess.CompletedProcess(text, 0)

        # The installer exits 0 without installing anything.
        with self.assertRaisesRegex(RuntimeError, "no ollama binary"):
            self.run_cell(run, installer_installs_ollama=False)


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body.encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CloudflareCellsTests(unittest.TestCase):
    TUNNEL = "https://quiet-river-1234.trycloudflare.com"
    GATEWAY = "https://ai-tutor-gateway.quoctrung121212.workers.dev"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "cloudflared").write_text("binary")
        self.paths = {
            '"/kaggle/working/ai-tutor-runtime"': repr((self.tmp / "runtime").as_posix()),
            '"/kaggle/working/ai-tutor-logs/cloudflared.log"': repr((self.tmp / "logs" / "cloudflared.log").as_posix()),
            '"/usr/local/bin/cloudflared"': repr((self.tmp / "cloudflared").as_posix()),
        }
        self.requests = []
        self.gateway_payload = {"status": "ok", "chat_model": QWEN, "embedding_model": "bge-m3"}
        self.tunnel_log = f"INF |  {self.TUNNEL}  |\nINF Registered tunnel connection connIndex=0\n"
        self.exit_code = None
        self.clock = FakeClock()
        secrets = types.ModuleType("kaggle_secrets")
        secrets.UserSecretsClient = lambda: types.SimpleNamespace(get_secret=lambda name: f"secret-{name}")
        patches = [patch.dict(sys.modules, {"kaggle_secrets": secrets}),
                   patch("urllib.request.urlopen", side_effect=self.urlopen),
                   patch("subprocess.Popen", side_effect=self.popen),
                   patch("shutil.which", return_value=None),
                   patch("time.time", side_effect=self.clock.time),
                   patch("time.sleep", side_effect=self.clock.sleep)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def source(self, text):
        for old, new in self.paths.items():
            text = text.replace(old, new)
        return text

    def popen(self, command, stdout, **kwargs):
        stdout.write(self.tunnel_log)
        stdout.flush()
        return types.SimpleNamespace(pid=4242, returncode=self.exit_code, poll=lambda: self.exit_code)

    def urlopen(self, request, timeout=None):
        if isinstance(request, urllib.request.Request):
            self.requests.append(("PUT", request.data.decode(), self.clock.time()))
            return FakeResponse(200, '{"success": true}')
        self.requests.append(("GET", request, self.clock.time()))
        if request.startswith(self.GATEWAY):
            return FakeResponse(200, json.dumps(self.gateway_payload))
        return FakeResponse(200, json.dumps({"status": "ok", "chat_model": QWEN, "embedding_model": "bge-m3"}))

    def run_cells(self, early=True, services_seconds=150):
        namespace = notebook_namespace(EARLY_GATEWAY_UPDATE=early)
        with capture() as out:
            exec(compile(self.source(EARLY_TUNNEL_CELL), "early-tunnel-cell", "exec"), namespace)
            namespace["TUNNEL_THREAD"].join(timeout=10)   # the services cell would run meanwhile
            self.clock.sleep(services_seconds)
            try:
                exec(compile(self.source(CLOUDFLARE_CELL), "cloudflare-cell", "exec"), namespace)
            finally:
                self.output = out.getvalue()
        return namespace

    def puts(self):
        return [r for r in self.requests if r[0] == "PUT"]

    def test_tunnel_and_gateway_are_prepared_while_the_services_start(self):
        namespace = self.run_cells()
        # One KV write, made before the services finished (the clock only advanced after the thread joined).
        [(_, origin, written_at)] = self.puts()
        self.assertEqual(origin, self.TUNNEL)
        self.assertLess(written_at, self.clock.time() - 100)
        self.assertTrue(namespace["GATEWAY_UPDATED"])
        self.assertTrue(namespace["GATEWAY_READY"])
        self.assertEqual(namespace["PUBLIC_URL"], self.TUNNEL)
        self.assertIn("Permanent AI Tutor URL (verified)", self.output)
        # Propagation already elapsed during startup: the gateway check is not a fixed 60s wait.
        self.assertLess(namespace["STARTUP_TIMINGS"]["cloudflare gateway"], 1)
        self.assertIn("cloudflare > tunnel registered (background)", namespace["STARTUP_TIMINGS"])

    def test_gateway_is_not_reported_ready_when_it_serves_another_deployment(self):
        self.gateway_payload = {"status": "ok", "chat_model": "some-other-model", "embedding_model": "bge-m3"}
        namespace = self.run_cells()
        self.assertTrue(namespace["GATEWAY_UPDATED"])
        self.assertFalse(namespace["GATEWAY_READY"])
        self.assertIn("NOT verified yet", self.output)
        self.assertNotIn("Permanent AI Tutor URL (verified)", self.output)

    def test_without_early_update_the_gateway_is_written_after_the_public_health_check(self):
        namespace = self.run_cells(early=False)
        [(_, origin, written_at)] = self.puts()
        public_health = next(t for kind, url, t in self.requests if kind == "GET" and url.startswith(self.TUNNEL))
        self.assertEqual(origin, self.TUNNEL)
        self.assertGreaterEqual(written_at, public_health)
        self.assertTrue(namespace["GATEWAY_READY"])

    def test_a_failed_early_kv_write_is_retried_after_the_public_health_check(self):
        put_results = iter([500, 200])
        original = self.urlopen

        def urlopen(request, timeout=None):
            if isinstance(request, urllib.request.Request):
                self.requests.append(("PUT", request.data.decode(), self.clock.time()))
                status = next(put_results)
                return FakeResponse(status, '{"success": %s}' % ("true" if status == 200 else "false"))
            return original(request, timeout)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            namespace = self.run_cells()
        early, retry = self.puts()
        public_health = next(t for kind, url, t in self.requests if kind == "GET" and url.startswith(self.TUNNEL))
        self.assertLess(early[2], public_health)
        self.assertGreaterEqual(retry[2], public_health)
        self.assertTrue(namespace["GATEWAY_UPDATED"])
        self.assertTrue(namespace["GATEWAY_READY"])

    def test_a_tunnel_that_exits_fails_the_cloudflare_cell(self):
        self.tunnel_log = "ERR failed to request quick Tunnel\n"
        self.exit_code = 1
        with self.assertRaisesRegex(RuntimeError, "Cloudflare tunnel failed to start"):
            self.run_cells()
        self.assertEqual(self.puts(), [])


class ServicesCellTimingTests(unittest.TestCase):
    SCRIPT_OUTPUT = [
        "[ai-tutor] Warming chat model in the background\n",
        "[timing] warmup embedding model: 40.0s\n",
        "[ai-tutor] Chroma embedding marker matches: bge-m3\n",   # start of the indexing/app phase
        "[timing] nginx ready: 1.0s\n",
        "[timing] warmup chat model: 95.0s\n",
        "[timing] wait for chat warmup: 30.0s\n",
        "[ai-tutor] All services are running\n",
    ]

    def test_the_final_wait_for_qwen_counts_as_model_time_and_stages_still_add_up(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        clock = FakeClock()
        lines = iter(self.SCRIPT_OUTPUT)

        def stdout():
            for line in lines:
                # 60s until the Chroma marker (models), then 50s of app phase incl. a 30s wait for Qwen.
                clock.sleep(5 if "Chroma" not in line else 50)
                if "wait for chat warmup" in line:
                    clock.sleep(30)
                yield line

        process = types.SimpleNamespace(stdout=stdout(), wait=lambda: 0)
        source = SERVICES_CELL.replace('"/kaggle/working/ai-tutor-runtime"', repr((tmp / "runtime").as_posix()))
        namespace = notebook_namespace(PROJECT_ROOT=str(tmp / "project"), LECTURE_INPUT_DIR="", APP_ENV={},
                                       FORCE_RESTART=True)
        with patch("subprocess.Popen", return_value=process), \
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="abc\n")), \
                patch("time.time", side_effect=clock.time), capture():
            exec(compile(source, "services-cell", "exec"), namespace)
        timings = namespace["STARTUP_TIMINGS"]
        total = timings["ollama/models"] + timings["indexing/app startup"]
        self.assertAlmostEqual(total, 5 + 5 + 50 + 5 + 5 + 35 + 5)   # unchanged wall clock of the script
        self.assertAlmostEqual(timings["ollama/models"], 5 + 5 + 50 + 30)
        self.assertAlmostEqual(timings["indexing/app startup"], 5 + 5 + 35 + 5 - 30)

    def test_a_warm_run_without_a_chat_wait_line_ignores_an_older_value(self):
        source = SERVICES_CELL
        # The wait is read from this run's own output, never from STARTUP_TIMINGS left by an earlier run.
        self.assertIn("chat_warmup_wait = 0.0", source)
        self.assertNotIn('STARTUP_TIMINGS.get("start_kaggle > wait for chat warmup"', source)


class NotebookStructureTests(unittest.TestCase):
    def test_cell_order_starts_the_tunnel_before_the_services(self):
        ids = [c.get("id") for c in NOTEBOOK["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLess(ids.index("d9adf282"), ids.index("3f1c2a7e"))   # dependencies (cloudflared) first
        self.assertLess(ids.index("3f1c2a7e"), ids.index("b57020be"))   # early tunnel, then services
        self.assertLess(ids.index("b57020be"), ids.index("57b57f91"))   # verification after the services
        for item in NOTEBOOK["cells"]:
            if item["cell_type"] == "code":
                compile("".join(item["source"]), item["id"], "exec")
        config = cell("9f872e66")
        self.assertIn("EARLY_GATEWAY_UPDATE = True", config)

    def test_snapshot_restore_and_autosave_are_untouched(self):
        self.assertIn('_restore_once(\n    "state",', cell("57fe69ec"))
        self.assertIn('save_state_snapshot(message="Automatic 10-minute snapshot")', cell("d1b5b6fb"))


if __name__ == "__main__":
    unittest.main()
