-- TaskPilot PostgreSQL Schema v0.12
-- Basierend auf Pflichtenheft Sektion 7.5.2

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Projekte
CREATE TABLE projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT '#3B82F6',
    description     TEXT,
    background_url  TEXT,
    icon_url        TEXT,
    icon_emoji      TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'paused')),
    priority        INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Board-Spalten pro Projekt (Custom-Kanban)
CREATE TABLE board_columns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    color           TEXT,
    icon_emoji      TEXT,
    position        FLOAT NOT NULL,
    is_archive      BOOLEAN DEFAULT false
);

-- Pipeline-Spalten (Agenda — globale Zeithorizonte)
CREATE TABLE pipeline_columns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    color           TEXT,
    icon_emoji      TEXT,
    position        FLOAT NOT NULL,
    column_type     TEXT NOT NULL DEFAULT 'horizon' CHECK (column_type IN ('active', 'planned', 'parked', 'horizon'))
);

-- Users
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    avatar_url      TEXT,
    role            TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member', 'viewer')),
    is_active       BOOLEAN DEFAULT true,
    settings        JSONB DEFAULT '{}'::jsonb,
    mfa_secret      TEXT,
    mfa_enabled     BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_login_at   TIMESTAMPTZ,
    invited_by      UUID REFERENCES users(id),
    must_change_password BOOLEAN DEFAULT false
);

-- Tasks
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    description     TEXT,
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    board_column_id UUID NOT NULL REFERENCES board_columns(id),
    board_position  FLOAT NOT NULL,
    pipeline_column_id UUID REFERENCES pipeline_columns(id),
    pipeline_position  FLOAT,
    assignee        TEXT NOT NULL DEFAULT 'me',
    due_date        DATE,
    data_class      TEXT NOT NULL DEFAULT 'internal' CHECK (data_class IN ('public', 'internal', 'confidential', 'highly_confidential')),
    llm_override    TEXT,
    autonomy_level  TEXT NOT NULL DEFAULT 'L1' CHECK (autonomy_level IN ('L0', 'L1', 'L2', 'L3')),
    is_completed    BOOLEAN DEFAULT false,
    is_pinned       BOOLEAN DEFAULT false,
    recurrence_rule TEXT,
    recurrence_end_date DATE,
    recurrence_max_instances INT,
    template_id     UUID REFERENCES tasks(id),
    email_message_id TEXT,
    email_conversation_id TEXT,
    calendar_event_id TEXT,
    calendar_duration_minutes INT,
    calendar_preferred_time TEXT,
    meeting_transcript_id UUID,  -- FK auf meeting_transcripts, weiter unten gesetzt (Definitionsreihenfolge)
    needs_review    BOOLEAN DEFAULT false,
    pipedrive_deal_id   INT,
    pipedrive_person_id INT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Checklisten-Eintraege
CREATE TABLE checklist_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    is_checked      BOOLEAN DEFAULT false,
    position        FLOAT NOT NULL
);

-- Tags
CREATE TABLE tags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL DEFAULT '#6B7280'
);

CREATE TABLE task_tags (
    task_id         UUID REFERENCES tasks(id) ON DELETE CASCADE,
    tag_id          UUID REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);

-- Anhaenge
CREATE TABLE attachments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    filepath        TEXT NOT NULL,
    mime_type       TEXT,
    uploaded_at     TIMESTAMPTZ DEFAULT now()
);

-- Agent-Jobs
CREATE TABLE agent_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    task_id         UUID REFERENCES tasks(id) ON DELETE CASCADE,
    job_type        TEXT,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed')),
    llm_model       TEXT,
    tokens_used     INT,
    cost_usd        NUMERIC(10,4),
    output          TEXT,
    error_message   TEXT,
    metadata        JSONB DEFAULT '{}'::jsonb,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Aktivitaets-Log
CREATE TABLE activity_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Audit-Log (sicherheitsrelevante Aktionen, unabhaengig von Task-Activity)
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    action          TEXT NOT NULL,
    resource        TEXT NOT NULL,
    resource_id     TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    details         JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_audit_log_user ON audit_log(user_id);
