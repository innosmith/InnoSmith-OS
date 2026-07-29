import { useState, useRef, useEffect, type KeyboardEvent } from 'react';
import { useDroppable } from '@dnd-kit/core';
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Palette, Star } from 'lucide-react';
import { LucideIconPicker, LucideIconByName } from './LucideIconPicker';
import { TaskCard } from './TaskCard';
import type { TaskCard as TaskCardType, Project } from '../types';

const COLUMN_COLORS: { key: string; label: string; swatch: string; bg: string; bgDark: string }[] = [
  { key: '', label: 'Standard', swatch: '', bg: '', bgDark: '' },
  { key: 'rose', label: 'Rose', swatch: '#fecdd3', bg: '#fecdd3', bgDark: 'rgba(76,5,25,0.5)' },
  { key: 'orange', label: 'Orange', swatch: '#fed7aa', bg: '#fed7aa', bgDark: 'rgba(67,20,7,0.5)' },
  { key: 'amber', label: 'Amber', swatch: '#fde68a', bg: '#fde68a', bgDark: 'rgba(69,26,3,0.5)' },
  { key: 'lime', label: 'Lime', swatch: '#d9f99d', bg: '#d9f99d', bgDark: 'rgba(26,46,5,0.5)' },
  { key: 'emerald', label: 'Emerald', swatch: '#a7f3d0', bg: '#a7f3d0', bgDark: 'rgba(6,78,59,0.5)' },
  { key: 'teal', label: 'Teal', swatch: '#99f6e4', bg: '#99f6e4', bgDark: 'rgba(19,78,74,0.5)' },
  { key: 'sky', label: 'Sky', swatch: '#bae6fd', bg: '#bae6fd', bgDark: 'rgba(12,74,110,0.5)' },
  { key: 'blue', label: 'Blue', swatch: '#bfdbfe', bg: '#bfdbfe', bgDark: 'rgba(30,58,138,0.5)' },
  { key: 'indigo', label: 'Indigo', swatch: '#c7d2fe', bg: '#c7d2fe', bgDark: 'rgba(49,46,129,0.5)' },
  { key: 'violet', label: 'Violet', swatch: '#ddd6fe', bg: '#ddd6fe', bgDark: 'rgba(76,29,149,0.5)' },
  { key: 'pink', label: 'Pink', swatch: '#fbcfe8', bg: '#fbcfe8', bgDark: 'rgba(112,26,117,0.5)' },
];

interface KanbanColumnProps {
  id: string;
  title: string;
  tasks: TaskCardType[];
  projectColorMap?: Record<string, string>;
  projectMap?: Record<string, Project>;
  showProjectIndicator?: boolean;
  showColumnCount?: boolean;
  hasBg?: boolean;
  userAvatarUrl?: string | null;
  columnColor?: string | null;
  columnIcon?: string | null;
  onTaskClick: (task: TaskCardType) => void;
  onCreateTask?: (columnId: string, title: string) => Promise<void>;
  onRenameColumn?: (columnId: string, name: string) => Promise<void>;
  onUpdateColumn?: (columnId: string, updates: { color?: string | null; icon_emoji?: string | null }) => Promise<void>;
  onDeleteColumn?: (columnId: string) => Promise<void>;
  onArchiveTask?: (taskId: string) => Promise<void>;
}

