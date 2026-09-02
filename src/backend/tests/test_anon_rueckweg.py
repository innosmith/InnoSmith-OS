"""Der Rueckweg der Anonymisierung -- mit Schluesseldatei statt Sitzung.

Der Anlass ist ein Weg, den es nur zur Haelfte gab: Die Oberflaeche bot den
Download der Schluesseldatei an, und ``POST /api/content/deanonymize`` nahm
ausschliesslich eine ``session_id`` entgegen. Der Mapping-Store haelt Sitzungen
zwei Stunden und ueberlebt keinen Neustart -- die heruntergeladene Datei war
damit etwas, das man aufbewahren, aber nie benutzen konnte. Genau dann, wenn man
sie braucht, gab es keinen Endpunkt, der sie annahm.

Geprueft wird deshalb die Behauptung: **Wer den Schluessel hat, kommt zurueck** --
auch ohne Sitzung. Dazu die Absicherung dagegen, dass eine falsche Datei
stillschweigend durchlaeuft: Eine JSON-Datei ohne ``mappings`` gaebe den Text
unveraendert zurueck, und das saehe aus wie ein sauberer Rueckweg.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers.content import DeanonymizeRequest, deanonymize_text
from app.services import anon_politik

pytestmark = pytest.mark.asyncio

SCHLUESSEL = {
    "session_id": "S1",
    "mappings": {"Peter Meier": "Louis Egli"},
    "entity_types": {"Peter Meier": "PERSON"},
}


class TestSchluesseldatei:
    async def test_schluessel_ersetzt_die_sitzung(self):
        with patch(
            "app.services.anon_politik.bilde_zurueck_mit_schluessel",
            new=AsyncMock(return_value=("Louis Egli zahlt.", [])),
        ) as mit_schluessel:
            antwort = await deanonymize_text(
                DeanonymizeRequest(text="Peter Meier zahlt.", keys=SCHLUESSEL),
                user=None,
            )
        assert antwort.original_text == "Louis Egli zahlt."
        mit_schluessel.assert_awaited_once()

    async def test_schluessel_hat_vorrang_vor_der_sitzung(self):
        """Wer eine Datei hochlaedt, meint sie -- nicht eine noch offene Sitzung."""
        ueber_sitzung = AsyncMock(return_value=("aus der Sitzung", []))
        with (
            patch(
                "app.services.anon_politik.bilde_zurueck_mit_schluessel",
                new=AsyncMock(return_value=("aus der Datei", [])),
            ),
            patch("app.services.anon_politik.bilde_zurueck", new=ueber_sitzung),
        ):
            antwort = await deanonymize_text(
                DeanonymizeRequest(text="egal", keys=SCHLUESSEL, session_id="S9"),
                user=None,
            )
        assert antwort.original_text == "aus der Datei"
        ueber_sitzung.assert_not_awaited()

    async def test_datei_ohne_mappings_wird_abgewiesen(self):
        """Sonst laeuft der Text unveraendert durch und sieht sauber aus."""
        with pytest.raises(HTTPException) as fehler:
            await deanonymize_text(
                DeanonymizeRequest(text="egal", keys={"irgendwas": 1}), user=None
            )
        assert fehler.value.status_code == 422

    async def test_weder_sitzung_noch_datei_wird_abgewiesen(self):
        with pytest.raises(HTTPException) as fehler:
            await deanonymize_text(DeanonymizeRequest(text="egal"), user=None)
        assert fehler.value.status_code == 422


class TestEigeneBegriffe:
    async def test_begriffe_erreichen_die_maskierung(self):
        """Kennungen ohne Namensform sieht die Erkennung nicht von allein."""
        werkzeug = AsyncMock(
            return_value={"anonymized_text": "maskiert", "mapping_keys": SCHLUESSEL}
        )
        with (
            patch("ai9.content_converter.call_tool", new=werkzeug),
            patch("ai9.content_converter.call_tool_liste", new=AsyncMock(return_value=[])),
        ):
            await anon_politik.maskiere("Text", ["Nordwind", " ", "GSW"])

        assert werkzeug.await_args.kwargs["always_mask"] == "Nordwind,GSW"

    async def test_ohne_begriffe_bleibt_die_liste_leer(self):
        werkzeug = AsyncMock(
            return_value={"anonymized_text": "maskiert", "mapping_keys": SCHLUESSEL}
        )
        with (
            patch("ai9.content_converter.call_tool", new=werkzeug),
            patch("ai9.content_converter.call_tool_liste", new=AsyncMock(return_value=[])),
        ):
            await anon_politik.maskiere("Text")

        assert werkzeug.await_args.kwargs["always_mask"] == ""