CREATE INDEX idx_audit_log_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_log_action ON audit_log(action);

-- Board-Members (Gast-Zugriff)
CREATE TABLE board_members (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'viewer')),
    invited_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project_id, user_id)
);

-- E-Mail-Triage
CREATE TABLE email_triage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    message_id      TEXT NOT NULL,
    subject         TEXT,
    from_address    TEXT,
    from_name       TEXT,
    received_at     TIMESTAMPTZ,
    inference_class TEXT,
    triage_class    TEXT CHECK (triage_class IN ('auto_reply', 'task', 'fyi')),
    reply_expected  BOOLEAN DEFAULT false,
    confidence      REAL,
    suggested_action JSONB,
    agent_job_id    UUID REFERENCES agent_jobs(id),
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'acted', 'dismissed')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_email_triage_user_message UNIQUE (user_id, message_id)
);

-- Absender-Profile (Beziehungsgedaechtnis)
CREATE TABLE sender_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    display_name    TEXT,
    organization    TEXT,
    relationship    TEXT CHECK (relationship IN ('kunde', 'partner', 'lieferant', 'intern', 'hochschule', 'behoerde', 'unbekannt')),
    tone            TEXT CHECK (tone IN ('formell', 'informell', 'neutral')),
    language        TEXT DEFAULT 'de' CHECK (language IN ('de', 'en', 'fr', 'it')),
    notes           TEXT,
    email_count     INT DEFAULT 0,
    last_contact_at TIMESTAMPTZ,
    -- Tone-of-Voice-Lernen (Migration 005)
    learned_tone    JSONB DEFAULT '{}'::jsonb,
    style_notes     TEXT,
    correction_count INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_sender_profiles_user_email UNIQUE (user_id, email)
);

-- Indizes
CREATE INDEX idx_tasks_project ON tasks(project_id);
CREATE INDEX idx_tasks_board_column ON tasks(board_column_id);
CREATE INDEX idx_tasks_pipeline_column ON tasks(pipeline_column_id);
CREATE INDEX idx_tasks_assignee ON tasks(assignee);
CREATE INDEX idx_tasks_is_completed ON tasks(is_completed);
CREATE INDEX idx_checklist_items_task ON checklist_items(task_id);
CREATE INDEX idx_activity_log_task ON activity_log(task_id);
CREATE INDEX idx_activity_log_created ON activity_log(created_at);
CREATE INDEX idx_agent_jobs_task ON agent_jobs(task_id);
CREATE INDEX idx_agent_jobs_status ON agent_jobs(status);
CREATE INDEX idx_board_columns_project ON board_columns(project_id);
CREATE INDEX idx_board_members_project ON board_members(project_id);
CREATE INDEX idx_board_members_user ON board_members(user_id);
CREATE INDEX idx_email_triage_status ON email_triage(status);
CREATE INDEX idx_email_triage_message_id ON email_triage(message_id);
CREATE INDEX idx_tasks_email_message ON tasks(email_message_id);
CREATE INDEX idx_sender_profiles_email ON sender_profiles(email);

