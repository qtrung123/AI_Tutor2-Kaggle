# Kiến trúc AI Tutor

Tài liệu này mô tả cấu trúc hiện tại của mã nguồn. Mọi thông tin đều lấy trực tiếp từ code; khi
code thay đổi, hãy cập nhật tài liệu này cùng lúc.

**Sơ đồ đi kèm** (không lặp lại nội dung ở đây):

- [`docs/system-architecture/`](system-architecture/README.md): sơ đồ kiến trúc phân tầng và
  3 cấu hình triển khai (local, Kaggle SQLite mode, Kaggle durable mode).
- [`docs/sequence-diagrams/`](sequence-diagrams/README.md): 13 sơ đồ tuần tự cho từng luồng
  nghiệp vụ (auth, upload/index, chat RAG, summary, flashcards, quiz, subjects, planner, phiên học).

## 1. Cấu trúc thư mục

```text
backend/
  main.py                 # Tạo FastAPI app, CORS, route hệ thống/admin, include các router
  api/                    # Lớp HTTP: mỗi file là một nhóm route theo tính năng
    deps.py               # require_current_user / require_admin_user (auth dùng chung)
    auth.py  documents.py  subjects.py  summary.py  flashcards.py  learning.py  conversations.py
    quiz_library.py  quiz_generation.py  quiz_attempts.py  planner_legacy.py  planner_v2.py
    google_calendar.py
  *_service.py            # Nghiệp vụ theo tính năng
  *_store.py              # Persistence (SQLite hoặc PostgreSQL qua db.py)
  db.py                   # Chọn backend CSDL: SQLite (mặc định) hoặc PostgreSQL (DATABASE_URL)
  vector_store.py         # Chroma cục bộ hoặc pgvector (DATABASE_URL)
  document_storage.py     # File gốc: đĩa cục bộ hoặc Supabase Storage (bucket private)
  durable_setup.py        # Kiểm tra PostgreSQL/pgvector/Storage/RLS trước khi khởi động (chế độ bền vững)
  llm_backend.py          # ChatOllama dùng chung (Ollama mặc định, hoặc vLLM khi LLM_BACKEND=vllm)
  model_registry.py       # id model công khai -> tham chiếu Ollama; kéo model lười
  summary_jobs.py         # Job nền cho Summary (start -> poll)
  quiz_units.py quiz_validation.py quiz_options.py quiz_common.py quiz_diagnostics.py
  quiz_legacy_v2.py       # Engine Quiz V2 cũ, KHÔNG nằm trên luồng chạy thật (giữ để so sánh/test)
  study_*.py              # Planner: scheduler, adaptation, placement, progress, preferences, time utilities
  subject_store.py        # Subjects (môn học) và gán tài liệu
  subject_grouping.py     # Gom nhóm chỉ-đọc cho thẻ Overview (không ghi CSDL)
  document_retrieval.py text_safety.py ingest.py topic_extractor.py rag_service.py perf_timing.py ...
frontend/
  index.html  styles.css  server.js
  js/                     # Các classic script theo tính năng, nạp TRƯỚC app.js
  app.js                  # Tra cứu DOM, gắn event listener, khởi động ứng dụng
deployment/               # start_kaggle.sh, start_vllm.sh, nginx.kaggle.conf, kaggle_persist.py, db_status.py
kaggle_run_ollama_cloudflare.ipynb  # Notebook Kaggle đang dùng (Cloudflare, SQLite mode)
kaggle_run.ipynb          # Notebook Kaggle cũ (ngrok); xem KAGGLE_DEPLOYMENT.md
config.py                 # Đường dẫn, model, tham số (đọc từ biến môi trường, .env.local khi chạy local)
prompts/                  # rag_prompt.txt, quiz_prompt.txt (quiz_prompt chỉ engine V2 cũ dùng)
tests/                    # unittest/pytest; tests/frontend_source.py đọc toàn bộ JS frontend
```

## 2. Luồng request backend

