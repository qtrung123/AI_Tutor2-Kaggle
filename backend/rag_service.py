import json
from pathlib import Path

from langchain_chroma import Chroma
from langchain_ollama import ChatOllama, OllamaEmbeddings

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

    The template contains placeholders for {context} and {question}. Those
    placeholders are filled in ask_rag() after relevant chunks are retrieved.
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


def ask_rag(question: str) -> tuple[str, list]:
    """
    Run the full RAG flow for one user question.

    RAG means:
    1. Retrieve: embed the user question and find the most relevant chunks.
    2. Augment: put those chunks into the prompt template as context.
    3. Generate: send the final prompt to the chat model and return its answer.
    """
    vectorstore = load_vectorstore()

    retriever = vectorstore.as_retriever(search_kwargs={"k": TOP_K})

    # Retrieve: find the top-k course chunks most similar to the user question.
    docs = retriever.invoke(question)

    # Augment: turn retrieved chunks into context and inject it into the prompt.
    context = format_docs(docs)
    prompt_template = load_prompt_template()
    final_prompt = prompt_template.format(
        context=context,
        question=question,
    )

    # Generate: send the final prompt to Ollama and read the model response.
    llm = ChatOllama(
        model=CHAT_MODEL,
        temperature=0,
    )
    response = llm.invoke(final_prompt)

    return response.content, docs


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


def answer_question(
    message: str,
    course: str | None = None,
    topic: str | None = None,
) -> dict:
    """
    Public chat service used by FastAPI.

    backend/main.py calls this function for POST /api/chat. The course/topic
    arguments are accepted for the API contract, but the current RAG search uses
    the message text only.
    """
    answer, docs = ask_rag(message)

    return {
        "answer": answer,
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
