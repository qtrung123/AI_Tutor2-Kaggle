import json
import hashlib
from pathlib import Path
from typing import List

from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma

from config import (
    DATA_DIR,
    VECTORSTORE_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
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


def load_indexed_files() -> dict:
    """
    Load the list of files that have already been indexed.

    The indexed_files.json file stores information about indexed documents,
    such as file hash, number of chunks, and file path.

    If the file does not exist, it means no document has been indexed yet,
    so the function returns an empty dictionary.
    """
    if not INDEXED_FILES_PATH.exists():
        return {}

    with open(INDEXED_FILES_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def save_indexed_files(indexed_files: dict):
    """
    Save indexing history to indexed_files.json.

    This function is used after indexing finishes.
    It stores information about indexed files, such as hash, chunks, and path.
    """
    with open(INDEXED_FILES_PATH, "w", encoding="utf-8") as file:
        json.dump(indexed_files, file, indent=4, ensure_ascii=False)


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


def index_files(file_paths: List[Path]):
    """
    Incremental indexing:
    - only index new files
    - skip files that were already indexed
    """
    DATA_DIR.mkdir(exist_ok=True)
    VECTORSTORE_DIR.mkdir(exist_ok=True)

    indexed_files = load_indexed_files()
    vectorstore = get_vectorstore()

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
            doc.metadata["file_hash"] = file_hash

        chunks = split_documents(documents)

        if not chunks:
            print(f"No chunks created for: {file_path.name}")
            continue

        ids = [
            f"{file_hash}_{i}"
            for i in range(len(chunks))
        ]

        vectorstore.add_documents(
            documents=chunks,
            ids=ids,
        )

        indexed_files[file_key] = {
            "hash": file_hash,
            "chunks": len(chunks),
            "path": str(file_path),
        }

        total_new_files += 1
        total_chunks += len(chunks)

        print(f"Indexed: {file_path.name} - {len(chunks)} chunks")

    save_indexed_files(indexed_files)

    return {
        "new_files": total_new_files,
        "new_chunks": total_chunks,
        "skipped_files": skipped_files,
        "total_indexed_files": len(indexed_files),
    }


def delete_indexed_file(file_name: str) -> dict:
    """
    Remove one indexed file from the app.

    This deletes three things that belong together:
    - Chroma vectors created for the file,
    - the metadata entry in indexed_files.json,
    - the original uploaded file in data/ when it exists there.
    """
    indexed_files = load_indexed_files()
    file_key = Path(file_name).name

    if file_key not in indexed_files:
        raise ValueError("Document was not found in indexed files.")

    info = indexed_files[file_key]
    file_hash = info.get("hash")
    chunk_count = int(info.get("chunks", 0))

    if file_hash and chunk_count:
        vectorstore = get_vectorstore()
        ids = [f"{file_hash}_{index}" for index in range(chunk_count)]
        vectorstore.delete(ids=ids)

    source_path = Path(info.get("path") or DATA_DIR / file_key)
    if not source_path.is_absolute():
        source_path = DATA_DIR / source_path.name

    data_root = DATA_DIR.resolve()
    resolved_source = source_path.resolve()
    if resolved_source.exists() and resolved_source.is_file():
        if resolved_source.parent == data_root:
            resolved_source.unlink()

    del indexed_files[file_key]
    save_indexed_files(indexed_files)

    return {
        "deleted": file_key,
        "deleted_chunks": chunk_count,
        "total_indexed_files": len(indexed_files),
    }


def index_all_data_files():
    """Index all supported files in data/ incrementally."""
    files = []

    for pattern in ["*.pdf", "*.txt"]:
        files.extend(DATA_DIR.glob(pattern))

    return index_files(files)


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