import hashlib
from pathlib import Path
from typing import List

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma

from backend.topic_extractor import TOPIC_SCHEMA_VERSION, TopicExtractor, ollama_heading_refiner
from backend.auth_store import LEGACY_USER_ID
from backend.indexed_document_store import (
    delete_indexed_document,
    list_indexed_documents,
    upsert_indexed_document,
)

from config import (
    DATA_DIR,
    VECTORSTORE_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    CHAT_MODEL,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    INDEXED_FILES_PATH,
)


def calculate_file_hash(file_path: Path) -> str:
    """
    Calculate a unique SHA256 hash for a file.

    This hash is used to check whether a file has already been indexed.
    If the file content changes, the hash will also change.
    """
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


def load_indexed_files(owner_id: str = LEGACY_USER_ID) -> dict:
    """
    Load the list of files that have already been indexed.

    The indexed_files.json file stores information about indexed documents,
    such as file hash, number of chunks, and file path.

    If the file does not exist, it means no document has been indexed yet,
    so the function returns an empty dictionary.
    """
    return {document["document_id"]: document for document in list_indexed_documents(owner_id)}


def save_indexed_files(indexed_files: dict, owner_id: str = LEGACY_USER_ID):
    """
    Save indexing history to indexed_files.json.

    This function is used after indexing finishes.
    It stores information about indexed files, such as hash, chunks, and path.
    """
    for document_id, info in indexed_files.items():
        upsert_indexed_document(owner_id, document_id, info)


def load_single_file(file_path: Path):
    """Load one PDF or TXT file."""
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        print(f"Loading PDF: {file_path.name}")
        loader = PyPDFLoader(str(file_path))
        return loader.load()

    if suffix == ".txt":
        print(f"Loading TXT: {file_path.name}")
        loader = TextLoader(str(file_path), encoding="utf-8")
        return loader.load()

    print(f"Skipping unsupported file: {file_path.name}")
    return []


def clean_documents(documents):
    """Remove empty pages/documents."""
    return [doc for doc in documents if doc.page_content and doc.page_content.strip()]


