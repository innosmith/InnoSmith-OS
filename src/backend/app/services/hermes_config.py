"""Generiert die Hermes-Runtime-Konfiguration (~/.hermes/config.yaml).

Hermes liest MCP-Server, Modell und Kontextfenster aus ``~/.hermes/config.yaml``.
``${VAR}``-Platzhalter in den ``env``-Blöcken werden von Hermes zur Discovery-Zeit
aus ``os.environ`` aufgelöst (siehe ``tools.mcp_tool._load_mcp_config``). Hermes'
``_build_safe_env`` reicht ausschliesslich die explizit in ``env`` aufgeführten
Werte an die MCP-Subprozesse weiter — Secrets müssen daher dort referenziert sein.

Strategie:
- Secrets bleiben als ``${TP_*}``-Platzhalter in der YAML (kein Klartext auf der Platte).
- ``populate_hermes_env()`` befüllt ``os.environ`` aus den DB-Settings (Owner) bzw.
  den Pydantic-Settings, bevor die Discovery läuft.
- Pfade (Python-Binary, MCP-Basisverzeichnis) werden konkret aufgelöst, damit
  dieselbe Logik in Dev (lokale venv) und Prod (Container, /app) funktioniert.
"""

import logging
import os
import sys
from pathlib import Path

import yaml

from app.config import get_settings
from app.database import async_session
from app.core.principal import get_owner_settings

logger = logging.getLogger("taskpilot.hermes_config")

# Effektives Fenster der stabilen Qwen-3.6-Produktion (Ollama 0.24: KvSize 65536).
# Nicht 256k: Ollama 0.32 setzt das auf der GB10 als VRAM-Default und sprengt Unified Memory.
LOCAL_CONTEXT_LENGTH = 65536


def get_hermes_home() -> Path:
    """Liefert das Hermes-Home (config.yaml, skills/, memories/, SOUL.md)."""
    return Path(os.path.expanduser(get_settings().hermes_home))


