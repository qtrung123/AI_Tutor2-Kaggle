# AI Tutor – System Architecture

This folder holds two high-level diagrams of the AI Tutor's system architecture. They were traced
from the current source code and deployment scripts. For request-level detail (routes, services,
database calls and LLM calls), see the 13 diagrams in
[`../sequence-diagrams/`](../sequence-diagrams/README.md).

| Diagram | Source | Export | Purpose |
| --- | --- | --- | --- |
| System architecture | [`system-architecture.mmd`](system-architecture.mmd) | [svg](exports/system-architecture.svg) | Layers, components and connections. Use this one in the thesis. |
| Deployment profiles | [`deployment-profiles.mmd`](deployment-profiles.mmd) | [svg](exports/deployment-profiles.svg) | How the three supported configurations differ at runtime |

Dashed borders and dashed arrows mark optional components and connections.

## 1. System architecture (layers)

| Layer | Component | Facts from the code |
| --- | --- | --- |
| Client | Learner's web browser | Vanilla JS, with no framework or bundler. All requests send the session cookie (`frontend/js/api.js`). |
| Gateway (Kaggle only) | Cloudflare Worker → Cloudflare Quick Tunnel → nginx :7860 | Learners use one permanent Worker URL (`*.workers.dev`). The Worker proxies each request to the origin stored in Workers KV (`ORIGIN_URL`). On every start, the notebook creates a new `*.trycloudflare.com` quick tunnel to nginx and writes its URL to KV through the Cloudflare API (`kaggle_run_ollama_cloudflare.ipynb`). nginx sends `/api/*` to FastAPI :8000 and `/` to Node :3000, so the UI and the API share one origin. Its upload limit is 100 MB and its timeouts are 600 s (`deployment/nginx.kaggle.conf`). |
| Frontend | Node.js `frontend/server.js` :3000 | Serves the static files and `/app-config.js`, which sets `API_BASE_URL`: `""` on Kaggle, `http://127.0.0.1:8000` locally. |
| Backend | FastAPI + Uvicorn :8000, one worker | `backend/api/*` routers resolve the cookie to an `owner_id` (`deps.py`). `*_service.py` holds the business logic: RAG chat, ingestion and topic extraction, background summary jobs, flashcards, quiz and mastery, and the deterministic planner. `*_store.py`, `db.py` and `vector_store.py` handle persistence. Indexing uses an in-process lock, which relies on the single worker. |
| AI inference | Ollama :11434 (GPU on Kaggle) | Qwen2.5-7B-Instruct Q4_K_M is the default for every generation feature. Gemma 3 12B is pulled only when a learner first selects it (`model_registry.py`). BGE-M3 provides all embeddings (1024-d). An optional vLLM server (:8001, `LLM_BACKEND=vllm`) can serve Qwen chat instead; embeddings stay on Ollama. |
| Data | **Exactly one profile per run** | The profile is chosen at startup by `DATABASE_URL` (`backend/db.py:selected_backend()`, `backend/vector_store.py:get_vectorstore()`). |
| | Profile A: SQLite mode | SQLite `data/conversations.db` (accounts, chats, documents, summaries, flashcards, quizzes, planner, calendar links), ChromaDB `vectorstore/` (collection `study_documents`), and uploaded originals in `data/users/{owner_id}/`. |
| | Profile B: durable mode | Supabase PostgreSQL holds the same tables, plus pgvector `document_chunks`. Originals go to a private Supabase Storage bucket. `backend/durable_setup.py` checks the connection, pgvector, the bucket privacy and row-level-security lockdown before the backend starts. |
| External (optional) | Google Calendar API | OAuth, freeBusy and study events, but only when the learner has connected a calendar (`google_calendar_service.py`). |
| | Private Kaggle Dataset | Snapshots of `data/` and `vectorstore/`: restored at notebook start and saved about every 10 min (`deployment/kaggle_persist.py`). Only Kaggle + profile A needs them to keep user data. |
| | Hugging Face / Ollama library | Model downloads (`ollama pull`) at startup or on first use of a lazy model. |

