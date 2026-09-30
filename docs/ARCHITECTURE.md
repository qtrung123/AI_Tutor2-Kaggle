# Kiến trúc AI Tutor

Tài liệu mô tả cấu trúc hiện tại của mã nguồn (sau đợt refactor Phase 2). Mọi thông tin dưới đây
lấy trực tiếp từ code; khi code thay đổi, hãy cập nhật tài liệu này cùng lúc.

## 1. Cấu trúc thư mục

```text
backend/
  main.py                 # Tạo FastAPI app, CORS, route hệ thống/admin, include các router
  api/                    # Lớp HTTP: mỗi file là một nhóm route theo tính năng
    deps.py               # require_current_user / require_admin_user (auth dùng chung)
    auth.py  documents.py  summary.py  flashcards.py  learning.py  conversations.py
    quiz_library.py  quiz_generation.py  quiz_attempts.py  planner_legacy.py  planner_v2.py
  *_service.py            # Nghiệp vụ theo tính năng
  *_store.py              # Lưu trữ SQLite (chỉ persistence)
  quiz_units.py quiz_validation.py quiz_options.py quiz_common.py quiz_diagnostics.py
  quiz_legacy_v2.py       # Engine Quiz V2 cũ, KHÔNG nằm trên luồng chạy thật (giữ để so sánh/test)
  study_*.py              # Planner: scheduler, adaptation, placement, progress, time utilities
  document_retrieval.py text_safety.py ingest.py rag_service.py model_registry.py ...
frontend/
  index.html  styles.css  server.js
  js/                     # Các classic script theo tính năng, nạp TRƯỚC app.js
  app.js                  # Tra cứu DOM, gắn event listener, khởi động ứng dụng
deployment/               # start_kaggle.sh, nginx.kaggle.conf, kaggle_persist.py
kaggle_run.ipynb          # Notebook chạy toàn bộ hệ thống trên Kaggle
config.py                 # Đường dẫn, model, tham số (đọc từ biến môi trường)
prompts/                  # rag_prompt.txt, quiz_prompt.txt (quiz_prompt chỉ engine V2 cũ dùng)
tests/                    # unittest/pytest; tests/frontend_source.py đọc toàn bộ JS frontend
```

## 2. Luồng request backend

```text
Trình duyệt -> (nginx :7860 trên Kaggle) -> /api/* -> FastAPI (backend.main:app, :8000)
  -> backend/api/<router>.py      : validate HTTP, Depends(require_current_user), map lỗi -> HTTP
  -> backend/<feature>_service.py : nghiệp vụ, gọi LLM qua Ollama khi cần
  -> backend/<feature>_store.py   : SQLite (DATABASE_PATH) ; Chroma (VECTORSTORE_DIR) qua ingest/retrieval
```

- `main.py` chỉ còn: tạo app + CORS, route hệ thống (`/`, `/api/model/health`, `/api/health`,
  `/api/models`, `/api/models/{id}/prepare`), 2 route admin benchmark, và các `app.include_router(...)`.
- **Thứ tự `include_router` trong `main.py` giữ đúng thứ tự đăng ký route gốc** (một số nhóm dùng
  2 router, ví dụ `quiz_attempts.history_router`/`router`, `quiz_generation.router`/`regenerate_router`).
  Không sắp xếp lại khi không cần.
- Quy tắc phụ thuộc (đã kiểm tra): router không import `main`; service/store không import `backend.api`;
  không có vòng import trong `backend`; store không gọi LLM/HTTP.
- Xoá tài liệu (`DELETE /api/sources/{id}`, `api/documents.py`) dọn dữ liệu theo thứ tự: index/vector
  -> quiz -> summary -> flashcards -> planner -> nguồn của hội thoại.

## 3. Kiến trúc Quiz

