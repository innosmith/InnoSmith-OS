"""Tests: kein Entwurf mit fremdem Namen aus dem Cloud-Schreibpfad.

Am 04.08.2026 gegen das echte contentConverter-Modell geprueft: die Maskierung
arbeitet nicht mit Platzhaltern, sondern mit ERSATZNAMEN -- «Gabriel» wird zu
«Senad Weibel», «InnoSmith GmbH» zu «Hess & Partner». Technische Angaben
(Hostnames, IPs, Ports, Fristen) bleiben unversehrt, der Round-Trip war in allen
drei Testtexten exakt.

Das Restrisiko liegt genau in dieser Fluessigkeit: schreibt das Modell den
Ersatznamen verkuerzt («Hoi Senad»), findet die Ruecksetzung ihn nicht. Der Entwurf
traegt dann einen fremden, voellig plausiblen Namen -- schwer zu erkennen, weil
nichts nach Fehler aussieht. Darum wird nach der Ruecksetzung geprueft und im
Zweifel lokal neu geschrieben.
"""

from unittest.mock import patch

import pytest

from app.services.hermes_worker import (
    _deanonymize_from_cloud,
    _residual_pseudonyms,
)

_KEYS = {
    "mappings": {
        "Senad Weibel": "Gabriel Brunner",
        "Hess & Partner": "InnoSmith GmbH",
    },
    "entity_types": {"Senad Weibel": "PERSON", "Hess & Partner": "ORG"},
}


def _store(keys):
    return patch("ai9.mapping_store.get_mapping_keys", return_value=keys)


def test_clean_text_has_no_residue():
    with _store(_KEYS):
        text = "Hoi Gabriel\n\nMerci. Liebe Grüsse\nAnthony, InnoSmith GmbH"
        assert _residual_pseudonyms(text, "s1") == []


def test_full_pseudonym_is_caught():
    with _store(_KEYS):
        assert _residual_pseudonyms("Hoi Senad Weibel", "s1") == ["Senad Weibel"]


def test_shortened_person_pseudonym_is_caught():
    """Der eigentliche Schadensfall: nur der Vorname des Tarnnamens bleibt stehen."""
    with _store(_KEYS):
        assert _residual_pseudonyms("Hoi Senad, besten Dank", "s1") == ["Senad"]


def test_org_fragment_does_not_trigger():
    # «Partner» allein ist ein Alltagswort -- Firmen nur als ganzer String pruefen.
    with _store(_KEYS):
        assert _residual_pseudonyms("Wir suchen einen Partner dafür.", "s1") == []


def test_expired_session_cannot_be_checked_here():
    """Ohne Mapping ist keine Pruefung moeglich -- deshalb wirft die Ruecksetzung."""
    with _store(None):
        assert _residual_pseudonyms("Hoi Senad Weibel", "s1") == []


@pytest.mark.asyncio
async def test_deanonymize_raises_when_mapping_gone():
    # Ein abgelaufenes Mapping darf nicht stillschweigend den maskierten Text
    # durchlassen: der Aufrufer verwirft bei Fehlern, gibt aber Text weiter.
    with _store(None), pytest.raises(RuntimeError):
        await _deanonymize_from_cloud("Hoi Senad Weibel", "s1")
