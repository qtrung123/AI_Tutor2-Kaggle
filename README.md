# TutorFlow — Local RAG Study Tutor

TutorFlow là ứng dụng học tập chạy local, cho phép người dùng upload tài liệu môn học, chat với tài liệu bằng RAG và tạo quiz trắc nghiệm để tự luyện tập.

Toàn bộ tài liệu, vector, lịch sử chat, quiz và kết quả làm bài được lưu trên máy. Chatbot và quiz sử dụng model chạy qua Ollama, không yêu cầu dịch vụ AI cloud.

## Tính năng hiện tại

### Quản lý tài liệu

- Upload nhiều file PDF hoặc TXT tại trang **Materials**.
- Chia tài liệu thành chunk và tạo embedding bằng Ollama.
- Lưu vector vào Chroma.
- Bỏ qua file đã được index nếu hash nội dung không thay đổi.
- Hiển thị số chunk của từng tài liệu.
- Xóa tài liệu khỏi `data/`, Chroma và `indexed_files.json`.
- Khi xóa tài liệu, hệ thống cũng xóa quiz liên quan và gỡ tài liệu khỏi các conversation.

### Chatbot RAG

- Mỗi cuộc chat là một conversation/thread độc lập.
- Mỗi conversation lưu danh sách tài liệu được phép sử dụng.
- Upload tài liệu mới không tự động thay đổi nguồn của conversation cũ.
- Cho phép mở lại và tiếp tục conversation sau khi reload.
- Lưu user message, assistant message, grounding status và citation bằng SQLite.
- Hỗ trợ câu hỏi nối tiếp bằng lịch sử gần nhất của conversation.
- Retrieval chỉ tìm trong tài liệu đã chọn cho conversation.
- Hiển thị trạng thái `Grounded` và citation theo tài liệu/trang.
- Nếu không đủ dữ liệu, chatbot được yêu cầu trả lời rằng không biết dựa trên tài liệu hiện có.

### Quiz

- Tạo quiz từ một tài liệu đã index.
- Chọn số câu hỏi từ giao diện: 5, 10, 15, 20, 30 hoặc 40.
- Backend hỗ trợ từ 1 đến 40 câu hỏi.
- Hỗ trợ đúng ba mức độ:
  - `easy`
  - `medium`
  - `difficult`
- Mỗi cặp tài liệu và độ khó được lưu thành một quiz riêng.
- Quiz key có dạng `document_id::difficulty`.
- Sử dụng toàn bộ chunk của tài liệu để tạo quiz; mỗi batch tối đa 8 câu.
- Parse, chuẩn hóa và kiểm tra JSON trả về từ model.
- Thử sửa/generate lại tối đa 3 lần cho mỗi batch khi JSON hoặc chất lượng không hợp lệ.
- Hiện đúng/sai ngay khi người dùng chọn đáp án.
- Tự động lưu tiến độ sau từng câu, không cần nút Submit.
- Tự hoàn thành attempt khi tất cả câu đã được trả lời.
- Lưu lịch sử điểm, đáp án và kết quả từng câu.
- Có thể xem lại các attempt đã hoàn thành.
- Nút **Explain** gọi RAG khi cần và chỉ giải thích vì sao đáp án đúng là đúng.
- Explanation được cache để không phải gọi model lại cho cùng một câu hỏi.

## Công nghệ sử dụng

- **Backend:** FastAPI, Python
- **Frontend:** HTML, CSS, Vanilla JavaScript
- **LLM runtime:** Ollama
- **Chat model:** `hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M`
- **Embedding model:** `bge-m3`
- **Vector database:** Chroma
- **RAG integration:** LangChain
- **Chat history:** SQLite
- **Quiz storage:** JSON local files

## Kiến trúc tổng quát

```text
Browser (localhost:3000)
        |
        | HTTP/JSON
        v
FastAPI (127.0.0.1:8000)
        |
        +-- Materials/ingest
        |     +-- data/
        |     +-- indexed_files.json
        |     +-- Chroma vectorstore/
        |
        +-- Chat RAG
        |     +-- Chroma retrieval theo conversation sources
        |     +-- prompts/rag_prompt.txt
        |     +-- Ollama chat model
        |     +-- data/conversations.db
        |
        +-- Quiz RAG
              +-- toàn bộ chunk của document
              +-- prompts/quiz_prompt.txt
              +-- Ollama chat model
              +-- generated_quizzes.json
              +-- quiz_attempts.json
              +-- quiz_explanations.json
```

