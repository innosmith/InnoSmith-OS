# TaskPilot Docker Management
# Verwendung: make <target>

COMPOSE_SHARED = docker compose -f docker/docker-compose.yml
COMPOSE_INT    = $(COMPOSE_SHARED) -f docker/docker-compose.integration.yml --profile clamav
COMPOSE_PROD   = docker compose -p taskpilot-prod --env-file .env.prod -f docker/docker-compose.prod.yml

.PHONY: help dev int prod build down logs-int logs-prod logs-indexer status health vendor sandbox sandbox-executor test test-smoke test-contract test-e2e test-explore test-all reset-dev schema-int seed-int backup-prod backup-schedule backup-unschedule backup-status

help: ## Zeigt diese Hilfe
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Shared Infra ──────────────────────────────────────────

infra: ## Shared Infra starten (Postgres + LiteLLM)
	@docker network inspect taskpilot-shared >/dev/null 2>&1 || docker network create taskpilot-shared
	$(COMPOSE_SHARED) up -d

infra-down: ## Shared Infra stoppen
	$(COMPOSE_SHARED) down

# ── Development ───────────────────────────────────────────

dev: infra ## Dev-Infra starten (Backend+Frontend laufen bare-metal)
	$(COMPOSE_SHARED) --profile clamav up -d clamav
	@echo ""
	@echo "Shared Infra laeuft (Postgres :5435, LiteLLM :4000, ClamAV :3310)."
	@echo ""
	@echo "Backend starten:"
	@echo "  cd $(PWD) && source .venv/bin/activate"
	@echo "  PYTHONPATH=src/backend uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir src/backend"
	@echo ""
	@echo "Frontend starten:"
	@echo "  cd src/frontend && npm run dev"

# ── Integration ──────────────────────────────────────────

int: infra ## Integration starten (Full Docker + ClamAV)
	CACHEBUST=$$(date +%s) $(COMPOSE_INT) up -d --build

int-down: ## Integration stoppen (Shared Infra bleibt)
	$(COMPOSE_INT) down --remove-orphans

# ── Produktion ───────────────────────────────────────────

prod: ## Produktion starten (Standalone, eigene DB + ClamAV)
	@docker network inspect taskpilot-shared >/dev/null 2>&1 || docker network create taskpilot-shared
	CACHEBUST=$$(date +%s) $(COMPOSE_PROD) up -d --build

prod-down: ## Produktion stoppen
	$(COMPOSE_PROD) down

# ── Build ─────────────────────────────────────────────────

vendor: ## Vendor-Verzeichnis vorbereiten (private Packages)
	./docker/build.sh

sandbox: ## Sandbox-Image bauen
	docker build -t taskpilot-sandbox:latest docker/sandbox/

sandbox-executor: ## Sandbox-Executor-Image bauen (Sidecar mit docker.sock)
	docker build -t taskpilot-sandbox-executor:latest -f docker/Dockerfile.sandbox-executor .

build: vendor sandbox sandbox-executor ## Alle Images bauen (ohne Start)
	$(COMPOSE_INT) build
	$(COMPOSE_PROD) build
	@echo "Alle Images gebaut."

# ── Alles stoppen ─────────────────────────────────────────

down: ## Alle Umgebungen stoppen
	-$(COMPOSE_PROD) down 2>/dev/null
	-$(COMPOSE_INT) down 2>/dev/null
	-$(COMPOSE_SHARED) --profile clamav down 2>/dev/null
	@echo "Alle Container gestoppt."

# ── Logs ──────────────────────────────────────────────────

logs-int: ## Integration Logs (follow)
	$(COMPOSE_INT) logs -f

logs-prod: ## Produktion Logs (follow)
	$(COMPOSE_PROD) logs -f

logs-backend-int: ## Nur Backend-Int Logs
	docker logs -f taskpilot-backend-int

logs-backend-prod: ## Nur Backend-Prod Logs
	docker logs -f taskpilot-backend-prod

logs-indexer: ## Nur Index-Daemon-Prod Logs (Vektorisierung)
	docker logs -f taskpilot-indexer-prod

