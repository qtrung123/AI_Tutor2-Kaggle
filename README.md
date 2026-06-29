# Tutoring - Local RAG Study Tutor

Tutoring is a local AI tutoring web app for learning from uploaded course
materials. The app lets a student upload PDF/TXT documents, index them into a
Chroma vector database, ask a grounded chatbot questions about those materials,
and generate multiple-choice quizzes from a selected document.

The current project runs locally with:

- FastAPI backend
- Vanilla HTML/CSS/JavaScript frontend
- Ollama chat and embedding models
- Chroma vectorstore for document retrieval
- LangChain integrations for loaders, embeddings, retrieval, and model calls

## Current Features

- Upload PDF/TXT course materials from the AI Tutor page.
- Index uploaded files into local Chroma vectorstore.
- Skip re-indexing files that already exist with the same hash.
- Delete indexed documents from the UI.
- Remove deleted document chunks from Chroma, metadata from `indexed_files.json`,
  and the uploaded file from `data/`.
- Ask the AI Tutor questions using RAG.
- Show retrieved sources/citations for chatbot answers.
- Generate grounded multiple-choice quizzes from a selected indexed document.
- Choose quiz question count: 3, 5, or 10.
- Choose quiz difficulty: easy, medium, or hard.
- Validate and normalize quiz JSON returned by the local model.
- Use a fallback quiz if the local model returns malformed JSON.
- Cache generated quizzes in memory for the same document/count/level.

## Project Structure

```text
tutoring/
  backend/
    main.py              FastAPI routes and API response models
    rag_service.py       Chatbot RAG flow: retrieve, build prompt, call Ollama
    quiz_service.py      Quiz RAG flow and quiz validation/normalization
    ingest.py            Upload indexing and document deletion logic
    __init__.py

  frontend/
    index.html           App layout
    styles.css           App styling
    app.js               Frontend state, API calls, chat, upload, delete, quiz UI
    server.js            Small static file server for the frontend
    package.json

  prompts/
    rag_prompt.txt       Prompt template for chatbot answers
    quiz_prompt.txt      Prompt template for quiz generation

  data/                  Uploaded/source course files
  vectorstore/           Chroma database files
  indexed_files.json     Metadata for indexed documents
  config.py              Shared config: paths, models, chunk settings
  requirements.txt       Python dependencies
```

## Main Flow

### Upload And Index Material

```text
AI Tutor page
-> Upload material button
-> frontend/app.js sends FormData to POST /api/sources/upload
-> backend/main.py receives files
-> backend/ingest.py saves files into data/
-> backend/ingest.py chunks documents and creates embeddings
-> Chroma stores vectors in vectorstore/
-> indexed_files.json stores file metadata
-> frontend refreshes sources and quiz document dropdown
```

### Ask Chatbot

```text
User question
-> POST /api/chat
-> backend/main.py
-> backend/rag_service.py answer_question()
-> ask_rag()
-> retrieve top-k chunks from Chroma
-> insert chunks into prompts/rag_prompt.txt
-> send final prompt to Ollama
-> return answer + citations
-> frontend renders response and retrieved sources
```

The core chatbot RAG function is `ask_rag()` in `backend/rag_service.py`.

### Generate Quiz

```text
Select document/count/level
-> POST /api/quiz/generate
-> backend/main.py
-> backend/quiz_service.py generate_quiz()
-> retrieve chunks for selected document from Chroma
-> insert chunks into prompts/quiz_prompt.txt
-> ask Ollama for strict JSON
-> parse and validate quiz JSON
-> return quiz questions to frontend
```

Quiz generation is also RAG, but the output is structured quiz JSON instead of
a natural-language chatbot answer.

### Delete Document

```text
Delete button in source list
-> DELETE /api/sources/{document_id}
-> backend/main.py
-> backend/ingest.py delete_indexed_file()
-> delete Chroma vector ids for that document
-> remove indexed_files.json entry
-> delete source file from data/
-> clear quiz cache for that document
-> frontend refreshes sources and quiz document dropdown
```

## Configuration

Main settings live in `config.py`.

```python
CHAT_MODEL = "gemma2:2b"
EMBEDDING_MODEL = "bge-m3"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
TOP_K = 4
```

The same embedding model must be used for indexing and retrieval. If you change
`EMBEDDING_MODEL`, re-index your documents.

## Requirements

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install frontend dependencies:

```bash
cd frontend
npm install
```

Install/pull Ollama models:

```bash
ollama pull gemma2:2b
ollama pull bge-m3
```

## Run The Project

Start Ollama first:

```bash
ollama serve
```

Start backend from the project root:

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

