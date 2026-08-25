# AI9 — Core-Extraktion aus TaskPilot

Lebendes Arbeitsdokument zur Ausgliederung des wiederverwendbaren Core **AI9**
(Anthonys IP, privates Repo) aus TaskPilot. TaskPilot und künftige Kunden-Apps
(z. B. Treuhand) werden schlanke Apps auf demselben Core.

Strategie-Referenz: `.cursor/plans/core-plattform_strategie_*.plan.md`.

## Leitinvariante

**App importiert Core — Core niemals App.** Jede neue Abhängigkeit wird gegen
diese Richtung geprüft.

## Grundentscheide

- Ein Deployment = eine Organisation, eigene DB, strikte Artefakt-Trennung. Nur
  der Core ist gemeinsam. Kein Multi-Tenant, nur `user_id`-Scope pro Deployment.
- Scope-Freeze Kunde v1: Chat (alle) + Mail-Assist (1–2). Lokal-first als Default.
- Name: **AI9** (intern/privat; keine Domain/Registry nötig).

## Zwei Regeln, die bisher nur im Kopf standen

Sie haben mehrfach Entscheide getragen, waren aber nirgends aufgeschrieben — und
eine ungeschriebene Architekturregel überlebt keine drei Monate.

### Regel der Zwei

> Ein Baustein wandert in den Core, sobald ein **zweiter** Verbraucher ihn
> braucht. Vorher bleibt er in der App, die ihn erfunden hat.

Der Grund ist nicht Sparsamkeit, sondern Erkenntnis: Beim ersten Verbraucher ist
noch nicht sichtbar, welcher Teil allgemein und welcher zufällig ist. Erst der
zweite Fall trennt beides. Wer früher extrahiert, giesst eine Besonderheit in
Beton und muss sie später mit Sonderfällen aufweichen.

Die Regel hat eine Kehrseite, die genauso verbindlich ist: **Ist der zweite
Verbraucher da, wird extrahiert — nicht kopiert.** Ein zweites Mal derselbe Code
in einem anderen Repo ist der teuerste Zustand von allen, weil Fehlerbehebungen
ab dann doppelt anfallen und trotzdem auseinanderlaufen.

Woher der Code stammt, ist eine getrennte Frage. Hat eine App den Baustein
bereits erprobt, wird von dort **gehoben**; gibt es ihn nirgends, entsteht er
gleich im Core.

### Keine Messenger-Kanäle

> Hermes wird ausschliesslich als Bibliothek eingebunden (`AIAgent`), nie als
> Gateway. In keiner erzeugten `config.yaml` steht ein Kanal.

Hermes bringt Anbindungen für Telegram, Discord, Slack, WhatsApp und E-Mail mit,
und die Dokumentation legt sie als Weg zum Mehrbenutzerbetrieb nahe
(`group_sessions_per_user`). Für uns ist das ausgeschlossen: Die Nachrichten
liefen über fremde Server, und bei Telegram kommt der Standort dazu. Der
Mehrbenutzerbetrieb läuft bei uns über **Profile** (siehe unten), nicht über
Kanäle.

Praktisch heisst das: `enabled_toolsets` wird immer **ausdrücklich** übergeben.
Hermes meldet beim Start zwar `discord`, `feishu_doc` und Verwandtes als
vorhanden, lädt sie aber nur bei ausdrücklicher Freigabe. Eine Allowlist ist hier
Pflicht, keine Bequemlichkeit.

## Phase 0 — Codebase-Audit (Befund)

### Grösse (gemessen)

| Bereich | Umfang |
|----|----|
| Python (ohne vendor) | ~54'800 Zeilen, 193 Dateien |
| Frontend (ts/tsx) | ~38'400 Zeilen, 104 Dateien |
| Backend-App allein | ~35'500 Zeilen |
| DB-MCP (`mcp-taskpilot/server.py`) | 573 Zeilen, 10 Tools |

→ Substanziell, aber inkrementell extrahierbar. Kein Big-Bang.

### Zentrale Altlast: Owner-Singleton

