from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.core.config import Settings, get_settings
from app.services.chunking import fixed_size, recursive, semantic
from app.services.embeddings import EmbeddingConfig, embed_documents, embed_query
from app.services.reranker import BGEReranker
from app.services.retrieval_eval import (
    RetrievalCase,
    candidate_doc_id,
    load_retrieval_dataset,
    retrieval_metrics,
)


COLLECTIONS = {
    "fixed": "docs_fixed",
    "recursive": "docs_recursive",
    "semantic": "docs_semantic",
}
TUNING_GRID = (
    (256, 32, 10),
    (256, 32, 20),
    (512, 64, 10),
    (512, 64, 20),
)


@dataclass(frozen=True)
class ChunkStats:
    total_chunks: int
    mean_chunks_per_document: float
    mean_chunk_tokens: float


@dataclass(frozen=True)
class CorpusStats:
    total_documents: int
    total_bytes: int
    total_tokens: int
    min_document_tokens: int
    mean_document_tokens: float
    max_document_tokens: int


@dataclass(frozen=True)
class EvaluationResult:
    strategy: str
    hit_rate_at_5: float
    mrr_at_10: float
    recall_at_10: float
    mean_chunk_tokens: float
    mean_retrieval_ms: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index and evaluate fixed, recursive and semantic RAG chunking."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/eval/retrieval_dataset.json"),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/retrieval-corpus"),
        help="Python/Ansible evaluation corpus JSON or Markdown directory.",
    )
    parser.add_argument(
        "--qdrant-path",
        type=Path,
        help="Use embedded Qdrant at this path instead of QDRANT_URL.",
    )
    parser.add_argument("--skip-reranker", action="store_true")
    parser.add_argument("--skip-tuning", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional machine-readable report path; stdout is always populated.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    report = run_experiment(
        settings,
        input_path=args.input,
        dataset_path=args.dataset,
        qdrant_path=args.qdrant_path,
        with_reranker=not args.skip_reranker,
        with_tuning=not args.skip_tuning,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(f"{rendered}\n", encoding="utf-8")


def run_experiment(
    settings: Settings,
    *,
    input_path: Path,
    dataset_path: Path,
    qdrant_path: Path | None,
    with_reranker: bool,
    with_tuning: bool,
) -> dict[str, Any]:
    from qdrant_client import QdrantClient

    dataset = load_retrieval_dataset(dataset_path)
    documents = load_documents(input_path)
    corpus = corpus_stats(documents)
    embedding_config = EmbeddingConfig.from_settings(settings)
    client = (
        QdrantClient(path=str(qdrant_path))
        if qdrant_path is not None
        else QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    )
    query_vectors = {
        case.question: embed_query(case.question, config=embedding_config) for case in dataset
    }

    stats_by_strategy: dict[str, ChunkStats] = {}
    results: list[EvaluationResult] = []
    try:
        semantic_embed_model = project_llama_embedding(embedding_config)
        for strategy in ("fixed", "recursive", "semantic"):
            nodes = make_nodes(
                strategy,
                documents,
                embed_model=semantic_embed_model,
                chunk_size=512,
                chunk_overlap=64,
            )
            stats = chunk_stats(nodes, document_count=len(documents))
            stats_by_strategy[strategy] = stats
            index_nodes(
                client,
                collection_name=COLLECTIONS[strategy],
                strategy=strategy,
                nodes=nodes,
                embedding_config=embedding_config,
                expected_dimension=settings.embedding_dim,
            )
            result, _ = evaluate_collection(
                client,
                collection_name=COLLECTIONS[strategy],
                strategy=strategy,
                dataset=dataset,
                query_vectors=query_vectors,
                top_k=10,
                mean_chunk_tokens=stats.mean_chunk_tokens,
            )
            results.append(result)

        best_strategy = choose_best_strategy(results)
        best_stats = stats_by_strategy[best_strategy]
        best_baseline, baseline_rankings = evaluate_collection(
            client,
            collection_name=COLLECTIONS[best_strategy],
            strategy=f"{best_strategy}_without_reranker",
            dataset=dataset,
            query_vectors=query_vectors,
            top_k=max(20, settings.rag_reranker_top_n),
            mean_chunk_tokens=best_stats.mean_chunk_tokens,
        )
        comparison_rows = [best_baseline]

        reranker_error: str | None = None
        if with_reranker:
            try:
                reranker = BGEReranker(settings.rag_reranker_model)
                reranked = evaluate_reranked(
                    dataset,
                    baseline_rankings,
                    reranker=reranker,
                    top_n=settings.rag_reranker_top_n,
                    strategy=f"{best_strategy}_with_reranker",
                    mean_chunk_tokens=best_stats.mean_chunk_tokens,
                    base_retrieval_ms=best_baseline.mean_retrieval_ms,
                )
                comparison_rows.append(reranked)
            except Exception as exc:
                reranker_error = f"{type(exc).__name__}: {exc}"

        tuning: list[dict[str, Any]] = []
        if with_tuning:
            for tuning_strategy in ("fixed", "recursive"):
                for chunk_size, overlap, top_k in TUNING_GRID:
                    nodes = make_nodes(
                        tuning_strategy,
                        documents,
                        embed_model=semantic_embed_model,
                        chunk_size=chunk_size,
                        chunk_overlap=overlap,
                    )
                    stats = chunk_stats(nodes, document_count=len(documents))
                    collection_name = "docs_tuning"
                    index_nodes(
                        client,
                        collection_name=collection_name,
                        strategy=tuning_strategy,
                        nodes=nodes,
                        embedding_config=embedding_config,
                        expected_dimension=settings.embedding_dim,
                    )
                    result, _ = evaluate_collection(
                        client,
                        collection_name=collection_name,
                        strategy=tuning_strategy,
                        dataset=dataset,
                        query_vectors=query_vectors,
                        top_k=top_k,
                        mean_chunk_tokens=stats.mean_chunk_tokens,
                    )
                    tuning.append(
                        {
                            "chunk_size": chunk_size,
                            "overlap": overlap,
                            "top_k": top_k,
                            **asdict(result),
                            **asdict(stats),
                        }
                    )
            if client.collection_exists("docs_tuning"):
                client.delete_collection("docs_tuning")

        return {
            "dataset_size": len(dataset),
            "document_count": len(documents),
            "corpus_stats": asdict(corpus),
            "collections": COLLECTIONS,
            "chunk_stats": {
                strategy: asdict(stats) for strategy, stats in stats_by_strategy.items()
            },
            "strategies": [asdict(result) for result in results],
            "best_strategy": best_strategy,
            "reranker_comparison": [asdict(result) for result in comparison_rows],
            "reranker_error": reranker_error,
            "tuning": tuning,
        }
    finally:
        client.close()


def load_documents(input_path: Path) -> list[Any]:
    try:
        from llama_index.core import Document
    except ImportError as exc:
        raise RuntimeError("Install llama-index to run the chunking experiment") from exc

    if not input_path.exists():
        raise FileNotFoundError(f"RAG input does not exist: {input_path}")

    if input_path.is_file():
        raw = json.loads(input_path.read_text(encoding="utf-8"))
        raw_documents = raw.get("documents") if isinstance(raw, dict) else raw
        if not isinstance(raw_documents, list):
            raise ValueError(f"{input_path} must contain a documents array")
        documents = []
        for index, item in enumerate(raw_documents):
            if not isinstance(item, dict):
                raise ValueError(f"Document #{index} in {input_path} must be an object")
            source = item.get("source")
            chunks = item.get("chunks")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"Document #{index} in {input_path} has no source")
            if not isinstance(chunks, list) or not all(
                isinstance(chunk, str) and chunk.strip() for chunk in chunks
            ):
                raise ValueError(f"Document {source} has invalid chunks")
            metadata = {
                key: value
                for key, value in item.items()
                if key != "chunks" and isinstance(value, (str, int, float, bool))
            }
            metadata["source"] = source.strip()
            metadata["file_name"] = source.strip()
            documents.append(
                Document(
                    text="\n\n".join(str(chunk).strip() for chunk in chunks),
                    metadata=metadata,
                    excluded_embed_metadata_keys=list(metadata),
                    excluded_llm_metadata_keys=list(metadata),
                )
            )
        return documents

    paths = sorted(input_path.glob("*.md"))
    if not paths:
        raise RuntimeError(f"No Markdown documents found in {input_path}")
    return [
        Document(
            text=path.read_text(encoding="utf-8"),
            metadata={"source": path.name, "file_name": path.name, "archived": False},
            excluded_embed_metadata_keys=["source", "file_name", "archived"],
            excluded_llm_metadata_keys=["source", "file_name", "archived"],
        )
        for path in paths
    ]


def make_nodes(
    strategy: str,
    documents: Sequence[Any],
    *,
    embed_model: Any,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Any]:
    if strategy == "fixed":
        return fixed_size(
            documents,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    if strategy == "recursive":
        return recursive(
            documents,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    if strategy == "semantic":
        return semantic(documents, embed_model=embed_model)
    raise ValueError(f"Unknown chunking strategy: {strategy}")


def node_text(node: Any) -> str:
    try:
        from llama_index.core.schema import MetadataMode

        return str(node.get_content(metadata_mode=MetadataMode.NONE)).strip()
    except (ImportError, AttributeError):
        return str(getattr(node, "text", "")).strip()


def chunk_stats(nodes: Sequence[Any], *, document_count: int) -> ChunkStats:
    if not nodes or document_count <= 0:
        raise ValueError("Chunk statistics require nodes and documents")
    tokenizer = llama_tokenizer()
    token_lengths = [len(tokenizer(node_text(node))) for node in nodes]
    source_counts = Counter(node_source(node) for node in nodes)
    if len(source_counts) > document_count:
        raise ValueError("Chunk metadata contains more sources than input documents")
    return ChunkStats(
        total_chunks=len(nodes),
        mean_chunks_per_document=len(nodes) / document_count,
        mean_chunk_tokens=statistics.fmean(token_lengths),
    )


def corpus_stats(documents: Sequence[Any]) -> CorpusStats:
    if not documents:
        raise ValueError("Corpus statistics require documents")
    texts = [node_text(document) for document in documents]
    if any(not text for text in texts):
        raise ValueError("Corpus documents must not be empty")
    tokenizer = llama_tokenizer()
    token_lengths = [len(tokenizer(text)) for text in texts]
    return CorpusStats(
        total_documents=len(texts),
        total_bytes=sum(len(text.encode("utf-8")) for text in texts),
        total_tokens=sum(token_lengths),
        min_document_tokens=min(token_lengths),
        mean_document_tokens=statistics.fmean(token_lengths),
        max_document_tokens=max(token_lengths),
    )


def llama_tokenizer() -> Callable[[str], list[int]]:
    from llama_index.core.utils import get_tokenizer

    return get_tokenizer()


def node_source(node: Any) -> str:
    metadata = getattr(node, "metadata", {}) or {}
    for key in ("source", "file_name", "file_path"):
        value = metadata.get(key)
        if value:
            return Path(str(value)).name
    raise ValueError("Chunk node has no source metadata")


def index_nodes(
    client: Any,
    *,
    collection_name: str,
    strategy: str,
    nodes: Sequence[Any],
    embedding_config: EmbeddingConfig,
    expected_dimension: int,
) -> None:
    from qdrant_client.http.models import Distance, PointStruct, VectorParams

    texts = [node_text(node) for node in nodes]
    vectors = embed_documents(texts, config=embedding_config)
    dimensions = {len(vector) for vector in vectors}
    if dimensions != {expected_dimension}:
        raise ValueError(
            f"Embedding dimensions {sorted(dimensions)} do not match {expected_dimension}"
        )

    if client.collection_exists(collection_name):
        client.delete_collection(collection_name)
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=expected_dimension, distance=Distance.COSINE),
    )

    points = []
    source_indexes: Counter[str] = Counter()
    for node, text, vector in zip(nodes, texts, vectors, strict=True):
        source = node_source(node)
        chunk_index = source_indexes[source]
        source_indexes[source] += 1
        point_id = str(uuid5(NAMESPACE_URL, f"{strategy}:{source}:{chunk_index}:{text}"))
        node_metadata = dict(getattr(node, "metadata", {}) or {})
        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": text,
                    "source": source,
                    "file_name": source,
                    "chunk_index": chunk_index,
                    "strategy": strategy,
                    "archived": bool(node_metadata.get("archived", False)),
                    "access_level": str(node_metadata.get("access_level", "internal")),
                    "tenant_id": str(node_metadata.get("tenant_id", "core")),
                },
            )
        )
    for start in range(0, len(points), 128):
        client.upsert(
            collection_name=collection_name,
            points=points[start : start + 128],
            wait=True,
        )


