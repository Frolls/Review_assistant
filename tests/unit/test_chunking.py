import pytest

from app.services.chunking import fixed_size, split_russian_sentences


def test_russian_sentence_splitter_preserves_sentences_and_punctuation():
    text = "Первое правило. Второе правило! API не ломаем? Да."

    assert split_russian_sentences(text) == [
        "Первое правило. ",
        "Второе правило! ",
        "API не ломаем? ",
        "Да.",
    ]


def test_sentence_splitter_preserves_spaces_and_paragraph_breaks():
    text = 'Первое правило.  Второе правило!\n\n"Third sentence."'
    assert "".join(split_russian_sentences(text)) == text


def test_recursive_chunks_do_not_glue_adjacent_sentences():
    from llama_index.core import Document
    from app.services.chunking import recursive

    text = "Первое правило описывает функцию. Второе правило описывает список. " * 30
    chunks = recursive([Document(text=text)], chunk_size=100, chunk_overlap=0)
    assert len(chunks) > 1
    assert any(". Второе" in chunk.text for chunk in chunks)
    assert all(".Второе" not in chunk.text and ".Первое" not in chunk.text for chunk in chunks)


def test_chunking_rejects_overlap_not_smaller_than_chunk_size():
    with pytest.raises(ValueError, match="smaller"):
        fixed_size([], chunk_size=64, chunk_overlap=64)