## Cấu trúc project

```text
python-ollama-rag/
├── backend/
│   ├── main.py                 # FastAPI models và routes
│   ├── ingest.py               # Đọc, chunk, embedding, index và xóa tài liệu
│   ├── rag_service.py          # Chat RAG và quiz explanation RAG
│   ├── conversation_store.py   # SQLite conversation/message/source/citation
│   ├── quiz_service.py         # Generate, validate, chấm điểm và quiz history
│   ├── quiz_store.py           # Persistent JSON storage cho quiz/attempt/explain
│   └── __init__.py
├── frontend/
│   ├── index.html              # Giao diện ứng dụng
│   ├── styles.css              # Styling và responsive layout
│   ├── app.js                  # Frontend state và API calls
│   ├── server.js               # Static server port 3000
│   └── package.json
├── prompts/
│   ├── rag_prompt.txt          # System constraints cho chatbot
│   └── quiz_prompt.txt         # Prompt generate quiz JSON
├── data/                       # File upload và dữ liệu persistent
├── vectorstore/                # Chroma database
├── indexed_files.json          # Metadata tài liệu đã index
├── config.py                   # Paths, models và chunk settings
├── requirements.txt
└── README.md
```

## Dữ liệu được lưu ở đâu?

| Dữ liệu | Nơi lưu |
| --- | --- |
| PDF/TXT đã upload | `data/` |
| Vector và metadata chunk | `vectorstore/` |
| Danh sách file đã index | `indexed_files.json` |
| Conversation, message, source, citation | `data/conversations.db` |
| Quiz đã generate | `data/generated_quizzes.json` |
| Tiến độ và lịch sử làm quiz | `data/quiz_attempts.json` |
| Explanation đã generate | `data/quiz_explanations.json` |

## Yêu cầu hệ thống

- Python 3.10 trở lên.
- Node.js và npm.
- Ollama đang chạy trên máy.
- Các model trong `config.py` đã được tải về.

## Cài đặt

### 1. Tạo virtual environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Cài Python dependencies

```powershell
pip install -r requirements.txt
```

### 3. Cài frontend dependencies

```powershell
cd frontend
npm install
cd ..
```

### 4. Tải Ollama models

```powershell
ollama pull bge-m3
ollama pull "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M"
```

Tên model phải khớp với `CHAT_MODEL` và `EMBEDDING_MODEL` trong `config.py`.

## Chạy ứng dụng

### 1. Khởi động Ollama

Nếu Ollama chưa chạy dưới dạng background service:

```powershell
ollama serve
```

### 2. Chạy backend

Tại thư mục gốc của project:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Backend:

```text
http://127.0.0.1:8000
```

Swagger API documentation:

```text
http://127.0.0.1:8000/docs
```

### 3. Chạy frontend

Mở terminal khác:

```powershell
cd frontend
npm start
```

Mở trình duyệt tại:

```text
http://localhost:3000
```

## Cách sử dụng

### Upload tài liệu

1. Mở trang **Materials**.
2. Chọn **Upload materials**.
3. Chọn một hoặc nhiều file PDF/TXT.
4. Chờ trạng thái indexing hoàn tất.
5. Kiểm tra tài liệu và số chunk trong danh sách.

### Chat với tài liệu

1. Mở trang **AI Tutor**.
2. Tạo conversation mới hoặc chọn conversation cũ.
3. Bấm **Sources (n)** trên header chat.
4. Chọn tài liệu được phép sử dụng và bấm **Apply sources**.
5. Nhập câu hỏi vào ô `Ask`.
6. Xem câu trả lời, grounding status và citation.

Conversation mới hiện chọn mặc định tất cả tài liệu đã có tại thời điểm tạo. Việc upload tài liệu sau đó không làm thay đổi source của conversation này.

### Tạo và làm quiz