def evaluate_collection(
    client: Any,
    *,
    collection_name: str,
    strategy: str,
    dataset: Sequence[RetrievalCase],
    query_vectors: dict[str, list[float]],
    top_k: int,
    mean_chunk_tokens: float,
) -> tuple[EvaluationResult, list[list[Any]]]:
    from qdrant_client.http.models import FieldCondition, Filter, MatchValue

    retrieval_filter = Filter(
        must_not=[
            FieldCondition(key="archived", match=MatchValue(value=True)),
            FieldCondition(key="access_level", match=MatchValue(value="restricted")),
        ]
    )
    rankings: list[list[Any]] = []
    latencies_ms: list[float] = []
    for case in dataset:
        query_vector = query_vectors[case.question]
        started = time.perf_counter()
        response = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=retrieval_filter,
            limit=top_k,
        )
        rankings.append(list(response.points))
        latencies_ms.append((time.perf_counter() - started) * 1000)

    metrics = retrieval_metrics(
        [[candidate_doc_id(point) for point in ranking] for ranking in rankings],
        [case.relevant_doc_ids for case in dataset],
    )
    return (
        EvaluationResult(
            strategy=strategy,
            hit_rate_at_5=metrics["hit_rate@5"],
            mrr_at_10=metrics["mrr@10"],
            recall_at_10=metrics["recall@10"],
            mean_chunk_tokens=mean_chunk_tokens,
            mean_retrieval_ms=statistics.fmean(latencies_ms),
        ),
        rankings,
    )


