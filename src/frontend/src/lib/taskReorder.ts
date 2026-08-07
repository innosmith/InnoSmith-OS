import { arrayMove } from '@dnd-kit/sortable';
import { api } from '../api/client';
import type { TaskCard } from '../types';

/**
 * Gemeinsame Drag-&-Drop-Logik für Agenda (Pipeline) und Projektboard.
 *
 * Die Funktionen sind absichtlich rein: sie bekommen den aktuellen Spaltenstand
 * und geben den neuen zurück. Damit kann der Aufrufer den Zielzustand *vor* dem
 * React-State-Update berechnen und exakt diesen ans Backend schicken -- statt
 * ihn nach einem asynchronen `setState` aus dem noch alten State zu lesen.
 */

/** `pipeline` schreibt die Agenda-Reihenfolge, `board` die des Projektboards. */
export type ReorderScope = 'board' | 'pipeline';

/** Minimale Spaltenform, die Agenda und Projektboard gemeinsam haben. */
export interface OrderedColumn {
  id: string;
  tasks: TaskCard[];
}

export function findColumnOfTask<C extends OrderedColumn>(
  columns: C[],
  taskId: string,
): C | undefined {
  return columns.find((col) => col.tasks.some((t) => t.id === taskId));
}

/**
 * Hängt einen Task in eine andere Spalte um. `overId` darf eine Task-ID sein
 * (dann wird davor einsortiert) oder eine Spalten-ID (dann ans Ende).
 * Gibt `null` zurück, wenn nichts zu tun ist.
 */
export function moveTaskToColumn<C extends OrderedColumn>(
  columns: C[],
  activeId: string,
  overId: string,
): C[] | null {
  const source = findColumnOfTask(columns, activeId);
  const target =
    columns.find((col) => col.id === overId) ?? findColumnOfTask(columns, overId);
  if (!source || !target || source.id === target.id) return null;

  const task = source.tasks.find((t) => t.id === activeId);
  if (!task) return null;

  return columns.map((col) => {
    if (col.id === source.id) {
      return { ...col, tasks: col.tasks.filter((t) => t.id !== activeId) };
    }
    if (col.id === target.id) {
      const tasks = [...col.tasks];
      const overIndex = tasks.findIndex((t) => t.id === overId);
      tasks.splice(overIndex >= 0 ? overIndex : tasks.length, 0, task);
      return { ...col, tasks };
    }
    return col;
  });
}

/**
 * Sortiert einen Task innerhalb seiner Spalte um. Gibt `null` zurück, wenn
 * `overId` kein Nachbar-Task derselben Spalte ist -- etwa beim Drop auf die
 * freie Fläche der eigenen Spalte.
 */
export function reorderWithinColumn<C extends OrderedColumn>(
  columns: C[],
  activeId: string,
  overId: string,
): C[] | null {
  const col = findColumnOfTask(columns, activeId);
  if (!col) return null;
  const oldIndex = col.tasks.findIndex((t) => t.id === activeId);
  const newIndex = col.tasks.findIndex((t) => t.id === overId);
  if (oldIndex === -1 || newIndex === -1 || oldIndex === newIndex) return null;
  return columns.map((c) =>
    c.id === col.id ? { ...c, tasks: arrayMove(c.tasks, oldIndex, newIndex) } : c,
  );
}

/**
 * Schreibt die Reihenfolge der betroffenen Spalten ans Backend, das sie
 * komplett mit 1..N neu durchnummeriert. Beim Spaltenwechsel gehört auch die
 * Herkunftsspalte dazu, damit sie lückenlos zurückbleibt.
 */
export async function persistTaskOrder(
  scope: ReorderScope,
  columns: OrderedColumn[],
  affectedColumnIds: Array<string | null | undefined>,
): Promise<void> {
  const uniqueIds = [...new Set(affectedColumnIds.filter((id): id is string => !!id))];
  const payload = uniqueIds
    .map((id) => columns.find((c) => c.id === id))
    .filter((c): c is OrderedColumn => c !== undefined)
    .map((c) => ({ column_id: c.id, task_ids: c.tasks.map((t) => t.id) }));
  if (payload.length === 0) return;
  await api.post('/api/tasks/reorder', { scope, columns: payload });
}
