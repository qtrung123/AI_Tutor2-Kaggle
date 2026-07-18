# ✅ Backend Functions — One Clear Responsibility per Function

This document maps every function in the `backend/` package to its primary responsibility. It is intended as a quick reference for maintenance, onboarding, and code review.

## Module Overview

| Module | Responsibility |
| --- | --- |
| `main.py` | Define FastAPI request/response models and expose HTTP endpoints. |
| `ingest.py` | Load, clean, chunk, index, and delete course documents. |
| `rag_service.py` | Retrieve grounded context and call the chat model. |
| `conversation_store.py` | Persist conversations, messages, source scopes, and citations in SQLite. |
| `quiz_service.py` | Orchestrate quiz generation, validation, scoring, progress, history, and explanations. |
| `quiz_store.py` | Persist quizzes, attempts, progress, history, and explanations in SQLite. |
| `__init__.py` | Mark `backend` as a Python package; it currently defines no functions. |

> Functions beginning with `_` are internal helpers. Other functions form the module's public service interface or FastAPI route layer.

## `backend/main.py`

FastAPI route handlers should validate HTTP input, delegate business logic to a service/store module, and translate service errors into HTTP responses.

| Function | Responsibility |
| --- | --- |
| `root()` | Confirm that the FastAPI backend is running. |
| `model_health()` | Return the configured chat model and basic backend status. |
| `sources()` | Return summaries of all indexed source documents. |
| `upload_sources()` | Validate uploaded PDF/TXT files, save them, run indexing, and return refreshed source data. |
| `delete_source()` | Delete an indexed document and remove its related quiz and conversation-source data. |
| `documents()` | Return indexed documents in the shape required by the Practice UI. |
| `quizzes()` | Return available quiz variants and status for every indexed document. |
| `quiz_history()` | List completed quiz attempts with optional document and difficulty filters. |
| `quiz_history_detail()` | Return the stored snapshot of one completed quiz attempt. |
| `quiz_detail()` | Load one saved quiz variant and its latest progress/attempt. |
| `quiz_generate()` | Validate a generation request and delegate quiz creation or cache loading. |
| `quiz_progress()` | Check one selected answer and persist the current quiz progress. |
| `quiz_progress_reset()` | Clear active progress without deleting completed attempt history. |
| `quiz_explain()` | Generate or retrieve the cached explanation for one quiz question. |
| `quiz_regenerate()` | Replace one document/difficulty quiz and reset its active attempt data. |
| `_validate_conversation_document_ids()` | Reject conversation source IDs that are not currently indexed. |
| `conversation_create()` | Create a conversation with an initial title and source scope. |
| `conversation_list()` | List saved conversations ordered by recent activity. |
| `conversation_get()` | Load one conversation with its messages, citations, and source IDs. |
| `conversation_update()` | Rename an existing conversation. |
| `conversation_delete()` | Delete a conversation and its dependent SQLite records. |
| `conversation_sources_update()` | Replace the set of documents available to one conversation. |
| `conversation_message()` | Send one message through the source-scoped conversational RAG flow. |

## `backend/ingest.py`

This module owns the document lifecycle from raw upload to searchable Chroma chunks.

| Function | Responsibility |
| --- | --- |
| `calculate_file_hash()` | Calculate a SHA-256 content hash for duplicate and change detection. |
| `load_indexed_files()` | Read indexed-document metadata from `indexed_files.json`. |
| `save_indexed_files()` | Persist indexed-document metadata to `indexed_files.json`. |
| `load_single_file()` | Load one supported PDF or TXT file into LangChain documents. |
| `clean_documents()` | Remove pages/documents that contain no usable text. |
| `split_documents()` | Divide documents into overlapping chunks using the configured chunk settings. |
| `get_vectorstore()` | Open or create the configured Chroma collection with Ollama embeddings. |
| `index_files()` | Incrementally hash, load, chunk, embed, and register a list of files. |
| `delete_indexed_file()` | Remove a document's vectors, metadata entry, and local uploaded file. |
| `index_all_data_files()` | Find all PDF/TXT files in `data/` and pass them through incremental indexing. |
| `main()` | Run the command-line indexing workflow and print its summary. |

## `backend/rag_service.py`

This module owns retrieval, prompt assembly, model calls, and citation construction for chat and on-demand quiz explanations.