Das Idiom `select(User).where(User.role == "owner")` löste an ~10 Stellen
dupliziert „wer bin ich / wessen Settings / wessen Mailbox" auf. Das ist die
Kernschuld für den Mehrbenutzer-Betrieb — und ihr Abbau deckt sich exakt mit dem
Kundenbedarf.

### Weitere strukturelle Befunde

- **Credentials global + prozessweit:** eine Graph-App/Mailbox (`config.py`);
  `populate_hermes_env()` schreibt Owner-Secrets prozess-global in `os.environ`.
  → Härtester Brocken für Mail-pro-Person (Phase D): Env-Injektion pro Job nötig.
- **Kein User-Scope** auf `agent_jobs`, `email_triage`, `sender_profiles`,
  `semantic_documents`, `learned_rules`, `sent_mail_examples` (Phase D).
- **Chat ist bereits pro Person** (`llm_conversations.user_id`) → Feature-Set 1
  datenmodell-seitig nah dran.
- **Graph-Client ist pro Mailbox adressierbar** (`/users/{email}`); nur die
  Identitätsherkunft ist global.

## Owner-Singleton-Inventar (Call-Sites)

Status nach Phase-A-Schritt 1 (Zentralisierung via `app/services/principal.py`):

| Datei | Vorher | Status |
|----|----|----|
| `services/briefing.py` | lokaler `_get_owner` | → `principal.get_owner` |
| `services/notification.py` | lokaler `_get_owner` (active_only) | → `principal.get_owner(active_only=True)` |
| `services/pipeline_promoter.py` | `_get_owner_id` (gecacht) | → `principal.get_owner_id` (Cache bleibt lokal) |
| `services/followup.py` | inline Query | → `principal.get_owner` |
| `services/triage.py` | inline Query | → `principal.get_owner` |
| `services/hermes_worker.py` | inline Query (settings) | → `principal.get_owner_settings` |
| `services/semantic_index.py` | inline Query (settings) | → `principal.get_owner_settings` |
| `services/llm_defaults.py` | inline Query (settings) | → `principal.get_owner_settings` |
| `services/hermes_config.py` | inline Query (settings) | → `principal.get_owner_settings` |
| `auth/deps.py` (`_authenticate_via_api_key`) | inline Query | **bewusst offen** — Extension-API-Key ist echt owner-gebunden; Revision in Phase D |

Das zentrale Modul `principal` ist die künftige Naht: In Phase D wird
`get_owner*` durch die Auflösung des tatsächlich handelnden Users ersetzt, ohne
die Call-Sites erneut anzufassen.

## Core- vs. App-Klassifikation (Arbeitsstand)

| Kandidat Core (AI9) | Bleibt App-spezifisch |
|----|----|
| `principal` (Acting Principal) | Triage-Post-Processing (Klassen, Skills) |
| Chat/Konversationen + SSE | `briefing`, `followup`, `pipeline_promoter`, `recurring` |
| Hermes-Runtime (`hermes_config`, Job-Runner-Kern) | `mcp-taskpilot` (Tasks/Projekte) |
| Anonymisierung (`content`, `mapping_store`) | `mcp-pipedrive/signa/bexio/invoiceinsight/toggl` |
| Semantik-Pipeline (`embeddings`, `semantic_index/search`) + Source-Adapter-IF | Cockpit/Agenda/Kanban/Finance/Capacity |
| Sandbox/Scripts, Graph-Client | Skills/Memory Anthony (`SOUL.md`, Schreibstil) |

### DB-MCP-Split (`mcp-taskpilot`, 573 LOC)

- **Core (`mcp-ai9`):** `get/list/update_agent_job`, `get/update_sender_profile`,
  `semantic_search_documents`.
- **App TaskPilot (`mcp-taskpilot`):** `list_projects`, `list_tasks`, `get_task`,
  `create_task`, `update_task`.

## Der zweite Verbraucher ist da (gsw-cockpit)

Damit greift die Regel der Zwei für fünf Bausteine. Sie werden **AI9 v0.4.0**.
Woher der Code stammt, entscheidet sich pro Baustein:

