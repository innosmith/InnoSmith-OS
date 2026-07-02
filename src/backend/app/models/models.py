import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(Text, nullable=False, server_default="#3B82F6")
    description: Mapped[str | None] = mapped_column(Text)
    background_url: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    icon_emoji: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    board_columns: Mapped[list["BoardColumn"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class BoardColumn(Base):
    __tablename__ = "board_columns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text)
    icon_emoji: Mapped[str | None] = mapped_column(Text)
    position: Mapped[float] = mapped_column(Float, nullable=False)
    is_archive: Mapped[bool] = mapped_column(Boolean, server_default="false")

    project: Mapped["Project"] = relationship(back_populates="board_columns")
    tasks: Mapped[list["Task"]] = relationship(back_populates="board_column")


class PipelineColumn(Base):
    __tablename__ = "pipeline_columns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text)
    icon_emoji: Mapped[str | None] = mapped_column(Text)
    position: Mapped[float] = mapped_column(Float, nullable=False)
    column_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="horizon")

    tasks: Mapped[list["Task"]] = relationship(back_populates="pipeline_column")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    mfa_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default="false")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    board_column_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("board_columns.id"), nullable=False)
    board_position: Mapped[float] = mapped_column(Float, nullable=False)
    pipeline_column_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipeline_columns.id"))
    pipeline_position: Mapped[float | None] = mapped_column(Float)
    assignee: Mapped[str] = mapped_column(Text, nullable=False, server_default="me")
    due_date: Mapped[datetime | None] = mapped_column(Date)
    data_class: Mapped[str] = mapped_column(Text, nullable=False, server_default="internal")
    llm_override: Mapped[str | None] = mapped_column(Text)
    autonomy_level: Mapped[str] = mapped_column(Text, nullable=False, server_default="L1")
    is_completed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_pinned: Mapped[bool] = mapped_column(Boolean, server_default="false")
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    recurrence_end_date: Mapped[date | None] = mapped_column(Date)
    recurrence_max_instances: Mapped[int | None] = mapped_column(Integer)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"))
    email_message_id: Mapped[str | None] = mapped_column(Text)
    email_conversation_id: Mapped[str | None] = mapped_column(Text)
    calendar_event_id: Mapped[str | None] = mapped_column(Text)
    calendar_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    calendar_preferred_time: Mapped[str | None] = mapped_column(Text)
    meeting_transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meeting_transcripts.id", ondelete="SET NULL")
    )
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default="false")
    pipedrive_deal_id: Mapped[int | None] = mapped_column(Integer)
    pipedrive_person_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="tasks")
    board_column: Mapped["BoardColumn"] = relationship(back_populates="tasks")
    pipeline_column: Mapped["PipelineColumn | None"] = relationship(back_populates="tasks")
    checklist_items: Mapped[list["ChecklistItem"]] = relationship(back_populates="task", cascade="all, delete-orphan", order_by="ChecklistItem.position")
    tags: Mapped[list["Tag"]] = relationship(secondary="task_tags", back_populates="tasks")
    agent_jobs: Mapped[list["AgentJob"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    activity_logs: Mapped[list["ActivityLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    attachments: Mapped[list["Attachment"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class ChecklistItem(Base):
    __tablename__ = "checklist_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_checked: Mapped[bool] = mapped_column(Boolean, server_default="false")
    position: Mapped[float] = mapped_column(Float, nullable=False)

    task: Mapped["Task"] = relationship(back_populates="checklist_items")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    color: Mapped[str] = mapped_column(Text, nullable=False, server_default="#6B7280")

    tasks: Mapped[list["Task"]] = relationship(secondary="task_tags", back_populates="tags")


class TaskTag(Base):
    __tablename__ = "task_tags"

    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    filepath: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="attachments")


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    job_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    llm_model: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    output: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    task: Mapped["Task | None"] = relationship(back_populates="agent_jobs")


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="activity_logs")


class BoardMember(Base):
    __tablename__ = "board_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="member")
    invited_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class EmailTriage(Base):
    __tablename__ = "email_triage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    message_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    from_address: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    inference_class: Mapped[str | None] = mapped_column(Text)
    triage_class: Mapped[str | None] = mapped_column(Text)
    reply_expected: Mapped[bool] = mapped_column(Boolean, server_default="false")
    confidence: Mapped[float | None] = mapped_column(Float)
    suggested_action: Mapped[dict | None] = mapped_column(JSONB)
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_jobs.id"))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class ChatTriage(Base):
    __tablename__ = "chat_triage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    chat_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    from_name: Mapped[str | None] = mapped_column(Text)
    from_id: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str | None] = mapped_column(Text)
    chat_type: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    triage_class: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    suggested_action: Mapped[dict | None] = mapped_column(JSONB)
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_jobs.id"))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class MeetingTranscript(Base):
    """Teams-Meeting-Transkript: Original (VTT), geparster Text, Protokoll.

    Anonymisierte Fassungen + Mapping bleiben ausschliesslich lokal
    (``anonymization_map`` wird nie exportiert).
    """

    __tablename__ = "meeting_transcripts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    meeting_id: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    organizer: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    raw_vtt: Mapped[str | None] = mapped_column(Text)
    transcript_text: Mapped[str | None] = mapped_column(Text)
    protocol_md: Mapped[str | None] = mapped_column(Text)
    anonymized_text: Mapped[str | None] = mapped_column(Text)
    anonymized_protocol_md: Mapped[str | None] = mapped_column(Text)
    anonymization_map: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_jobs.id"))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class FollowupSuggestion(Base):
    """Follow-up-Erkennung: eine Zeile pro geprüfter Sent-Konversation.

    Dient als Dedupe-Anker (auch nach Verwerfen des Task-Vorschlags) und als
    Datenquelle für die Inbox-Statistik. ``status``: suggested | answered |
    skipped (LLM-Gate hat bewusst kein Nachfassen empfohlen).
    """

    __tablename__ = "followup_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    subject: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="suggested")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class SenderProfile(Base):
    __tablename__ = "sender_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    organization: Mapped[str | None] = mapped_column(Text)
    relationship: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, server_default="de")
    notes: Mapped[str | None] = mapped_column(Text)
    email_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_contact_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    learned_tone: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    style_notes: Mapped[str | None] = mapped_column(Text)
    correction_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class AgentFeedback(Base):
    """Erfasste Korrektursignale fuer das Self-Learning.

    Quelle der Wahrheit fuer Lern-KPIs (Korrekturrate, Approval-ohne-Edit) und
    Ausgangspunkt fuer episodische Lektionen + gelernte Regeln.
    """

    __tablename__ = "agent_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_jobs.id", ondelete="SET NULL"))
    sender_email: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="cockpit")
    feedback_type: Mapped[str] = mapped_column(Text, nullable=False)
    original: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    corrected: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    diff_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class AgentEpisode(Base):
    """Episodisches Gedaechtnis: ein abgeschlossener Entscheid + sein Ausgang.

    Die ``embedding``-Spalte (pgvector) wird hier bewusst NICHT gemappt, um die
    pgvector-Python-Abhaengigkeit aus dem ORM herauszuhalten -- Schreiben/Recall
    laufen ueber rohes SQL im Embeddings-Service.
    """

    __tablename__ = "agent_episodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    agent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_jobs.id", ondelete="SET NULL"))
    job_type: Mapped[str | None] = mapped_column(Text)
    sender_email: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    was_corrected: Mapped[bool] = mapped_column(Boolean, server_default="false")
    lesson: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class SentMailExample(Base):
    """Style-Store: eine gesendete Antwort als Few-Shot-Stil-Anker fuer Entwuerfe.

    Wie ``AgentEpisode`` wird die ``embedding``-Spalte (pgvector) bewusst NICHT
    gemappt -- Schreiben/Retrieval laufen ueber rohes SQL im Style-Store-Service.
    """

    __tablename__ = "sent_mail_examples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    graph_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    recipient: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    language: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class LearnedRule(Base):
    """Vom Agenten vorgeschlagene oder manuell gepflegte Regel.

    Zwei Typen:
      - ``rule_type='llm'``: Freitext-Leitregel (``rule_text``), die je nach
        ``scope`` (Skill/Kontext) in den passenden Prompt injiziert wird.
      - ``rule_type='deterministic'``: ``match_conditions`` -> ``action`` wird im
        Code vor dem LLM ausgeführt (z. B. Absender X -> fyi + Kategorie + Move).
    """

    __tablename__ = "learned_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="triage")
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    autonomy_hint: Mapped[str | None] = mapped_column(Text)
    # Regel-Engine: Typ unterscheidet LLM-Leitregel von deterministischer Override.
    rule_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="llm")
    # Liste von Bedingungen ``[{field, op, value}, ...]`` (AND), nur deterministisch.
    match_conditions: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    # Aktion ``{triage_class, category, folder}``, nur deterministisch.
    action: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    # Reihenfolge bei deterministischen Regeln (kleiner = zuerst geprüft).
    priority: Mapped[int] = mapped_column(Integer, server_default="100")
    # Wie oft die Regel bereits angewandt wurde (Anzeige/Vertrauen).
    applied_count: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class LlmConversation(Base):
    __tablename__ = "llm_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, server_default="chat")
    temperature: Mapped[float] = mapped_column(Float, server_default="0.7")
    grounding: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    total_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    total_cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    messages: Mapped[list["LlmMessage"]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="LlmMessage.created_at")
    task: Mapped["Task | None"] = relationship()


