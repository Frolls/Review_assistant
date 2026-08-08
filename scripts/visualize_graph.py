from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.agent_graph import custom_graph, prebuilt_graph

DOCS_DIR = PROJECT_ROOT / "docs"


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    graphs = {
        "agent-graph-custom.mmd": custom_graph,
        "agent-graph-prebuilt.mmd": prebuilt_graph,
    }
    for filename, graph in graphs.items():
        mermaid = graph.get_graph().draw_mermaid()
        (DOCS_DIR / filename).write_text(mermaid.rstrip() + "\n", encoding="utf-8")
        print(f"saved {DOCS_DIR / filename}")

    try:
        png = custom_graph.get_graph().draw_mermaid_png()
    except Exception as exc:  # noqa: BLE001 - PNG is an optional best-effort artifact
        print(f"PNG skipped: {type(exc).__name__}: {exc}")
    else:
        (DOCS_DIR / "agent-graph.png").write_bytes(png)
        print(f"saved {DOCS_DIR / 'agent-graph.png'}")


if __name__ == "__main__":
    main()