-- NOTIFY-Trigger fuer Real-Time-Updates
CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(TG_ARGV[0], json_build_object(
        'op', TG_OP, 'id', COALESCE(NEW.id, OLD.id)
    )::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tasks_notify AFTER INSERT OR UPDATE OR DELETE ON tasks
    FOR EACH ROW EXECUTE FUNCTION notify_change('tasks_changed');

CREATE TRIGGER agent_jobs_notify AFTER INSERT OR UPDATE ON agent_jobs
    FOR EACH ROW EXECUTE FUNCTION notify_change('agent_jobs_changed');

CREATE TRIGGER email_triage_notify AFTER INSERT OR UPDATE ON email_triage
    FOR EACH ROW EXECUTE FUNCTION notify_change('email_triage_changed');

-- updated_at Trigger
CREATE OR REPLACE FUNCTION update_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER projects_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER sender_profiles_updated_at BEFORE UPDATE ON sender_profiles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- LLM-Konversationen
CREATE TABLE llm_conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT,
    task_id         UUID REFERENCES tasks(id) ON DELETE SET NULL,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    model           TEXT NOT NULL,
    mode            TEXT DEFAULT 'agent' CHECK (mode IN ('agent', 'deep_research')),
    temperature     REAL DEFAULT 0.7,
    grounding       JSONB DEFAULT '{}'::jsonb,
    total_tokens    INT DEFAULT 0,
    total_cost_usd  NUMERIC(10,4) DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE llm_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES llm_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    model           TEXT,
    tokens          INT,
    cost_usd        NUMERIC(10,6),
    attachments     JSONB DEFAULT '[]'::jsonb,
    citations       JSONB DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Angepinnte Kontext-Dokumente pro Konversation (Re-Injektion bei jedem Turn)
CREATE TABLE conversation_context_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES llm_conversations(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL CHECK (source_type IN ('local_upload', 'onedrive_file', 'onedrive_folder')),
    source_ref      TEXT,
    name            TEXT NOT NULL,
    content         TEXT NOT NULL,
    char_count      INT DEFAULT 0,
    pinned          BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_conversation_context_items_conv ON conversation_context_items(conversation_id);

-- Web-Suchen (Tavily etc.)
CREATE TABLE web_searches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query           TEXT NOT NULL,
    provider        TEXT NOT NULL DEFAULT 'tavily',
    results         JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_count    INT DEFAULT 0,
    triggered_by    TEXT NOT NULL DEFAULT 'user' CHECK (triggered_by IN ('user', 'agent', 'system')),
    task_id         UUID REFERENCES tasks(id) ON DELETE SET NULL,
    conversation_id UUID REFERENCES llm_conversations(id) ON DELETE SET NULL,
    user_id         UUID REFERENCES users(id),
    credits_used    INT DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- LLM-Finanzanalysen (Treuhand-, Finanz-, Kosten-Analysen)
CREATE TABLE finance_analyses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_type   TEXT NOT NULL,
    title           TEXT NOT NULL,
    model           TEXT NOT NULL,
    anonymized      BOOLEAN DEFAULT false,
    status          TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'failed')),
    prompt          TEXT,
    report          TEXT,
    thinking        TEXT,
    snapshot_meta   JSONB DEFAULT '{}'::jsonb,
    tokens          INT,
    cost_usd        NUMERIC(10,6),
    error_message   TEXT,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_finance_analyses_created ON finance_analyses(created_at DESC);
CREATE INDEX idx_finance_analyses_type ON finance_analyses(analysis_type);

-- Indizes
CREATE INDEX idx_llm_conversations_task ON llm_conversations(task_id);
CREATE INDEX idx_llm_conversations_user ON llm_conversations(user_id);
CREATE INDEX idx_llm_conversations_created ON llm_conversations(created_at DESC);
CREATE INDEX idx_llm_messages_conversation ON llm_messages(conversation_id);
CREATE INDEX idx_llm_messages_created ON llm_messages(created_at);
CREATE INDEX idx_web_searches_task ON web_searches(task_id);
CREATE INDEX idx_web_searches_query ON web_searches USING gin(to_tsvector('german', query));
CREATE INDEX idx_web_searches_created ON web_searches(created_at DESC);
CREATE INDEX idx_web_searches_user ON web_searches(user_id);

-- Trigger
CREATE TRIGGER llm_conversations_updated_at BEFORE UPDATE ON llm_conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Teams-Chat-Triage
CREATE TABLE chat_triage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    chat_id         TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    from_name       TEXT,
    from_id         TEXT,
    body_preview    TEXT,
    chat_type       TEXT,
    received_at     TIMESTAMPTZ,
    triage_class    TEXT CHECK (triage_class IN ('task', 'fyi', 'auto_reply', 'meeting_summary')),
    confidence      REAL,
    suggested_action JSONB,
    agent_job_id    UUID REFERENCES agent_jobs(id),
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'acted', 'dismissed')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_chat_triage_user_message UNIQUE (user_id, message_id)
);

