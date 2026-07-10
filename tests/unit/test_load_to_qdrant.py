from __future__ import annotations

import json

import pytest

from scripts.load_to_qdrant import load_chunks


def test_load_chunks_reads_structured_json(tmp_path):
    data = {
        "documents": [
            {
                "source": "pep8_style_guide.md",
                "title": "PEP 8 Style Guide",
                "category": "python",
                "department": "engineering",
                "tenant_id": "core",
                "access_level": "public",
                "archived": False,
                "created_at": "2026-05-20",
                "chunks": [
                    "Код в PR должен быть читаемым без пояснений автора.",
                    "Исключения должны быть конкретными.",
                ],
            }
        ]
    }
    (tmp_path / "review_knowledge.json").write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )

    chunks = load_chunks(tmp_path)

    assert len(chunks) == 2
    assert chunks[0].payload["source"] == "pep8_style_guide.md"
    assert chunks[0].payload["title"] == "PEP 8 Style Guide"
    assert chunks[0].payload["archived"] == "false"
    assert chunks[0].payload["created_at"] == "2026-05-20T00:00:00Z"
    assert chunks[0].payload["document_id"] == "pep8_style_guide.md#0"
    assert chunks[1].payload["chunk_index"] == 1


def test_load_chunks_rejects_invalid_json_schema(tmp_path):
    data = {
        "documents": [
            {
                "source": "broken.md",
                "category": "python",
                "department": "engineering",
                "tenant_id": "core",
                "access_level": "public",
                "archived": False,
                "created_at": "2026-05-20",
            }
        ]
    }
    (tmp_path / "review_knowledge.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing fields: chunks"):
        load_chunks(tmp_path)
