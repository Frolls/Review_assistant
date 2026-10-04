from io import BytesIO

import pytest
from fastapi import UploadFile

from app.chat.media import media_to_part, _checked_text


@pytest.mark.asyncio
async def test_python_file_is_read_as_data_without_execution():
    text = "raise RuntimeError('must not execute')"
    result = await media_to_part(UploadFile(filename="example.py", file=BytesIO(text.encode())))
    assert text in result["text"]
    assert result["type"] == "text"


@pytest.mark.asyncio
async def test_binary_disguised_as_text_is_rejected():
    with pytest.raises(ValueError):
        await media_to_part(UploadFile(filename="example.py", file=BytesIO(bytes([0, 255]))))


def test_large_document_is_rejected_instead_of_silently_truncated():
    with pytest.raises(ValueError, match="слишком большой"):
        _checked_text("x" * 30001, "large.txt")
