"""Router für Content-Services: Anonymisierung, Extraktion, Templates, Konvertierung.

Dünne REST-Schicht die alle Aufrufe an den contentConverter MCP-Client-Service
delegiert. Mapping-Keys werden im In-Memory-Store verwaltet (TTL 2h).
"""

import logging
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth.deps import get_current_user, require_role
from app.models import User
from ai9 import content_converter as cc
from ai9 import mapping_store
from app.services.document_export import (
    ConvertOptions,
    convert_markdown,
    fetch_templates,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/content", tags=["content"])

_TMP_DIR = Path(tempfile.gettempdir()) / "taskpilot-content"
_TMP_DIR.mkdir(parents=True, exist_ok=True)


# --- Schemas ---


class AnonymizeRequest(BaseModel):
    text: str
    language: str = "auto"


class AnonymizeResponse(BaseModel):
    session_id: str
    anonymized_text: str
    diff: list[dict]
    restbestaende: list[str] = []
    """Bruchstuecke echter Werte, die im maskierten Text stehen geblieben sind.

    Der typische Fall ist die Teilnennung: «Egli Immobilien AG» wird ersetzt, das
    alleinstehende «Eglis» zwei Saetze weiter nicht. Sie automatisch nachzuziehen
    hiesse raten -- gemeldet werden sie, damit der Mensch vor dem Kopieren
    hinsehen kann.
    """


class DeanonymizeRequest(BaseModel):
    text: str
    session_id: str


class DeanonymizeResponse(BaseModel):
    original_text: str
    rueckstaende: list[str] = []
    """Ersatznamen, die die Rueckbildung nicht erwischt hat -- meist gebeugt
    geschrieben («Weibels» statt «Weibel»). Sie sind der stillste Fehler dieser
    Strecke: plausible fremde Namen in einem Text, der als fertig gilt.
    """


class ExtractResponse(BaseModel):
    text: str


# --- Anonymisierung ---


@router.post("/anonymize", response_model=AnonymizeResponse)
async def anonymize_text(
    body: AnonymizeRequest,
    user: User = Depends(require_role("owner")),
):
    """Anonymisiert Text mit realistischen Ersatznamen.

    Gibt den anonymisierten Text plus eine Session-ID zurück.
    Die Mapping-Keys bleiben im Backend (In-Memory, TTL 2h).

    Die Entitätenliste ist **nicht** mehr Teil der Anfrage. Sie war es bis zum
    24.08.2026 und stand auf ``PERSON, ORG, LOCATION`` -- E-Mail-Adressen,
    Telefonnummern, IBAN, AHV- und UID-Nummern blieben also unberührt im Text,
    obwohl contentConverter sie erkennt. Dieser Weg endet damit, dass ein Mensch
    den Text in ein fremdes Sprachmodell kopiert; eine Wahl, die dabei still
    schiefgehen kann, gehört nicht in eine Anfrage. Was gilt, steht in
    ``app/services/anon_politik.py``.
    """
    from app.services import anon_politik

    try:
        anonymized_text, session_id, diff_pairs, restbestaende = await anon_politik.maskiere(
            body.text
        )
    except Exception:  # noqa: BLE001
        logger.exception("Anonymisierung fehlgeschlagen")
        raise HTTPException(status_code=503, detail="Content-Service nicht erreichbar")

    return AnonymizeResponse(
        session_id=session_id,
        anonymized_text=anonymized_text,
        diff=diff_pairs,
        restbestaende=restbestaende,
    )


@router.post("/anonymize/file", response_model=AnonymizeResponse)
async def anonymize_file(
    file: UploadFile = File(...),
    language: str = "auto",
    user: User = Depends(require_role("owner")),
):
    """Anonymisiert eine hochgeladene Datei (MD, DOCX, PDF)."""
    from app.services import anon_politik

    suffix = Path(file.filename or "upload.txt").suffix
    tmp_path = _TMP_DIR / f"{uuid.uuid4().hex}{suffix}"

    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        extracted = await cc.call_tool("extract_content", input_file=str(tmp_path))
        text_content = extracted if isinstance(extracted, str) else str(extracted)

        anonymized_text, session_id, diff_pairs, restbestaende = await anon_politik.maskiere(
            text_content
        )
    except Exception:  # noqa: BLE001
        logger.exception("Datei-Anonymisierung fehlgeschlagen")
        raise HTTPException(status_code=503, detail="Content-Service nicht erreichbar")
    finally:
        tmp_path.unlink(missing_ok=True)

    return AnonymizeResponse(
        session_id=session_id,
        anonymized_text=anonymized_text,
        diff=diff_pairs,
        restbestaende=restbestaende,
    )


# --- De-Anonymisierung ---


@router.post("/deanonymize", response_model=DeanonymizeResponse)
async def deanonymize_text(
    body: DeanonymizeRequest,
    user: User = Depends(require_role("owner")),
):
    """Stellt Originalwerte in anonymisiertem Text wieder her.

    Nutzt die im Backend gespeicherten Mapping-Keys (via session_id).
    """
    from app.services import anon_politik

    if mapping_store.get_mapping_keys(body.session_id) is None:
        raise HTTPException(
            status_code=404,
            detail="Mapping-Keys nicht gefunden oder abgelaufen (TTL 2h). "
            "Bitte erneut anonymisieren oder die heruntergeladene Key-Datei verwenden.",
        )

    original_text, rueckstaende = await anon_politik.bilde_zurueck(body.text, body.session_id)
    if rueckstaende:
        logger.warning("De-Anonymisierung: Ersatznamen im Text geblieben: %s", rueckstaende[:5])

    return DeanonymizeResponse(original_text=original_text, rueckstaende=rueckstaende)


# --- Mapping-Keys Download ---


@router.get("/mapping-keys/{session_id}/download")
async def download_mapping_keys(
    session_id: str,
    user: User = Depends(require_role("owner")),
):
    """Gibt die Mapping-Keys als JSON-Download zurück.

    Der User kann die Datei lokal speichern für spätere De-Anonymisierung.
    """
    keys = mapping_store.export_mapping_keys(session_id)
    if keys is None:
        raise HTTPException(
            status_code=404,
            detail="Mapping-Keys nicht gefunden oder abgelaufen.",
        )

    return JSONResponse(
        content=keys,
        headers={
            "Content-Disposition": f'attachment; filename="mapping-keys-{session_id[:8]}.json"',
        },
    )


@router.get("/mapping-keys/{session_id}/diff")
async def get_diff_pairs(
    session_id: str,
    user: User = Depends(require_role("owner")),
):
    """Gibt die Diff-Paare für die Frontend-Anzeige zurück."""
    diff = mapping_store.get_diff_pairs(session_id)
    if diff is None:
        raise HTTPException(
            status_code=404,
            detail="Session nicht gefunden oder abgelaufen.",
        )
    return diff


# --- Extraktion ---


@router.post("/extract", response_model=ExtractResponse)
async def extract_content(
    file: UploadFile = File(...),
    user: User = Depends(require_role("owner")),
):
    """Extrahiert Text aus Dokumenten (PDF, DOCX) als Markdown."""
    suffix = Path(file.filename or "upload.txt").suffix
    tmp_path = _TMP_DIR / f"{uuid.uuid4().hex}{suffix}"

    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        result = await cc.call_tool("extract_content", input_file=str(tmp_path))
    except RuntimeError as e:
        logger.exception("Text-Extraktion fehlgeschlagen")
        raise HTTPException(status_code=503, detail="Content-Service nicht erreichbar")
    finally:
        tmp_path.unlink(missing_ok=True)

    return ExtractResponse(
        text=result if isinstance(result, str) else str(result),
    )


# --- Templates ---


@router.get("/templates")
async def list_templates(
    user: User = Depends(require_role("owner")),
):
    """Listet die verfügbaren Template-Profile auf.

    Reference-Dokumente (``reference_*.docx``) werden ausgeblendet: Sie sind keine
    Profile, sondern genau das Standard-Layout, das die Auswahl ohnehin schon als
    ersten Eintrag anbietet.
    """
    return [
        entry
        for entry in await fetch_templates()
        if Path(str(entry.get("path", ""))).is_dir()
    ]


# --- Direkt-Konvertierung ---


class ConvertRequest(BaseModel):
    text: str
    format: Literal["docx", "pdf", "pptx"]
    title: str = "Export"
    author: str = "InnoSmith"
    title_page: bool = True
    toc: bool = True
    template: str | None = None
    pptx_template: str | None = None
    filename: str | None = None


@router.post("/convert")
async def convert_text(
    body: ConvertRequest,
    user: User = Depends(require_role("owner")),
):
    """Konvertiert Markdown-Text direkt in DOCX, PDF oder PPTX."""
    base_name = body.filename or f"export-{date.today().isoformat()}"

    opts = ConvertOptions(
        format=body.format,
        title=body.title,
        author=body.author,
        title_page=body.title_page,
        toc=body.toc,
        template=body.template,
        pptx_template=body.pptx_template,
        filename=body.filename,
    )

    return await convert_markdown(body.text, base_name, opts)


@router.post("/convert/file")
async def convert_file(
    file: UploadFile = File(...),
    format: Literal["docx", "pdf", "pptx"] = Form("docx"),
    title: str = Form("Export"),
    author: str = Form("InnoSmith"),
    title_page: bool = Form(True),
    toc: bool = Form(True),
    template: str | None = Form(None),
    pptx_template: str | None = Form(None),
    filename: str | None = Form(None),
    user: User = Depends(require_role("owner")),
):
    """Konvertiert eine hochgeladene Datei (.md, .docx, .pdf) ins Zielformat.

    Bei Nicht-Markdown-Dateien wird zuerst der Text via extract_content
    extrahiert, dann ins Zielformat konvertiert.
    """
    suffix = Path(file.filename or "upload.txt").suffix.lower()
    tmp_path = _TMP_DIR / f"{uuid.uuid4().hex}{suffix}"

    try:
        content_bytes = await file.read()
        tmp_path.write_bytes(content_bytes)

        if suffix == ".md":
            text_content = content_bytes.decode("utf-8")
        else:
            extracted = await cc.call_tool("extract_content", input_file=str(tmp_path))
            text_content = extracted if isinstance(extracted, str) else str(extracted)
    except RuntimeError as e:
        logger.exception("Datei-Konvertierung fehlgeschlagen")
        raise HTTPException(status_code=503, detail="Content-Service nicht erreichbar")
    finally:
        tmp_path.unlink(missing_ok=True)

    base_name = filename or Path(file.filename or "export").stem

    opts = ConvertOptions(
        format=format,
        title=title,
        author=author,
        title_page=title_page,
        toc=toc,
        template=template,
        pptx_template=pptx_template,
        filename=filename,
    )

    return await convert_markdown(text_content, base_name, opts)
