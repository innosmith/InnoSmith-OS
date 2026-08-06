"""Shared Konvertierungslogik für Dokumenten-Export.

Wird von routers/export.py (Chat-Nachrichten-Export) und
routers/content.py (Direkt-Konvertierung) gemeinsam genutzt.
"""

import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ai9 import content_converter as cc

logger = logging.getLogger("taskpilot.document_export")

EXPORT_TMP_DIR = Path(tempfile.gettempdir()) / "taskpilot-exports"
EXPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ConvertOptions:
    """Parameter für die Dokumenten-Konvertierung."""

    format: Literal["markdown", "docx", "pdf", "pptx"]
    title: str = "Export"
    author: str = "InnoSmith"
    title_page: bool = True
    toc: bool = True
    template: str | None = None
    pptx_template: str | None = None
    filename: str | None = None


def _as_template_entry(entry) -> dict | None:
    """Normalisiert einen Listeneintrag der Template-Liste auf ein Dict."""
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        try:
            parsed = json.loads(entry)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


async def fetch_templates() -> list[dict]:
    """Liest die Template-Liste vom contentConverter.

    Der MCP-Server serialisiert Listen als einen Textblock pro Element. Der
    geteilte MCP-Client parst nur Einzelblöcke als JSON und reicht bei mehreren
    Blöcken rohe Strings durch -- ab dem zweiten Template kämen also Strings statt
    Dicts an. Diese Funktion normalisiert beide Formen.
    """
    try:
        result = await cc.call_tool("list_templates")
    except RuntimeError:
        logger.exception("Template-Liste konnte nicht geladen werden")
        raise HTTPException(
            status_code=503,
            detail="Content-Service nicht erreichbar",
        )

    if isinstance(result, dict):
        # Einzelnes Template (ein Textblock) bzw. strukturierte Antwort
        inner = result.get("result")
        result = inner if isinstance(inner, list) else [result]

    if not isinstance(result, list):
        return []

    entries = (_as_template_entry(e) for e in result)
    return [e for e in entries if e and e.get("path")]


async def resolve_template(name: str | None) -> str | None:
    """Löst den Anzeigenamen eines Word-Templates auf sein Profil-Verzeichnis auf.

    ``list_templates`` liefert den Namen aus der ``template.yaml`` (z. B.
    "Kanton Bern MBA"), ``load_template_profile`` sucht aber ausschliesslich nach
    Verzeichnisnamen (z. B. "Kt. Bern MBA") -- ohne Mapping fällt die Konvertierung
    still aufs Standard-Layout zurück. Die Liste dient zugleich als Allowlist, damit
    kein beliebiger Dateisystempfad als Template durchgereicht werden kann.

    Returns:
        Absoluter Pfad zum Profil-Verzeichnis oder ``None`` für das Standard-Layout.
    """
    if not name:
        return None

    for entry in await fetch_templates():
        path = Path(str(entry.get("path", "")))
        aliases = {str(entry.get("name", "")).lower(), path.name.lower()}
        if name.lower() in aliases:
            # Reference-Dokumente (reference_*.docx) sind keine Profile --
            # sie entsprechen dem Standard-Layout.
            return str(path) if path.is_dir() else None

    raise HTTPException(status_code=400, detail=f"Unbekanntes Template: {name}")


async def convert_markdown(
    content: str,
    base_name: str,
    opts: ConvertOptions,
) -> FileResponse:
    """Konvertiert Markdown-Text ins Zielformat und gibt eine FileResponse zurück."""
    if opts.format == "markdown":
        md_path = EXPORT_TMP_DIR / f"{uuid.uuid4().hex}.md"
        md_path.write_text(content, encoding="utf-8")
        return FileResponse(
            str(md_path),
            filename=f"{base_name}.md",
            media_type="text/markdown",
        )

    if opts.format == "pptx":
        return await _convert_pptx(content, base_name, opts)

    return await _convert_docx_pdf(content, base_name, opts)


async def _convert_pptx(
    content: str,
    base_name: str,
    opts: ConvertOptions,
) -> FileResponse:
    """Konvertiert als PowerPoint via contentConverter MCP-Server."""
    pptx_template = opts.pptx_template
    if not pptx_template:
        from app.config import get_settings

        settings = get_settings()
        template_dir = Path(settings.pptx_template_dir)
        candidates = list(template_dir.glob("*.pptx")) if template_dir.exists() else []
        if candidates:
            pptx_template = str(candidates[0])

    if not pptx_template:
        raise HTTPException(
            status_code=400,
            detail="Kein PPTX-Template gefunden. Bitte Template-Pfad angeben.",
        )

    slide_script = await cc.call_tool("prepare_for_slides", text=content)

    tmp_md = EXPORT_TMP_DIR / f"{uuid.uuid4().hex}.md"
    tmp_md.write_text(
        slide_script if isinstance(slide_script, str) else str(slide_script),
        encoding="utf-8",
    )

    try:
        result_path = await cc.call_tool(
            "convert_to_pptx",
            input_file=str(tmp_md),
            template=pptx_template,
            output=str(EXPORT_TMP_DIR / f"{uuid.uuid4().hex}.pptx"),
        )
    except Exception as e:
        logger.error("PowerPoint-Konvertierung fehlgeschlagen: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"PowerPoint-Konvertierung fehlgeschlagen: {e}",
        )
    finally:
        tmp_md.unlink(missing_ok=True)

    output_path = result_path if isinstance(result_path, str) else str(result_path)

    return FileResponse(
        output_path,
        filename=f"{base_name}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


async def _convert_docx_pdf(
    content: str,
    base_name: str,
    opts: ConvertOptions,
) -> FileResponse:
    """Konvertiert als Word oder PDF via contentConverter MCP-Server."""
    template_dir = await resolve_template(opts.template)

    try:
        prepared = await cc.call_tool(
            "prepare_for_word",
            text=content,
            title=opts.title,
            author=opts.author,
            lang="de-CH",
        )
    except Exception as e:
        logger.error("Markdown-Vorbereitung fehlgeschlagen: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Markdown-Vorbereitung fehlgeschlagen: {e}",
        )

    tmp_md = EXPORT_TMP_DIR / f"{uuid.uuid4().hex}.md"
    tmp_md.write_text(
        prepared if isinstance(prepared, str) else str(prepared),
        encoding="utf-8",
    )

    try:
        docx_output = str(EXPORT_TMP_DIR / f"{uuid.uuid4().hex}.docx")
        docx_path = await cc.call_tool(
            "convert_to_word",
            input_file=str(tmp_md),
            output=docx_output,
            lang="de-CH",
            author=opts.author,
            title=opts.title,
            template=template_dir,
            title_page=opts.title_page,
            toc=opts.toc,
        )
    except Exception as e:
        logger.error("Word-Konvertierung fehlgeschlagen: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Konvertierung fehlgeschlagen: {e}",
        )
    finally:
        tmp_md.unlink(missing_ok=True)

    docx_path_str = docx_path if isinstance(docx_path, str) else str(docx_path)

    if opts.format == "pdf":
        try:
            pdf_path = await cc.call_tool(
                "convert_to_pdf",
                input_file=docx_path_str,
            )
        except Exception as e:
            logger.error("PDF-Konvertierung fehlgeschlagen: %s", e)
            raise HTTPException(
                status_code=500,
                detail=f"PDF-Konvertierung fehlgeschlagen: {e}",
            )
        return FileResponse(
            pdf_path if isinstance(pdf_path, str) else str(pdf_path),
            filename=f"{base_name}.pdf",
            media_type="application/pdf",
        )

    return FileResponse(
        docx_path_str,
        filename=f"{base_name}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
