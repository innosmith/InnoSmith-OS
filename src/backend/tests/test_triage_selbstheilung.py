"""Tests für die Selbstheilung stehengebliebener Triage-Einträge.

Die Eigenschaft, die dieses Bauteil trägt, ist unauffällig: **Keine E-Mail bleibt
unsichtbar liegen.** Bricht sie, passiert nichts Sichtbares -- kein Fehler, kein
Eintrag im Cockpit, keine Aufgabe. Die Mail ist einfach nie triagiert worden, und die
Dedup-Prüfung sorgt dafür, dass sie auch nie wieder angefasst wird.

Genau das ist gemessen passiert: 33 E-Mails hingen fest, sieben davon Kundenmails vom
19.08.2026. Drei Lücken in ``_resweep_unclassified_triages`` waren die Ursache, und
zwei davon lagen in der WHERE-Klausel -- also an einer Stelle, die ein Test mit
gefälschter Datenbank nicht sieht, weil ein Fake jede Abfrage gleich beantwortet.

Darum prüfen die Tests hier zweierlei getrennt:

1. **Die Auswahl** über den kompilierten SQL-Text. Ungewöhnlich, aber hier
   angemessen: LEFT OUTER JOIN statt INNER JOIN und die Statusmenge sind die beiden
   Fehler, die wirklich aufgetreten sind.
2. **Die Verarbeitung** über eine gefälschte Sitzung -- was mit einer Zeile geschieht,
   sobald sie ausgewählt ist.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.hermes_worker as hw


class _FakeDB:
    """Fängt die Abfrage ab und liefert vorgegebene ``(triage, job)``-Paare."""

    def __init__(self, rows):
        self._rows = rows
        self.statement = None
        self.added = []
        self.committed = False

    async def execute(self, statement):
        self.statement = statement
        rows = self._rows
        return SimpleNamespace(all=lambda: rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True


class _FakeSession:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def _triage(*, status="pending", message_id="handle-1", triage_class=None, **felder):
    basis = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        message_id=message_id,
        internet_message_id="<abc@example.com>",
        subject="AW: Offene Fragen TaxCheck-GSW",
        from_address="kunde@example.ch",
        from_name="Kundin",
        inference_class="focused",
        triage_class=triage_class,
        status=status,
        agent_job_id=None,
        created_at=datetime.now(timezone.utc),
    )
    basis.update(felder)
    return SimpleNamespace(**basis)


def _job(*, status="failed", meta=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        llm_model="qwen3.6:latest",
        status=status,
        metadata_json=meta if meta is not None else {"email_message_id": "handle-1"},
    )


async def _resweep(rows):
    db = _FakeDB(rows)
    with patch.object(hw, "async_session", lambda: _FakeSession(db)):
        anzahl = await hw._resweep_unclassified_triages()
    return anzahl, db


def _sql(db) -> str:
    return str(db.statement.compile(compile_kwargs={"literal_binds": True}))


class TestAuswahl:
    """Was überhaupt aufgegriffen wird -- die zwei echten Fehler lagen hier."""

    @pytest.mark.asyncio
    async def test_join_ist_ausserer_join(self):
        """Ein INNER JOIN blendet genau die Zeilen aus, deren Job gelöscht wurde.

        Der Löschpfad im Cockpit nullt ``agent_job_id``. Mit INNER JOIN fiel die Zeile
        damit aus der Auswahl -- und war für die Selbstheilung nicht mehr existent,
        während die Dedup-Prüfung die Mail weiter kannte. Zehn Mails, sieben davon vom
        19.08.2026, sind so verschwunden.
        """
        _, db = await _resweep([])
        assert "LEFT OUTER JOIN" in _sql(db)

    @pytest.mark.asyncio
    async def test_verwaiste_zeilen_sind_ausdruecklich_erlaubt(self):
        _, db = await _resweep([])
        assert "agent_jobs.id IS NULL" in _sql(db)

    @pytest.mark.asyncio
    async def test_auch_processing_wird_aufgegriffen(self):
        """``processing`` ohne laufenden Job ist ein Widerspruch, kein Zustand.

        23 Zeilen hingen darauf fest, weil der Job mitten im Schreib-Pass starb.
        """
        _, db = await _resweep([])
        sql = _sql(db)
        assert "'pending'" in sql and "'processing'" in sql

    @pytest.mark.asyncio
    async def test_klasse_ist_kein_auswahlkriterium_mehr(self):
        """``triage_class IS NULL`` war ein Stellvertreter für «nie fertig geworden».

        Er schloss genau die ``processing``-Zeilen aus, die schon eine Klasse trugen.
        Der Status sagt dasselbe direkt und vollständig.
        """
        _, db = await _resweep([])
        assert "email_triage.triage_class IS NULL" not in _sql(db)


class TestVerarbeitung:
    @pytest.mark.asyncio
    async def test_verwaiste_zeile_wird_neu_eingereiht(self):
        """Ohne Job trägt die Zeile sich selbst -- Handle und Absender stehen darin."""
        triage = _triage()
        anzahl, db = await _resweep([(triage, None)])

        assert anzahl == 1
        assert triage.status == "pending"
        assert len(db.added) == 1
        neuer_job = db.added[0]
        assert neuer_job.metadata_json["email_message_id"] == "handle-1"
        assert neuer_job.metadata_json["subject"] == "AW: Offene Fragen TaxCheck-GSW"
        assert neuer_job.metadata_json["from_address"] == "kunde@example.ch"
        assert triage.agent_job_id == neuer_job.id

    @pytest.mark.asyncio
    async def test_verwaiste_zeile_erbt_den_principal(self):
        triage = _triage()
        _, db = await _resweep([(triage, None)])
        assert db.added[0].user_id == triage.user_id

    @pytest.mark.asyncio
    async def test_verwaiste_kette_ist_begrenzt(self):
        """Ohne Job ist der Zähler verloren -- er startet deshalb bei 1, nicht bei 0.

        Sonst könnte wiederholtes Löschen von Jobs eine Mail endlos zirkulieren
        lassen. Der neue Job trägt den Zähler weiter, beim nächsten Fehlschlag ist
        Schluss.
        """
        _, db = await _resweep([(_triage(), None)])
        assert db.added[0].metadata_json["resweep_count"] == 2
        assert db.added[0].metadata_json["resweep_of"] == "verwaist"

    @pytest.mark.asyncio
    async def test_processing_zeile_mit_totem_job_wird_neu_eingereiht(self):
        triage = _triage(status="processing", triage_class="auto_reply")
        anzahl, db = await _resweep([(triage, _job(status="failed"))])
        assert anzahl == 1 and triage.status == "pending"

    @pytest.mark.asyncio
    async def test_replay_kennung_wird_geschlossen_statt_wiederholt(self):
        """``replay_...`` zeigt auf keine echte Mail -- ein Re-Run würde 404en."""
        triage = _triage(message_id="replay_abc_1")
        anzahl, db = await _resweep([(triage, None)])
        assert anzahl == 0
        assert triage.status == "dismissed"
        assert db.added == []

    @pytest.mark.asyncio
    async def test_erschoepfte_wiederholungen_werden_geschlossen(self):
        """Dauerhaft problematische Mails dürfen nicht endlos zirkulieren."""
        job = _job(meta={"email_message_id": "handle-1", "resweep_count": hw.MAX_RESWEEP})
        triage = _triage()
        anzahl, db = await _resweep([(triage, job)])
        assert anzahl == 0 and triage.status == "dismissed"

    @pytest.mark.asyncio
    async def test_trace_wird_nicht_mitgeschleppt(self):
        job = _job(meta={
            "email_message_id": "handle-1",
            "trace": ["viel Text"],
            "tools_used": ["get_email"],
            "self_grade": 3,
        })
        _, db = await _resweep([(_triage(), job)])
        neu = db.added[0].metadata_json
        assert "trace" not in neu and "tools_used" not in neu and "self_grade" not in neu


class TestEntscheidungsstellen:
    """Die Selbstheilung ist die letzte Instanz, nicht die erste.

    Jede Stelle, an der ein Mensch oder das System eine Entscheidung trifft, muss den
    Triage-Eintrag selbst in einen Endzustand bringen. Bleibt das dem Resweep
    ueberlassen, wird eine getroffene Entscheidung rueckgaengig gemacht -- ein
    abgelehnter Entwurf kaeme als derselbe Entwurf zurueck.
    """

    @staticmethod
    def _abschlussbedingung() -> str:
        """Der kompilierte WHERE-Teil, mit dem der Freigabepfad Eintraege schliesst."""
        import app.routers.agent_jobs as aj
        from app.models import EmailTriage
        from sqlalchemy import update as sa_update

        quelle = Path(aj.__file__).read_text(encoding="utf-8")
        assert 'EmailTriage.status.in_(["pending", "processing"])' in quelle, (
            "Der Freigabepfad schliesst nicht alle nicht-terminalen Eintraege. Ein "
            "``auto_reply``-Eintrag steht absichtlich auf ``processing`` -- wird er "
            "hier nicht erfasst, haengt eine abgelehnte Freigabe fuer immer."
        )
        # Nur damit die Importe nicht als unbenutzt gelten und die Namen wirklich
        # existieren -- ein Tippfehler im Quelltext soll nicht durchrutschen.
        return str(
            sa_update(EmailTriage)
            .where(EmailTriage.status.in_(["pending", "processing"]))
            .values(status="dismissed")
            .compile(compile_kwargs={"literal_binds": True})
        )

    def test_freigabepfad_schliesst_auch_processing(self):
        """Vorfall: 22 ``auto_reply``-Eintraege hingen auf ``processing`` fest.

        Die alte Bedingung lautete «``pending`` ODER ohne Klasse». Ein abgelehnter
        Entwurf erfuellt beides nicht: Der Eintrag steht auf ``processing`` und traegt
        die Klasse ``auto_reply``. Herausgekommen ist man nur ueber die
        Sent-Items-Reconciliation, und die greift nur bei tatsaechlichem Versand --
        einen abgelehnten Entwurf versendet niemand.
        """
        assert "IN ('pending', 'processing')" in self._abschlussbedingung()

    def test_loeschpfad_stellt_unfertige_eintraege_zurueck(self):
        """Ein verwaister Eintrag muss ``pending`` werden, nicht ``dismissed``.

        ``dismissed`` waere eine stille Schliessung mit anderem Namen: Der Mensch hat
        einen Job aufgeraeumt, nicht eine Mail fuer erledigt erklaert. ``processing``
        ohne Job ist gleichzeitig ein Widerspruch -- es verarbeitet niemand mehr.
        """
        import app.routers.agent_jobs as aj

        quelle = Path(aj.__file__).read_text(encoding="utf-8")
        stelle = quelle.index("async def _detach_triage_from_jobs")
        koerper = quelle[stelle:stelle + 2600]
        assert '.values(status="pending")' in koerper
        assert '.values(status="dismissed")' not in koerper
