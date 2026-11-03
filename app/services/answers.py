"""The published answer, after evidence and output checks have completed."""
from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict

from app.schemas.rag import RAGSource


class Source(RAGSource):
    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class ReviewAnswer:
    text: str
    status: Literal["answered", "clarify", "refused", "blocked"]
    cited_sources: tuple[Source, ...] = ()

    @property
    def confident(self) -> bool:
        return self.status == "answered" and bool(self.cited_sources)

    def metadata(self) -> dict:
        return {
            "status": self.status,
            "confident": self.confident,
            "sources": [source.model_dump() for source in self.cited_sources],
        }
