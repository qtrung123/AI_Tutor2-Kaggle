# AI Tutor – Sequence Diagrams

These UML sequence diagrams show the main use cases of the AI Tutor. Each one was traced from the
current source code: the routes in `backend/api/`, the services and stores in `backend/`, and the
callers in `frontend/`. The Mermaid sources (`.mmd`) can be edited. The rendered SVGs in
[`exports/`](exports/) are the ones to put in the thesis.

## Diagram list

| # | Diagram | Source | Export | Lifelines | Size (px) |
| --- | --- | --- | --- | --- | --- |
| 1 | Authentication | [`01-authentication.mmd`](01-authentication.mmd) | [svg](exports/01-authentication.svg) | 5 | 1322 × 1637 |
| 2 | Document upload, topic extraction & indexing | [`02-document-upload-indexing.mmd`](02-document-upload-indexing.mmd) | [svg](exports/02-document-upload-indexing.svg) | 9 | 1845 × 1622 |
| 3 | AI Chat / RAG (streaming) | [`03-chat-rag.mmd`](03-chat-rag.mmd) | [svg](exports/03-chat-rag.svg) | 8 | 1794 × 1548 |
| 4a | Summary – request, cache, job start, polling | [`04a-summary-request-polling.mmd`](04a-summary-request-polling.mmd) | [svg](exports/04a-summary-request-polling.svg) | 5 | 1336 × 1385 |
| 4b | Summary – background generation (batching, retry) | [`04b-summary-background-generation.mmd`](04b-summary-background-generation.mmd) | [svg](exports/04b-summary-background-generation.svg) | 5 | 1130 × 1107 |
| 5 | Flashcards generation | [`05-flashcards-generation.mmd`](05-flashcards-generation.mmd) | [svg](exports/05-flashcards-generation.svg) | 7 | 1648 × 1420 |
| 6a | Quiz generation | [`06a-quiz-generation.mmd`](06a-quiz-generation.mmd) | [svg](exports/06a-quiz-generation.svg) | 7 | 1512 × 1241 |
| 6b | Quiz submission & scoring | [`06b-quiz-submission-scoring.mmd`](06b-quiz-submission-scoring.mmd) | [svg](exports/06b-quiz-submission-scoring.svg) | 6 | 1426 × 1455 |
| 7 | Subject management | [`07-subject-management.mmd`](07-subject-management.mmd) | [svg](exports/07-subject-management.svg) | 5 | 1236 × 1615 |
| 8a | Planner – plan, availability, materials | [`08a-planner-setup.mmd`](08a-planner-setup.mmd) | [svg](exports/08a-planner-setup.svg) | 5 | 1235 × 1253 |
| 8b | Planner – preview & confirm (scheduling) | [`08b-planner-preview-confirm.mmd`](08b-planner-preview-confirm.mmd) | [svg](exports/08b-planner-preview-confirm.svg) | 7 | 1644 × 1569 |
| 9a | Study session – start, complete, skip | [`09a-study-session-execution.mmd`](09a-study-session-execution.mmd) | [svg](exports/09a-study-session-execution.svg) | 5 | 1289 × 1769 |
| 9b | Progress tracking | [`09b-progress-tracking.mmd`](09b-progress-tracking.mmd) | [svg](exports/09b-progress-tracking.svg) | 5 | 1220 × 752 |

Diagrams 4, 8 and 9 were each split in two, so that every diagram fits on one portrait page.

## Presentation conventions

- **Same style in every file.** All diagrams share one `%%{init}%%` header: the `neutral` theme,
  Times New Roman at 16 px, light notes (`#f7f7f2`), actors mirrored at the bottom, numbered
  messages, participant boxes 130 px wide and 35 px apart.
- **Abstraction level.** The diagrams name each API route and the main service function.
  Request bodies, query parameters and internal helpers appear only when they explain a decision.
  `require_current_user()` (cookie → user, otherwise 401, every query scoped by `owner_id`) is
  shown once, in diagram 1. `prepare_generation_model()` is shown as a note: it checks Ollama
  `/api/tags` and pulls a lazy model on first use.
