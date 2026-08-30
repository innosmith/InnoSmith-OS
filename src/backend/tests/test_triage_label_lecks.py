"""Kein Schreibpfad darf ein erfundenes Wort ins Feld ``label`` legen.

``triage_labels.py`` ist die einzige Quelle des Vokabulars, und ``normalize_agent_label``
bewacht sie -- aber nur gegenüber dem **Modell**. Die deterministischen Pfade schreiben
``suggested_action`` direkt, und dort sind über die Zeit drei Wörter hineingewandert,
die keine Kategorie sind: ``Aufgabe`` (Fahnen-Aufgriff), ``Duplikat`` und ``Verworfen``
(Unterdrückungspfade).

Der Schaden ist doppelt und beides Mal still:

- **Die Statistik lügt.** ``Aufgabe`` war gemessen die dritthäufigste «Kategorie» im
  Bestand, ohne je eine zu sein. Wer die Verteilung liest, um zu beurteilen, ob
  ``Finanzen`` zu selten vergeben wird, rechnet mit Phantomen.
- **Die echte Kategorie verschwindet.** ``Duplikat`` und ``Verworfen`` **ersetzten**
  das ganze ``suggested_action``-Objekt, also auch das Label, das das Modell zuvor
  korrekt bestimmt hatte.

Der Grundsatz: Das Feld ``label`` beantwortet «worum geht es thematisch». Ein
deterministischer Pfad, der kein Modell ruft, weiss das nicht -- und darf dann nichts
hineinschreiben. Was er weiss (dass eine Regel griff, dass ein Duplikat vorlag), gehört
in eigene Felder wie ``deterministic_override`` oder ``deduplicated``.
"""

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.triage as triage_service
from app.services.triage_labels import TRIAGE_LABELS

SCHREIBPFADE = ("services/triage.py", "services/hermes_worker.py")
APP = Path(__file__).resolve().parents[1] / "app"


def test_no_write_path_invents_a_label():
    """Jedes wörtlich geschriebene ``label`` muss im Vokabular stehen.

    Absichtlich eine Quelltext-Prüfung: Der Fehler entsteht beim Hinschreiben eines
    hübsch klingenden Wortes, nicht zur Laufzeit. Ein Verhaltenstest müsste jeden
    Schreibpfad einzeln kennen und würde den nächsten neuen übersehen -- genau so sind
    diese drei entstanden.

    Werte mit ``{`` sind Platzhalter in Prompt-Vorlagen und keine Zuweisungen.
    """
    muster = re.compile(r'"label":\s*"([^"]+)"')
    verstoesse: list[str] = []
    for rel in SCHREIBPFADE:
        pfad = APP / rel
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if zeile.lstrip().startswith("#"):
                continue
            for wert in muster.findall(zeile):
                if "{" in wert or wert in TRIAGE_LABELS:
                    continue
                verstoesse.append(f"{rel}:{nr}: label = {wert!r}")
    assert not verstoesse, (
        "Diese Schreibpfade legen ein Wort ins Feld 'label', das keine Kategorie ist. "
        "Die Tatsache gehört in ein eigenes Feld:\n" + "\n".join(verstoesse)
    )


class TestFahnenAufgriff:
    """Der Aufgriff weiss, dass Arbeit ansteht -- nicht, um was es geht."""

    @staticmethod
    async def _aufgriff():
        mail = {
            "id": "handle-1",
            "internetMessageId": "<abc@example.com>",
            "subject": "Bitte um Rückmeldung Projekt NITL",
            "from": {"emailAddress": {"address": "kunde@example.ch", "name": "Kundin"}},
            "bodyPreview": "Guten Tag Herr Smith",
            "receivedDateTime": "2026-08-30T08:00:00Z",
            "inferenceClassification": "focused",
        }
        db = SimpleNamespace(add=lambda obj: erfasst.append(obj), flush=AsyncMock())
        erfasst: list = []
        with (
            patch.object(
                triage_service, "system_principal_id", AsyncMock(return_value=uuid.uuid4())
            ),
            patch(
                "app.services.hermes_worker._create_email_task",
                AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), title="T")),
            ),
        ):
            await triage_service._create_task_from_flag(db, mail)
        return erfasst[0]

    @pytest.mark.asyncio
    async def test_writes_no_label(self):
        eintrag = await self._aufgriff()
        assert "label" not in eintrag.suggested_action, (
            "Ein Wort wie 'Aufgabe' wäre eine Behauptung über das Thema -- und kein "
            "gültiges Label: es steht nicht in TRIAGE_LABELS und existiert in Outlook "
            "nicht."
        )

    @pytest.mark.asyncio
    async def test_records_the_reason_it_does_know(self):
        """Was der Pfad tatsächlich weiss, muss er festhalten -- sonst ist er stumm."""
        eintrag = await self._aufgriff()
        assert eintrag.suggested_action["deterministic_override"] == "manual_flag"
        assert eintrag.triage_class == "task"


def test_suppression_paths_merge_instead_of_replacing():
    """``Duplikat`` und ``Verworfen`` dürfen die Kategorie des Modells nicht löschen.

    Beide Pfade laufen **nach** dem vollständigen Schreiben von ``suggested_action``.
    Ein Ersetzen wirft dort das korrekt bestimmte Label weg; der JSONB-Merge (``||``)
    behält es und ergänzt nur die Tatsache.
    """
    zeilen = (APP / "services" / "hermes_worker.py").read_text(encoding="utf-8").splitlines()
    # Kommentare weg: Sie erklären den Vorfall und nennen die Merkmale dabei
    # zwangsläufig -- geprüft wird der Code, nicht die Erklärung darüber.
    code = "\n".join(z for z in zeilen if not z.lstrip().startswith("#"))
    for merkmal in ("suppressed_by_dismissal", "deduplicated"):
        stelle = code.index(merkmal)
        umgebung = code[max(0, stelle - 400):stelle]
        assert 'op("||")' in umgebung, (
            f"Der Pfad um '{merkmal}' ersetzt suggested_action vollständig, statt zu "
            "ergänzen -- das Label des Modells geht dabei verloren."
        )