If using Git Bash on Windows and the virtual environment is not activated:

```bash
./.venv/Scripts/python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

If using PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Start frontend in another terminal:

```bash
cd frontend
npm start
```

Open:

```text
http://localhost:3000
```

Backend runs at:

```text
http://127.0.0.1:8000
```

FastAPI docs are available at:

```text
http://127.0.0.1:8000/docs
```

## Optional CLI Indexing

If files already exist in `data/` and you want to index them without using the
frontend upload button:

```bash
python -m backend.ingest
```

## API List

### `GET /`

Health-style root endpoint.

Response:

```json
{
  "message": "Tutoring backend is running"
}
```

### `GET /api/model/health`

Returns backend/model status.

Response:

```json
{
  "ok": true,
  "model": "gemma2:2b",
  "status": "Backend is running"
}
```

### `GET /api/sources`

Returns indexed/uploaded source summaries for the AI Tutor source panel.

Response:

```json
[
  {
    "sourceId": 1,
    "title": "Lecture 4 - Socket Programming - TCP.pdf",
    "chunks": 32,
    "path": "C:\\Users\\ADMIN\\tutoring\\data\\Lecture 4 - Socket Programming - TCP.pdf"
  }
]
```

### `POST /api/sources/upload`

Uploads and indexes PDF/TXT files.

Content type:

```text
multipart/form-data
```

Form field:

```text
files: one or more .pdf/.txt files
```

Response:

```json
{
  "new_files": 1,
  "new_chunks": 32,
  "skipped_files": [],
  "total_indexed_files": 7,
  "sources": [
    {
      "sourceId": 1,
      "title": "Lecture 4 - Socket Programming - TCP.pdf",
      "chunks": 32,
      "path": "C:\\Users\\ADMIN\\tutoring\\data\\Lecture 4 - Socket Programming - TCP.pdf"
    }
  ]
}
```

### `DELETE /api/sources/{document_id}`

Deletes one indexed document.

Example:

```text
DELETE /api/sources/Lecture%204%20-%20Socket%20Programming%20-%20TCP.pdf
```

Response:

```json
{
  "deleted": "Lecture 4 - Socket Programming - TCP.pdf",
  "deleted_chunks": 32,
  "total_indexed_files": 6,
  "sources": []
}
```

### `GET /api/documents`

Returns indexed documents for the quiz document dropdown.

Response:

```json
[
  {
    "id": "Lecture 4 - Socket Programming - TCP.pdf",
    "title": "Lecture 4 - Socket Programming - TCP.pdf",
    "chunks": 32
  }
]
```

### `POST /api/chat`

Answers a user question using RAG.

Request:

```json
{
  "message": "What is TCP socket programming?",
  "course": "Net-centric Computing",
  "topic": "Course materials"
}
```

Response:

```json
{
  "answer": "TCP socket programming is ...",
  "model": "gemma2:2b",
  "citations": [
    {
      "sourceId": 1,
      "title": "Lecture 4 - Socket Programming - TCP.pdf",
      "page": "18",
      "content": "Retrieved chunk text..."
    }
  ]
}
```

### `POST /api/quiz/generate`

Generates a grounded multiple-choice quiz from one indexed document.

Request:

```json
{
  "document_id": "Lecture 4 - Socket Programming - TCP.pdf",
  "num_questions": 5,
  "level": "medium"
}
```

Allowed values:

- `num_questions`: `3`, `5`, `10`
- `level`: `easy`, `medium`, `hard`

Response:

```json
{
  "document_id": "Lecture 4 - Socket Programming - TCP.pdf",
  "level": "medium",
  "questions": [
    {
      "id": 1,
      "question": "Which statement best describes TCP?",
      "options": [
        "A. TCP provides reliable communication.",
        "B. TCP never uses ports.",
        "C. TCP is only used for file compression.",
        "D. TCP cannot be used in socket programming."
      ],
      "correct_answer": "A",
      "explanation": "Option A is grounded in the retrieved lecture chunk.",
      "source": {
        "title": "Lecture 4 - Socket Programming - TCP.pdf",
        "page": "18"
      }
    }
  ]
}
```

## Notes And Limitations

- The app is designed for local development and local Ollama models.
- Uploaded files are stored in `data/`.
- Vector data is stored locally in `vectorstore/`.
- `indexed_files.json` is the app's source-of-truth metadata for indexed files.
- Quiz cache is in memory only and resets when the backend restarts.
- If an uploaded file has the same name as an existing file, the file in `data/`
  can be overwritten. If the content hash is unchanged, indexing is skipped.
- If the embedding model changes, existing vector data should be rebuilt.
