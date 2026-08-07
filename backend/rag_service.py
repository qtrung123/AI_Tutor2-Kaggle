import json
from pathlib import Path

from langchain_chroma import Chroma
from langchain_ollama import ChatOllama, OllamaEmbeddings

from backend.conversation_store import (
    add_message,
    get_conversation,
    update_conversation_title,
)

from config import (
    CHAT_MODEL,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    INDEXED_FILES_PATH,
    PROMPT_PATH,
    TOP_K,
    VECTORSTORE_DIR,
)


def load_vectorstore() -> Chroma:
    """
    Open the existing local Chroma vector database.

    This is the "knowledge base" created by backend/ingest.py. The same
    embedding model must be used for both indexing and searching, otherwise
    the question vectors and document vectors would not live in the same space.
    """
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)

    return Chroma(
        persist_directory=str(VECTORSTORE_DIR),
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
    )


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


def _chroma_filter(document_ids: list[str], topic_id: str | None = None) -> dict:
    source_filter = (
        {"source": document_ids[0]}
        if len(document_ids) == 1
        else {"source": {"$in": document_ids}}
    )
    if not topic_id:
        return source_filter
    return {"$and": [source_filter, {"topic_id": topic_id}]}


def _retrieve_conversation_docs(
    question: str, document_ids: list[str], topic_id: str | None = None
) -> list:
    """Retrieve globally relevant chunks while strictly enforcing the thread's source scope."""
    vectorstore = load_vectorstore()
    source_filter = _chroma_filter(document_ids, topic_id)
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
                    question, k=TOP_K, filter=_chroma_filter([document_id], topic_id)
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


def _history_for_prompt(messages: list[dict], limit: int = 8) -> str:
    recent = messages[-limit:]
    if not recent:
        return "No previous messages."
    return "\n".join(
        f"{('Student' if message['role'] == 'user' else 'Tutor')}: {message['content']}"
        for message in recent
    )


def answer_conversation_message(conversation_id: str, message: str) -> dict:
    """Answer and persist one message in a source-isolated conversation."""
    conversation = get_conversation(conversation_id, include_messages=True)
    user_message = add_message(conversation_id, "user", message.strip())

    existing_messages = conversation.get("messages", [])
    if conversation["title"] == "New conversation" and not existing_messages:
        update_conversation_title(conversation_id, message.strip()[:64])

    document_ids = conversation.get("document_ids", [])
    if not document_ids:
        answer = "I don't know based on the provided documents. Select at least one material for this conversation."
        assistant_message = add_message(
            conversation_id, "assistant", answer, "insufficient_context", []
        )
        return {
            "user_message": user_message,
            "assistant_message": assistant_message,
            "answer": answer,
            "model": CHAT_MODEL,
            "grounding_status": "insufficient_context",
            "citations": [],
        }

    recent_user_text = " ".join(
        item["content"] for item in existing_messages[-4:] if item["role"] == "user"
    )
    retrieval_query = f"{recent_user_text}\n{message}".strip()
    docs = _retrieve_conversation_docs(retrieval_query, document_ids)
    if not docs:
        answer = "I don't know based on the provided documents."
        citations = []
        grounding_status = "insufficient_context"
    else:
        final_prompt = load_prompt_template().format(
            context=format_docs(docs),
            conversation_history=_history_for_prompt(existing_messages),
            question=message.strip(),
        )
        response = ChatOllama(model=CHAT_MODEL, temperature=0).invoke(final_prompt)
        answer = str(response.content).strip()
        citations = _build_citations(docs)
        grounding_status = (
            "insufficient_context"
            if "don't know based on the provided documents" in answer.lower()
            else "supported"
        )

    stored_citations = [
        {
            **citation,
            "document_id": citation["title"],
            "chunk": "",
        }
        for citation in citations
    ]
    assistant_message = add_message(
        conversation_id,
        "assistant",
        answer,
        grounding_status,
        stored_citations,
    )
    return {
        "user_message": user_message,
        "assistant_message": assistant_message,
        "answer": answer,
        "model": CHAT_MODEL,
        "grounding_status": grounding_status,
        "citations": citations,
    }


def explain_quiz_answer(
    document_id: str,
    question: str,
    options: list[str],
    correct_answer: str,
) -> dict:
    """Generate a short, grounded quiz explanation from only the selected document."""
    vectorstore = load_vectorstore()
    retrieval_query = f"{question}\n" + "\n".join(options)
    docs = vectorstore.similarity_search(
        retrieval_query,
        k=TOP_K,
        filter={"source": document_id},
    )
    if not docs:
        raise ValueError("No relevant chunks were found in the selected document.")

    correct_option = next(
        (option for option in options if option.strip().upper().startswith(f"{correct_answer}.")),
        correct_answer,
    )
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
    llm = ChatOllama(model=CHAT_MODEL, temperature=0, num_ctx=4096, num_predict=220)
    response = llm.invoke(prompt)
    return {
        "explanation": str(response.content).strip(),
        "model": CHAT_MODEL,
        "citations": _build_citations(docs),
    }


def list_uploaded_sources() -> list[dict]:
    """
    Return the indexed document summary shown in the frontend source panel.

    This reads indexed_files.json, which is written by backend/ingest.py after
    documents are chunked and saved into Chroma.
    """
    if not INDEXED_FILES_PATH.exists():
        return []

    with open(INDEXED_FILES_PATH, "r", encoding="utf-8") as file:
        indexed_files = json.load(file)

    sources = []
    for source_id, (file_name, info) in enumerate(indexed_files.items(), start=1):
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
