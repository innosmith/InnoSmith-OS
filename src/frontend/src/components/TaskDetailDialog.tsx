import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { TaskDetailContent, TaskDetailAttachments, TaskDetailActivity, TaskDetailSidebar } from './task-detail';
import { TracePanel } from './TracePanel';
import { FormattedOutput } from './FormattedOutput';
import type { ActivityLogEntry, AttachmentEntry, ModelsData } from './task-detail';
import {
  CloseIcon, ModalIcon, PanelIcon, FullscreenIcon, AgentSmallIcon,
  MoreHorizontalIcon, TrashIcon, CopyIcon, LinkIcon, SectionLabel,
  MailIcon,
} from './task-detail/shared';
import type {
  TaskDetail, TaskUpdatePayload, AgentJob, Tag,
  TaskDetailMode, PipelineColumn, PipelineData, Project, BoardColumn,
} from '../types';

interface TaskDetailDialogProps {
  taskId: string | null;
  onClose: () => void;
  onUpdated: () => void;
  onOpenTask?: (taskId: string) => void;
  reviewMode?: boolean;
  onReviewConfirm?: () => void;
  onReviewDismiss?: () => void;
}

export function TaskDetailDialog({ taskId, onClose, onUpdated, onOpenTask, reviewMode, onReviewConfirm, onReviewDismiss }: TaskDetailDialogProps) {
  const { isOwner, user: authUser } = useAuth();
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [agentJobs, setAgentJobs] = useState<AgentJob[]>([]);
  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [allProjects, setAllProjects] = useState<Project[]>([]);
  const [pipelineCols, setPipelineCols] = useState<PipelineColumn[]>([]);
  const [boardMembers, setBoardMembers] = useState<{ user_id: string; display_name: string; avatar_url?: string | null }[]>([]);
  const [currentProject, setCurrentProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<TaskDetailMode>('modal');
  const [models, setModels] = useState<ModelsData | null>(null);
  const [defaultLocalModel, setDefaultLocalModel] = useState('');
  const [activities, setActivities] = useState<ActivityLogEntry[]>([]);
  const [attachments, setAttachments] = useState<AttachmentEntry[]>([]);
  const [showActionMenu, setShowActionMenu] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.get<ModelsData>('/api/models').then(setModels).catch(() => {});
    api.get<Project[]>('/api/projects').then(setAllProjects).catch(() => {});
    api.get<PipelineData>('/api/pipeline').then((d) => setPipelineCols(d.columns)).catch(() => {});
    api.get<{ llm_default_local_model: string | null }>('/api/settings/llm')
      .then(s => { if (s.llm_default_local_model) setDefaultLocalModel(s.llm_default_local_model); })
      .catch(() => {});
  }, []);

  useEffect(() => {
    api.get<{ task_detail_mode?: string }>('/api/settings')
      .then((s) => { if (s.task_detail_mode === 'panel' || s.task_detail_mode === 'fullscreen') setMode(s.task_detail_mode); })
      .catch(() => {});
  }, []);

  const persistMode = useCallback((m: TaskDetailMode) => {
    setMode(m);
    api.patch('/api/settings', { task_detail_mode: m }).catch(() => {});
  }, []);

  const loadProject = useCallback((projectId: string) => {
    api.get<Project>(`/api/projects/${projectId}`)
      .then(setCurrentProject)
      .catch(() => {});
    api.get<{ user_id: string; display_name: string; avatar_url?: string | null }[]>(`/api/projects/${projectId}/members`)
      .then(setBoardMembers)
      .catch(() => setBoardMembers([]));
  }, []);

  useEffect(() => {
    if (!taskId) { setTask(null); setAgentJobs([]); setActivities([]); setAttachments([]); setCurrentProject(null); return; }
    setLoading(true);
    Promise.all([
      api.get<TaskDetail>(`/api/tasks/${taskId}`),
      api.get<Tag[]>('/api/tags'),
      api.get<ActivityLogEntry[]>(`/api/tasks/${taskId}/activity`),
      api.get<AttachmentEntry[]>(`/api/tasks/${taskId}/attachments`),
    ])
      .then(([taskData, tagsData, activityData, attachmentData]) => {
        setTask(taskData);
        setAllTags(tagsData);
        setActivities(activityData);
        setAttachments(attachmentData);
        loadProject(taskData.project_id);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    api.get<AgentJob[]>(`/api/agent-jobs?task_id=${taskId}`)
      .then(setAgentJobs).catch(() => setAgentJobs([]));
  }, [taskId, loadProject]);

  const refreshAgentJobs = useCallback(async () => {
    if (!taskId) return;
    try {
      const jobs = await api.get<AgentJob[]>(`/api/agent-jobs?task_id=${taskId}`);
      setAgentJobs(jobs);
    } catch { /* ignore */ }
  }, [taskId]);

  const refreshTask = useCallback(async () => {
    if (!taskId) return;
    const [updated, activityData] = await Promise.all([
      api.get<TaskDetail>(`/api/tasks/${taskId}`),
      api.get<ActivityLogEntry[]>(`/api/tasks/${taskId}/activity`),
    ]);
    setTask(updated);
    setActivities(activityData);
  }, [taskId]);

  const updateTask = useCallback(async (payload: TaskUpdatePayload) => {
    if (!taskId) return;
    await api.patch(`/api/tasks/${taskId}`, payload);
    onUpdated();
    const updated = await api.get<TaskDetail>(`/api/tasks/${taskId}`);
    setTask(updated);
    if (payload.project_id) loadProject(payload.project_id);
    else if (payload.board_column_id && currentProject) {
      setCurrentProject({ ...currentProject });
    }
  }, [taskId, onUpdated, loadProject, currentProject]);

  const handleProjectChange = useCallback(async (newProjectId: string) => {
    if (!task || newProjectId === task.project_id) return;
    const proj = allProjects.find((p) => p.id === newProjectId);
    if (!proj) return;
    const firstCol = proj.board_columns?.sort((a, b) => a.position - b.position)[0];
    if (!firstCol) return;
    await updateTask({ project_id: newProjectId, board_column_id: firstCol.id } as TaskUpdatePayload);
  }, [task, allProjects, updateTask]);

  const refreshTags = useCallback(async () => {
    const tags = await api.get<Tag[]>('/api/tags');
    setAllTags(tags);
  }, []);

  const toggleTag = useCallback(async (tag: Tag) => {
    if (!taskId || !task) return;
    const hasTag = task.tags.some((t) => t.id === tag.id);
    if (hasTag) await api.delete(`/api/tags/tasks/${taskId}/tags/${tag.id}`);
    else await api.post(`/api/tags/tasks/${taskId}/tags/${tag.id}`);
    const updated = await api.get<TaskDetail>(`/api/tasks/${taskId}`);
    setTask(updated);
    onUpdated();
  }, [taskId, task, onUpdated]);

  const handleDuplicate = useCallback(async () => {
    if (!task || !taskId) return;
    setShowActionMenu(false);
    const copy = await api.post<TaskDetail>('/api/tasks', {
      title: `${task.title} (Kopie)`,
      description: task.description,
      project_id: task.project_id,
      board_column_id: task.board_column_id,
      pipeline_column_id: task.pipeline_column_id,
      assignee: task.assignee,
      due_date: task.due_date,
    });
    for (const tag of task.tags) {
      await api.post(`/api/tags/tasks/${copy.id}/tags/${tag.id}`).catch(() => {});
    }
    onUpdated();
  }, [task, taskId, onUpdated]);

  const handleCopyLink = useCallback(() => {
    if (!taskId) return;
    setShowActionMenu(false);
    const url = `${window.location.origin}/tasks/${taskId}`;
    navigator.clipboard.writeText(url).catch(() => {});
  }, [taskId]);

  const handleDelete = useCallback(async () => {
    if (!taskId) return;
    setDeleting(true);
    try {
      await api.delete(`/api/tasks/${taskId}`);
      onUpdated();
      onClose();
    } finally {
      setDeleting(false);
      setShowDeleteConfirm(false);
    }
  }, [taskId, onUpdated, onClose]);

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (showDeleteConfirm) { setShowDeleteConfirm(false); return; }
        if (showActionMenu) { setShowActionMenu(false); return; }
        onClose();
      }
    };
    const handleKeyShortcut = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && task) {
        e.preventDefault();
        // Wiederkehrende Vorlagen können nicht erledigt werden.
        if (task.recurrence_rule && !task.template_id) return;
        updateTask({ is_completed: !task.is_completed });
      }
    };
    document.addEventListener('keydown', handleEsc);
    document.addEventListener('keydown', handleKeyShortcut);
    return () => {
      document.removeEventListener('keydown', handleEsc);
      document.removeEventListener('keydown', handleKeyShortcut);
    };
  }, [onClose, showDeleteConfirm, showActionMenu, task, updateTask]);

  useEffect(() => {
    if (!showActionMenu) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (actionMenuRef.current && !actionMenuRef.current.contains(e.target as Node)) {
        setShowActionMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showActionMenu]);

  if (!taskId) return null;

  const backdropClass =
    mode === 'fullscreen'
      ? 'fixed inset-0 z-50'
      : mode === 'modal'
        ? 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm'
        : 'fixed inset-0 z-50 flex justify-end bg-black/30 backdrop-blur-sm';

  const panelClass =
    mode === 'fullscreen'
      ? 'flex h-full w-full flex-col overflow-hidden bg-white pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] dark:bg-gray-950'
      : mode === 'modal'
        ? 'flex w-full max-w-4xl max-h-[90dvh] flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-gray-200 dark:bg-gray-950 dark:ring-gray-700/60'
        : 'flex h-full w-full max-w-4xl flex-col overflow-hidden bg-white shadow-2xl ring-1 ring-gray-200 dark:bg-gray-950 dark:ring-gray-700/60';

  const boardColumns: BoardColumn[] = currentProject?.board_columns?.sort((a, b) => a.position - b.position) || [];

  return (
    <div ref={backdropRef} className={backdropClass} onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}>
      <div className={panelClass}>
        {/* ─── Header ─── */}
        <div className="flex items-center justify-between border-b border-gray-100 bg-gray-50/60 px-5 py-2.5 dark:border-gray-800 dark:bg-gray-900/40">
          <div className="flex items-center gap-3">
            {task?.recurrence_rule && !task?.template_id ? (
              <span
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-50/80 px-2 py-0.5 text-[11px] font-semibold text-indigo-600 ring-1 ring-indigo-200/60 dark:bg-indigo-950/40 dark:text-indigo-300 dark:ring-indigo-800/40"
                title="Wiederkehrende Vorlage — Instanzen werden automatisch erzeugt und einzeln erledigt"
              >
                Wiederkehrende Vorlage
              </span>
            ) : (
              <label className="flex cursor-pointer items-center gap-2" title="Cmd/Ctrl+Enter">
                <input
                  type="checkbox"
                  checked={task?.is_completed ?? false}
                  onChange={() => task && updateTask({ is_completed: !task.is_completed })}
                  className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 dark:border-gray-600"
                />
                <span className={`text-sm font-medium ${task?.is_completed ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-400'}`}>
                  {task?.is_completed ? 'Erledigt' : 'Offen'}
                </span>
              </label>
            )}
            {currentProject && (
              <span className="rounded-md bg-white/80 px-2 py-0.5 text-[11px] font-medium text-gray-500 shadow-sm ring-1 ring-gray-200/60 dark:bg-gray-800 dark:text-gray-400 dark:ring-gray-700">
                {currentProject.name}
              </span>
            )}
            {task?.email_message_id && (
              <span className="rounded-md bg-sky-50/80 px-2 py-0.5 text-[11px] font-medium text-sky-600 ring-1 ring-sky-200/60 dark:bg-sky-950/40 dark:text-sky-400 dark:ring-sky-800/40">
                Aus E-Mail
              </span>
            )}
          </div>
          <div className="flex items-center gap-0.5">
            {/* Action Menu */}
            <div className="relative" ref={actionMenuRef}>
              <button
                onClick={() => setShowActionMenu(!showActionMenu)}
                className="rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-200/60 dark:hover:bg-gray-800"
                title="Aktionen"
              >
                <MoreHorizontalIcon className="h-4 w-4" />
              </button>
              {showActionMenu && (
                <div className="absolute right-0 top-9 z-20 w-48 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  <button onClick={handleDuplicate} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800">
                    <CopyIcon className="h-3.5 w-3.5 text-gray-400" /> Duplizieren
                  </button>
                  <button onClick={handleCopyLink} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800">
                    <LinkIcon className="h-3.5 w-3.5 text-gray-400" /> Link kopieren
                  </button>
                  <div className="my-1 border-t border-gray-100 dark:border-gray-800" />
                  <button onClick={() => { setShowActionMenu(false); setShowDeleteConfirm(true); }} className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30">
                    <TrashIcon className="h-3.5 w-3.5" /> Löschen…
                  </button>
                </div>
              )}
            </div>

            <div className="mx-1 h-4 w-px bg-gray-200 dark:bg-gray-700" />

            {([['modal', 'Modal', ModalIcon], ['panel', 'Seitenpanel', PanelIcon], ['fullscreen', 'Vollbild', FullscreenIcon]] as const).map(([m, title, Icon]) => (
              <button key={m} onClick={() => persistMode(m as TaskDetailMode)} className={`rounded-md p-1.5 transition-colors ${mode === m ? 'bg-indigo-100 text-indigo-600 dark:bg-indigo-950 dark:text-indigo-400' : 'text-gray-400 hover:bg-gray-200/60 dark:hover:bg-gray-800'}`} title={title}>
                <Icon className="h-4 w-4" />
              </button>
            ))}
            <div className="mx-1 h-4 w-px bg-gray-200 dark:bg-gray-700" />
            <button onClick={onClose} className="rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-200/60 dark:hover:bg-gray-800">
              <CloseIcon className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* ─── Delete Confirmation ─── */}
        {showDeleteConfirm && (
          <div className="border-b border-red-100 bg-red-50/80 px-5 py-3 dark:border-red-900/40 dark:bg-red-950/20">
            <div className="flex items-center justify-between">
              <p className="text-sm text-red-700 dark:text-red-300">Task unwiderruflich löschen?</p>
              <div className="flex gap-2">
                <button onClick={() => setShowDeleteConfirm(false)} className="rounded-lg px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-white dark:text-gray-400 dark:hover:bg-gray-800">Abbrechen</button>
                <button onClick={handleDelete} disabled={deleting} className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-red-700 disabled:opacity-50">
                  {deleting ? 'Wird gelöscht…' : 'Löschen'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ─── Review-Herkunfts-Banner ─── */}
        {reviewMode && task && (
          <div className="flex items-center gap-2.5 border-b border-amber-200/60 bg-amber-50/80 px-5 py-2.5 dark:border-amber-800/40 dark:bg-amber-950/20">
            {task.email_message_id ? (
              <>
                <MailIcon className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <div className="min-w-0 flex-1">
                  <span className="text-sm text-amber-800 dark:text-amber-300">
                    Vorschlag aus E-Mail-Triage
                  </span>
                  {task.source_email_subject && (
                    <span className="ml-1 text-sm text-amber-700/70 dark:text-amber-400/60">
                      — {task.source_email_subject}
                      {task.source_email_from && ` (${task.source_email_from})`}
                    </span>
                  )}
                </div>
              </>
            ) : (
              <>
                <AgentSmallIcon className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <span className="text-sm text-amber-800 dark:text-amber-300">
                  Automatisch erstellt am {new Date(task.created_at).toLocaleDateString('de-CH')}
                </span>
              </>
            )}
            <span className="shrink-0 rounded-full bg-amber-200/60 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-800/40 dark:text-amber-300">
              Prüfung ausstehend
            </span>
          </div>
        )}

        {loading ? (
          <div className="flex flex-1 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
          </div>
        ) : task ? (
          <div className="flex-1 overflow-y-auto">
            <div className="grid grid-cols-1 md:grid-cols-[1fr_340px]">
              {/* ═══════════ Linke Spalte: Inhalt ═══════════ */}
              <div className="min-w-0 space-y-6 p-6 md:border-r md:border-gray-100 md:dark:border-gray-800">
                <TaskDetailContent
                  task={task}
                  taskId={taskId}
                  onUpdated={onUpdated}
                  updateTask={updateTask}
                  refreshTask={refreshTask}
                  onOpenTask={onOpenTask}
                />

                <TaskDetailAttachments
                  taskId={taskId}
                  attachments={attachments}
                  onAttachmentsChanged={setAttachments}
                  isOwner={isOwner}
                />

                {/* Agent-Aufträge (Owner-only) */}
                {isOwner && agentJobs.length > 0 && (
                  <section>
                    <SectionLabel icon={AgentSmallIcon} text="Agent-Aufträge" />
                    <div className="space-y-2">
                      {agentJobs.map((job) => (
                        <div key={job.id} className="rounded-xl border border-gray-100 bg-gray-50/40 p-3 dark:border-gray-800 dark:bg-gray-900/30">
                          <div className="flex items-center justify-between">
                            <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                              job.status === 'completed' ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' :
                              job.status === 'running' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                              job.status === 'failed' ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' :
                              job.status === 'awaiting_approval' ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300' :
                              'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                            }`}>{job.status}</span>
                            <span className="text-[10px] text-gray-400">{new Date(job.created_at).toLocaleString('de-CH')}</span>
                          </div>
                          {job.output && (
                            <div className="mt-2 max-h-72 overflow-y-auto rounded-lg bg-white p-2.5 text-xs text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                              <FormattedOutput output={job.output} />
                            </div>
                          )}
                          {job.error_message && <p className="mt-2 text-xs text-red-600 dark:text-red-400">{job.error_message}</p>}
                          {(job.status === 'completed' || job.status === 'failed' || job.status === 'awaiting_approval') && (
                            <div className="mt-1.5"><TracePanel jobId={job.id} compact /></div>
                          )}
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <TaskDetailActivity
                  taskId={taskId}
                  projectId={task?.project_id}
                  activities={activities}
                  onActivitiesChanged={setActivities}
                  currentUserEmail={authUser?.email}
                  isOwner={isOwner}
                  refreshTask={refreshTask}
                />
              </div>

              {/* ═══════════ Rechte Spalte: Attribute ═══════════ */}
              <TaskDetailSidebar
                task={task}
                taskId={taskId}
                isOwner={isOwner}
                authUser={authUser ? { id: authUser.id, email: authUser.email, avatar_url: authUser.avatar_url } : null}
                allProjects={allProjects}
                pipelineCols={pipelineCols}
                boardColumns={boardColumns}
                boardMembers={boardMembers}
                allTags={allTags}
                models={models}
                defaultLocalModel={defaultLocalModel}
                updateTask={updateTask}
                handleProjectChange={handleProjectChange}
                toggleTag={toggleTag}
                onTagsChanged={refreshTags}
                agentJobs={agentJobs}
                onAgentJobsChanged={refreshAgentJobs}
              />
            </div>
          </div>
        ) : (
          <div className="flex flex-1 items-center justify-center text-gray-500">Task nicht gefunden.</div>
        )}

        {/* ─── Review-Aktionsleiste ─── */}
        {reviewMode && task && (
          <div className="flex items-center justify-end gap-3 border-t border-gray-200 bg-gray-50/80 px-5 py-3 dark:border-gray-800 dark:bg-gray-900/60">
            <button
              onClick={onReviewDismiss}
              className="rounded-lg px-4 py-2 text-sm font-medium text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30"
            >
              Verwerfen
            </button>
            <button
              onClick={onReviewConfirm}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-emerald-700"
            >
              Übernehmen
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