export function KanbanColumn({
  id,
  title,
  tasks,
  projectColorMap,
  projectMap,
  showProjectIndicator = false,
  showColumnCount = false,
  hasBg = false,
  userAvatarUrl,
  columnColor,
  columnIcon,
  onTaskClick,
  onCreateTask,
  onRenameColumn,
  onUpdateColumn,
  onDeleteColumn,
  onArchiveTask,
}: KanbanColumnProps) {
  const { setNodeRef: setDropRef, isOver } = useDroppable({ id });

  const {
    attributes,
    listeners,
    setNodeRef: setSortRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id, data: { type: 'column' } });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  const [adding, setAdding] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(title);
  const [menuOpen, setMenuOpen] = useState(false);
  const [colorPickerOpen, setColorPickerOpen] = useState(false);
  const [iconPickerOpen, setIconPickerOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const editRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing && editRef.current) {
      editRef.current.focus();
      editRef.current.select();
    }
  }, [editing]);

  useEffect(() => {
    if (!menuOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setColorPickerOpen(false);
        setIconPickerOpen(false);
        setConfirmDelete(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [menuOpen]);

  const handleSubmit = async () => {
    const trimmed = newTitle.trim();
    if (!trimmed || !onCreateTask) return;
    await onCreateTask(id, trimmed);
    setNewTitle('');
    setAdding(false);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSubmit();
    if (e.key === 'Escape') {
      setNewTitle('');
      setAdding(false);
    }
  };

  const handleRenameSubmit = async () => {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== title && onRenameColumn) {
      await onRenameColumn(id, trimmed);
    }
    setEditing(false);
  };

  const handleRenameKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleRenameSubmit();
    if (e.key === 'Escape') {
      setEditValue(title);
      setEditing(false);
    }
  };

  const isDark = typeof window !== 'undefined' && document.documentElement.classList.contains('dark');
  const colorEntry = columnColor ? COLUMN_COLORS.find((c) => c.key === columnColor) : null;
  const headerBgColor = colorEntry
    ? (isDark ? colorEntry.bgDark : colorEntry.bg)
    : undefined;

  const hasCustomColor = !!columnColor && !!colorEntry;

  const headerClasses = hasCustomColor
    ? ''
    : hasBg
      ? 'bg-white/15 backdrop-blur-sm'
      : '';

  const textClasses = hasCustomColor
    ? (isDark ? 'text-gray-100' : 'text-gray-800')
    : hasBg
      ? 'text-white'
      : 'text-gray-600 dark:text-gray-400';

  const badgeClasses = hasCustomColor
    ? (isDark ? 'bg-white/20 text-gray-100' : 'bg-black/10 text-gray-700')
    : hasBg
      ? 'bg-white/20 text-white'
      : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';

  const btnClasses = hasCustomColor
    ? (isDark ? 'text-gray-200 hover:bg-white/15' : 'text-gray-600 hover:bg-black/10')
    : hasBg
      ? 'text-white/70 hover:bg-white/15 hover:text-white'
      : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300';

  if (collapsed) {
    return (
      <div
        ref={setSortRef}
        style={style}
        className="flex w-10 shrink-0 cursor-pointer flex-col items-center"
        onClick={() => setCollapsed(false)}
        title={`${title} (${tasks.length}) — Klicken zum Aufklappen`}
      >
        <div
          className={`flex w-full flex-col items-center gap-2 rounded-xl px-1 py-3 ${headerClasses}`}
          style={headerBgColor ? { backgroundColor: headerBgColor } : undefined}
        >
          <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${badgeClasses}`}>
            {tasks.length}
          </span>
          <span
            className={`text-xs font-semibold ${textClasses}`}
            style={{ writingMode: 'vertical-rl', textOrientation: 'mixed' }}
          >
            {title}
          </span>
        </div>
      </div>
    );
  }

  return (
    // Mobil eine Spalte pro Bildschirm (Container-Padding p-4 abgezogen), damit
    // Karten nicht halb angeschnitten stehen; Snap-Ziel für den Spalten-Pager.
    <div
      ref={setSortRef}
      id={`kbcol-${id}`}
      style={style}
      className="flex w-[calc(100vw-2rem)] shrink-0 snap-start flex-col sm:w-72"
    >
      <div
        className={`relative z-20 mb-3 flex items-center justify-between rounded-xl px-3 py-2 ${headerClasses}`}
        style={headerBgColor ? { backgroundColor: headerBgColor } : undefined}
      >
        <div
          className="flex min-w-0 flex-1 cursor-grab items-center gap-1.5"
          {...attributes}
          {...listeners}
        >
          {columnIcon && (
            <LucideIconByName name={columnIcon} className={`h-3.5 w-3.5 shrink-0 ${textClasses}`} />
          )}
          {editing ? (
            <input
              ref={editRef}
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onKeyDown={handleRenameKeyDown}
              onBlur={handleRenameSubmit}
              className="min-w-0 flex-1 rounded bg-white/90 px-1 py-0.5 text-sm font-semibold text-gray-900 outline-none dark:bg-gray-800 dark:text-white"
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <h3
              className={`cursor-pointer truncate text-sm font-semibold ${textClasses}`}
              onDoubleClick={(e) => {
                e.stopPropagation();
                e.preventDefault();
                if (onRenameColumn) {
                  setEditValue(title);
                  setEditing(true);
                }
              }}
              title="Doppelklick zum Umbenennen"
            >
              {title}
            </h3>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {showColumnCount && (
            <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${badgeClasses}`}>
              {tasks.length}
            </span>
          )}
          {onCreateTask && (
            <button
              onClick={() => setAdding(true)}
              className={`flex h-9 w-9 items-center justify-center rounded-lg transition-colors lg:h-auto lg:w-auto lg:p-1 ${btnClasses}`}
              title="Neue Aufgabe"
            >
              <PlusIcon className="h-4 w-4" />
            </button>
          )}
          {(onUpdateColumn || onDeleteColumn) && (
            <div className="relative" ref={menuRef}>
              <button
                onClick={() => { setMenuOpen(!menuOpen); setColorPickerOpen(false); setIconPickerOpen(false); setConfirmDelete(false); }}
                className={`flex h-9 w-9 items-center justify-center rounded-lg transition-colors lg:h-auto lg:w-auto lg:p-1 ${btnClasses}`}
                title="Spalten-Einstellungen"
              >
                <DotsIcon className="h-4 w-4" />
              </button>
              {menuOpen && (
                <div className="absolute right-0 top-8 z-[100] w-60 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  <button
                    onClick={() => { setColorPickerOpen(!colorPickerOpen); setIconPickerOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <Palette className="h-4 w-4" strokeWidth={1.5} />
                    Hintergrundfarbe
                  </button>
                  {colorPickerOpen && (
                    <div className="border-t border-gray-100 px-3 py-2 dark:border-gray-800">
                      <div className="grid grid-cols-6 gap-1.5">
                        {COLUMN_COLORS.map((c) => (
                          <button
                            key={c.key}
                            onClick={async () => {
                              if (onUpdateColumn) await onUpdateColumn(id, { color: c.key || null });
                              setColorPickerOpen(false);
                              setMenuOpen(false);
                            }}
                            className={`flex h-7 w-7 items-center justify-center rounded-md border transition-all ${
                              (columnColor || '') === c.key
                                ? 'border-indigo-500 ring-2 ring-indigo-300 dark:ring-indigo-700'
                                : 'border-gray-200 dark:border-gray-700'
                            }`}
                            title={c.label}
                          >
                            {c.key ? (
                              <span className="h-5 w-5 rounded" style={{ backgroundColor: c.swatch }} />
                            ) : (
                              <span className="text-xs text-gray-400">∅</span>
                            )}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  <button
                    onClick={() => { setIconPickerOpen(!iconPickerOpen); setColorPickerOpen(false); setMenuOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <Star className="h-4 w-4" strokeWidth={1.5} />
                    Spalten-Icon
                  </button>
                  <div className="my-1 border-t border-gray-100 dark:border-gray-800" />
                  <button
                    onClick={() => { setCollapsed(true); setMenuOpen(false); }}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                  >
                    <CollapseIcon className="h-4 w-4" />
                    Spalte einklappen
                  </button>
                  {onDeleteColumn && !confirmDelete && (
                    <button
                      data-testid="column-delete-trigger"
                      onClick={() => setConfirmDelete(true)}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                    >
                      <TrashIcon className="h-4 w-4" />
                      Spalte löschen
                    </button>
                  )}
                  {onDeleteColumn && confirmDelete && (
                    <div className="px-3 py-2">
                      <p className="mb-2 text-xs text-red-600 dark:text-red-400">
                        Spalte wirklich löschen? Enthaltene Aufgaben werden in eine andere Spalte verschoben.
                      </p>
                      <div className="flex gap-2">
                        <button
                          data-testid="column-delete-confirm"
                          onClick={async () => {
                            if (onDeleteColumn) await onDeleteColumn(id);
                            setConfirmDelete(false);
                            setMenuOpen(false);
                          }}
                          className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700"
                        >
                          Löschen
                        </button>
                        <button
                          data-testid="column-delete-cancel"
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
              {iconPickerOpen && (
                <LucideIconPicker
                  currentIcon={columnIcon}
                  onSelect={async (iconName) => {
                    if (onUpdateColumn) await onUpdateColumn(id, { icon_emoji: iconName });
                    setIconPickerOpen(false);
                  }}
                  onClose={() => setIconPickerOpen(false)}
                />
              )}
            </div>
          )}
        </div>
      </div>

      <div
        ref={setDropRef}
        className={`flex flex-1 flex-col gap-2 rounded-xl border-2 border-dashed p-2 transition-colors ${
          isOver
            ? 'border-indigo-400 bg-indigo-50/50 dark:border-indigo-500 dark:bg-indigo-950/30'
            : 'border-transparent'
        }`}
        style={{ minHeight: 200 }}
      >
        {adding && (
          <div className="rounded-xl border border-indigo-300 bg-white p-2 shadow-sm dark:border-indigo-700 dark:bg-gray-900">
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={handleKeyDown}
              onBlur={() => {
                if (!newTitle.trim()) setAdding(false);
              }}
              autoFocus
              placeholder="Titel eingeben..."
              className="w-full rounded bg-transparent px-1 py-0.5 text-sm text-gray-900 outline-none placeholder:text-gray-400 dark:text-white dark:placeholder:text-gray-600"
            />
          </div>
        )}

        <SortableContext
          items={tasks.map((t) => t.id)}
          strategy={verticalListSortingStrategy}
        >
          {tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              projectColor={projectColorMap?.[task.project_id]}
              projectName={projectMap?.[task.project_id]?.name}
              projectIconUrl={projectMap?.[task.project_id]?.icon_url}
              projectIconEmoji={projectMap?.[task.project_id]?.icon_emoji}
              showProjectIndicator={showProjectIndicator}
              hasBg={hasBg}
              userAvatarUrl={userAvatarUrl}
              onClick={onTaskClick}
              onArchive={onArchiveTask}
            />
          ))}
        </SortableContext>
      </div>
    </div>
  );
}

function PlusIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
    </svg>
  );
}

function CollapseIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M18.75 19.5l-7.5-7.5 7.5-7.5m-6 15L5.25 12l7.5-7.5" />
    </svg>
  );
}

function DotsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.75a.75.75 0 1 1 0-1.5.75.75 0 0 1 0 1.5ZM12 12.75a.75.75 0 1 1 0-1.5.75.75 0 0 1 0 1.5ZM12 18.75a.75.75 0 1 1 0-1.5.75.75 0 0 1 0 1.5Z" />
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
