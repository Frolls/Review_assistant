from __future__ import annotations

from pathlib import Path

from app.services.ingestion import (
    TECHNICAL_METADATA_KEYS,
    IngestionService,
    build_file_metadata,
    extract_version,
    failed_file_target,
    iter_supported_files,
)


def test_build_file_metadata_enriches_category_version_and_dates(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    path = data_root / "python" / "typing-guide-v2.1.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Typing\n\nPublic functions need type hints.", encoding="utf-8")

    metadata = build_file_metadata(path, data_root=data_root)

    assert metadata["source"] == "typing-guide-v2.1.md"
    assert metadata["category"] == "python"
    assert metadata["version"] == "2.1"
    assert metadata["created_at"]
    assert metadata["last_modified"]


def test_markdown_reader_sets_stable_id_page_and_metadata_exclusions(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    path = data_root / "python" / "guide-v1.0.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Guide\n\nUse type hints.", encoding="utf-8")
    service = object.__new__(IngestionService)

    first = service.load_file(path, data_root=data_root)
    second = service.load_file(path, data_root=data_root)

    assert first
    assert [document.id_ for document in first] == [document.id_ for document in second]
    assert first[0].metadata["category"] == "python"
    assert first[0].metadata["page"] == 1
    assert set(TECHNICAL_METADATA_KEYS) <= set(first[0].excluded_embed_metadata_keys)


def test_supported_file_scan_skips_json_and_failed_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "b.html").write_text("<body>b</body>", encoding="utf-8")
    (tmp_path / "legacy.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broken.pdf.failed").write_text("x", encoding="utf-8")

    assert [path.name for path in iter_supported_files(tmp_path)] == ["a.md", "b.html"]


def test_extract_version_returns_none_for_unversioned_file() -> None:
    assert extract_version("policy.md") is None


def test_failed_file_target_remains_unique_and_ends_with_failed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "broken.pdf"
    source.write_text("invalid", encoding="utf-8")
    (tmp_path / "broken.pdf.failed").write_text("first failure", encoding="utf-8")

    target = failed_file_target(source)

    assert target.name == "broken-1.pdf.failed"


def test_all_four_supported_readers_extract_text_and_docx_author(tmp_path: Path) -> None:
    import pymupdf
    from docx import Document as DocxDocument

    data_root = tmp_path / "data"
    category = data_root / "policies"
    category.mkdir(parents=True)

    markdown = category / "guide.md"
    markdown.write_text("# Guide\n\nMarkdown policy.", encoding="utf-8")
    html = category / "page.html"
    html.write_text("<html><body><h1>HTML policy</h1></body></html>", encoding="utf-8")

    docx_path = category / "memo-v3.docx"
    docx = DocxDocument()
    docx.core_properties.author = "Policy Team"
    docx.add_paragraph("DOCX policy")
    docx.save(docx_path)

    pdf_path = category / "regulation.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "PDF policy")
    pdf.save(pdf_path)
    pdf.close()

    service = object.__new__(IngestionService)
    loaded = {
        path.suffix: service.load_file(path, data_root=data_root)
        for path in (markdown, html, docx_path, pdf_path)
    }

    assert all(documents for documents in loaded.values())
    assert loaded[".docx"][0].metadata["author"] == "Policy Team"
    assert loaded[".pdf"][0].metadata["page"] == 1