CREATE INDEX idx_chat_triage_status ON chat_triage(status);
CREATE INDEX idx_chat_triage_message_id ON chat_triage(message_id);
CREATE INDEX idx_chat_triage_chat_id ON chat_triage(chat_id);

CREATE TRIGGER chat_triage_notify AFTER INSERT OR UPDATE ON chat_triage
    FOR EACH ROW EXECUTE FUNCTION notify_change('chat_triage_changed');

-- Follow-up-Erkennung: Dedupe-Anker pro Sent-Konversation
CREATE TABLE followup_suggestions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    task_id         UUID REFERENCES tasks(id) ON DELETE SET NULL,
    subject         TEXT,
    recipient       TEXT,
    sent_at         TIMESTAMPTZ,
    status          TEXT DEFAULT 'suggested'
        CHECK (status IN ('suggested', 'answered', 'skipped')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_followup_suggestions_user_conversation UNIQUE (user_id, conversation_id)
);

CREATE INDEX idx_followup_suggestions_status ON followup_suggestions(status);

-- Teams-Meeting-Transkripte (Original + Protokoll + Anonymisierung)
CREATE TABLE meeting_transcripts (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID REFERENCES users(id) ON DELETE CASCADE,
    meeting_id              TEXT NOT NULL,
    transcript_id           TEXT NOT NULL,
    subject                 TEXT,
    organizer               TEXT,
    started_at              TIMESTAMPTZ,
    ended_at                TIMESTAMPTZ,
    raw_vtt                 TEXT,
    transcript_text         TEXT,
    protocol_md             TEXT,
    anonymized_text         TEXT,
    anonymized_protocol_md  TEXT,
    anonymization_map       JSONB,
    status                  TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    agent_job_id            UUID REFERENCES agent_jobs(id),
    error_message           TEXT,
    created_at              TIMESTAMPTZ DEFAULT now(),
    updated_at              TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_meeting_transcripts_user_transcript UNIQUE (user_id, transcript_id)
);

CREATE INDEX idx_meeting_transcripts_status ON meeting_transcripts(status);
CREATE INDEX idx_meeting_transcripts_started ON meeting_transcripts(started_at DESC);

-- FK tasks.meeting_transcript_id (hier gesetzt, da meeting_transcripts erst hier existiert)
ALTER TABLE tasks
    ADD CONSTRAINT tasks_meeting_transcript_id_fkey
    FOREIGN KEY (meeting_transcript_id) REFERENCES meeting_transcripts(id) ON DELETE SET NULL;
CREATE INDEX idx_tasks_meeting_transcript ON tasks(meeting_transcript_id);

-- Notifications
CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT,
    link            TEXT,
    source_type     TEXT,
    source_id       UUID,
    is_read         BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_notifications_user ON notifications(user_id);
CREATE INDEX idx_notifications_user_unread ON notifications(user_id, is_read) WHERE NOT is_read;
CREATE INDEX idx_notifications_created ON notifications(created_at DESC);

CREATE TRIGGER notifications_notify AFTER INSERT OR UPDATE ON notifications
    FOR EACH ROW EXECUTE FUNCTION notify_change('notifications_changed');