def _parse_skill_frontmatter(content: str) -> dict:
    """Liest das YAML-Frontmatter eines SKILL.md (zwischen den ``---``-Zeilen)."""
    import yaml

    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    raw = content[3:end]
    try:
        data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first_body_line(content: str) -> str:
    """Erste nicht-leere, nicht-Heading-Zeile als Fallback-Beschreibung.

    Ueberspringt ein etwaiges YAML-Frontmatter (Block zwischen zwei ``---``-Zeilen).
    """
    lines = content.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    for line in lines[start:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped != "---":
            return stripped[:200]
    return ""


def discover_skills() -> list[dict]:
    """Erkennt die Hermes-Skills im ``skills/``-Verzeichnis.

    Hermes-nativ liegen Skills als ``skills/<name>/SKILL.md`` mit YAML-Frontmatter
    (``name``, ``description``, ``metadata.hermes.requires_toolsets``). Diese Funktion
    ist die Single Source of Truth fuer alle Skill-Listings im Frontend (Intelligenz-
    Tab, Heartbeat, Brain). Faellt auf alte Flat-``.md``-Dateien zurueck, falls (noch)
    keine nativen Skills vorhanden sind.

    Returns: Liste von Dicts mit ``name``, ``description``, ``requires_toolsets``,
    ``content`` und ``size``, sortiert nach Name.
    """
    skills_dir = get_hermes_home() / "skills"
    if not skills_dir.exists():
        return []

    out: list[dict] = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_skill_frontmatter(content)
        name = str(fm.get("name") or skill_file.parent.name).strip()
        description = str(fm.get("description") or _first_body_line(content)).strip()
        hermes_meta = (fm.get("metadata") or {}).get("hermes") or {}
        req = hermes_meta.get("requires_toolsets") or []
        if not isinstance(req, list):
            req = [str(req)]
        try:
            size = skill_file.stat().st_size
        except OSError:
            size = len(content.encode("utf-8"))
        out.append({
            "name": name,
            "description": description,
            "requires_toolsets": [str(t) for t in req],
            "content": content,
            "size": size,
        })

    # Fallback: alte Flat-Skills (skills/<name>.md), nur wenn keine nativen da sind.
    if not out:
        for f in sorted(skills_dir.glob("*.md")):
            if not f.is_file():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append({
                "name": f.stem,
                "description": _first_body_line(content),
                "requires_toolsets": [],
                "content": content,
                "size": f.stat().st_size,
            })

    out.sort(key=lambda s: s["name"])
    return out


def _mcp_base_dir() -> str:
    """Verzeichnis, das die ``mcp-*``-Server enthält (Dev: ``src/``, Prod: ``/app``)."""
    override = os.environ.get("TP_MCP_BASE_DIR")
    if override:
        return override
    # services/hermes_config.py -> parents[3] == <repo>/src
    return str(Path(__file__).resolve().parents[3])


def _python_bin() -> str:
    """Python-Interpreter für die MCP-Subprozesse (gleiche venv wie das Backend)."""
    return os.environ.get("TP_HERMES_PYTHON") or sys.executable


# Mapping Settings-Attribut -> Env-Var-Name, die die config.yaml referenziert.
# Reihenfolge: DB-Settings (Owner) haben Vorrang, sonst Pydantic-Settings (.env).
_DB_TOKEN_KEYS: dict[str, str] = {
    "pipedrive_api_token": "TP_PIPEDRIVE_API_TOKEN",
    "pipedrive_domain": "TP_PIPEDRIVE_DOMAIN",
    "toggl_api_token": "TP_TOGGL_API_TOKEN",
    "toggl_workspace_id": "TP_TOGGL_WORKSPACE_ID",
    "bexio_api_token": "TP_BEXIO_API_TOKEN",
    "invoiceinsight_api_key": "TP_INVOICEINSIGHT_API_KEY",
    "invoiceinsight_url": "TP_INVOICEINSIGHT_URL",
    "tavily_api_key": "TP_TAVILY_API_KEY",
}

# Settings-Attribut -> Env-Var, immer aus Pydantic-Settings (kein DB-Override).
_SETTINGS_KEYS: dict[str, str] = {
    "db_host": "TP_DB_HOST",
    "db_port": "TP_DB_PORT",
    "db_user": "TP_DB_USER",
    "db_password": "TP_DB_PASSWORD",
    "db_name": "TP_DB_NAME",
    "graph_tenant_id": "TP_GRAPH_TENANT_ID",
    "graph_client_id": "TP_GRAPH_CLIENT_ID",
    "graph_client_secret": "TP_GRAPH_CLIENT_SECRET",
    "graph_user_email": "TP_GRAPH_USER_EMAIL",
    "isi_host": "TP_ISI_HOST",
    "isi_db": "TP_ISI_DB",
    "isi_user": "TP_ISI_USER",
    "isi_secret": "TP_ISI_SECRET",
    "openai_api_key": "TP_OPENAI_API_KEY",
    "sandbox_executor_url": "TP_SANDBOX_EXECUTOR_URL",
    "sandbox_executor_token": "TP_SANDBOX_EXECUTOR_TOKEN",
    "datenraum_dir": "TP_DATENRAUM_DIR",
}


async def populate_hermes_env() -> None:
    """Setzt alle ``TP_*``-Env-Vars, die ``config.yaml`` referenziert, in ``os.environ``.

    Hermes löst die ``${VAR}``-Platzhalter zur Discovery-Zeit aus ``os.environ`` auf.

    Zwei Gruppen mit **verschiedener** Rangfolge, siehe die Begründung unten:

    - Infrastruktur (``_SETTINGS_KEYS``): gesetzte Env-Var gewinnt, der Betreiber
      übersteuert die Konfiguration.
    - Zugangsdaten (``_DB_TOKEN_KEYS``): die DB gewinnt, denn dort schreibt die
      Oberfläche hin -- gleiche Rangfolge wie in den Finanz- und Bexio-Routern.

    Läuft einmal je Prozess (über ``ensure_runtime_ready``). Ein in der Oberfläche
    geänderter Token erreicht die bereits gestarteten MCP-Subprozesse deshalb erst
    nach einem Neustart des Backends.
    """
    cfg = get_settings()

    for attr, env_key in _SETTINGS_KEYS.items():
        if os.environ.get(env_key):
            continue
        value = getattr(cfg, attr, "")
        os.environ[env_key] = str(value) if value not in (None, "") else ""

    # DB-Settings des Owners haben für die Integrations-Tokens Vorrang.
    db_settings: dict = {}
    try:
        async with async_session() as db:
            db_settings = await get_owner_settings(db)
    except Exception:
        logger.warning("Hermes-Env: DB-Settings nicht lesbar — nutze .env-Fallback")

    # Rangfolge: DB (Oberfläche) -> Container-Env/.env -> leer. Absichtlich eine
    # andere als bei den _SETTINGS_KEYS oben, und sie muss mit den Finanz- und
    # Bexio-Routern übereinstimmen: liest die eine Strecke zuerst die DB und die
    # andere zuerst .env, benutzen Agent und Oberfläche verschiedene Zugangsdaten.
    # Das erscheint dann als 401 nur im Agenten, während dieselbe Integration in
    # der Oberfläche läuft -- und ein Ablaufdatum widerlegt es nicht, denn ein
    # widerrufener Token bleibt bis zu seinem Ablauf unauffällig.
    for db_key, env_key in _DB_TOKEN_KEYS.items():
        value = db_settings.get(db_key) or os.environ.get(env_key) or getattr(cfg, db_key, "")
        os.environ[env_key] = str(value) if value not in (None, "") else ""

    # Hermes' native Web-Tools (web_search/web_extract) suchen den Tavily-Key
    # UNpräfixiert (TAVILY_API_KEY) in os.environ bzw. ~/.hermes/.env. Ohne
    # Spiegelung fällt die Backend-Kaskade auf ddgs zurück (search-only) und
    # web_extract ist funktionslos.
    if not os.environ.get("TAVILY_API_KEY"):
        os.environ["TAVILY_API_KEY"] = os.environ.get("TP_TAVILY_API_KEY", "")


def build_config_dict() -> dict:
    """Baut das Hermes-Config-Dict (Modell + MCP-Server + contentConverter)."""
    cfg = get_settings()
    base = _mcp_base_dir()
    py = _python_bin()
    pythonpath = ":".join([
        base,
        f"{base}/email-graph",
        f"{base}/pipedrive",
        f"{base}/bexio",
        f"{base}/toggl",
    ])

    # Ollama /v1 (OpenAI-kompatibel) als custom-Provider — Spike-validiert.
    ollama_v1 = f"{cfg.ollama_base_url.rstrip('/')}/v1"

    def _local_aux() -> dict:
        """Frisches Aux-Slot-Dict auf dem lokalen Modell (keine YAML-Alias-Anker)."""
        return {
            "provider": "custom",
            "base_url": ollama_v1,
            "api_key": "ollama",
            "api_mode": "chat_completions",
            "model": cfg.triage_model.removeprefix("ollama/"),
            "context_length": LOCAL_CONTEXT_LENGTH,
        }

    def stdio(server_subdir: str, env: dict, extra_pythonpath: str | None = None) -> dict:
        pp = pythonpath if extra_pythonpath is None else extra_pythonpath
        return {
            "command": py,
            "args": [f"{base}/{server_subdir}/server.py"],
            "env": {**env, "PYTHONPATH": pp},
            "timeout": 120,
            "connect_timeout": 60,
        }

    mcp_servers: dict = {
        "taskpilot": stdio("mcp-taskpilot", {
            "TP_DB_HOST": "${TP_DB_HOST}",
            "TP_DB_PORT": "${TP_DB_PORT}",
            "TP_DB_USER": "${TP_DB_USER}",
            "TP_DB_PASSWORD": "${TP_DB_PASSWORD}",
            "TP_DB_NAME": "${TP_DB_NAME}",
        }),
        # Zwei Registrierungen desselben Servers, weil Hermes Tools nur auf
        # Server-Ebene filtern kann (``enabled_toolsets``):
        # - ``graph`` (Modus ``safe``): alles ausser den zustandsveraendernden
        #   Tools. Der Triage-Agent laeuft ausschliesslich hierauf, damit das LLM
        #   Kategorien und Ordner nicht selbst setzt (das macht das Backend
        #   deterministisch aus der validierten Klassifikation).
        # - ``graphAdmin`` (Modus ``admin``): nur die zustandsveraendernden Tools,
        #   ausschliesslich fuer den Chat-Agenten. Die Aufteilung ist disjunkt, damit
        #   die Lese-Tools nicht doppelt im Chat-Kontext liegen.
        # Der Key ``graph`` bleibt bewusst der eingeschraenkte: so behalten die
        # eingespielten Triage-Prompts, -Skills und die Callback-Hooks in
        # hermes_worker.py ihre Tool-Namen (``mcp_graph_*``).
        #
        # ``GRAPH_TRIAGE_DRAFT`` verschiebt ``create_draft`` im Zwei-Pass-Betrieb
        # von ``graph`` nach ``graphAdmin``: dann kann nur noch der Schreib-Pass
        # (voller Worker-Agent) Entwuerfe erstellen, nicht mehr der reduzierte
        # Triage-Agent. Ohne diese Trennung erzeugte der Klassifikations-Lauf
        # gelegentlich einen Platzhalter-Entwurf, der den Schreib-Pass verdraengte.
        "graph": stdio("mcp-graph", {
            "GRAPH_TENANT_ID": "${TP_GRAPH_TENANT_ID}",
            "GRAPH_CLIENT_ID": "${TP_GRAPH_CLIENT_ID}",
            "GRAPH_CLIENT_SECRET": "${TP_GRAPH_CLIENT_SECRET}",
            "GRAPH_USER_EMAIL": "${TP_GRAPH_USER_EMAIL}",
            "GRAPH_TOOL_MODE": "safe",
            "GRAPH_TRIAGE_DRAFT": "0" if cfg.two_pass_draft else "1",
        }, extra_pythonpath=f"{base}:{base}/email-graph"),
        "graphAdmin": stdio("mcp-graph", {
            "GRAPH_TENANT_ID": "${TP_GRAPH_TENANT_ID}",
            "GRAPH_CLIENT_ID": "${TP_GRAPH_CLIENT_ID}",
            "GRAPH_CLIENT_SECRET": "${TP_GRAPH_CLIENT_SECRET}",
            "GRAPH_USER_EMAIL": "${TP_GRAPH_USER_EMAIL}",
            "GRAPH_TOOL_MODE": "admin",
            "GRAPH_TRIAGE_DRAFT": "0" if cfg.two_pass_draft else "1",
        }, extra_pythonpath=f"{base}:{base}/email-graph"),
        # Kapazitätsplanung: braucht DB (Plan) UND Toggl (Ist). Eigener Server statt
        # Anbau an taskpilot, weil die Domäne eine andere ist -- und weil der Agent
        # sonst Stunden- und Verfügbarkeitsfragen aus dem E-Mail-Archiv beantwortet,
        # was am 03.08.2026 nachweislich zu einer veralteten Budgetzahl im Entwurf führte.
        "capacity": stdio("mcp-capacity", {
            "TP_DB_HOST": "${TP_DB_HOST}",
            "TP_DB_PORT": "${TP_DB_PORT}",
            "TP_DB_USER": "${TP_DB_USER}",
            "TP_DB_PASSWORD": "${TP_DB_PASSWORD}",
            "TP_DB_NAME": "${TP_DB_NAME}",
            "TP_TOGGL_API_TOKEN": "${TP_TOGGL_API_TOKEN}",
            "TP_TOGGL_WORKSPACE_ID": "${TP_TOGGL_WORKSPACE_ID}",
        }, extra_pythonpath=f"{base}:{base}/capacity:{base}/toggl"),
        "pipedrive": stdio("mcp-pipedrive", {
            "TP_PIPEDRIVE_API_TOKEN": "${TP_PIPEDRIVE_API_TOKEN}",
            "TP_PIPEDRIVE_DOMAIN": "${TP_PIPEDRIVE_DOMAIN}",
        }, extra_pythonpath=f"{base}:{base}/pipedrive"),
        "toggl": stdio("mcp-toggl", {
            "TP_TOGGL_API_TOKEN": "${TP_TOGGL_API_TOKEN}",
            "TP_TOGGL_WORKSPACE_ID": "${TP_TOGGL_WORKSPACE_ID}",
        }, extra_pythonpath=f"{base}:{base}/toggl"),
        "bexio": stdio("mcp-bexio", {
            "TP_BEXIO_API_TOKEN": "${TP_BEXIO_API_TOKEN}",
        }, extra_pythonpath=f"{base}:{base}/bexio"),
        "signa": stdio("mcp-signa", {
            "ISI_HOST": "${TP_ISI_HOST}",
            "ISI_DB": "${TP_ISI_DB}",
            "ISI_USER": "${TP_ISI_USER}",
            "ISI_SECRET": "${TP_ISI_SECRET}",
            "TP_OPENAI_API_KEY": "${TP_OPENAI_API_KEY}",
        }, extra_pythonpath=base),
        "invoiceinsight": stdio("mcp-invoiceinsight", {
            "TP_INVOICEINSIGHT_URL": "${TP_INVOICEINSIGHT_URL}",
            "TP_INVOICEINSIGHT_API_KEY": "${TP_INVOICEINSIGHT_API_KEY}",
        }, extra_pythonpath=base),
        # mcp-scripts delegiert (wie mcp-sandbox) an den Sandbox-Executor; Registry,
        # Secrets und docker.sock liegen dort. Der MCP-Prozess braucht nur URL + Token.
        "scripts": stdio("mcp-scripts", {
            "TP_SANDBOX_EXECUTOR_URL": "${TP_SANDBOX_EXECUTOR_URL}",
            "TP_SANDBOX_EXECUTOR_TOKEN": "${TP_SANDBOX_EXECUTOR_TOKEN}",
        }, extra_pythonpath=base),
        "sandbox": stdio("mcp-sandbox", {
            "TP_SANDBOX_EXECUTOR_URL": "${TP_SANDBOX_EXECUTOR_URL}",
            "TP_SANDBOX_EXECUTOR_TOKEN": "${TP_SANDBOX_EXECUTOR_TOKEN}",
        }, extra_pythonpath=base),
        # Datenraum: liest den Katalog von der Platte und stoesst Abgleiche an. Er
        # nutzt dieselbe Bibliothek wie der Worker im Backend (app.services.datenraum)
        # -- deshalb der volle PYTHONPATH samt Backend und die Zugangsdaten der
        # Fachsysteme. Eine zweite Implementierung wuerde driften, und eine Abweichung
        # faellt bei Zahlen erst auf, wenn sie beim Kunden steht.
        "datenraum": stdio("mcp-datenraum", {
            "TP_MCP_BASE_DIR": base,
            "TP_DATENRAUM_DIR": "${TP_DATENRAUM_DIR}",
            "TP_DB_HOST": "${TP_DB_HOST}",
            "TP_DB_PORT": "${TP_DB_PORT}",
            "TP_DB_USER": "${TP_DB_USER}",
            "TP_DB_PASSWORD": "${TP_DB_PASSWORD}",
            "TP_DB_NAME": "${TP_DB_NAME}",
            "TP_BEXIO_API_TOKEN": "${TP_BEXIO_API_TOKEN}",
            "TP_TOGGL_API_TOKEN": "${TP_TOGGL_API_TOKEN}",
            "TP_TOGGL_WORKSPACE_ID": "${TP_TOGGL_WORKSPACE_ID}",
            "TP_PIPEDRIVE_API_TOKEN": "${TP_PIPEDRIVE_API_TOKEN}",
        }, extra_pythonpath=":".join([pythonpath, f"{base}/backend"])),
        # Content-Converter (md -> docx/pptx). Binary nur im Docker-Image
        # vorhanden; in der Dev-Umgebung schlaegt die Discovery still fehl
        # (Hermes loggt eine Warnung und fahrt fort).
        "contentConverter": {
            "command": os.environ.get("TP_CONTENTCONVERTER_CCONV_BIN")
            or os.environ.get("TP_CCONV_BIN", "cconv"),
            "args": ["serve"],
            "env": {},
            "timeout": 120,
            "connect_timeout": 60,
        },
    }

    return {
        "model": {
            "default": cfg.triage_model.removeprefix("ollama/"),
            "provider": "custom",
            "base_url": ollama_v1,
            "api_key": "ollama",
            "api_mode": "chat_completions",
            "context_length": LOCAL_CONTEXT_LENGTH,
        },
        # Web-Recherche: Backends EXPLIZIT statt kaskadenabhängig festlegen.
        # Suche via ddgs (DuckDuckGo): anonym (kein API-Key/Account), gratis --
        # die Suchanfrage ist der sensible Teil und bleibt unpersonalisiert.
        # Extraktion via Tavily: einziges konfiguriertes Extract-Backend; sieht
        # nur URLs oeffentlicher Seiten (nicht die Suchintention). Der Abruf
        # laeuft auf Tavily-Servern -- Egress bleibt auf api.tavily.com
        # begrenzbar (siehe docs/netzwerk-whitelist-gx10.md, Abschnitt 9).
        # Braucht TAVILY_API_KEY unpraefixiert (Spiegelung in populate_hermes_env).
        "web": {
            "search_backend": "ddgs",
            "extract_backend": "tavily",
        },
        # Built-in-Memory aktiv schalten: MEMORY.md + USER.md werden in den
        # System-Prompt injiziert (nur bei lokalen Modellen, da der Worker/Chat
        # fuer Cloud-Modelle skip_memory setzt). Kein externer Provider (Honcho):
        # die Built-in-Layer + die DB-gestuetzten Episoden/Regeln decken das ab.
        # Die *_char_limit-Werte sind Schreib-Budgets des memory-Tools, keine
        # harte Kuerzung beim Laden -- USER.md/MEMORY.md werden vollstaendig injiziert.
        # Angehoben (2200->6000 / 1375->3000): Das alte, knappe Budget hat das
        # memory-Tool beim Lernen blockiert ("Memory voll"). Durable Geschaefts-
        # regeln gehoeren ohnehin in die DB (LearnedRule) -- MEMORY.md bleibt der
        # schlanke Always-on-Layer; das groessere Budget ist nur Puffer, damit der
        # Agent beim Notieren nicht mehr an die Wand laeuft.
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "nudge_interval": 10,
            "memory_char_limit": 6000,
            "user_char_limit": 3000,
            "provider": "",
        },
        # Skill-Selbstkuratierung: seltener zur Skill-Erstellung anstupsen,
        # damit Fachjobs (Triage) nicht durch Meta-Hinweise gestoert werden.
        # write_approval=True gated ALLE skill_manage-Writes (create/edit/patch/
        # delete) -- auch die des post-turn background_review-Forks: Aenderungen
        # werden nur noch gestaged statt still committet. Das stoppt den frueheren
        # stillen Skill-Drift (Self-Patching). skill_view (Lesen, Triage-Pfad)
        # bleibt unberuehrt; unsere eigene Skill-Editor-UI schreibt direkt via
        # Backend-File-API und umgeht diesen Tool-Gate bewusst.
        "skills": {
            "creation_nudge_interval": 25,
            "write_approval": True,
        },
        # Ab 0.19 ist Smart Approvals Default: ein LLM darf geflaggte Kommandos
        # allein durchwinken. Das bricht Leitprinzip 4 (externe Kommunikation
        # immer L1). mode=manual zwingt jede Freigabe auf den Menschen.
        "approvals": {
            "mode": "manual",
        },
        # Gateway-Skill-Curator (periodisches Pruning/Archivieren) defensiv AUS:
        # er laeuft ohnehin nur ueber den Hermes-Gateway, den wir nicht fahren.
        # enabled=false verhindert, dass ein manuelles `hermes curator run` je
        # unsere kritischen Skills (email-triage/email-style) archiviert;
        # prune_builtins=false + consolidate=false als zusaetzliche Sicherung
        # (kein Built-in-Pruning, keine aux-modell-teure Konsolidierung).
        "curator": {
            "enabled": False,
            "prune_builtins": False,
            "consolidate": False,
        },
        # Kontext-Kompression: lange Threads/Chats werden ab 70 % des Kontext-
        # fensters zusammengefasst (die letzten 20 Turns bleiben unangetastet).
        "compression": {
            "enabled": True,
            "threshold": 0.7,
            "target_ratio": 0.3,
            "protect_last_n": 20,
        },
        # Hilfsmodelle fuer Nebenaufgaben (Kompression, Vision) laufen BEWUSST auf
        # demselben lokalen Hauptmodell. Begruendung: ein separates Modell wuerde in
        # Ollama Model-Loading/-Offloading ausloesen (zusaetzlicher RAM + Latenz) --
        # alles ueber das ohnehin geladene Hauptmodell ist effizienter und haelt die
        # Daten lokal. Explizit gesetzt (provider=custom + base_url), damit der
        # Endpoint inkl. Kontextfenster eindeutig ist und die Kompressions-
        # Feasibility-Pruefung beim Sessionstart nicht warnt.
        # ALLE Aux-Slots explizit auf das lokale Modell gepinnt (nicht "auto"),
        # damit kein Nebenaufgaben-Task (Kompression, Vision, Web-Extraktion,
        # Titel, Kuratierung und der post-turn background_review-Fork) unbemerkt
        # ein nicht-lokales Modell waehlt -- Datenschutz-Souveraenitaet.
        # background_review auf dem Hauptmodell bleibt cache-warm (kein Routing,
        # voller Replay). Hinweis: Der Slot heisst in Hermes 0.18
        # 'title_generation' (der fruehere Key 'title' war wirkungslos);
        # 'web_extract' ist der Aux-Slot der nativen Websuche-Extraktion.
        "auxiliary": {
            "compression": _local_aux(),
            "vision": _local_aux(),
            "web_extract": _local_aux(),
            "background_review": _local_aux(),
            "title_generation": _local_aux(),
            "curator": _local_aux(),
        },
        # Werkzeug-Aufschub: ab welcher Schemagroesse die Werkzeuge durch die Bruecke
        # tool_search/tool_describe/tool_call ersetzt werden.
        #
        # Gemessen am 02.09.2026: unsere 129 Werkzeuge wiegen ~14'170 Token, die
        # 10-Prozent-Vorgabe zog bei ~13'107. Wir reissen die Schwelle also um acht
        # Prozent -- und bezahlen dafuer teuer. Der Aufschub kostet pro Zug mindestens
        # eine zusaetzliche Runde (tool_search), und ``tool_call`` verlangt die
        # Argumente als JSON-Zeichenkette *innerhalb* eines JSON-Aufrufs. Mehrzeiligen
        # Python-Code da hineinzuschreiben, misslang dem lokalen Modell in einem
        # Auswertungslauf vier von vier Malen ("Unterminated string"); derselbe Code
        # lief als Direktaufruf durch.
        #
        # Der Prozentsatz muss gegen ``LOCAL_CONTEXT_LENGTH`` gerechnet werden, und das
        # sind 65'536 Token, nicht 131'072. Ein erster Anlauf setzte 15 Prozent in dem
        # Glauben, das Fenster sei doppelt so gross -- die Schwelle lag damit bei 9'830
        # Token, also *unter* der Werkzeugmenge, und die Bruecke blieb an. Die Absicht
        # war richtig, die Rechnung falsch.
        #
        # 25 Prozent ergeben 16'384 Token: oberhalb der 14'170, die unsere Werkzeuge
        # wiegen, und unterhalb des Qualitaetsknicks von 20'000, den Hermes selbst
        # nennt. Der Aufschub bleibt damit bestehen -- er greift erst, wenn die
        # Werkzeugliste wirklich gross wird. ``test_werkzeug_aufschub_greift_erst_am_
        # qualitaetsknick`` haelt beide Grenzen fest und rechnet gegen die Konstante,
        # damit die naechste Fensteraenderung nicht wieder still danebengreift.
        "tools": {
            "tool_search": {"enabled": "auto", "threshold_pct": 25.0},
        },
        # Tool-Loop-Guardrails: schuetzen vor Endlosschleifen/Token-Verbrennung.
        #
        # Die alte Grenze von 8 gleichartigen Fehlschlaegen war zu weit. Am 02.09.2026
        # verschrieb sich das lokale Modell bei einem Spaltennamen (``gewonn_en_am``
        # statt ``gewonnen_am``) und fand achtzehn Sandbox-Laeufe lang nicht zurueck --
        # obwohl DuckDB in *jeder* Fehlermeldung den richtigen Namen nannte. Zwei
        # zufaellige Teilerfolge setzten den Zaehler zurueck, der harte Stopp griff nie,
        # und nach 600 Sekunden brach der Lauf mit einem Zeitlimit ab.
        #
        # Das Bittere daran: Die Antwort lag nach dem zweiten Aufruf vollstaendig vor.
        # Alles danach war Beiwerk, das der Agent sich selbst aufgetragen hatte. Ein
        # frueher Stopp haette eine richtige Antwort erzwungen statt gar keiner --
        # darum 5 statt 8. In zwei ausgewerteten Laeufen mit zusammen 36 Sandbox-
        # Aufrufen hat sich das Modell nach dem dritten Fehlschlag in Folge kein
        # einziges Mal mehr gefangen.
        "tool_loop_guardrails": {
            "warnings_enabled": True,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 2,
                "same_tool_failure": 2,
                "idempotent_no_progress": 2,
            },
            "hard_stop_after": {
                "exact_failure": 3,
                "same_tool_failure": 5,
                "idempotent_no_progress": 3,
            },
        },
        "mcp_servers": mcp_servers,
    }


def write_hermes_config() -> Path:
    """Schreibt ``~/.hermes/config.yaml`` (Verzeichnisse werden angelegt)."""
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("skills", "memories", "sessions", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    config_path = home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(build_config_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    logger.info("Hermes-Config geschrieben: %s", config_path)
    return config_path
