"""Tests für die Template-Auflösung im Dokumenten-Export.

Zwei Produktionsfehler werden hier abgesichert:

1. Der MCP-Server serialisiert Listen als einen Textblock pro Element. Der
   geteilte MCP-Client parst nur Einzelblöcke als JSON und reicht bei mehreren
   Blöcken rohe Strings durch -- die Template-Liste kam ab dem zweiten Eintrag
   als JSON-String im Frontend an, wo ``t.name`` undefiniert war und die Auswahl
   leer blieb. Die Mocks liefern deshalb bewusst die echte String-Form.
2. Das Frontend schickt den Anzeigenamen aus ``list_templates`` (z. B.
   "Kanton Bern MBA"), contentConverter sucht das Profil aber ausschliesslich
   über den Verzeichnisnamen ("Kt. Bern MBA"). Ohne Mapping liefert
   ``load_template_profile`` still ``None`` und das Dokument entsteht im
   Standard-Layout -- der Export sieht erfolgreich aus, die gewählte Vorlage
   fehlt aber.

Kein echter MCP-Aufruf: ``cc.call_tool`` wird gemockt.
"""

import json

import pytest
from fastapi import HTTPException

from app.services import document_export


@pytest.fixture
def template_dirs(tmp_path):
    """Legt eine Template-Struktur wie im contentConverter-Repo an."""
    profile = tmp_path / "Kt. Bern MBA"
    profile.mkdir()
    (profile / "HERMES-Template-MBA.docx").write_bytes(b"dummy")

    reference = tmp_path / "reference_de_CH.docx"
    reference.write_bytes(b"dummy")

    return {
        "profile": profile,
        "reference": reference,
        # Genau die Form, die der MCP-Client bei mehreren Treffern liefert:
        # ein JSON-String pro Listenelement.
        "entries": [
            json.dumps({"name": "Standard (de-CH)", "path": str(reference)}),
            json.dumps({"name": "Kanton Bern MBA", "path": str(profile)}),
        ],
    }


@pytest.fixture
def mock_list_templates(monkeypatch, template_dirs):
    """Ersetzt den MCP-Aufruf durch die vorbereitete Template-Liste."""
    async def _call_tool(tool_name: str, **kwargs):
        assert tool_name == "list_templates"
        return template_dirs["entries"]

    monkeypatch.setattr(document_export.cc, "call_tool", _call_tool)
    return template_dirs


class TestFetchTemplates:
    """Prüft die Normalisierung der MCP-Antwort auf Dicts."""

    @pytest.mark.asyncio
    async def test_json_strings_werden_zu_dicts(self, mock_list_templates):
        entries = await document_export.fetch_templates()
        assert [e["name"] for e in entries] == ["Standard (de-CH)", "Kanton Bern MBA"]

    @pytest.mark.asyncio
    async def test_einzelnes_template_als_dict(self, monkeypatch, template_dirs):
        """Ein einziger Textblock kommt bereits als Dict an, nicht als Liste."""
        async def _call_tool(tool_name: str, **kwargs):
            return {"name": "Kanton Bern MBA", "path": str(template_dirs["profile"])}

        monkeypatch.setattr(document_export.cc, "call_tool", _call_tool)

        entries = await document_export.fetch_templates()
        assert len(entries) == 1
        assert entries[0]["name"] == "Kanton Bern MBA"

    @pytest.mark.asyncio
    async def test_unlesbare_eintraege_werden_uebersprungen(self, monkeypatch):
        async def _call_tool(tool_name: str, **kwargs):
            return ["kein JSON", json.dumps({"name": "Ohne Pfad"})]

        monkeypatch.setattr(document_export.cc, "call_tool", _call_tool)

        assert await document_export.fetch_templates() == []


class TestResolveTemplate:
    """Prüft die Zuordnung Anzeigename → Profil-Verzeichnis."""

    @pytest.mark.asyncio
    async def test_none_bleibt_none(self, mock_list_templates):
        assert await document_export.resolve_template(None) is None

    @pytest.mark.asyncio
    async def test_leerstring_bleibt_none(self, mock_list_templates):
        assert await document_export.resolve_template("") is None

    @pytest.mark.asyncio
    async def test_yaml_name_wird_zu_verzeichnis(self, mock_list_templates):
        result = await document_export.resolve_template("Kanton Bern MBA")
        assert result == str(mock_list_templates["profile"])

    @pytest.mark.asyncio
    async def test_verzeichnisname_funktioniert_ebenfalls(self, mock_list_templates):
        result = await document_export.resolve_template("Kt. Bern MBA")
        assert result == str(mock_list_templates["profile"])

    @pytest.mark.asyncio
    async def test_gross_kleinschreibung_egal(self, mock_list_templates):
        result = await document_export.resolve_template("kanton bern mba")
        assert result == str(mock_list_templates["profile"])

    @pytest.mark.asyncio
    async def test_reference_dokument_ergibt_standard_layout(self, mock_list_templates):
        assert await document_export.resolve_template("Standard (de-CH)") is None

    @pytest.mark.asyncio
    async def test_unbekanntes_template_wird_abgewiesen(self, mock_list_templates):
        """Allowlist: kein beliebiger Dateisystempfad als Template."""
        with pytest.raises(HTTPException) as exc:
            await document_export.resolve_template("/etc/passwd")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_content_service_nicht_erreichbar(self, monkeypatch):
        async def _fail(tool_name: str, **kwargs):
            raise RuntimeError("contentConverter-Fehler: nicht verfügbar")

        monkeypatch.setattr(document_export.cc, "call_tool", _fail)

        with pytest.raises(HTTPException) as exc:
            await document_export.resolve_template("Kanton Bern MBA")
        assert exc.value.status_code == 503


class TestTemplatesEndpoint:
    """Contract-Test für GET /api/content/templates."""

    @pytest.mark.asyncio
    async def test_liefert_profile_ohne_reference_dokumente(
        self, client_as_owner, mock_list_templates
    ):
        resp = await client_as_owner.get("/api/content/templates")
        assert resp.status_code == 200

        entries = resp.json()
        assert [e["name"] for e in entries] == ["Kanton Bern MBA"]
        assert entries[0]["path"] == str(mock_list_templates["profile"])

    @pytest.mark.asyncio
    async def test_rejects_anonymous(self, client_anonymous):
        resp = await client_anonymous.get("/api/content/templates")
        assert resp.status_code in (401, 403)
