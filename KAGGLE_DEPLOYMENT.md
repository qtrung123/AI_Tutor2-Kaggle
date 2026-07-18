# Triển khai AI Tutor trên Kaggle

## Kiến trúc

```text
Ngrok public URL
        |
        v
Nginx 127.0.0.1:7860
   |-- /api/* --> FastAPI 127.0.0.1:8000
   `-- /*     --> Node.js 127.0.0.1:3000
                         |
FastAPI --> Ollama 127.0.0.1:11434 --> Qwen GGUF + bge-m3
        --> Chroma + SQLite trong /kaggle/working/AI_Tutor2
```

Trình duyệt chỉ truy cập cổng 7860 qua tunnel. Frontend dùng URL `/api/...` tương đối, nên API và giao diện có cùng origin. Nginx giữ nguyên prefix `/api` khi chuyển request đến FastAPI.

Khi chạy local bằng `npm start`, frontend server phát cấu hình mặc định `http://127.0.0.1:8000` để giữ cách chạy hai port hiện tại. Có thể override bằng biến `API_BASE_URL` của Node hoặc `localStorage.setItem("API_BASE_URL", "...")`. Script Kaggle đặt giá trị rỗng để dùng same-origin.

## Chuẩn bị model GGUF

1. Tạo Kaggle Dataset hoặc Kaggle Model riêng tư.
2. Upload file Qwen `.gguf` vào đó. Không đưa GGUF vào Git repository.
3. Trong notebook, chọn **Add Input** và attach dataset/model.
4. Mở cây `/kaggle/input` để lấy đường dẫn chính xác, rồi đặt `GGUF_MODEL_PATH` trong cell cấu hình, ví dụ `/kaggle/input/qwen-tutor/qwen2.5-3b-q4_k_m.gguf`.

Script tạo một `Modelfile` runtime trỏ trực tiếp đến input (kể cả đường dẫn có dấu cách), rồi chỉ gọi `ollama create` khi `OLLAMA_CHAT_MODEL` chưa tồn tại. Đặt `RECREATE_OLLAMA_MODEL = True` nếu chủ động muốn tạo lại.

## Chạy notebook

1. Import hoặc mở [kaggle_run.ipynb](kaggle_run.ipynb), chọn **Copy & Edit**.
2. Trong **Settings**, chọn GPU accelerator và bật Internet.
3. Attach input chứa GGUF. Có thể attach thêm input bài giảng.
4. Trong **Add-ons > Secrets**, tạo secret `NGROK_AUTHTOKEN` bằng token từ tài khoản Ngrok. Không dán token vào cell hoặc source code.
5. Sửa duy nhất cell **User configuration**. Nếu có bài giảng, đặt `LECTURE_INPUT_DIR` tới thư mục input chứa PDF/TXT.
6. Chọn **Run All**. Mở URL được in theo dạng `AI Tutor URL: https://...`.

Notebook clone repository nếu chưa có; các lần chạy sau dùng `fetch`, `checkout`, `pull --ff-only`. Dependency frontend dùng `npm ci`. Script lưu PID và dừng process của lần chạy trước trước khi khởi động lại.

Các PDF/TXT được copy từ Kaggle Input vào `data/` (vùng ghi được) và được index tăng dần khi script chạy. File đã có cùng hash sẽ được bỏ qua. Người dùng cũng có thể upload và index tài liệu từ trang Materials.

## Cấu hình runtime

Local Windows tiếp tục dùng các mặc định trong `config.py`. Kaggle đặt các biến sau:

| Biến | Giá trị Kaggle mặc định |
| --- | --- |
| `OLLAMA_CHAT_MODEL` | `qwen-tutor` |
| `OLLAMA_EMBEDDING_MODEL` | `bge-m3` |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` |
| `AI_TUTOR_DATA_DIR` | `<PROJECT_ROOT>/data` |
| `AI_TUTOR_VECTORSTORE_DIR` | `<PROJECT_ROOT>/vectorstore` |
| `AI_TUTOR_DATABASE_PATH` | `<PROJECT_ROOT>/data/conversations.db` |

Local và Kaggle phải sử dụng cùng embedding model `bge-m3`. Không được tái sử dụng ChromaDB được tạo bằng embedding model khác.

Kaggle ghi model vào marker `<AI_TUTOR_VECTORSTORE_DIR>/.embedding_model`. Nếu marker khác `bge-m3`, hoặc database có dữ liệu nhưng thiếu marker, script sẽ đổi tên ChromaDB cũ thành backup có timestamp, backup `indexed_files.json`, tạo database mới và index lại tài liệu. Cơ chế này từ chối chạy ngoài `/kaggle/working`, vì vậy không xóa hoặc đổi tên ChromaDB local trên Windows hay dữ liệu read-only trong `/kaggle/input`. Có thể đặt `REBUILD_CHROMA_ON_EMBEDDING_CHANGE=0` để dừng với lỗi thay vì tự backup/rebuild.

## Kiểm tra service và GPU

```bash
curl -fsS http://127.0.0.1:7860/
curl -fsS http://127.0.0.1:7860/api/health
ollama list
ollama ps
nvidia-smi
```

Health mong đợi chứa `"chat_model":"qwen-tutor"`, `"embedding_model":"bge-m3"` và `"status":"ok"`. Script kiểm tra cả endpoint backend cổng 8000 và endpoint qua Nginx cổng 7860; model sai hoặc health degraded làm quá trình khởi động dừng.

`/api/health` không chạy inference; endpoint gọi `/api/tags` của Ollama và báo rõ model thiếu. Sau khi Chat hoặc Quiz gọi model ít nhất một lần, xem cột `PROCESSOR` của `ollama ps`. Chỉ coi là dùng GPU khi Ollama báo GPU (hoặc tỷ lệ GPU) và `nvidia-smi` cho thấy tiến trình/bộ nhớ GPU. Nếu model lớn hơn VRAM, Ollama có thể offload một phần hoặc toàn bộ sang CPU.

## Logs

```bash
tail -n 100 /kaggle/working/ai-tutor-logs/ollama.log
tail -n 100 /kaggle/working/ai-tutor-logs/backend.log
tail -n 100 /kaggle/working/ai-tutor-logs/frontend.log
tail -n 100 /kaggle/working/ai-tutor-logs/nginx.log
```

Khi một service không sẵn sàng trước timeout, `start_kaggle.sh` in log liên quan và thoát khác 0. PID nằm trong `/kaggle/working/ai-tutor-runtime`.

## Dữ liệu và tính lâu bền

`/kaggle/input` là read-only. Tài liệu upload, `data/conversations.db`, `vectorstore/` và `indexed_files.json` được ghi dưới `/kaggle/working/AI_Tutor2`. Chúng mất khi session bị xóa nếu chưa lưu version/output.

Để giữ dữ liệu, trước khi kết thúc session hãy tạo archive trong `/kaggle/working`:

```bash
tar -czf /kaggle/working/ai-tutor-state.tar.gz \
  -C /kaggle/working/AI_Tutor2 data vectorstore indexed_files.json
```

Chọn **Save Version** để archive trở thành Notebook Output. Ở session sau, attach output đó và giải nén vào project trước cell khởi động. Không publish output nếu tài liệu hoặc hội thoại là dữ liệu riêng tư.

## Lỗi thường gặp

- **Health báo missing model:** kiểm tra `GGUF_MODEL_PATH`, `OLLAMA_CHAT_MODEL`, rồi xem `ollama.log`. Tên trong `ollama list` phải khớp cấu hình.
- **Không có public URL:** bật Internet, thêm đúng secret `NGROK_AUTHTOKEN`, và chạy lại cell tunnel. Chỉ tunnel cổng 7860.
- **502 Bad Gateway:** xem backend/frontend logs và kiểm tra các PID. Chạy lại cell khởi động để cleanup sạch.
- **Upload quá lâu:** Nginx cho phép 100 MB và timeout 600 giây; kiểm tra dung lượng file, Ollama và embedding model.
- **Retrieval sai sau khi đổi embedding:** tạo lại Chroma với cùng embedding model dùng khi truy vấn.
- **Model chạy CPU:** kiểm tra `ollama ps`, VRAM và `nvidia-smi`; chọn GGUF quantization vừa VRAM.
- **`git pull --ff-only` thất bại:** thư mục Kaggle working có thay đổi source cục bộ. Xóa project working hoặc xử lý thay đổi trước khi chạy lại.
