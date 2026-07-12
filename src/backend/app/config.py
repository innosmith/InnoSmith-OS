from pathlib import Path
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings

try:
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
except IndexError:
    _PROJECT_ROOT = Path("/app")


class Settings(BaseSettings):
    app_name: str = "TaskPilot"
    app_env: str = "prod"
    debug: bool = False

    @property
    def mfa_issuer(self) -> str:
        """Issuer-Name für TOTP (Google Authenticator etc.).

        Leitet sich vom konfigurierbaren ``app_name`` ab (Branding pro Deployment):
        Prod → '<app_name>', sonst '<app_name>-Dev' / '<app_name>-Int'.
        """
        if self.app_env in ("prod", "production"):
            return self.app_name
        return f"{self.app_name}-{self.app_env.capitalize()}"

    # PostgreSQL
    db_host: str = "localhost"
    db_port: int = 5435
    db_user: str = "taskpilot"
    db_password: str = "taskpilot_dev_2026"
    db_name: str = "taskpilot_dev"

    @property
    def database_url(self) -> str:
        pwd = quote_plus(self.db_password)
        return (
            f"postgresql+asyncpg://{self.db_user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def database_url_sync(self) -> str:
        pwd = quote_plus(self.db_password)
        return (
            f"postgresql+psycopg://{self.db_user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # Auth
    secret_key: str = "dev-secret-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60  # 1h Access-Token
    refresh_token_expire_hours: int = 168  # 7d Refresh-Token (Owner), 4h fuer Member

    # Owner (Phase 0: nur ein User) — Werte kommen aus .env.dev / .env.integration / .env.prod
    owner_email: str = ""
    owner_password: str = ""
    owner_display_name: str = ""

    # CORS
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://tp.innosmith.ai",
    ]

    # Hermes Agent-Runtime
    # Home-Verzeichnis fuer Hermes (config.yaml, skills/, memories/, SOUL.md).
    # Ersetzt das fruehere ~/.nanobot/workspace.
    hermes_home: str = "~/.hermes"

    # Unsplash (optional, for background image search)
    unsplash_access_key: str = ""

    # Microsoft Graph API (E-Mail-Integration)
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_user_email: str = ""
    # Region fuer die Microsoft Search API im App-only-Modus (Pflicht dort;
    # z. B. "CHE"/"EUR"). Leer = Search-API-Doc-Preview wird uebersprungen, der
    # semantische Index + lokale tsvector-Suche laufen unabhaengig davon.
    graph_search_region: str = ""

    # LiteLLM Gateway
    litellm_base_url: str = "http://localhost:4000"
    ollama_base_url: str = "http://localhost:11434"
    # Triage-/Worker-Modell (lokal via Ollama). ACHTUNG: Der Tag ``:latest`` ist
    # GLEITEND -- ein Ollama-Modell-Update kann die Triage-Qualitaet ueber Nacht
    # veraendern (beobachtet als Regression). Fuer reproduzierbares Verhalten in
    # Prod einen FIXEN Tag/Digest via ``TP_TRIAGE_MODEL`` setzen und Kandidaten
    # vorher mit der Eval-Suite (scripts/eval/) vergleichen.
    triage_model: str = "ollama/qwen3.6:latest"

    # Agent-Memory / Lernen (lokale Embeddings via Ollama)
    # Qwen3-Embedding-0.6B: SOTA-Familie (MTEB-Multilingual #1 bei 8B), exzellentes
    # Deutsch, native Dimension 1024 -> passt exakt zum vector(1024)-Schema.
    embed_model: str = "qwen3-embedding:0.6b-fp16"
    embed_dim: int = 1024
    # Episodischen Recall + Few-Shot-Injektion ein-/ausschalten (Saeule 2)
    agent_recall_enabled: bool = True
    # Reflexions-Job (lokal): konsolidiert Korrektursignale zu Regel-Vorschlaegen
    agent_reflection_enabled: bool = True
    # Intervall des Reflexions-Jobs in Sekunden (Default: taeglich)
    agent_reflection_interval_seconds: int = 86400
    # Mindestzahl gleichartiger Korrekturen, bevor eine Regel vorgeschlagen wird
    agent_reflection_min_occurrences: int = 2
    # Structured-Output-Rettung im Fallback: Wenn der agentische Triage-Loop keinen
    # verwertbaren JSON-Block liefert, wird EIN tool-freier Klassifikations-Call an
    # Ollama gestellt (schema-constrained -> parse-garantiert), bevor fail-closed
    # auf fyi/needs_review zurueckgefallen wird. Bewusst NICHT global im Agenten-
    # Loop (request_overrides gilt fuer jeden Turn und wuerde Tool-Calls brechen).
    # Default AN: strikt nicht-destruktiv (greift nur bei fehlendem JSON-Block) und
    # Best Practice (constrained Decoding = 100% valides JSON statt fail-closed).
    triage_structured_fallback: bool = True
    # Thinking (Reasoning) fuer den Triage-Job deaktivieren. Best-Practice-Belege:
    # "Thinking"-Modelle verbrennen bei reiner Klassifikation Tokens/Latenz und
    # liefern eher schlechteres JSON. ABER unsere Triage ist agentisch mit Tool-
    # Use (Mails verschieben/kategorisieren) -- Thinking-Aus kann die Tool-Wahl
    # kleiner Modelle verschlechtern. Default AUS (Thinking an); erst nach Eval-
    # Beleg (scripts/eval/ --no-think) aktivieren.
    triage_disable_thinking: bool = False
    # Confidence-Schwelle: Klassifikationen unterhalb dieses Wertes werden im
    # Cockpit als needs_review markiert (Best-Practice-Audit-Bucket), statt still
    # durchzugehen. Nur wirksam, wenn das Modell eine Confidence liefert.
    triage_low_confidence_threshold: float = 0.5
    # Tool-Scoping fuer Triage-Jobs: reduzierter Agent (Core ohne web + graph +
    # taskpilot) statt aller ~113 MCP-Tools. Weniger Prompt-Ballast = bessere
    # Tool-Wahl des lokalen Modells; zugleich Datenminimierung (keine Websuche
    # mit E-Mail-Inhalten aus der Triage heraus). Per Flag reversibel.
    triage_tool_scoping: bool = True

    # Zwei-Pass-Entwurf (Best Practice: klassifizieren -> schreiben trennen). Wenn
    # aktiv, erstellt der Klassifikations-Lauf KEINEN Entwurf; sobald auto_reply
    # feststeht, schreibt ein zweiter, fokussierter Agenten-Lauf den Draft mit
    # Prosa-Sampling und eigenem Kontext (email-style, Thread, Stil-Anker) -- ohne
    # JSON-/Tool-/Klassifikations-Druck. Hebt die sprachliche Qualitaet, jederzeit
    # per Flag reversibel.
    two_pass_draft: bool = True
    # Prosa-Sampling fuer den Schreib-Pass -- offizielle Qwen-3.6-Instruct-Empfehlung
    # (temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5). Bewusst NICHT
    # temp=0: deterministisches Sampling erzeugt flaches, repetitives Deutsch. Nur
    # fuer den Draft-Pass; die Klassifikation bleibt deterministisch.
    draft_temperature: float = 0.7
    draft_top_p: float = 0.8
    draft_top_k: int = 20
    draft_presence_penalty: float = 1.5

    # Semantische Suche (user-facing Dokument-/E-Mail-Index, pgvector).
    # Bewusst GETRENNT vom 0.6B-Agent-Index: staerkeres Modell (Qwen3-Embedding-4B,
    # native 2560d) fuer maximale Retrieval-Qualitaet (MTEB-Multilingual 69.45 vs
    # 64.33 bei 0.6B). Speichertyp halfvec (fp16): bei 2560d ist float32-HNSW nicht
    # moeglich (pgvector-Cap 2000d; halfvec hebt auf 4000d), Recall-Verlust < 0.5 %.
    search_index_enabled: bool = True
    search_embed_model: str = "qwen3-embedding:4b-fp16"
    search_embed_dim: int = 2560
    # Ingest-Scheduler (lokale Embeddings, best-effort)
    search_index_interval_seconds: int = 86400  # taeglich
    search_index_mail_top: int = 500            # Fallback fuer den alten One-Off-Aufruf
    # In-Process-Scheduler im Backend. Default AUS: die Indexierung laeuft als
    # dedizierter Daemon-Container (app.scripts.index_daemon), damit die schwere
    # Embedding-Arbeit den API-Prozess nicht konkurrenziert und keine Doppelarbeit
    # (Backend + Daemon) entsteht.
    search_index_in_process: bool = False
    # Volle E-Mail-Pagination (nextLink): Seitengroesse + Obergrenze je Ordner.
    search_index_mail_page_size: int = 200      # $top je Graph-Seite
    search_index_mail_max_per_folder: int = 0   # 0 = unbegrenzt (Voll-Archiv)
    search_index_max_file_mb: int = 25          # groessere Dateien ueberspringen
    search_index_max_chunks_per_doc: int = 300  # Ausreisser-Cap (riesige PDFs)
    # Chunking (Zeichen)
    search_chunk_chars: int = 1000
    search_chunk_overlap: int = 150
    # Retrieval. Grosszuegig gehalten -- die semantische Suche ist der bewusste,
    # nicht-per-Tastendruck-Pfad; hier soll man vollstaendig finden, nicht ein
    # kleines Fenster sehen. ANN mit LIMIT ist auch bei einigen Hundert schnell.
    search_semantic_k: int = 50                 # finale Trefferzahl (Default)
    search_semantic_k_max: int = 500            # harte Obergrenze fuer den Endpoint
    search_candidate_k: int = 300               # Kandidaten vor Rerank/Fusion
    # Reranker (tiefer/agentischer Pfad) -- Cross-Encoder ueber die Kandidaten.
    # Default AUS, bis das Modell lokal vorliegt; interaktiver UI-Pfad nutzt ihn nie.
    search_reranker_enabled: bool = False
    search_reranker_model: str = "qwen3-reranker:4b"

    # Style-Store (lokaler Few-Shot-Speicher gesendeter Mails, pgvector). Ein
    # periodischer Sync indexiert Anthonys gesendete Antworten; pro Draft werden
    # die stilistisch/thematisch passendsten als Few-Shot-Anker geholt -- auch fuer
    # neue Kontakte ohne History. Bleibt vollstaendig on-prem (lokale Embeddings).
    style_store_enabled: bool = True
    # Sync-Intervall (Default: taeglich) und Menge pro Lauf/Backfill.
    style_store_sync_interval_seconds: int = 86400
    style_store_sync_top: int = 300

    # Pipedrive CRM
    pipedrive_api_token: str = ""
    pipedrive_domain: str = "innosmith"

    # Toggl Track
    toggl_api_token: str = ""
    toggl_workspace_id: int = 0

    # Bexio Buchhaltung
    bexio_api_token: str = ""

    # LLM Provider API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    perplexity_api_key: str = ""

    # MeisterLabs (MeisterTask API)
    meisterlabs_token: str = ""

    # Web Search
    tavily_api_key: str = ""

    # SIGNA Strategic Intelligence (read-only DB)
    isi_host: str = ""
    isi_db: str = ""
    isi_user: str = ""
    isi_secret: str = ""
    isi_port: int = 5432
    # Schreibzugang nur für Embedding-Backfill (Rolle signa_embedder: SELECT + UPDATE(embedding))
    isi_write_user: str = ""
    isi_write_secret: str = ""

    # InvoiceInsight (Kreditoren-Analyse)
    invoiceinsight_api_key: str = ""
    invoiceinsight_url: str = "http://127.0.0.1:8055/mcp"

    # Triage
    triage_interval_seconds: int = 120
    chat_triage_interval_seconds: int = 300

    # Integrations-Steuerung: Nur aktive Umgebung pollt E-Mails/Chats
    integrations_active: bool = True

    # ClamAV (Virenscanner fuer Uploads)
    clamav_host: str = "localhost"
    clamav_port: int = 3310

    # Sandbox-Executor (Code-Sandbox via Sidecar)
    # Der einzige Dienst mit docker.sock-Zugriff. Backend + Hermes rufen ihn per
    # token-geschuetzter HTTP-API. Dev (bare-metal Backend): 127.0.0.1:8090;
    # Int/Prod: http://taskpilot-sandbox-executor:8090 (internes Netz).
    sandbox_executor_url: str = "http://127.0.0.1:8090"
    sandbox_executor_token: str = ""

    # Document Export & Content-Services (contentConverter)
    contentconverter_path: str = "/home/innosmith/dev/github/contentConverter"
    contentconverter_cconv_bin: str = "/home/innosmith/dev/github/TaskPilot/.venv/bin/cconv"
    pptx_template_dir: str = "/home/innosmith/dev/github/contentConverter/templates"
    mapping_keys_ttl_seconds: int = 7200  # 2h
    # Zusätzliche PATH-Verzeichnisse (colon-separiert), die dem contentConverter-
    # Subprozess vorangestellt werden, damit das Mermaid-CLI (mmdc) und node
    # gefunden werden. Leer = Dev-Defaults werden automatisch ermittelt
    # (~/.local/bin + neuestes ~/.nvm/versions/node/*/bin).
    contentconverter_extra_path: str = ""

    model_config = {
        "env_file": str(_PROJECT_ROOT / ".env.dev"),
        "env_prefix": "TP_",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
