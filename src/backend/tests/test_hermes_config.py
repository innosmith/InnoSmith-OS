"""Tests für die generierte Hermes-Runtime-Config (build_config_dict).

Sichert die Governance-/Hardening-Entscheidungen ab: Skill-Writes gegated,
alle Auxiliary-Slots auf dem lokalen Ollama-Endpoint, Gateway-Curator aus.
Kein DB/Netz nötig -- build_config_dict liest nur Pydantic-Settings.
"""

from unittest.mock import AsyncMock

import yaml

from app.services.hermes_config import LOCAL_CONTEXT_LENGTH, build_config_dict

_AUX_SLOTS = (
    "compression", "vision", "web_extract", "background_review",
    "title_generation", "curator",
)


def test_skill_writes_are_gated():
    cfg = build_config_dict()
    assert cfg["skills"]["write_approval"] is True
    # Bestehende Einstellung bleibt erhalten.
    assert cfg["skills"]["creation_nudge_interval"] == 25


def test_approvals_zwingen_den_menschen():
    """Smart Approvals sind ab Hermes 0.19 Default -- TaskPilot darf das nicht."""
    cfg = build_config_dict()
    assert cfg["approvals"]["mode"] == "manual"


def test_all_auxiliary_slots_pinned_to_local():
    cfg = build_config_dict()
    aux = cfg["auxiliary"]
    for slot in _AUX_SLOTS:
        assert slot in aux, f"Aux-Slot fehlt: {slot}"
        assert aux[slot]["provider"] == "custom"
        assert aux[slot]["api_key"] == "ollama"
        assert aux[slot]["base_url"].endswith("/v1")
        assert aux[slot]["model"] and "ollama/" not in aux[slot]["model"]


def test_legacy_title_slot_removed():
    """Der fruehere Key 'title' war in Hermes 0.18 wirkungslos -- der korrekte
    Slot heisst 'title_generation'."""
    cfg = build_config_dict()
    assert "title" not in cfg["auxiliary"]
    assert "title_generation" in cfg["auxiliary"]


def test_web_backends_explicitly_pinned():
    """Suche via ddgs (anonym, gratis), Extraktion via Tavily (einziges
    Extract-Backend). Explizit statt kaskadenabhängig -- die Auto-Detect-
    Kaskade war zuvor auf ddgs (search-only) gefallen und web_extract
    damit funktionslos."""
    cfg = build_config_dict()
    assert cfg["web"]["search_backend"] == "ddgs"
    assert cfg["web"]["extract_backend"] == "tavily"


def test_taskpilot_mcp_env_without_tavily_key():
    """Das MCP-Tool mcp_taskpilot_web_search wurde entfernt (Redundanz zur
    Hermes-nativen Websuche + Doppel-Logging) -- der taskpilot-Server braucht
    den Tavily-Key nicht mehr."""
    cfg = build_config_dict()
    env = cfg["mcp_servers"]["taskpilot"]["env"]
    assert "TP_TAVILY_API_KEY" not in env


def test_populate_hermes_env_mirrors_tavily_key(monkeypatch):
    """Hermes' native Web-Tools lesen TAVILY_API_KEY UNpräfixiert -- ohne
    Spiegelung fällt die Backend-Kaskade auf ddgs zurück und web_extract
    ist funktionslos."""
    import asyncio
    import os

    from app.services.hermes_config import populate_hermes_env

    monkeypatch.setenv("TP_TAVILY_API_KEY", "tvly-test-dummy")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    asyncio.run(populate_hermes_env())
    assert os.environ.get("TAVILY_API_KEY") == "tvly-test-dummy"


def test_db_token_schlaegt_env(monkeypatch):
    """Der in der Oberflaeche gepflegte Token muss die MCP-Server erreichen.

    Der Vorfall (02.09.2026): In Produktion setzt ``.env.prod`` die Variable
    ``TP_BEXIO_API_TOKEN``. ``populate_hermes_env`` liess gesetzte Env-Vars
    unangetastet und kam damit nie zur DB -- waehrend die Finanz- und
    Bexio-Router zuerst die DB lesen. Zwei Pfade, umgekehrte Rangfolge: Der
    ueber die Oberflaeche erneuerte Token lief in der Finanzansicht, der Agent
    benutzte weiter den alten aus der Datei und bekam bei jedem Bexio-Werkzeug
    einen 401.

    Unentdeckt blieb es, weil beide Tokens plausibel aussahen: Der Startcheck
    liest nur den ``exp``-Claim des Datei-Tokens und meldete ihn als monatelang
    gueltig -- abgelaufen war er nicht, bloss ersetzt.
    """
    import asyncio
    import os

    from app.services import hermes_config
    from app.services.hermes_config import populate_hermes_env

    monkeypatch.setenv("TP_BEXIO_API_TOKEN", "aus-der-datei")
    monkeypatch.setattr(
        hermes_config,
        "get_owner_settings",
        AsyncMock(return_value={"bexio_api_token": "aus-der-oberflaeche"}),
    )
    asyncio.run(populate_hermes_env())
    assert os.environ["TP_BEXIO_API_TOKEN"] == "aus-der-oberflaeche"