| Modul | Herkunft | Warum jetzt |
|----|----|----|
| `ai9.hermes` | `services/hermes_config.py` + `build_chat_agent` | Beide Produkte brauchen Hermes; Profile pro Person braucht keines von beiden allein |
| `ai9.schleuse` | `hermes_worker._schleuse_nach_draussen` | Ein Kunde arbeitet mit öffentlichem Modell — dort entscheidet die Schranke, ob Mandantendaten das Haus verlassen |
| `ai9.dokumentkontext` | `services/conversation_context.py` + lokaler Teil `context_resolver.py` | Dokumente im Chat will jeder |
| `ai9.einstellungen` | `core/principal.get_principal_settings`, um die Instanzebene erweitert | «Haus setzt Vorgabe, Person weicht ab» gibt es in TaskPilot nur einstufig |
| `ai9.laeufe` | `gsw-cockpit/services/laeufe.py` + Protokollidee aus `agent_jobs` | Lange Antworten und Wiederverbindung sind produktunabhängig |

**Bleibt im Produkt:** Routen, Oberfläche, Vokabular, Skills, die konkreten
Einstellungsschlüssel, Navigationsfreigabe.

### Nachtrag: vier davon sind noch nicht erprobt

Gezählt nach dem Bau (Dateien mit direktem Import):

| Modul | gsw-cockpit | InnoSmith OS | Stand |
|----|----:|----:|----|
| `content_converter` | 2 | 5 | **erprobt** |
| `mapping_store` | 3 | 2 | **erprobt** |
| `hermes` | 6 | 0 | noch nicht erprobt |
| `einstellungen` | 2 | 0 | noch nicht erprobt |
| `laeufe` | 1 | 0 | noch nicht erprobt |
| `dokumentkontext` | 1 | 0 | noch nicht erprobt |
| `schleuse` | 0 | 0 | **ohne Verbraucher** |

Die Regel der Zwei ist oben aufgeschrieben und im selben Zug fünfmal nicht
angewandt worden. Der Anlass war jedesmal plausibel — TaskPilot *wird* Hermes
brauchen, ein Kunde *wird* ein öffentliches Modell nutzen —, aber «wird» ist
nicht «tut». `ai9.schleuse` steht bis heute ohne jeden Aufrufer.

**Was das praktisch heisst:** Die Form dieser Module ist geraten, nicht erprobt.
Beim ersten echten zweiten Verbraucher werden sie sich biegen, und das Biegen
eines separat versionierten Pakets kostet ungleich mehr als das Biegen einer
Datei im Produkt. Zwei Beispiele aus dem ersten Tag: Der Schutz `if text:` war in
TaskPilot vorhanden, ging beim Herausheben verloren und erschien im Cockpit als
Wort «None» in der Antwort; das stille Datenleck beim einzelnen Restbestand
musste an drei Stellen behoben werden.

**Folgerung:** Kein weiteres Modul ohne zweiten echten Verbraucher. Wo eine
Invariante zu wertvoll ist, um sie zu verdoppeln, aber die zweite Umsetzung noch
fehlt, wird die **Regel** geteilt und nicht der Code — siehe
`AI9/docs/postfach-disziplin.md`.

### Was gsw-cockpit besser gelöst hat

Beim Werkzeugaufruf schickt `routers/chat.py` nur den **Namen** ans Frontend
(`_emit("tool_start", str(name))`). Man sieht «web_search», aber nicht, wonach
gesucht wurde — die Anfrage steht nur im Server-Protokoll `web_searches`. Wer
nicht sehen kann, was das Haus verlassen hat, kann eine Websuche auch nicht
verantworten. Die Suchanfrage gehört ins Ereignis; das fliesst zurück.

## Hermes im Mehrbenutzerbetrieb (gemessen, nicht vermutet)

Sonden gegen `hermes-agent==0.18.0` im Abbild `taskpilot-backend:prod`:

| Frage | Befund |
|----|----|
| Trennung pro Person | `set_hermes_home_override()` setzt eine **ContextVar**, nicht `os.environ` — ausdrücklich so gebaut. `asyncio.to_thread` trägt sie in den Arbeitsthread. Ein Prozess genügt für alle Mitarbeitenden |
| Grenze | Ein selbst gestarteter `threading.Thread` erbt sie **nicht** und fällt auf `os.environ["HERMES_HOME"]` zurück |
| Was das Profil umfasst | `config.yaml`, `SOUL.md`, `memories/`, `skills/`, `sessions/`, `state.db`, `cache/` |
| Denkmodus | `request_overrides={"reasoning_effort": "none"}` — Ollamas `/v1` beachtet es: 0 Denkzeichen und 0.6s statt 7.8s |
| Websuche | Toolset `web` liefert `web_search` (ddgs, kein Schlüssel) und `web_extract` (Tavily, Schlüssel nötig). Die Suchanfrage steht im `args` des `tool_start_callback` |
| Rückgabe | `final_response`, dazu `input_tokens`, `output_tokens`, `api_calls`, `completed`, `interrupted` — genug für ein Laufprotokoll |
| Agentbau | **24s einmal je Prozess**, danach 0.02s. Also beim Start vorwärmen, wie beim Anonymisierungsmodell |
| Netzzwang | Keiner. Mit Netz holt Hermes einmalig Modell-Metadaten von OpenRouter (Preise, Kontextlängen) in `cache/`; ohne Netz läuft alles weiter |

**Folgerung für den Aufbau:** Das prozessweite `HERMES_HOME` zeigt auf ein
**neutrales Basisverzeichnis** mit dem gemeinsamen Skill-Vorrat und ohne
Gedächtnisdateien. Fällt je ein roher Thread aus dem Kontext, landet er dort —
und nicht im Profil einer anderen Person.

**Gewählt:** Gedächtnis pro Person, Verfahren gemeinsam. Ein geteiltes
`MEMORY.md` wäre im Treuhandumfeld ein Berufsgeheimnis-Problem: Was der Agent aus
dem Mandatsgespräch der einen gelernt hat, könnte im Chat der anderen auftauchen.
Dazu gehört zwingend eine Stelle in der Oberfläche, an der jede Person ihr
eigenes Gedächtnis **einsehen und löschen** kann.

### Nachtrag: MCP bricht die Profiltrennung nicht

Aus der Grenze oben — ein roher Thread erbt den Vorrang nicht — war zunächst
geschlossen worden, MCP sei mit Profilen unvereinbar, weil `tools/mcp_tool.py`
eine Hintergrund-Ereignisschleife in einem rohen Thread führt. `ai9.hermes`
schrieb darum kein `mcp_servers`. **Der Schluss war falsch**, und das war der
Unterschied zwischen «gemessen» und «plausibel»: Gemessen war nur, dass ein
*beliebiger* roher Thread den Vorrang verliert.

Nachgemessen mit `gsw-cockpit/src/backend/scripts/sonde_mcp_profile.py`:

| Frage | Befund |
|----|----|
| Roher Thread | sieht das neutrale Verzeichnis — die allgemeine Aussage stimmt |
| MCP-Schleife | sieht das **Profil der Person**. `_wrap_with_home_override` trägt den Vorrang ausdrücklich hinüber, und jeder Werkzeugaufruf geht über `_run_on_mcp_loop` dort hindurch |
| Zwei Personen gleichzeitig | bleiben getrennt; der Vorrang wird je Aufgabe gesetzt und zurückgenommen |
| Bleibende Grenze | Ein MCP-Server ist ein **Unterprozess** und erbt `os.environ`, nie eine ContextVar. Er sieht das neutrale Verzeichnis. Folgenlos für Fachserver mit eigener Datenquelle; wer einen MCP-Server baut, der Hermes-Dateien liest, muss die Person als Argument bekommen |
| Freischaltung | Hermes filtert **auch MCP-Werkzeuge** gegen `enabled_toolsets`. Ein Server, der nur in der `config.yaml` steht, startet trotzdem — seine Werkzeuge werden nur nie angeboten. `ai9.hermes` verweigert darum den Start, statt still zu verarmen |