| Function | Responsibility |
| --- | --- |
| `load_vectorstore()` | Open the shared Chroma collection with the configured embedding model. |
| `load_prompt_template()` | Load the grounded chat prompt from `prompts/rag_prompt.txt`. |
| `format_docs()` | Format retrieved documents and metadata as LLM context. |
| `_build_citations()` | Convert retrieved LangChain documents into frontend citation objects. |
| `_retrieve_conversation_docs()` | Retrieve top-ranked chunks only from the active conversation's selected sources. |
| `_history_for_prompt()` | Format the most recent conversation messages for follow-up understanding. |
| `answer_conversation_message()` | Orchestrate source-scoped conversational RAG and persist both sides of the exchange. |
| `explain_quiz_answer()` | Generate a short grounded explanation of why a quiz answer is correct. |
| `list_uploaded_sources()` | Convert `indexed_files.json` metadata into source summaries for the UI. |

## `backend/conversation_store.py`

This module provides the SQLite persistence boundary for chat history.

| Function | Responsibility |
| --- | --- |
| `_now()` | Produce the current UTC timestamp in ISO format. |
| `_connect()` | Open a SQLite connection with row mapping and foreign keys enabled. |
| `initialize_conversation_store()` | Create conversation, source, message, citation, and index tables when missing. |
| `_source_ids()` | Load the ordered source-document IDs assigned to one conversation. |
| `create_conversation()` | Insert a conversation and its initial source-document relationships. |
| `list_conversations()` | Return all conversations with their source IDs, newest activity first. |
| `get_conversation()` | Load one conversation and optionally include messages and citations. |
| `update_conversation_title()` | Change a conversation title and update its activity timestamp. |
| `set_conversation_sources()` | Replace all source-document relationships for a conversation. |
| `add_message()` | Persist one user/assistant message and any attached citations. |
| `delete_conversation()` | Delete one conversation and cascade-delete dependent records. |
| `remove_source_from_conversations()` | Remove a deleted document from every conversation scope. |

## `backend/quiz_service.py`

This module contains quiz business logic. It reads indexed material, builds prompts, validates model output, manages attempts, and coordinates persistence through `quiz_store.py`.

### Document and Context Helpers

| Function | Responsibility |
| --- | --- |
| `_load_indexed_files()` | Read indexed-document metadata for quiz services. |
| `list_indexed_documents()` | Return documents available for quiz generation with IDs, chunk counts, and hashes. |
| `_document_lookup()` | Build a document-ID lookup table from indexed-document summaries. |
| `_load_vectorstore()` | Open Chroma for direct access to a selected document's chunks. |
| `_source_matches()` | Determine whether Chroma source metadata belongs to a document ID. |
| `_chunk_index_from_id()` | Derive an ordered chunk number from a Chroma vector ID. |
| `_result_to_chunks()` | Normalize raw Chroma results into ordered quiz-context chunk dictionaries. |
| `get_document_chunks()` | Load all chunks for one selected document, with a compatibility fallback query. |
| `_format_context()` | Format all selected chunks as bounded, metadata-labelled quiz context. |
| `load_quiz_prompt_template()` | Load the quiz generation template from `prompts/quiz_prompt.txt`. |

### Prompt and Output Helpers

| Function | Responsibility |
| --- | --- |
| `_difficulty_instructions()` | Return the cognitive requirements for easy, medium, or difficult questions. |
| `_format_avoid_questions()` | Format previously used question text for prompt instructions. |
| `_build_prompt()` | Fill the main quiz template with document, count, difficulty, constraints, and context. |
| `_build_retry_prompt()` | Build a focused repair prompt containing the previous validation errors. |
| `_extract_json()` | Remove optional code fences and parse the JSON object from model output. |
| `_normalize_correct_answer()` | Convert answer letters or answer text into a canonical `A`–`D` letter. |
| `_normalize_options()` | Convert dictionary/list options into four consistently labelled choices. |
| `_clean_inline_text()` | Collapse whitespace and newline artifacts in model-generated text. |
| `_option_text()` | Remove the leading option label from an answer choice. |
| `_looks_like_raw_chunk()` | Detect answer choices that appear to copy a long source chunk. |
| `_validate_question_quality()` | Reject generic questions, duplicate options, forbidden distractors, and raw chunk text. |
| `_validate_difficulty_quality()` | Reject obvious recall questions that do not satisfy medium/difficult requirements. |
| `_validate_quiz_batch()` | Normalize and validate an exact-size batch of model-generated questions. |
| `_quiz_output_budget()` | Calculate an output-token budget from missing question count and retry number. |
| `_generate_quiz_batch()` | Generate, validate, retain valid questions, and retry until one batch is complete. |
| `_batch_plan()` | Split a requested question count into batches of at most eight. |

