import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  DndContext,
  DragOverlay,
  closestCorners,
  pointerWithin,
  PointerSensor,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragStartEvent,
  type DragEndEvent,
  type DragOverEvent,
} from '@dnd-kit/core';
import { arrayMove, SortableContext, horizontalListSortingStrategy } from '@dnd-kit/sortable';
import { api } from '../api/client';
import {
  findColumnOfTask,
  moveTaskToColumn,
  persistTaskOrder,
  reorderWithinColumn,
} from '../lib/taskReorder';
import { KanbanColumn } from '../components/KanbanColumn';
import { ColumnPager } from '../components/ColumnPager';
import { TaskCard } from '../components/TaskCard';
import { TaskDetailDialog } from '../components/TaskDetailDialog';
import { BackgroundPicker } from '../components/BackgroundPicker';
import type {
  PipelineColumn,
  PipelineData,
  TaskCard as TaskCardType,
  TaskCreatePayload,
  Project,
} from '../types';

export function PipelinePage() {
  const navigate = useNavigate();
  const [columns, setColumns] = useState<PipelineColumn[]>([]);
  // Spiegelt `columns` synchron: Drag-Events feuern schneller als React
  // rendert, der State im Closure kann darum veraltet sein. Alle Schreibzugriffe
  // laufen über commitColumns, damit Ref und State nie auseinanderlaufen.
  const columnsRef = useRef<PipelineColumn[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeTask, setActiveTask] = useState<TaskCardType | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [agendaBg, setAgendaBg] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [userAvatarUrl, setUserAvatarUrl] = useState<string | null>(null);
  const [showColCount, setShowColCount] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const settingsRef = useRef<HTMLDivElement>(null);
  /** Spalte, aus der der laufende Drag gestartet ist (für den Reorder-Request). */
  const dragSourceColumnRef = useRef<string | null>(null);

  const commitColumns = useCallback((next: PipelineColumn[]) => {
    columnsRef.current = next;
    setColumns(next);
  }, []);

  useEffect(() => {
    if (!settingsOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setSettingsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [settingsOpen]);

  const projectColorMap = Object.fromEntries(
    projects.map((p) => [p.id, p.color]),
  );

  const projectMap = Object.fromEntries(
    projects.map((p) => [p.id, p]),
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const collisionDetection: CollisionDetection = (args) => {
    const pointerCollisions = pointerWithin(args);
    if (pointerCollisions.length > 0) {
      const hasItem = pointerCollisions.some(
        (c) => !columns.some((col) => col.id === c.id),
      );
      if (hasItem) {
        return pointerCollisions.filter(
          (c) => !columns.some((col) => col.id === c.id),
        );
      }
      return pointerCollisions;
    }
    return closestCorners(args);
  };

  const fetchData = useCallback(async () => {
    try {
      const [pipelineData, projectData] = await Promise.all([
        api.get<PipelineData>('/api/pipeline'),
        api.get<Project[]>('/api/projects'),
      ]);
      commitColumns(pipelineData.columns);
      setProjects(projectData);
    } catch {
      /* Pipeline + Projekte sind kritisch -- Fehler wird vom api-Client geloggt */
    }

    api.get<{ agenda_background_url: string | null; show_column_count: boolean | null }>('/api/settings')
      .then((s) => { setAgendaBg(s.agenda_background_url); setShowColCount(s.show_column_count ?? false); })
      .catch(() => {});
    api.get<{ avatar_url: string | null }>('/api/auth/me')
      .then((u) => setUserAvatarUrl(u.avatar_url))
      .catch(() => {});

    setLoading(false);
  }, [commitColumns]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleCreateTask = useCallback(
    async (pipelineColumnId: string, title: string) => {
      if (projects.length === 0) {
        setCreateError('Erstelle zuerst ein Projekt, bevor du Tasks hinzufügst.');
        return;
      }
      const defaultProject = projects[0];
      const firstCol = defaultProject.board_columns?.[0];
      if (!firstCol) {
        setCreateError(`Projekt "${defaultProject.name}" hat keine Board-Spalten.`);
        return;
      }
      try {
        await api.post<TaskCardType>('/api/tasks', {
          title,
          project_id: defaultProject.id,
          board_column_id: firstCol.id,
          pipeline_column_id: pipelineColumnId,
        } satisfies TaskCreatePayload);
        setCreateError(null);
        fetchData();
      } catch {
        setCreateError('Task konnte nicht erstellt werden.');
      }
    },
    [projects, fetchData],
  );

  const isColumnId = (itemId: string) =>
    columns.some((col) => col.id === itemId);

  const handleDragStart = (event: DragStartEvent) => {
    const itemId = event.active.id as string;
    if (isColumnId(itemId)) return;
    const sourceCol = findColumnOfTask(columnsRef.current, itemId);
    if (!sourceCol) return;
    dragSourceColumnRef.current = sourceCol.id;
    setActiveTask(sourceCol.tasks.find((t) => t.id === itemId) ?? null);
  };

  const handleDragOver = (event: DragOverEvent) => {
    const { active, over } = event;
    if (!over) return;

    const activeId = active.id as string;
    if (isColumnId(activeId)) return;

    // Spaltenwechsel schon während des Ziehens anwenden, damit die Karte am
    // Zielort einrastet. Umsortieren innerhalb der Spalte macht erst dragEnd.
    const next = moveTaskToColumn(columnsRef.current, activeId, over.id as string);
    if (next) commitColumns(next);
  };

  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveTask(null);
    const sourceColumnId = dragSourceColumnRef.current;
    dragSourceColumnRef.current = null;

    if (!over) return;

    const activeId = active.id as string;
    const overId = over.id as string;

    if (isColumnId(activeId) && isColumnId(overId) && activeId !== overId) {
      const oldIndex = columnsRef.current.findIndex((c) => c.id === activeId);
      const newIndex = columnsRef.current.findIndex((c) => c.id === overId);
      if (oldIndex !== -1 && newIndex !== -1) {
        const reordered = arrayMove(columnsRef.current, oldIndex, newIndex);
        commitColumns(reordered);
        for (let i = 0; i < reordered.length; i++) {
          api.patch(`/api/pipeline/columns/${reordered[i].id}`, { position: i + 1 }).catch(() => {});
        }
      }
      return;
    }

    // Zielzustand synchron berechnen und genau diesen persistieren -- ein
    // Rückgriff auf den State nach commitColumns würde noch die alte
    // Reihenfolge liefern und die manuelle Priorisierung verwerfen.
    const next = reorderWithinColumn(columnsRef.current, activeId, overId) ?? columnsRef.current;
    commitColumns(next);

    const targetCol = findColumnOfTask(next, activeId);
    if (!targetCol) return;

    try {
      await persistTaskOrder('pipeline', next, [targetCol.id, sourceColumnId]);
    } catch {
      fetchData();
    }
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  const hasBg = !!agendaBg;
  const isGradient = agendaBg?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: agendaBg!.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${agendaBg})`, backgroundSize: 'cover', backgroundPosition: 'center' }
      : undefined;

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { agenda_background_url: url });
    setAgendaBg(url);
  };

  return (
    <div className="relative flex h-full flex-col" style={bgStyle}>
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/30" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/5 dark:bg-black/20" />}

      <div className={`relative z-20 border-b px-4 py-4 sm:px-6 ${hasBg ? 'border-white/10 bg-black/20 backdrop-blur-sm' : 'border-gray-200 dark:border-gray-800'}`}>
        <div className="flex items-center justify-between">
          <div>
            <h1 className={`hidden text-xl font-bold lg:block ${hasBg ? 'text-white' : 'text-gray-900 dark:text-white'}`}>
              Agenda
            </h1>
            <p className={`text-sm lg:mt-0.5 ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
              Alle Aufgaben nach Zeithorizont organisiert
            </p>
          </div>
          <div className="relative" ref={settingsRef}>
            <button
              onClick={() => setSettingsOpen(!settingsOpen)}
              className={`rounded-lg p-2 transition-colors ${
                hasBg
                  ? 'text-white/70 hover:bg-white/10 hover:text-white'
                  : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'
              }`}
              title="Agenda-Einstellungen"
            >
              <GearIcon className="h-5 w-5" />
            </button>
            {settingsOpen && (
              <div className="absolute right-0 top-10 z-20 w-56 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                <button
                  onClick={() => { setBgPickerOpen(true); setSettingsOpen(false); }}
                  className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  <CameraIcon className="h-4 w-4" />
                  Hintergrundbild
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      <div
        data-board-scroller
        className={`relative z-10 flex-1 overflow-x-auto scroll-px-4 p-4 sm:p-6 ${
          activeTask ? '' : 'snap-x snap-proximity sm:snap-none'
        }`}
      >
        {!loading && projects.length === 0 && (
          <div className={`relative z-20 mb-4 flex items-center gap-3 rounded-xl border px-4 py-3 ${
            hasBg
              ? 'border-white/20 bg-white/10 text-white backdrop-blur-sm'
              : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-700/50 dark:bg-amber-900/20 dark:text-amber-200'
          }`}>
            <InfoIcon className="h-5 w-5 shrink-0" />
            <span className="text-sm">Erstelle zuerst ein Projekt, um Tasks in der Agenda zu planen.</span>
            <button
              onClick={() => navigate('/projects')}
              className="ml-auto shrink-0 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-indigo-700"
            >
              Projekt erstellen
            </button>
          </div>
        )}

        {createError && (
          <div className={`relative z-20 mb-4 flex items-center gap-3 rounded-xl border px-4 py-3 ${
            hasBg
              ? 'border-red-400/30 bg-red-500/10 text-white backdrop-blur-sm'
              : 'border-red-200 bg-red-50 text-red-800 dark:border-red-700/50 dark:bg-red-900/20 dark:text-red-200'
          }`}>
            <span className="text-sm">{createError}</span>
            <button onClick={() => setCreateError(null)} className="ml-auto text-sm opacity-60 hover:opacity-100">✕</button>
          </div>
        )}

        <DndContext
          sensors={sensors}
          collisionDetection={collisionDetection}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <ColumnPager columns={columns} hasBg={hasBg} />
          <div className="flex gap-4" style={{ minHeight: 'calc(100dvh - 180px)' }}>
            <SortableContext
              items={columns.map((col) => col.id)}
              strategy={horizontalListSortingStrategy}
            >
              {columns.map((col) => (
                <KanbanColumn
                  key={col.id}
                  id={col.id}
                  title={col.name}
                  tasks={col.tasks}
                  projectColorMap={projectColorMap}
                  projectMap={projectMap}
                  showProjectIndicator
                  showColumnCount={showColCount}
                  hasBg={hasBg}
                  userAvatarUrl={userAvatarUrl}
                  columnColor={col.color}
                  columnIcon={col.icon_emoji}
                  onTaskClick={(task) => setSelectedTaskId(task.id)}
                  onCreateTask={projects.length > 0 ? handleCreateTask : undefined}
                  onRenameColumn={async (colId, name) => {
                    await api.patch(`/api/pipeline/columns/${colId}`, { name });
                    fetchData();
                  }}
                  onUpdateColumn={async (colId, updates) => {
                    await api.patch(`/api/pipeline/columns/${colId}`, updates);
                    fetchData();
                  }}
                  onDeleteColumn={async (colId) => {
                    await api.delete(`/api/pipeline/columns/${colId}`);
                    fetchData();
                  }}
                  onArchiveTask={async (taskId) => {
                    await api.patch(`/api/tasks/${taskId}`, { is_completed: true });
                    fetchData();
                  }}
                />
              ))}
            </SortableContext>
            <div className="flex w-[calc(100vw-2rem)] shrink-0 snap-start flex-col sm:w-72">
              <button
                onClick={async () => {
                  await api.post('/api/pipeline/columns', { name: 'Neue Spalte' });
                  fetchData();
                }}
                className={`flex items-center justify-center gap-2 rounded-xl border-2 border-dashed px-4 py-3 text-sm font-medium transition-colors ${
                  hasBg
                    ? 'border-white/20 text-white/60 hover:bg-white/10 hover:text-white'
                    : 'border-gray-200 text-gray-400 hover:border-gray-300 hover:text-gray-600 dark:border-gray-700 dark:text-gray-500 dark:hover:border-gray-600 dark:hover:text-gray-400'
                }`}
              >
                <AddColIcon className="h-4 w-4" />
                Spalte hinzufügen
              </button>
            </div>
          </div>

          <DragOverlay>
            {activeTask && (
              <div className="drag-overlay">
                <TaskCard
                  task={activeTask}
                  projectColor={projectColorMap[activeTask.project_id]}
                  projectName={projectMap[activeTask.project_id]?.name}
                  projectIconUrl={projectMap[activeTask.project_id]?.icon_url}
                  projectIconEmoji={projectMap[activeTask.project_id]?.icon_emoji}
                  showProjectIndicator
                  onClick={() => {}}
                />
              </div>
            )}
          </DragOverlay>
        </DndContext>
      </div>

      <TaskDetailDialog
        taskId={selectedTaskId}
        onClose={() => setSelectedTaskId(null)}
        onUpdated={fetchData}
        onOpenTask={setSelectedTaskId}
      />

      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={agendaBg}
        onSelect={(url) => handleBgSelect(url)}
      />
    </div>
  );
}

function InfoIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z" />
    </svg>
  );
}

function CameraIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Zm16.5-13.5h.008v.008h-.008V7.5Zm0 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" />
    </svg>
  );
}

function AddColIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
    </svg>
  );
}

function GearIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.431.992a7.723 7.723 0 0 1 0 .255c-.007.378.138.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  );
}
