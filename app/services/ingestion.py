from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.core.config import Settings
from app.services.chunking import split_russian_sentences
from app.services.embeddings import EmbeddingConfig, LlamaIndexEmbeddingAdapter


logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".html", ".htm", ".md", ".markdown"}
TECHNICAL_METADATA_KEYS = {
    "file_path",
    "file_name",
    "source",
    "created_at",
    "last_modified",
    "author",
    "category",
    "version",
    "page",
    "page_label",
    "total_pages",
    "tag",
    "tag_id",
}
VERSION_RE = re.compile(
    r"(?:^|[-_.])v(?:ersion)?[-_.]?(?P<version>\d+(?:[._-]\d+){0,3})(?:[-_.]|$)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class IngestionResult:
    files_total: int
    changed: int
    unchanged: int
    failed: int
    chunks_written: int
    formats: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class IngestionService:
    """Multi-format reader and persistent LlamaIndex UPSERT pipeline."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ingest_path(
        self,
        input_path: Path | str,
        *,
        rename_failed: bool = True,
        show_progress: bool = False,
    ) -> IngestionResult:
        root = Path(input_path).resolve()
        files = list(iter_supported_files(root))
        documents: list[Any] = []
        file_documents: dict[Path, list[Any]] = {}
        failures: list[str] = []

        for path in files:
            try:
                loaded = self.load_file(path, data_root=_data_root(root, path))
                if not loaded:
                    raise ValueError("reader returned no text")
                documents.extend(loaded)
                file_documents[path] = loaded
            except Exception as exc:
                logger.exception("ingestion.file_failed", extra={"path": str(path)})
                failures.append(f"{path}: {exc}")
                if rename_failed and not path.name.endswith(".failed"):
                    path.rename(failed_file_target(path))

        pipeline, docstore = self.build_pipeline()
        changed_files, unchanged_files = _classify_files(file_documents, docstore)
        nodes = pipeline.run(documents=documents, show_progress=show_progress)
        storage_dir = self.settings.rag_pipeline_storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        pipeline.persist(persist_dir=str(storage_dir))

        formats = Counter(path.suffix.lower().lstrip(".") for path in files)
        result = IngestionResult(
            files_total=len(files),
            changed=changed_files,
            unchanged=unchanged_files,
            failed=len(failures),
            chunks_written=len(nodes),
            formats=dict(sorted(formats.items())),
            failures=failures,
        )
        logger.info(
            "ingestion.completed",
            extra={
                "files_total": result.files_total,
                "changed": result.changed,
                "unchanged": result.unchanged,
                "failed": result.failed,
                "chunks_written": result.chunks_written,
            },
        )
        return result

    def load_file(self, path: Path, *, data_root: Path | None = None) -> list[Any]:
        from llama_index.readers.file import (
            DocxReader,
            HTMLTagReader,
            MarkdownReader,
            PyMuPDFReader,
        )

        suffix = path.suffix.lower()
        metadata = build_file_metadata(path, data_root=data_root)
        if suffix == ".pdf":
            documents = PyMuPDFReader().load_data(path, extra_info=metadata)
        elif suffix == ".docx":
            documents = DocxReader().load_data(path, extra_info=metadata)
        elif suffix in {".html", ".htm"}:
            # body works for ordinary pages and Confluence HTML exports.
            documents = HTMLTagReader(tag="body").load_data(path, extra_info=metadata)
        elif suffix in {".md", ".markdown"}:
            documents = MarkdownReader().load_data(str(path), extra_info=metadata)
        else:
            raise ValueError(f"Unsupported document format: {suffix}")

        cleaned: list[Any] = []
        for index, document in enumerate(documents):
            text = document.get_content().strip()
            if not text:
                continue
            document.metadata.update(metadata)
            page = _page_number(document.metadata, index)
            document.metadata["page"] = page
            document.id_ = stable_document_id(path, index)
            document.excluded_embed_metadata_keys = sorted(TECHNICAL_METADATA_KEYS)
            document.excluded_llm_metadata_keys = [
                "file_path",
                "created_at",
                "last_modified",
                "total_pages",
                "tag",
                "tag_id",
            ]
            cleaned.append(document)
        return cleaned

    def build_pipeline(self) -> tuple[Any, Any]:
        from llama_index.core.ingestion import DocstoreStrategy, IngestionPipeline
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.core.storage.docstore import SimpleDocumentStore
        from llama_index.vector_stores.qdrant import QdrantVectorStore
        from qdrant_client import QdrantClient

        storage_dir = self.settings.rag_pipeline_storage_dir
        docstore_path = storage_dir / "docstore.json"
        if docstore_path.exists():
            docstore = SimpleDocumentStore.from_persist_path(str(docstore_path))
        else:
            docstore = SimpleDocumentStore()

        client = QdrantClient(
            url=self.settings.qdrant_url,
            api_key=self.settings.qdrant_api_key,
        )
        vector_store = QdrantVectorStore(
            client=client,
            collection_name=self.settings.rag_collection,
        )
        pipeline = IngestionPipeline(
            transformations=[
                SentenceSplitter(
                    chunk_size=self.settings.rag_chunk_size,
                    chunk_overlap=self.settings.rag_chunk_overlap,
                    paragraph_separator="\n\n",
                    chunking_tokenizer_fn=split_russian_sentences,
                ),
                LlamaIndexEmbeddingAdapter(
                    EmbeddingConfig.from_settings(self.settings)
                ),
            ],
            docstore=docstore,
            vector_store=vector_store,
            docstore_strategy=DocstoreStrategy.UPSERTS,
        )
        return pipeline, docstore


def iter_supported_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path
        return
    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")
    for candidate in sorted(path.rglob("*")):
        if (
            candidate.is_file()
            and candidate.suffix.lower() in SUPPORTED_SUFFIXES
            and not candidate.name.endswith(".failed")
        ):
            yield candidate


def build_file_metadata(path: Path, *, data_root: Path | None = None) -> dict[str, Any]:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    metadata: dict[str, Any] = {
        "source": path.name,
        "file_name": path.name,
        "file_path": str(path.resolve()),
        "last_modified": modified,
        "created_at": datetime.fromtimestamp(stat.st_ctime, tz=UTC).isoformat(),
        "category": _category(path, data_root),
    }
    version = extract_version(path.name)
    if version is not None:
        metadata["version"] = version
    author = docx_author(path) if path.suffix.lower() == ".docx" else None
    if author:
        metadata["author"] = author
    return metadata


def docx_author(path: Path) -> str | None:
    try:
        from docx import Document as DocxDocument

        author = DocxDocument(path).core_properties.author
    except Exception:
        return None
    return author.strip() if author and author.strip() else None


def extract_version(file_name: str) -> str | None:
    match = VERSION_RE.search(Path(file_name).stem)
    if match is None:
        return None
    return match.group("version").replace("_", ".").replace("-", ".")


def stable_document_id(path: Path, part: int) -> str:
    identity = f"{path.resolve()}#{part}".encode()
    return hashlib.sha256(identity).hexdigest()


def failed_file_target(path: Path) -> Path:
    """Return an unused target whose final suffix is always ``.failed``."""
    candidate = path.with_name(f"{path.name}.failed")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem}-{counter}{path.suffix}.failed"
        )
        counter += 1
    return candidate


def _data_root(root: Path, path: Path) -> Path:
    if root.is_dir():
        return root
    for parent in path.parents:
        if parent.name == "data":
            return parent
    return path.parent


def _category(path: Path, data_root: Path | None) -> str:
    if data_root is not None:
        try:
            relative = path.resolve().relative_to(data_root.resolve())
            if len(relative.parts) > 1:
                return relative.parts[0]
        except ValueError:
            pass
    return path.parent.name


def _page_number(metadata: dict[str, Any], fallback_index: int) -> int | None:
    for key in ("page", "page_label", "source"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value + 1 if value == fallback_index else value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return fallback_index + 1


def _classify_files(file_documents: dict[Path, Sequence[Any]], docstore: Any) -> tuple[int, int]:
    changed = 0
    unchanged = 0
    for documents in file_documents.values():
        is_changed = any(
            docstore.get_document_hash(document.id_) != document.hash
            for document in documents
        )
        if is_changed:
            changed += 1
        else:
            unchanged += 1
    return changed, unchanged
