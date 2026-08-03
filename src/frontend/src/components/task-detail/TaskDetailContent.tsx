import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { marked } from 'marked';
import TurndownService from 'turndown';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { api } from '../../api/client';
import type { TaskDetail, ChecklistItem, TaskUpdatePayload } from '../../types';
import { RichTextEditor } from '../RichTextEditor';
import { EmailThreadPanel } from '../EmailThreadPanel';
import { SectionLabel, DescIcon, ChecklistIcon, GripIcon, TrashIcon, MailIcon } from './shared';

/** Wandelt TipTap-HTML zurück zu Markdown (Speicherformat bleibt Markdown-First). */
const turndown = new TurndownService({
  headingStyle: 'atx',
  bulletListMarker: '-',
  hr: '---',
  codeBlockStyle: 'fenced',
});

interface TaskDetailContentProps {
  task: TaskDetail;
  taskId: string;
  onUpdated: () => void;
  updateTask: (payload: TaskUpdatePayload) => Promise<void>;
  refreshTask: () => Promise<void>;
  onOpenTask?: (taskId: string) => void;
}

function ArrowUpRightIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
    </svg>
  );
}

function renderTextWithLinks(text: string) {
  const urlPattern = /(https?:\/\/[^\s]+)/g;
  const parts = text.split(urlPattern);
  if (parts.length === 1) return text;
  return parts.map((part, i) =>
    urlPattern.test(part) ? (
      <a
        key={i}
        href={part}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="text-indigo-500 hover:underline dark:text-indigo-400"
      >
        {part}
      </a>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

interface SortableChecklistItemProps {
  item: ChecklistItem;
  isEditing: boolean;
  editText: string;
  onToggle: () => void;
  onStartEdit: () => void;
  onEditChange: (v: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onDelete: () => void;
  onConvertToTask?: () => void;
}

function SortableChecklistItem({
  item,
  isEditing,
  editText,
  onToggle,
  onStartEdit,
  onEditChange,
  onSaveEdit,
  onCancelEdit,
  onDelete,
  onConvertToTask,
}: SortableChecklistItemProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: item.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const editRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isEditing && editRef.current) {
      editRef.current.focus();
      editRef.current.select();
    }
  }, [isEditing]);

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="group flex min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/50"
    >
      <button
        type="button"
        className="cursor-grab touch-none text-gray-300 hover:text-gray-500 dark:text-gray-600 dark:hover:text-gray-400"
        {...attributes}
        {...listeners}
      >
        <GripIcon className="h-4 w-4" />
      </button>

      <label className="flex cursor-pointer items-center">
        <input
          type="checkbox"
          checked={item.is_checked}
          onChange={onToggle}
          className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 dark:border-gray-600 dark:bg-gray-700"
        />
      </label>

      {isEditing ? (
        <input
          ref={editRef}
          type="text"
          value={editText}
          onChange={(e) => onEditChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSaveEdit();
            if (e.key === 'Escape') onCancelEdit();
          }}
          onBlur={onSaveEdit}
          className="min-w-0 flex-1 rounded border border-indigo-300 bg-white px-2 py-0.5 text-sm text-gray-800 outline-none focus:ring-1 focus:ring-indigo-400 dark:border-indigo-600 dark:bg-gray-800 dark:text-gray-200"
        />
      ) : (
        <span
          onClick={onStartEdit}
          className={`min-w-0 flex-1 cursor-text break-words text-sm ${
            item.is_checked
              ? 'text-gray-400 line-through dark:text-gray-500'
              : 'text-gray-700 dark:text-gray-300'
          }`}
        >
          {renderTextWithLinks(item.text)}
        </span>
      )}

      {!item.is_checked && onConvertToTask && (
        <button
          type="button"
          onClick={onConvertToTask}
          title="Als Task erstellen"
          className="shrink-0 text-gray-300 opacity-0 transition-opacity hover:text-indigo-500 group-hover:opacity-100 dark:text-gray-600 dark:hover:text-indigo-400"
        >
          <ArrowUpRightIcon className="h-4 w-4" />
        </button>
      )}

      <button
        type="button"
        onClick={onDelete}
        className="shrink-0 text-gray-300 opacity-0 transition-opacity hover:text-red-500 group-hover:opacity-100 dark:text-gray-600 dark:hover:text-red-400"
      >
        <TrashIcon className="h-4 w-4" />
      </button>
    </div>
  );
}

