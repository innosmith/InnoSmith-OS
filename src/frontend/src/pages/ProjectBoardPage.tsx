import { useState, useEffect, useCallback, useRef, type KeyboardEvent } from 'react';
import { useParams, useNavigate, useOutletContext } from 'react-router-dom';
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
import { useAuth } from '../contexts/AuthContext';
import { KanbanColumn } from '../components/KanbanColumn';
import { ColumnPager } from '../components/ColumnPager';
import { TaskCard } from '../components/TaskCard';
import { TaskDetailDialog } from '../components/TaskDetailDialog';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { ProjectIcon } from '../components/ProjectIcon';
import { LucideIconPicker, LucideIconByName } from '../components/LucideIconPicker';
import type { BoardData, TaskCard as TaskCardType, TaskCreatePayload } from '../types';

interface ProjectMember {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  invited_at: string | null;
}

export function ProjectBoardPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { isOwner } = useAuth();
  const { refreshSidebar } = useOutletContext<{ refreshSidebar: () => void }>();
  const [board, setBoard] = useState<BoardData | null>(null);
  const [activeTask, setActiveTask] = useState<TaskCardType | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [iconPickerOpen, setIconPickerOpen] = useState(false);
  const [iconMenuOpen, setIconMenuOpen] = useState(false);
  const [userAvatarUrl, setUserAvatarUrl] = useState<string | null>(null);
  const [showColCount, setShowColCount] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [teamUsers, setTeamUsers] = useState<{ id: string; email: string; display_name: string }[]>([]);
  const [selectedUserId, setSelectedUserId] = useState('');
  const [inviteLoading, setInviteLoading] = useState(false);
  const [inviteMsg, setInviteMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const settingsRef = useRef<HTMLDivElement>(null);
  const iconUploadRef = useRef<HTMLInputElement>(null);
  const renameRef = useRef<HTMLInputElement>(null);
  const renameActiveRef = useRef(false);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const collisionDetection: CollisionDetection = (args) => {
    const pointerCollisions = pointerWithin(args);
    if (pointerCollisions.length > 0) {
      const hasItem = pointerCollisions.some(
        (c) => !board?.columns.some((col) => col.id === c.id),
      );
      if (hasItem) {
        return pointerCollisions.filter(
          (c) => !board?.columns.some((col) => col.id === c.id),
        );
      }
      return pointerCollisions;
    }
    return closestCorners(args);
  };

  const fetchBoard = useCallback(async () => {
    if (!id) return;
    try {
      const [data, userData] = await Promise.all([
        api.get<BoardData>(`/api/projects/${id}/board`),
        api.get<{ avatar_url: string | null }>('/api/auth/me'),
      ]);
      setBoard(data);
      setUserAvatarUrl(userData.avatar_url);
      api.get<{ show_column_count: boolean | null }>('/api/settings')
        .then((s) => setShowColCount(s.show_column_count ?? false))
        .catch(() => {});
    } catch {
      /* handled by api client */
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    setLoading(true);
    fetchBoard();
  }, [fetchBoard]);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setSettingsOpen(false);
        setConfirmDelete(false);
        setIconPickerOpen(false);
        setIconMenuOpen(false);
      }
    };
    if (settingsOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [settingsOpen]);

  useEffect(() => {
    if (renaming && renameRef.current) {
      renameRef.current.focus();
      renameRef.current.select();
    }
  }, [renaming]);

  const handleCreateTask = useCallback(
    async (boardColumnId: string, title: string) => {
      if (!id) return;
      try {
        await api.post<TaskCardType>('/api/tasks', {
          title,
          project_id: id,
          board_column_id: boardColumnId,
        } satisfies TaskCreatePayload);
        fetchBoard();
      } catch {
        /* API-Fehler: Input trotzdem schliessen */
      }
    },
    [id, fetchBoard],
  );

  const findColumnByTaskId = (taskId: string) =>
    board?.columns.find((col) => col.tasks.some((t) => t.id === taskId));

  const isColumnId = (itemId: string) =>
    board?.columns.some((col) => col.id === itemId) ?? false;

  const handleDragStart = (event: DragStartEvent) => {
    const itemId = event.active.id as string;
    if (isColumnId(itemId)) return;
    if (!board) return;
    for (const col of board.columns) {
      const task = col.tasks.find((t) => t.id === itemId);
      if (task) {
        setActiveTask(task);
        break;
      }
    }
  };

  const handleDragOver = (event: DragOverEvent) => {
    const { active, over } = event;
    if (!over || !board) return;

    const activeId = active.id as string;
    const overId = over.id as string;

    if (isColumnId(activeId)) return;

    const sourceCol = findColumnByTaskId(activeId);
    const overCol =
      board.columns.find((col) => col.id === overId) ||
      findColumnByTaskId(overId);

    if (!sourceCol || !overCol || sourceCol.id === overCol.id) return;

    setBoard((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        columns: prev.columns.map((col) => {
          if (col.id === sourceCol.id) {
            return {
              ...col,
              tasks: col.tasks.filter((t) => t.id !== activeId),
            };
          }
          if (col.id === overCol.id) {
            const task = sourceCol.tasks.find((t) => t.id === activeId);
            if (!task) return col;
            const overIndex = col.tasks.findIndex((t) => t.id === overId);
            const newTasks = [...col.tasks];
            if (overIndex >= 0) {
              newTasks.splice(overIndex, 0, task);
            } else {
              newTasks.push(task);
            }
            return { ...col, tasks: newTasks };
          }
          return col;
        }),
      };
    });
  };

  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveTask(null);

    if (!over || !board) return;

    const activeId = active.id as string;
    const overId = over.id as string;

    if (isColumnId(activeId) && isColumnId(overId) && activeId !== overId) {
      const oldIndex = board.columns.findIndex((c) => c.id === activeId);
      const newIndex = board.columns.findIndex((c) => c.id === overId);
      if (oldIndex !== -1 && newIndex !== -1) {
        const reordered = arrayMove(board.columns, oldIndex, newIndex);
        setBoard((prev) => prev ? { ...prev, columns: reordered } : prev);
        for (let i = 0; i < reordered.length; i++) {
          api.patch(`/api/projects/${id}/columns/${reordered[i].id}`, { position: i + 1 }).catch(() => {});
        }
      }
      return;
    }

    if (activeId !== overId) {
      const col = findColumnByTaskId(activeId);
      if (col) {
        const oldIndex = col.tasks.findIndex((t) => t.id === activeId);
        const newIndex = col.tasks.findIndex((t) => t.id === overId);
        if (oldIndex !== -1 && newIndex !== -1) {
          setBoard((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              columns: prev.columns.map((c) =>
                c.id === col.id
                  ? { ...c, tasks: arrayMove(c.tasks, oldIndex, newIndex) }
                  : c,
              ),
            };
          });
        }
      }
    }

    const targetCol = findColumnByTaskId(activeId);
    if (!targetCol) return;
    const newPosition = targetCol.tasks.findIndex((t) => t.id === activeId);

    try {
      await api.patch(`/api/tasks/${activeId}`, {
        board_column_id: targetCol.id,
        board_position: newPosition,
      });
    } catch {
      fetchBoard();
    }
  };

  const fetchMembers = useCallback(async () => {
    if (!id) return;
    try {
      const data = await api.get<ProjectMember[]>(`/api/projects/${id}/members`);
      setMembers(data);
    } catch { /* leer */ }
  }, [id]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  if (!board) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500">
        Projekt nicht gefunden.
      </div>
    );
  }

  const hasBg = !!board.project.background_url;
  const bgUrl = board.project.background_url || '';
  const isGradient = bgUrl.startsWith('gradient:');
  const bgStyle = isGradient
    ? { background: bgUrl.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
      : undefined;

  const handleBgSelect = async (url: string | null) => {
    if (!id) return;
    await api.patch(`/api/projects/${id}`, { background_url: url });
    fetchBoard();
  };

  const handleArchive = async () => {
    if (!id) return;
    const newStatus = board?.project.status === 'archived' ? 'active' : 'archived';
    await api.patch(`/api/projects/${id}`, { status: newStatus });
    if (newStatus === 'archived') {
      navigate('/projects');
    } else {
      fetchBoard();
    }
    setSettingsOpen(false);
  };

  const startRename = () => {
    if (!board) return;
    setRenameValue(board.project.name);
    renameActiveRef.current = true;
    setRenaming(true);
    setSettingsOpen(false);
  };

  const handleRenameSubmit = async () => {
    if (!renameActiveRef.current) return;
    renameActiveRef.current = false;
    if (!id || !board) {
      setRenaming(false);
      return;
    }
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === board.project.name) {
      setRenaming(false);
      return;
    }
    try {
      await api.patch(`/api/projects/${id}`, { name: trimmed });
      setBoard({ ...board, project: { ...board.project, name: trimmed } });
      refreshSidebar();
    } catch {
      /* API-Fehler: Edit-Modus trotzdem schliessen */
    } finally {
      setRenaming(false);
    }
  };

  const handleRenameKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleRenameSubmit();
    }
    if (e.key === 'Escape') {
      renameActiveRef.current = false;
      setRenameValue(board?.project.name ?? '');
      setRenaming(false);
    }
  };

  const handleDelete = async () => {
    if (!id) return;
    await api.delete(`/api/projects/${id}`);
    navigate('/projects');
  };

  const openMembersPanel = () => {
    setSettingsOpen(false);
    setMembersOpen(true);
    fetchMembers();
    api.get<{ id: string; email: string; display_name: string }[]>('/api/auth/users')
      .then(setTeamUsers).catch(() => {});
  };

  const handleAddMember = async () => {
    if (!id || !selectedUserId) return;
    const user = teamUsers.find((u) => u.id === selectedUserId);
    if (!user) return;
    setInviteLoading(true);
    setInviteMsg(null);
    try {
      await api.post<{ email: string; temp_password: string }>('/api/auth/invite', {
        email: user.email,
        project_id: id,
      });
      setInviteMsg({ type: 'ok', text: `${user.display_name} wurde dem Projekt hinzugefügt.` });
      setSelectedUserId('');
      fetchMembers();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Hinzufügen fehlgeschlagen';
      setInviteMsg({ type: 'err', text: msg });
    } finally {
      setInviteLoading(false);
    }
  };

  const handleRemoveMember = async (userId: string) => {
    if (!id) return;
    try {
      await api.delete(`/api/projects/${id}/members/${userId}`);
      fetchMembers();
    } catch { /* leer */ }
  };

  return (
    <div className="relative flex h-full flex-col" style={bgStyle}>
      {hasBg && !isGradient && (
        <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/30" />
      )}
      {isGradient && (
        <div className="pointer-events-none absolute inset-0 bg-black/5 dark:bg-black/20" />
      )}

      <div
        className={`relative z-30 border-b px-4 py-4 sm:px-6 ${
          hasBg
            ? 'border-white/10 bg-black/20 text-white backdrop-blur-sm'
            : 'border-gray-200 dark:border-gray-800'
        }`}
      >
        <div className="flex items-center gap-3">
          {board.project.icon_emoji ? (
            <LucideIconByName
              name={board.project.icon_emoji}
              className={`h-6 w-6 shrink-0 ${hasBg ? 'text-white' : 'text-gray-600 dark:text-gray-300'}`}
            />
          ) : (
            <ProjectIcon
              iconUrl={board.project.icon_url}
              iconEmoji={null}
              color={board.project.color}
              size={24}
            />
          )}
          {renaming ? (
            <input
              ref={renameRef}
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={handleRenameKeyDown}
              onBlur={handleRenameSubmit}
              className={`min-w-0 flex-1 rounded-lg border border-gray-300 bg-white/90 px-2 py-1 text-xl font-bold text-gray-900 outline-none focus:border-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white ${
                hasBg ? 'shadow-sm' : ''
              }`}
            />
          ) : (
            <h1
              className={`hidden text-xl font-bold lg:block ${
                hasBg ? 'text-white' : 'text-gray-900 dark:text-white'
              } ${isOwner ? 'cursor-pointer' : ''}`}
              onDoubleClick={() => {
                if (isOwner) startRename();
              }}
              title={isOwner ? 'Doppelklick zum Umbenennen' : undefined}
            >
              {board.project.name}
            </h1>
          )}
          <div className="ml-auto flex items-center gap-1">
            {isOwner && <div className="relative" ref={settingsRef}>
              <button
                onClick={() => { setSettingsOpen(!settingsOpen); setConfirmDelete(false); setIconPickerOpen(false); setIconMenuOpen(false); }}
                className={`rounded-lg p-2 transition-colors ${
                  hasBg
                    ? 'text-white/70 hover:bg-white/10 hover:text-white'
                    : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'
                }`}
                title="Projekteinstellungen"
              >
                <GearIcon className="h-5 w-5" />
              </button>
              {settingsOpen && (
                <div className="absolute right-0 top-10 z-[100] w-64 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  <button
                    onClick={startRename}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <PencilIcon className="h-4 w-4" />
                    Umbenennen
                  </button>
                  <button
                    onClick={() => { setIconMenuOpen(!iconMenuOpen); setSettingsOpen(false); }}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <EmojiIcon className="h-4 w-4" />
                    Projekt-Icon
                  </button>
                  <button
                    onClick={() => { setBgPickerOpen(true); setSettingsOpen(false); }}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <CameraIcon className="h-4 w-4" />
                    Hintergrundbild
                  </button>
                  {isOwner && (
                    <button
                      onClick={openMembersPanel}
                      className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                    >
                      <UsersIcon className="h-4 w-4" />
                      Mitglieder verwalten
                    </button>
                  )}
                  <button
                    onClick={handleArchive}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <ArchiveIcon className="h-4 w-4" />
                    {board.project.status === 'archived' ? 'Reaktivieren' : 'Archivieren'}
                  </button>
                  <div className="my-1 border-t border-gray-100 dark:border-gray-800" />
                  {!confirmDelete ? (
                    <button
                      onClick={() => setConfirmDelete(true)}
                      className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                    >
                      <TrashIcon className="h-4 w-4" />
                      Projekt löschen
                    </button>
                  ) : (
                    <div className="px-4 py-2">
                      <p className="mb-2 text-xs text-red-600 dark:text-red-400">Alle Tasks werden unwiderruflich gelöscht!</p>
                      <div className="flex gap-2">
                        <button
                          onClick={handleDelete}
                          className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700"
                        >
                          Löschen
                        </button>
                        <button
                          onClick={() => setConfirmDelete(false)}
                          className="rounded-lg px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
                        >
                          Abbrechen
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
              {iconMenuOpen && (
                <div className="absolute right-0 top-10 z-30 w-56 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  <button
                    onClick={() => { iconUploadRef.current?.click(); }}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <CameraIcon className="h-4 w-4" />
                    Bild hochladen
                  </button>
                  <button
                    onClick={() => { setIconPickerOpen(true); setIconMenuOpen(false); }}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <EmojiIcon className="h-4 w-4" />
                    Icon-Bibliothek
                  </button>
                  {(board.project.icon_url || board.project.icon_emoji) && (
                    <>
                      <div className="my-1 border-t border-gray-100 dark:border-gray-800" />
                      <button
                        onClick={async () => {
                          if (!id) return;
                          await api.patch(`/api/projects/${id}`, { icon_emoji: null, icon_url: null });
                          fetchBoard();
                          refreshSidebar();
                          setIconMenuOpen(false);
                        }}
                        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                      >
                        <TrashIcon className="h-4 w-4" />
                        Icon entfernen
                      </button>
                    </>
                  )}
                  <input
                    ref={iconUploadRef}
                    type="file"
                    accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
                    className="hidden"
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file || !id) return;
                      const form = new FormData();
                      form.append('file', file);
                      try {
                        const token = localStorage.getItem('taskpilot_token');
                        const res = await fetch('/api/uploads/icons', {
                          method: 'POST',
                          headers: { Authorization: `Bearer ${token}` },
                          body: form,
                        });
                        if (!res.ok) {
                          const err = await res.text().catch(() => '');
                          alert(`Icon-Upload fehlgeschlagen: ${res.status} ${err || res.statusText}`);
                          return;
                        }
                        const { url } = await res.json();
                        await api.patch(`/api/projects/${id}`, { icon_url: url, icon_emoji: null });
                        fetchBoard();
                        refreshSidebar();
                        setIconMenuOpen(false);
                      } catch (e) {
                        alert(`Icon-Upload fehlgeschlagen: ${e instanceof Error ? e.message : 'Unbekannter Fehler'}`);
                      }
                    }}
                  />
                </div>
              )}
              {iconPickerOpen && (
                <LucideIconPicker
                  currentIcon={board.project.icon_emoji}
                  onSelect={async (iconName) => {
                    if (!id) return;
                    if (iconName) {
                      await api.patch(`/api/projects/${id}`, { icon_emoji: iconName, icon_url: null });
                    } else {
                      await api.patch(`/api/projects/${id}`, { icon_emoji: null, icon_url: null });
                    }
                    fetchBoard();
                    refreshSidebar();
                    setIconPickerOpen(false);
                  }}
                  onClose={() => setIconPickerOpen(false)}
                />
              )}
            </div>}
          </div>
        </div>
      </div>

      <div
        data-board-scroller
        className={`relative z-10 flex-1 overflow-x-auto scroll-px-4 p-4 sm:p-6 ${
          activeTask ? '' : 'snap-x snap-proximity sm:snap-none'
        }`}
      >
        <DndContext
          sensors={sensors}
          collisionDetection={collisionDetection}
          onDragStart={handleDragStart}
          onDragOver={handleDragOver}
          onDragEnd={handleDragEnd}
        >
          <ColumnPager columns={board.columns} hasBg={hasBg} />
          <div
            className="flex gap-4"
            style={{ minHeight: 'calc(100dvh - 180px)' }}
          >
            <SortableContext
              items={board.columns.map((col) => col.id)}
              strategy={horizontalListSortingStrategy}
            >
              {board.columns.map((col) => (
                <KanbanColumn
                  key={col.id}
                  id={col.id}
                  title={col.name}
                  tasks={col.tasks}
                  showColumnCount={showColCount}
                  hasBg={hasBg}
                  userAvatarUrl={userAvatarUrl}
                  columnColor={col.color}
                  columnIcon={col.icon_emoji}
                  onTaskClick={(task) => setSelectedTaskId(task.id)}
                  onCreateTask={handleCreateTask}
                  onRenameColumn={isOwner ? async (colId, name) => {
                    await api.patch(`/api/projects/${id}/columns/${colId}`, { name });
                    fetchBoard();
                  } : undefined}
                  onUpdateColumn={isOwner ? async (colId, updates) => {
                    await api.patch(`/api/projects/${id}/columns/${colId}`, updates);
                    fetchBoard();
                  } : undefined}
                  onDeleteColumn={isOwner ? async (colId) => {
                    try {
                      await api.delete(`/api/projects/${id}/columns/${colId}`);
                      fetchBoard();
                    } catch (err) {
                      const e = err as { status?: number };
                      if (e.status === 409) {
                        alert('Die letzte Spalte eines Projekts kann nicht gelöscht werden.');
                      } else {
                        alert('Spalte konnte nicht gelöscht werden. Bitte versuche es erneut.');
                      }
                    }
                  } : undefined}
                  onArchiveTask={async (taskId) => {
                    await api.patch(`/api/tasks/${taskId}`, { is_completed: true });
                    fetchBoard();
                  }}
                />
              ))}
            </SortableContext>
            {isOwner && (
              <div className="flex w-[calc(100vw-2rem)] shrink-0 snap-start flex-col sm:w-72">
                <button
                  onClick={async () => {
                    if (!id) return;
                    await api.post(`/api/projects/${id}/columns`, { name: 'Neue Spalte' });
                    fetchBoard();
                  }}
                  className={`flex items-center justify-center gap-2 rounded-xl border-2 border-dashed px-4 py-3 text-sm font-medium transition-colors ${
                    hasBg
                      ? 'border-white/20 text-white/60 hover:bg-white/10 hover:text-white'
                      : 'border-gray-200 text-gray-400 hover:border-gray-300 hover:text-gray-600 dark:border-gray-700 dark:text-gray-500 dark:hover:border-gray-600 dark:hover:text-gray-400'
                  }`}
                >
                  <AddColumnIcon className="h-4 w-4" />
                  Spalte hinzufügen
                </button>
              </div>
            )}
          </div>

          <DragOverlay>
            {activeTask && (
              <div className="drag-overlay">
                <TaskCard task={activeTask} onClick={() => {}} />
              </div>
            )}
          </DragOverlay>
        </DndContext>
      </div>

      <TaskDetailDialog
        taskId={selectedTaskId}
        onClose={() => setSelectedTaskId(null)}
        onUpdated={fetchBoard}
        onOpenTask={setSelectedTaskId}
      />

      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={board.project.background_url}
        onSelect={(url) => handleBgSelect(url)}
      />

      {membersOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm modal-safe">
          <div className="w-full max-w-lg rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-gray-700 dark:bg-gray-900">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                Mitglieder verwalten
              </h2>
              <button
                onClick={() => { setMembersOpen(false); setInviteMsg(null); }}
                className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
              >
                <CloseIcon className="h-5 w-5" />
              </button>
            </div>

            <div className="mb-4 space-y-2">
              {members.length === 0 ? (
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Noch keine Mitglieder in diesem Projekt.
                </p>
              ) : (
                members.map((m) => (
                  <div
                    key={m.user_id}
                    className="flex items-center justify-between rounded-lg border border-gray-100 px-3 py-2 dark:border-gray-800"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
                        {m.display_name}
                      </p>
                      <p className="truncate text-xs text-gray-500 dark:text-gray-400">
                        {m.email}
                      </p>
                    </div>
                    <span className="mx-2 shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                      {m.role}
                    </span>
                    <button
                      onClick={() => handleRemoveMember(m.user_id)}
                      className="shrink-0 rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950 dark:hover:text-red-400"
                      title="Mitglied entfernen"
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </div>
                ))
              )}
            </div>

            <div className="border-t border-gray-100 pt-4 dark:border-gray-800">
              <p className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">
                Mitglied hinzufügen
              </p>
              {(() => {
                const memberIds = new Set(members.map((m) => m.user_id));
                const available = teamUsers.filter((u) => !memberIds.has(u.id));
                if (available.length === 0) {
                  return (
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      Alle Team-Mitglieder sind bereits in diesem Projekt.
                    </p>
                  );
                }
                return (
                  <div className="flex gap-2">
                    <select
                      value={selectedUserId}
                      onChange={(e) => setSelectedUserId(e.target.value)}
                      className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      <option value="">Person auswählen…</option>
                      {available.map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.display_name} ({u.email})
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={handleAddMember}
                      disabled={inviteLoading || !selectedUserId}
                      className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-50"
                    >
                      {inviteLoading ? 'Lädt…' : 'Hinzufügen'}
                    </button>
                  </div>
                );
              })()}
              {inviteMsg && (
                <div className={`mt-3 rounded-lg p-3 text-sm ${inviteMsg.type === 'ok' ? 'border border-green-200 bg-green-50 text-green-800 dark:border-green-800 dark:bg-green-950 dark:text-green-300' : 'border border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-300'}`}>
                  <p>{inviteMsg.text}</p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function CameraIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Zm16.5-13.5h.008v.008h-.008V7.5Zm0 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" />
    </svg>
  );
}

function PencilIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10" />
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

function ArchiveIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m20.25 7.5-.625 10.632a2.25 2.25 0 0 1-2.247 2.118H6.622a2.25 2.25 0 0 1-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125Z" />
    </svg>
  );
}

function TrashIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
    </svg>
  );
}

function EmojiIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.182 15.182a4.5 4.5 0 0 1-6.364 0M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9.75 9.75c0 .414-.168.75-.375.75S9 10.164 9 9.75 9.168 9 9.375 9s.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Zm5.625 0c0 .414-.168.75-.375.75s-.375-.336-.375-.75.168-.75.375-.75.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Z" />
    </svg>
  );
}

function AddColumnIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
    </svg>
  );
}

function UsersIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 19.128a9.38 9.38 0 0 0 2.625.372 9.337 9.337 0 0 0 4.121-.952 4.125 4.125 0 0 0-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 0 1 8.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0 1 11.964-3.07M12 6.375a3.375 3.375 0 1 1-6.75 0 3.375 3.375 0 0 1 6.75 0Zm8.25 2.25a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z" />
    </svg>
  );
}

function CloseIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
    </svg>
  );
}