```text
Trình duyệt
  -> (Kaggle: Cloudflare Worker -> Cloudflare Quick Tunnel -> nginx :7860)
  -> /api/* -> FastAPI (backend.main:app, :8000, một worker)
  -> backend/api/<router>.py      : validate HTTP, Depends(require_current_user), map lỗi -> HTTP
  -> backend/<feature>_service.py : nghiệp vụ, gọi LLM/embedding qua Ollama khi cần
  -> backend/<feature>_store.py   : CSDL quan hệ (SQLite hoặc PostgreSQL)
     vector_store.py              : Chroma (SQLite mode) hoặc pgvector (DATABASE_URL)
```

- `main.py` chỉ còn: tạo app + CORS, route hệ thống (`/`, `/api/model/health`, `/api/health`,
  `/api/models`, `/api/models/{id}/prepare`), 2 route admin benchmark, và các `app.include_router(...)`.
- **Thứ tự `include_router` trong `main.py` giữ đúng thứ tự đăng ký route gốc** (một số nhóm dùng
  2 router, ví dụ `quiz_attempts.history_router`/`router`, `quiz_generation.router`/`regenerate_router`).
  Không sắp xếp lại khi không cần.
- Quy tắc phụ thuộc (đã kiểm tra): router không import `main`; service/store không import `backend.api`;
  không có vòng import trong `backend`; store không gọi LLM/HTTP.
- Upload và index chạy trong threadpool dưới khoá tiến trình `_INDEXING_LOCK` (`api/documents.py`).
  Khoá này dựa vào việc chỉ có **một** worker Uvicorn.
- Xoá tài liệu (`DELETE /api/sources/{id}`, `api/documents.py`) dọn dữ liệu theo thứ tự: index/vector
  -> quiz -> summary -> flashcards -> planner (kèm đồng bộ Google Calendar) -> nguồn của hội thoại.

## 3. Kiến trúc Quiz

| Thành phần | Vai trò |
| --- | --- |
| `quiz_service.py` | Sinh/sinh lại quiz (luồng "Study Units": `generate_quiz` -> `_generate_quiz_from_units`), thư viện quiz, xoá quiz, dashboard |
| `quiz_units.py` | Chia tài liệu thành study units, prompt, parse/validate/chọn câu hỏi, fill_blank |
| `flashcard_quiz_service.py` | Câu matching / multi_blank dựng từ flashcards (không gọi LLM); quiz luyện tập từ flashcards |
| `quiz_attempt_service.py` | Vòng đời bài làm: resume, autosave, nộp/chấm điểm, làm lại, lịch sử, kết quả, giải thích |
| `quiz_common.py` | `QUIZ_DIFFICULTIES`, `list_indexed_documents`, `_document_lookup` dùng chung |
| `quiz_store.py` | Lưu quiz, attempt, tiến độ, lịch sử, giải thích, topic mastery |
| `quiz_legacy_v2.py` | Engine V2 dựa trên Planner — không được gọi từ production |
| API | `api/quiz_generation.py` (xem/sinh/sinh lại), `api/quiz_attempts.py` (lịch sử + player), `api/quiz_library.py` (danh sách/xoá) |

- Quiz mới yêu cầu tài liệu đã có flashcards (409 nếu chưa). LLM chỉ viết câu trắc nghiệm còn
  thiếu; mọi câu đều được kiểm tra tất định (không dùng LLM). Chấm điểm cũng tất định.
- Truy xuất chunk dùng chung ở `document_retrieval.py` (lọc metadata, không embedding); kiểm tra
  văn bản an toàn ở `text_safety.py`.

## 4. Subjects (môn học)

| Thành phần | Vai trò |
| --- | --- |
| `subject_store.py` | Bảng `subjects` (tên duy nhất theo owner, không phân biệt hoa thường) và cột `indexed_documents.subject_id`; gán/chuyển/bỏ gán theo kiểu all-or-nothing |
| `api/subjects.py` | `GET/POST /api/subjects`, `PATCH/DELETE /api/subjects/{id}`, `POST /api/subjects/assign` |
| `api/documents.py` | Upload có thể kèm `subject_id` hoặc `new_subject_name`; gán sau khi index thành công, tài liệu đã có subject giữ nguyên |
| `subject_grouping.py` | Gom nhóm chỉ-đọc cho Overview, không ghi CSDL |

