from __future__ import annotations

import pytest

from app.moderation import ModerationService


@pytest.mark.asyncio
async def test_keyword_moderation_blocks_forbidden_input() -> None:
    service = ModerationService()

    result = await service.check_input("I want to kill them.")

    assert result.allowed is False
    assert "violence" in result.categories
    assert result.blocked_by == "keyword"