| Thành phần | Vai trò |
| --- | --- |
| `quiz_service.py` | Sinh/sinh lại quiz (luồng "Study Units": `generate_quiz` -> `_generate_quiz_from_units`), thư viện quiz, xoá quiz, dashboard |
| `quiz_units.py` | Chia tài liệu thành study units, prompt, parse/validate/chọn câu hỏi, fill_blank |
| `quiz_attempt_service.py` | Vòng đời bài làm: resume, autosave, nộp/chấm điểm, làm lại, lịch sử, kết quả, giải thích |
| `quiz_common.py` | `QUIZ_DIFFICULTIES`, `list_indexed_documents`, `_document_lookup` dùng chung |
| `quiz_store.py` | Lưu quiz, attempt, tiến độ, lịch sử, giải thích (SQLite) |
| `quiz_legacy_v2.py` | Engine V2 dựa trên Planner — không được gọi từ production |
| API | `api/quiz_generation.py` (xem/sinh/sinh lại), `api/quiz_attempts.py` (lịch sử + player), `api/quiz_library.py` (danh sách/xoá) |

Truy xuất chunk dùng chung ở `document_retrieval.py`; kiểm tra văn bản an toàn ở `text_safety.py`.

## 4. Kiến trúc Planner

| Thành phần | Vai trò |
| --- | --- |
| `study_time.py` | Toán học khoảng thời gian thuần (phút trong ngày, merge/subtract, `free_minutes_by_date`) |
| `study_planner_store.py` | Persistence cho cả V1 (task/block/availability) và V2 (plan/material/session) |
| `study_planner_service.py` | Planner V1 (task/block, scheduler cũ) + `add_plan_material`, `build_scheduling_context` dùng cho V2 |
| `study_scheduler.py` + `study_scheduler_contracts.py` | Scheduler V2 tất định: `SchedulingContext` -> `ScheduleResult` |
| `study_adaptation.py` / `study_placement.py` | Lập lại kế hoạch thích ứng / đặt phiên do người học chọn |
| `study_plan_api_service.py` | Điều phối API V2: preview, confirm, vòng đời session, tiến độ, lỗi có mã |
| `study_progress.py`, `document_study_state.py` | Trạng thái học và tiến độ theo tài liệu |
| API | `api/planner_legacy.py` (tasks/blocks/availability — availability dùng chung với V2), `api/planner_v2.py` (plans/sessions/progress) |

### Google Calendar (MVP, một chiều mỗi hướng)

| Thành phần | Vai trò |
| --- | --- |
| `google_calendar_service.py` | OAuth (authorization code, offline, scope `calendar.freebusy` + `calendar.app.created` + `calendar.calendarlist.readonly` (tuỳ chọn)), CalendarList + freeBusy trên primary và các lịch đang hiển thị (trừ "AI Tutor Study Plan"), mã hoá refresh token (Fernet), refresh/revoke, gọi REST Calendar qua httpx (`_transport` là seam cho test) |
| `google_calendar_store.py` | SQLite (cùng DB với planner): `google_calendar_connections`, `google_calendar_event_links`, `google_oauth_states` |
| `google_calendar_sync.py` | Busy time -> Planner (dòng availability `busy=True`, `free_minutes_by_date` trừ đi) và đồng bộ session đã confirm -> lịch "AI Tutor Study Plan" (idempotent: event id tất định + hash nội dung) |
| API | `api/google_calendar.py`: `/api/integrations/google-calendar/{status,connect,callback,disconnect,settings,sync,busy}` |

- Scheduler không đổi: chỉ availability đầu vào bị trừ busy time (`study_plan_api_service._effective_availability`).
- Mọi hành động planner gọi `google_calendar_sync.after_planner_change` SAU khi đã commit: lỗi Google chỉ ghi
  `sync_status="error"`, không bao giờ rollback hành động. Chưa kết nối = hành vi và response như cũ.