**Folge:** `Umgebung.mcp_server` gibt es seit AI9 v0.4.4, ab Werk leer. Damit
kann derselbe Kern die Fachwerkzeuge von TaskPilot tragen — und GSW eigene
bekommen, ohne dass ein zweiter Strang entsteht.

## Ausgliederung vollzogen: Schwester-Repo `ai9`

Die zunächst interne Grenze `app/core/` ist als **eigenständiges Paket `ai9`** in
ein Schwester-Repo ausgegliedert (`/home/innosmith/dev/github/AI9`, auf gleicher
Ebene wie TaskPilot). TaskPilot bindet es via **Editable-Install** ein
(`pip install -e ../AI9`) und importiert direkt aus `ai9` (z. B.
`from ai9.embeddings import embed_text`).

- **Config via Dependency Injection:** `ai9.config` definiert das Protokoll
  `CoreSettings` (nur die Felder, die der Core braucht) + `configure()/`
  `get_core_settings()`. TaskPilots `Settings` erfüllt es per strukturellem Typing.
  Verdrahtung im **Composition Root** `app/__init__.py` (garantierter Choke-Point:
  jeder Import unter `app.*` triggert das Wiring, bevor der Core Config liest).
- **Erster extrahierter Batch (ai9 v0.1.0):** `config`, `embeddings`,
  `semantic_search`, `mapping_store`, `content_converter`.
- **In `app/core/` verbleibt nur `principal`:** hängt an `app.models.User` und ist
  Kernfrage des Mehrbenutzer-Umbaus (Phase D) → wandert erst mit dem Identity-Modell.
- **Multi-Root-Workspace:** `TaskPilot/ai9-taskpilot.code-workspace` bündelt beide
  Repos in einem Cursor-Fenster.
- **requirements.txt:** `ai9` als privates Package dokumentiert (Muster wie
  `contentconverter`), kein hartes `-e` (Docker-Build-Schutz); Docker vendored.
- **Verifikation:** ai9 standalone 4 passed; TaskPilot volle Suite 14 failed /
  590 passed = **identisch zur Baseline** (null Regression).

Die frühere „interne Grenze"-Beschreibung ist damit überholt; Extraktions-Pfad war
`app.services.X` → `app.core.X` → `ai9.X`.

### Phase A/3 — Anonymisierungs-Infrastruktur migriert

- `mapping_store` (In-Memory-Session-Store Fake↔Original, TTL, RAM-only) →
  `app/core/mapping_store.py`. Rein, nur `cachetools` + `app.config`.
- `content_converter` (MCP-stdio-Client für `cconv`: anonymize/deanonymize +
  Dokument-Export) → `app/core/content_converter.py`. Nur `mcp` + `app.config` +
  stdlib.
- Importer umgeleitet: `main.py` (Lifespan start/stop), `routers/content.py`,
  `routers/analysis.py`, `services/document_export.py`. Keine verbleibenden
  `app.services.{content_converter,mapping_store}`-Referenzen; keine Test-Patches
  betroffen.
- Der HTTP-Router `routers/content.py` bleibt bewusst app-seitig (FastAPI-Wiring)
  und ruft die Core-Bausteine auf — die App-→-Core-Richtung bleibt gewahrt.
- Offene Kopplung: beide Module lesen `app.config.get_settings` (nur
  `mapping_keys_ttl_seconds` bzw. `contentconverter_*`). Config-Abstraktion des
  Core ist ein späterer Schritt (Phase C), nicht blockierend.

### Phase A/3b — Semantik-Lese-Grundlage migriert

- `embeddings` (lokales Ollama-Embedding, `embed_text`/`to_pgvector`) →
  `app/core/embeddings.py`. Rein: nur `httpx` + `app.config`.
- `semantic_search` (Hybrid-Retrieval RRF über `semantic_documents`, `hybrid_search`,
  `rrf_fuse`) → `app/core/semantic_search.py`. Generisch: nur `app.config`,
  `sqlalchemy`, Core-`embeddings`.
- Importer umgeleitet: `services/semantic_index.py`, `services/style_store.py`,
  `services/learning.py`, `routers/search.py`, `tests/test_semantic_search.py`.
