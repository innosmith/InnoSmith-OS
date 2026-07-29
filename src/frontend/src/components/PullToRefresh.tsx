import { useRef, useState, useCallback, useEffect, type ReactNode } from 'react';

const THRESHOLD = 80;
const MAX_PULL = 130;
const RESISTANCE = 0.45;

async function clearAllCaches() {
  if ('caches' in window) {
    const names = await caches.keys();
    await Promise.all(names.map((n) => caches.delete(n)));
  }
  if ('serviceWorker' in navigator) {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map((r) => r.unregister()));
  }
}

/**
 * Nächster vertikal scrollbarer Vorfahre des berührten Elements.
 *
 * Der äussere Container ist `overflow-hidden` und hat darum immer `scrollTop === 0`.
 * Ohne diese Suche würde Pull-to-Refresh auch mitten in einer gescrollten Liste
 * auslösen und die Seite neu laden.
 */
function findScrollableAncestor(target: EventTarget | null): HTMLElement | null {
  let el = target instanceof Element ? (target as HTMLElement) : null;
  while (el && el !== document.body) {
    const overflowY = getComputedStyle(el).overflowY;
    if ((overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 1) {
      return el;
    }
    el = el.parentElement;
  }
  return null;
}

interface PullToRefreshProps {
  children: ReactNode;
  scrollContainerRef?: React.RefObject<HTMLElement | null>;
}

export function PullToRefresh({ children, scrollContainerRef }: PullToRefreshProps) {
  const [pullY, setPullY] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const startY = useRef(0);
  const startX = useRef(0);
  const pulling = useRef(false);
  const armed = useRef(false);
  const scroller = useRef<HTMLElement | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const isAtTop = useCallback(() => {
    const el = scrollContainerRef?.current ?? scroller.current;
    if (!el) return true;
    return el.scrollTop <= 0;
  }, [scrollContainerRef]);

  const onTouchStart = useCallback(
    (e: TouchEvent) => {
      if (refreshing) return;
      scroller.current = findScrollableAncestor(e.target);
      if (!isAtTop()) return;
      startY.current = e.touches[0].clientY;
      startX.current = e.touches[0].clientX;
      armed.current = true;
      pulling.current = false;
    },
    [refreshing, isAtTop],
  );

  const onTouchMove = useCallback(
    (e: TouchEvent) => {
      if (!armed.current || refreshing) return;
      const dy = e.touches[0].clientY - startY.current;
      const dx = e.touches[0].clientX - startX.current;

      // Horizontale Gesten (Kanban-Spalten, Chip-Leisten) nicht kapern.
      if (!pulling.current) {
        if (Math.abs(dx) > Math.abs(dy)) {
          armed.current = false;
          return;
        }
        if (dy <= 0) return;
        pulling.current = true;
      }

      if (dy < 0) {
        pulling.current = false;
        armed.current = false;
        setPullY(0);
        return;
      }
      setPullY(Math.min(dy * RESISTANCE, MAX_PULL));
    },
    [refreshing],
  );

  const onTouchEnd = useCallback(async () => {
    if (!pulling.current && pullY === 0) return;
    pulling.current = false;
    armed.current = false;

    if (pullY >= THRESHOLD) {
      setRefreshing(true);
      setPullY(THRESHOLD);
      try {
        await clearAllCaches();
      } catch {
        /* best-effort */
      }
      window.location.reload();
      return;
    }
    setPullY(0);
  }, [pullY]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchmove', onTouchMove, { passive: true });
    el.addEventListener('touchend', onTouchEnd);
    return () => {
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
      el.removeEventListener('touchend', onTouchEnd);
    };
  }, [onTouchStart, onTouchMove, onTouchEnd]);

  const progress = Math.min(pullY / THRESHOLD, 1);
  const showIndicator = pullY > 10;

  return (
    <div ref={containerRef} className="relative flex h-full flex-col overflow-hidden">
      {/* Pull indicator */}
      <div
        className="pointer-events-none absolute inset-x-0 top-0 z-50 flex items-start justify-center overflow-hidden transition-[height] duration-100 ease-out"
        style={{ height: pullY }}
      >
        <div
          className={`mt-[calc(0.75rem+env(safe-area-inset-top,0px))] flex h-9 w-9 items-center justify-center rounded-full shadow-lg transition-all duration-200 ${
            refreshing
              ? 'bg-indigo-500 text-white'
              : pullY >= THRESHOLD
                ? 'bg-indigo-500 text-white scale-110'
                : 'bg-white text-gray-500 dark:bg-gray-800 dark:text-gray-400'
          }`}
          style={{
            opacity: showIndicator ? 1 : 0,
            transform: `rotate(${progress * 180}deg) scale(${showIndicator ? 1 : 0.5})`,
          }}
        >
          {refreshing ? (
            <svg className="h-5 w-5 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          ) : (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 5v14M5 12l7-7 7 7" />
            </svg>
          )}
        </div>
      </div>

      {/* Content shifted down by pullY */}
      <div
        className="flex flex-1 flex-col overflow-hidden"
        style={{
          transform: pullY > 0 ? `translateY(${pullY}px)` : undefined,
          transition: pulling.current ? 'none' : 'transform 200ms ease-out',
        }}
      >
        {children}
      </div>
    </div>
  );
}
