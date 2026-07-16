import pytest

from app.services.chunking import fixed_size, split_russian_sentences


def test_russian_sentence_splitter_preserves_sentences_and_punctuation():
    text = "Первое правило. Второе правило! API не ломаем? Да."

    assert split_russian_sentences(text) == [
        "Первое правило.",
        "Второе правило!",
        "API не ломаем?",
        "Да.",
    ]


def test_chunking_rejects_overlap_not_smaller_than_chunk_size():
    with pytest.raises(ValueError, match="smaller"):
        fixed_size([], chunk_size=64, chunk_overlap=64)
