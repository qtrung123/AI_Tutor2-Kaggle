import re
import threading
import time
from contextlib import closing
from pathlib import Path

from backend import perf_timing, vector_store
from backend.conversation_store import (
    add_exchange,
    add_message,
    get_conversation,
    update_conversation_title,
)
from backend.indexed_document_store import list_indexed_documents
from backend.ingest import migrate_legacy_vector_ownership

from config import (
    CHAT_MODEL,
    CHAT_MAX_TOKENS,
    CHAT_NUM_CTX,
    GENERATION_KEEP_ALIVE,
    PROMPT_PATH,
    TOP_K,
)
from backend.llm_backend import ChatOllama
from backend.model_registry import resolve_generation_model


def load_vectorstore():
    """
    Open the document chunk store created by backend/ingest.py: durable pgvector on PostgreSQL,
    local Chroma on SQLite (backend/vector_store.py). The same embedding model must be used for
    both indexing and searching, otherwise the question vectors and document vectors would not
    live in the same space.
    """
    store = vector_store.get_vectorstore()
    migrate_legacy_vector_ownership(store)
    return store


def load_prompt_template() -> str:
    """
    Read the RAG prompt template.

    The template contains placeholders for context, conversation history, and question.
    """
    with open(PROMPT_PATH, "r", encoding="utf-8") as file:
        return file.read()


def format_docs(docs) -> str:
    """
    Convert retrieved LangChain documents into plain text context for the LLM.

    Each document keeps metadata such as source file and page number. Including
    that metadata in the prompt helps the model cite the material clearly.
    """
    context_parts = []

    for index, doc in enumerate(docs, start=1):
        metadata = getattr(doc, "metadata", {}) or {}
        source = metadata.get("source", "Unknown source")
        page = metadata.get("page", "Unknown page")

        context_parts.append(
            f"[Document {index}]\n"
            f"Source: {source}\n"
            f"Page: {page}\n"
            f"Content:\n{doc.page_content}"
        )

    return "\n\n".join(context_parts)


def _build_citations(docs) -> list[dict]:
    """
    Shape retrieved documents into the citation objects expected by frontend.

    The chatbot answer is response.content from Ollama; citations are the
    original chunks that were used as context for that answer.
    """
    citations = []

    for source_id, doc in enumerate(docs, start=1):
        metadata = getattr(doc, "metadata", {}) or {}
        source = str(metadata.get("source", "Unknown source"))
        title = Path(source).name if source != "Unknown source" else source
        citations.append(
            {
                "sourceId": source_id,
                "title": title,
                "page": str(metadata.get("page", "Unknown page")),
                "content": getattr(doc, "page_content", ""),
            }
        )

    return citations


def _chroma_filter(owner_id: str, document_ids: list[str], topic_id: str | None = None) -> dict:
    source_filter = (
        {"document_id": document_ids[0]}
        if len(document_ids) == 1
        else {"document_id": {"$in": document_ids}}
    )
    clauses = [{"owner_id": owner_id}, source_filter]
    if topic_id:
        clauses.append({"topic_id": topic_id})
    return {"$and": clauses}