- Subject chỉ để tổ chức: xoá subject chỉ bỏ gán tài liệu, không xoá dữ liệu nào của tài liệu.
- Planner nhóm tài liệu theo subject **chỉ ở giao diện** (`js/planner.js`, `js/subjects.js`); mỗi
  material của plan luôn là một tài liệu, scheduler không biết đến subject.

## 5. Kiến trúc Planner

| Thành phần | Vai trò |
| --- | --- |
| `study_time.py` | Toán học khoảng thời gian thuần (phút trong ngày, merge/subtract, `free_minutes_by_date`) |
| `study_planner_store.py` | Persistence cho cả V1 (task/block/availability) và V2 (plan/material/session) |
| `study_planner_service.py` | Planner V1 (task/block, scheduler cũ) + `add_plan_material`, `build_scheduling_context` dùng cho V2 |
| `study_scheduler.py` + `study_scheduler_contracts.py` | Scheduler V2 tất định, không dùng LLM: `SchedulingContext` -> `ScheduleResult` |
| `study_combined_sessions.py`, `study_session_duration.py` | Phiên kết hợp 2 hoạt động; độ dài phiên |
| `study_adaptation.py` / `study_placement.py` | Lập lại kế hoạch thích ứng / đặt phiên do người học kéo thả |
| `study_preferences.py` | Mục tiêu phút/ngày, thời gian nghỉ, chiến lược, thời lượng theo hoạt động |
| `study_plan_api_service.py` | Điều phối API V2: preview, confirm, vòng đời session, tiến độ, lỗi có mã |
| `study_progress.py`, `document_study_state.py` | Trạng thái học và tiến độ theo tài liệu (chỉ đọc) |
| API | `api/planner_legacy.py` (tasks/blocks/availability — availability dùng chung với V2), `api/planner_v2.py` (plans/materials/preview/confirm/sessions/adaptation/preferences/progress) |

- Preview không lưu gì; confirm tính lại lịch phía server rồi lưu sessions + `schedule_run` trong
  một transaction. Client không bao giờ gửi danh sách session.

### Google Calendar (MVP, một chiều mỗi hướng)

| Thành phần | Vai trò |
| --- | --- |
| `google_calendar_service.py` | OAuth (authorization code, offline, scope `calendar.freebusy` + `calendar.app.created` + `calendar.calendarlist.readonly` (tuỳ chọn)), CalendarList + freeBusy trên primary và các lịch đang hiển thị (trừ "AI Tutor Study Plan"), mã hoá refresh token (Fernet), refresh/revoke, gọi REST Calendar qua httpx (`_transport` là seam cho test) |
| `google_calendar_store.py` | Cùng CSDL với planner: `google_calendar_connections`, `google_calendar_event_links`, `google_oauth_states` |
| `google_calendar_sync.py` | Busy time -> Planner (dòng availability `busy=True`, `free_minutes_by_date` trừ đi) và đồng bộ session đã confirm -> lịch "AI Tutor Study Plan" (idempotent: event id tất định + hash nội dung) |
| API | `api/google_calendar.py`: `/api/integrations/google-calendar/{status,connect,callback,disconnect,settings,sync,busy}` |

- Scheduler không đổi: chỉ availability đầu vào bị trừ busy time (`study_plan_api_service._effective_availability`),
  và chỉ khi người học bật "Avoid conflicts".
- Mọi hành động planner gọi `google_calendar_sync.after_planner_change` SAU khi đã commit: lỗi Google chỉ ghi
  `sync_status="error"`, không bao giờ rollback hành động. Chưa kết nối = hành vi và response như cũ.