- **Bewusst app-seitig belassen:** `services/semantic_index.py` (Schreib-/Ingest-
  Seite, 667 LOC). Es ist der deployment-spezifische **Source-Adapter** (OneDrive/
  Graph-Walking, `_DEFAULT_EXCLUDED_PATHS`, Owner-Settings). Der Core erhält später
  ein Source-Adapter-Interface (Phase E); der OneDrive-Adapter selbst bleibt
  App/Deployment. `semantic_index` importiert `embeddings` aus dem Core (app→core).
- **Keine Daten-/Schema-Berührung:** reine Modul-Verschiebungen. Weder die Tabelle
  `semantic_documents`, die `embedding`-Spalte (halfvec), noch `embed_dim`/
  `search_embed_dim` wurden verändert → gespeicherte Vektoren bleiben unangetastet,
  kein Re-Indexing nötig.

## Branding

- `mfa_issuer` leitet sich jetzt aus `app_name` ab (statt Literal „TaskPilot").
- `app_name`, `cors_origins` sind bereits env-konfigurierbar (`TP_APP_NAME`,
  `TP_CORS_ORIGINS`); `main.py` nutzt `app_name` für Titel + Health.

## Verifikation (Regressionsnachweis)

Volle Backend-Suite (`pytest tests/ --ignore=tests/e2e`):
**mit** meinen Änderungen 14 failed / 590 passed — **Baseline ohne** (`git stash`)
ebenfalls 14 failed / 590 passed → **null Regression**. Die 14 Fehler sind
vorbestehende DB-Event-Loop- und LLM-Integrationstests (kein Bezug zum Umbau).

## Fortschritt

- [x] Phase 0: Audit + Owner-Singleton-Inventar
- [x] Phase A/1: Zentrales `principal`-Modul + alle Service-Call-Sites umgeleitet
      (verhaltensneutral; keine Import-Zyklen)
- [x] Phase A/2: Interne `core/`-Paketgrenze etabliert (Seed: `principal`) +
      Branding (`mfa_issuer`) konfigurierbar (null Regression verifiziert)
- [x] Phase A/3a: Anonymisierungs-Infrastruktur (`mapping_store`,
      `content_converter`) nach `app/core/` migriert (null Regression verifiziert)
- [x] Phase A/3b: Semantik-Lese-Grundlage (`embeddings`, `semantic_search`) nach
      `app/core/` migriert; `semantic_index` (Source-Adapter) bewusst app-seitig
- [x] Phase C/1: Config-Provider (DI) + Composition Root; 4 Module entkoppelt
      (null `app.*`-Importe)
- [x] Phase C/2: Schwester-Repo `ai9` erstellt (pyproject, src/ai9, tests, git init
      `main`), 5 Module extrahiert, TaskPilot auf `ai9.*` umgestellt, Editable-Install,
      Multi-Root-Workspace (null Regression verifiziert)
- [x] Phase C/3: Docker-Vendoring für `ai9` (`docker/build.sh` kopiert `../AI9` nach
      `src/backend/vendor/ai9`, `Dockerfile.backend` installiert es von dort). Baubar
      als Wheel verifiziert. Offen (nicht blockierend): SemVer/Git-Ref-Pinning statt
      lokalem Arbeitsstand für 100% reproduzierbare Builds.
- [ ] Phase A/3c: Source-Adapter-Interface für den Index entwerfen (Vorbereitung Phase E)
- [ ] Phase B: Feature-Set 1 (Chat + Anonymisierung + Sandbox) mehrbenutzerfähig
      → wird durch AI9 v0.4.0 getragen: `hermes` (Profile pro Person), `laeufe`,
      `einstellungen`, `schleuse`, `dokumentkontext`. Erster Verbraucher ist
      gsw-cockpit; TaskPilot zieht danach nach
- [ ] Phase C: Core in privates Schwester-Repo + Vendoring
- [ ] Phase D: Credential-/Config-Store pro User + Mail-Agent (M365 App-only + AAP)
- [ ] Phase E: DMS-Source-Adapter + Index-Scoping