- Biến môi trường: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`,
  `GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY` (tuỳ chọn `AI_TUTOR_FRONTEND_URL` khi frontend khác origin với backend).

## 5. Frontend

- Không dùng framework/bundler. `index.html` nạp theo thứ tự:
  `app-config.js` -> `js/api.js` -> `js/utils.js` -> `js/session.js` -> `js/tutor.js` -> `js/quiz.js`
  -> `js/planner.js` -> `js/home.js` -> `js/state.js` -> `app.js`.
- Tất cả là **classic script dùng chung global scope** (không phải ES module). Các file `js/*.js`
  (trừ `api.js`, `state.js`) chỉ chứa khai báo `function`; mọi câu lệnh chạy lúc tải (tra DOM, gắn
  listener, khởi động) nằm trong `app.js` theo thứ tự gốc. `state.js` nạp sau `planner.js` vì
  `plannerWeekStart` gọi `plannerMondayOf` lúc tải.
- `js/api.js`: `API_BASE_URL`, bọc `window.fetch` (gửi cookie), các URL endpoint, `fetchJson`.
- Thêm file script mới: thêm thẻ `<script>` vào `index.html` trước `app.js`, và nhớ các test trình
  duyệt copy cả thư mục `frontend/js/`.

## 6. Persistence và model

- SQLite: `DATABASE_PATH` (mặc định `data/conversations.db`) chứa auth, hội thoại, quiz, summary,
  flashcards, planner, tài liệu đã index. File tải lên nằm ở `data/users/<owner_id>/`.
- Vector: Chroma tại `VECTORSTORE_DIR`, collection `study_documents`, embedding `bge-m3` qua Ollama.
- Model: `model_registry.py` ánh xạ id công khai đang hoạt động (`qwen-2.5-7b`, `gemma3-12b`) sang
  tham chiếu Ollama; `prepare_generation_model` kéo model lười (lazy) trước khi sinh. `deepseek-r1-14b`
  và `glm4-9b` tạm ngưng (`enabled=False`): không chọn/không kéo được, chỉ giữ để đọc artifact cũ.
  Mặc định cho Quiz/Chat/Summary/Flashcards là `qwen-2.5-7b`. Không bao giờ tự đổi sang model khác.

## 7. Luồng chạy trên Kaggle

1. `kaggle_run.ipynb`: cấu hình -> clone/cập nhật repo -> khôi phục `data/` + `vectorstore/` từ
   Kaggle Dataset riêng tư (một lần mỗi runtime, `deployment/kaggle_persist.py`) -> cài apt/pip/npm
   -> (tuỳ chọn) chạy test -> cài Ollama -> chạy `deployment/start_kaggle.sh`.
2. `start_kaggle.sh`: `ollama serve` -> kéo/warm chỉ model mặc định (Qwen) + embedding -> index tăng
   dần tài liệu trong `data/` -> `uvicorn backend.main:app` (:8000) -> `node frontend/server.js`
   (:3000, `API_BASE_URL=""`) -> nginx (:7860: `/api/` -> 8000, `/` -> 3000) -> kiểm tra `/api/health`.
3. Notebook mở tunnel ngrok tới cổng 7860 và lưu snapshot trạng thái khoảng 10 phút một lần.

## 8. Sửa tính năng ở đâu

| Muốn sửa | Backend | Frontend |
| --- | --- | --- |
| Đăng nhập/phiên | `api/auth.py`, `api/deps.py`, `auth_store.py` | `app.js` (auth shell) |
| Tải/xoá tài liệu, index | `api/documents.py`, `ingest.py`, `indexed_document_store.py` | `js/tutor.js` (sources panel) |
| Chat AI Tutor | `api/conversations.py`, `rag_service.py`, `conversation_store.py` | `js/tutor.js` |
| Summary | `api/summary.py`, `summary_service.py`, `summary_store.py` | `js/session.js` |
| Flashcards | `api/flashcards.py`, `flashcard_service.py`, `flashcard_store.py` | `js/session.js` |
| Sinh quiz | `api/quiz_generation.py`, `quiz_service.py`, `quiz_units.py` | `js/quiz.js` |
| Làm bài/chấm điểm/lịch sử | `api/quiz_attempts.py`, `quiz_attempt_service.py`, `quiz_store.py` | `js/quiz.js` |
| Mastery/Dashboard/gợi ý | `api/learning.py`, `mastery_service.py`, `knowledge_gap_service.py`, `recommendation_service.py` | `js/home.js`, `js/session.js` |
| Planner | `api/planner_v2.py`, `study_plan_api_service.py`, `study_scheduler.py`, `study_adaptation.py` | `js/planner.js` |
| Model/benchmark | `model_registry.py`, `main.py` (admin), `model_benchmark_service.py` | `app.js` (model selector, admin) |
| Chạy trên Kaggle | `deployment/`, `kaggle_run.ipynb`, `config.py` | `frontend/server.js` |
