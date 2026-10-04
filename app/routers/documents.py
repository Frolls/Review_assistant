from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import aiofiles
import asyncio

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from app.core.config import Settings
from app.deps.providers import SettingsDep
from app.observability.logging import get_logger
from app.services.ingestion import IngestionService, SUPPORTED_SUFFIXES


router = APIRouter(prefix="/documents", tags=["documents"])
logger = get_logger(__name__)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post(
    "/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Save a document and index it in the background",
)
async def upload_document(
    background_tasks: BackgroundTasks,
    request: Request,
    settings: SettingsDep,
    file: UploadFile = File(...),
    category: str = Form("uploads"),
) -> dict[str, str]:
    file_name = _safe_file_name(file.filename)
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "unsupported_document_format",
                "message": "Supported formats: PDF, DOCX, HTML and Markdown.",
            },
        )
    safe_category = _safe_category(category)
    target_dir = settings.rag_input_dir / safe_category
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(target_dir / file_name)

    written = 0
    try:
        async with aiofiles.open(target, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "document_too_large",
                            "message": "Maximum upload size is 50 MiB.",
                        },
                    )
                await output.write(chunk)
    except Exception:
        if target.exists():
            target.unlink()
        raise
    finally:
        await file.close()

    background_tasks.add_task(_index_uploaded_file, target, settings, request.app)
    return {
        "status": "accepted",
        "file_name": target.name,
        "path": str(target),
    }


async def _index_uploaded_file(path: Path, settings: Settings, app: object) -> None:
    try:
        async with app.state.ingestion_lock:
            result = await asyncio.to_thread(
                IngestionService(settings).ingest_path, path, rename_failed=True,
            )
        if result.failed:
            logger.error(
                "documents.upload_index_failed",
                path=str(path),
                failures=result.failures,
            )
            return
        from app.services.rag import RAGService

        if getattr(app.state, "rag_service", None) is None:
            async with app.state.rag_init_lock:
                if app.state.rag_service is None:
                    refreshed = RAGService(settings)
                    await refreshed.build()
                    app.state.rag_service = refreshed
                    from app.agents.graph import build_supervisor_graph
                    from app.agents.tools import make_search_knowledge_base_tool
                    from app.services.agent_persistent import _build_model
                    app.state.multi_agent_graph = build_supervisor_graph(
                        model=_build_model(settings),
                        search_tool=make_search_knowledge_base_tool(refreshed),
                    )
        logger.info(
            "documents.upload_indexed",
            path=str(path),
            chunks=result.chunks_written,
        )
    except Exception as exc:
        logger.exception("documents.upload_index_failed", path=str(path), error=str(exc))


def _safe_file_name(value: str | None) -> str:
    name = Path(value or "document").name
    sanitized = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", name).strip("._")
    return sanitized or f"document-{uuid4().hex[:8]}"


def _safe_category(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "_", value).strip("._")
    return sanitized or "uploads"


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}-{uuid4().hex[:8]}{path.suffix}")