def evaluate_reranked(
    dataset: Sequence[RetrievalCase],
    rankings: Sequence[Sequence[Any]],
    *,
    reranker: BGEReranker,
    top_n: int,
    strategy: str,
    mean_chunk_tokens: float,
    base_retrieval_ms: float,
) -> EvaluationResult:
    reranked_ids: list[list[str]] = []
    latencies_ms: list[float] = []
    for case, candidates in zip(dataset, rankings, strict=True):
        started = time.perf_counter()
        reranked = reranker.rerank(case.question, candidates, top_n=top_n)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        reranked_ids.append([candidate_doc_id(candidate) for candidate in reranked])
    metrics = retrieval_metrics(
        reranked_ids,
        [case.relevant_doc_ids for case in dataset],
    )
    return EvaluationResult(
        strategy=strategy,
        hit_rate_at_5=metrics["hit_rate@5"],
        mrr_at_10=metrics["mrr@10"],
        recall_at_10=metrics["recall@10"],
        mean_chunk_tokens=mean_chunk_tokens,
        mean_retrieval_ms=base_retrieval_ms + statistics.fmean(latencies_ms),
    )


def choose_best_strategy(results: Sequence[EvaluationResult]) -> str:
    tie_breaker = {"recursive": 2, "fixed": 1, "semantic": 0}
    winner = max(
        results,
        key=lambda item: (
            item.hit_rate_at_5,
            item.mrr_at_10,
            item.recall_at_10,
            tie_breaker[item.strategy],
            -item.mean_retrieval_ms,
        ),
    )
    return winner.strategy


def project_llama_embedding(config: EmbeddingConfig) -> Any:
    from llama_index.core.embeddings import BaseEmbedding
    from pydantic import PrivateAttr

    class ProjectEmbedding(BaseEmbedding):
        _embedding_config: EmbeddingConfig = PrivateAttr()

        def __init__(self) -> None:
            super().__init__(model_name=config.model)
            self._embedding_config = config

        @classmethod
        def class_name(cls) -> str:
            return "project_embedding"

        def _get_query_embedding(self, query: str) -> list[float]:
            return embed_query(query, config=self._embedding_config)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._get_query_embedding(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return embed_documents([text], config=self._embedding_config)[0]

        def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
            return embed_documents(texts, config=self._embedding_config)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._get_text_embedding(text)

    return ProjectEmbedding()


if __name__ == "__main__":
    main()