def split_documents(documents):
    """Split documents into chunks."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        add_start_index=True,
    )

    return splitter.split_documents(documents)


def get_vectorstore():
    """Open or create Chroma vectorstore."""
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)

    vectorstore = Chroma(
        persist_directory=str(VECTORSTORE_DIR),
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
    )

    return vectorstore


def migrate_legacy_vector_ownership(vectorstore) -> int:
    """Add legacy ownership in place without changing vector IDs or chunk traceability."""
    result = vectorstore.get(limit=100000)
    ids = result.get("ids", []) or []
    metadatas = result.get("metadatas", []) or []
    update_ids = []
    update_metadatas = []
    for vector_id, metadata in zip(ids, metadatas):
        metadata = dict(metadata or {})
        if metadata.get("owner_id"):
            continue
        metadata["owner_id"] = LEGACY_USER_ID
        metadata["document_id"] = str(metadata.get("document_id") or metadata.get("source") or "")
        update_ids.append(vector_id)
        update_metadatas.append(metadata)
    if update_ids:
        vectorstore._collection.update(ids=update_ids, metadatas=update_metadatas)
    return len(update_ids)


def add_topic_metadata(documents, chunks, ids, extractor: TopicExtractor | None = None) -> list[dict]:
    """Add stable chunk IDs and extracted topic metadata without changing chunk text."""
    extractor = extractor or TopicExtractor(llm_refiner=ollama_heading_refiner(CHAT_MODEL))
    topics, headings = extractor.extract(documents)
    extractor.map_chunks(chunks, documents, topics, headings)
    for chunk, chunk_id in zip(chunks, ids):
        chunk.metadata["chunk_id"] = chunk_id
    return topics


def delete_stale_source_vectors(vectorstore, owner_id: str, document_id: str, current_ids: list[str]) -> list[str]:
    """Delete source vectors not present in the completed replacement index."""
    result = vectorstore.get(where={"$and": [{"owner_id": owner_id}, {"document_id": document_id}]})
    stale_ids = sorted(set(result.get("ids", []) or []) - set(current_ids))
    if stale_ids:
        vectorstore.delete(ids=stale_ids)
    return stale_ids


def index_files(file_paths: List[Path], owner_id: str = LEGACY_USER_ID):
    """
    Incremental indexing:
    - only index new files
    - skip files that were already indexed
    """
    DATA_DIR.mkdir(exist_ok=True)
    VECTORSTORE_DIR.mkdir(exist_ok=True)

    indexed_files = load_indexed_files(owner_id)
    vectorstore = get_vectorstore()
    migrate_legacy_vector_ownership(vectorstore)

    total_new_files = 0
    total_chunks = 0
    skipped_files = []

    # Process each file in the given list
    for file_path in file_paths:
        file_path = Path(file_path)

        if not file_path.exists():
            print(f"File does not exist: {file_path}")
            continue

        file_hash = calculate_file_hash(file_path)
        file_key = file_path.name

        if file_key in indexed_files and indexed_files[file_key]["hash"] == file_hash:
            if indexed_files[file_key].get("topic_schema_version") == TOPIC_SCHEMA_VERSION:
                print(f"Skipping already indexed file: {file_path.name}")
                skipped_files.append(file_path.name)
                continue

        documents = load_single_file(file_path)
        documents = clean_documents(documents)

        if not documents:
            print(f"No extractable text found in: {file_path.name}")
            continue

        for doc in documents:
            doc.metadata["source"] = file_path.name
            doc.metadata["document_id"] = file_path.name
            doc.metadata["owner_id"] = owner_id
            doc.metadata["file_hash"] = file_hash

        chunks = split_documents(documents)

        if not chunks:
            print(f"No chunks created for: {file_path.name}")
            continue

        # Chroma IDs are owner-namespaced for isolation. Provenance IDs remain
        # stable document-chunk identities and are carried in metadata.
        provenance_ids = [f"{file_hash}_{i}" for i in range(len(chunks))]
        vector_ids = [
            provenance_id if owner_id == LEGACY_USER_ID else f"{owner_id}_{provenance_id}"
            for provenance_id in provenance_ids
        ]

        topics = add_topic_metadata(documents, chunks, provenance_ids)

        vectorstore.add_documents(
            documents=chunks,
            ids=vector_ids,
        )
        delete_stale_source_vectors(vectorstore, owner_id, file_key, vector_ids)

        info = {
            "hash": file_hash,
            "chunks": len(chunks),
            "path": str(file_path),
            "topic_schema_version": TOPIC_SCHEMA_VERSION,
            "topics": topics,
        }
        indexed_files[file_key] = info
        upsert_indexed_document(owner_id, file_key, info)

        total_new_files += 1
        total_chunks += len(chunks)

        print(f"Indexed: {file_path.name} - {len(chunks)} chunks")

    return {
        "new_files": total_new_files,
        "new_chunks": total_chunks,
        "skipped_files": skipped_files,
        "total_indexed_files": len(indexed_files),
    }


def delete_indexed_file(file_name: str, owner_id: str = LEGACY_USER_ID) -> dict:
    """
    Remove one indexed file from the app.

    This deletes three things that belong together:
    - Chroma vectors created for the file,
    - the metadata entry in indexed_files.json,
    - the original uploaded file in data/ when it exists there.
    """
    indexed_files = load_indexed_files(owner_id)
    file_key = Path(file_name).name

    if file_key not in indexed_files:
        raise ValueError("Document was not found in indexed files.")

    info = indexed_files[file_key]
    file_hash = info.get("hash")
    chunk_count = int(info.get("chunks", 0))

    if file_hash and chunk_count:
        vectorstore = get_vectorstore()
        ids = [
            f"{file_hash}_{index}" if owner_id == LEGACY_USER_ID else f"{owner_id}_{file_hash}_{index}"
            for index in range(chunk_count)
        ]
        vectorstore.delete(ids=ids)

    source_path = Path(info.get("path") or DATA_DIR / file_key)
    if not source_path.is_absolute():
        source_path = DATA_DIR / source_path.name

    data_root = (DATA_DIR if owner_id == LEGACY_USER_ID else DATA_DIR / "users" / owner_id).resolve()
    resolved_source = source_path.resolve()
    if resolved_source.exists() and resolved_source.is_file():
        if resolved_source.parent == data_root:
            resolved_source.unlink()

    delete_indexed_document(owner_id, file_key)

    return {
        "deleted": file_key,
        "deleted_chunks": chunk_count,
        "total_indexed_files": len(indexed_files),
    }


def index_all_data_files(owner_id: str = LEGACY_USER_ID):
    """Index all supported files in data/ incrementally."""
    files = []

    for pattern in ["*.pdf", "*.txt"]:
        files.extend(DATA_DIR.glob(pattern))

    return index_files(files, owner_id)


def main():
    print("Starting Incremental Indexing Pipeline...")

    result = index_all_data_files()

    print("Indexing completed.")
    print(f"New files indexed: {result['new_files']}")
    print(f"New chunks added: {result['new_chunks']}")
    print(f"Skipped files: {len(result['skipped_files'])}")
    print(f"Total indexed files: {result['total_indexed_files']}")


if __name__ == "__main__":
    main()
