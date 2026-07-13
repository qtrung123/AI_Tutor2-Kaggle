# TutorFlow — Local RAG Study Tutor

TutorFlow is a local learning application for studying from personal course materials. Users can upload documents, ask grounded questions through a RAG chatbot, and generate multiple-choice quizzes for practice.

Documents, vectors, conversations, quizzes, answers, and learning history are stored locally. Chat and quiz generation use models served by Ollama and do not require a cloud AI service.

## Features

### Course Materials

- Upload multiple PDF or TXT files from the dedicated **Materials** page.
- Split documents into chunks and create embeddings with Ollama.
- Store vectors in Chroma.
- Skip unchanged files by comparing content hashes.
- Display each indexed document and its chunk count.
- Delete documents from `data/`, Chroma, and `indexed_files.json`.
- Remove deleted documents from conversation scopes and delete their quiz data.

### Grounded Chat

- Keep every chat in an independent conversation/thread.
- Assign a separate set of source documents to each conversation.
- Upload new documents without changing existing conversation scopes.
- Reopen old conversations and continue chatting after a page reload.
- Save user messages, assistant messages, grounding status, and citations in SQLite.
- Use recent conversation history to understand follow-up questions.
- Retrieve context only from the documents selected for the active conversation.
- Show grounding status and document/page citations below answers.
- Return an insufficient-context response when the selected materials do not support an answer.

### Quiz Practice

- Generate a multiple-choice quiz from one indexed document.
- Select 5, 10, 15, 20, 30, or 40 questions from the UI.
- Support between 1 and 40 questions at the backend level.
- Support exactly three difficulty levels:
  - `easy`
  - `medium`
  - `difficult`
- Store a separate quiz for each document and difficulty.
- Use `document_id::difficulty` as the persistent quiz key.
- Use all document chunks during generation and generate at most eight questions per batch.
- Extract, normalize, and validate the JSON returned by the model.
- Retry a failed or incomplete batch up to three times.
- Check an answer immediately after the learner selects it.
- Save progress after every answered question without requiring a Submit button.
- Complete and archive an attempt when every question has been answered.
- Save scores, answers, and per-question results.
- Review previously completed attempts in Quiz History.
- Generate explanations only when the learner selects **Explain**.
- Explain only why the correct answer is correct.
- Cache generated explanations for later use.

## Technology Stack

- **Backend:** FastAPI and Python
- **Frontend:** HTML, CSS, and vanilla JavaScript
- **LLM runtime:** Ollama
- **Chat model:** `hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M`
- **Embedding model:** `bge-m3`
- **Vector database:** Chroma
- **RAG integration:** LangChain
- **Chat history:** SQLite
- **Quiz storage:** Local JSON files

## Architecture

```text
Browser (localhost:3000)
        |
        | HTTP/JSON
        v
FastAPI (127.0.0.1:8000)
        |
        +-- Materials and ingestion
        |     +-- data/
        |     +-- indexed_files.json
        |     +-- Chroma vectorstore/
        |
        +-- Chat RAG
        |     +-- Chroma retrieval filtered by conversation sources
        |     +-- prompts/rag_prompt.txt
        |     +-- Ollama chat model
        |     +-- data/conversations.db
        |
        +-- Quiz RAG
              +-- all chunks from the selected document
              +-- prompts/quiz_prompt.txt
              +-- Ollama chat model
              +-- generated_quizzes.json
              +-- quiz_attempts.json
              +-- quiz_explanations.json
```

## Project Structure

```text
python-ollama-rag/
├── backend/
│   ├── main.py                 # FastAPI request models and routes
│   ├── ingest.py               # Document loading, chunking, indexing, and deletion
│   ├── rag_service.py          # Chat RAG and quiz explanation RAG
│   ├── conversation_store.py   # SQLite conversation persistence
│   ├── quiz_service.py         # Quiz generation, validation, scoring, and history
│   ├── quiz_store.py           # Persistent quiz, attempt, and explanation storage
│   └── __init__.py
├── frontend/
│   ├── index.html              # Application layout
│   ├── styles.css              # Styling and responsive layout
│   ├── app.js                  # Frontend state and API integration
│   ├── server.js               # Static development server
│   └── package.json
├── prompts/
│   ├── rag_prompt.txt          # Grounded chat instructions
│   └── quiz_prompt.txt         # Quiz generation instructions
├── data/                       # Uploaded documents and persistent application data
├── vectorstore/                # Chroma database
├── indexed_files.json          # Indexed-document metadata
├── config.py                   # Paths, models, and retrieval settings
├── requirements.txt
└── README.md
```

## Local Data

| Data | Storage |
| --- | --- |
| Uploaded PDF/TXT files | `data/` |
| Document vectors and chunk metadata | `vectorstore/` |
| Indexed-document registry | `indexed_files.json` |
| Conversations, messages, sources, and citations | `data/conversations.db` |
| Generated quizzes | `data/generated_quizzes.json` |
| Current quiz progress and completed attempts | `data/quiz_attempts.json` |
| Generated quiz explanations | `data/quiz_explanations.json` |

## Requirements

- Python 3.10 or newer
- Node.js and npm
- Ollama running locally
- The models configured in `config.py`

## Installation

### 1. Create a virtual environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install Python dependencies

```powershell
pip install -r requirements.txt
```

### 3. Install frontend dependencies

```powershell
cd frontend
npm install
cd ..
```

### 4. Pull the Ollama models

```powershell
ollama pull bge-m3
ollama pull "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M"
```

The installed model names must match `CHAT_MODEL` and `EMBEDDING_MODEL` in `config.py`.

