from llama_index.core import Document

from app.services.ingestion import IngestionService
from app.services.rule_chunking import RuleSentenceSplitter


def chunks(text, *, profile=True, **kwargs):
    document = Document(
        text=text, id_="guide",
        metadata={"chunking_profile": "rule_paragraphs"} if profile else {},
        excluded_embed_metadata_keys=["chunking_profile"],
        excluded_llm_metadata_keys=["chunking_profile"],
    )
    return RuleSentenceSplitter(**kwargs).get_nodes_from_documents([document])


def test_independent_rules_have_no_cross_paragraph_overlap():
    nodes = chunks(
        "Bare except hides defects. Catch specific exceptions.\n\n"
        "Mutable default arguments share state. Use None and create a list.",
        chunk_size=256, chunk_overlap=32,
    )
    assert len(nodes) == 2
    assert "except" in nodes[0].text and "Mutable" not in nodes[0].text
    assert "Mutable" in nodes[1].text and "except" not in nodes[1].text
    assert all(node.ref_doc_id == "guide" for node in nodes)


def test_regular_documents_keep_existing_paragraph_context():
    nodes = chunks("First paragraph.\n\nRelated explanation.", profile=False)
    assert len(nodes) == 1
    assert "Related explanation" in nodes[0].text


def test_code_examples_and_intro_are_not_split_at_blank_lines():
    text = "Example:\n\n```python\ndef f():\n\n    return []\n```\n\nExplanation."
    assert chunks(text)[0].text == text
    assert len(chunks(text)) == 1


def test_heading_and_list_keep_their_context():
    nodes = chunks("# Rule\n\nAllowed values:\n\n- one\n- two\n\nAnother rule.")
    assert len(nodes) == 2
    assert "# Rule" in nodes[0].text and "- two" in nodes[0].text
    assert nodes[1].text == "Another rule."


def test_title_and_source_do_not_become_an_empty_search_result():
    nodes = chunks("# Guide\n\nИсточник: https://example.com\n\nFirst rule.\n\nSecond rule.")
    assert len(nodes) == 2
    assert "First rule." in nodes[0].text
    assert nodes[1].text == "Second rule."


def test_long_rule_still_obeys_chunk_size():
    nodes = chunks("A detailed rule. " * 200 + "\n\nA separate topic.",
                   chunk_size=64, chunk_overlap=8)
    assert len(nodes) > 2
    assert nodes[-1].text == "A separate topic."
    assert all("separate" not in node.text for node in nodes[:-1])


def test_ingestion_marks_only_curated_markdown(tmp_path):
    service = object.__new__(IngestionService)
    for category in ("retrieval-corpus", "rag-block-03", "uploads"):
        path = tmp_path / category / "guide.md"
        path.parent.mkdir()
        path.write_text("# Guide\n\nFirst rule.\n\nSecond rule.")
        document = service.load_file(path, data_root=tmp_path)[0]
        assert (document.metadata.get("chunking_profile") == "rule_paragraphs") == (
            category != "uploads"
        )
        assert "chunking_profile" in document.excluded_embed_metadata_keys
        assert "chunking_profile" in document.excluded_llm_metadata_keys
        if category != "uploads":
            nodes = RuleSentenceSplitter().get_nodes_from_documents([document])
            assert len(nodes) == 2
            assert "First rule." in nodes[0].text
            assert nodes[1].text == "Second rule."