class LlmMessage(Base):
    __tablename__ = "llm_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("llm_conversations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    attachments: Mapped[dict] = mapped_column(JSONB, server_default="[]")
    citations: Mapped[dict] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    conversation: Mapped["LlmConversation"] = relationship(back_populates="messages")


class ConversationContextItem(Base):
    """Angepinntes Kontext-Dokument einer Konversation.

    Angehängte Dokumente (Uploads, OneDrive, E-Mail-Texte) werden beim ersten
    Senden einmalig extrahiert und hier persistiert. Bei jedem weiteren Turn
    der Konversation wird der Korpus re-injiziert — Dokumente «leben» damit in
    der ganzen Konversation statt nur im Request, mit dem sie hochkamen.
    """

    __tablename__ = "conversation_context_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("llm_conversations.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)  # local_upload | onedrive_file | onedrive_folder
    source_ref: Mapped[str | None] = mapped_column(Text)            # upload_id bzw. OneDrive item_id
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)      # extrahierter Volltext (einmalig aufgelöst)
    char_count: Mapped[int] = mapped_column(Integer, server_default="0")
    pinned: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class WebSearch(Base):
    __tablename__ = "web_searches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    query: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="tavily")
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="[]")
    result_count: Mapped[int] = mapped_column(Integer, server_default="0")
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_conversations.id", ondelete="SET NULL"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    credits_used: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class FinanceAnalysis(Base):
    """Persistierte LLM-Finanzanalyse (Treuhand-, Finanz-, Kosten-Analysen)."""

    __tablename__ = "finance_analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    analysis_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    anonymized: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="completed")
    prompt: Mapped[str | None] = mapped_column(Text)          # finaler (ggf. anonymisierter) Prompt
    report: Mapped[str | None] = mapped_column(Text)          # Markdown-Report (de-anonymisiert)
    thinking: Mapped[str | None] = mapped_column(Text)
    snapshot_meta: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    error_message: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class FinanceDocument(Base):
    """Hochgeladenes Finanzdokument (z. B. Jahresrechnung) als extrahierter Text.

    Es wird bewusst nur der extrahierte Text gespeichert (kein PDF-Binary) --
    die Anonymisierung erfolgt pro Analyse-Lauf, bevor der Text an ein Cloud-LLM
    geht. Owner-only.
    """

    __tablename__ = "finance_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    label: Mapped[str] = mapped_column(Text, nullable=False)        # z. B. "Jahresrechnung 2025"
    filename: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(Integer)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_read: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class MindmapFolder(Base):
    __tablename__ = "mindmap_folders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mindmap_folders.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    color: Mapped[str | None] = mapped_column(Text)
    icon_emoji: Mapped[str | None] = mapped_column(Text)
    position: Mapped[float] = mapped_column(Float, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    children: Mapped[list["MindmapFolder"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    parent: Mapped["MindmapFolder | None"] = relationship(back_populates="children", remote_side=[id])
    mindmaps: Mapped[list["Mindmap"]] = relationship(back_populates="folder")


class Mindmap(Base):
    __tablename__ = "mindmaps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    title: Mapped[str] = mapped_column(Text, nullable=False)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mindmap_folders.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    visibility: Mapped[str] = mapped_column(Text, nullable=False, server_default="private")
    flow_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    background_url: Mapped[str | None] = mapped_column(Text)
    background_color: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    is_template: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    folder: Mapped["MindmapFolder | None"] = relationship(back_populates="mindmaps")
    project: Mapped["Project | None"] = relationship()
    shares: Mapped[list["MindmapShare"]] = relationship(back_populates="mindmap", cascade="all, delete-orphan")


class MindmapShare(Base):
    __tablename__ = "mindmap_shares"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    mindmap_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mindmaps.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False, server_default=func.encode(func.gen_random_bytes(24), "hex"))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False, server_default="view")
    label: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    mindmap: Mapped["Mindmap"] = relationship(back_populates="shares")


class CapacityProject(Base):
    __tablename__ = "capacity_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(Text, nullable=False, server_default="#3B82F6")
    icon_url: Mapped[str | None] = mapped_column(Text)
    icon_emoji: Mapped[str | None] = mapped_column(Text)
    client_name: Mapped[str | None] = mapped_column(Text)
    hourly_rate: Mapped[float | None] = mapped_column(Numeric(10, 2))
    is_billable: Mapped[bool] = mapped_column(Boolean, server_default="true")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="bestätigt")
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    toggl_project_id: Mapped[int | None] = mapped_column(Integer)
    toggl_client_id: Mapped[int | None] = mapped_column(Integer)
    toggl_billable_filter: Mapped[str | None] = mapped_column(Text)
    pipedrive_deal_id: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    project: Mapped["Project | None"] = relationship()
    allocations: Mapped[list["CapacityAllocation"]] = relationship(back_populates="capacity_project", cascade="all, delete-orphan")


class CapacityAllocation(Base):
    __tablename__ = "capacity_allocations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    capacity_project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capacity_projects.id", ondelete="CASCADE"), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    allocation_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="week")
    series_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    capacity_project: Mapped["CapacityProject"] = relationship(back_populates="allocations")


class CapacityTimeOff(Base):
    __tablename__ = "capacity_time_off"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, server_default="ferien")
    label: Mapped[str | None] = mapped_column(Text)
    hours: Mapped[float] = mapped_column(Float, server_default="8.0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