# ── Status ────────────────────────────────────────────────

status: ## Status aller TaskPilot-Container
	@docker ps --filter "name=taskpilot" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

health: ## Health-Checks ausfuehren
	@echo "=== Dev Backend ==="
	@curl -sf http://localhost:8000/api/health 2>/dev/null || echo "  nicht erreichbar"
	@echo ""
	@echo "=== Integration Backend ==="
	@curl -sf http://localhost:8100/api/health 2>/dev/null || echo "  nicht erreichbar"
	@echo ""
	@echo "=== Produktion Backend ==="
	@curl -sf http://localhost:8200/api/health 2>/dev/null || echo "  nicht erreichbar"
	@echo ""
	@echo "=== ClamAV ==="
	@echo "PING" | nc -w 2 localhost 3310 2>/dev/null || echo "  nicht erreichbar"
	@echo ""
	@echo "=== LiteLLM ==="
	@curl -sf http://localhost:4000/health 2>/dev/null || echo "  nicht erreichbar"
	@echo ""
	@echo "=== Ollama ==="
	@curl -sf http://localhost:11434/api/tags 2>/dev/null | head -c 100 || echo "  nicht erreichbar"
	@echo ""

# ── Tests ─────────────────────────────────────────────────

test: ## Backend Unit-Tests (pytest, Schicht 1)
	cd src/backend && ../../.venv/bin/python -m pytest tests/ -v --ignore=tests/e2e

test-smoke: ## Smoke-Tests gegen Integration (Schicht 2)
	.venv/bin/python -m pytest tests/smoke/ -v

test-contract: ## OpenAPI-Contract-Guard (gegen laufendes Backend, Default: Dev)
	.venv/bin/python -m pytest tests/contract/ -v

test-e2e: ## Playwright E2E-Tests gegen Integration (Schicht 3)
	.venv/bin/python -m pytest tests/e2e/ -v

test-explore: ## AI-Explorations-Audit mit browser-use (Schicht 4)
	.venv/bin/python tests/ai-audit/run_audit.py