def test_env_traegt_wenn_die_db_nichts_hat(monkeypatch):
    """Ohne Eintrag in der Oberflaeche bleibt die Datei die Quelle."""
    import asyncio
    import os

    from app.services import hermes_config
    from app.services.hermes_config import populate_hermes_env

    monkeypatch.setenv("TP_BEXIO_API_TOKEN", "aus-der-datei")
    monkeypatch.setattr(hermes_config, "get_owner_settings", AsyncMock(return_value={}))
    asyncio.run(populate_hermes_env())
    assert os.environ["TP_BEXIO_API_TOKEN"] == "aus-der-datei"


def test_infrastruktur_bleibt_beim_betreiber(monkeypatch):
    """Die umgekehrte Rangfolge gilt nur fuer Zugangsdaten, nicht fuer die Infrastruktur."""
    import asyncio
    import os

    from app.services import hermes_config
    from app.services.hermes_config import populate_hermes_env

    monkeypatch.setenv("TP_DB_HOST", "der-container-sagt-das")
    monkeypatch.setattr(hermes_config, "get_owner_settings", AsyncMock(return_value={}))
    asyncio.run(populate_hermes_env())
    assert os.environ["TP_DB_HOST"] == "der-container-sagt-das"


def test_gateway_curator_defensively_disabled():
    cfg = build_config_dict()
    assert cfg["curator"]["enabled"] is False
    assert cfg["curator"]["prune_builtins"] is False
    assert cfg["curator"]["consolidate"] is False


def test_werkzeug_aufschub_greift_erst_am_qualitaetsknick():
    """Die Bruecke tool_search/tool_call darf bei unserer Werkzeugmenge nicht anspringen.

    Sie kostet pro Zug eine Runde und verlangt Code als JSON-Zeichenkette in einem
    JSON-Aufruf -- daran scheiterte das lokale Modell im Auswertungslauf vom
    02.09.2026 vier von vier Malen. Bei ~14'170 Token Schema muss die Schwelle
    darueber liegen, ohne den Schutz ganz abzuschalten.

    Gerechnet wird gegen ``LOCAL_CONTEXT_LENGTH``, nicht gegen eine hier notierte
    Zahl: Die erste Fassung dieses Tests trug 131'072 fest im Code, waehrend das
    Fenster tatsaechlich 65'536 misst. Der Test war damit gruen und seine Aussage
    trotzdem falsch -- die Schwelle lag unter der Werkzeugmenge statt darueber.
    """
    ts = build_config_dict()["tools"]["tool_search"]
    assert ts["enabled"] == "auto"
    schwelle = LOCAL_CONTEXT_LENGTH * ts["threshold_pct"] / 100
    assert 14_170 < schwelle <= 20_000, (
        f"Schwelle {schwelle:.0f} Token passt nicht zwischen Werkzeugmenge und Qualitaetsknick"
    )


def test_config_yaml_roundtrip_without_aliases():
    """Aux-Slots sind eigenstaendige Dicts -- yaml.safe_dump darf keine Anker
    (&id/*id) erzeugen, die manche Loader anders behandeln."""
    cfg = build_config_dict()
    dumped = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    assert "&id" not in dumped and " *id" not in dumped
    reloaded = yaml.safe_load(dumped)
    assert reloaded["skills"]["write_approval"] is True
    assert set(_AUX_SLOTS).issubset(reloaded["auxiliary"].keys())


def test_schleifenwaechter_stoppt_bevor_das_zeitlimit_greift():
    """Der harte Stopp muss zuschlagen, solange noch Zeit fuer eine Antwort bleibt.

    Am 02.09.2026 verschrieb sich das lokale Modell bei einem Spaltennamen und
    wiederholte den Fehler achtzehn Sandbox-Laeufe lang. Die Grenze stand auf 8
    gleichartigen Fehlschlaegen, zwei zufaellige Teilerfolge setzten den Zaehler
    zurueck -- gegriffen hat am Ende nur das Zeitlimit von 600 Sekunden, und der
    Nutzer bekam nach zehn Minuten gar nichts.

    Ein Sandbox-Lauf samt Modellrunde kostet grob 30 Sekunden. Die Grenzen muessen
    deshalb so liegen, dass selbst der ungeduldigste Zaehler lange vor
    ``MAX_AGENT_TIMEOUT`` stoppt und dem Modell Zeit laesst, aus dem bereits
    Erreichten eine Antwort zu schreiben.
    """
    from app.routers.chat import MAX_AGENT_TIMEOUT

    stopps = build_config_dict()["tool_loop_guardrails"]["hard_stop_after"]
    warnungen = build_config_dict()["tool_loop_guardrails"]["warn_after"]

    assert max(stopps.values()) * 30 < MAX_AGENT_TIMEOUT / 2, (
        "Der harte Stopp darf nicht erst kurz vor dem Zeitlimit greifen"
    )
    for art, grenze in stopps.items():
        assert warnungen[art] < grenze, f"Warnung fuer '{art}' muss vor dem Stopp kommen"
