export interface Tag {
  id: string;
  name: string;
  color: string;
}

export interface AssigneeUser {
  id: string;
  display_name: string;
  avatar_url: string | null;
}

export interface TaskCard {
  id: string;
  title: string;
  project_id: string;
  board_column_id: string;
  board_position: number;
  pipeline_column_id: string | null;
  pipeline_position: number | null;
  assignee: string;
  assignee_user: AssigneeUser | null;
  due_date: string | null;
  is_completed: boolean;
  is_pinned: boolean;
  recurrence_rule: string | null;
  template_id: string | null;
  tags: Tag[];
  checklist_total: number;
  checklist_done: number;
  pipeline_column_name: string | null;
  pipeline_column_color: string | null;
  needs_review?: boolean;
}

export interface ChecklistItem {
  id: string;
  text: string;
  is_checked: boolean;
  position: number;
}

export interface TaskDetail {
  id: string;
  title: string;
  description: string | null;
  project_id: string;
  board_column_id: string;
  board_position: number;
  pipeline_column_id: string | null;
  pipeline_position: number | null;
  assignee: string;
  assignee_user: AssigneeUser | null;
  due_date: string | null;
  is_completed: boolean;
  is_pinned: boolean;
  data_class: string;
  llm_override: string | null;
  autonomy_level: string;
  recurrence_rule: string | null;
  recurrence_end_date: string | null;
  recurrence_max_instances: number | null;
  template_id: string | null;
  email_message_id: string | null;
  email_conversation_id: string | null;
  calendar_event_id: string | null;
  calendar_duration_minutes: number | null;
  calendar_preferred_time: string | null;
  meeting_transcript_id: string | null;
  needs_review: boolean;
  pipedrive_deal_id: number | null;
  pipedrive_person_id: number | null;
  source_email_subject: string | null;
  source_email_from: string | null;
  meeting_subject: string | null;
  tags: Tag[];
  checklist_items: ChecklistItem[];
  created_at: string;
  updated_at: string;
}

export interface BoardColumn {
  id: string;
  name: string;
  color: string | null;
  icon_emoji: string | null;
  position: number;
  project_id: string;
}

export interface Project {
  id: string;
  name: string;
  color: string;
  background_url: string | null;
  icon_url: string | null;
  icon_emoji: string | null;
  status: string;
  board_columns: BoardColumn[];
}

export interface BoardData {
  project: Project;
  columns: Array<BoardColumn & { tasks: TaskCard[] }>;
}

export interface PipelineColumn {
  id: string;
  name: string;
  color: string | null;
  icon_emoji: string | null;
  position: number;
  tasks: TaskCard[];
}

export interface PipelineData {
  columns: PipelineColumn[];
}

export interface LoginRequest {
  email: string;
  password: string;
  mfa_code?: string;
}

export interface LoginResponse {
  access_token: string;
  requires_mfa?: boolean;
  mfa_token?: string;
}

export type UserRole = 'owner' | 'member' | 'viewer';

export interface UserProfile {
  id: string;
  email: string;
  display_name: string;
  avatar_url?: string | null;
  role: UserRole;
  mfa_enabled?: boolean;
}

export interface TaskUpdatePayload {
  title?: string;
  description?: string;
  project_id?: string;
  board_column_id?: string;
  board_position?: number;
  pipeline_column_id?: string | null;
  pipeline_position?: number | null;
  assignee?: string;
  due_date?: string | null;
  is_completed?: boolean;
  is_pinned?: boolean;
  data_class?: string;
  llm_override?: string;
  autonomy_level?: string;
  recurrence_rule?: string | null;
  recurrence_end_date?: string | null;
  recurrence_max_instances?: number | null;
  calendar_event_id?: string | null;
  calendar_duration_minutes?: number | null;
  calendar_preferred_time?: string | null;
}

export type TaskDetailMode = 'modal' | 'panel' | 'fullscreen';