- **Arrows.** Solid arrows are calls. Dashed arrows are returns. The SSE events in diagram 3 and
  the poll results in diagram 4a are drawn as dashed returns to the frontend.
- **Simplified lifelines.** "SQLite" stands for every store module (`*_store.py`) and its tables.
  "ChromaDB" is the `study_documents` collection. "BGE-M3" and "Qwen2.5-7B" are the two Ollama
  models.

## Deployment notes that apply to all diagrams

- **Databases.** The diagrams show the default local setup: SQLite (`data/conversations.db`) and
  local ChromaDB (`vectorstore/`). When `DATABASE_URL` is set, the same calls go to PostgreSQL +
  pgvector, and uploaded originals are also stored in a private Supabase Storage bucket
  (`backend/db.py`, `backend/vector_store.py`, `backend/document_storage.py`).
- **Models.** BGE-M3 is used only for embeddings: chunks in diagram 2 and the question in diagram 3.
  Summary, Flashcards and Quiz read chunks with a ChromaDB metadata filter and do not embed
  anything. Qwen2.5-7B (or Gemma 3 12B, if selected) runs topic-heading refinement, Chat, Summary,
  Flashcards and quiz multiple-choice questions. Grading, mastery, the scheduler and progress are
  deterministic and never call the LLM.
- **Google Calendar** is optional. When connected, confirmed sessions are synced as events. Busy
  time is subtracted from availability only when "Avoid conflicts" is enabled. A sync failure never
  undoes a planner change.
- **Proxies.** On Kaggle, requests pass through nginx and a Cloudflare tunnel. These only forward
  traffic, so they are not drawn. Summary uses a background job because a long generation would
  exceed the proxy timeout (HTTP 524).

## Diagram descriptions and source references

**1. Authentication.** On page load the frontend calls `/me` to restore the session. Sign-up and
sign-in check the password with Argon2. A session token is stored as its SHA-256 hash and sent once
as an HttpOnly cookie, valid for 14 days. Logout revokes the session.
Sources: `backend/api/auth.py`, `backend/api/deps.py`, `backend/auth_store.py`, `frontend/app.js`.

**2. Document upload & indexing.** One synchronous request runs under `_INDEXING_LOCK`. Each file is
hashed to detect duplicates, parsed and chunked (800/150). Topics are extracted, and Qwen is called
only when heading confidence is low. Chunks are embedded with BGE-M3 and stored in ChromaDB, then
registered in SQLite. An optional subject is assigned after indexing.
Sources: `backend/api/documents.py`, `backend/ingest.py`, `backend/topic_extractor.py`,
`backend/vector_store.py`, `backend/indexed_document_store.py`, `frontend/js/tutor.js`.

**3. Chat / RAG.** One conversation per document. Retrieval returns the top 4 chunks, filtered by
owner and document. If there is no document or no relevant chunk, a fixed answer is returned
without calling the LLM. Tokens are streamed over SSE. Nothing is saved until the answer is
complete, and then it is saved in one transaction (`add_exchange`).
Sources: `backend/api/conversations.py`, `backend/rag_service.py`, `backend/conversation_store.py`,
`prompts/chat/rag_answer.txt`, `frontend/js/tutor.js`, `frontend/js/chat_stream.js`.

**4a/4b. Summary.** Opening the tab only looks up an existing summary. Generate either returns the
cached summary or starts a job and returns 202 with a `job_id`. A second request for the same
document joins the running job. The frontend polls every 2 s and stops after 5 consecutive errors.
A worker with one thread splits the topics into batches of at most 4 that fit `num_ctx` 32768. Each
batch gets up to 2 attempts, and the second attempt asks only for missing topics. Each topic's notes
are checked against that topic's evidence. One synthesis call then writes the overview and 4–7
takeaways. A job that is still marked processing but has no live worker (after a restart) is
marked failed.
Sources: `backend/api/summary.py`, `backend/summary_jobs.py`, `backend/summary_service.py`,
`backend/summary_store.py`, `frontend/js/session.js`.

