import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


# --- Project ---

class ProjectBase(BaseModel):
    name: str
    color: str = "#3B82F6"
    description: str | None = None
    background_url: str | None = None
    icon_url: str | None = None
    icon_emoji: str | None = None
    status: str = "active"
    priority: int = 0


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None
    background_url: str | None = None
    icon_url: str | None = None
    icon_emoji: str | None = None
    status: str | None = None
    priority: int | None = None


class BoardColumnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    color: str | None
    icon_emoji: str | None = None
    position: float
    is_archive: bool


class ProjectOut(ProjectBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ProjectWithColumns(ProjectOut):
    board_columns: list[BoardColumnOut] = []


# --- Pipeline ---

class PipelineColumnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    color: str | None = None
    icon_emoji: str | None = None
    position: float
    column_type: str


# --- Tag ---

class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    color: str


# --- Checklist ---

class ChecklistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    text: str
    is_checked: bool
    position: float


class ChecklistItemCreate(BaseModel):
    text: str
    position: float | None = None


class ChecklistItemUpdate(BaseModel):
    text: str | None = None
    is_checked: bool | None = None
    position: float | None = None


# --- Task ---

class TaskBase(BaseModel):
    title: str
    description: str | None = None
    assignee: str = "me"
    due_date: date | None = None
    data_class: str = "internal"
    llm_override: str | None = None
    autonomy_level: str = "L1"
    is_pinned: bool = False
    recurrence_rule: str | None = None
    recurrence_end_date: date | None = None
    recurrence_max_instances: int | None = None


class TaskCreate(TaskBase):
    project_id: uuid.UUID
    board_column_id: uuid.UUID
    board_position: float | None = None
    pipeline_column_id: uuid.UUID | None = None
    pipeline_position: float | None = None
    email_message_id: str | None = None
    email_conversation_id: str | None = None
    calendar_duration_minutes: int | None = None
    calendar_preferred_time: str | None = None
    pipedrive_deal_id: int | None = None
    pipedrive_person_id: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    project_id: uuid.UUID | None = None
    board_column_id: uuid.UUID | None = None
    board_position: float | None = None
    pipeline_column_id: uuid.UUID | None = None
    pipeline_position: float | None = None
    assignee: str | None = None
    due_date: date | None = None
    data_class: str | None = None
    llm_override: str | None = None
    autonomy_level: str | None = None
    is_completed: bool | None = None
    is_pinned: bool | None = None
    recurrence_rule: str | None = None
    recurrence_end_date: date | None = None
    recurrence_max_instances: int | None = None
    calendar_event_id: str | None = None
    calendar_duration_minutes: int | None = None
    calendar_preferred_time: str | None = None
    pipedrive_deal_id: int | None = None
    pipedrive_person_id: int | None = None


class AssigneeUser(BaseModel):
    id: uuid.UUID
    display_name: str
    avatar_url: str | None = None


class TaskOut(TaskBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    board_column_id: uuid.UUID
    board_position: float
    pipeline_column_id: uuid.UUID | None
    pipeline_position: float | None
    is_completed: bool
    email_message_id: str | None = None
    email_conversation_id: str | None = None
    calendar_event_id: str | None = None
    calendar_duration_minutes: int | None = None
    calendar_preferred_time: str | None = None
    meeting_transcript_id: uuid.UUID | None = None
    needs_review: bool = False
    template_id: uuid.UUID | None = None
    recurrence_end_date: date | None = None
    recurrence_max_instances: int | None = None
    pipedrive_deal_id: int | None = None
    pipedrive_person_id: int | None = None
    assignee_user: AssigneeUser | None = None
    source_email_subject: str | None = None
    source_email_from: str | None = None
    # Aus email_message_id berechnet, nicht gespeichert -- so gibt es genau einen Ort,
    # an dem die Outlook-URL entsteht (app/services/email_links.py).
    source_email_web_link: str | None = None
    meeting_subject: str | None = None
    created_at: datetime
    updated_at: datetime
    tags: list[TagOut] = []
    checklist_items: list[ChecklistItemOut] = []


class TaskCard(BaseModel):
    """Leichtgewichtige Task-Darstellung für Board/Pipeline-Ansichten."""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    project_id: uuid.UUID
    board_column_id: uuid.UUID
    board_position: float
    pipeline_column_id: uuid.UUID | None
    pipeline_position: float | None
    assignee: str
    assignee_user: AssigneeUser | None = None
    due_date: date | None
    is_completed: bool
    is_pinned: bool
    recurrence_rule: str | None = None
    template_id: uuid.UUID | None = None
    tags: list[TagOut] = []
    checklist_total: int = 0
    checklist_done: int = 0
    pipeline_column_name: str | None = None
    pipeline_column_color: str | None = None
    # Agent-Präsenz: vom Agenten erzeugte/berührte Task, die eine menschliche
    # Prüfung erwartet -- in Board/Pipeline/Agenda als Hinweis sichtbar.
    needs_review: bool = False


# --- Board ---

class BoardColumnWithTasks(BoardColumnOut):
    tasks: list[TaskCard] = []


class BoardOut(BaseModel):
    project: ProjectOut
    columns: list[BoardColumnWithTasks]


# --- Pipeline ---

class PipelineColumnWithTasks(PipelineColumnOut):
    tasks: list[TaskCard] = []


class PipelineOut(BaseModel):
    columns: list[PipelineColumnWithTasks]


# --- Auth ---

class LoginRequest(BaseModel):
    email: str
    password: str
    mfa_code: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    requires_mfa: bool = False
    mfa_token: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    avatar_url: str | None = None
    is_active: bool = True
    mfa_enabled: bool = False


# --- Board Column Management ---

class BoardColumnCreate(BaseModel):
    name: str
    color: str | None = None
    icon_emoji: str | None = None
    position: float | None = None
    is_archive: bool = False


class BoardColumnUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon_emoji: str | None = None
    position: float | None = None
    is_archive: bool | None = None


class PipelineColumnUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon_emoji: str | None = None
    position: float | None = None


# --- Agent Jobs ---

class AgentJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task_id: uuid.UUID | None
    job_type: str | None = None
    status: str
    llm_model: str | None
    tokens_used: int | None
    cost_usd: float | None
    output: str | None
    error_message: str | None
    metadata_json: dict | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class AgentJobCreate(BaseModel):
    task_id: uuid.UUID | None = None
    job_type: str | None = None
    llm_model: str | None = None
    metadata_json: dict | None = None


class AgentJobUpdate(BaseModel):
    status: str | None = None
    output: str | None = None
    error_message: str | None = None
    llm_model: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    metadata_json: dict | None = None


class AgentJobWithTask(AgentJobOut):
    task_title: str | None = None


# --- Email Triage ---

class EmailTriageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    message_id: str
    subject: str | None
    from_address: str | None
    from_name: str | None
    received_at: datetime | None
    inference_class: str | None
    triage_class: str | None
    confidence: float | None
    suggested_action: dict | None
    agent_job_id: uuid.UUID | None
    status: str
    created_at: datetime


class EmailTriageUpdate(BaseModel):
    triage_class: str | None = None
    status: str | None = None
    suggested_action: dict | None = None


# --- LLM Chat ---

class LlmConversationCreate(BaseModel):
    title: str | None = None
    task_id: uuid.UUID | None = None
    model: str
    mode: str = "chat"
    temperature: float = 0.7


class LlmConversationUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    mode: str | None = None
    temperature: float | None = None


class LlmMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    tokens: int | None = None
    cost_usd: float | None = None
    attachments: list | None = None
    citations: list | None = None
    created_at: datetime


class LlmConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str | None = None
    task_id: uuid.UUID | None = None
    model: str
    mode: str
    temperature: float
    total_tokens: int
    total_cost_usd: float
    created_at: datetime
    updated_at: datetime


class LlmConversationWithMessages(LlmConversationOut):
    messages: list[LlmMessageOut] = []


class LlmConversationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str | None = None
    task_id: uuid.UUID | None = None
    model: str
    mode: str
    total_tokens: int
    total_cost_usd: float
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None


class ChatMessageCreate(BaseModel):
    content: str
    attachments: list[dict] | None = None


# --- Web Search ---

class WebSearchRequest(BaseModel):
    query: str
    search_depth: str = "basic"
    task_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class WebSearchResultItem(BaseModel):
    title: str
    url: str
    content: str
    score: float | None = None


class WebSearchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    query: str
    provider: str
    results: list
    result_count: int
    triggered_by: str
    task_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    credits_used: int
    created_at: datetime


# --- LLM Settings ---

class LlmProviderConfig(BaseModel):
    enabled: bool = False
    models: list[str] = []


class LlmSettings(BaseModel):
    llm_providers: dict[str, LlmProviderConfig] | None = None
    llm_default_model: str | None = None
    llm_default_local_model: str | None = None
    llm_default_temperature: float | None = None


# --- Mind-Maps ---

class MindmapFolderCreate(BaseModel):
    name: str
    parent_id: uuid.UUID | None = None
    color: str | None = None
    icon_emoji: str | None = None
    position: float = 0


class MindmapFolderUpdate(BaseModel):
    name: str | None = None
    parent_id: uuid.UUID | None = None
    color: str | None = None
    icon_emoji: str | None = None
    position: float | None = None


class MindmapFolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    color: str | None = None
    icon_emoji: str | None = None
    position: float
    created_at: datetime
    updated_at: datetime


class MindmapCreate(BaseModel):
    title: str
    folder_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    visibility: str = "private"
    flow_data: dict | None = None
    settings: dict | None = None
    background_url: str | None = None
    background_color: str | None = None
    is_template: bool = False


class MindmapUpdate(BaseModel):
    title: str | None = None
    folder_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    visibility: str | None = None
    flow_data: dict | None = None
    settings: dict | None = None
    background_url: str | None = None
    background_color: str | None = None
    thumbnail_url: str | None = None
    is_template: bool | None = None


class MindmapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    folder_id: uuid.UUID | None
    project_id: uuid.UUID | None
    owner_id: uuid.UUID
    visibility: str
    flow_data: dict
    settings: dict
    background_url: str | None = None
    background_color: str | None = None
    thumbnail_url: str | None = None
    is_template: bool
    created_at: datetime
    updated_at: datetime


class MindmapListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    folder_id: uuid.UUID | None
    project_id: uuid.UUID | None
    project_name: str | None = None
    owner_id: uuid.UUID
    visibility: str
    background_url: str | None = None
    background_color: str | None = None
    thumbnail_url: str | None = None
    is_template: bool
    share_count: int = 0
    created_at: datetime
    updated_at: datetime


class MindmapShareCreate(BaseModel):
    password: str
    permission: str = "view"
    label: str | None = None
    expires_at: datetime | None = None


class MindmapShareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    mindmap_id: uuid.UUID
    token: str
    permission: str
    label: str | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime


class ConvertToTasksRequest(BaseModel):
    node_ids: list[str]
    project_id: uuid.UUID
    board_column_id: uuid.UUID


class ConvertToTasksResponse(BaseModel):
    created_task_ids: list[uuid.UUID]
    count: int


class ShareVerifyRequest(BaseModel):
    password: str
