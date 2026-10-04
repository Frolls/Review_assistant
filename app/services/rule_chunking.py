"""Keep independent rules in curated prose guides out of each other's chunks."""

from __future__ import annotations

import re
from typing import Any, Sequence

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.node_parser.node_utils import build_nodes_from_splits
from llama_index.core.schema import BaseNode, MetadataMode


class RuleSentenceSplitter(SentenceSplitter):
    """Use hard paragraph boundaries only for explicitly marked prose guides.

    Ordinary documents retain SentenceSplitter behavior. Guides containing code
    blocks also use the standard splitter: blank lines inside examples are not
    independent rules. Overlap applies within a rule, never across rules.
    """

    @classmethod
    def class_name(cls) -> str:
        return "RuleSentenceSplitter"

    def _parse_nodes(
        self, nodes: Sequence[BaseNode], show_progress: bool = False, **kwargs: Any
    ) -> list[BaseNode]:
        result: list[BaseNode] = []
        for node in nodes:
            text = node.get_content(metadata_mode=MetadataMode.NONE)
            if (
                node.metadata.get("chunking_profile") != "rule_paragraphs"
                or re.search(r"(?m)^(?: {0,3}(?:`{3,}|~{3,})|\t| {4}\S)", text)
            ):
                result.extend(super()._parse_nodes([node], show_progress, **kwargs))
                continue

            paragraphs = re.split(r"\n\s*\n", text.strip())
            blocks: list[str] = []
            for paragraph in paragraphs:
                # Keep a heading or an introduction with its following list.
                if blocks and (
                    _is_heading_prefix(blocks[-1])
                    or re.match(r"\s*(?:[-*+] |\d+[.)] )", paragraph)
                    or blocks[-1].rstrip().endswith(":")
                ):
                    blocks[-1] += "\n\n" + paragraph
                elif paragraph.strip():
                    blocks.append(paragraph)

            splits = [
                part
                for block in blocks
                for part in self.split_text_metadata_aware(
                    block, metadata_str=self._get_metadata_str(node)
                )
            ]
            result.extend(build_nodes_from_splits(splits, node, id_func=self.id_func))
        return result


def _is_heading_prefix(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(
        re.match(r"#{1,6} ", line) or line.startswith("Источник:")
        for line in lines
    )