### Quiz Use Cases

| Function | Responsibility |
| --- | --- |
| `generate_quiz()` | Validate parameters, load/save by cache key, generate all batches, and persist the final quiz. |
| `load_quiz_with_attempt()` | Return one saved quiz together with its latest active progress/attempt. |
| `list_quiz_statuses()` | Report available difficulty variants and question counts for every document. |
| `update_quiz_progress()` | Check one answer, recompute partial score, and autosave progress or completion. |
| `list_completed_quiz_attempts()` | Return compact summaries of completed attempts for Quiz History. |
| `load_completed_quiz_attempt()` | Load the immutable question/result snapshot of one completed attempt. |
| `clear_quiz_progress()` | Reset the current attempt while preserving completed history. |
| `explain_quiz_question()` | Load/cache a grounded explanation for one question's correct answer. |

## `backend/quiz_store.py`

This module is the SQLite repository for versioned quizzes, questions, options, attempts, answers, and explanations. It also performs a one-time import from the legacy JSON files.

### Database and Migration Helpers

| Function | Responsibility |
| --- | --- |
| `utc_now_iso()` | Produce a stable UTC timestamp for quiz records. |
| `_connect()` | Open the shared SQLite database with foreign keys and a busy timeout. |
| `_read_legacy_json()` | Read an old quiz JSON file only for one-time migration. |
| `initialize_quiz_store()` | Create all quiz tables/indexes and trigger migration once. |
| `_iter_legacy_quizzes()` | Normalize legacy flat/nested quiz JSON into document/difficulty variants. |
| `_migrate_legacy_json()` | Import existing quizzes, attempts, answers, and explanations into SQLite. |

### Generated Quiz Storage

| Function | Responsibility |
| --- | --- |
| `quiz_cache_key()` | Build the stable `document_id::difficulty` quiz key. |
| `_normalize_options()` | Normalize legacy dictionary/list options before SQL insertion. |
| `_insert_quiz()` | Deactivate the previous variant and insert a versioned quiz with questions/options. |
| `_row_to_quiz()` | Reconstruct the quiz API shape from normalized SQL rows. |
| `get_quiz()` | Load the active quiz for one document and difficulty. |
| `save_quiz()` | Persist a new active quiz version in one transaction. |
| `list_document_quizzes()` | Return every active difficulty variant for one document. |

### Attempt and Progress Storage

| Function | Responsibility |
| --- | --- |
| `_row_to_attempt()` | Reconstruct answers and question-result snapshots from SQL rows. |
| `get_latest_attempt()` | Return the latest attempt/progress for one document and difficulty. |
| `_save_attempt_row()` | Upsert an attempt and replace its normalized answer rows. |
| `save_quiz_progress()` | Upsert current progress and archive it once when it first becomes complete. |
| `reset_quiz_progress()` | Clear the latest active attempt without deleting completed history. |

### Explanation and Cleanup Storage

| Function | Responsibility |
| --- | --- |
| `get_quiz_explanation()` | Retrieve one explanation by its cache key. |
| `save_quiz_explanation()` | Persist one generated explanation. |
| `delete_document_quiz_data()` | Delete all quizzes, attempts, and explanations for a removed document. |
| `delete_document_attempts()` | Reset active attempt/explanation data for one regenerated quiz variant. |
| `list_quiz_history()` | Collect and sort completed attempts with optional filters. |
| `get_quiz_history_attempt()` | Find one completed attempt by its unique attempt ID. |

## Responsibility Boundaries

```text
main.py
  -> HTTP validation and response handling

ingest.py
  -> document lifecycle and vector indexing

rag_service.py
  -> retrieval, prompts, LLM calls, and citations

conversation_store.py
  -> SQLite chat persistence

quiz_service.py
  -> quiz business rules and orchestration

quiz_store.py
  -> SQLite quiz persistence and legacy JSON migration
```

Keeping these boundaries intact prevents route handlers from accumulating business logic and prevents storage modules from depending on UI or HTTP concerns.
