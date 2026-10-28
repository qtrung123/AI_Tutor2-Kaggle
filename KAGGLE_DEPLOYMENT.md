# Triển khai AI Tutor trên Kaggle

Sơ đồ kiến trúc và các cấu hình triển khai: [`docs/system-architecture/`](docs/system-architecture/README.md).
Cấu trúc mã nguồn: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Có hai notebook. Cả hai đều gọi cùng script `deployment/start_kaggle.sh`:

| Notebook | Trạng thái | Lối vào công khai | Lưu trữ |
| --- | --- | --- | --- |
| [`kaggle_run_ollama_cloudflare.ipynb`](kaggle_run_ollama_cloudflare.ipynb) | **Đang dùng** | Cloudflare Worker (URL cố định) -> Cloudflare Quick Tunnel -> nginx | SQLite mode + snapshot Kaggle Dataset |
| [`kaggle_run.ipynb`](kaggle_run.ipynb) | Cũ (sửa lần cuối 2026-09-27) | ngrok -> nginx | Chỉ chạy được ở durable mode (Supabase), xem [mục 7](#7-tuỳ-chọn-durable-mode-supabase) |

## 1. Kiến trúc đang dùng

```text
https://ai-tutor-gateway.<account>.workers.dev        (URL cố định cho người dùng)
        |  Cloudflare Worker, proxy tới origin lưu trong Workers KV (khoá ORIGIN_URL)
        v
https://<random>.trycloudflare.com                    (Quick Tunnel, đổi mỗi lần chạy)
        v
nginx 127.0.0.1:7860
   |-- /api/* --> FastAPI 127.0.0.1:8000 --> Ollama 127.0.0.1:11434 (Qwen2.5-7B + bge-m3, GPU)
   `-- /*     --> Node.js 127.0.0.1:3000          SQLite + Chroma + data/ trong /kaggle/working
                                                  <-> Kaggle Dataset riêng tư (snapshot ~10 phút)
```

- Mỗi lần chạy, notebook mở một Quick Tunnel mới tới nginx. Sau đó notebook ghi URL của tunnel
  vào Workers KV qua Cloudflare API, nên URL Worker cố định luôn trỏ tới phiên đang chạy. Với
  `EARLY_GATEWAY_UPDATE = True`, tunnel được mở và KV được cập nhật trong lúc model đang tải.
- Notebook chỉ in "Permanent AI Tutor URL (verified)" khi `/api/health` qua Worker trả 200. Workers
  KV có độ trễ lan truyền khoảng 60 giây.
- Mã nguồn Cloudflare Worker **không** nằm trong repository này; notebook chỉ cập nhật KV.
- Frontend gọi `/api/...` theo đường dẫn tương đối, nên UI và API cùng origin. nginx giữ nguyên
  prefix `/api` khi chuyển tiếp. Giới hạn upload là 100 MB, timeout 600 giây.
- Khi chạy local bằng `npm start`, frontend server phát cấu hình mặc định `http://127.0.0.1:8000`
  (hai cổng, backend cho phép CORS). Có thể override bằng biến `API_BASE_URL` của Node hoặc
  `localStorage.setItem("API_BASE_URL", "...")`. Script Kaggle đặt giá trị rỗng để dùng same-origin.

## 2. Kaggle Secrets cần có

Tạo trong **Add-ons > Secrets** và attach vào notebook. Không dán giá trị vào cell hoặc source code.

| Secret | Bắt buộc | Dùng cho |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`, `GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY`, `AI_TUTOR_FRONTEND_URL` | Có: notebook dừng nếu thiếu | Google Calendar OAuth. Nên đặt redirect URI và frontend URL theo URL Worker cố định, vì URL trycloudflare đổi mỗi lần chạy |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_KV_NAMESPACE_ID` | Để có URL cố định | Ghi `ORIGIN_URL` vào Workers KV. Thiếu thì app vẫn chạy qua URL trycloudflare tạm |
| `KAGGLE_USERNAME`, `KAGGLE_KEY` | Để giữ dữ liệu | Khôi phục/lưu snapshot Kaggle Dataset (mục 5) |

## 3. Chạy notebook

1. Mở [`kaggle_run_ollama_cloudflare.ipynb`](kaggle_run_ollama_cloudflare.ipynb), chọn **Copy & Edit**.
2. Trong **Settings**, chọn GPU accelerator và bật Internet.
3. Attach các secret ở mục 2. Có thể attach thêm input chứa bài giảng PDF/TXT.
4. Chỉ sửa cell **User configuration**: `STATE_DATASET_SLUG` (dạng `"<kaggle-username>/<slug>"`),
   tuỳ chọn `CACHE_DATASET_SLUG` và `LECTURE_INPUT_DIR`.
5. Chọn **Run All**. Mở URL in ở cuối: "Permanent AI Tutor URL (verified)".

Notebook chạy theo thứ tự sau:

1. Nạp secret Google.
2. Clone repo, hoặc `fetch` + `pull --ff-only` khi `UPDATE_CODE = True`.
3. Khôi phục state, một lần mỗi runtime.
4. Cài apt + Ollama, pip, npm và cloudflared song song. Thành phần nào đã có sẵn thì bỏ qua.
5. Mở tunnel và cập nhật Worker.
6. Chạy `start_kaggle.sh`.
7. Bật snapshot định kỳ.
8. Kiểm tra health qua tunnel và qua Worker.
9. In thời gian từng giai đoạn.

Khi **Run All** lại trong cùng runtime (warm run), các bước cài đặt, kéo model, test và, nếu code,
cấu hình và service không đổi, cả việc khởi động lại service đều được bỏ qua. Đặt
`FORCE_RESTART = True` để buộc khởi động lại. Test chỉ chạy khi `QUICK_START = False`. Các cell
trong **Manual actions** không làm gì trong một Run All bình thường.

`start_kaggle.sh` chạy các bước sau theo thứ tự:

1. Kiểm tra chế độ lưu trữ (mục 5 và 7).
2. Chạy `ollama serve`.
3. Kéo và warm model mặc định cùng `bge-m3`.
4. Kiểm tra embedding marker của Chroma.
5. Index tăng dần các file trong `data/`. File cùng hash được bỏ qua. PDF/TXT từ
   `LECTURE_INPUT_DIR` được copy vào `data/` trước bước này.
6. Khởi động Uvicorn :8000, Node :3000 (`API_BASE_URL=""`) và nginx :7860.
7. Kiểm tra `/api/health` cả trực tiếp và qua nginx.

Script lưu PID và dừng các process của lần chạy trước trước khi khởi động lại.

## 4. Model

| Biến (cell cấu hình) | Giá trị hiện tại | Ghi chú |
| --- | --- | --- |
| `OLLAMA_CHAT_MODEL` | `hf.co/bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M` | Kéo trực tiếp từ Hugging Face qua `ollama pull` (thử `--insecure` trước) |
| `OLLAMA_DEFAULT_GENERATION_MODEL` | `qwen-2.5-7b` | id công khai trong `backend/model_registry.py` |
| `OLLAMA_GENERATION_MODELS` | `qwen-2.5-7b` | Danh sách model hiển thị trên UI. Thêm `gemma3-12b` để cho chọn Gemma 3 12B, được kéo lười khi dùng lần đầu |
| `PRELOAD_ALL_MODELS` | `False` | `True` chỉ dùng khi benchmark (kéo sẵn mọi model trong danh sách) |
| `OLLAMA_EMBEDDING_MODEL` | `bge-m3` | Phải giống nhau giữa local và Kaggle |
| `LLM_BACKEND` | `ollama` | Notebook này không chạy vLLM. `vllm` là tuỳ chọn của `start_kaggle.sh` (`deployment/start_vllm.sh`, :8001) |

Các biến runtime khác do notebook hoặc script đặt: `OLLAMA_HOST=http://127.0.0.1:11434`,
`AI_TUTOR_DATA_DIR=<PROJECT_ROOT>/data`, `AI_TUTOR_VECTORSTORE_DIR=<PROJECT_ROOT>/vectorstore`,
`AI_TUTOR_DATABASE_PATH=<PROJECT_ROOT>/data/conversations.db`, `AI_TUTOR_ALLOW_SQLITE=1`,
`REBUILD_CHROMA_ON_EMBEDDING_CHANGE=1`.

**GGUF từ Kaggle Input (cách cũ, vẫn được hỗ trợ):** nếu `OLLAMA_CHAT_MODEL` không phải `hf.co/...`
và model chưa có, `start_kaggle.sh` tạo model từ `GGUF_MODEL_PATH` (một file `.gguf` trong
`/kaggle/input`) bằng một `Modelfile` runtime. Đặt `RECREATE_OLLAMA_MODEL = True` nếu chủ động
muốn tạo lại. Không đưa file GGUF vào Git repository.

**Chroma và embedding:** script ghi tên model embedding vào marker
`<AI_TUTOR_VECTORSTORE_DIR>/.embedding_model`. Nếu marker khác `bge-m3`, hoặc database có dữ liệu
nhưng thiếu marker, script đổi tên ChromaDB cũ thành bản backup có timestamp, xoá registry tài liệu
đã index trong SQLite rồi index lại. Cơ chế này từ chối chạy ngoài `/kaggle/working`. Đặt
`REBUILD_CHROMA_ON_EMBEDDING_CHANGE=0` để script dừng với lỗi thay vì tự rebuild. Ở durable mode
không có Chroma cục bộ nên bước này được bỏ qua.

## 5. Dữ liệu và tính lâu bền (SQLite mode)

`/kaggle/input` là read-only. Ở SQLite mode, tài liệu upload, `vectorstore/` (Chroma) và
`data/conversations.db` nằm dưới `/kaggle/working/AI_Tutor2` và mất khi session kết thúc, trừ khi
được snapshot. `start_kaggle.sh` dừng nếu không có `DATABASE_URL`, trừ khi `AI_TUTOR_ALLOW_SQLITE=1`.
Notebook đang dùng đặt sẵn giá trị này, nên không bao giờ âm thầm chạy SQLite.

Notebook tự động hoá việc lưu bằng hai Kaggle Dataset riêng tư. Không cần tạo trước: lần lưu đầu
tiên sẽ tự tạo dataset.

| Biến cấu hình | Nội dung lưu | Tác dụng |
| --- | --- | --- |
| `STATE_DATASET_SLUG` | `data/` và `vectorstore/` | Tài khoản, hội thoại, tài liệu đã index, quiz, planner… còn lại qua session mới |
| `CACHE_DATASET_SLUG` | Model Ollama + cache pip/npm | Không phải tải lại model và cài lại package mỗi lần chạy |

- **Khôi phục:** chỉ chạy một lần mỗi runtime, trước khi khởi động service. Lần đầu chưa có bản lưu
  thì bỏ qua, không báo lỗi. Các journal SQLite cũ được dọn và snapshot được kiểm tra trước khi dùng
  (`deployment/kaggle_persist.py`).
- **Lưu tự động:** khoảng 10 phút một lần (`STATE_SNAPSHOT_INTERVAL_SECONDS`) sau khi service đã chạy.
- **Lưu thủ công:** trong **Manual actions**, đặt `SAVE_STATE_NOW = True` rồi chạy riêng cell đó,
  nên làm trước khi dừng session. Đặt `SAVE_CACHE_NOW = True` để lưu cache; chỉ cần sau lần setup
  đầu tiên hoặc khi đổi model hay package.

Để trống `STATE_DATASET_SLUG`/`CACHE_DATASET_SLUG` nếu không muốn dữ liệu rời khỏi máy Kaggle.
Dataset luôn ở chế độ riêng tư.

## 6. Kiểm tra service và GPU

```bash
curl -fsS http://127.0.0.1:7860/
curl -fsS http://127.0.0.1:7860/api/health
ollama list
ollama ps
nvidia-smi
```

Kết quả health mong đợi chứa `"chat_model"` bằng giá trị `OLLAMA_CHAT_MODEL`,
`"embedding_model":"bge-m3"` và `"status":"ok"`. Model sai hoặc health degraded làm quá trình khởi
động dừng.

`/api/health` không chạy inference: endpoint gọi `/api/tags` của Ollama và báo rõ model nào thiếu.
Sau khi Chat hoặc Quiz gọi model ít nhất một lần, xem cột `PROCESSOR` của `ollama ps`. Chỉ coi là
đang dùng GPU khi Ollama báo GPU (hoặc tỷ lệ GPU) và `nvidia-smi` cho thấy tiến trình hoặc bộ nhớ
GPU. Nếu model lớn hơn VRAM, Ollama có thể offload một phần hoặc toàn bộ sang CPU.

### Logs

```bash
tail -n 100 /kaggle/working/ai-tutor-logs/ollama.log
tail -n 100 /kaggle/working/ai-tutor-logs/backend.log
tail -n 100 /kaggle/working/ai-tutor-logs/frontend.log
tail -n 100 /kaggle/working/ai-tutor-logs/nginx.log
```

Khi một service không sẵn sàng trước timeout, `start_kaggle.sh` in log liên quan và thoát với mã
khác 0. PID nằm trong `/kaggle/working/ai-tutor-runtime`; URL và PID của cloudflared cũng được ghi
ở đó.

## 7. Tuỳ chọn: durable mode (Supabase)

Cấu hình này **không** được dùng trong notebook đang chạy. Để bật nó, bỏ `AI_TUTOR_ALLOW_SQLITE`
khỏi cell cấu hình và thêm các secret bên dưới. Notebook cũ `kaggle_run.ipynb` không đặt
`AI_TUTOR_ALLOW_SQLITE`, nên chỉ chạy được ở chế độ này.

Khi có `DATABASE_URL`, mọi dữ liệu cần giữ lại đều nằm ngoài Kaggle, nên reset runtime không làm
mất gì:

| Dữ liệu | Nơi lưu |
| --- | --- |
| Tài khoản, hội thoại, metadata tài liệu, subjects, summary, flashcard, quiz và kết quả, mastery/progress, planner, Google Calendar | Supabase PostgreSQL |
| Chunk tài liệu, embedding `bge-m3` (1024 chiều), metadata, `document_id`, `owner_id` | Supabase PostgreSQL + pgvector (bảng `document_chunks`) |
| File gốc PDF/TXT đã upload | Supabase Storage, bucket **private** `ai-tutor-documents` (đường dẫn `<user_id>/<sha256>.pdf`) |

Ổ đĩa Kaggle chỉ là cache: file gốc được tải lại từ Storage khi cần (ví dụ khi mở trình xem tài
liệu). Chat RAG đọc vector đã lưu, không embed lại.

### Kaggle Secrets cho durable mode

| Secret | Giá trị |
| --- | --- |
| `DATABASE_URL` | `postgresql://USER:PASSWORD@HOST:PORT/postgres?sslmode=require`. Dùng **pooler** của Supabase vì Kaggle không có IPv6. Mật khẩu có ký tự đặc biệt phải percent-encode (`@` → `%40`) |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service role / secret key của project. Chỉ dùng phía server, không bao giờ gửi xuống trình duyệt |

`start_kaggle.sh` tự đọc ba secret này (hoặc biến môi trường cùng tên), rồi chạy
`python -m backend.durable_setup` **trước khi** tải model. Script dừng với lỗi rõ ràng nếu thiếu
secret, không kết nối được, thiếu pgvector hoặc bucket đang public. Nó không bao giờ âm thầm quay về
SQLite/Chroma/ổ đĩa tạm. Đặt `AI_TUTOR_DOCUMENT_STORAGE=local` nếu muốn giữ file gốc trên đĩa cục
bộ thay vì Supabase Storage. Log chỉ in trạng thái, ví dụ:

```
[db] backend=postgres connected
[vectors] backend=pgvector schema=extensions model=bge-m3 dimensions=1024
[storage] backend=supabase bucket=ai-tutor-documents private=true
[security] row level security enabled on 35 app tables; revoked anon, authenticated
```

Bảng chỉ được tạo hoặc bổ sung (`CREATE TABLE IF NOT EXISTS`, thêm cột còn thiếu), không bao giờ bị
xóa hay truncate. Dữ liệu SQLite mode cũ (`data/conversations.db`, Chroma cũ) **không** được tự động
chuyển sang.

### pgvector

Khi khởi động, script tự chạy `CREATE EXTENSION IF NOT EXISTS vector` (vào schema `extensions` nếu
có). Nếu role không đủ quyền, chạy một lần trong Supabase SQL Editor:

```sql
create extension if not exists vector with schema extensions;
```

### Bảo mật Supabase (Data API)

Supabase mở mọi bảng trong schema `public` qua Data API cho role `anon`/`authenticated`. Khi khởi
động, app tự bật Row Level Security (không có policy) và thu hồi quyền của `anon`/`authenticated`
trên đúng các bảng của app. Backend kết nối bằng role sở hữu bảng nên không bị ảnh hưởng. Có thể
kiểm tra hoặc chạy tay trong SQL Editor (thay `<table>` cho từng bảng của app):

```sql
select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity;   -- phải rỗng
alter table public.<table> enable row level security;
revoke all on table public.<table> from anon, authenticated;
```

Không đưa `SUPABASE_SERVICE_ROLE_KEY` vào frontend; bucket phải giữ chế độ private.

### Nghiệm thu sau khi reset Kaggle

1. Run All trên commit mới; log phải có bốn dòng `[db]`, `[vectors]`, `[storage]`, `[security]` ở trên.
2. Đăng ký một tài khoản test, upload một PDF, hỏi một câu RAG về PDF đó (câu trả lời có trích dẫn trang).
3. Tạo summary, flashcard, một quiz; làm và nộp quiz; tạo một study plan.
4. Ghi lại kết quả của
   `!cd /kaggle/working/AI_Tutor2 && python -m deployment.db_status --email <email-test>`. Lệnh này
   in `user_id`, số dòng mỗi bảng và từng tài liệu (`chunks`, `vectors`, `original_stored=yes`).
5. Factory reset session Kaggle, Run All lại.
6. Đăng nhập lại cùng tài khoản; chạy lại lệnh ở bước 4: `user_id`, `document_id`, `vectors` và số
   dòng phải giống hệt.
7. Mở tài liệu trong trình xem (file tải từ Storage, không cần upload lại). Hỏi lại câu RAG (trả lời
   từ vector đã lưu, không embed lại). Kiểm tra hội thoại, summary, flashcard, kết quả quiz và
   planner vẫn còn.
8. Đăng nhập tài khoản thứ hai: không thấy tài liệu hay dữ liệu nào của tài khoản test.

## 8. Notebook cũ: `kaggle_run.ipynb` (ngrok)

Notebook này không còn được cập nhật từ 2026-09-27. Nó công khai nginx :7860 qua ngrok (cần secret
`NGROK_AUTHTOKEN`) và dùng cùng cơ chế snapshot Kaggle Dataset. Vì không đặt
`AI_TUTOR_ALLOW_SQLITE`, với `start_kaggle.sh` hiện tại nó cần các secret durable mode (mục 7). Nếu
thiếu, khởi động sẽ dừng với lỗi `DATABASE_URL is not set`. Khi bắt đầu mới, nên dùng notebook
Cloudflare.

## 9. Lỗi thường gặp

- **Notebook dừng ở cell Google Calendar ("Missing Kaggle Secret"):** attach đủ 5 secret Google ở mục 2.
- **`DATABASE_URL is not set`:** cell cấu hình thiếu `AI_TUTOR_ALLOW_SQLITE: 1` (SQLite mode), hoặc chưa attach secret durable mode.
- **URL Worker chưa "verified":** KV có thể đang lan truyền (tối đa khoảng 60 giây). Kiểm tra ba secret `CLOUDFLARE_*`. Trong lúc chờ có thể dùng URL trycloudflare tạm in bên dưới.
- **Google OAuth quay về sai trang:** notebook không tự cập nhật `GOOGLE_CALENDAR_REDIRECT_URI` và `AI_TUTOR_FRONTEND_URL`; kiểm tra hai secret này trỏ tới URL đang dùng (URL Worker cố định), không phải một URL trycloudflare cũ.
- **Health báo missing model:** kiểm tra `OLLAMA_CHAT_MODEL` (và `GGUF_MODEL_PATH` nếu dùng GGUF), rồi xem `ollama.log` và `ollama-pull-*.log`. Tên trong `ollama list` phải khớp cấu hình.
- **502 Bad Gateway:** xem log backend và frontend, kiểm tra các PID. Chạy lại với `FORCE_RESTART = True` để khởi động lại sạch.
- **Upload quá lâu:** nginx cho phép 100 MB và timeout 600 giây; kiểm tra dung lượng file, Ollama và embedding model.
- **Retrieval sai sau khi đổi embedding:** tạo lại Chroma với cùng embedding model dùng khi truy vấn (mục 4).
- **Model chạy CPU:** kiểm tra `ollama ps`, VRAM và `nvidia-smi`; chọn quantization vừa với VRAM.
- **`git pull --ff-only` thất bại:** thư mục làm việc trên Kaggle có thay đổi source cục bộ. Xóa thư mục project hoặc xử lý thay đổi đó trước khi chạy lại.
- **Cell Restore/Save báo "State persistence disabled":** thiếu secret `KAGGLE_USERNAME`/`KAGGLE_KEY`, hoặc `STATE_DATASET_SLUG`/`CACHE_DATASET_SLUG` để trống.
- **Save báo lỗi 403/404 từ Kaggle API:** slug dataset không thuộc tài khoản đang đăng nhập, hoặc token đã hết hạn. Tạo lại token tại kaggle.com/settings và cập nhật secret `KAGGLE_KEY`.
