#!/usr/bin/env python3
"""Generate the raw RAGAS testset that must be manually curated afterwards."""

from __future__ import annotations

import argparse
import os
from math import isnan
from pathlib import Path

import pandas as pd
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas.run_config import RunConfig
from ragas.testset import TestsetGenerator


def main() -> None:
    args = parse_args()
    api_key = os.getenv("OPENAI_API_KEY", "ollama")
    base_url = os.getenv("OPENAI_BASE_URL", "http://host.docker.internal:11434/v1")
    llm = ChatOpenAI(
        model=args.model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        timeout=args.timeout,
        extra_body={"think": False},
    )
    embeddings = OpenAIEmbeddings(
        model=args.embedding_model,
        api_key=api_key,
        base_url=base_url,
        check_embedding_ctx_length=False,
    )
    documents = []
    for path in sorted(args.data.rglob("*.md")):
        if not path.is_file():
            continue
        file_documents = []
        for paragraph_index, paragraph in enumerate(
            path.read_text(encoding="utf-8").split("\n\n"),
            start=1,
        ):
            paragraph = paragraph.strip()
            if len(paragraph) < 120 or paragraph.startswith(("Источник:", "Источники:")):
                continue
            file_documents.append(
                Document(
                    page_content=paragraph,
                    metadata={
                        "file_name": path.name,
                        "source": str(path),
                        "paragraph": paragraph_index,
                    },
                )
            )
        documents.extend(file_documents[: args.chunks_per_file])
    if not documents:
        raise RuntimeError(f"no documents found under {args.data}")

    generator = TestsetGenerator.from_langchain(
        llm=llm,
        embedding_model=embeddings,
        llm_context=(
            "Вопросы и reference должны быть на русском языке и описывать "
            "конкретную ситуацию code review, а не просить общее определение."
        ),
    )
    # RAGAS 0.4.3 turns a failed sample into float("nan") when
    # raise_exceptions=False and then crashes while constructing Testset.
    # Taking the documented executor path lets us retain all valid generated
    # samples and write the raw audit artifact.
    executor = generator.generate_with_chunks(
        documents,
        testset_size=args.size,
        run_config=RunConfig(
            timeout=args.timeout,
            max_retries=2,
            max_workers=args.workers,
        ),
        raise_exceptions=False,
        return_executor=True,
    )
    generated = executor.results()
    rows = [
        sample.model_dump(mode="json")
        for sample in generated
        if hasattr(sample, "model_dump")
        and not (isinstance(sample, float) and isnan(sample))
    ]
    if len(rows) < 30:
        raise RuntimeError(
            f"TestsetGenerator returned only {len(rows)} valid samples; "
            "rerun with a larger --size or a stronger model"
        )
    dataframe = pd.DataFrame(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_csv(args.output, index=False)
    print(f"Wrote {len(dataframe)} raw rows to {args.output}")
    print("Next step is mandatory: remove duplicates/bad questions and correct references manually.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/retrieval-corpus"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/eval/golden_dataset_raw.csv"),
    )
    parser.add_argument("--size", type=int, default=40)
    parser.add_argument(
        "--model",
        default=os.getenv("RAG_TESTSET_MODEL", "qwen3:1.7b"),
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("EMBEDDING_MODEL", "qwen3-embedding:4b"),
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--chunks-per-file", type=int, default=4)
    args = parser.parse_args()
    if args.size < 30:
        parser.error("--size must be at least 30")
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.chunks_per_file < 1:
        parser.error("--chunks-per-file must be positive")
    return args


if __name__ == "__main__":
    main()