1. Mở trang **Practice**.
2. Chọn document.
3. Chọn số câu hỏi.
4. Chọn `easy`, `medium` hoặc `difficult`.
5. Bấm **Generate Quiz**.
6. Chọn đáp án để xem đúng/sai ngay lập tức.
7. Bấm **Explain** khi cần giải thích đáp án đúng.
8. Khi trả lời đủ câu, kết quả tự động được thêm vào **Quiz History**.

## Flow Chatbot hiện tại

```text
User gửi câu hỏi
    -> POST /api/conversations/{conversation_id}/messages
    -> answer_conversation_message()
    -> tải conversation + message history + document_ids từ SQLite
    -> lưu user message
    -> ghép các câu hỏi gần nhất thành retrieval query
    -> tìm TOP_K chunk trong đúng conversation sources
    -> ghép system prompt + history + context + question
    -> gọi ChatOllama
    -> lưu assistant message + grounding status + citations
    -> trả kết quả cho frontend
```

`conversation_history` chỉ dùng để hiểu câu hỏi nối tiếp. Nó không được xem là bằng chứng. Mọi nội dung trả lời phải dựa trên context lấy từ tài liệu.

Nếu conversation không có source hoặc không tìm thấy chunk phù hợp, backend trả trạng thái `insufficient_context`.

Endpoint `POST /api/chat` cũ vẫn được giữ để tương thích, nhưng UI hiện tại sử dụng conversation endpoint.

## Flow tạo Quiz hiện tại

```text
Chọn document + question_count + difficulty
    -> POST /api/quiz/generate
    -> generate_quiz()
    -> kiểm tra saved quiz bằng key document_id::difficulty
    -> nếu HIT: trả quiz đã lưu
    -> nếu MISS: lấy toàn bộ chunk của document từ Chroma
    -> chia số câu thành các batch, tối đa 8 câu/batch
    -> build prompt theo difficulty
    -> gọi Ollama
    -> extract/parse/normalize/validate JSON
    -> retry tối đa 3 lần cho batch lỗi
    -> lưu quiz vào generated_quizzes.json
    -> trả quiz cho frontend
```

Lưu ý: nếu quiz của cùng `document_id::difficulty` đã tồn tại, API generate trả lại quiz đó. Muốn thay thế bằng bộ câu hỏi mới hoặc đổi số lượng câu, sử dụng chức năng **Regenerate Quiz**.

## API chính

### System và Materials

| Method | Endpoint | Mục đích |
| --- | --- | --- |
| `GET` | `/` | Kiểm tra backend |
| `GET` | `/api/model/health` | Xem model đang cấu hình |
| `GET` | `/api/sources` | Danh sách tài liệu đã index |
| `POST` | `/api/sources/upload` | Upload và index PDF/TXT |
| `DELETE` | `/api/sources/{document_id}` | Xóa tài liệu và dữ liệu liên quan |
| `GET` | `/api/documents` | Danh sách document cho Practice |

### Conversations

| Method | Endpoint | Mục đích |
| --- | --- | --- |
| `POST` | `/api/conversations` | Tạo conversation |
| `GET` | `/api/conversations` | Liệt kê conversation |
| `GET` | `/api/conversations/{conversation_id}` | Mở conversation và messages |
| `PATCH` | `/api/conversations/{conversation_id}` | Đổi tên conversation |
| `DELETE` | `/api/conversations/{conversation_id}` | Xóa conversation |
| `PUT` | `/api/conversations/{conversation_id}/sources` | Cập nhật source scope |
| `POST` | `/api/conversations/{conversation_id}/messages` | Gửi câu hỏi và nhận RAG answer |
| `POST` | `/api/chat` | Legacy chat endpoint |

### Quiz

