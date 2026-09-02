"""Tests: was gilt, wenn Text das Haus verlaesst.

Am 04.08.2026 gegen das echte contentConverter-Modell geprueft: die Maskierung
arbeitet nicht mit Platzhaltern, sondern mit ERSATZNAMEN -- «Gabriel» wird zu
«Senad Weibel», «InnoSmith GmbH» zu «Hess & Partner». Technische Angaben
(Hostnames, IPs, Ports, Fristen) bleiben unversehrt, der Round-Trip war in allen
drei Testtexten exakt.

Das Restrisiko liegt genau in dieser Fluessigkeit. Es hat **zwei** Richtungen,
und die zweite fiel erst am 24.08.2026 auf:

- **Hinaus** (Restbestand): Ein Bruchstueck des echten Werts bleibt im maskierten
  Text stehen -- «Egli Immobilien AG» wird ersetzt, das alleinstehende «Eglis»
  zwei Saetze weiter nicht. Echte Daten gehen mit.
- **Zurueck** (Rueckstand): Das Modell schreibt den Ersatznamen gebeugt («Hoi
  Senad»), die Ruecksetzung findet ihn nicht. Ein fremder, plausibler Name bleibt
  im Ergebnis -- schwer zu erkennen, weil nichts nach Fehler aussieht.

Die Konsequenz haengt nicht am Fehler, sondern daran, **ob ein Mensch den Text
sieht, bevor er wirkt** (siehe ``app/services/anon_politik``): Der Agent-Job
bricht ab und laeuft lokal, die Finanzanalyse warnt in der Vorschau.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services import anon_politik
from app.services.hermes_worker import (
    _anonymize_for_cloud,
    _deanonymize_from_cloud,
    _schleuse_nach_draussen,
)

_KEYS = {
    "session_id": "s1",
    "mappings": {
        "Senad Weibel": "Gabriel Brunner",
        "Hess & Partner": "InnoSmith GmbH",
    },
    "entity_types": {"Senad Weibel": "PERSON", "Hess & Partner": "ORG"},
}


def _cc(**antworten):
    """Ersetzt contentConverter durch feste Antworten je Werkzeugname."""

    async def ruf(name, **_kwargs):
        if name not in antworten:
            raise AssertionError(f"unerwarteter Werkzeugaufruf: {name}")
        wert = antworten[name]
        if isinstance(wert, Exception):
            raise wert
        return wert

    return patch("ai9.content_converter.call_tool", side_effect=ruf)


def _store(keys):
    return patch("ai9.mapping_store.get_mapping_keys", return_value=keys)


# ── Die Politik selbst ───────────────────────────────────────────────────────


def test_ahv_und_uid_werden_maskiert():
    """AHV und UID fehlten in allen drei Listen -- nie abgewaehlt, nur vergessen.

    Der Regressionswert dieses Tests liegt nicht in den beiden Namen, sondern
    darin, dass die Liste jetzt an einem Ort steht. Wer sie kuerzt, kommt hier
    vorbei.
    """
    assert "AHV" in anon_politik.ENTITAETEN
    assert "UID" in anon_politik.ENTITAETEN


def test_schwelle_ueberlaesst_die_vorgabe_dem_paket():
    """Hier stand einmal ``SCHWELLE < 0.4`` -- und der Test schuetzte eine Illusion.

    Der Gedanke war richtig: Ein zu viel maskiertes Sachwort ist sichtbar, ein
    uebersehener Name ist ein stiller Abfluss, die Fehlerarten sind nicht
    gleichwertig. Nur bewirkte die Zahl 0.25 nichts. Sie ist ein **Nachfilter**
    und kann nur nach oben wirken; das Erkennungsmodell schneidet bei 0.5 ab und
    gibt darunter nichts aus. Gemessen an der Eval-Menge von contentConverter
    ergab 0.25 zeichengleich dasselbe wie 0.4 -- der Regler war tot, und dieser
    Test bestaetigte ihm jeden Tag, dass er lebt.

    Unentdeckt blieb es, weil beide Seiten fuer sich stimmten: Die Zahl war
    kleiner als die Vorgabe, und die Anonymisierung funktionierte. Gemessen wurde
    nie, ob die Zahl einen Unterschied macht.

    ``None`` heisst jetzt: Die Vorgabe kommt aus dem Paket. Wer hier wieder eine
    Zahl eintraegt, belegt sie mit einem Lauf von ``run_eval.py``.
    """
    assert anon_politik.SCHWELLE is None


# ── Hinaus: Restbestaende ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maskieren_meldet_restbestaende():
    ergebnis = {"anonymized_text": "Wir prüfen Eglis Antrag.", "mapping_keys": _KEYS}
    with _cc(anonymize_content=ergebnis, find_residual_originals=["Eglis"]):
        _text, _sid, _diff, reste = await anon_politik.maskiere("...")
    assert reste == ["Eglis"]


@pytest.mark.asyncio
async def test_ein_einzelner_restbestand_bleibt_ein_name():
    """MCP liefert bei genau einem Fund eine Zeichenkette statt einer Liste.

    Die Form des Ergebnisses hing damit an der Anzahl der Funde. Ein
    ``[str(r) for r in reste]`` zerlegte «Eglis» dann in fuenf Buchstaben --
    gemeldet wurde etwas, das niemand als den uebersehenen Namen erkennt, und
    der Abbruch stuetzte sich auf fuenf Scheinfunde statt auf einen echten.
    """
    ergebnis = {"anonymized_text": "Wir prüfen Eglis Antrag.", "mapping_keys": _KEYS}
    with _cc(anonymize_content=ergebnis, find_residual_originals="Eglis"):
        _text, _sid, _diff, reste = await anon_politik.maskiere("...")
    assert reste == ["Eglis"]


@pytest.mark.asyncio
async def test_nicht_pruefbar_gilt_als_fund():
    """Ein nicht geprueter Text ist kein sauberer Text.

    Faellt contentConverter aus, waere die bequeme Annahme «keine Treffer». Sie
    haette hier die falsche Richtung: Der Aufrufer schickt den Text dann hinaus,
    obwohl niemand hingesehen hat.
    """
    ergebnis = {"anonymized_text": "...", "mapping_keys": _KEYS}
    with _cc(anonymize_content=ergebnis, find_residual_originals=RuntimeError("weg")):
        _text, _sid, _diff, reste = await anon_politik.maskiere("...")
    assert reste  # nicht leer


@pytest.mark.asyncio
async def test_agent_job_bricht_bei_restbestand_ab():
    """Unbeaufsichtigter Weg: Abbruch, nicht Warnung.

    Der Auftrag laeuft dann lokal. Eine Warnung waere hier wertlos -- sie wuerde
    gelesen, nachdem der Text bereits beim Cloud-Anbieter liegt.
    """
    ergebnis = {"anonymized_text": "...", "mapping_keys": _KEYS}
    with _cc(anonymize_content=ergebnis, find_residual_originals=["Eglis"]):
        with pytest.raises(RuntimeError, match="Bruchstuecke"):
            await _anonymize_for_cloud("...")


@pytest.mark.asyncio
async def test_sauberer_text_geht_durch():
    ergebnis = {"anonymized_text": "Hoi Senad Weibel", "mapping_keys": _KEYS}
    with _cc(anonymize_content=ergebnis, find_residual_originals=[]):
        text, session_id = await _anonymize_for_cloud("Hoi Gabriel Brunner")
    assert text == "Hoi Senad Weibel"
    assert session_id


# ── Zurueck: Rueckstaende ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sauberer_ruecklauf_meldet_nichts():
    with _store(_KEYS), _cc(
        deanonymize_content="Hoi Gabriel Brunner", find_residual_fakes=[]
    ):
        text, rueckstaende = await _deanonymize_from_cloud("Hoi Senad Weibel", "s1")
    assert text == "Hoi Gabriel Brunner"
    assert rueckstaende == []


@pytest.mark.asyncio
async def test_verkuerzter_ersatzname_wird_gemeldet():
    """Der eigentliche Schadensfall: nur der Vorname des Ersatznamens bleibt stehen.

    Die Pruefung liegt seit dem 24.08.2026 in contentConverter
    (``find_residual_fakes``) statt in einer eigenen Heuristik hier. Der Grund
    ist nicht Aufraeumen: Die hiesige Heuristik kannte die Beugungsregeln nicht,
    die der Anonymisierer beim Ersetzen selbst anwendet -- sie suchte «Senad
    Weibel» und uebersah «Weibels». Wer maskiert, muss auch pruefen, sonst laufen
    zwei Vorstellungen davon auseinander, was ein Rueckstand ist, und die
    stillere von beiden gewinnt.
    """
    with _store(_KEYS), _cc(
        deanonymize_content="Hoi Senad, besten Dank", find_residual_fakes=["Senad"]
    ):
        _text, rueckstaende = await _deanonymize_from_cloud("...", "s1")
    assert rueckstaende == ["Senad"]


@pytest.mark.asyncio
async def test_ein_einzelner_rueckstand_bleibt_ein_name():
    """Dieselbe Falle wie bei den Restbestaenden, in der stummeren Richtung.

    MCP liefert bei genau einem Fund eine Zeichenkette statt einer Liste. Ein
    ``[str(r) for r in rueckstaende]`` machte daraus fuenf Buchstaben. Hier
    stuende am Ende ein erfundener, voellig plausibler Name in einem Schreiben
    an den Mandanten, und die Warnung davor waere unlesbar.
    """
    with _store(_KEYS), _cc(
        deanonymize_content="Hoi Senad, besten Dank", find_residual_fakes="Senad"
    ):
        _text, rueckstaende = await _deanonymize_from_cloud("...", "s1")
    assert rueckstaende == ["Senad"]


@pytest.mark.asyncio
async def test_abgelaufenes_mapping_gibt_maskierten_text_mit_warnung():
    """Frueher gab dieser Fall stillschweigend den maskierten Text zurueck.

    Weil die Maskierung Ersatznamen statt Platzhalter verwendet, las der sich
    vollkommen unauffaellig: ein Bericht ueber «Hess & Partner», wo «InnoSmith
    GmbH» stehen muesste, gibt keinen Anlass zu zweifeln. Der Rueckstand ist das
    einzige Anzeichen, das der Aufrufer noch hat.
    """
    with _store(None):
        text, rueckstaende = await _deanonymize_from_cloud("Hoi Senad Weibel", "s1")
    assert text == "Hoi Senad Weibel"  # unveraendert, also maskiert
    assert rueckstaende  # aber als solcher gekennzeichnet


@pytest.mark.asyncio
async def test_gescheiterte_rueckbildung_meldet_rueckstand():
    with _store(_KEYS), _cc(deanonymize_content=RuntimeError("weg")):
        text, rueckstaende = await _deanonymize_from_cloud("Hoi Senad Weibel", "s1")
    assert text == "Hoi Senad Weibel"
    assert rueckstaende


# ── Die Schleuse: gilt fuer jeden Job-Typ, nicht nur fuer den Entwurf ────────

_LOKAL = object()
_CLOUD = object()


@contextmanager
def _schleuse_umgebung(maskieren):
    """Die Umgebung eines Worker-Jobs mit Cloud-Override.

    Gepatcht wird ``anon_politik.maskiere`` und nicht mehr ein Helfer im Worker:
    Seit dem 25.08.2026 entscheidet ``app.services.schleuse`` -- dieselbe Stelle,
    die der Chat befragt --, und der Worker sagt ihr nur noch, dass sein Weg
    unbeaufsichtigt ist. Bliebe der Test am alten Helfer, prueefte er eine
    Abzweigung, die niemand mehr nimmt.
    """
    with patch.multiple(
        "app.services.hermes_worker",
        _is_local_model=lambda m: m.startswith("ollama/"),
        _build_cloud_job_agent=lambda m: _CLOUD,
    ), patch("app.services.anon_politik.maskiere", maskieren):
        yield


@pytest.mark.asyncio
async def test_meeting_mit_cloud_override_geht_maskiert_hinaus():
    """Der Vorfall: Die Meeting-Nachanalyse schickte das Transkript im Klartext.

    Das Meeting ist hier nur das Beispiel. Geprueft wird, dass die Schleuse den
    Job-Typ gar nicht ansieht -- der Entwurfspfad hatte seine Maskierung, weil
    dort jemand daran gedacht hatte, das Protokoll hatte keine, weil dort
    niemand daran gedacht hatte. Eine Regel, die bei jeder Erweiterung mitgedacht
    werden muss, ist keine Regel.
    """
    meta = {"llm_override": "anthropic/opus", "meeting_transcript_id": "M1"}
    with _schleuse_umgebung(AsyncMock(return_value=("MASKIERT", "S1", [], []))):
        agent, prompt, session = await _schleuse_nach_draussen(
            _LOKAL, "Transkript mit Gabriel Brunner", meta, "job1"
        )

    assert agent is _CLOUD
    assert prompt == "MASKIERT"
    assert session == "S1"


@pytest.mark.asyncio
async def test_gescheiterte_maskierung_haelt_den_job_im_haus():
    """Fail-closed: lieber langsam lokal als schnell nach draussen.

    Der Rueckfall gibt den **urspruenglichen** Prompt zurueck, nicht einen halb
    maskierten -- lokal gibt es nichts zu verbergen, und ein beschaedigter Prompt
    waere ein zweiter Fehler.
    """
    meta = {"llm_override": "anthropic/opus"}
    with _schleuse_umgebung(AsyncMock(side_effect=RuntimeError("Modell weg"))):
        agent, prompt, session = await _schleuse_nach_draussen(
            _LOKAL, "Transkript mit Gabriel Brunner", meta, "job1"
        )

    assert agent is _LOKAL
    assert prompt == "Transkript mit Gabriel Brunner"
    assert session is None


@pytest.mark.asyncio
async def test_restbestaende_halten_den_job_im_haus():
    """Unbeaufsichtigt heisst: Bruchstuecke sind ein Abbruch, keine Warnung.

    Der Job laeuft, waehrend niemand hinsieht. Eine Warnung, die erst danach
    jemand liest, aendert nichts mehr -- anders als im Chat, wo derselbe Befund
    dem Menschen vorgelegt wird, weil er davor sitzt.
    """
    meta = {"llm_override": "anthropic/opus"}
    with _schleuse_umgebung(
        AsyncMock(return_value=("Fast maskiert", "S1", [], ["Brunner"]))
    ):
        agent, prompt, session = await _schleuse_nach_draussen(
            _LOKAL, "Transkript mit Gabriel Brunner", meta, "job1"
        )

    assert agent is _LOKAL
    assert prompt == "Transkript mit Gabriel Brunner"
    assert session is None


@pytest.mark.asyncio
async def test_lokaler_override_wird_nicht_maskiert():
    """Im Haus bleibt der Klartext -- Maskierung kostet dort nur Kontext."""
    maskieren = AsyncMock()
    meta = {"llm_override": "ollama/qwen3.6:latest"}
    with _schleuse_umgebung(maskieren):
        agent, prompt, session = await _schleuse_nach_draussen(
            _LOKAL, "Klartext", meta, "job1"
        )

    assert (agent, prompt, session) == (_LOKAL, "Klartext", None)
    maskieren.assert_not_awaited()


@pytest.mark.asyncio
async def test_ohne_override_passiert_nichts():
    maskieren = AsyncMock()
    with _schleuse_umgebung(maskieren):
        agent, prompt, session = await _schleuse_nach_draussen(_LOKAL, "Klartext", {}, "j")

    assert (agent, prompt, session) == (_LOKAL, "Klartext", None)
    maskieren.assert_not_awaited()


@pytest.mark.asyncio
async def test_mapping_verlaesst_das_backend_nicht():
    """Die Rueckbildung nimmt eine Session-Kennung, keine Klartextwerte.

    Sonst reisten die Originalwerte durch Antwortkoerper und Frontend-Zustand --
    und damit ausgerechnet durch die Schichten, vor denen die Maskierung sie
    schuetzen soll.
    """
    gesehen = {}

    async def ruf(name, **kwargs):
        gesehen[name] = kwargs
        return "Hoi Gabriel Brunner" if name == "deanonymize_content" else []

    with _store(_KEYS), patch("ai9.content_converter.call_tool", side_effect=ruf):
        await anon_politik.bilde_zurueck("Hoi Senad Weibel", "s1")

    assert gesehen["deanonymize_content"]["mapping_keys"] is _KEYS