-- Mind-Maps
CREATE TABLE mindmap_folders (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    parent_id   UUID REFERENCES mindmap_folders(id) ON DELETE CASCADE,
    owner_id    UUID NOT NULL REFERENCES users(id),
    color       TEXT,
    icon_emoji  TEXT,
    position    FLOAT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE mindmaps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    folder_id       UUID REFERENCES mindmap_folders(id) ON DELETE SET NULL,
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    owner_id        UUID NOT NULL REFERENCES users(id),
    visibility      TEXT NOT NULL DEFAULT 'private'
                    CHECK (visibility IN ('private', 'project', 'shared')),
    flow_data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    background_url  TEXT,
    background_color TEXT,
    thumbnail_url   TEXT,
    is_template     BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE mindmap_shares (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mindmap_id  UUID NOT NULL REFERENCES mindmaps(id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(24), 'hex'),
    password_hash TEXT NOT NULL,
    permission  TEXT NOT NULL DEFAULT 'view' CHECK (permission IN ('view', 'edit')),
    label       TEXT,
    expires_at  TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_mindmap_folders_owner ON mindmap_folders(owner_id);
CREATE INDEX idx_mindmap_folders_parent ON mindmap_folders(parent_id);
CREATE INDEX idx_mindmaps_owner ON mindmaps(owner_id);
CREATE INDEX idx_mindmaps_folder ON mindmaps(folder_id);
CREATE INDEX idx_mindmaps_project ON mindmaps(project_id);
CREATE INDEX idx_mindmaps_visibility ON mindmaps(visibility);
CREATE INDEX idx_mindmap_shares_mindmap ON mindmap_shares(mindmap_id);
CREATE INDEX idx_mindmap_shares_token ON mindmap_shares(token);

CREATE TRIGGER mindmap_folders_updated_at BEFORE UPDATE ON mindmap_folders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER mindmaps_updated_at BEFORE UPDATE ON mindmaps
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER mindmaps_notify AFTER INSERT OR UPDATE OR DELETE ON mindmaps
    FOR EACH ROW EXECUTE FUNCTION notify_change('mindmaps_changed');

-- Kapazitätsplanung: Projekte (eigene Entität, entkoppelt von projects)
CREATE TABLE capacity_projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT '#3B82F6',
    icon_url        TEXT,
    icon_emoji      TEXT,
    client_name     TEXT,
    hourly_rate     NUMERIC(10,2),
    is_billable     BOOLEAN DEFAULT true,
    status          TEXT NOT NULL DEFAULT 'bestätigt'
                    CHECK (status IN ('bestätigt', 'vorläufig')),
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    toggl_project_id INT,
    toggl_client_id  INT,
    toggl_billable_filter TEXT CHECK (toggl_billable_filter IN ('non_billable', 'billable')),
    pipedrive_deal_id INT,
    sort_order      INT DEFAULT 0,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_cap_projects_status ON capacity_projects(status);
CREATE INDEX idx_cap_projects_project ON capacity_projects(project_id);

-- Kapazitätsplanung: Zuweisungen pro Projekt und Woche/Tag
CREATE TABLE capacity_allocations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    capacity_project_id UUID NOT NULL REFERENCES capacity_projects(id) ON DELETE CASCADE,
    week_start          DATE NOT NULL,
    minutes             INT NOT NULL DEFAULT 0,
    allocation_type     TEXT NOT NULL DEFAULT 'week'
                        CHECK (allocation_type IN ('week', 'day')),
    series_id           UUID,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE(capacity_project_id, week_start)
);

CREATE INDEX idx_cap_alloc_week ON capacity_allocations(week_start);
CREATE INDEX idx_cap_alloc_series ON capacity_allocations(series_id);
CREATE INDEX idx_cap_alloc_project ON capacity_allocations(capacity_project_id);

-- Kapazitätsplanung: Ferien und freie Tage
CREATE TABLE capacity_time_off (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date            DATE NOT NULL UNIQUE,
    type            TEXT DEFAULT 'ferien'
                    CHECK (type IN ('ferien', 'feiertag', 'krank', 'sonstiges')),
    label           TEXT,
    hours           REAL DEFAULT 8.0,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_cap_timeoff_date ON capacity_time_off(date);

CREATE TRIGGER capacity_projects_updated_at BEFORE UPDATE ON capacity_projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER capacity_allocations_updated_at BEFORE UPDATE ON capacity_allocations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─────────────────────────────────────────────────────────
-- Agent-Lern-Schicht (Memory & Self-Learning, Migration 005)
-- ─────────────────────────────────────────────────────────

-- Erfasste Korrektursignale (Draft-Edits, Reklassifikation, Daumen, Chat-Teach)
CREATE TABLE agent_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    agent_job_id    UUID REFERENCES agent_jobs(id) ON DELETE SET NULL,
    sender_email    TEXT,
    source          TEXT NOT NULL DEFAULT 'cockpit'
                    CHECK (source IN ('cockpit', 'outlook', 'chat', 'system')),
    feedback_type   TEXT NOT NULL CHECK (feedback_type IN (
        'draft_edit', 'approved_clean', 'rejected',
        'triage_reclass', 'task_deleted', 'task_moved',
        'thumbs_up', 'thumbs_down', 'chat_teach'
    )),
    original        JSONB DEFAULT '{}'::jsonb,
    corrected       JSONB DEFAULT '{}'::jsonb,
    diff_text       TEXT,
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_agent_feedback_job ON agent_feedback(agent_job_id);
CREATE INDEX idx_agent_feedback_type ON agent_feedback(feedback_type);
CREATE INDEX idx_agent_feedback_sender ON agent_feedback(sender_email);
CREATE INDEX idx_agent_feedback_created ON agent_feedback(created_at DESC);

-- Episodisches Gedaechtnis mit lokalem Embedding (pgvector-Recall)
CREATE TABLE agent_episodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    agent_job_id    UUID REFERENCES agent_jobs(id) ON DELETE SET NULL,
    job_type        TEXT,
    sender_email    TEXT,
    summary         TEXT NOT NULL,
    decision        JSONB DEFAULT '{}'::jsonb,
    was_corrected   BOOLEAN DEFAULT false,
    lesson          TEXT,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_agent_episodes_job ON agent_episodes(agent_job_id);
CREATE INDEX idx_agent_episodes_type ON agent_episodes(job_type);
CREATE INDEX idx_agent_episodes_sender ON agent_episodes(sender_email);
CREATE INDEX idx_agent_episodes_corrected ON agent_episodes(was_corrected);
CREATE INDEX idx_agent_episodes_embedding
    ON agent_episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Gelernte/gepflegte Regeln (Agent schlaegt vor oder manuell gepflegt)
--   rule_type='llm'           -> Freitext-Leitregel, je nach scope in Prompt injiziert
--   rule_type='deterministic' -> match_conditions -> action, im Code vor dem LLM
CREATE TABLE learned_rules (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID REFERENCES users(id) ON DELETE CASCADE,
    scope            TEXT NOT NULL DEFAULT 'triage'
                     CHECK (scope IN ('triage', 'draft', 'task', 'calendar', 'general', 'chat')),
    rule_text        TEXT NOT NULL,
    evidence         JSONB DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'active', 'rejected', 'archived')),
    autonomy_hint    TEXT,
    rule_type        TEXT NOT NULL DEFAULT 'llm'
                     CHECK (rule_type IN ('llm', 'deterministic')),
    match_conditions JSONB DEFAULT '{}'::jsonb,
    action           JSONB DEFAULT '{}'::jsonb,
    priority         INT DEFAULT 100,
    applied_count    INT DEFAULT 0,
    created_at       TIMESTAMPTZ DEFAULT now(),
    approved_at      TIMESTAMPTZ
);

