import { useCallback, useEffect, useRef, useState } from 'react';

interface PagerColumn {
  id: string;
  name: string;
  tasks: unknown[];
}

/** Muss zum `scroll-px-4`/`p-4` des Board-Scrollers passen. */
const SCROLL_PADDING = 16;

/**
 * Scrollt genau einen Container horizontal — ohne `scrollIntoView`, das auch alle
 * übergeordneten Scroller verschiebt. Genau das brach den Sprung zur Spalte ab,
 * weil das Zentrieren des Chips den laufenden Board-Scroll überschrieb.
 */
function scrollElementIntoTrack(
  scroller: HTMLElement,
  target: HTMLElement,
  align: 'start' | 'center',
) {
  const offset = target.getBoundingClientRect().left - scroller.getBoundingClientRect().left;
  const left =
    align === 'start'
      ? scroller.scrollLeft + offset - SCROLL_PADDING
      : scroller.scrollLeft +
        offset -
        (scroller.clientWidth - target.getBoundingClientRect().width) / 2;
  scroller.scrollTo({ left: Math.max(0, left), behavior: 'smooth' });
}

/**
 * Spalten-Pager für Kanban-Boards auf schmalen Bildschirmen.
 *
 * Mobil ist nur eine Spalte sichtbar; ohne Pager weiss man nicht, wie viele
 * Spalten es gibt und wo man steht. Die Chips springen direkt zur Spalte und
 * markieren die aktuell sichtbare.
 */
export function ColumnPager({ columns, hasBg }: { columns: PagerColumn[]; hasBg: boolean }) {
  const [activeId, setActiveId] = useState<string | null>(columns[0]?.id ?? null);
  const chipRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const stripRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (columns.length === 0) return;
    const observer = new IntersectionObserver(
      entries => {
        const visible = entries
          .filter(e => e.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible) setActiveId(visible.target.id.replace('kbcol-', ''));
      },
      { threshold: [0.5, 0.75] },
    );
    for (const col of columns) {
      const el = document.getElementById(`kbcol-${col.id}`);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [columns]);

  useEffect(() => {
    const strip = stripRef.current;
    const chip = activeId ? chipRefs.current[activeId] : null;
    if (strip && chip) scrollElementIntoTrack(strip, chip, 'center');
  }, [activeId]);

  const jumpToColumn = useCallback((columnId: string) => {
    const el = document.getElementById(`kbcol-${columnId}`);
    const scroller = el?.closest<HTMLElement>('[data-board-scroller]');
    if (el && scroller) scrollElementIntoTrack(scroller, el, 'start');
  }, []);

  if (columns.length < 2) return null;

  return (
    <div
      ref={stripRef}
      className="sticky left-0 z-30 -mx-4 mb-3 flex gap-1.5 overflow-x-auto px-4 pb-1 sm:hidden [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
    >
      {columns.map(col => {
        const isActive = col.id === activeId;
        return (
          <button
            key={col.id}
            ref={el => {
              chipRefs.current[col.id] = el;
            }}
            onClick={() => jumpToColumn(col.id)}
            className={`flex min-h-9 shrink-0 items-center gap-1.5 rounded-full px-3 text-xs font-medium transition-colors ${
              isActive
                ? 'bg-indigo-600 text-white shadow-sm'
                : hasBg
                  ? 'bg-black/30 text-white/80 backdrop-blur-sm'
                  : 'bg-white/80 text-gray-600 shadow-sm dark:bg-gray-800/80 dark:text-gray-300'
            }`}
          >
            <span className="max-w-[9rem] truncate">{col.name}</span>
            <span className={isActive ? 'text-white/70' : 'text-gray-400 dark:text-gray-500'}>
              {col.tasks.length}
            </span>
          </button>
        );
      })}
    </div>
  );
}