def _retrieve_conversation_docs(
    owner_id: str, question: str, document_ids: list[str], topic_id: str | None = None
) -> list:
    """Retrieve globally relevant chunks while strictly enforcing the thread's source scope."""
    vectorstore = load_vectorstore()
    source_filter = _chroma_filter(owner_id, document_ids, topic_id)
    try:
        ranked_docs = vectorstore.similarity_search_with_score(
            question, k=TOP_K, filter=source_filter
        )
    except Exception:
        # Older Chroma versions may not accept $in. Keep the same strict scope
        # by querying each selected document and merging the ranked results.
        ranked_docs = []
        for document_id in document_ids:
            ranked_docs.extend(
                vectorstore.similarity_search_with_score(
                    question, k=TOP_K, filter=_chroma_filter(owner_id, [document_id], topic_id)
                )
            )

    ranked_docs.sort(key=lambda item: item[1])
    docs = []
    seen = set()
    for doc, _score in ranked_docs:
        metadata = getattr(doc, "metadata", {}) or {}
        key = (
            metadata.get("source"),
            metadata.get("page"),
            metadata.get("chunk"),
            getattr(doc, "page_content", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        docs.append(doc)
        if len(docs) >= TOP_K:
            break
    return docs


# Conversation history only resolves follow-ups ("it", "that", "and the second one?"); it is never
# evidence. So the prompt carries the last few turns, each shortened, within a total budget -- a long
# tutor answer or an Explain More request never bloats every later prompt.
HISTORY_MESSAGES = 6              # the last three exchanges
HISTORY_STUDENT_CHARS = 300
HISTORY_TUTOR_CHARS = 400
HISTORY_TOTAL_CHARS = 1800
# A short question is likely a follow-up: its retrieval query also gets the previous question.
FOLLOW_UP_MAX_WORDS = 12
RETRIEVAL_PREVIOUS_CHARS = 200
# The frontend's quiz review requests (js/quiz.js quizReviewExplainMessage): long, structured
# messages whose "Question:" line is what a later turn may refer to.
_EXPLAIN_MORE_PREFIX = "Explain more about this quiz question"


def _shorten(text: str, limit: int) -> str:
    """Whitespace collapsed; when too long, cut at the last sentence end (else word) before `limit`."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentence = max(head.rfind(". "), head.rfind("? "), head.rfind("! "))
    if sentence >= limit // 2:
        return head[:sentence + 1] + " …"
    return (head.rsplit(" ", 1)[0] if " " in head else head).rstrip(" ,;:-") + " …"


def _student_text(text: str, limit: int) -> str:
    """A student message as later turns need it: an Explain More request becomes its question."""
    if text.lstrip().startswith(_EXPLAIN_MORE_PREFIX):
        question = next((line.split(":", 1)[1].strip() for line in text.splitlines()
                         if line.strip().startswith("Question:")), "")
        if question:
            text = f"Explain more about the quiz question: {question}"
    return _shorten(text, limit)


_CITATION = re.compile(r"\s*\(source:[^)]*\)", re.IGNORECASE)


def _history_for_prompt(messages: list[dict], limit: int = HISTORY_MESSAGES) -> str:
    # Past tutor answers lose their (source: ...) citations: history is not evidence, so it must not
    # look citable.
    lines = [
        f"Student: {_student_text(message['content'], HISTORY_STUDENT_CHARS)}" if message["role"] == "user"
        else f"Tutor: {_shorten(_CITATION.sub('', message['content']), HISTORY_TUTOR_CHARS)}"
        for message in messages[-limit:]
    ]
    while lines and sum(len(line) + 1 for line in lines) > HISTORY_TOTAL_CHARS:
        lines.pop(0)   # oldest first; the latest exchange matters most for a follow-up
    return "\n".join(lines) if lines else "No previous messages."


def _retrieval_query(existing_messages: list[dict], message: str) -> str:
    """The new question; a short one (likely a follow-up) also carries the previous question,
    shortened, so "why?" still finds the right passages. Long requests stand on their own."""
    question = message.strip()
    if len(question.split()) > FOLLOW_UP_MAX_WORDS:
        return question
    previous = next((item["content"] for item in reversed(existing_messages) if item["role"] == "user"), "")
    if not previous:
        return question
    return f"{_student_text(previous, RETRIEVAL_PREVIOUS_CHARS)}\n{question}"


def _complete_answer(answer: str, metadata: dict | None) -> str:
    """An answer the output cap cut off ends at its last complete sentence (or paragraph) instead of
    mid-sentence; any other answer is returned unchanged."""
    if (metadata or {}).get("done_reason") != "length":
        return answer
    cut = max(answer.rfind("\n\n"), *(answer.rfind(mark) for mark in (". ", ".\n", "? ", "! ", "。")))
    return answer[:cut + 1].rstrip() if cut >= len(answer) // 2 else answer


def default_conversation_title(message: str, limit: int = 60) -> str:
    """Readable chat title from the first question: whitespace collapsed, cut at a word boundary."""
    text = " ".join(message.split())
    if len(text) <= limit:
        return text or "New conversation"
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,.;:-") + "…"


NO_MATERIAL_ANSWER = "I don't know based on the provided documents. Select at least one material for this conversation."
NO_CONTEXT_ANSWER = "I don't know based on the provided documents."


def _answer_inputs(owner_id: str, conversation: dict, existing_messages: list[dict], message: str):
    """(fixed answer, retrieved docs, prompt): a fixed answer when there is no material or no
    relevant chunk (prompt None), else the RAG prompt for the model. Shared by both chat APIs."""
    document_id = conversation.get("document_id")
    if not document_id:
        return NO_MATERIAL_ANSWER, [], None
    docs = _retrieve_conversation_docs(owner_id, _retrieval_query(existing_messages, message), [document_id])
    docs = [
        doc for doc in docs
        if (getattr(doc, "metadata", {}) or {}).get("owner_id") == owner_id
        and (getattr(doc, "metadata", {}) or {}).get("document_id") == document_id
    ]
    if not docs:
        return NO_CONTEXT_ANSWER, [], None
    prompt = load_prompt_template().format(
        context=format_docs(docs),
        conversation_history=_history_for_prompt(existing_messages),
        question=message.strip(),
    )
    return None, docs, prompt


_CHAT_MODELS: dict = {}


def _chat_model(model: str):
    # Same context window and residency as the Quiz calls, so Qwen is not reloaded between them.
    # One client per model per process: building one opens new HTTP clients (~0.5 s), which every
    # answer otherwise paid before its first token. Keyed by the class too, so a patched one is not mixed in.
    key = (ChatOllama, model)
    if key not in _CHAT_MODELS:
        _CHAT_MODELS[key] = ChatOllama(model=model, temperature=0, num_ctx=CHAT_NUM_CTX,
                                       num_predict=CHAT_MAX_TOKENS, keep_alive=GENERATION_KEEP_ALIVE)
    return _CHAT_MODELS[key]


def _grounding_status(answer: str) -> str:
    return "insufficient_context" if "don't know based on the provided documents" in answer.lower() else "supported"


def _save_assistant_answer(owner_id: str, conversation_id: str, answer: str, docs: list) -> tuple[dict, str, list]:
    """Persist the assistant message with its citations: (message, grounding status, citations)."""
    citations = _build_citations(docs)
    grounding_status = _grounding_status(answer)
    assistant_message = add_message(owner_id, conversation_id, "assistant", answer, grounding_status,
                                    _stored_citations(citations))
    return assistant_message, grounding_status, citations


def _stored_citations(citations: list[dict]) -> list[dict]:
    return [{**citation, "document_id": citation["title"], "chunk": ""} for citation in citations]


def _first_title(conversation: dict, existing_messages: list, message: str) -> str | None:
    """The title a new chat gets from its first question, else None (the title stays)."""
    if conversation["title"] == "New conversation" and not existing_messages:
        return default_conversation_title(message)
    return None


def _set_first_title(owner_id: str, conversation_id: str, conversation: dict, existing_messages: list, message: str):
    title = _first_title(conversation, existing_messages, message)
    if title is not None:
        update_conversation_title(owner_id, conversation_id, title, return_conversation=False)


def _conversation_for_answer(owner_id: str, conversation_id: str) -> dict:
    """The conversation with just what answering needs: its last HISTORY_MESSAGES messages (the
    prompt history and the previous question for a follow-up), without citations. The full
    history with citations is only loaded for the conversation view (GET /api/conversations/{id})."""
    return get_conversation(owner_id, conversation_id, include_messages=True,
                            recent_messages=HISTORY_MESSAGES, include_citations=False)


def answer_conversation_message(owner_id: str, conversation_id: str, message: str, model_id: str | None = None) -> dict:
    """Answer and persist one message in a source-isolated conversation."""
    model = resolve_generation_model(model_id)
    conversation = _conversation_for_answer(owner_id, conversation_id)
    user_message = add_message(owner_id, conversation_id, "user", message.strip())

    existing_messages = conversation.get("messages", [])
    _set_first_title(owner_id, conversation_id, conversation, existing_messages, message)

    answer, docs, prompt = _answer_inputs(owner_id, conversation, existing_messages, message)
    if prompt is not None:
        started = time.perf_counter()
        response = _chat_model(model).invoke(prompt)
        perf_timing.log_llm_call("chat", response, time.perf_counter() - started)
        answer = _complete_answer(str(response.content).strip(), getattr(response, "response_metadata", None))

    assistant_message, grounding_status, citations = _save_assistant_answer(owner_id, conversation_id, answer, docs)
    return {
        "user_message": user_message,
        "assistant_message": assistant_message,
        "answer": answer,
        "model": model,
        "grounding_status": grounding_status,
        "citations": citations,
    }


def stream_conversation_message(owner_id: str, conversation_id: str, message: str, model_id: str | None = None,
                                cancelled: threading.Event | None = None):
    """The streaming twin of answer_conversation_message. Validates eagerly (an unknown model or
    conversation raises ValueError here, before any event), then returns a generator of events:

        {"type": "start", "model": ...}
        {"type": "delta", "text": ...}       (zero or more, in order: the answer as it is generated)
        {"type": "done", ...}                (the same payload answer_conversation_message returns)

    Nothing is persisted until the model has finished: the user message, the title of a new chat
    and the assistant message with its citations are saved together right before "done". When
    `cancelled` is set (the client went away) or the generator is closed early, it stops reading
    the model stream -- which closes the request to Ollama/vLLM -- and saves nothing. Model errors
    propagate to the caller, also with nothing saved."""
    model = resolve_generation_model(model_id)
    conversation = _conversation_for_answer(owner_id, conversation_id)
    text = message.strip()

    def stopped() -> bool:
        return cancelled is not None and cancelled.is_set()

    def events():
        yield {"type": "start", "model": model}
        existing_messages = conversation.get("messages", [])
        answer, docs, prompt = _answer_inputs(owner_id, conversation, existing_messages, text)
        if prompt is None:
            yield {"type": "delta", "text": answer}
        else:
            parts, last, first_token = [], None, None
            started = time.perf_counter()
            with closing(_chat_model(model).stream(prompt)) as chunks:
                for chunk in chunks:
                    if stopped():
                        return
                    last = chunk
                    piece = str(chunk.content or "")
                    if piece:
                        first_token = first_token or time.perf_counter()
                        parts.append(piece)
                        yield {"type": "delta", "text": piece}
            perf_timing.log_llm_call("chat_stream", last, time.perf_counter() - started,
                                     first_token_ms=round((first_token - started) * 1000, 1) if first_token else None)
            # The pieces already streamed may end mid-sentence when the cap hit; the saved answer
            # (and the "done" event the frontend renders last) does not.
            answer = _complete_answer("".join(parts).strip(), getattr(last, "response_metadata", None))
        if stopped():
            return
        # One transaction: the question, the answer with its citations and a new chat's title.
        citations = _build_citations(docs)
        grounding_status = _grounding_status(answer)
        user_message, assistant_message = add_exchange(
            owner_id, conversation_id, text, answer, grounding_status, _stored_citations(citations),
            title=_first_title(conversation, existing_messages, text),
        )
        yield {
            "type": "done",
            "user_message": user_message,
            "assistant_message": assistant_message,
            "answer": answer,
            "model": model,
            "grounding_status": grounding_status,
            "citations": citations,
        }

    return events()


def explain_quiz_answer(
    owner_id: str,
    document_id: str,
    question: str,
    options: list[str],
    correct_answer: str | list[str],
) -> dict:
    """Generate a short, grounded quiz explanation from only the selected document."""
    vectorstore = load_vectorstore()
    retrieval_query = f"{question}\n" + "\n".join(options)
    docs = vectorstore.similarity_search(
        retrieval_query,
        k=TOP_K,
        filter={"$and": [{"owner_id": owner_id}, {"document_id": document_id}]},
    )
    if not docs:
        raise ValueError("No relevant chunks were found in the selected document.")

    correct_answers = correct_answer if isinstance(correct_answer, list) else [correct_answer]
    correct_options = [
        option for answer in correct_answers
        for option in options if option.strip().upper().startswith(f"{str(answer).upper()}.")
    ]
    correct_option = "; ".join(correct_options or [str(answer) for answer in correct_answers])
    prompt = f"""
Use only the course material context below.

Question: {question}
Options:
{chr(10).join(options)}
Correct answer: {correct_option}

Explain in 2 to 3 concise sentences why the correct answer is correct.
Only explain the correct answer. Do not discuss any learner selection or wrong option.
Do not mention retrieval, chunks, or these instructions. Do not use outside knowledge.

COURSE MATERIAL CONTEXT:
{format_docs(docs)}
""".strip()
    llm = ChatOllama(model=CHAT_MODEL, temperature=0, num_ctx=CHAT_NUM_CTX, num_predict=220,
                     keep_alive=GENERATION_KEEP_ALIVE)
    started = time.perf_counter()
    response = llm.invoke(prompt)
    perf_timing.log_llm_call("quiz_explanation", response, time.perf_counter() - started)
    return {
        "explanation": str(response.content).strip(),
        "model": CHAT_MODEL,
        "citations": _build_citations(docs),
    }


def list_uploaded_sources(owner_id: str) -> list[dict]:
    """
    Return the indexed document summary shown in the frontend source panel.

    This reads the indexed_documents table, which backend/ingest.py updates after
    documents are chunked and saved into Chroma.
    """
    sources = []
    for source_id, info in enumerate(list_indexed_documents(owner_id), start=1):
        file_name = info["document_id"]
        source_path = Path(info.get("path", file_name))
        sources.append(
            {
                "sourceId": source_id,
                "title": file_name,
                "chunks": info.get("chunks", 0),
                "path": str(source_path),
            }
        )

    return sources
