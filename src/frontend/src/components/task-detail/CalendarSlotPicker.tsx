import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';

interface CalendarSlotPickerProps {
  taskTitle: string;
  taskDescription?: string | null;
  initialDate?: string | null;
  onConfirm: (eventId: string) => void;
  onClose: () => void;
}

interface CalendarEvent {
  id: string;
  subject: string | null;
  start: string | null;
  end: string | null;
  is_all_day: boolean;
  show_as: string | null;
}

const DAY_START_HOUR = 7;
const DAY_END_HOUR = 20;
const TOTAL_MINUTES = (DAY_END_HOUR - DAY_START_HOUR) * 60;
const PX_PER_MINUTE = 1.2;
const TIMELINE_HEIGHT = TOTAL_MINUTES * PX_PER_MINUTE;
const SNAP_MINUTES = 15;
const MIN_DURATION = 15;

const DAY_NAMES = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
const MONTH_NAMES = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'];

function formatDateDE(d: Date): string {
  return `${DAY_NAMES[d.getDay()]}, ${d.getDate()}. ${MONTH_NAMES[d.getMonth()]} ${d.getFullYear()}`;
}

function formatTime(d: Date): string {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function formatMinutes(totalMin: number): string {
  const h = DAY_START_HOUR + Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function startOfDay(d: Date): Date {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

function endOfDay(d: Date): Date {
  const r = new Date(d);
  r.setHours(23, 59, 59, 999);
  return r;
}

function snapTo15(minutes: number): number {
  return Math.floor(minutes / SNAP_MINUTES) * SNAP_MINUTES;
}

function toLocalISOString(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function minutesSinceDayStart(d: Date): number {
  return (d.getHours() - DAY_START_HOUR) * 60 + d.getMinutes();
}

function clamp(val: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, val));
}

type DragMode = 'create' | 'resize' | 'move' | null;

export default function CalendarSlotPicker({
  taskTitle,
  taskDescription,
  initialDate,
  onConfirm,
  onClose,
}: CalendarSlotPickerProps) {
  const [currentDate, setCurrentDate] = useState<Date>(() => {
    if (initialDate) return new Date(initialDate + 'T00:00:00');
    return startOfDay(new Date());
  });
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [duration, setDuration] = useState(60);
  const [selectedStartMin, setSelectedStartMin] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);

  const [dragMode, setDragMode] = useState<DragMode>(null);
  const [dragAnchorMin, setDragAnchorMin] = useState(0);
  const [dragCurrentMin, setDragCurrentMin] = useState(0);
  const [moveOffset, setMoveOffset] = useState(0);

  const timelineRef = useRef<HTMLDivElement>(null);

  const fetchEvents = useCallback(async (date: Date) => {
    setLoading(true);
    setSelectedStartMin(null);
    try {
      const dayStartISO = startOfDay(date).toISOString();
      const dayEndISO = endOfDay(date).toISOString();
      const evts = await api.get<CalendarEvent[]>(
        `/api/calendar/events?start=${encodeURIComponent(dayStartISO)}&end=${encodeURIComponent(dayEndISO)}`,
      );
      setEvents(evts.filter((e) => !e.is_all_day));
    } catch {
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchEvents(currentDate);
  }, [currentDate, fetchEvents]);

  const navigateDay = (offset: number) => {
    setCurrentDate((prev) => {
      const next = new Date(prev);
      next.setDate(next.getDate() + offset);
      return next;
    });
  };

  const parsedEvents = events
    .filter((e) => e.start && e.end)
    .map((e) => {
      const start = new Date(e.start!);
      const end = new Date(e.end!);
      const startMin = clamp(minutesSinceDayStart(start), 0, TOTAL_MINUTES);
      const endMin = clamp(minutesSinceDayStart(end), 0, TOTAL_MINUTES);
      return { ...e, startMin, endMin, startDate: start, endDate: end };
    })
    .filter((e) => e.endMin > e.startMin);

  const overlapsWithEvent = (startMin: number, endMin: number): boolean => {
    return parsedEvents.some((e) => startMin < e.endMin && endMin > e.startMin);
  };

  const findNextFreeSlot = (fromMin: number, durationMin: number): number | null => {
    let candidate = fromMin;
    const maxStart = TOTAL_MINUTES - durationMin;
    for (let i = 0; i < 100; i++) {
      if (candidate > maxStart) return null;
      const candidateEnd = candidate + durationMin;
      const blocking = parsedEvents.find((e) => candidate < e.endMin && candidateEnd > e.startMin);
      if (!blocking) return candidate;
      candidate = blocking.endMin;
    }
    return null;
  };

  const yToMinutes = useCallback((clientY: number): number => {
    if (!timelineRef.current) return 0;
    const rect = timelineRef.current.getBoundingClientRect();
    const y = clientY - rect.top + timelineRef.current.scrollTop;
    const raw = y / PX_PER_MINUTE;
    return snapTo15(clamp(raw, 0, TOTAL_MINUTES));
  }, []);

  // Drag-to-Create: mousedown on empty timeline area
  const handleTimelineMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('[data-event]')) return;
    if ((e.target as HTMLElement).closest('[data-selected]')) return;
    if ((e.target as HTMLElement).closest('[data-resize]')) return;
    if (e.button !== 0) return;

    const min = yToMinutes(e.clientY);
    setDragMode('create');
    setDragAnchorMin(min);
    setDragCurrentMin(min);
    e.preventDefault();
  };

  // Move: mousedown on the selected block
  const handleBlockMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('[data-resize]')) return;
    if (e.button !== 0 || selectedStartMin === null) return;

    const min = yToMinutes(e.clientY);
    setDragMode('move');
    setMoveOffset(min - selectedStartMin);
    e.preventDefault();
    e.stopPropagation();
  };

  // Resize: mousedown on the resize handle
  const handleResizeMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0 || selectedStartMin === null) return;
    setDragMode('resize');
    e.preventDefault();
    e.stopPropagation();
  };

  // Global mousemove and mouseup handlers
  useEffect(() => {
    if (!dragMode) return;

    const handleMouseMove = (e: MouseEvent) => {
      const min = yToMinutes(e.clientY);
      setDragCurrentMin(min);

      if (dragMode === 'create') {
        // Live preview is rendered from dragAnchorMin to dragCurrentMin
      } else if (dragMode === 'resize') {
        if (selectedStartMin !== null) {
          const newDuration = Math.max(MIN_DURATION, snapTo15(min - selectedStartMin));
          setDuration(clamp(newDuration, MIN_DURATION, TOTAL_MINUTES - selectedStartMin));
        }
      } else if (dragMode === 'move') {
        if (selectedStartMin !== null) {
          const newStart = snapTo15(min - moveOffset);
          const clamped = clamp(newStart, 0, TOTAL_MINUTES - duration);
          setSelectedStartMin(clamped);
        }
      }
    };

    const handleMouseUp = (e: MouseEvent) => {
      if (dragMode === 'create') {
        const endMin = yToMinutes(e.clientY);
        const start = Math.min(dragAnchorMin, endMin);
        const end = Math.max(dragAnchorMin, endMin);
        const draggedDuration = end - start;

        if (draggedDuration >= MIN_DURATION) {
          if (!overlapsWithEvent(start, end)) {
            setSelectedStartMin(start);
            setDuration(draggedDuration);
          } else {
            const free = findNextFreeSlot(start, draggedDuration);
            if (free !== null) {
              setSelectedStartMin(free);
              setDuration(draggedDuration);
            }
          }
        } else {
          // Short click: use default duration
          const clickMin = snapTo15(Math.min(dragAnchorMin, endMin));
          const clampedMin = clamp(clickMin, 0, TOTAL_MINUTES - duration);
          if (!overlapsWithEvent(clampedMin, clampedMin + duration)) {
            setSelectedStartMin(clampedMin);
          } else {
            const free = findNextFreeSlot(clampedMin, duration);
            if (free !== null) setSelectedStartMin(free);
          }
        }
      }
      setDragMode(null);
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dragMode, dragAnchorMin, selectedStartMin, duration, moveOffset]);

  const handleConfirm = async () => {
    if (selectedStartMin === null) return;
    setCreating(true);
    try {
      const startDt = new Date(currentDate);
      startDt.setHours(DAY_START_HOUR + Math.floor(selectedStartMin / 60), selectedStartMin % 60, 0, 0);
      const endDt = new Date(startDt.getTime() + duration * 60000);
      const event = await api.post<{ id: string }>('/api/calendar/events', {
        subject: taskTitle,
        start: toLocalISOString(startDt),
        end: toLocalISOString(endDt),
        body: taskDescription || '',
        show_as: 'busy',
      });
      onConfirm(event.id);
    } catch {
      setCreating(false);
    }
  };

  const dayFullyBooked = !loading && parsedEvents.length > 0 && findNextFreeSlot(0, MIN_DURATION) === null;
  const hours = Array.from({ length: DAY_END_HOUR - DAY_START_HOUR }, (_, i) => DAY_START_HOUR + i);

  // Compute drag preview for create mode
  const dragPreview = dragMode === 'create' ? {
    start: Math.min(dragAnchorMin, dragCurrentMin),
    end: Math.max(dragAnchorMin, dragCurrentMin),
  } : null;

  return (
    <div
      className="modal-safe fixed inset-0 z-[9999] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="flex max-h-full w-full max-w-lg lg:max-h-[85dvh] flex-col rounded-2xl bg-white shadow-2xl dark:bg-gray-950">
        {/* Header */}
        <div className="flex flex-shrink-0 items-center justify-between border-b border-gray-100 px-5 py-3.5 dark:border-gray-800">
          <div className="flex items-center gap-2">
            <button
              onClick={() => navigateDay(-1)}
              className="rounded-lg p-1.5 text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" />
              </svg>
            </button>
            <span className="min-w-[200px] text-center text-sm font-medium text-gray-800 dark:text-gray-200">
              {formatDateDE(currentDate)}
            </span>
            <button
              onClick={() => navigateDay(1)}
              className="rounded-lg p-1.5 text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
              </svg>
            </button>
          </div>

          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Hint */}
        <div className="flex-shrink-0 border-b border-gray-50 px-5 py-1.5 text-[10px] text-gray-400 dark:border-gray-900 dark:text-gray-500">
          Klicken oder ziehen um Slot zu wählen · Unteren Rand ziehen zum Vergrössern · Block verschieben per Drag
        </div>

        {/* Body */}
        <div
          ref={timelineRef}
          className="flex-1 overflow-y-auto px-2 py-2"
          onMouseDown={!loading && !dayFullyBooked ? handleTimelineMouseDown : undefined}
          style={{ cursor: dragMode === 'create' ? 'crosshair' : undefined }}
        >
          {loading ? (
            <div className="flex h-48 items-center justify-center">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
              <span className="ml-2 text-sm text-gray-400">Kalender wird geladen…</span>
            </div>
          ) : dayFullyBooked ? (
            <div className="flex h-48 flex-col items-center justify-center gap-2 text-center">
              <svg className="h-8 w-8 text-gray-300 dark:text-gray-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 0 1 2.25-2.25h13.5A2.25 2.25 0 0 1 21 7.5v11.25m-18 0A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75m-18 0v-7.5A2.25 2.25 0 0 1 5.25 9h13.5A2.25 2.25 0 0 1 21 11.25v7.5" />
              </svg>
              <p className="text-sm font-medium text-gray-500 dark:text-gray-400">Tag vollständig belegt</p>
              <p className="text-xs text-gray-400 dark:text-gray-500">Kein freier Slot verfügbar</p>
            </div>
          ) : (
            <div className="relative select-none" style={{ height: TIMELINE_HEIGHT }}>
              {/* Hour grid */}
              {hours.map((hour) => {
                const top = (hour - DAY_START_HOUR) * 60 * PX_PER_MINUTE;
                return (
                  <div key={hour} className="absolute left-0 right-0" style={{ top }}>
                    <div className="flex items-start">
                      <span className="w-12 flex-shrink-0 text-[10px] text-gray-400 dark:text-gray-500">
                        {String(hour).padStart(2, '0')}:00
                      </span>
                      <div className="flex-1 border-t border-gray-200 dark:border-gray-800" />
                    </div>
                    <div className="absolute left-12 right-0" style={{ top: 30 * PX_PER_MINUTE }}>
                      <div className="border-t border-dashed border-gray-100 dark:border-gray-850" />
                    </div>
                  </div>
                );
              })}

              {/* Existing events */}
              {parsedEvents.map((evt) => (
                <div
                  key={evt.id}
                  data-event
                  className="absolute left-12 right-1 rounded-r-lg border-l-2 border-slate-400 bg-slate-100 px-2 py-0.5 dark:border-slate-500 dark:bg-slate-800"
                  style={{
                    top: evt.startMin * PX_PER_MINUTE,
                    height: Math.max((evt.endMin - evt.startMin) * PX_PER_MINUTE, 18),
                  }}
                >
                  <p className="truncate text-[10px] font-medium text-slate-700 dark:text-slate-300">
                    {evt.subject || '(Kein Betreff)'}
                  </p>
                  {(evt.endMin - evt.startMin) * PX_PER_MINUTE > 30 && (
                    <p className="text-[9px] text-slate-500 dark:text-slate-400">
                      {formatTime(evt.startDate)} – {formatTime(evt.endDate)}
                    </p>
                  )}
                </div>
              ))}

              {/* Drag preview (while creating) */}
              {dragPreview && dragPreview.end - dragPreview.start >= MIN_DURATION && (
                <div
                  className="pointer-events-none absolute left-12 right-1 rounded-r-lg border-l-2 border-indigo-400 bg-indigo-100/70 px-2 py-0.5 dark:bg-indigo-900/30"
                  style={{
                    top: dragPreview.start * PX_PER_MINUTE,
                    height: (dragPreview.end - dragPreview.start) * PX_PER_MINUTE,
                  }}
                >
                  <p className="text-[10px] font-medium text-indigo-600 dark:text-indigo-300">
                    {formatMinutes(dragPreview.start)} – {formatMinutes(dragPreview.end)}
                  </p>
                </div>
              )}

              {/* Selected slot — visible during idle, move, and resize */}
              {selectedStartMin !== null && dragMode !== 'create' && (
                <div
                  data-selected
                  className={`absolute left-12 right-1 rounded-r-lg border-l-2 border-indigo-500 bg-indigo-100 px-2 py-0.5 transition-none dark:bg-indigo-900/40 ${
                    dragMode === 'move' || dragMode === 'resize' ? 'ring-2 ring-indigo-400/50' : ''
                  }`}
                  style={{
                    top: selectedStartMin * PX_PER_MINUTE,
                    height: duration * PX_PER_MINUTE,
                    cursor: dragMode === 'move' ? 'grabbing' : 'grab',
                  }}
                  onMouseDown={handleBlockMouseDown}
                >
                  <p className="truncate text-[10px] font-semibold text-indigo-800 dark:text-indigo-200">
                    {taskTitle}
                  </p>
                  <p className="text-[9px] text-indigo-600 dark:text-indigo-300">
                    {formatMinutes(selectedStartMin)} – {formatMinutes(selectedStartMin + duration)}
                  </p>
                  {/* Resize handle */}
                  <div
                    data-resize
                    className="absolute bottom-0 left-0 right-0 flex h-3 cursor-ns-resize items-center justify-center rounded-b-lg hover:bg-indigo-200/40 dark:hover:bg-indigo-800/30"
                    onMouseDown={handleResizeMouseDown}
                  >
                    <div className="h-[2px] w-8 rounded-full bg-indigo-400/60" />
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex flex-shrink-0 items-center justify-between border-t border-gray-100 px-5 py-3 dark:border-gray-800">
          <span className="text-[10px] text-gray-400 dark:text-gray-500">
            {selectedStartMin !== null
              ? `${formatMinutes(selectedStartMin)} – ${formatMinutes(selectedStartMin + duration)} (${duration} Min)`
              : 'Kein Slot ausgewählt'}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-lg px-3.5 py-1.5 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
            >
              Abbrechen
            </button>
            <button
              onClick={handleConfirm}
              disabled={selectedStartMin === null || creating}
              className="rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {creating ? 'Wird erstellt…' : 'Termin blockieren'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
