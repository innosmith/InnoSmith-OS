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
- [ ] Phase C: Core in privates Schwester-Repo + Vendoring
- [ ] Phase D: Credential-/Config-Store pro User + Mail-Agent (M365 App-only + AAP)
- [ ] Phase E: DMS-Source-Adapter + Index-Scoping