test-all: test test-smoke ## Alle automatisierten Tests (Schicht 1-2 + Contract gegen INT)
	TP_SMOKE_BACKEND_URL=$${TP_SMOKE_BACKEND_URL:-http://localhost:8100} \
	.venv/bin/python -m pytest tests/contract/ -v
	@echo "Alle Tests bestanden."

# ── LLM-Eval ──────────────────────────────────────────────
# Misst Klassifikation UND Entwurfsqualitaet gegen den Golden Set. Nutzt denselben
# Schreib-Auftrag wie die Produktion (app.services.draft_prompt) -- fruehere
# Fassungen pflegten einen eigenen Prompt und massen ein Parallelsystem.

EVAL_MODELS ?= qwen3.5:35b
EVAL_LIMIT  ?= 60

eval: ## LLM-Eval nur Klassifikation, schnell (EVAL_MODELS=..., EVAL_LIMIT=...)
	.venv/bin/python scripts/eval/run_llm_eval.py \
		--models "$(EVAL_MODELS)" \
		--golden scripts/eval/golden_set.jsonl \
		--limit $(EVAL_LIMIT) --no-drafts --structured

eval-drafts: ## LLM-Eval inkl. Entwuerfe (Aehnlichkeit, Schweizer Schreibung, Platzhalter-Quote)
	.venv/bin/python scripts/eval/run_llm_eval.py \
		--models "$(EVAL_MODELS)" \
		--golden scripts/eval/golden_set.jsonl \
		--limit $(EVAL_LIMIT) --draft-sampling

# ── Backup ────────────────────────────────────────────────

SYSTEMD_USER_DIR = $(HOME)/.config/systemd/user
BACKUP_UNITS     = taskpilot-backup.service taskpilot-backup.timer

backup-prod: ## Prod-Backup auf OneDrive erstellen
	./scripts/backup-prod.sh

backup-schedule: ## Taegl. Backup aktivieren (03:00, systemd-timer)
	@mkdir -p $(SYSTEMD_USER_DIR)
	@cp systemd/taskpilot-backup.service $(SYSTEMD_USER_DIR)/
	@cp systemd/taskpilot-backup.timer $(SYSTEMD_USER_DIR)/
	@systemctl --user daemon-reload
	@systemctl --user enable --now taskpilot-backup.timer
	@echo ""
	@echo "Backup-Timer aktiviert (taeglich 03:00)."
	@echo ""
	@if ! loginctl show-user $(USER) 2>/dev/null | grep -q "Linger=yes"; then \
		echo "HINWEIS: Linger ist nicht aktiviert. Timer laeuft nur bei aktiver Session."; \
		echo "Fuer Ausfuehrung ohne Login:  sudo loginctl enable-linger $(USER)"; \
		echo ""; \
	fi
	@systemctl --user list-timers taskpilot-backup.timer

backup-unschedule: ## Taegl. Backup deaktivieren
	@systemctl --user disable --now taskpilot-backup.timer 2>/dev/null || true
	@rm -f $(SYSTEMD_USER_DIR)/taskpilot-backup.service
	@rm -f $(SYSTEMD_USER_DIR)/taskpilot-backup.timer
	@systemctl --user daemon-reload
	@echo "Backup-Timer deaktiviert und Unit-Dateien entfernt."

backup-status: ## Status des Backup-Timers anzeigen
	@echo "=== Timer-Status ==="
	@systemctl --user list-timers taskpilot-backup.timer 2>/dev/null || echo "  Timer nicht aktiv"
	@echo ""
	@echo "=== Letzter Lauf ==="
	@systemctl --user status taskpilot-backup.service 2>/dev/null | head -15 || echo "  Noch kein Lauf"
	@echo ""
	@echo "Logs:  journalctl --user -u taskpilot-backup -n 50"

# ── DB-Schema & Migration ────────────────────────────────

reset-dev: infra ## DEV-DB komplett zuruecksetzen (DROP + Schema + Seed + Alembic)
	@docker exec taskpilot-postgres psql -U taskpilot -d postgres \
		-c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='taskpilot_dev' AND pid != pg_backend_pid();" > /dev/null 2>&1 || true
	docker exec taskpilot-postgres psql -U taskpilot -d postgres \
		-c "DROP DATABASE IF EXISTS taskpilot_dev;"
	docker exec taskpilot-postgres psql -U taskpilot -d postgres \
		-c "CREATE DATABASE taskpilot_dev OWNER taskpilot;"
	docker exec taskpilot-postgres psql -U taskpilot -d taskpilot_dev \
		-c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"; CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
	docker exec -i taskpilot-postgres psql -U taskpilot -d taskpilot_dev < db/schema.sql
	docker exec -i taskpilot-postgres psql -U taskpilot -d taskpilot_dev < db/seed.sql
	cd src/backend && PYTHONPATH=. ../../.venv/bin/alembic stamp head
	@echo ""
	@echo "DEV-DB zurueckgesetzt. Backend starten → Owner wird automatisch angelegt."

schema-int: ## Schema direkt auf taskpilot_int anwenden (frische DB)
	docker exec -i taskpilot-postgres psql -U taskpilot -d taskpilot_int < db/schema.sql
	@echo "Schema auf taskpilot_int angewendet."

seed-int: ## Seed-Daten in INT-DB einspielen (idempotent, ON CONFLICT DO NOTHING)
	docker exec -i taskpilot-postgres psql -U taskpilot -d taskpilot_int < db/seed.sql
	@echo "Seed-Daten in taskpilot_int eingespielt."

migrate-dev: ## Alembic-Migration auf Dev-DB ausfuehren
	cd src/backend && PYTHONPATH=. ../../.venv/bin/alembic upgrade head

migrate-int: ## Alembic-Migration auf Integration-DB ausfuehren
	docker exec taskpilot-backend-int alembic upgrade head

migrate-prod: ## Alembic-Migration auf Produktion-DB ausfuehren
	docker exec taskpilot-backend-prod alembic upgrade head