export default function TaskDetailContent({
  task,
  taskId,
  onUpdated,
  updateTask,
  refreshTask,
  onOpenTask,
}: TaskDetailContentProps) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState(task.title);
  const titleRef = useRef<HTMLInputElement>(null);

  const [editingDesc, setEditingDesc] = useState(false);
  const [descValue, setDescValue] = useState('');
  const descDirtyRef = useRef(false);

  const [editingItemId, setEditingItemId] = useState<string | null>(null);
  const [editItemText, setEditItemText] = useState('');
  const [newChecklistText, setNewChecklistText] = useState('');
  const [convertedTask, setConvertedTask] = useState<{ id: string; title: string } | null>(null);
  const convertTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Beschreibungen sind Markdown (bleach strippt HTML beim Speichern). Die frühere
  // HTML-Heuristik traf fälschlich auf «Von: Name <addr@domain>» im Quell-Block.
  const descriptionHtml = useMemo(
    () => (task.description ? (marked.parse(task.description, { breaks: true }) as string) : ''),
    [task.description],
  );

  useEffect(() => {
    setTitleValue(task.title);
  }, [task.title]);

  useEffect(() => {
    if (!editingDesc) {
      setDescValue(descriptionHtml);
      descDirtyRef.current = false;
    }
  }, [descriptionHtml, editingDesc]);

  useEffect(() => {
    if (editingTitle && titleRef.current) {
      titleRef.current.focus();
      titleRef.current.select();
    }
  }, [editingTitle]);

  // Betreff und Absender als eine Kopfzeile -- zugeklappt ist das alles, was es an
  // Herkunftsinfo braucht; früher stand dasselbe zusätzlich in der Beschreibung.
  const sourceEmailSubtitle = useMemo(() => {
    const parts = [task.source_email_subject, task.source_email_from && `von ${task.source_email_from}`];
    return parts.filter(Boolean).join(' · ');
  }, [task.source_email_subject, task.source_email_from]);

  const outlookButton = task.source_email_web_link ? (
    <a
      href={task.source_email_web_link}
      target="_blank"
      rel="noopener noreferrer"
      title="Original mit Anhängen in Outlook öffnen"
      className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-indigo-600 transition-colors hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950/40"
    >
      Outlook <ArrowUpRightIcon className="h-3 w-3" />
    </a>
  ) : undefined;

  const saveTitle = useCallback(async () => {
    const trimmed = titleValue.trim();
    if (trimmed && trimmed !== task.title) {
      await updateTask({ title: trimmed });
    } else {
      setTitleValue(task.title);
    }
    setEditingTitle(false);
  }, [titleValue, task.title, updateTask]);

  const saveDescriptionAndClose = useCallback(async () => {
    if (descDirtyRef.current) {
      // TipTap liefert HTML; Backend speichert Markdown → Round-Trip via turndown.
      const markdown = turndown.turndown(descValue);
      await updateTask({ description: markdown });
      descDirtyRef.current = false;
    }
    setEditingDesc(false);
  }, [descValue, updateTask]);

  const checklist = [...(task.checklist_items ?? [])].sort((a, b) => a.position - b.position);
  const doneCount = checklist.filter((i) => i.is_checked).length;
  const totalCount = checklist.length;
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;

  const toggleChecklistItem = useCallback(
    async (item: ChecklistItem) => {
      await api.patch(`/api/tasks/${taskId}/checklist/${item.id}`, {
        is_checked: !item.is_checked,
      });
      await refreshTask();
      onUpdated();
    },
    [taskId, refreshTask, onUpdated],
  );

  const startEditItem = useCallback((item: ChecklistItem) => {
    setEditingItemId(item.id);
    setEditItemText(item.text);
  }, []);

  const saveEditItem = useCallback(async () => {
    if (!editingItemId) return;
    const trimmed = editItemText.trim();
    if (trimmed) {
      await api.patch(`/api/tasks/${taskId}/checklist/${editingItemId}`, {
        text: trimmed,
      });
      await refreshTask();
      onUpdated();
    }
    setEditingItemId(null);
    setEditItemText('');
  }, [editingItemId, editItemText, taskId, refreshTask, onUpdated]);

  const cancelEditItem = useCallback(() => {
    setEditingItemId(null);
    setEditItemText('');
  }, []);

  const deleteChecklistItem = useCallback(
    async (itemId: string) => {
      await api.delete(`/api/tasks/${taskId}/checklist/${itemId}`);
      await refreshTask();
      onUpdated();
    },
    [taskId, refreshTask, onUpdated],
  );

  const addChecklistItem = useCallback(async () => {
    const trimmed = newChecklistText.trim();
    if (!trimmed) return;
    await api.post(`/api/tasks/${taskId}/checklist`, { text: trimmed });
    setNewChecklistText('');
    await refreshTask();
    onUpdated();
  }, [newChecklistText, taskId, refreshTask, onUpdated]);

  const convertChecklistToTask = useCallback(
    async (item: ChecklistItem) => {
      try {
        const newTask = await api.post<TaskDetail>('/api/tasks', {
          title: item.text,
          project_id: task.project_id,
          board_column_id: task.board_column_id,
        });
        await api.patch(`/api/tasks/${taskId}/checklist/${item.id}`, {
          is_checked: true,
        });
        await refreshTask();
        onUpdated();

        if (convertTimerRef.current) clearTimeout(convertTimerRef.current);
        setConvertedTask({ id: newTask.id, title: item.text });
        convertTimerRef.current = setTimeout(() => setConvertedTask(null), 5000);
      } catch { /* Fehler ignorieren */ }
    },
    [task.project_id, task.board_column_id, taskId, refreshTask, onUpdated],
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = useCallback(
    async (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;

      const oldIndex = checklist.findIndex((i) => i.id === active.id);
      const newIndex = checklist.findIndex((i) => i.id === over.id);
      if (oldIndex === -1 || newIndex === -1) return;

      // Komplette neue Reihenfolge berechnen und allen Eintraegen eine
      // eindeutige, fortlaufende Position zuweisen. Nur einen Wert zu kopieren
      // wuerde bei benachbarten Verschiebungen doppelte Positionen erzeugen.
      const reordered = arrayMove(checklist, oldIndex, newIndex);
      await Promise.all(
        reordered
          .map((item, index) => ({ item, index }))
          .filter(({ item, index }) => item.position !== index)
          .map(({ item, index }) =>
            api.patch(`/api/tasks/${taskId}/checklist/${item.id}`, { position: index }),
          ),
      );
      await refreshTask();
      onUpdated();
    },
    [checklist, taskId, refreshTask, onUpdated],
  );

  return (
    <div className="space-y-6">
      {/* Titel */}
      <div>
        {editingTitle ? (
          <input
            ref={titleRef}
            type="text"
            value={titleValue}
            onChange={(e) => setTitleValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveTitle();
              if (e.key === 'Escape') {
                setTitleValue(task.title);
                setEditingTitle(false);
              }
            }}
            onBlur={saveTitle}
            className="w-full rounded-lg border border-indigo-300 bg-white px-3 py-1.5 text-lg font-bold text-gray-900 outline-none focus:ring-2 focus:ring-indigo-400 dark:border-indigo-600 dark:bg-gray-800 dark:text-white"
          />
        ) : (
          <h2
            onClick={() => setEditingTitle(true)}
            className="cursor-text break-words rounded-lg px-3 py-1.5 text-lg font-bold text-gray-900 transition-colors hover:bg-gray-50 dark:text-white dark:hover:bg-gray-800/50"
          >
            {task.title}
          </h2>
        )}
      </div>

      {/* Beschreibung */}
      <div>
        <SectionLabel icon={DescIcon} text="Beschreibung" />
        {editingDesc ? (
          <div
            onBlur={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node)) return;
              saveDescriptionAndClose();
            }}
          >
            <RichTextEditor
              content={descValue}
              onChange={(html) => { setDescValue(html); descDirtyRef.current = true; }}
              editable
              placeholder="Beschreibung eingeben…"
              minHeight="80px"
            />
          </div>
        ) : (
          <div
            onClick={(e) => {
              // Links in der Leseansicht öffnen, statt den Editor zu starten.
              if ((e.target as HTMLElement).closest('a')) return;
              setEditingDesc(true);
            }}
            className="cursor-text rounded-lg transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/30"
          >
            {task.description ? (
              <div className="prose prose-sm max-w-none text-gray-700 dark:prose-invert dark:text-gray-300">
                <RichTextEditor
                  content={descriptionHtml}
                  editable={false}
                  openLinksOnClick
                  minHeight="40px"
                />
              </div>
            ) : (
              <p className="rounded-lg px-3 py-3 text-sm text-gray-400 dark:text-gray-500">
                Beschreibung eingeben…
              </p>
            )}
          </div>
        )}
      </div>

      {/* Quell-E-Mail: hier statt in der Sidebar, weil die Originalmail in der
          breiten Spalte lesbar bleibt. Zugeklappt ist es eine einzige Zeile. */}
      {task.email_message_id && (
        <div>
          {task.email_conversation_id ? (
            <EmailThreadPanel
              conversationId={task.email_conversation_id}
              title="Quell-E-Mail"
              subtitle={sourceEmailSubtitle}
              action={outlookButton}
              roomy
            />
          ) : (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 dark:border-gray-700">
              <MailIcon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
              <span className="min-w-0 flex-1 truncate text-xs text-gray-500 dark:text-gray-400">
                {sourceEmailSubtitle || 'Quell-E-Mail'}
              </span>
              {outlookButton}
            </div>
          )}
        </div>
      )}

      {/* Checkliste */}
      <div>
        <SectionLabel
          icon={ChecklistIcon}
          text="Checkliste"
          action={totalCount > 0 ? (
            <span className="text-[10px] font-normal text-gray-400 dark:text-gray-500">
              {doneCount}/{totalCount}
            </span>
          ) : undefined}
        />

        {totalCount > 0 && (
          <div className="mb-3">
            <div className="mb-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
              <span>
                {doneCount} / {totalCount} erledigt
              </span>
              <span>{progressPct}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
              <div
                className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500 transition-all duration-300"
                style={{ width: `${progressPct}%` }}
              />
            </div>
          </div>
        )}

        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={checklist.map((i) => i.id)}
            strategy={verticalListSortingStrategy}
          >
            <div className="space-y-0.5">
              {checklist.map((item) => (
                <SortableChecklistItem
                  key={item.id}
                  item={item}
                  isEditing={editingItemId === item.id}
                  editText={editItemText}
                  onToggle={() => toggleChecklistItem(item)}
                  onStartEdit={() => startEditItem(item)}
                  onEditChange={setEditItemText}
                  onSaveEdit={saveEditItem}
                  onCancelEdit={cancelEditItem}
                  onDelete={() => deleteChecklistItem(item.id)}
                  onConvertToTask={() => convertChecklistToTask(item)}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>

        <div className="mt-2">
          <input
            type="text"
            value={newChecklistText}
            onChange={(e) => setNewChecklistText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') addChecklistItem();
            }}
            onBlur={addChecklistItem}
            placeholder="Neuer Eintrag…"
            className="w-full rounded-lg border border-gray-200 bg-transparent px-3 py-1.5 text-sm text-gray-700 outline-none placeholder:text-gray-400 focus:border-indigo-300 focus:ring-1 focus:ring-indigo-300 dark:border-gray-700 dark:text-gray-300 dark:placeholder:text-gray-500 dark:focus:border-indigo-600"
          />
        </div>

        {convertedTask && (
          <div className="mt-2 flex items-center gap-2 rounded-lg border border-indigo-200 bg-indigo-50/60 px-3 py-2 dark:border-indigo-800 dark:bg-indigo-950/30">
            <ArrowUpRightIcon className="h-3.5 w-3.5 shrink-0 text-indigo-500" />
            <span className="flex-1 truncate text-xs text-indigo-700 dark:text-indigo-300">
              «{convertedTask.title}» als Task erstellt
            </span>
            {onOpenTask && (
              <button
                type="button"
                onClick={() => { onOpenTask(convertedTask.id); setConvertedTask(null); }}
                className="shrink-0 rounded-md bg-indigo-600 px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-indigo-700"
              >
                Öffnen
              </button>
            )}
            <button
              type="button"
              onClick={() => setConvertedTask(null)}
              className="shrink-0 text-indigo-400 hover:text-indigo-600 dark:hover:text-indigo-300"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