| Method | Endpoint | Mục đích |
| --- | --- | --- |
| `GET` | `/api/quizzes` | Trạng thái quiz theo document/difficulty |
| `GET` | `/api/quiz/{document_id}?difficulty=easy` | Mở quiz và tiến độ gần nhất |
| `POST` | `/api/quiz/generate` | Generate hoặc lấy quiz đã lưu |
| `POST` | `/api/quiz/{document_id}/regenerate` | Thay quiz hiện tại bằng quiz mới |
| `PATCH` | `/api/quiz/{document_id}/progress` | Check đáp án và autosave một câu |
| `DELETE` | `/api/quiz/{document_id}/progress?difficulty=easy` | Reset tiến độ hiện tại |
| `POST` | `/api/quiz/{document_id}/submit` | Endpoint submit thủ công còn được hỗ trợ |
| `POST` | `/api/quiz/{document_id}/questions/{question_id}/explain` | Generate/load explanation |
| `GET` | `/api/quiz-history` | Danh sách attempt hoàn thành |
| `GET` | `/api/quiz-history/{attempt_id}` | Chi tiết một attempt |

## Ví dụ API

### Tạo conversation

```http
POST /api/conversations
Content-Type: application/json
```

```json
{
  "title": "New conversation",
  "document_ids": ["Lecture 1.pdf"]
}
```

### Gửi message

```http
POST /api/conversations/{conversation_id}/messages
Content-Type: application/json
```

```json
{
  "message": "TCP hoạt động như thế nào?"
}
```

### Generate quiz

```http
POST /api/quiz/generate
Content-Type: application/json
```

```json
{
  "document_id": "Lecture 1.pdf",
  "question_count": 5,
  "difficulty": "medium"
}
```

### Lưu một đáp án

```http
PATCH /api/quiz/Lecture%201.pdf/progress
Content-Type: application/json
```

```json
{
  "difficulty": "medium",
  "question_id": 1,
  "selected_answer": "A"
}
```

### Explain một câu hỏi

```http
POST /api/quiz/Lecture%201.pdf/questions/1/explain
Content-Type: application/json
```

```json
{
  "difficulty": "medium"
}
```

## Cấu hình

Các cấu hình chính nằm trong `config.py`:

```python
CHAT_MODEL = "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M"
EMBEDDING_MODEL = "bge-m3"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
TOP_K = 4
```

Nếu thay đổi `EMBEDDING_MODEL`, cần index lại tài liệu vì vector cũ được tạo bằng embedding model trước đó.

## Index file bằng CLI

Nếu file đã nằm trong `data/`, có thể chạy ingest trực tiếp:

```powershell
.\.venv\Scripts\python.exe -m backend.ingest
```

## Giới hạn hiện tại

- Chỉ hỗ trợ PDF và TXT.
- Indexing đang chạy đồng bộ; file lớn có thể cần thời gian chờ.
- Trạng thái upload/index hiện hiển thị chung, chưa có state machine riêng cho từng file.
- Retrieval chatbot lấy tối đa `TOP_K = 4` chunk.
- Chat grounding chủ yếu được kiểm soát bằng prompt; chưa có bước hậu kiểm mọi factual claim/citation.
- Conversation history chỉ đưa tối đa 8 message gần nhất vào prompt.
- Quiz sử dụng toàn bộ chunk nhưng giới hạn 900 ký tự cho nội dung mỗi chunk khi build context.
- Việc chống câu hỏi trùng giữa các difficulty đang tạm tắt; các level khác nhau vẫn có thể sinh câu giống hoặc gần giống nhau.
- Local model có thể trả JSON lỗi; backend retry nhưng vẫn có thể thất bại nếu không thu đủ số câu hợp lệ.
- Study Plan hiện mới là UI minh họa, chưa có backend generation.

## Xử lý lỗi thường gặp

### Backend không gọi được Ollama

Kiểm tra:

```powershell
ollama list
```

Đảm bảo tên model trong danh sách khớp `config.py` và Ollama đang chạy.

### Chat trả insufficient context

- Mở drawer **Sources** trong conversation.
- Chọn ít nhất một tài liệu.
- Bấm **Apply sources**.
- Kiểm tra tài liệu đã được index và có chunk.

### Đổi embedding model nhưng retrieval sai

Vector hiện tại được tạo bằng model cũ. Cần xóa/rebuild `vectorstore/` và index lại tài liệu bằng embedding model mới.

### Frontend chưa nhận CSS/JavaScript mới

Reload trình duyệt bằng `Ctrl + F5` và xác nhận frontend đang chạy tại port 3000.