CREATE INDEX idx_learned_rules_status ON learned_rules(status);
CREATE INDEX idx_learned_rules_scope ON learned_rules(scope);
CREATE INDEX idx_learned_rules_type ON learned_rules(rule_type);

-- Style-Store: gesendete Antworten als Few-Shot-Stil-Anker fuer Entwuerfe (pgvector)
CREATE TABLE sent_mail_examples (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    graph_id    TEXT NOT NULL,
    recipient   TEXT,
    subject     TEXT,
    body_text   TEXT NOT NULL,
    sent_at     TIMESTAMPTZ,
    language    TEXT,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_sent_mail_examples_user_graph UNIQUE (user_id, graph_id)
);

CREATE INDEX idx_sent_mail_examples_recipient ON sent_mail_examples(recipient);
CREATE INDEX idx_sent_mail_examples_sent_at ON sent_mail_examples(sent_at DESC);
CREATE INDEX idx_sent_mail_examples_embedding
    ON sent_mail_examples USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TRIGGER agent_feedback_notify AFTER INSERT OR UPDATE ON agent_feedback
    FOR EACH ROW EXECUTE FUNCTION notify_change('agent_feedback_changed');

CREATE TRIGGER learned_rules_notify AFTER INSERT OR UPDATE ON learned_rules
    FOR EACH ROW EXECUTE FUNCTION notify_change('learned_rules_changed');