## Running the Application

### 1. Start Ollama

If Ollama is not already running as a background service:

```powershell
ollama serve
```

### 2. Start the backend

Run this command from the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Backend URL:

```text
http://127.0.0.1:8000
```

FastAPI documentation:

```text
http://127.0.0.1:8000/docs
```

### 3. Start the frontend

Open another terminal:

```powershell
cd frontend
npm start
```

Open the application at:

```text
http://localhost:3000
```

## Usage

### Upload Materials

1. Open **Materials**.
2. Select **Upload materials**.
3. Choose one or more PDF/TXT files.
4. Wait for indexing to finish.
5. Confirm that each document appears with a chunk count.

### Chat with Documents

1. Open **AI Tutor**.
2. Create a conversation or select an existing one.
3. Select **Sources (n)** in the chat header.
4. Choose the documents available to that conversation.
5. Select **Apply sources**.
6. Enter a question in the `Ask` field.
7. Review the answer, grounding status, and citations.

A new conversation initially selects all documents available at the time it is created. Uploading another document later does not modify that conversation.

### Generate and Complete a Quiz

1. Open **Practice**.
2. Select an indexed document.
3. Select the question count.
4. Select `easy`, `medium`, or `difficult`.
5. Select **Generate Quiz**.
6. Choose an answer to receive immediate feedback.
7. Select **Explain** when an explanation is needed.
8. Answer every question to complete the attempt and add it to Quiz History.

## Chat Flow

```text
User sends a question
    -> POST /api/conversations/{conversation_id}/messages
    -> answer_conversation_message()
    -> load the conversation, messages, and document IDs from SQLite
    -> save the user message
    -> combine recent user questions into a retrieval query
    -> retrieve TOP_K chunks from the selected conversation sources
    -> combine system prompt, history, context, and current question
    -> call ChatOllama
    -> save the assistant message, grounding status, and citations
    -> return the response to the frontend
```

Conversation history is used only to understand follow-up references. It is not treated as supporting evidence. The answer must be grounded in retrieved document context.

If a conversation has no selected source or retrieval finds no matching document chunks, the backend returns `insufficient_context`.

## Quiz Generation Flow

```text
Select document, question count, and difficulty
    -> POST /api/quiz/generate
    -> generate_quiz()
    -> check persistent storage with document_id::difficulty
    -> cache HIT: return the saved quiz
    -> cache MISS: load all chunks for the selected document
    -> split the requested count into batches of up to eight questions
    -> build a difficulty-specific prompt
    -> call Ollama
    -> extract, parse, normalize, and validate JSON
    -> retry a failed batch up to three times
    -> save the quiz in generated_quizzes.json
    -> return the quiz to the frontend
```

If a quiz already exists for the same `document_id::difficulty`, the generate endpoint returns the saved quiz. Use **Regenerate Quiz** to replace it or change its question count.

## API Overview

### System and Materials

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Check that the backend is running |
| `GET` | `/api/model/health` | Return the configured chat model |
| `GET` | `/api/sources` | List indexed materials |
| `POST` | `/api/sources/upload` | Upload and index PDF/TXT files |
| `DELETE` | `/api/sources/{document_id}` | Delete a document and related data |
| `GET` | `/api/documents` | List indexed documents for Practice |

### Conversations

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/api/conversations` | Create a conversation |
| `GET` | `/api/conversations` | List conversations |
| `GET` | `/api/conversations/{conversation_id}` | Load a conversation and its messages |
| `PATCH` | `/api/conversations/{conversation_id}` | Rename a conversation |
| `DELETE` | `/api/conversations/{conversation_id}` | Delete a conversation |
| `PUT` | `/api/conversations/{conversation_id}/sources` | Replace the conversation source scope |
| `POST` | `/api/conversations/{conversation_id}/messages` | Send a question and receive a RAG answer |

### Quiz

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/quizzes` | List quiz availability by document and difficulty |
| `GET` | `/api/quiz/{document_id}?difficulty=easy` | Load a quiz and its latest progress |
| `POST` | `/api/quiz/generate` | Generate or load a saved quiz |
| `POST` | `/api/quiz/{document_id}/regenerate` | Replace a saved quiz |
| `PATCH` | `/api/quiz/{document_id}/progress` | Check and save one answer |
| `DELETE` | `/api/quiz/{document_id}/progress?difficulty=easy` | Reset current progress |
| `POST` | `/api/quiz/{document_id}/questions/{question_id}/explain` | Generate or load an explanation |
| `GET` | `/api/quiz-history` | List completed attempts |
| `GET` | `/api/quiz-history/{attempt_id}` | Load one completed attempt |

## Configuration

The main settings are defined in `config.py`:

```python
CHAT_MODEL = "hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M"
EMBEDDING_MODEL = "bge-m3"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
TOP_K = 4
```

If `EMBEDDING_MODEL` changes, existing documents must be indexed again because their vectors were created with the previous model.

## Troubleshooting

### The backend cannot connect to Ollama

Check the locally installed models:

```powershell
ollama list
```

Confirm that the model names match `config.py` and that Ollama is running.

### Chat returns insufficient context

1. Open **Sources** in the active conversation.
2. Select at least one indexed document.
3. Select **Apply sources**.
4. Confirm that the selected document contains indexed chunks.

### Retrieval becomes incorrect after changing the embedding model

Rebuild `vectorstore/` and index the documents again with the new embedding model.

### The frontend still shows an older UI

Reload the browser with `Ctrl + F5` and confirm that the frontend is running on port 3000.