export interface TaskCreatePayload {
  title: string;
  project_id: string;
  board_column_id: string;
  description?: string;
  pipeline_column_id?: string;
  assignee?: string;
  due_date?: string;
  recurrence_rule?: string;
}

// --- Agent Jobs ---

export interface AgentJob {
  id: string;
  task_id: string | null;
  job_type: string | null;
  status: 'planned' | 'queued' | 'running' | 'awaiting_approval' | 'completed' | 'failed' | 'blocked';
  llm_model: string | null;
  tokens_used: number | null;
  cost_usd: number | null;
  output: string | null;
  error_message: string | null;
  metadata_json: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  task_title: string | null;
}

// --- Project Metrics ---

export interface ProjectMetrics {
  id: string;
  name: string;
  color: string;
  status: string;
  total_tasks: number;
  open_tasks: number;
  completed_tasks: number;
  overdue_tasks: number;
  progress_pct: number;
}

// --- Search ---

export interface SearchTaskHit {
  id: string;
  title: string;
  project_id: string;
  project_name: string;
  assignee: string;
  is_completed: boolean;
  due_date: string | null;
}

export interface SearchProjectHit {
  id: string;
  name: string;
  color: string;
  status: string;
}

export interface SearchTagHit {
  id: string;
  name: string;
  color: string;
}

export interface SearchResults {
  tasks: SearchTaskHit[];
  projects: SearchProjectHit[];
  tags: SearchTagHit[];
}

// --- LLM Chat ---

export interface LlmModel {
  id: string;
  name: string;
  type: 'local' | 'cloud';
  provider: string;
  capabilities: string[];
}

export interface LlmConversation {
  id: string;
  title: string | null;
  task_id: string | null;
  model: string;
  mode: string;
  temperature: number;
  total_tokens: number;
  total_cost_usd: number;
  created_at: string;
  updated_at: string;
  message_count?: number;
  last_message_preview?: string | null;
}

export interface LlmMessage {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  tokens: number | null;
  cost_usd: number | null;
  attachments: unknown[];
  citations: unknown[];
  created_at: string;
}

export interface LlmConversationWithMessages extends LlmConversation {
  messages: LlmMessage[];
}

// --- Web Search ---

export interface WebSearchResult {
  id: string;
  query: string;
  provider: string;
  results: Array<{
    title: string;
    url: string;
    content: string;
    score: number | null;
  }>;
  result_count: number;
  triggered_by: string;
  task_id: string | null;
  conversation_id: string | null;
  credits_used: number;
  created_at: string;
}

// --- LLM Settings ---

export interface LlmProviderConfig {
  enabled: boolean;
  models: string[];
}

export interface LlmSettings {
  llm_providers: Record<string, LlmProviderConfig> | null;
  llm_default_model: string | null;
  llm_default_temperature: number | null;
}

// --- Mind-Maps ---

export interface MindmapFolder {
  id: string;
  name: string;
  parent_id: string | null;
  color: string | null;
  icon_emoji: string | null;
  position: number;
  created_at: string;
  updated_at: string;
}

export interface MindmapListItem {
  id: string;
  title: string;
  folder_id: string | null;
  project_id: string | null;
  project_name: string | null;
  owner_id: string;
  visibility: 'private' | 'project' | 'shared';
  background_url: string | null;
  background_color: string | null;
  thumbnail_url: string | null;
  is_template: boolean;
  share_count: number;
  created_at: string;
  updated_at: string;
}

export interface MindmapDetail {
  id: string;
  title: string;
  folder_id: string | null;
  project_id: string | null;
  owner_id: string;
  visibility: 'private' | 'project' | 'shared';
  flow_data: {
    nodes: any[];
    edges: any[];
    viewport: { x: number; y: number; zoom: number };
  };
  settings: Record<string, any>;
  background_url: string | null;
  background_color: string | null;
  thumbnail_url: string | null;
  is_template: boolean;
  created_at: string;
  updated_at: string;
}

export interface MindmapShare {
  id: string;
  mindmap_id: string;
  token: string;
  permission: 'view' | 'edit';
  label: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}