-- Semantischer Such-Index ueber E-Mails + Dokumente (user-facing, pgvector).
-- Eigenes, staerkeres Embedding-Modell (Qwen3-Embedding-4B, native 2560d) als der
-- Agent-Index (0.6B/1024). Speichertyp halfvec(2560): fp16 -- bei 2560d ist
-- float32-HNSW technisch nicht moeglich (pgvector-Cap 2000d; halfvec hebt auf
-- 4000d), Recall-Verlust nachweislich < 0.5 %. Ein Chunk = eine durchsuchbare
-- Passage; ``content_tsv`` (generiert) traegt den lexikalen Hybrid-Anteil.
CREATE TABLE semantic_documents (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID REFERENCES users(id) ON DELETE CASCADE,
    source_type        TEXT NOT NULL
                       CHECK (source_type IN ('email', 'onedrive', 'upload', 'transcript')),
    source_id          TEXT NOT NULL,
    chunk_index        INT NOT NULL DEFAULT 0,
    title              TEXT,
    content_text       TEXT NOT NULL,
    url                TEXT,
    mime               TEXT,
    metadata           JSONB DEFAULT '{}'::jsonb,
    embedding          halfvec(2560),
    content_tsv        tsvector GENERATED ALWAYS AS (
                           to_tsvector('german',
                               coalesce(title, '') || ' ' || coalesce(content_text, ''))
                       ) STORED,
    source_modified_at TIMESTAMPTZ,
    indexed_at         TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_semantic_documents_user_source_chunk UNIQUE (user_id, source_type, source_id, chunk_index)
);

CREATE INDEX idx_semantic_documents_source ON semantic_documents(source_type, source_id);
CREATE INDEX idx_semantic_documents_modified ON semantic_documents(source_modified_at DESC);
CREATE INDEX idx_semantic_documents_embedding
    ON semantic_documents USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX idx_semantic_documents_tsv
    ON semantic_documents USING gin (content_tsv);

-- User-Scope-Indizes (Mehrbenutzer-Fundament): jede persoenliche Tabelle traegt
-- eine user_id und wird nach Principal gefiltert.
CREATE INDEX idx_agent_jobs_user ON agent_jobs(user_id);
CREATE INDEX idx_email_triage_user ON email_triage(user_id);
CREATE INDEX idx_chat_triage_user ON chat_triage(user_id);
CREATE INDEX idx_sender_profiles_user ON sender_profiles(user_id);
CREATE INDEX idx_learned_rules_user ON learned_rules(user_id);
CREATE INDEX idx_sent_mail_examples_user ON sent_mail_examples(user_id);
CREATE INDEX idx_agent_feedback_user ON agent_feedback(user_id);
CREATE INDEX idx_agent_episodes_user ON agent_episodes(user_id);
CREATE INDEX idx_followup_suggestions_user ON followup_suggestions(user_id);
CREATE INDEX idx_meeting_transcripts_user ON meeting_transcripts(user_id);
CREATE INDEX idx_semantic_documents_user ON semantic_documents(user_id);

-- Laufstatus des semantischen Indexer-Daemons (Singleton, id=1). Genau EIN Writer
-- (der Daemon), das Backend liest nur -> Transparenz im Suchindex-Tab ohne Kopplung.
CREATE TABLE index_status (
    id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    state            TEXT NOT NULL DEFAULT 'idle'
                     CHECK (state IN ('idle', 'running')),
    phase            TEXT,
    detail           TEXT,
    run_started_at   TIMESTAMPTZ,
    run_finished_at  TIMESTAMPTZ,
    heartbeat_at     TIMESTAMPTZ,
    folders_total    INT NOT NULL DEFAULT 0,
    folders_done     INT NOT NULL DEFAULT 0,
    docs_total       INT NOT NULL DEFAULT 0,
    docs_done        INT NOT NULL DEFAULT 0,
    last_emails      INT NOT NULL DEFAULT 0,
    last_documents   INT NOT NULL DEFAULT 0,
    last_chunks      INT NOT NULL DEFAULT 0,
    last_error       TEXT,
    last_error_at    TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO index_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