**5. Flashcards.** The cache key is the document hash, topic schema, version, model, topics and
language. There are at most 2 attempts, and a retry covers only topics that still have no valid
card. Cards are filtered for provenance, quality, language and duplicates. A set is saved only if
every topic has at least one card. Otherwise the API returns 502.
Sources: `backend/api/flashcards.py`, `backend/flashcard_service.py`, `backend/flashcard_store.py`.

**6a. Quiz generation.** The document must already have flashcards (otherwise 409). Matching and
multi-blank questions are built from the flashcards without the LLM. The LLM writes only the
multiple-choice questions, in a bounded loop: each call is streamed with a deadline, and the loop
stops at the target count, the stall limit or the time budget. Each candidate is validated
deterministically. The request fails with 422 only if no valid question was produced. The frontend
always sends `regenerate: true`, so every Create saves a new quiz.
Sources: `backend/api/quiz_generation.py`, `backend/quiz_service.py`, `backend/quiz_units.py`,
`backend/quiz_store.py`, `frontend/js/quiz.js`.

**6b. Quiz submission & scoring.** Answers are autosaved while the student works. On submit, grading
is deterministic and the result is saved as an immutable attempt. Mastery is recomputed (but not for
flashcard practice), and the planner session's quiz activity is completed. Planner errors are
reported in the response but do not fail the submit. Afterwards the frontend asks for a plan
adaptation: a small change is applied immediately, and a large change needs the learner's review.
Sources: `backend/api/quiz_attempts.py`, `backend/quiz_attempt_service.py`,
`backend/mastery_service.py`, `backend/study_plan_api_service.py`, `frontend/js/quiz.js`,
`frontend/js/planner.js`.

**7. Subjects.** Create, rename, assign and delete. Subject names are unique per owner, ignoring
case. Assignment is all-or-nothing. Deleting a subject only unassigns its documents.
Sources: `backend/api/subjects.py`, `backend/subject_store.py`, `frontend/js/subjects.js`.

**8a/8b. Planner.** Create a plan, add availability (slots in the past are rejected) and add
documents as materials. Any change triggers a debounced preview, which runs the deterministic
scheduler and saves nothing. Confirm recomputes the schedule on the server, rejects outdated
calendar drags (409), and saves the sessions and the `schedule_run` in one transaction. If nothing
can be scheduled, confirm returns 400.
Sources: `backend/api/planner_v2.py`, `backend/api/planner_legacy.py` (availability),
`backend/study_plan_api_service.py`, `backend/study_planner_service.py`,
`backend/study_scheduler.py`, `backend/study_placement.py`, `backend/google_calendar_sync.py`,
`frontend/js/planner.js`.

**9a/9b. Study session & progress.** Start returns 409 if the session's time has passed, and
otherwise tells the frontend which tool to open; nothing is generated. Quiz activities are completed
by submitting the quiz (6b). Combined sessions complete step by step. A single session can be
completed or skipped, and a skip can trigger adaptation. The progress endpoints only read data.
Sources: `backend/api/planner_v2.py`, `backend/study_plan_api_service.py`,
`backend/study_planner_store.py`, `backend/study_progress.py`, `frontend/js/planner.js`,
`frontend/js/home.js`, `frontend/js/session.js`.

## Rendering

All 13 diagrams render with Mermaid CLI 11.4.2 without errors. To regenerate the exports from the
repository root:

```bash
# all diagrams → SVG (vector, best for the thesis)
for f in docs/sequence-diagrams/*.mmd; do
  npx -p @mermaid-js/mermaid-cli@11.4.2 mmdc -i "$f" \
    -o "docs/sequence-diagrams/exports/$(basename "${f%.mmd}").svg" -b white
done

# one diagram → PNG (e.g. for slides)
npx -p @mermaid-js/mermaid-cli@11.4.2 mmdc -i docs/sequence-diagrams/03-chat-rag.mmd -o 03-chat-rag.png -b white -w 1600
```

Tips for the thesis: insert the SVGs at full text width. The largest diagram (02) is about 1850 px
wide, so 16 px text prints at roughly 5–6 pt on a 16 cm column. If that is too small, use a
landscape page or crop to the lifelines that matter. You can also paste a source file into
<https://mermaid.live> for quick edits.