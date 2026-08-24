"""Tests fuer die Kontext-Recherche vor dem Schreib-Pass (Pass 2a).

Hintergrund (Messung vom 03.08.2026): Der Produktions-Schreibauftrag wies nirgends
an, Fachkontext zu suchen. Gegen qwen3.6 rief das Modell die semantische Suche in
**0 von 5** Laeufen auf; mit expliziter Anweisung in **5 von 5**. Die duennen
Entwuerfe waren damit weder Tool-Calling-Defekt noch Modellentscheid, sondern eine
fehlende Anweisung -- passend zu 1 von 34 Produktions-Jobs mit Kontextsuche und
einer Clean-Rate von 8.6 %.

Geprueft wird die *Logik* rundherum, nicht LLM-Ausgabe:
- ``_context_need``: konditionales Gate (Terminanfragen recherchieren nicht).
- ``_sender_org_hint``: Suchbaustein aus der Absenderdomaene.
- ``_collect_context_sources``: Provenance aus verschachtelten Tool-Ergebnissen.
- ``render_gather_task``/``render_dossier_block``: Prompt-Bausteine.
- ``_generate_reply_draft``: Sammeln vor Schreiben, Ueberspringen bei Termin.
- Cloud-Pfad: fail-closed auf lokal, wenn die Anonymisierung scheitert.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.hermes_worker as hw
from app.services.draft_prompt import (
    render_dossier_block,
    render_gather_task,
)


def _settings(**over):
    base = dict(
        draft_context_research=True,
        draft_context_temperature=0.2,
        draft_context_max_sources=10,
        draft_context_max_chars=6000,
        draft_context_wide_tools=False,
        draft_model="",
        draft_temperature=0.7,
        draft_top_p=0.8,
        draft_top_k=20,
        draft_presence_penalty=0.0,
        draft_reasoning_effort="none",
        two_pass_draft=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── _context_need: das konditionale Gate ─────────────────────────────────────


def test_context_need_defaults_to_substance():
    """Ohne Signal wird recherchiert: eine ausgelassene Recherche kostet den Entwurf,
    eine ueberfluessige nur Zeit."""
    assert hw._context_need({"subject": "Rückfrage zum Testreport"}, None) == "substance"


def test_context_need_detects_scheduling_and_skips_research():
    """Anthonys Beispiel: «Wann hast du freie Zeit?» braucht den Kalender, sonst nichts.

    Ohne diese Verzweigung liefe fuer jede Terminanfrage eine Fachrecherche, die
    nichts findet und nur Latenz kostet -- und im schlechten Fall fachfremden
    Kontext in eine reine Terminantwort traegt.
    """
    meta = {"subject": "Terminvorschlag", "body_preview": "Wann hast du nächste Woche Zeit?"}
    assert hw._context_need(meta, None) == "calendar"


def test_context_need_respects_explicit_value_from_pass_one():
    """Eine ausdrueckliche Angabe aus der Klassifikation hat Vorrang vor der Heuristik."""
    meta = {"subject": "Terminvorschlag", "body_preview": "Wann hast du Zeit?"}
    assert hw._context_need(meta, {"context_need": "substance"}) == "substance"
    assert hw._context_need({"subject": "X"}, {"context_need": "none"}) == "none"


def test_context_need_ignores_invalid_value():
    assert hw._context_need({"subject": "X"}, {"context_need": "vielleicht"}) == "substance"


# ── _sender_org_hint: Suchbaustein aus der Domaene ───────────────────────────


def test_sender_org_hint_extracts_company_label():
    assert hw._sender_org_hint("franziska.koenig@swissbankers.ch") == "swissbankers"
    assert hw._sender_org_hint("a.b@sub.example.com") == "sub"


def test_sender_org_hint_skips_freemail():
    """«gmail» als Suchbegriff erzeugt nur Rauschen -- besser gar kein Baustein."""
    for addr in ("x@gmail.com", "y@bluewin.ch", "z@gmx.net"):
        assert hw._sender_org_hint(addr) == ""


def test_sender_org_hint_tolerates_garbage():
    assert hw._sender_org_hint("") == ""
    assert hw._sender_org_hint("kein-at-zeichen") == ""


# ── Provenance: Quellen aus verschachtelten Tool-Ergebnissen ─────────────────


def test_collect_context_sources_parses_nested_mcp_result():
    """Hermes wrappt das MCP-Ergebnis, dessen Text wiederum JSON enthaelt."""
    hw._job_context_sources.clear()
    result = {
        "result": '{"results": ['
        '{"title": "KreditorenBot Release", "source_type": "email", "from": "f@sb.ch"},'
        '{"title": "Pendenzen.pdf", "source_type": "onedrive"}]}'
    }
    hw._collect_context_sources(result, limit=10)
    titles = [s["title"] for s in hw._job_context_sources]
    assert titles == ["KreditorenBot Release", "Pendenzen.pdf"]
    assert hw._job_context_sources[0]["from"] == "f@sb.ch"


def test_collect_context_sources_dedupes_and_respects_limit():
    hw._job_context_sources.clear()
    payload = {"results": [{"title": f"Dok {i}", "source_type": "onedrive"} for i in range(20)]}
    hw._collect_context_sources(payload, limit=3)
    hw._collect_context_sources(payload, limit=3)
    assert len(hw._job_context_sources) == 3


def test_collect_context_sources_ignores_unparsable():
    """Provenance ist Beiwerk -- ein unlesbares Ergebnis darf nichts kaputt machen."""
    hw._job_context_sources.clear()
    hw._collect_context_sources("kein json", limit=10)
    hw._collect_context_sources(None, limit=10)
    assert hw._job_context_sources == []


# ── Prompt-Bausteine ─────────────────────────────────────────────────────────


def test_gather_task_asks_for_narrow_queries_and_forbids_writing():
    """Der Sammel-Auftrag muss zwei Dinge klarstellen: suchen, aber nicht schreiben.

    Mehrere schmale Abfragen statt einer ueberladenen, weil die Keyword-Haelfte der
    Hybrid-Suche alle Begriffe mit UND verknuepft (live gemessen am 03.08.2026:
    «Franziska KreditorenBot Stammdaten» liefert 3 Treffer, «KreditorenBot» allein
    das gesamte Projektmaterial).
    """
    text = render_gather_task(
        today="Montag, 03.08.2026",
        subject="KreditorenBot Testreport",
        from_name="Franziska König",
        from_addr="franziska.koenig@swissbankers.ch",
        body_block="Kannst du die offenen Punkte erläutern?",
        sender_org="swissbankers",
        topic_hint="KreditorenBot Testreport",
    )
    assert "semantic_search_documents" in text
    assert "schmale Abfragen" in text
    assert "KEINE Antwort" in text and "KEINEN Entwurf" in text
    assert "swissbankers" in text
    assert "Nicht gefunden" in text


def test_dossier_block_distinguishes_found_from_nothing_found():
    with_hits = render_dossier_block("**Sachstand:** 49 Fälle geprüft.")
    assert "49 Fälle geprüft" in with_hits
    assert "FACHKONTEXT AUS DER RECHERCHE" in with_hits

    empty = render_dossier_block("")
    assert "NICHTS GEFUNDEN" in empty
    assert "keine erfundenen" in empty


def test_dossier_block_demands_use_of_the_facts():
    """Ein Dossier allein genuegt nicht -- es muss auch benutzt werden.

    Live-Vergleich vom 03.08.2026 (Fall «Schulungen KI», qwen3.6 in Produktion):
    Mit dem urspruenglichen, warnend formulierten Dossier-Abschnitt schrieb das
    Modell 253 Zeichen «melde mich nach den Ferien» und ignorierte den kompletten
    Sachstand -- gebuchtes Erstgespräch, Budgetgrenze, offene Klärungspunkte. Ohne
    Dossier waren es 501 Zeichen mit Substanz. Ursache war die Rahmung: der
    Abschnitt bestand ueberwiegend aus «offen» und «nicht gefunden», was das Modell
    als «ich weiss nichts» las. Die Aufforderung, das Bekannte konkret aufzugreifen,
    steht deshalb VOR den Warnungen -- und «nicht gefunden» wird ausdruecklich
    entschaerft.
    """
    text = render_dossier_block("**Sachstand:** Erstgespräch am 07.08. gebucht.")
    assert "NUTZE IHN" in text
    assert "Konkret werden" in text
    assert "kein Grund, auch das Bekannte wegzulassen" in text
    # Die Warnungen bleiben, aber nachgeordnet.
    assert text.index("Konkret werden") < text.index("Nichts erfinden")


def test_dossier_block_forbids_quoting_sources_in_the_mail():
    """Quellenangaben gehoeren in die Freigabe-Ansicht, nicht in die Kundenmail."""
    text = render_dossier_block("**Sachstand:** X (Quelle: Mail vom 1.7.)")
    assert "zitiere keine Quellenangaben" in text


def test_dossier_block_forbids_naming_other_clients():
    """Die Suche laeuft bewusst ohne harten Kundenfilter -- ein Filter kostet Recall
    und braucht Metadaten, die der Index nicht hat.

    Im Live-Lauf vom 03.08.2026 zog die Recherche zu einer Schulungsanfrage von
    Kunde A ein Dokument von Kunde B heran (T+R AG). Verallgemeinerte Erfahrung
    daraus ist erwuenscht, der Name des Dritten nicht. Zweite Sicherung ist die
    Quellenliste bei der Freigabe (``context_sources``).
    """
    text = render_dossier_block("**Sachstand:** Schulung bei Firma B durchgeführt.")
    assert "Andere Kunden bleiben ungenannt" in text
    assert "verallgemeinert" in text


def test_gather_task_routes_volatile_facts_to_the_specialist_system():
    """Der Ausloeser dieses Pakets: Am 03.08.2026 nannte ein Entwurf «14h Budget fuer
    Juli» als heutigen Stand -- die Zahl stammte aus einer Mail vom 02.07.2026.

    Zugang allein behebt das nicht. Der Agent hatte den Kalender schon vorher und
    fragte trotzdem das Archiv. Die Systemkarte sagt darum ausdruecklich, welches
    System fortlaufend veraenderliche Fakten beantwortet -- und dass ein Mailfund
    dazu ein datierter Hinweis ist, keine Tatsache.
    """
    text = render_gather_task(
        today="Heute", subject="Kapazität August", from_name="S", from_addr="s@sb.ch",
        body_block="Wie viele Stunden hast du noch?",
    )
    assert "get_capacity_overview" in text
    assert "get_absences" in text
    assert "älteren E-Mails" in text
    assert "keine Tatsache" in text


def test_gather_task_only_names_systems_the_agent_may_call():
    """Ein Verweis auf ein Werkzeug ausserhalb der Allowlist produziert Fehlversuche.

    Die Systemkarte waechst darum mit dem Werkzeug-Umfang mit.
    """
    kwargs = dict(
        today="Heute", subject="Angebot", from_name="S", from_addr="s@sb.ch", body_block="?",
    )
    narrow = render_gather_task(**kwargs)
    wide = render_gather_task(
        **kwargs,
        extra_systems="- Verkaufschancen, Deals, Angebotsstand → **search_deals**\n",
    )

    assert "search_deals" not in narrow
    assert "search_deals" in wide


def test_gather_task_demands_a_date_for_every_fact():
    """Ohne Datum ist eine zeitraumbezogene Angabe nicht pruefbar -- genau daran
    scheiterte der Juli-Fall. Die Suche liefert das Feld inzwischen mit."""
    text = render_gather_task(
        today="Heute", subject="Budget", from_name="S", from_addr="s@sb.ch", body_block="?",
    )
    assert "Stand womöglich veraltet" in text
    assert "`date`" in text


def test_dossier_block_binds_old_numbers_to_their_period():
    """Der Schreib-Pass darf eine Juli-Zahl nennen -- aber nur als Juli-Zahl."""
    text = render_dossier_block("**Stand womöglich veraltet:** 14h Juli-Budget (02.07.2026)")

    assert "Alte Zahlen bleiben alt" in text
    assert "nie als heutigen Stand" in text


def test_dossier_block_forbids_promising_available_budget():
    """Die Planung ist kein Vertragskontingent. Der Entwurf sagte «14h verfügbar» --
    eine Zusage, die aus Planungsstunden gar nicht folgt."""
    text = render_dossier_block("**Sachstand:** 14h geplant")

    assert "Vertragskontingent" in text


def test_gather_task_forbids_repeating_queries():
    """Ohne diesen Hinweis wiederholte das Modell im Live-Test dieselbe Abfrage
    dreimal. Die harte Absicherung ist das Iterationslimit (``_run_agent_sync``);
    der Hinweis spart die verschwendeten Runden davor."""
    text = render_gather_task(
        today="Montag, 03.08.2026", subject="X", from_name="Y",
        from_addr="y@firma.ch", body_block="Inhalt",
    )
    assert "Wiederhole nie eine Abfrage" in text


# ── Schreib-Prompt: Dossier nur, wenn auch recherchiert wurde ────────────────


def _draft_prompt_patches():
    return [
        patch.object(hw, "_style_skill_available", lambda: True),
        patch.object(hw, "_build_sender_style_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_rules_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_recall_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_style_anchor_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_absence_ranges", new=AsyncMock(return_value=[])),
        patch.object(hw, "_load_email_body_text", new=AsyncMock(return_value="Inhalt")),
    ]


@pytest.mark.asyncio
async def test_draft_prompt_embeds_dossier_when_researched():
    meta = {"email_message_id": "M1", "subject": "Testreport", "from_address": "f@sb.ch"}
    with patch.object(hw, "get_settings", lambda: _settings()):
        for p in _draft_prompt_patches():
            p.start()
        try:
            text = await hw._build_draft_prompt(
                meta, None, "**Sachstand:** Stammdaten-Logik geklärt.", researched=True
            )
        finally:
            patch.stopall()
    assert "Stammdaten-Logik geklärt" in text
    assert "FACHKONTEXT AUS DER RECHERCHE" in text


@pytest.mark.asyncio
async def test_draft_prompt_omits_dossier_section_when_not_researched():
    """Bei einer Terminanfrage waere «nichts gefunden» schlicht falsch -- es wurde
    ja gar nicht gesucht. Der Abschnitt entfaellt dann ersatzlos."""
    meta = {"email_message_id": "M1", "subject": "Termin", "from_address": "f@sb.ch"}
    with patch.object(hw, "get_settings", lambda: _settings()):
        for p in _draft_prompt_patches():
            p.start()
        try:
            text = await hw._build_draft_prompt(meta, None, None, researched=False)
        finally:
            patch.stopall()
    assert "FACHKONTEXT" not in text
    assert "NICHTS GEFUNDEN" not in text


# ── Orchestrierung: Sammeln vor Schreiben ────────────────────────────────────


def _writing_agent(draft_id="D1"):
    """Simuliert den Schreib-Lauf: das create_draft-Callback setzt die echte ID."""

    async def _run(*_args, **_kwargs):
        hw._job_created_draft_id = draft_id
        return ""

    return SimpleNamespace(to_thread=_run)


@pytest.mark.asyncio
async def test_generate_reply_draft_gathers_before_writing():
    meta = {"email_message_id": "M1", "subject": "Rückfrage Testreport", "from_address": "f@sb.ch"}
    gather = AsyncMock(return_value="**Sachstand:** X")
    build = AsyncMock(return_value="PROMPT")
    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_gather_draft_context", gather),
        patch.object(hw, "_build_draft_prompt", build),
        patch.object(hw, "asyncio", _writing_agent()),
    ):
        result = await hw._generate_reply_draft(meta, {"rationale": "Kunde fragt"})

    gather.assert_awaited_once()
    assert build.await_args.args[2] == "**Sachstand:** X"
    assert build.await_args.args[3] is True
    assert result == "D1"


@pytest.mark.asyncio
async def test_gather_events_are_tagged_before_the_writing_pass_starts():
    """Recherche- und Schreib-Events muessen im Trace unterscheidbar bleiben.

    ``_tag_trace_pass`` markiert alles noch Unmarkierte. Wird der Sammel-Lauf nicht
    unmittelbar danach markiert, faellt er im Cockpit unter «Entwurf» -- und die
    Frage «worauf stuetzt sich der Text?» ist wieder nur aus der Quellenliste zu
    beantworten, nicht aus dem Ablauf.
    """
    hw._job_trace.clear()
    hw._job_trace.append({"type": "tool_start", "name": "klassifikation", "pass": "classify"})

    async def gather(*_a, **_kw):
        hw._job_trace.append({"type": "tool_start", "name": "semantic_search_documents"})
        return "**Sachstand:** X"

    meta = {"email_message_id": "M1", "subject": "Rückfrage Testreport", "from_address": "f@sb.ch"}
    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_gather_draft_context", gather),
        patch.object(hw, "_build_draft_prompt", AsyncMock(return_value="PROMPT")),
        patch.object(hw, "asyncio", _writing_agent()),
    ):
        await hw._generate_reply_draft(meta, None)

    passes = [e.get("pass") for e in hw._job_trace]
    assert passes == ["classify", "gather"]
    hw._job_trace.clear()


@pytest.mark.asyncio
async def test_generate_reply_draft_skips_gathering_for_scheduling_mail():
    """Terminanfragen laufen wie bisher -- ohne Recherche und ohne Verzoegerung."""
    meta = {
        "email_message_id": "M1",
        "subject": "Terminanfrage",
        "body_preview": "Wann hast du freie Zeit?",
        "from_address": "f@sb.ch",
    }
    gather = AsyncMock(return_value="sollte nicht laufen")
    build = AsyncMock(return_value="PROMPT")
    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_gather_draft_context", gather),
        patch.object(hw, "_build_draft_prompt", build),
        patch.object(hw, "asyncio", SimpleNamespace(to_thread=AsyncMock(return_value=""))),
    ):
        hw._job_created_draft_id = "D1"
        await hw._generate_reply_draft(meta, None)

    gather.assert_not_awaited()
    assert build.await_args.args[3] is False


@pytest.mark.asyncio
async def test_generate_reply_draft_survives_failed_research():
    """Scheitert die Recherche, entsteht trotzdem ein Entwurf -- nur ohne Fachkontext."""
    meta = {"email_message_id": "M1", "subject": "Rückfrage", "from_address": "f@sb.ch"}
    build = AsyncMock(return_value="PROMPT")
    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_gather_draft_context", AsyncMock(return_value=None)),
        patch.object(hw, "_build_draft_prompt", build),
        patch.object(hw, "asyncio", _writing_agent()),
    ):
        result = await hw._generate_reply_draft(meta, None)

    assert result == "D1"
    assert build.await_args.args[2] is None
    assert build.await_args.args[3] is True


@pytest.mark.asyncio
async def test_research_disabled_by_flag():
    """Feature-Flag: eine Env-Variable schaltet die Stufe komplett ab."""
    meta = {"email_message_id": "M1", "subject": "Rückfrage", "from_address": "f@sb.ch"}
    gather = AsyncMock()
    with (
        patch.object(hw, "get_settings", lambda: _settings(draft_context_research=False)),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_gather_draft_context", gather),
        patch.object(hw, "_build_draft_prompt", AsyncMock(return_value="P")),
        patch.object(hw, "asyncio", SimpleNamespace(to_thread=AsyncMock(return_value=""))),
    ):
        hw._job_created_draft_id = "D1"
        await hw._generate_reply_draft(meta, None)
    gather.assert_not_awaited()


# ── Cloud-Schreibpfad ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_writer_falls_back_to_local_when_anonymization_fails():
    """Fail-closed: ohne funktionierende Maskierung geht NICHTS an die Cloud.

    E-Mail-Entwuerfe entstehen unbeaufsichtigt -- der Kontext ist vorher nicht
    pruefbar. Darum ist die Barriere hier nicht abschaltbar, und ein Ausfall des
    Anonymisierungs-Dienstes fuehrt zum lokalen Modell, nicht zum ungeschuetzten
    Versand.
    """
    meta = {"email_message_id": "M1"}
    calls: list = []
    local_run = _writing_agent("LOKAL1")

    async def _tracking(*args, **kwargs):
        calls.append(args)
        return await local_run.to_thread(*args, **kwargs)

    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(
            hw, "_anonymize_for_cloud", AsyncMock(side_effect=RuntimeError("Dienst weg"))
        ),
        patch.object(hw, "_build_cloud_job_agent", lambda m: object()),
        patch.object(hw, "asyncio", SimpleNamespace(to_thread=_tracking)),
    ):
        result = await hw._write_draft_with_cloud_model(meta, "PROMPT", "anthropic/opus")

    assert result == "LOKAL1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cloud_writer_discards_draft_when_deanonymization_fails():
    """Lieber kein Entwurf als einer mit fremden Namen statt echter.

    Die Ruecksetzung wirft nicht mehr, sie meldet den Fehlschlag als Rueckstand
    (siehe ``app.services.anon_politik``). Fuer diesen Weg ist die Konsequenz
    dieselbe wie bei einem echten Rueckstand: lokal neu schreiben. Der Entwurf
    geht an einen echten Kunden -- ein fremder Name darin ist nicht zu erklaeren.
    """
    meta = {"email_message_id": "M1"}
    create = AsyncMock(return_value="D9")
    local_run = _writing_agent("LOKAL3")
    calls: list = []

    async def _tracking(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            return "Hallo Senad Weibel"
        return await local_run.to_thread(*args, **kwargs)

    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_anonymize_for_cloud", AsyncMock(return_value=("MASKIERT", "S1"))),
        patch.object(hw, "_build_cloud_job_agent", lambda m: object()),
        patch.object(hw, "asyncio", SimpleNamespace(to_thread=_tracking)),
        patch.object(
            hw,
            "_deanonymize_from_cloud",
            AsyncMock(return_value=("Hallo Senad Weibel", ["(Rueckbildung fehlgeschlagen)"])),
        ),
        patch.object(hw, "_create_reply_draft_from_text", create),
    ):
        result = await hw._write_draft_with_cloud_model(meta, "PROMPT", "anthropic/opus")

    assert result == "LOKAL3"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_writer_creates_draft_server_side():
    """Das werkzeuglose Cloud-Modell liefert Text; den Entwurf legt das Backend an.

    Werkzeuglos ist Absicht: ein maskiertes Modell wuerde mit Platzhaltern suchen
    («PERSON_1 KreditorenBot») und nichts finden. Gesammelt wird deshalb vorher,
    lokal und mit echten Namen.
    """
    meta = {"email_message_id": "M1"}
    create = AsyncMock(return_value="D42")
    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_anonymize_for_cloud", AsyncMock(return_value=("MASKIERT", "S1"))),
        patch.object(hw, "_build_cloud_job_agent", lambda m: object()),
        patch.object(
            hw, "asyncio", SimpleNamespace(to_thread=AsyncMock(return_value="Hallo <PERSON_1>"))
        ),
        patch.object(
            hw, "_deanonymize_from_cloud", AsyncMock(return_value=("Hallo Franziska", []))
        ),
        patch.object(hw, "_create_reply_draft_from_text", create),
    ):
        result = await hw._write_draft_with_cloud_model(meta, "PROMPT", "anthropic/opus")

    assert result == "D42"
    assert create.await_args.args[1] == "Hallo Franziska"


def test_cloud_writer_suffix_states_no_tools_and_name_handling():
    """Der Auftrag muss die Maskierung korrekt beschreiben.

    Die fruehere Fassung sprach von Platzhaltern «etwa <PERSON_1>». Ein Test am
    04.08.2026 gegen das echte contentConverter-Modell zeigte: maskiert wird mit
    ERSATZNAMEN («Senad Weibel»), nicht mit Platzhaltern. Eine falsche Beschreibung
    ist hier nicht bloss unsauber -- sie laedt das Modell dazu ein, den Namen frei
    zu behandeln, und genau dann scheitert die Ruecksetzung.
    """
    assert "KEINE Werkzeuge" in hw._CLOUD_WRITER_SUFFIX
    assert "ANDERE Namen" in hw._CLOUD_WRITER_SUFFIX
    assert "nie verkürzt" in hw._CLOUD_WRITER_SUFFIX
    assert "<PERSON_1>" not in hw._CLOUD_WRITER_SUFFIX


@pytest.mark.asyncio
async def test_cloud_writer_falls_back_locally_when_pseudonym_survives():
    """Ein Tarnname im Entwurf ist gefaehrlicher als kein Cloud-Entwurf.

    Schreibt das Modell «Hoi Senad» statt «Hoi Senad Weibel», setzt die
    Ruecksetzung nichts zurueck -- der Entwurf traegt dann einen fremden, plausiblen
    Namen. Dann schreibt das lokale Modell.
    """
    meta = {"email_message_id": "M1"}
    create = AsyncMock(return_value="D9")
    local_run = _writing_agent("LOKAL2")
    calls: list = []

    async def _tracking(*args, **kwargs):
        calls.append(args)
        # Aufruf 1 baut den Cloud-Agenten, Aufruf 2 ist dessen Schreib-Lauf,
        # Aufruf 3 der lokale Rueckfall.
        if len(calls) == 2:
            return "Hoi Senad, danke"
        return await local_run.to_thread(*args, **kwargs)

    with (
        patch.object(hw, "get_settings", lambda: _settings()),
        patch.object(hw, "_agent", object()),
        patch.object(hw, "_anonymize_for_cloud", AsyncMock(return_value=("MASKIERT", "S1"))),
        patch.object(hw, "_build_cloud_job_agent", lambda m: object()),
        patch.object(hw, "asyncio", SimpleNamespace(to_thread=_tracking)),
        patch.object(
            hw,
            "_deanonymize_from_cloud",
            AsyncMock(return_value=("Hoi Senad, danke", ["Senad"])),
        ),
        patch.object(hw, "_create_reply_draft_from_text", create),
    ):
        result = await hw._write_draft_with_cloud_model(meta, "PROMPT", "anthropic/opus")

    assert result == "LOKAL2"
    create.assert_not_awaited()


def test_plain_text_to_html_builds_paragraphs_and_escapes():
    html = hw._plain_text_to_html("Hallo Franziska\nkurze Zeile\n\nZweiter Absatz & mehr")
    assert html.count("<p>") == 2
    assert "<br>" in html
    assert "&amp;" in html
    assert hw._plain_text_to_html("") == ""


# ── Sampling des Sammel-Laufs ────────────────────────────────────────────────


def test_run_agent_sync_caps_and_restores_iterations():
    """Die Rundengrenze muss strukturell wirken, nicht als Bitte im Prompt.

    Live-Test vom 03.08.2026: Trotz «höchstens 5 Suchvorgänge» im Prompt wiederholte
    das Modell dieselbe Abfrage bis zum Abbruch und lieferte gar kein Dossier. Erst
    das Iterationslimit erzwingt ein Ergebnis -- Hermes fordert beim Erreichen
    selbsttaetig eine werkzeuglose Zusammenfassung an.
    """
    seen: dict = {}

    class _Agent:
        max_iterations = 90
        request_overrides = None

        def run_conversation(self, prompt, system_message=None):
            seen["iterations"] = self.max_iterations
            return {"final_response": "Dossier"}

    agent = _Agent()
    out = hw._run_agent_sync(agent, "P", True, None, max_iterations=6)

    assert out == "Dossier"
    assert seen["iterations"] == 6
    assert agent.max_iterations == 90  # danach wiederhergestellt


def test_gather_uses_bounded_rounds():
    """Das Limit steht in einer Konstanten, nicht verstreut im Code."""
    assert 3 <= hw._GATHER_MAX_ROUNDS <= 5


def test_gather_sampling_is_cooler_than_prose_sampling():
    """Der Sammel-Lauf waehlt Werkzeuge, er formuliert nicht -- Prosa-Sampling
    (temp 0.7) verrauscht dort die Query-Wahl."""
    with patch.object(hw, "get_settings", lambda: _settings()):
        gather = hw._gather_sampling_overrides()
        prose = hw._draft_sampling_overrides(True)
    assert gather["temperature"] < prose["temperature"]
    assert gather["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert gather["reasoning_effort"] == "none"
