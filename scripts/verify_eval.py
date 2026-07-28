#!/usr/bin/env python3
"""Fail fast when the optional eval/tracing dependency set is inconsistent."""

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from pandas import DataFrame
from phoenix.otel import register
from ragas.embeddings import OpenAIEmbeddings
from ragas.llms import llm_factory
from ragas.metrics import discrete_metric
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)
from ragas.testset import TestsetGenerator


def main() -> None:
    symbols = (
        AsyncAnthropic,
        AsyncOpenAI,
        LlamaIndexInstrumentor,
        OTLPSpanExporter,
        TracerProvider,
        DataFrame,
        register,
        OpenAIEmbeddings,
        llm_factory,
        discrete_metric,
        Faithfulness,
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        TestsetGenerator,
    )
    print(f"eval/tracing imports: ok ({len(symbols)} symbols)")


if __name__ == "__main__":
    main()