- Biến môi trường: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`,
  `GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY` (tuỳ chọn `AI_TUTOR_FRONTEND_URL` khi frontend khác origin với backend).
  Khi chạy local, có thể đặt trong `.env.local` (xem `.env.example`).

## 6. Frontend

- Không dùng framework/bundler. `index.html` nạp theo thứ tự:
  `app-config.js` -> `js/api.js` -> `js/utils.js` -> `js/session.js` -> `js/chat_stream.js`
  -> `js/tutor.js` -> `js/quiz.js` -> `js/planner.js` -> `js/subjects.js` -> `js/home.js`
  -> `js/state.js` -> `app.js`.
- Tất cả là **classic script dùng chung global scope** (không phải ES module). Mỗi file `js/*.js`
  chứa các hàm của một tính năng, cùng hằng số và biến trạng thái của tính năng đó (ví dụ
  `SUBJECTS_API_URL`, `userSubjects` trong `subjects.js`). Việc tra DOM, gắn listener và khởi
  động chạy trong `app.js` theo thứ tự gốc. `state.js` nạp sau `planner.js` vì `plannerWeekStart`
  gọi `plannerMondayOf` lúc tải.
- `js/api.js`: `API_BASE_URL`, bọc `window.fetch` (luôn gửi cookie), các URL endpoint, `fetchJson`.
  `js/chat_stream.js`: đọc luồng SSE của Chat và render Markdown có giới hạn tần suất.
- `server.js` phục vụ file tĩnh và sinh `/app-config.js`: `API_BASE_URL` mặc định là
  `http://127.0.0.1:8000` (local, khác origin), còn trên Kaggle là chuỗi rỗng (cùng origin qua nginx).
- Thêm file script mới: thêm thẻ `<script>` vào `index.html` trước `app.js`, và nhớ các test trình
  duyệt copy cả thư mục `frontend/js/`.

## 7. Persistence và model

**Hai profile lưu trữ, mỗi lần chạy chỉ dùng một**, chọn lúc khởi động bởi `DATABASE_URL`
(`db.selected_backend()`; `AI_TUTOR_ALLOW_SQLITE=1` buộc dùng SQLite kể cả khi có `DATABASE_URL`):

| | SQLite mode (mặc định khi không có `DATABASE_URL`) | Durable mode (`DATABASE_URL`) |
| --- | --- | --- |
| CSDL quan hệ | SQLite `DATABASE_PATH` (mặc định `data/conversations.db`): auth, hội thoại, tài liệu đã index, subjects, summary + `summary_jobs`, flashcards, quiz, mastery, planner, Google Calendar | PostgreSQL (Supabase) với cùng các bảng |
| Vector | Chroma tại `VECTORSTORE_DIR`, collection `study_documents` | pgvector, bảng `document_chunks` |
| File gốc | `data/users/<owner_id>/` | Supabase Storage, bucket private (hoặc đĩa cục bộ nếu `AI_TUTOR_DOCUMENT_STORAGE=local`) |

- Embedding luôn là `bge-m3` (1024 chiều) qua Ollama, dùng khi index chunk và khi truy vấn Chat.
  Summary, Flashcards và Quiz đọc chunk bằng lọc metadata, không embed lại.
- Model sinh: `model_registry.py` ánh xạ id công khai sang tham chiếu Ollama. Model hoạt động là
  `qwen-2.5-7b` (mặc định cho Chat, Summary, Flashcards, Quiz và tinh chỉnh topic) và `gemma3-12b`
  (kéo lười lần đầu được chọn). Danh sách hiển thị lấy từ `OLLAMA_GENERATION_MODELS`; notebook
  Kaggle đang dùng chỉ đặt `qwen-2.5-7b`. `deepseek-r1-14b` và `glm4-9b` tạm ngưng
  (`enabled=False`): không chọn/không kéo được, chỉ giữ để đọc artifact cũ. Không bao giờ tự đổi
  sang model khác.
- Qwen được kéo từ Hugging Face (`hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M`); Gemma và
  `bge-m3` từ thư viện Ollama. `LLM_BACKEND=vllm` (tuỳ chọn) chuyển sinh văn bản Qwen sang vLLM
  (`deployment/start_vllm.sh`, :8001); embedding vẫn ở Ollama.

## 8. Triển khai

Chi tiết từng bước nằm trong [`KAGGLE_DEPLOYMENT.md`](../KAGGLE_DEPLOYMENT.md); sơ đồ ở
[`docs/system-architecture/`](system-architecture/README.md). Tóm tắt:

| Cấu hình | Lối vào | Lưu trữ |
| --- | --- | --- |
| Local | Trình duyệt -> Node :3000 (UI) và FastAPI :8000 (API, CORS) | SQLite mode (hoặc `DATABASE_URL` trong `.env.local`) |
| **Kaggle đang dùng** (`kaggle_run_ollama_cloudflare.ipynb`) | Cloudflare Worker (URL cố định) -> Cloudflare Quick Tunnel -> nginx :7860 | SQLite mode (`AI_TUTOR_ALLOW_SQLITE=1`) + snapshot Kaggle Dataset mỗi ~10 phút |
| Kaggle durable mode (tuỳ chọn) | Tunnel công khai -> nginx :7860 | Supabase PostgreSQL + pgvector + Storage |
| Kaggle cũ (`kaggle_run.ipynb`) | ngrok -> nginx :7860 | Không đặt `AI_TUTOR_ALLOW_SQLITE`, nên chỉ chạy được ở durable mode |

Trên Kaggle, notebook: cấu hình -> clone/cập nhật repo -> khôi phục `data/` + `vectorstore/` từ
Kaggle Dataset (một lần mỗi runtime) -> cài apt/Ollama/pip/npm/cloudflared song song -> mở tunnel
và cập nhật Worker -> chạy `deployment/start_kaggle.sh`. Script này: kiểm tra lưu trữ (SQLite mode
hoặc `durable_setup`) -> `ollama serve` -> kéo/warm Qwen + `bge-m3` -> kiểm tra Chroma -> index tăng
dần `data/` -> `uvicorn backend.main:app` (:8000) -> `node frontend/server.js` (:3000,
`API_BASE_URL=""`) -> nginx (:7860: `/api/` -> 8000, `/` -> 3000) -> kiểm tra `/api/health`.

## 9. Sửa tính năng ở đâu

| Muốn sửa | Backend | Frontend |
| --- | --- | --- |
| Đăng nhập/phiên | `api/auth.py`, `api/deps.py`, `auth_store.py` | `app.js` (auth shell) |
| Tải/xoá tài liệu, index | `api/documents.py`, `ingest.py`, `topic_extractor.py`, `indexed_document_store.py`, `vector_store.py`, `document_storage.py` | `js/tutor.js` (sources panel) |
| Subjects | `api/subjects.py`, `subject_store.py`, `subject_grouping.py` | `js/subjects.js` |
| Chat AI Tutor | `api/conversations.py`, `rag_service.py`, `conversation_store.py` | `js/tutor.js`, `js/chat_stream.js` |
| Summary | `api/summary.py`, `summary_jobs.py`, `summary_service.py`, `summary_store.py` | `js/session.js` |
| Flashcards | `api/flashcards.py`, `flashcard_service.py`, `flashcard_store.py` | `js/session.js` |
| Sinh quiz | `api/quiz_generation.py`, `quiz_service.py`, `quiz_units.py`, `flashcard_quiz_service.py` | `js/quiz.js` |
| Làm bài/chấm điểm/lịch sử | `api/quiz_attempts.py`, `quiz_attempt_service.py`, `quiz_store.py` | `js/quiz.js` |
| Mastery/Dashboard/gợi ý | `api/learning.py`, `mastery_service.py`, `knowledge_gap_service.py`, `recommendation_service.py` | `js/home.js`, `js/session.js` |
| Planner | `api/planner_v2.py`, `study_plan_api_service.py`, `study_scheduler.py`, `study_adaptation.py` | `js/planner.js` |
| Google Calendar | `api/google_calendar.py`, `google_calendar_service.py`, `google_calendar_sync.py`, `google_calendar_store.py` | `js/planner.js` |
| Model/benchmark | `model_registry.py`, `llm_backend.py`, `main.py` (admin), `model_benchmark_service.py` | `app.js` (model selector, admin) |
| CSDL / lưu trữ bền vững | `db.py`, `vector_store.py`, `document_storage.py`, `durable_setup.py` | — |
| Chạy trên Kaggle | `deployment/`, `kaggle_run_ollama_cloudflare.ipynb`, `config.py` | `frontend/server.js` |