The diagram stays high-level on purpose. Each service-layer box stands for many modules; the
sequence diagrams show which modules, tables and models each use case touches.

## 2. Deployment profiles

The two alternatives (tunnel and storage) are never combined in one runtime. The supported
configurations are:

| Profile | How it starts | Browser path | Storage | Persistence across restarts |
| --- | --- | --- | --- | --- |
| **1. Local development** | `uvicorn backend.main:app --port 8000` + `npm start` (frontend) + local Ollama | Browser → Node :3000 for the UI, then directly to FastAPI :8000 for `/api` (cross-origin, allowed by CORS) | Profile A by default (an optional `DATABASE_URL` in `.env.local` switches to profile B) | The local disk |
| **2. Kaggle, SQLite mode (active deployment)** | `kaggle_run_ollama_cloudflare.ipynb` → `deployment/start_kaggle.sh`, with `AI_TUTOR_ALLOW_SQLITE=1` set in the notebook | Cloudflare Worker (permanent URL) → Cloudflare Quick Tunnel → nginx :7860 | Profile A in `/kaggle/working`, which is lost when the session ends | `data/` and `vectorstore/` are snapshotted to a private Kaggle Dataset (restored once at start, saved about every 10 min, plus a manual `SAVE_STATE_NOW` backup) |
| **3. Kaggle, durable mode** | `start_kaggle.sh` without `AI_TUTOR_ALLOW_SQLITE`, plus the secrets `DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` | A public tunnel to nginx :7860. The older `kaggle_run.ipynb` uses ngrok | Profile B (Supabase) | Built in; the Kaggle disk is only a cache |

In profiles 2 and 3, `start_kaggle.sh` runs these steps in order:

1. Storage check. Without `DATABASE_URL` the script stops, unless `AI_TUTOR_ALLOW_SQLITE=1` is set;
   it never falls back to SQLite silently. With `DATABASE_URL`, `backend.durable_setup` checks the
   database and storage.
2. Start Ollama.
3. Pull and warm up the models (only Qwen and BGE-M3).
4. Incrementally index `data/`.
5. Start Uvicorn, then Node, then nginx.
6. Check `/api/health` through nginx.

Profile 2 makes one more check after that: the notebook confirms that `/api/health` also answers
through the Worker.

## Rendering

Both diagrams render with Mermaid CLI 11.4.2 without errors. Their sizes are about 1790 × 1580 px
(architecture) and 1855 × 1010 px (profiles). Run this from the repository root:

```bash
for f in system-architecture deployment-profiles; do
  npx -p @mermaid-js/mermaid-cli@11.4.2 mmdc -i docs/system-architecture/$f.mmd \
    -o docs/system-architecture/exports/$f.svg -b white
done
```

Both use the same typography as the sequence diagrams: the `neutral` theme and Times New Roman at
16 px. The architecture diagram fits a portrait page at full text width. The profiles diagram is
wide and short, so it suits a landscape page or a slide.

## Notes

- **The tunnel and the storage mode are independent.** Both notebooks run the same
  `start_kaggle.sh`, and only `AI_TUTOR_ALLOW_SQLITE` / `DATABASE_URL` choose the storage mode.
  The active Cloudflare notebook hard-codes SQLite mode. The ngrok notebook `kaggle_run.ipynb` was
  last changed on 2026-09-27, before durable mode existed. It does not set `AI_TUTOR_ALLOW_SQLITE`,
  so today it starts only if the Supabase secrets are attached. It also keeps its Kaggle Dataset
  snapshot, but in durable mode user data no longer depends on that snapshot.
- **The Cloudflare Worker's source code is not in this repository.** Its behavior (proxy to the
  KV `ORIGIN_URL`) is documented from the notebook code and from
  `tests/test_kaggle_startup_perf.py`, which call it.
- **Optional local storage for originals.** In durable mode, `AI_TUTOR_DOCUMENT_STORAGE=local`
  keeps original uploads on local disk instead of Supabase Storage (`backend/document_storage.py`).
  The diagram shows the default, Supabase Storage.
