import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Plus, Trash2, ArrowRightLeft, Pencil,
  ChevronLeft, ChevronRight, ChevronDown, Calendar, X, GripVertical, Palmtree, Unlink,
  Check, AlertTriangle, EyeOff, Link2Off,
} from 'lucide-react';
import { DndContext, closestCenter, PointerSensor, useSensor, useSensors, type DragEndEvent, useDroppable, useDraggable } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy, useSortable, arrayMove } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { restrictToHorizontalAxis } from '@dnd-kit/modifiers';
import { api } from '../api/client';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { ProjectIcon } from '../components/ProjectIcon';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { LucideIconPicker } from '../components/LucideIconPicker';

// ── Types ────────────────────────────────────────────────────────────────────

interface CapProject {
  id: string;
  name: string;
  color: string;
  client_name: string | null;
  hourly_rate: number | null;
  is_billable: boolean;
  status: 'bestätigt' | 'vorläufig';
  project_id: string | null;
  toggl_project_id: number | null;
  toggl_client_id: number | null;
  toggl_billable_filter: 'non_billable' | 'billable' | null;
  pipedrive_deal_id: number | null;
  sort_order: number;
  notes: string | null;
  icon_url: string | null;
  icon_emoji: string | null;
  created_at?: string;
  alloc_count?: number;
}

interface Allocation {
  id: string;
  capacity_project_id: string;
  week_start: string;
  minutes: number;
  allocation_type?: 'week' | 'day';
  series_id: string | null;
  notes: string | null;
}

interface WeeklySummary {
  week_start: string;
  available_minutes: number;
  planned_minutes: number;
  tentative_minutes: number;
  utilization_pct: number;
}

interface TimeOffEntry {
  id: string;
  date: string;
  type: string;
  label: string | null;
  hours: number;
}

interface PlanVsActualProject {
  toggl_project_id: number | null;
  capacity_project_id?: string;
  name: string;
  weeks: { week_start: string; planned_minutes: number; actual_minutes: number }[];
}

interface MonthlyActualProject {
  capacity_project_id: string;
  name: string;
  planned_minutes: number;
  actual_minutes: number;
  prev_month_planned?: number;
  prev_month_actual?: number;
}

type ViewRange = '3m' | '6m' | '1y';

// ── Helpers ──────────────────────────────────────────────────────────────────

function getMonday(d: Date): Date {
  const day = d.getDay();
  const diff = d.getDate() - day + (day === 0 ? -6 : 1);
  return new Date(d.getFullYear(), d.getMonth(), diff);
}

function addWeeks(d: Date, weeks: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + weeks * 7);
}

function formatWeek(d: Date): string {
  const day = d.getDate();
  const month = d.toLocaleDateString('de-CH', { month: 'short' });
  return `${day}. ${month}`;
}

function toIso(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function getWeeksForRange(start: Date, range: ViewRange): Date[] {
  const weeks: Date[] = [];
  const count = range === '3m' ? 13 : range === '6m' ? 26 : 52;
  for (let i = 0; i < count; i++) {
    weeks.push(addWeeks(start, i));
  }
  return weeks;
}

function minutesToDisplay(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

function getIsoWeekNumber(d: Date): number {
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const dayNr = (target.getDay() + 6) % 7;
  target.setDate(target.getDate() - dayNr + 3);
  const firstThursday = new Date(target.getFullYear(), 0, 4);
  const firstDayNr = (firstThursday.getDay() + 6) % 7;
  firstThursday.setDate(firstThursday.getDate() - firstDayNr + 3);
  return 1 + Math.round((target.getTime() - firstThursday.getTime()) / (7 * 24 * 3600 * 1000));
}

/** Ganze Stunden für die Übersicht; die Minuten stehen in der Detailzeile. */
function hoursRounded(min: number): string {
  const abs = Math.abs(min);
  if (abs > 0 && abs < 30) return '<1h';
  return `${Math.round(abs / 60)}h`;
}

/** Farbschema der Auslastung — identisch zur Desktop-Matrix. */
function utilizationColors(util: number): { bar: string; text: string } {
  if (util === 0) return { bar: 'bg-gray-300 dark:bg-gray-600', text: 'text-gray-400 dark:text-gray-500' };
  if (util <= 60) return { bar: 'bg-emerald-400 dark:bg-emerald-500', text: 'text-emerald-700 dark:text-emerald-300' };
  if (util <= 85) return { bar: 'bg-emerald-500 dark:bg-emerald-400', text: 'text-emerald-700 dark:text-emerald-300' };
  if (util <= 100) return { bar: 'bg-amber-400 dark:bg-amber-400', text: 'text-amber-700 dark:text-amber-300' };
  return { bar: 'bg-red-500 dark:bg-red-500', text: 'text-red-700 dark:text-red-300' };
}

interface WeekListProps {
  weeks: Date[];
  summary: WeeklySummary[];
  allocations: Allocation[];
  projects: CapProject[];
  togglWeekMap: Record<string, number>;
  holidayWeekMap: Record<string, { hours: number; entries: { label: string; dayIndex: number }[] }>;
  timeOff: TimeOffEntry[];
  onCellClick: (projectId: string, weekStart: string) => void;
}

/**
 * Kapazität als Wochenliste (mobil).
 *
 * Die Matrix-Ansicht des Desktops setzt Breite voraus. Mobil ist die Leitfrage
 * nicht «welches Projekt», sondern «wo habe ich noch Luft und wo bin ich drüber».
 * Darum trägt jede Zeile die Restkapazität als Hauptwert; die Projekte einer
 * Woche stehen aufgeklappt darunter und führen per Tap in den Buchungsdialog.
 */
function CapacityWeekList({
  weeks, summary, allocations, projects, togglWeekMap, holidayWeekMap, timeOff, onCellClick,
}: WeekListProps) {
  const todayWeek = toIso(getMonday(new Date()));
  // Startzustand bewusst zugeklappt: die Übersicht über alle Wochen ist der Zweck
  // der Liste; eine offene Woche schiebt den Rest aus dem Bild.
  const [openWeek, setOpenWeek] = useState<string | null>(null);
  const [showAllProjects, setShowAllProjects] = useState(false);

  const minutesByWeekProject = useMemo(() => {
    const map: Record<string, Record<string, number>> = {};
    for (const a of allocations) {
      if (!map[a.week_start]) map[a.week_start] = {};
      map[a.week_start][a.capacity_project_id] =
        (map[a.week_start][a.capacity_project_id] || 0) + a.minutes;
    }
    return map;
  }, [allocations]);

  const projectById = useMemo(() => {
    const map: Record<string, CapProject> = {};
    for (const p of projects) map[p.id] = p;
    return map;
  }, [projects]);

  /** Kennzahlen pro Woche — einmal berechnet, für Zeilen und Kopfzeile. */
  const weekStats = useMemo(() => {
    // Ab der laufenden Woche: sie ist planbar und gehört in die Bilanz.
    const currentWeekStart = getMonday(new Date());
    return weeks.map(week => {
      const weekStr = toIso(week);
      const holidayHours = holidayWeekMap[weekStr]?.hours || 0;
      const s = summary.find(x => x.week_start === weekStr);
      const availMin = Math.max(0, (s?.available_minutes ?? 2400) - holidayHours * 60);
      const plannedMin = s?.planned_minutes ?? 0;
      return {
        week,
        weekStr,
        availMin,
        plannedMin,
        freeMin: availMin - plannedMin,
        util: availMin > 0 ? Math.round((plannedMin / availMin) * 100) : plannedMin > 0 ? 999 : 0,
        isFuture: week >= currentWeekStart,
      };
    });
  }, [weeks, summary, holidayWeekMap]);

  const overview = useMemo(() => {
    const future = weekStats.filter(w => w.isFuture);
    return {
      weeks: future.length,
      over: future.filter(w => w.freeMin < 0).length,
      freeMin: future.reduce((sum, w) => sum + Math.max(0, w.freeMin), 0),
    };
  }, [weekStats]);

  return (
    <div data-testid="capacity-week-list">
      {/* Gesamtbild zuerst: wie viele Wochen sind überbucht, wie viel ist frei. */}
      <div className="sticky top-0 z-10 flex items-center gap-3 border-b border-gray-200 bg-white/95 px-4 py-2 text-xs backdrop-blur dark:border-gray-700 dark:bg-gray-900/95">
        <span className="text-gray-500 dark:text-gray-400">{overview.weeks} Wochen</span>
        <span className={`flex items-center gap-1 font-medium ${overview.over > 0 ? 'text-red-600 dark:text-red-400' : 'text-gray-400 dark:text-gray-500'}`}>
          <span className="h-2 w-2 rounded-full bg-current" />
          {overview.over} überbucht
        </span>
        <span className="ml-auto font-medium text-emerald-600 tabular-nums dark:text-emerald-400">
          {minutesToDisplay(overview.freeMin)} frei
        </span>
      </div>
      <div className="divide-y divide-gray-100 dark:divide-gray-800">
      {weekStats.map(({ week, weekStr, availMin, plannedMin, freeMin, util, isFuture }) => {
        const weekEndDate = new Date(week.getFullYear(), week.getMonth(), week.getDate() + 6);
        const holidayInfo = holidayWeekMap[weekStr];
        const actualMin = togglWeekMap[weekStr] || 0;
        const isPast = !isFuture;
        const isCurrent = weekStr === todayWeek;
        const colors = utilizationColors(util);
        // Ganze Woche abwesend und nichts geplant: «0h voll» wäre irreführend.
        // Ist trotz Abwesenheit Arbeit gebucht, bleibt es eine Überbuchung —
        // genau die will man sehen, statt sie als «Ferien» zu verstecken.
        const isHolidayWeek = availMin === 0 && plannedMin === 0;

        const manualOffHours = timeOff
          .filter(t => t.date >= weekStr && t.date <= toIso(weekEndDate))
          .reduce((sum, t) => sum + t.hours, 0);
        const totalOffHours = manualOffHours + (holidayInfo?.hours || 0);

        const perProject = minutesByWeekProject[weekStr] || {};
        const bookedProjects = Object.entries(perProject)
          .filter(([id]) => projectById[id])
          .sort((a, b) => b[1] - a[1]);
        const isOpen = openWeek === weekStr;
        const unbookedProjects = projects.filter(p => !perProject[p.id]);

        return (
          <div
            key={weekStr}
            className={`border-l-2 ${isCurrent ? 'border-indigo-500 bg-indigo-50/60 dark:bg-indigo-950/20' : 'border-transparent'}`}
          >
            <button
              onClick={() => { setOpenWeek(isOpen ? null : weekStr); setShowAllProjects(false); }}
              className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left"
              aria-expanded={isOpen}
            >
              <div className="w-[52px] shrink-0">
                <div className={`text-xs font-semibold ${isCurrent ? 'text-indigo-700 dark:text-indigo-300' : isPast ? 'text-gray-400 dark:text-gray-500' : 'text-gray-700 dark:text-gray-200'}`}>
                  KW {getIsoWeekNumber(week)}
                </div>
                <div className="text-[10px] text-gray-400 dark:text-gray-500">{formatWeek(week)}</div>
              </div>
              <div className="min-w-0 flex-1">
                <div className="relative h-2.5 w-full overflow-hidden rounded-full bg-gray-200/80 dark:bg-gray-700/60">
                  <div className={`h-full rounded-full ${colors.bar}`} style={{ width: `${Math.min(100, util)}%` }} />
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-gray-400 dark:text-gray-500">
                  <span className="tabular-nums">{minutesToDisplay(plannedMin)}/{minutesToDisplay(availMin)}</span>
                  {/* Ohne verfügbare Zeit gibt es keine sinnvolle Quote (999%). */}
                  {availMin > 0 && <span className={`tabular-nums ${colors.text}`}>{util}%</span>}
                  {totalOffHours > 0 && (
                    <span className="flex items-center gap-0.5 text-amber-600 dark:text-amber-400">
                      <Palmtree className="h-2.5 w-2.5" />{totalOffHours}h
                    </span>
                  )}
                  {isPast && actualMin > 0 && (
                    <span className="tabular-nums">Ist {minutesToDisplay(actualMin)}</span>
                  )}
                </div>
              </div>
              {/* Hauptaussage rechts: verbleibende oder überbuchte Stunden. */}
              <div className="w-[52px] shrink-0 text-right">
                {isHolidayWeek ? (
                  <>
                    <div className="text-sm font-bold leading-tight text-amber-600 dark:text-amber-400">—</div>
                    <div className="text-[9px] text-amber-600/80 dark:text-amber-400/80">Ferien</div>
                  </>
                ) : (
                  <>
                    <div className={`text-sm font-bold leading-tight tabular-nums ${freeMin > 0 ? 'text-emerald-600 dark:text-emerald-400' : freeMin < 0 ? 'text-red-600 dark:text-red-400' : 'text-amber-600 dark:text-amber-400'}`}>
                      {freeMin > 0 ? `+${hoursRounded(freeMin)}` : freeMin < 0 ? `\u2212${hoursRounded(freeMin)}` : '0h'}
                    </div>
                    <div className="text-[9px] text-gray-400 dark:text-gray-500">
                      {freeMin > 0 ? 'frei' : freeMin < 0 ? 'über' : 'voll'}
                    </div>
                  </>
                )}
              </div>
              <ChevronDown className={`h-4 w-4 shrink-0 text-gray-300 transition-transform dark:text-gray-600 ${isOpen ? 'rotate-180' : ''}`} />
            </button>

            {isOpen && (
              <div className="border-t border-gray-100 bg-gray-50/60 px-4 py-2 dark:border-gray-800 dark:bg-gray-900/40">
                {holidayInfo && holidayInfo.entries.length > 0 && (
                  <div className="mb-1 flex items-center gap-1.5 text-[11px] text-amber-700 dark:text-amber-400">
                    <Palmtree className="h-3.5 w-3.5" />
                    {holidayInfo.entries.map(e => e.label).join(', ')}
                  </div>
                )}
                {bookedProjects.length === 0 && (
                  <p className="py-1 text-xs text-gray-400 dark:text-gray-500">Keine Kapazität geplant</p>
                )}
                {bookedProjects.map(([projectId, minutes]) => {
                  const p = projectById[projectId];
                  return (
                    <button
                      key={projectId}
                      onClick={() => onCellClick(projectId, weekStr)}
                      className="flex min-h-11 w-full items-center gap-2 text-left"
                    >
                      <ProjectIcon iconUrl={p.icon_url} iconEmoji={p.icon_emoji} color={p.color} size={18} />
                      <span className="min-w-0 flex-1 truncate text-xs text-gray-700 dark:text-gray-200">
                        {p.name}
                        {p.client_name && <span className="text-gray-400 dark:text-gray-500"> · {p.client_name}</span>}
                      </span>
                      <span className="shrink-0 text-xs font-medium tabular-nums text-gray-600 dark:text-gray-300">
                        {minutesToDisplay(minutes)}
                      </span>
                    </button>
                  );
                })}
                {unbookedProjects.length > 0 && (
                  showAllProjects ? (
                    <div className="mt-1 border-t border-gray-200 pt-1 dark:border-gray-700">
                      {unbookedProjects.map(p => (
                        <button
                          key={p.id}
                          onClick={() => onCellClick(p.id, weekStr)}
                          className="flex min-h-11 w-full items-center gap-2 text-left opacity-60"
                        >
                          <ProjectIcon iconUrl={p.icon_url} iconEmoji={p.icon_emoji} color={p.color} size={18} />
                          <span className="min-w-0 flex-1 truncate text-xs text-gray-700 dark:text-gray-200">{p.name}</span>
                          <Plus className="h-3.5 w-3.5 shrink-0 text-gray-400" />
                        </button>
                      ))}
                    </div>
                  ) : (
                    <button
                      onClick={() => setShowAllProjects(true)}
                      className="mt-1 flex min-h-11 items-center gap-1 text-xs font-medium text-indigo-600 dark:text-indigo-400"
                    >
                      <Plus className="h-3.5 w-3.5" /> Projekt einplanen
                    </button>
                  )
                )}
              </div>
            )}
          </div>
        );
      })}
      </div>
    </div>
  );
}

function getMonthPacing(): { expectedPct: number; workdaysElapsed: number; workdaysTotal: number } {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth();
  const today = now.getDate();
  const lastDay = new Date(year, month + 1, 0).getDate();
  let elapsed = 0;
  let total = 0;
  for (let d = 1; d <= lastDay; d++) {
    const dow = new Date(year, month, d).getDay();
    if (dow >= 1 && dow <= 5) {
      total++;
      if (d <= today) elapsed++;
    }
  }
  return { expectedPct: total > 0 ? Math.round(elapsed / total * 100) : 0, workdaysElapsed: elapsed, workdaysTotal: total };
}

const WEEKS_PER_MONTH = 52 / 12;

/**
 * Computus: Ostersonntag nach dem Anonymous Gregorian Algorithm (Meeus/Jones/Butcher).
 * Exakte Formel für den Gregorianischen Kalender, gültig für alle Jahre ab 1583.
 */
function easterSunday(year: number): Date {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return new Date(year, month - 1, day);
}

function bernHolidays(year: number): { date: string; label: string }[] {
  const easter = easterSunday(year);
  const offset = (days: number) => {
    const d = new Date(easter);
    d.setDate(d.getDate() + days);
    return d;
  };
  const candidates: { d: Date; label: string }[] = [
    { d: new Date(year, 0, 1), label: 'Neujahr' },
    { d: new Date(year, 0, 2), label: 'Berchtoldstag' },
    { d: offset(-2), label: 'Karfreitag' },
    { d: offset(1), label: 'Ostermontag' },
    { d: offset(39), label: 'Auffahrt' },
    { d: offset(50), label: 'Pfingstmontag' },
    { d: new Date(year, 7, 1), label: 'Bundesfeiertag' },
    { d: new Date(year, 11, 25), label: 'Weihnachten' },
    { d: new Date(year, 11, 26), label: 'Stephanstag' },
  ];
  return candidates
    .filter(({ d }) => { const dow = d.getDay(); return dow >= 1 && dow <= 5; })
    .map(({ d, label }) => ({ date: toIso(d), label }));
}

function getColClass(_range: ViewRange): string {
  return 'flex-1 min-w-0';
}

function getTodayOffset(weekStart: Date): number | null {
  const today = new Date();
  const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const weekEnd = new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + 7);
  if (todayMidnight >= weekStart && todayMidnight < weekEnd) {
    const diffDays = Math.round((todayMidnight.getTime() - weekStart.getTime()) / 86400000);
    return Math.round((diffDays / 7) * 100);
  }
  return null;
}

// ── Color utilities ─────────────────────────────────────────────────────────

function hexToHsl(hex: string): [number, number, number] {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return [0, 0, 50];
  const r = parseInt(m[1], 16) / 255;
  const g = parseInt(m[2], 16) / 255;
  const b = parseInt(m[3], 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0;
  const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: h = ((b - r) / d + 2) / 6; break;
      case b: h = ((r - g) / d + 4) / 6; break;
    }
  }
  return [h * 360, s * 100, l * 100];
}

function hslToHex(h: number, s: number, l: number): string {
  s /= 100; l /= 100;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const c = l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1);
    return Math.round(255 * c).toString(16).padStart(2, '0');
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

function muteColor(hex: string): string {
  const [h, s, l] = hexToHsl(hex);
  return hslToHex(h, Math.min(s, 50), l < 40 ? 45 : l > 65 ? 55 : l);
}

function getContrastText(hex: string): string {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return '#ffffff';
  const toLinear = (v: number) => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  const lum = 0.2126 * toLinear(parseInt(m[1], 16) / 255)
            + 0.7152 * toLinear(parseInt(m[2], 16) / 255)
            + 0.0722 * toLinear(parseInt(m[3], 16) / 255);
  return lum > 0.179 ? '#1a1a2e' : '#ffffff';
}

// ── Allocation DnD helpers ───────────────────────────────────────────────────

function DroppableWeekCell({ id, colClass, measureRef, children }: {
  id: string; colClass: string; measureRef?: React.Ref<HTMLDivElement>; children: React.ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id });
  return (
    <div
      ref={(node) => {
        setNodeRef(node);
        if (measureRef && typeof measureRef === 'function') measureRef(node);
        else if (measureRef && 'current' in measureRef) (measureRef as React.MutableRefObject<HTMLDivElement | null>).current = node;
      }}
      className={`relative flex h-full min-h-[44px] ${colClass} items-center justify-center border-r border-gray-100 dark:border-gray-800 ${isOver ? 'ring-2 ring-inset ring-indigo-400 bg-indigo-50/40 dark:bg-indigo-900/20' : ''}`}
    >
      {children}
    </div>
  );
}

function DraggableAllocBlock({ allocId, children, onClick, onContextMenu, className, blockStyle, selected, title }: {
  allocId: string; children: React.ReactNode;
  onClick?: (e: React.MouseEvent) => void;
  onContextMenu?: (e: React.MouseEvent) => void;
  className?: string;
  blockStyle?: React.CSSProperties;
  selected?: boolean;
  title?: string;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: `alloc-${allocId}` });
  const combinedStyle: React.CSSProperties = {
    ...blockStyle,
    ...(transform ? {
      transform: `translate3d(${transform.x}px, 0, 0)`,
      zIndex: 40,
      opacity: 0.7,
    } : {}),
    cursor: 'grab',
  };

  return (
    <div
      ref={setNodeRef}
      style={combinedStyle}
      className={`${className || ''} ${isDragging ? 'shadow-lg' : ''} ${selected ? 'ring-2 ring-white ring-offset-1' : ''}`}
      {...attributes}
      {...listeners}
      title={title}
      onClick={(e) => { if (!isDragging && onClick) { e.stopPropagation(); onClick(e); } }}
      onContextMenu={(e) => { if (onContextMenu) { e.preventDefault(); e.stopPropagation(); onContextMenu(e); } }}
    >
      {children}
    </div>
  );
}

// ── Sortable Project Row ─────────────────────────────────────────────────────

function SortableProjectRow({
  project, weeks, allocations, onCellClick, onContextMenu, onEditProject, colClass, viewRange: _viewRange, onAllocDrop, selectedAllocIds, onToggleSelect, timeOffWeekMap, holidayWeekMap, monthlyData, sollIstExpanded,
}: {
  project: CapProject;
  weeks: Date[];
  allocations: Allocation[];
  onCellClick: (projectId: string, weekStart: string) => void;
  onEditProject: (project: CapProject) => void;
  colClass: string;
  viewRange: ViewRange;
  onContextMenu: (e: React.MouseEvent, alloc: Allocation) => void;
  onAllocDrop: (allocId: string, targetWeekStr: string) => void;
  selectedAllocIds: Set<string>;
  onToggleSelect: (allocId: string) => void;
  timeOffWeekMap: Record<string, number>;
  holidayWeekMap: Record<string, { hours: number; entries: { label: string; dayIndex: number }[] }>;
  monthlyData?: MonthlyActualProject;
  sollIstExpanded: boolean;
}) {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({ id: project.id });
  const style = { transform: CSS.Transform.toString(transform), transition };
  const allocDndSensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }));
  const [cellWidth, setCellWidth] = useState(80);
  const observerRef = useRef<ResizeObserver | null>(null);
  const cellMeasureCallback = useCallback((node: HTMLDivElement | null) => {
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    if (node) {
      observerRef.current = new ResizeObserver(entries => {
        if (entries[0]) setCellWidth(entries[0].contentRect.width);
      });
      observerRef.current.observe(node);
    }
  }, []);

  const showDaySlots = cellWidth >= 20;

  const allocMap = useMemo(() => {
    const map: Record<string, Allocation[]> = {};
    for (const a of allocations) {
      let weekKey: string;
      if (a.allocation_type === 'day') {
        const d = new Date(a.week_start + 'T00:00:00');
        weekKey = toIso(getMonday(d));
      } else {
        weekKey = a.week_start;
      }
      if (!map[weekKey]) map[weekKey] = [];
      map[weekKey].push(a);
    }
    return map;
  }, [allocations]);

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="flex items-stretch border-b border-gray-200 dark:border-gray-700 hover:bg-gray-50/50 dark:hover:bg-gray-800/30"
      data-testid={`capacity-project-row-${project.id}`}
    >
      {/* Projekt-Label */}
      <div
        className="flex w-40 min-w-40 shrink-0 lg:w-64 lg:min-w-64 items-center gap-2 border-r border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-900"
        style={{ borderLeft: `4px solid ${project.color}` }}
      >
        <button
          onClick={() => onEditProject(project)}
          className="flex flex-1 items-center gap-2 min-w-0 rounded-md px-1 py-0.5 transition hover:bg-gray-100 dark:hover:bg-gray-800"
          data-testid="capacity-project-edit-btn"
        >
          <ProjectIcon iconUrl={project.icon_url} iconEmoji={project.icon_emoji} color={project.color} size={20} />
          <div className="flex flex-col min-w-0 text-left">
            <span className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">{project.name}</span>
            {project.client_name && (
              <span className="truncate text-xs text-gray-500 dark:text-gray-400">{project.client_name}</span>
            )}
          </div>
        </button>
        {project.status === 'vorläufig' && (
          <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
            vorl.
          </span>
        )}
        {!project.toggl_project_id && !project.toggl_client_id && (
          <span title="Kein Toggl-Projekt verknüpft — Ist-Vergleich nicht möglich" className="text-gray-400 dark:text-gray-500">
            <Unlink className="h-3 w-3" />
          </span>
        )}
        {!project.project_id && (
          <span title="Kein InnoSmith OS-Board verknüpft — Planungs-Check im Briefing nicht möglich" className="text-amber-400 dark:text-amber-500">
            <Link2Off className="h-3 w-3" />
          </span>
        )}
        <button {...attributes} {...listeners} className="cursor-grab text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" data-testid="capacity-project-drag">
          <GripVertical className="h-4 w-4" />
        </button>
      </div>

      {/* Soll/Ist-Spalte */}
      <div
        className={`shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/30 flex items-center ${sollIstExpanded ? 'px-2 py-1' : 'px-0.5 py-1'}`}
        style={{ width: sollIstExpanded ? 120 : 32 }}
      >
        {monthlyData ? (
          sollIstExpanded ? (() => {
            const actualPct = monthlyData.planned_minutes > 0
              ? Math.round((monthlyData.actual_minutes / monthlyData.planned_minutes) * 100)
              : 0;
            const barPct = Math.min(actualPct, 100);
            const deltaMin = monthlyData.actual_minutes - monthlyData.planned_minutes;
            const pacing = getMonthPacing();
            const isBillable = project.is_billable;
            const behindPace = isBillable && monthlyData.planned_minutes > 0 && actualPct < pacing.expectedPct - 10;
            const fulfilled = deltaMin >= 0 && monthlyData.planned_minutes > 0;
            const overFulfilled = isBillable && monthlyData.planned_minutes > 0 && monthlyData.actual_minutes > monthlyData.planned_minutes * 1.2;

            const barColor = behindPace
              ? 'bg-amber-500 dark:bg-amber-400'
              : 'bg-emerald-500 dark:bg-emerald-400';
            const deltaColor = overFulfilled
              ? 'text-amber-600 dark:text-amber-400'
              : fulfilled
                ? 'text-emerald-600 dark:text-emerald-400'
                : 'text-gray-500 dark:text-gray-400';

            return (
              <div className="flex flex-col w-full gap-0.5">
                <div className="relative h-1.5 w-full rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${barColor}`}
                    style={{ width: `${barPct}%` }}
                  />
                  {monthlyData.planned_minutes > 0 && pacing.expectedPct < 100 && (
                    <div
                      className="absolute top-0 bottom-0 w-[2px] bg-gray-900/30 dark:bg-white/30"
                      style={{ left: `${pacing.expectedPct}%` }}
                      title={`${pacing.expectedPct}% des Monats verstrichen, ${actualPct}% geleistet`}
                    />
                  )}
                </div>
                <div className="flex items-center justify-between gap-0.5">
                  <span className="text-[10px] font-medium text-gray-700 dark:text-gray-300 truncate flex items-center gap-0.5">
                    {fulfilled && <Check className="h-2.5 w-2.5 text-emerald-600 dark:text-emerald-400 shrink-0" />}
                    {Math.round(monthlyData.actual_minutes / 60)}/{Math.round(monthlyData.planned_minutes / 60)}h
                  </span>
                  <span className={`text-[9px] font-medium flex items-center gap-0.5 shrink-0 ${deltaColor}`}>
                    {overFulfilled && <AlertTriangle className="h-2.5 w-2.5 shrink-0" />}
                    {deltaMin >= 0 ? '+' : ''}{Math.round(deltaMin / 60)}h
                  </span>
                </div>
                {monthlyData.prev_month_planned != null && monthlyData.prev_month_planned > 0 && (
                  <span className="text-[8px] text-gray-400 dark:text-gray-500 truncate">
                    Vm: {Math.round((monthlyData.prev_month_actual ?? 0) / 60)}/{Math.round(monthlyData.prev_month_planned / 60)}h
                  </span>
                )}
              </div>
            );
          })() : (() => {
            const actualPct = monthlyData.planned_minutes > 0
              ? Math.round((monthlyData.actual_minutes / monthlyData.planned_minutes) * 100)
              : 0;
            const barPct = Math.min(actualPct, 100);
            const pacing = getMonthPacing();
            const isBillableC = project.is_billable;
            const behindPace = isBillableC && monthlyData.planned_minutes > 0 && actualPct < pacing.expectedPct - 10;
            const fulfilled = monthlyData.actual_minutes >= monthlyData.planned_minutes && monthlyData.planned_minutes > 0;
            const barColor = behindPace
              ? 'bg-amber-500 dark:bg-amber-400'
              : 'bg-emerald-500 dark:bg-emerald-400';
            return (
              <div className="flex flex-col items-center w-full gap-0.5" title={`${Math.round(monthlyData.actual_minutes / 60)}/${Math.round(monthlyData.planned_minutes / 60)}h`}>
                <span className={`text-[8px] font-bold ${fulfilled ? 'text-emerald-600 dark:text-emerald-400' : behindPace ? 'text-amber-600 dark:text-amber-400' : 'text-gray-600 dark:text-gray-400'}`}>
                  {monthlyData.planned_minutes > 0 ? (fulfilled ? '\u2713' : `${Math.min(actualPct, 999)}%`) : '\u2013'}
                </span>
                <div className="w-1.5 h-4 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
                  <div
                    className={`w-full rounded-full ${barColor}`}
                    style={{ height: `${barPct}%` }}
                  />
                </div>
              </div>
            );
          })()
        ) : (
          <span className="text-[8px] text-gray-300 dark:text-gray-600 w-full text-center">{'\u2013'}</span>
        )}
      </div>

      {/* Wochen-Zellen mit Allocation-DnD */}
      <DndContext
        sensors={allocDndSensors}
        modifiers={[restrictToHorizontalAxis]}
        onDragEnd={(event) => {
          const { active, over } = event;
          if (!over) return;
          const allocId = String(active.id).replace('alloc-', '');
          const targetWeek = String(over.id).replace(`drop-${project.id}-`, '');
          if (targetWeek && allocId) onAllocDrop(allocId, targetWeek);
        }}
      >
        <div className="flex flex-1">
          {weeks.map((week, idx) => {
            const weekStr = toIso(week);
            const weekAllocs = allocMap[weekStr];
            const totalMin = weekAllocs ? weekAllocs.reduce((s, a) => s + a.minutes, 0) : 0;
            const weekOnlyAllocs = weekAllocs?.filter(a => a.allocation_type !== 'day') || [];
            const dayAllocs = weekAllocs?.filter(a => a.allocation_type === 'day') || [];
            const todayOffset = getTodayOffset(week);
            return (
              <DroppableWeekCell key={weekStr} id={`drop-${project.id}-${weekStr}`} colClass={colClass} measureRef={idx === 0 ? cellMeasureCallback : undefined}>
                {/* Klick-Hintergrund: nur für leere Zellen → Dialog öffnen */}
                <div
                  className="absolute inset-0 cursor-pointer"
                  onClick={() => { if (!weekOnlyAllocs.length) onCellClick(project.id, weekStr); }}
                  data-testid={`capacity-cell-${project.id}-${weekStr}`}
                />
                {/* Wochen-Allocations als draggable Block */}
                {weekOnlyAllocs.length > 0 && (() => {
                  const isSeries = !!weekOnlyAllocs[0].series_id;
                  const plannedMin = weekOnlyAllocs.reduce((s, a) => s + a.minutes, 0);
                  return (
                    <DraggableAllocBlock
                      allocId={weekOnlyAllocs[0].id}
                      className={`absolute inset-0.5 rounded-md flex flex-col items-center justify-center font-medium transition-all hover:scale-[1.02] ${cellWidth < 40 ? 'text-[8px]' : cellWidth < 60 ? 'text-[9px]' : 'text-xs'}`}
                      blockStyle={{
                        backgroundColor: project.status === 'vorläufig' ? `${muteColor(project.color)}8C` : muteColor(project.color),
                        border: project.status === 'vorläufig' ? `2px dashed ${muteColor(project.color)}` : 'none',
                        color: getContrastText(muteColor(project.color)),
                      }}
                      selected={selectedAllocIds.has(weekOnlyAllocs[0].id)}
                      title={minutesToDisplay(plannedMin)}
                      onClick={(e) => {
                        if (e.ctrlKey || e.metaKey) { onToggleSelect(weekOnlyAllocs[0].id); }
                        else { onContextMenu(e, weekOnlyAllocs[0]); }
                      }}
                      onContextMenu={(e) => onContextMenu(e, weekOnlyAllocs[0])}
                    >
                      <span>
                        {cellWidth >= 40 ? (cellWidth >= 60 ? minutesToDisplay(plannedMin) : `${Math.round(plannedMin / 60)}h`) : ''}
                      </span>
                      {isSeries && <span className="absolute bottom-0.5 left-1/2 -translate-x-1/2 w-3 h-[3px] rounded-full bg-white/60" />}
                    </DraggableAllocBlock>
                  );
                })()}

                {/* Tages-Allocations als schmale positionierte Blöcke */}
                {dayAllocs.length > 0 && showDaySlots && dayAllocs.map(da => {
                  const slotStyle = getDaySlotStyle(da.week_start);
                  return (
                    <DraggableAllocBlock
                      key={da.id}
                      allocId={da.id}
                      className="absolute top-0.5 bottom-0.5 rounded-sm flex items-center justify-center font-medium text-[8px]"
                      blockStyle={{
                        left: slotStyle.left,
                        width: slotStyle.width,
                        backgroundColor: project.status === 'vorläufig' ? `${muteColor(project.color)}8C` : muteColor(project.color),
                        border: `1px solid ${muteColor(project.color)}`,
                        color: getContrastText(muteColor(project.color)),
                      }}
                      selected={selectedAllocIds.has(da.id)}
                      title={`${new Date(da.week_start + 'T00:00:00').toLocaleDateString('de-CH', { weekday: 'short', day: 'numeric', month: 'short' })}: ${minutesToDisplay(da.minutes)}`}
                      onClick={(e) => {
                        if (e.ctrlKey || e.metaKey) { onToggleSelect(da.id); }
                        else { onContextMenu(e, da); }
                      }}
                      onContextMenu={(e) => onContextMenu(e, da)}
                    >
                      <span>
                        {cellWidth >= 60 ? `${Math.round(da.minutes / 60)}` : ''}
                      </span>
                      {!!da.series_id && <span className="absolute bottom-0 left-1/2 -translate-x-1/2 w-2 h-[2px] rounded-full bg-white/60" />}
                    </DraggableAllocBlock>
                  );
                })}

                {/* Tages-Allocations aggregiert (wenn Zelle zu klein) */}
                {dayAllocs.length > 0 && !showDaySlots && weekOnlyAllocs.length === 0 && (
                  <DraggableAllocBlock
                    allocId={dayAllocs[0].id}
                    className={`absolute inset-0.5 rounded-md flex items-center justify-center font-medium ${cellWidth < 40 ? 'text-[8px]' : 'text-[9px]'}`}
                    blockStyle={{
                      backgroundColor: muteColor(project.color),
                      color: getContrastText(muteColor(project.color)),
                    }}
                    selected={selectedAllocIds.has(dayAllocs[0].id)}
                    title={`${dayAllocs.length} Einzeltag(e): ${minutesToDisplay(totalMin)}`}
                    onClick={(e) => {
                      if (e.ctrlKey || e.metaKey) { onToggleSelect(dayAllocs[0].id); }
                      else { onContextMenu(e, dayAllocs[0]); }
                    }}
                    onContextMenu={(e) => onContextMenu(e, dayAllocs[0])}
                  >
                    <span>{cellWidth >= 25 ? `${Math.round(totalMin / 60)}h` : ''}</span>
                  </DraggableAllocBlock>
                )}

                {todayOffset !== null && (
                  <div className="absolute top-0 bottom-0 w-[2px] bg-red-500 dark:bg-red-400 z-10 pointer-events-none" style={{ left: `${todayOffset}%` }} />
                )}

                {/* Ferien-Overlay */}
                {(() => {
                  const totalOff = timeOffWeekMap[weekStr] || 0;
                  if (totalOff >= 40) {
                    return (
                      <div
                        className="absolute inset-0 z-20 pointer-events-none rounded-md bg-black/30 dark:bg-black/40"
                        style={{ backgroundImage: 'repeating-linear-gradient(135deg, transparent, transparent 4px, rgba(0,0,0,0.15) 4px, rgba(0,0,0,0.15) 8px)' }}
                        title={`Ferien: ${totalOff}h — keine Kapazität verfügbar`}
                      />
                    );
                  }
                  return null;
                })()}
                {/* Ferien-Teilwoche (manuelle Abwesenheit, nicht Feiertag) */}
                {(() => {
                  const totalOff = timeOffWeekMap[weekStr] || 0;
                  if (totalOff >= 40) return null;
                  const manualOff = totalOff - (holidayWeekMap[weekStr]?.hours || 0);
                  if (manualOff <= 0) return null;
                  return (
                    <div
                      className="absolute inset-x-0 top-0 h-1.5 z-20 pointer-events-none bg-amber-500/80 dark:bg-amber-400/70 rounded-t-md"
                      title={`Ferien: ${manualOff}h — reduzierte Kapazität`}
                    />
                  );
                })()}
                {/* Feiertags-Markierungen (tagesgenau, gleiche Schraffur wie Ferien) */}
                {holidayWeekMap[weekStr]?.entries.map((entry, i) => (
                  <div
                    key={`hol-${i}`}
                    className="absolute top-0 bottom-0 z-20 pointer-events-none bg-black/30 dark:bg-black/40 rounded-sm"
                    style={{ left: `${(entry.dayIndex / 5) * 100}%`, width: '20%', backgroundImage: 'repeating-linear-gradient(135deg, transparent, transparent 4px, rgba(0,0,0,0.15) 4px, rgba(0,0,0,0.15) 8px)' }}
                    title={entry.label}
                  />
                ))}
              </DroppableWeekCell>
            );
          })}
        </div>
      </DndContext>
    </div>
  );
}

// ── Allocation Dialog ────────────────────────────────────────────────────────

function getDaySlotStyle(dateStr: string): { left: string; width: string } {
  const d = new Date(dateStr + 'T00:00:00');
  const dayIndex = (d.getDay() + 6) % 7; // Mo=0, Di=1, ..., Sa=5
  return { left: `${(dayIndex / 7) * 100}%`, width: '14.28%' };
}

function MiniCalendar({ month, year, selectedDays, onToggleDay }: {
  month: number; year: number;
  selectedDays: Set<string>;
  onToggleDay: (iso: string) => void;
}) {
  if (!Number.isFinite(year) || !Number.isFinite(month)) return null;
  const firstDay = new Date(year, month, 1);
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const startWeekday = (firstDay.getDay() + 6) % 7; // Mo=0
  const cells: (number | null)[] = Array(startWeekday).fill(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  const monthLabel = firstDay.toLocaleDateString('de-CH', { month: 'long', year: 'numeric' });

  return (
    <div className="select-none">
      <div className="mb-1 text-center text-xs font-semibold text-gray-700 dark:text-gray-300">{monthLabel}</div>
      <div className="grid grid-cols-7 gap-0.5 text-center text-[10px] font-medium text-gray-400 dark:text-gray-500 mb-0.5">
        {['Mo','Di','Mi','Do','Fr','Sa','So'].map(d => <div key={d}>{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-0.5">
        {cells.map((day, i) => {
          if (day === null) return <div key={`e-${i}`} />;
          const d = new Date(year, month, day);
          const iso = toIso(d);
          const isSunday = d.getDay() === 0;
          const isSelected = selectedDays.has(iso);
          return (
            <button
              key={iso}
              type="button"
              disabled={isSunday}
              onClick={() => onToggleDay(iso)}
              className={`h-6 w-6 rounded text-[11px] font-medium transition
                ${isSunday ? 'text-gray-300 dark:text-gray-600 cursor-not-allowed' : 'cursor-pointer hover:bg-indigo-100 dark:hover:bg-indigo-900/30'}
                ${isSelected ? 'bg-indigo-600 text-white hover:bg-indigo-700' : ''}`}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function AllocationDialog({
  open, onClose, onSave, projects, initialProjectId, initialWeek, editAllocId, editMinutes, editScope, editAllocationType,
}: {
  open: boolean;
  onClose: () => void;
  onSave: (data: {
    capacity_project_id: string;
    week_start: string;
    minutes: number;
    allocation_type: 'week' | 'day';
    repeat: boolean;
    end_date?: string;
    interval_weeks?: number;
    extra_days?: string[];
  }) => void;
  projects: CapProject[];
  initialProjectId: string;
  initialWeek: string;
  editAllocId?: string;
  editMinutes?: number;
  editScope?: 'single' | 'series' | 'series_from';
  editAllocationType?: 'week' | 'day';
}) {
  const [mode, setMode] = useState<'week' | 'calendar'>('week');
  const [projectId, setProjectId] = useState(initialProjectId);
  const [hoursInput, setHoursInput] = useState('8');
  const [minutesInput, setMinutesInput] = useState('0');
  const [repeat, setRepeat] = useState(false);
  const [repeatMode, setRepeatMode] = useState<'count' | 'date'>('count');
  const [repeatCount, setRepeatCount] = useState(4);
  const [endDate, setEndDate] = useState('');
  const [intervalWeeks, setIntervalWeeks] = useState(1);
  const [monthlyTargetMode, setMonthlyTargetMode] = useState(false);
  const [monthlyTargetInput, setMonthlyTargetInput] = useState('');

  // Calendar mode state
  const [selectedDays, setSelectedDays] = useState<Set<string>>(new Set());
  const [calMonth, setCalMonth] = useState(() => {
    const d = new Date(initialWeek + 'T00:00:00');
    return { month: d.getMonth(), year: d.getFullYear() };
  });
  const [dayHours, setDayHours] = useState('8');

  useEffect(() => {
    setProjectId(initialProjectId);
    const isDay = editAllocationType === 'day';
    setMode(isDay ? 'calendar' : 'week');
    if (editMinutes != null) {
      if (isDay) {
        setDayHours(String(Math.floor(editMinutes / 60)));
      } else {
        setHoursInput(String(Math.floor(editMinutes / 60)));
        setMinutesInput(String(editMinutes % 60));
      }
    } else {
      setHoursInput('8');
      setMinutesInput('0');
      setDayHours('8');
    }
    setRepeat(false);
    setRepeatMode('count');
    setRepeatCount(4);
    setEndDate('');
    setIntervalWeeks(1);
    setMonthlyTargetMode(false);
    setMonthlyTargetInput('');
    setSelectedDays(isDay && initialWeek ? new Set([initialWeek]) : new Set());
    const d = new Date(initialWeek + 'T00:00:00');
    setCalMonth({ month: d.getMonth(), year: d.getFullYear() });
  }, [initialProjectId, initialWeek, editMinutes, editAllocationType]);

  const totalMinutes = useMemo(() => {
    return (parseInt(hoursInput) || 0) * 60 + (parseInt(minutesInput) || 0);
  }, [hoursInput, minutesInput]);

  const dayTotalMinutes = useMemo(() => {
    return (parseInt(dayHours) || 0) * 60;
  }, [dayHours]);

  const toggleDay = useCallback((iso: string) => {
    setSelectedDays(prev => {
      const next = new Set(prev);
      if (next.has(iso)) next.delete(iso); else next.add(iso);
      return next;
    });
  }, []);

  const nextMonth = useMemo(() => {
    const m = calMonth.month + 1;
    return m > 11 ? { month: 0, year: calMonth.year + 1 } : { month: m, year: calMonth.year };
  }, [calMonth]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm modal-safe" data-testid="capacity-alloc-dialog">
      <div className={`rounded-xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-gray-700 dark:bg-gray-900 ${mode === 'calendar' ? 'w-full max-w-lg' : 'w-full max-w-sm'}`}>
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-100">
            {editAllocId
              ? editScope === 'series' ? 'Ganze Serie ändern'
                : editScope === 'series_from' ? 'Serie ab hier ändern'
                : mode === 'calendar' ? 'Einzeltag ändern' : 'Stunden ändern'
              : 'Kapazität planen'}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" data-testid="capacity-dialog-close">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Projekt */}
        <div className="mb-4">
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Projekt</label>
          <select
            value={projectId}
            onChange={e => setProjectId(e.target.value)}
            disabled={!!editAllocId}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
            data-testid="capacity-dialog-project"
          >
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name}{p.client_name ? ` (${p.client_name})` : ''}</option>
            ))}
          </select>
        </div>

        {/* Modus-Umschalter */}
        {!(editAllocId && editScope !== 'single') && (
          <div className="mb-4 flex rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden" data-testid="capacity-dialog-toggle-mode">
            <button
              onClick={() => setMode('week')}
              className={`flex-1 px-3 py-1.5 text-xs font-medium transition ${mode === 'week' ? 'bg-indigo-600 text-white' : 'bg-gray-50 text-gray-600 hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'}`}
            >
              Woche
            </button>
            <button
              onClick={() => setMode('calendar')}
              className={`flex-1 px-3 py-1.5 text-xs font-medium transition ${mode === 'calendar' ? 'bg-indigo-600 text-white' : 'bg-gray-50 text-gray-600 hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'}`}
            >
              Einzeltage
            </button>
          </div>
        )}

        {mode === 'week' ? (
          <>
            {/* Wochenmodus: Stunden pro Woche */}
            <div className="mb-4">
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Stunden pro Woche</label>
              <div className="flex items-center gap-2">
                <input
                  type="number" min="0" max="80" value={hoursInput}
                  onChange={e => setHoursInput(e.target.value)}
                  className="w-20 rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                  data-testid="capacity-dialog-hours"
                />
                <span className="text-sm text-gray-500">h</span>
                <input
                  type="number" min="0" max="59" value={minutesInput}
                  onChange={e => setMinutesInput(e.target.value)}
                  className="w-20 rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                  data-testid="capacity-dialog-minutes"
                />
                <span className="text-sm text-gray-500">min</span>
              </div>
              {totalMinutes > 0 && (
                <div className="mt-2 rounded-lg bg-indigo-50 px-3 py-1.5 text-xs text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300">
                  <div>{minutesToDisplay(totalMinutes)}/Woche = {Math.round(totalMinutes / 24)}% Auslastung</div>
                  <div className="mt-0.5 text-indigo-600/80 dark:text-indigo-400/80">
                    ≈ {minutesToDisplay(Math.round(totalMinutes * WEEKS_PER_MONTH))}/Monat (Ø 52/12 Wochen)
                  </div>
                </div>
              )}
              <div className="mt-2">
                <button
                  type="button"
                  onClick={() => setMonthlyTargetMode(!monthlyTargetMode)}
                  className="text-[11px] text-indigo-600 hover:text-indigo-800 dark:text-indigo-400 dark:hover:text-indigo-300 underline underline-offset-2"
                >
                  {monthlyTargetMode ? 'Manuell eingeben' : 'Aus Monatsziel berechnen'}
                </button>
                {monthlyTargetMode && (
                  <div className="mt-1.5 flex items-center gap-2">
                    <input
                      type="number" min="0" max="320" step="0.5"
                      placeholder="z.B. 14"
                      value={monthlyTargetInput}
                      onChange={e => {
                        const val = e.target.value;
                        setMonthlyTargetInput(val);
                        const monthly = parseFloat(val);
                        if (monthly > 0) {
                          const weeklyMin = Math.round(monthly * 60 * 12 / 52);
                          setHoursInput(String(Math.floor(weeklyMin / 60)));
                          setMinutesInput(String(weeklyMin % 60));
                        }
                      }}
                      className="w-20 rounded-lg border border-indigo-300 px-2 py-1 text-sm dark:border-indigo-600 dark:bg-gray-800 dark:text-gray-200"
                      data-testid="capacity-dialog-monthly-target"
                    />
                    <span className="text-xs text-gray-500 dark:text-gray-400">h/Monat</span>
                  </div>
                )}
              </div>
            </div>

            {/* Wiederholung — im Edit-Modus ausgeblendet */}
            {!editAllocId && <div className="mb-4">
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input type="checkbox" checked={repeat} onChange={e => setRepeat(e.target.checked)} className="rounded border-gray-300" data-testid="capacity-dialog-repeat" />
                Wiederholen
              </label>
              {repeat && (
                <div className="mt-2 space-y-2 pl-6">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-500">Alle</span>
                    <input type="number" min="1" max="4" value={intervalWeeks} onChange={e => setIntervalWeeks(parseInt(e.target.value) || 1)}
                      className="w-14 rounded-lg border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200" data-testid="capacity-dialog-interval" />
                    <span className="text-xs text-gray-500">Woche(n)</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400 cursor-pointer">
                      <input type="radio" name="repeatMode" checked={repeatMode === 'count'} onChange={() => setRepeatMode('count')} className="text-indigo-600" />
                      <input type="number" min="2" max="52" value={repeatCount} onChange={e => setRepeatCount(parseInt(e.target.value) || 2)}
                        className="w-12 rounded border border-gray-300 px-1.5 py-0.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200" data-testid="capacity-dialog-count" />
                      <span>mal</span>
                    </label>
                    <label className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400 cursor-pointer">
                      <input type="radio" name="repeatMode" checked={repeatMode === 'date'} onChange={() => setRepeatMode('date')} className="text-indigo-600" />
                      <span>bis</span>
                      <input type="date" value={endDate} min={initialWeek}
                        onChange={e => { setEndDate(e.target.value); setRepeatMode('date'); }}
                        className="rounded border border-gray-300 px-1.5 py-0.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200" data-testid="capacity-dialog-end-date" />
                    </label>
                  </div>
                </div>
              )}
            </div>}

            {/* Aktionen */}
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                Abbrechen
              </button>
              <button
                onClick={() => {
                  if (totalMinutes <= 0 || !projectId) return;
                  let computedEndDate = endDate || undefined;
                  if (repeat && repeatMode === 'count' && !computedEndDate) {
                    const start = new Date(initialWeek + 'T00:00:00');
                    const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + (repeatCount - 1) * intervalWeeks * 7);
                    computedEndDate = toIso(end);
                  }
                  onSave({
                    capacity_project_id: projectId,
                    week_start: initialWeek,
                    minutes: totalMinutes,
                    allocation_type: 'week',
                    repeat,
                    end_date: computedEndDate,
                    interval_weeks: intervalWeeks,
                  });
                }}
                disabled={totalMinutes <= 0}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                data-testid="capacity-dialog-save"
              >
                {editAllocId ? 'Übernehmen' : 'Speichern'}
              </button>
            </div>
          </>
        ) : (
          <>
            {/* Kalendermodus: Tage auswählen */}
            <div className="mb-4">
              <div className="flex items-center justify-between mb-2">
                <button onClick={() => setCalMonth(p => {
                  const m = p.month - 1;
                  return m < 0 ? { month: 11, year: p.year - 1 } : { month: m, year: p.year };
                })} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <button onClick={() => setCalMonth(p => {
                  const m = p.month + 1;
                  return m > 11 ? { month: 0, year: p.year + 1 } : { month: m, year: p.year };
                })} className="p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <MiniCalendar month={calMonth.month} year={calMonth.year} selectedDays={selectedDays} onToggleDay={toggleDay} />
                <MiniCalendar month={nextMonth.month} year={nextMonth.year} selectedDays={selectedDays} onToggleDay={toggleDay} />
              </div>
              {selectedDays.size > 0 && (
                <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  {selectedDays.size} Tag{selectedDays.size > 1 ? 'e' : ''} gewählt
                </div>
              )}
            </div>

            {/* Stunden pro Tag */}
            <div className="mb-4">
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Stunden pro Tag</label>
              <div className="flex items-center gap-2">
                <input type="number" min="1" max="12" value={dayHours} onChange={e => setDayHours(e.target.value)}
                  className="w-20 rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200" data-testid="capacity-dialog-day-hours" />
                <span className="text-sm text-gray-500">h</span>
              </div>
              {selectedDays.size > 0 && dayTotalMinutes > 0 && (
                <div className="mt-2 rounded-lg bg-purple-50 px-3 py-1.5 text-xs text-purple-700 dark:bg-purple-900/20 dark:text-purple-300">
                  Total: {minutesToDisplay(selectedDays.size * dayTotalMinutes)} ({selectedDays.size} × {dayHours}h)
                </div>
              )}
            </div>

            {/* Wiederholung im Kalendermodus */}
            <div className="mb-4">
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input type="checkbox" checked={repeat} onChange={e => setRepeat(e.target.checked)} className="rounded border-gray-300" />
                Wöchentlich wiederholen
              </label>
              {repeat && (
                <div className="mt-2 flex items-center gap-2 pl-6">
                  <span className="text-xs text-gray-500">Bis</span>
                  <input type="date" value={endDate} min={[...selectedDays].sort()[0] || initialWeek}
                    onChange={e => setEndDate(e.target.value)}
                    className="rounded-lg border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200" />
                </div>
              )}
            </div>

            {/* Aktionen */}
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                Abbrechen
              </button>
              <button
                onClick={() => {
                  if (selectedDays.size === 0 || dayTotalMinutes <= 0 || !projectId) return;
                  const sortedDays = [...selectedDays].sort();
                  if (editAllocId) {
                    const editDay = sortedDays.find(d => d === initialWeek) ?? sortedDays[0];
                    const extraDays = sortedDays.filter(d => d !== editDay);
                    onSave({
                      capacity_project_id: projectId,
                      week_start: editDay,
                      minutes: dayTotalMinutes,
                      allocation_type: 'day',
                      repeat: false,
                      extra_days: extraDays.length > 0 ? extraDays : undefined,
                    });
                  } else {
                    const [firstDay, ...restDays] = sortedDays;
                    onSave({
                      capacity_project_id: projectId,
                      week_start: firstDay,
                      minutes: dayTotalMinutes,
                      allocation_type: 'day',
                      repeat: false,
                      extra_days: restDays.length > 0 ? restDays : undefined,
                    });
                  }
                }}
                disabled={selectedDays.size === 0 || dayTotalMinutes <= 0}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                data-testid="capacity-dialog-save-days"
              >
                {editAllocId
                  ? (selectedDays.size > 1 ? `Ändern + ${selectedDays.size - 1} neue(n) Tag(e)` : 'Übernehmen')
                  : (selectedDays.size > 1 ? `${selectedDays.size} Tage speichern` : 'Speichern')}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Project Dialog ───────────────────────────────────────────────────────────

interface AvailableProjectOption {
  name: string;
  source: 'both' | 'toggl' | 'taskpilot';
  toggl_project_id: number | null;
  project_id: string | null;
  icon_url: string | null;
  icon_emoji: string | null;
  color: string;
  client_name: string | null;
  billable: boolean;
}

function ProjectDialog({
  open, onClose, onSave, onDelete, initial, appLogoUrl,
}: {
  open: boolean;
  onClose: () => void;
  onSave: (data: Partial<CapProject> & { toggl_project_id?: number }) => void;
  onDelete?: () => void;
  initial?: CapProject | null;
  appLogoUrl?: string | null;
}) {
  const [name, setName] = useState(initial?.name || '');
  const [color, setColor] = useState(initial?.color || '#3B82F6');
  const [clientName, setClientName] = useState(initial?.client_name || '');
  const [hourlyRate, setHourlyRate] = useState(initial?.hourly_rate?.toString() || '');
  const [isBillable, setIsBillable] = useState(initial?.is_billable ?? true);
  const [status, setStatus] = useState<'bestätigt' | 'vorläufig'>(initial?.status || 'bestätigt');
  const [notes, setNotes] = useState(initial?.notes || '');
  const [iconEmoji, setIconEmoji] = useState(initial?.icon_emoji || '');
  const [iconUrl, setIconUrl] = useState(initial?.icon_url || '');
  const [projectId, setProjectId] = useState<string | null>(initial?.project_id || null);
  const [togglProjectId, setTogglProjectId] = useState<number | null>(initial?.toggl_project_id || null);
  const [togglClientId, setTogglClientId] = useState<number | null>(initial?.toggl_client_id || null);
  const [togglBillableFilter, setTogglBillableFilter] = useState<string | null>(initial?.toggl_billable_filter || null);

  const [available, setAvailable] = useState<AvailableProjectOption[]>([]);
  const [loadingAvail, setLoadingAvail] = useState(false);
  const [defaultRate, setDefaultRate] = useState<number | null>(null);
  const [togglClients, setTogglClients] = useState<{ id: number; name: string }[]>([]);
  const [boards, setBoards] = useState<{ id: string; name: string }[]>([]);
  const [filter, setFilter] = useState('');
  const [showPicker, setShowPicker] = useState(!initial);
  const [iconPickerOpen, setIconPickerOpen] = useState(false);

  useEffect(() => {
    setName(initial?.name || '');
    setColor(initial?.color || '#3B82F6');
    setClientName(initial?.client_name || '');
    setHourlyRate(initial?.hourly_rate?.toString() || '');
    setIsBillable(initial?.is_billable ?? true);
    setStatus(initial?.status || 'bestätigt');
    setNotes(initial?.notes || '');
    setIconEmoji(initial?.icon_emoji || '');
    setIconUrl(initial?.icon_url || '');
    setProjectId(initial?.project_id || null);
    setTogglProjectId(initial?.toggl_project_id || null);
    setTogglClientId(initial?.toggl_client_id || null);
    setTogglBillableFilter(initial?.toggl_billable_filter || null);
    setShowPicker(!initial);
    setFilter('');
  }, [initial]);

  useEffect(() => {
    if (open && !initial && available.length === 0) {
      setLoadingAvail(true);
      api.get<AvailableProjectOption[]>('/api/capacity/available-projects')
        .then(setAvailable)
        .catch(() => {})
        .finally(() => setLoadingAvail(false));
    }
  }, [open, initial]);

  useEffect(() => {
    if (!open) return;
    api.get<{ default_hourly_rate?: number | null }>('/api/settings')
      .then(s => setDefaultRate(s.default_hourly_rate ?? null))
      .catch(() => {});
  }, [open]);

  useEffect(() => {
    if (open && togglClients.length === 0) {
      api.get<{ id: number; name: string }[]>('/api/capacity/toggl-clients')
        .then(setTogglClients)
        .catch(() => {});
    }
  }, [open]);

  // TaskPilot-Boards für die Verknüpfung laden (Basis für Planungs-Check im Briefing).
  useEffect(() => {
    if (open && boards.length === 0) {
      api.get<{ id: string; name: string }[]>('/api/projects')
        .then((ps) => setBoards(ps.map((p) => ({ id: p.id, name: p.name }))))
        .catch(() => {});
    }
  }, [open]);

  const filtered = useMemo(() => {
    if (!filter) return available;
    const term = filter.toLowerCase();
    return available.filter(p =>
      p.name.toLowerCase().includes(term) || (p.client_name || '').toLowerCase().includes(term)
    );
  }, [available, filter]);

  if (!open) return null;

  const colors = ['#3B82F6', '#10B981', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#6366F1', '#84CC16', '#F97316'];

  const sourceBadge = (src: string) => {
    if (src === 'both') return <span className="rounded bg-emerald-100 px-1 py-0.5 text-[9px] font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">Toggl + InnoSmith OS</span>;
    if (src === 'toggl') return <span className="rounded bg-purple-100 px-1 py-0.5 text-[9px] font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">Toggl</span>;
    return <span className="rounded bg-blue-100 px-1 py-0.5 text-[9px] font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">InnoSmith OS</span>;
  };

  const handleSelectAvailable = (p: AvailableProjectOption) => {
    setName(p.name);
    setColor(p.color);
    setIconUrl(p.icon_url || '');
    setIconEmoji(p.icon_emoji || '');
    setClientName(p.client_name || '');
    setIsBillable(p.billable);
    setProjectId(p.project_id);
    setTogglProjectId(p.toggl_project_id);
    setShowPicker(false);
  };

  const handleIconUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    try {
      const token = localStorage.getItem('taskpilot_token');
      const res = await fetch('/api/uploads/icons', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      if (!res.ok) return;
      const { url } = await res.json();
      setIconUrl(url);
      setIconEmoji('');
    } catch { /* ignore */ }
  };

  const handleSave = () => {
    if (!name.trim()) return;
    let finalIconUrl = iconUrl || null;
    if (!finalIconUrl && !iconEmoji && !isBillable && appLogoUrl) {
      finalIconUrl = appLogoUrl;
    }
    onSave({
      name: name.trim(),
      color,
      client_name: clientName || null,
      hourly_rate: hourlyRate ? parseFloat(hourlyRate) : null,
      is_billable: isBillable,
      status,
      notes: notes || null,
      icon_emoji: iconEmoji || null,
      icon_url: finalIconUrl,
      project_id: projectId,
      toggl_project_id: togglProjectId ?? undefined,
      toggl_client_id: togglClientId ?? undefined,
      toggl_billable_filter: togglBillableFilter ?? undefined,
    } as Partial<CapProject> & { toggl_project_id?: number });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm modal-safe" data-testid="capacity-project-dialog">
      <div className="w-full max-w-lg overflow-y-auto rounded-xl border border-gray-200 bg-white p-6 shadow-2xl max-h-full lg:max-h-[90vh] dark:border-gray-700 dark:bg-gray-900">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-100">
            {initial ? 'Projekt bearbeiten' : 'Kapazitätsprojekt'}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Projekt-Picker (nur bei Neuanlage) */}
        {!initial && showPicker && (
          <div className="mb-4 space-y-2">
            <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Bestehendes Projekt übernehmen</label>
            <input
              type="text"
              placeholder="Projekt suchen (Toggl + InnoSmith OS)..."
              value={filter}
              onChange={e => setFilter(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
              data-testid="capacity-project-search"
              autoFocus
            />
            <div className="max-h-48 overflow-y-auto rounded-lg border border-gray-200 dark:border-gray-700">
              {loadingAvail ? (
                <div className="flex items-center justify-center py-6">
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
                </div>
              ) : filtered.length === 0 ? (
                <p className="px-3 py-4 text-center text-xs text-gray-400">Keine Projekte gefunden</p>
              ) : (
                filtered.map((p, i) => (
                  <button
                    key={`${p.source}-${p.name}-${i}`}
                    onClick={() => handleSelectAvailable(p)}
                    className="flex w-full items-center gap-2 border-b border-gray-100 px-3 py-2 text-left text-sm transition last:border-b-0 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50"
                    data-testid={`capacity-avail-${i}`}
                  >
                    <ProjectIcon iconUrl={p.icon_url} iconEmoji={p.icon_emoji} color={p.color} size={20} />
                    <span className="flex-1 truncate font-medium text-gray-800 dark:text-gray-200">{p.name}</span>
                    {p.client_name && <span className="text-xs text-gray-400 mr-1">{p.client_name}</span>}
                    {sourceBadge(p.source)}
                  </button>
                ))
              )}
            </div>
            <div className="border-t border-gray-100 pt-2 dark:border-gray-800">
              <button
                onClick={() => setShowPicker(false)}
                className="text-xs font-medium text-indigo-600 hover:text-indigo-800 dark:text-indigo-400"
                data-testid="capacity-project-new"
              >
                + Komplett neues Projekt erstellen
              </button>
            </div>
          </div>
        )}

        {/* Rückkehr zum Picker */}
        {!initial && !showPicker && (
          <button
            onClick={() => setShowPicker(true)}
            className="mb-3 text-xs text-gray-500 hover:text-indigo-600 dark:text-gray-400 dark:hover:text-indigo-400"
          >
            ← Bestehendes Projekt auswählen
          </button>
        )}

        {/* Detail-Formular */}
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Name</label>
            <input
              type="text" value={name} onChange={e => setName(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
              data-testid="capacity-project-name"
              autoFocus={!showPicker || !!initial}
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Icon / Farbe</label>
            <div className="flex items-center gap-3">
              <ProjectIcon iconUrl={iconUrl || null} iconEmoji={iconEmoji || null} color={color} size={28} />
              <div className="flex flex-wrap gap-1.5">
                {colors.map(c => (
                  <button
                    key={c}
                    onClick={() => setColor(c)}
                    className={`h-5 w-5 rounded-full border-2 transition ${color === c && !iconEmoji && !iconUrl ? 'border-gray-800 dark:border-white scale-110' : 'border-transparent'}`}
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
              <button
                onClick={() => setIconPickerOpen(true)}
                className="rounded-lg border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800"
                data-testid="capacity-project-icon-picker-btn"
              >
                Icon wählen
              </button>
              <label className="cursor-pointer rounded-lg border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                Bild
                <input type="file" accept="image/*" className="hidden" onChange={handleIconUpload} data-testid="capacity-project-icon-upload" />
              </label>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Kunde</label>
              <input
                type="text" value={clientName} onChange={e => setClientName(e.target.value)}
                className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                data-testid="capacity-project-client"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Stundensatz (CHF)</label>
              <input
                type="number" value={hourlyRate} onChange={e => setHourlyRate(e.target.value)}
                placeholder={defaultRate != null ? String(defaultRate) : '240'}
                className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                data-testid="capacity-project-rate"
              />
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {hourlyRate
                  ? 'Manueller Satz – überschreibt den Toggl-Satz.'
                  : togglProjectId
                    ? 'Leer: effektiver Toggl-Satz wird verwendet.'
                    : status === 'vorläufig'
                      ? `Leer: Default-Satz (${defaultRate ?? 240} CHF/h) wird verwendet.`
                      : 'Leer: Toggl-Satz, sonst Default-Satz.'}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
              <input type="checkbox" checked={isBillable} onChange={e => setIsBillable(e.target.checked)} className="rounded border-gray-300" />
              Verrechenbar
            </label>
            <select
              value={status} onChange={e => setStatus(e.target.value as 'bestätigt' | 'vorläufig')}
              className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
              data-testid="capacity-project-status"
            >
              <option value="bestätigt">Bestätigt</option>
              <option value="vorläufig">Vorläufig</option>
            </select>
          </div>

          {/* Toggl-Aggregation (nur sichtbar wenn kein einzelnes Toggl-Projekt) */}
          {!togglProjectId && togglClients.length > 0 && (
            <div className="rounded-lg border border-purple-200 bg-purple-50/50 p-3 dark:border-purple-800 dark:bg-purple-900/20">
              <label className="mb-2 block text-xs font-semibold text-purple-700 dark:text-purple-400 uppercase">
                Toggl-Aggregation
              </label>
              <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                Alle Toggl-Projekte eines Clients aggregieren (z.B. alle non-billable Projekte von InnoSmith).
              </p>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Toggl-Client</label>
                  <select
                    value={togglClientId ?? ''}
                    onChange={e => setTogglClientId(e.target.value ? parseInt(e.target.value) : null)}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-project-toggl-client"
                  >
                    <option value="">— Kein Client —</option>
                    {togglClients.map(c => (
                      <option key={c.id} value={c.id}>{c.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Filter</label>
                  <select
                    value={togglBillableFilter ?? ''}
                    onChange={e => setTogglBillableFilter(e.target.value || null)}
                    disabled={!togglClientId}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-project-toggl-filter"
                  >
                    <option value="">Alle Projekte</option>
                    <option value="non_billable">Nur non-billable</option>
                    <option value="billable">Nur billable</option>
                  </select>
                </div>
              </div>
              {togglClientId && (
                <div className="mt-2 rounded-md bg-purple-100 px-2.5 py-1.5 text-[11px] text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                  Aggregiert automatisch alle {togglBillableFilter === 'non_billable' ? 'non-billable ' : togglBillableFilter === 'billable' ? 'billable ' : ''}Toggl-Projekte von «{togglClients.find(c => c.id === togglClientId)?.name}»
                </div>
              )}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">InnoSmith OS-Board</label>
            <select
              value={projectId ?? ''}
              onChange={e => setProjectId(e.target.value || null)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
              data-testid="capacity-project-board"
            >
              <option value="">— Kein Board verknüpft —</option>
              {boards.map(b => (
                <option key={b.id} value={b.id}>{b.name}</option>
              ))}
            </select>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Verknüpft die Kapazitätsplanung mit dem Task-Board — Grundlage für den
              Planungs-Check in den Briefings (geplante Stunden vs. offene Aufgaben).
            </p>
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Notizen</label>
            <textarea
              value={notes} onChange={e => setNotes(e.target.value)} rows={2}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
              data-testid="capacity-project-notes"
            />
          </div>
        </div>

        <div className="mt-5 flex items-center justify-between">
          {initial && onDelete ? (
            <button
              onClick={onDelete}
              className="rounded-lg border border-red-300 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-900/20"
              data-testid="capacity-project-delete"
            >
              Projekt löschen
            </button>
          ) : <div />}
          <div className="flex gap-2">
            <button onClick={onClose} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
              Abbrechen
            </button>
            <button
              onClick={handleSave}
              disabled={!name.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              data-testid="capacity-project-save"
            >
              Speichern
            </button>
          </div>
        </div>

        {/* LucideIconPicker Popover */}
        {iconPickerOpen && (
          <div className="absolute inset-0 z-60">
            <LucideIconPicker
              currentIcon={iconEmoji || null}
              onSelect={(iconName) => {
                if (iconName) {
                  setIconEmoji(iconName);
                  setIconUrl('');
                } else {
                  setIconEmoji('');
                }
                setIconPickerOpen(false);
              }}
              onClose={() => setIconPickerOpen(false)}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Context Menu ─────────────────────────────────────────────────────────────

function ContextMenu({
  alloc, onAction,
}: {
  alloc: Allocation;
  onAction: (action: 'delete' | 'delete_series' | 'delete_from' | 'shift' | 'shift_single' | 'edit' | 'edit_series' | 'edit_series_from' | 'shift_day', weeks?: number) => void;
}) {
  const hasSeries = !!alloc.series_id;
  const isDay = alloc.allocation_type === 'day';
  const btn = "flex w-full items-center gap-2 px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700 whitespace-nowrap";
  const btnRed = "flex w-full items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/20 whitespace-nowrap";

  return (
    <>
      <button onClick={() => onAction('edit')} className={btn}>
        <Pencil className="h-4 w-4" /> {isDay ? 'Termin ändern' : 'Stunden ändern'}
      </button>
      {hasSeries && (
        <>
          <button onClick={() => onAction('edit_series')} className={btn}>
            <Pencil className="h-4 w-4" /> Ganze Serie ändern
          </button>
          <button onClick={() => onAction('edit_series_from')} className={btn}>
            <Pencil className="h-4 w-4" /> Serie ab hier ändern
          </button>
        </>
      )}
      <hr className="my-1 border-gray-200 dark:border-gray-700" />
      <button onClick={() => onAction('delete')} className={btn}>
        <Trash2 className="h-4 w-4" /> Zuweisung löschen
      </button>
      {hasSeries && (
        <>
          <button onClick={() => onAction('delete_series')} className={btnRed}>
            <Trash2 className="h-4 w-4" /> Ganze Serie löschen
          </button>
          <button onClick={() => onAction('delete_from')} className={btn}>
            <Trash2 className="h-4 w-4" /> Serie ab hier löschen
          </button>
        </>
      )}
      <hr className="my-1 border-gray-200 dark:border-gray-700" />
      {hasSeries ? (
        <>
          <button onClick={() => onAction('shift', 1)} className={btn}>
            <ArrowRightLeft className="h-4 w-4" /> Serie +1 Woche
          </button>
          <button onClick={() => onAction('shift', -1)} className={btn}>
            <ArrowRightLeft className="h-4 w-4" /> Serie −1 Woche
          </button>
        </>
      ) : (
        <>
          <button onClick={() => onAction('shift_single', 1)} className={btn}>
            <ArrowRightLeft className="h-4 w-4" /> +1 Woche
          </button>
          <button onClick={() => onAction('shift_single', -1)} className={btn}>
            <ArrowRightLeft className="h-4 w-4" /> −1 Woche
          </button>
        </>
      )}
      {isDay && (
        <>
          <hr className="my-1 border-gray-200 dark:border-gray-700" />
          <button onClick={() => onAction('shift_day', 1)} className={btn}>
            <Calendar className="h-4 w-4" /> +1 Tag
          </button>
          <button onClick={() => onAction('shift_day', -1)} className={btn}>
            <Calendar className="h-4 w-4" /> −1 Tag
          </button>
        </>
      )}
    </>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export function CapacityPage() {
  const [projects, setProjects] = useState<CapProject[]>([]);
  const [allocations, setAllocations] = useState<Allocation[]>([]);
  const [summary, setSummary] = useState<WeeklySummary[]>([]);
  const [timeOff, setTimeOff] = useState<TimeOffEntry[]>([]);
  const [planVsActual, setPlanVsActual] = useState<PlanVsActualProject[]>([]);
  const [monthlyActual, setMonthlyActual] = useState<MonthlyActualProject[]>([]);
  const [viewRange, setViewRange] = useState<ViewRange>('3m');
  const [startDate, setStartDate] = useState<Date>(getMonday(new Date()));
  const [showTentative, setShowTentative] = useState(true);
  const [loading, setLoading] = useState(true);
  const [emptyGroupExpanded, setEmptyGroupExpanded] = useState(false);

  // Soll/Ist column
  const [sollIstExpanded, setSollIstExpanded] = useState<boolean>(() => {
    return localStorage.getItem('cap_soll_ist') !== 'collapsed';
  });

  // Background
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [appLogoUrl, setAppLogoUrl] = useState<string | null>(null);

  // Multi-Select
  const [selectedAllocIds, setSelectedAllocIds] = useState<Set<string>>(new Set());

  const handleToggleSelect = useCallback((allocId: string) => {
    setSelectedAllocIds(prev => {
      const next = new Set(prev);
      if (next.has(allocId)) next.delete(allocId); else next.add(allocId);
      return next;
    });
  }, []);

  const handleBulkDelete = async () => {
    if (selectedAllocIds.size === 0) return;
    await Promise.all([...selectedAllocIds].map(id => api.delete(`/api/capacity/allocations/${id}`)));
    setSelectedAllocIds(new Set());
    fetchData();
  };

  const handleBulkShift = async (weeks: number) => {
    if (selectedAllocIds.size === 0) return;
    const updates = [...selectedAllocIds].map(id => {
      const alloc = allocations.find(a => a.id === id);
      if (!alloc) return null;
      const d = new Date(alloc.week_start + 'T00:00:00');
      const shifted = new Date(d.getFullYear(), d.getMonth(), d.getDate() + weeks * 7);
      return api.patch(`/api/capacity/allocations/${id}`, { week_start: toIso(shifted) });
    }).filter(Boolean);
    await Promise.all(updates);
    setSelectedAllocIds(new Set());
    fetchData();
  };

  // Dialogs
  const [allocDialog, setAllocDialog] = useState<{ open: boolean; projectId: string; weekStart: string; editAllocId?: string; editMinutes?: number; editScope?: 'single' | 'series' | 'series_from'; editSeriesId?: string; editAllocationType?: 'week' | 'day' }>({ open: false, projectId: '', weekStart: '' });
  const [projectDialog, setProjectDialog] = useState<{ open: boolean; editing: CapProject | null }>({ open: false, editing: null });
  const [timeOffDialog, setTimeOffDialog] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; alloc: Allocation } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Time-off dialog state
  const [toFromDate, setToFromDate] = useState('');
  const [toToDate, setToToDate] = useState('');
  const [toType, setToType] = useState('ferien');
  const [toLabel, setToLabel] = useState('');
  const [toHours, setToHours] = useState('8');

  const weeks = useMemo(() => getWeeksForRange(startDate, viewRange), [startDate, viewRange]);
  const endDate = useMemo(() => addWeeks(startDate, weeks.length), [startDate, weeks.length]);

  // Projekte gruppieren: Hauptliste = Kapazität im sichtbaren Fenster ODER
  // noch nie geplant (neu). Übrige wandern in eine eingeklappte Gruppe und
  // tauchen automatisch wieder auf, sobald im navigierten Zeitraum Kapazität
  // existiert (allocations werden pro Fenster geladen). Aus der Gruppe heraus
  // lässt sich jederzeit Kapazität buchen, wodurch das Projekt wieder hochwandert.
  const { mainProjects, emptyProjects, visibleProjects } = useMemo(() => {
    const visible = projects.filter(p => showTentative || p.status === 'bestätigt');
    const withWindowCap = new Set(allocations.map(a => a.capacity_project_id));
    const main: CapProject[] = [];
    const empty: CapProject[] = [];
    for (const p of visible) {
      const hasWindowCap = withWindowCap.has(p.id);
      const neverPlanned = (p.alloc_count ?? 0) === 0;
      if (hasWindowCap || neverPlanned) main.push(p);
      else empty.push(p);
    }
    return { mainProjects: main, emptyProjects: empty, visibleProjects: visible };
  }, [projects, allocations, showTentative]);

  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }));

  const timelineRef = useRef<HTMLDivElement>(null);
  const scrollAccRef = useRef(0);
  const scrollWeeksRef = useRef(0);
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startDateRef = useRef(startDate);
  startDateRef.current = startDate;

  const timelineWheelHandler = useCallback((e: WheelEvent) => {
    if (Math.abs(e.deltaX) < 2) return;
    e.preventDefault();
    scrollAccRef.current += e.deltaX;
    const threshold = 120;
    if (Math.abs(scrollAccRef.current) >= threshold) {
      const dir = scrollAccRef.current > 0 ? 1 : -1;
      scrollWeeksRef.current += dir;
      scrollAccRef.current = 0;
    }
    if (scrollTimerRef.current) clearTimeout(scrollTimerRef.current);
    scrollTimerRef.current = setTimeout(() => {
      if (scrollWeeksRef.current !== 0) {
        const w = scrollWeeksRef.current;
        scrollWeeksRef.current = 0;
        setStartDate(prev => addWeeks(prev, w));
      }
    }, 250);
  }, []);

  const timelineRefCallback = useCallback((node: HTMLDivElement | null) => {
    if (timelineRef.current) {
      timelineRef.current.removeEventListener('wheel', timelineWheelHandler);
    }
    timelineRef.current = node;
    if (node) {
      node.addEventListener('wheel', timelineWheelHandler, { passive: false });
    }
  }, [timelineWheelHandler]);

  // ── Data Fetching ──────────────────────────────────────────────────────────

  const fetchData = useCallback(async () => {
    setLoading(true);
    const from = toIso(startDate);
    const to = toIso(endDate);
    const [projRes, allocRes, summaryRes, timeOffRes] = await Promise.allSettled([
      api.get<CapProject[]>('/api/capacity/projects'),
      api.get<Allocation[]>(`/api/capacity/allocations?from=${from}&to=${to}&include_tentative=${showTentative}`),
      api.get<WeeklySummary[]>(`/api/capacity/weekly-summary?from=${from}&to=${to}&include_tentative=${showTentative}`),
      api.get<TimeOffEntry[]>(`/api/capacity/time-off?year=${startDate.getFullYear()}`),
    ]);
    if (projRes.status === 'fulfilled') setProjects(projRes.value);
    if (allocRes.status === 'fulfilled') setAllocations(allocRes.value);
    else console.warn('Allocations laden fehlgeschlagen — evtl. Migration pending:', allocRes.reason);
    if (summaryRes.status === 'fulfilled') setSummary(summaryRes.value);
    if (timeOffRes.status === 'fulfilled') setTimeOff(timeOffRes.value);
    setLoading(false);
  }, [startDate, endDate, showTentative]);

  const fetchPlanVsActual = useCallback(async () => {
    try {
      const from = toIso(startDate);
      const to = toIso(endDate);
      const res = await api.get<{ projects: PlanVsActualProject[] }>(`/api/capacity/plan-vs-actual?from=${from}&to=${to}`);
      setPlanVsActual(res.projects);
    } catch {
      /* optional */
    }
  }, [startDate, endDate]);

  const fetchMonthlyActual = useCallback(async () => {
    try {
      const res = await api.get<{ month: string; projects: MonthlyActualProject[] }>('/api/capacity/monthly-actual');
      setMonthlyActual(res.projects);
    } catch (err) {
      console.error('Soll/Ist-Daten konnten nicht geladen werden:', err);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);
  useEffect(() => { fetchPlanVsActual(); }, [fetchPlanVsActual]);
  useEffect(() => { fetchMonthlyActual(); }, [fetchMonthlyActual]);
  useEffect(() => {
    localStorage.setItem('cap_soll_ist', sollIstExpanded ? 'expanded' : 'collapsed');
  }, [sollIstExpanded]);
  useEffect(() => {
    api.get<Record<string, string | null>>('/api/settings')
      .then(s => {
        setBgUrl(s.capacity_background_url ?? null);
        setAppLogoUrl(s.app_logo_url ?? null);
      })
      .catch(() => {});
  }, []);

  // ── Handlers ───────────────────────────────────────────────────────────────

  const handleCellClick = (projectId: string, weekStart: string) => {
    setAllocDialog({ open: true, projectId, weekStart });
  };

  const handleSaveAllocation = async (data: {
    capacity_project_id: string;
    week_start: string;
    minutes: number;
    allocation_type: 'week' | 'day';
    repeat: boolean;
    end_date?: string;
    interval_weeks?: number;
    extra_days?: string[];
  }) => {
    try {
      if (allocDialog.editAllocId && allocDialog.editScope === 'series' && allocDialog.editSeriesId) {
        await api.post('/api/capacity/allocations/bulk', {
          action: 'update', series_id: allocDialog.editSeriesId, minutes: data.minutes,
        });
      } else if (allocDialog.editAllocId && allocDialog.editScope === 'series_from' && allocDialog.editSeriesId) {
        await api.post('/api/capacity/allocations/bulk', {
          action: 'update_from', series_id: allocDialog.editSeriesId, from_week: allocDialog.weekStart, minutes: data.minutes,
        });
      } else if (allocDialog.editAllocId) {
        const patchData: Record<string, unknown> = { minutes: data.minutes };
        if (data.allocation_type === 'day' && data.week_start !== allocDialog.weekStart) {
          patchData.week_start = data.week_start;
        }
        await api.patch(`/api/capacity/allocations/${allocDialog.editAllocId}`, patchData);
        if (data.extra_days && data.extra_days.length > 0) {
          const results = await Promise.allSettled(
            data.extra_days.map(dayIso => api.post('/api/capacity/allocations', {
              capacity_project_id: data.capacity_project_id,
              week_start: dayIso,
              minutes: data.minutes,
              allocation_type: 'day',
            })),
          );
          const failed = results.find(r => r.status === 'rejected') as PromiseRejectedResult | undefined;
          if (failed) throw failed.reason;
        }
      } else if (data.repeat && data.end_date) {
        await api.post('/api/capacity/allocations/repeat', {
          capacity_project_id: data.capacity_project_id,
          week_start: data.week_start,
          end_date: data.end_date,
          minutes: data.minutes,
          allocation_type: data.allocation_type,
          interval_weeks: data.interval_weeks || 1,
        });
      } else {
        await api.post('/api/capacity/allocations', {
          capacity_project_id: data.capacity_project_id,
          week_start: data.week_start,
          minutes: data.minutes,
          allocation_type: data.allocation_type,
        });
        if (data.extra_days && data.extra_days.length > 0) {
          const results = await Promise.allSettled(
            data.extra_days.map(dayIso => api.post('/api/capacity/allocations', {
              capacity_project_id: data.capacity_project_id,
              week_start: dayIso,
              minutes: data.minutes,
              allocation_type: 'day',
            })),
          );
          const failed = results.find(r => r.status === 'rejected') as PromiseRejectedResult | undefined;
          if (failed) throw failed.reason;
        }
      }
      setAllocDialog({ open: false, projectId: '', weekStart: '' });
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Zuweisung speichern fehlgeschlagen:', err);
      setActionError(`Zuweisung speichern fehlgeschlagen: ${msg}`);
    }
  };

  const handleSaveProject = async (data: Partial<CapProject>) => {
    try {
      if (projectDialog.editing) {
        await api.patch(`/api/capacity/projects/${projectDialog.editing.id}`, data);
      } else {
        await api.post('/api/capacity/projects', data);
      }
      setProjectDialog({ open: false, editing: null });
      fetchData();
    } catch (err) {
      console.error('Projekt speichern fehlgeschlagen:', err);
    }
  };

  const handleDeleteAllocation = async (allocId: string) => {
    try {
      await api.delete(`/api/capacity/allocations/${allocId}`);
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Löschen fehlgeschlagen:', err);
      setActionError(`Zuweisung konnte nicht gelöscht werden: ${msg}`);
    }
    setContextMenu(null);
  };

  const handleBulkAction = async (action: string, seriesId: string, fromWeek?: string, weeks?: number, minutes?: number) => {
    try {
      await api.post('/api/capacity/allocations/bulk', {
        action, series_id: seriesId, from_week: fromWeek, weeks, minutes,
      });
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Bulk-Aktion fehlgeschlagen:', err);
      setActionError(`Aktion fehlgeschlagen: ${msg}`);
    }
    setContextMenu(null);
  };

  const handleShiftSingle = async (allocId: string, weeks: number) => {
    const alloc = allocations.find(a => a.id === allocId);
    if (!alloc) return;
    const d = new Date(alloc.week_start + 'T00:00:00');
    const shifted = new Date(d.getFullYear(), d.getMonth(), d.getDate() + weeks * 7);
    try {
      await api.patch(`/api/capacity/allocations/${allocId}`, { week_start: toIso(shifted) });
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Verschieben fehlgeschlagen:', err);
      setActionError(`Verschieben fehlgeschlagen: ${msg}`);
    }
    setContextMenu(null);
  };

  const handleShiftDay = async (allocId: string, days: number) => {
    const alloc = allocations.find(a => a.id === allocId);
    if (!alloc) return;
    const d = new Date(alloc.week_start + 'T00:00:00');
    const shifted = new Date(d.getFullYear(), d.getMonth(), d.getDate() + days);
    try {
      await api.patch(`/api/capacity/allocations/${allocId}`, { week_start: toIso(shifted) });
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Verschieben fehlgeschlagen:', err);
      setActionError(`Verschieben fehlgeschlagen: ${msg}`);
    }
    setContextMenu(null);
  };

  const handleAllocDrop = async (allocId: string, targetWeekStr: string) => {
    const alloc = allocations.find(a => a.id === allocId);
    if (!alloc) return;
    let newDate = targetWeekStr;
    if (alloc.allocation_type === 'day') {
      const origDate = new Date(alloc.week_start + 'T00:00:00');
      const origDayIndex = (origDate.getDay() + 6) % 7;
      const targetMonday = new Date(targetWeekStr + 'T00:00:00');
      const shifted = new Date(targetMonday);
      shifted.setDate(shifted.getDate() + origDayIndex);
      newDate = toIso(shifted);
    }
    if (alloc.week_start === newDate) return;
    try {
      await api.patch(`/api/capacity/allocations/${allocId}`, { week_start: newDate });
      fetchData();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error('Verschieben fehlgeschlagen:', err);
      setActionError(`Verschieben fehlgeschlagen: ${msg}`);
    }
  };

  const handleDragEnd = async (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = projects.findIndex(p => p.id === active.id);
    const newIndex = projects.findIndex(p => p.id === over.id);
    const reordered = arrayMove(projects, oldIndex, newIndex);
    setProjects(reordered);
    const items = reordered.map((p, i) => ({ id: p.id, sort_order: i }));
    try {
      await api.post('/api/capacity/projects/reorder', items);
    } catch (err) {
      console.error('Reihenfolge speichern fehlgeschlagen:', err);
      fetchData();
    }
  };

  const handleBgSelect = async (url: string | null, _type?: string | null) => {
    await api.patch('/api/settings', { capacity_background_url: url });
    setBgUrl(url);
  };

  const handleSaveTimeOff = async () => {
    if (!toFromDate) return;
    const start = new Date(toFromDate);
    const end = toToDate ? new Date(toToDate) : start;
    const hours = parseFloat(toHours) || 8;
    for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
      const day = d.getDay();
      if (day === 0 || day === 6) continue;
      try {
        await api.post('/api/capacity/time-off', {
          date: d.toISOString().slice(0, 10),
          type: toType,
          label: toLabel || null,
          hours,
        });
      } catch { /* Duplikat ignorieren */ }
    }
    setTimeOffDialog(false);
    setToFromDate(''); setToToDate(''); setToLabel(''); setToHours('8');
    fetchData();
  };

  // ── Toggl Ist-Daten als Wochen-Map für Inline-Anzeige ──────────────────────

  const togglWeekMap = useMemo(() => {
    const map: Record<string, number> = {};
    for (const proj of planVsActual) {
      for (const w of proj.weeks) {
        map[w.week_start] = (map[w.week_start] || 0) + w.actual_minutes;
      }
    }
    return map;
  }, [planVsActual]);

  const monthlyByProject = useMemo(() => {
    const map: Record<string, MonthlyActualProject> = {};
    for (const p of monthlyActual) {
      map[p.capacity_project_id] = p;
    }
    return map;
  }, [monthlyActual]);

  // ── Time off weeks map ─────────────────────────────────────────────────────

  const computedHolidays = useMemo(() => {
    const years = new Set<number>();
    for (const w of weeks) years.add(w.getFullYear());
    const lastWeek = weeks[weeks.length - 1];
    if (lastWeek) years.add(new Date(lastWeek.getFullYear(), lastWeek.getMonth(), lastWeek.getDate() + 6).getFullYear());
    const all: { date: string; label: string }[] = [];
    for (const y of years) all.push(...bernHolidays(y));
    return all;
  }, [weeks]);

  const holidayWeekMap = useMemo(() => {
    const map: Record<string, { hours: number; entries: { label: string; dayIndex: number }[] }> = {};
    const manualDates = new Set(timeOff.map(t => t.date));
    for (const h of computedHolidays) {
      if (manualDates.has(h.date)) continue;
      const d = new Date(h.date + 'T00:00:00');
      const key = toIso(getMonday(d));
      const dayIndex = (d.getDay() + 6) % 7;
      if (!map[key]) map[key] = { hours: 0, entries: [] };
      map[key].hours += 8;
      map[key].entries.push({ label: h.label, dayIndex });
    }
    return map;
  }, [computedHolidays, timeOff]);

  const timeOffWeekMap = useMemo(() => {
    const map: Record<string, number> = {};
    for (const t of timeOff) {
      const d = new Date(t.date);
      const monday = getMonday(d);
      const key = toIso(monday);
      map[key] = (map[key] || 0) + t.hours;
    }
    const manualDates = new Set(timeOff.map(t => t.date));
    for (const h of computedHolidays) {
      if (manualDates.has(h.date)) continue;
      const d = new Date(h.date + 'T00:00:00');
      const monday = getMonday(d);
      const key = toIso(monday);
      map[key] = (map[key] || 0) + 8;
    }
    return map;
  }, [timeOff, computedHolidays]);

  // ── Render ─────────────────────────────────────────────────────────────────

  // Die Gantt-Matrix braucht Breite: Projektspalte + Soll/Ist + n Wochenspalten.
  // Auf 393px blieben pro Woche wenige Pixel — die Auslastung war unlesbar.
  // Mobil wird deshalb transponiert: eine Zeile pro Woche, Projekte aufklappbar.
  const isDesktop = useMediaQuery('(min-width: 1024px)');

  if (loading && projects.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div
      className="flex h-full flex-col overflow-hidden"
      data-testid="capacity-page"
      style={bgUrl
        ? bgUrl.startsWith('gradient:')
          ? { background: bgUrl.slice('gradient:'.length) }
          : { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
        : undefined
      }
    >
      {/* Header */}
      {/* Mobil: Kopf stapeln und die Werkzeuge in eine Scrollzeile — zehn Controls
          nebeneinander reichten auf 393px weit über den Viewport hinaus. */}
      <div className="flex flex-col gap-2 border-b border-gray-200 bg-white/80 px-4 py-2 backdrop-blur lg:flex-row lg:items-center lg:justify-between lg:px-6 lg:py-3 dark:border-gray-700 dark:bg-gray-900/80">
        <div className="flex items-center gap-3">
          <h1 className="hidden text-xl font-bold text-gray-800 lg:block dark:text-gray-100">Kapazitätsplanung</h1>
          <span className="rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-400">
            Anthony Smith · 40h/Woche
          </span>
        </div>
        <div className="-mx-4 flex items-center gap-2 overflow-x-auto px-4 lg:mx-0 lg:overflow-visible lg:px-0 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <label className="flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap text-xs text-gray-600 lg:min-h-0 dark:text-gray-400">
            <input
              type="checkbox"
              checked={showTentative}
              onChange={e => setShowTentative(e.target.checked)}
              className="h-4 w-4 rounded border-gray-300 lg:h-3.5 lg:w-3.5"
              data-testid="capacity-toggle-tentative"
            />
            Vorläufige anzeigen
          </label>
          <div className="flex shrink-0 rounded-lg border border-gray-200 lg:ml-3 dark:border-gray-700">
            {(['3m', '6m', '1y'] as ViewRange[]).map(r => (
              <button
                key={r}
                onClick={() => setViewRange(r)}
                className={`min-h-9 whitespace-nowrap px-3 py-1.5 text-xs font-medium transition ${viewRange === r ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400' : 'text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                data-testid={`capacity-range-${r}`}
              >
                <span className="lg:hidden">{r === '3m' ? '3M' : r === '6m' ? '6M' : '1J'}</span>
                <span className="hidden lg:inline">{r === '3m' ? '3 Monate' : r === '6m' ? '6 Monate' : '1 Jahr'}</span>
              </button>
            ))}
          </div>
          <button onClick={() => setStartDate(addWeeks(startDate, -4))} className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 lg:h-auto lg:w-auto lg:p-1.5 dark:hover:bg-gray-800" data-testid="capacity-nav-prev" aria-label="4 Wochen zurück">
            <ChevronLeft className="h-4 w-4" />
          </button>
          <button onClick={() => setStartDate(getMonday(new Date()))} className="min-h-10 shrink-0 rounded-lg px-2 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100 lg:min-h-0 dark:text-gray-400 dark:hover:bg-gray-800" data-testid="capacity-nav-today">
            Heute
          </button>
          <button onClick={() => setStartDate(addWeeks(startDate, 4))} className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 lg:h-auto lg:w-auto lg:p-1.5 dark:hover:bg-gray-800" data-testid="capacity-nav-next" aria-label="4 Wochen vorwärts">
            <ChevronRight className="h-4 w-4" />
          </button>
          <button
            onClick={() => setTimeOffDialog(true)}
            className="flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-amber-300 px-3 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-50 lg:ml-3 lg:min-h-0 dark:border-amber-700 dark:text-amber-400 dark:hover:bg-amber-900/20"
            data-testid="capacity-add-timeoff"
          >
            <Palmtree className="h-3.5 w-3.5" /> Ferien
          </button>
          <button
            onClick={() => setProjectDialog({ open: true, editing: null })}
            className="flex min-h-10 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 lg:min-h-0"
            data-testid="capacity-add-project"
          >
            <Plus className="h-3.5 w-3.5" /> Projekt
          </button>
          <button
            onClick={() => setBgPickerOpen(true)}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1.5 dark:hover:bg-gray-800 dark:hover:text-gray-300"
            title="Hintergrund ändern"
            data-testid="capacity-bg-picker"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>
          </button>
        </div>
      </div>

      {!isDesktop && (
        <div className="flex-1 overflow-y-auto" data-testid="capacity-timeline">
          <CapacityWeekList
            weeks={weeks}
            summary={summary}
            allocations={allocations}
            projects={visibleProjects}
            togglWeekMap={togglWeekMap}
            holidayWeekMap={holidayWeekMap}
            timeOff={timeOff}
            onCellClick={handleCellClick}
          />
        </div>
      )}

      {/* Timeline */}
      {isDesktop && (
      <div ref={timelineRefCallback} className="flex-1 overflow-hidden" data-testid="capacity-timeline">
        <div className="h-full w-full overflow-y-auto">
          {/* Auslastungs-Header (Runn.io-Stil) */}
          <div className="sticky top-0 z-20 border-b border-gray-200 bg-white/95 backdrop-blur dark:border-gray-700 dark:bg-gray-900/95">
            {/* Wochenlabels */}
            <div className="flex">
              <div className="flex w-40 min-w-40 shrink-0 lg:w-64 lg:min-w-64 items-end border-r border-gray-200 bg-white px-3 pb-1 dark:border-gray-700 dark:bg-gray-900">
                <span className="text-[10px] font-semibold text-gray-500 uppercase dark:text-gray-400">Woche</span>
              </div>
              <div
                className="shrink-0 flex items-end justify-center border-r border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 pb-0.5 cursor-pointer select-none"
                style={{ width: sollIstExpanded ? 120 : 32 }}
                onClick={() => setSollIstExpanded(v => !v)}
                title={sollIstExpanded ? 'Soll/Ist-Spalte einklappen' : 'Soll/Ist-Spalte ausklappen'}
              >
                {sollIstExpanded ? (
                  <div className="flex items-center gap-1">
                    <span className="text-[10px] font-semibold text-gray-500 uppercase dark:text-gray-400">
                      {new Date().toLocaleDateString('de-CH', { month: 'short' })}
                    </span>
                    <ChevronLeft className="h-3 w-3 text-gray-400" />
                  </div>
                ) : (
                  <ChevronRight className="h-3 w-3 text-gray-400" />
                )}
              </div>
              <div className="flex flex-1">
                {weeks.map((week) => {
                  const weekStr = toIso(week);
                  const isYear = viewRange === '1y';
                  const isHalf = viewRange === '6m';
                  const showMonthLabel = isYear && week.getDate() <= 7;
                  const todayOffset = getTodayOffset(week);
                  return (
                    <div key={weekStr} className={`relative ${getColClass(viewRange)} flex items-end justify-center pb-0.5 border-r border-gray-100 dark:border-gray-800`}>
                      {!isYear && !isHalf && (
                        <span className="text-[10px] text-gray-500 dark:text-gray-400 truncate">{formatWeek(week)}</span>
                      )}
                      {isHalf && (
                        <span className="text-[9px] text-gray-500 dark:text-gray-400 truncate">{week.getDate()}.{week.getMonth() + 1}</span>
                      )}
                      {showMonthLabel && (
                        <span className="text-[9px] font-medium text-gray-600 dark:text-gray-400 truncate">
                          {week.toLocaleDateString('de-CH', { month: 'short' })}
                        </span>
                      )}
                      {todayOffset !== null && (
                        <div className="absolute top-0 bottom-0 w-[2px] bg-red-500 dark:bg-red-400 z-10 pointer-events-none" style={{ left: `${todayOffset}%` }} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
            {/* Auslastungs-Blöcke */}
            <div className="flex">
              <div className="flex w-40 min-w-40 shrink-0 lg:w-64 lg:min-w-64 items-center border-r border-gray-200 bg-white px-3 py-1.5 dark:border-gray-700 dark:bg-gray-900">
                <span className="text-[10px] font-semibold text-gray-500 uppercase dark:text-gray-400">Auslastung</span>
              </div>
              <div
                className="shrink-0 flex items-center justify-center border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50"
                style={{ width: sollIstExpanded ? 120 : 32 }}
              >
                {sollIstExpanded && (
                  <span className="text-[9px] font-semibold text-gray-500 dark:text-gray-400 uppercase">Soll/Ist</span>
                )}
              </div>
              <div className="flex flex-1">
                {weeks.map((week) => {
                  const weekStr = toIso(week);
                  const s = summary.find(s => s.week_start === weekStr);
                  const holidayMin = (holidayWeekMap[weekStr]?.hours || 0) * 60;
                  const rawAvail = s?.available_minutes || 2400;
                  const adjustedAvail = Math.max(0, rawAvail - holidayMin);
                  const plannedMin = s?.planned_minutes || 0;
                  const util = adjustedAvail > 0 ? Math.round(plannedMin / adjustedAvail * 100) : (plannedMin > 0 ? 999 : 0);
                  const isYear = viewRange === '1y';
                  const isHalf = viewRange === '6m';
                  const isPast = week < new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
                  const actualMin = togglWeekMap[weekStr] || 0;

                  let bgColor: string;
                  let textColor: string;
                  if (util === 0) {
                    bgColor = 'bg-gray-100 dark:bg-gray-800';
                    textColor = 'text-gray-400 dark:text-gray-500';
                  } else if (util <= 60) {
                    bgColor = 'bg-emerald-100 dark:bg-emerald-900/40';
                    textColor = 'text-emerald-700 dark:text-emerald-300';
                  } else if (util <= 85) {
                    bgColor = 'bg-emerald-200 dark:bg-emerald-800/50';
                    textColor = 'text-emerald-800 dark:text-emerald-200';
                  } else if (util <= 100) {
                    bgColor = 'bg-amber-200 dark:bg-amber-800/50';
                    textColor = 'text-amber-800 dark:text-amber-200';
                  } else {
                    bgColor = 'bg-red-200 dark:bg-red-900/50';
                    textColor = 'text-red-800 dark:text-red-200';
                  }

                  const availMin = adjustedAvail;
                  const actualPct = availMin > 0 ? Math.round((actualMin / availMin) * 100) : 0;
                  const actualTextColor = isPast && actualMin > 0
                    ? (actualMin > plannedMin * 1.1 ? 'text-red-600 dark:text-red-400' : actualMin < plannedMin * 0.5 ? 'text-amber-600 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400')
                    : '';

                  return (
                    <div
                      key={weekStr}
                      className={`relative ${getColClass(viewRange)} flex flex-col items-center justify-center border-r border-white/50 dark:border-gray-900/50 ${bgColor} py-0.5`}
                      title={`${formatWeek(week)} — ${Math.round(util)}% Auslastung\nGeplant: ${minutesToDisplay(plannedMin)} / ${minutesToDisplay(availMin)}${isPast && actualMin > 0 ? `\nEffektiv (Toggl): ${minutesToDisplay(actualMin)} (${actualPct}%)` : ''}`}
                    >
                      {util > 100 && (
                        <div className="absolute inset-x-0 top-0 h-[3px] bg-red-600 dark:bg-red-500" />
                      )}
                      <span className={`text-[10px] font-bold leading-none ${textColor} ${isHalf ? 'text-[9px]' : ''} ${isYear ? 'text-[7px]' : ''}`}>
                        {Math.round(util)}%
                      </span>
                      {isPast && actualMin > 0 && !isYear && (
                        <span className={`text-[9px] font-semibold leading-none mt-0.5 ${actualTextColor} ${isHalf ? 'text-[8px]' : ''}`}>
                          {isHalf ? `${actualPct}%` : `${actualPct}% Ist`}
                        </span>
                      )}
                      {isYear && isPast && actualMin > 0 && (
                        <div className={`h-1 w-1 rounded-full mt-0.5 ${actualMin > plannedMin * 1.1 ? 'bg-red-500' : actualMin < plannedMin * 0.5 ? 'bg-amber-400' : 'bg-emerald-500'}`} />
                      )}
                      {(() => {
                        const offset = getTodayOffset(week);
                        if (offset === null) return null;
                        return <div className="absolute top-0 bottom-0 w-[2px] bg-red-500 dark:bg-red-400 z-10 pointer-events-none" style={{ left: `${offset}%` }} />;
                      })()}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Ferien-/Feiertags-Zeile */}
          {(timeOff.length > 0 || computedHolidays.length > 0) && (
            <div className="flex items-stretch border-b border-amber-200 dark:border-amber-800/50 bg-amber-50/50 dark:bg-amber-900/10">
              <div className="flex w-40 min-w-40 shrink-0 lg:w-64 lg:min-w-64 items-center gap-2 border-r border-gray-200 bg-amber-50 px-3 py-1.5 dark:border-gray-700 dark:bg-amber-900/20">
                <Palmtree className="h-4 w-4 text-amber-500" />
                <span className="text-xs font-medium text-amber-700 dark:text-amber-400">Ferien / Feiertage</span>
              </div>
              <div
                className="shrink-0 border-r border-amber-100 dark:border-amber-900/30"
                style={{ width: sollIstExpanded ? 120 : 32 }}
              />
              <div className="flex flex-1">
                {weeks.map(week => {
                  const weekStr = toIso(week);
                  const manualHoursOff = (() => {
                    let h = 0;
                    const weekEnd = toIso(new Date(week.getFullYear(), week.getMonth(), week.getDate() + 6));
                    for (const t of timeOff) { if (t.date >= weekStr && t.date <= weekEnd) h += t.hours; }
                    return h;
                  })();
                  const holidayInfo = holidayWeekMap[weekStr];
                  const todayOffset = getTodayOffset(week);
                  const totalOff = manualHoursOff + (holidayInfo?.hours || 0);
                  const tooltipParts: string[] = [];
                  if (holidayInfo) tooltipParts.push(holidayInfo.entries.map(e => e.label).join(', '));
                  if (manualHoursOff > 0) tooltipParts.push(`${manualHoursOff}h Ferien`);
                  return (
                    <div
                      key={weekStr}
                      className={`relative flex ${getColClass(viewRange)} items-center justify-center border-r border-amber-100 dark:border-amber-900/30 ${totalOff > 0 ? (totalOff >= 40 ? 'bg-amber-300/60 dark:bg-amber-700/40' : 'bg-amber-200/50 dark:bg-amber-800/30') : ''}`}
                      title={tooltipParts.length > 0 ? `${formatWeek(week)}: ${tooltipParts.join(' — ')}` : undefined}
                    >
                      {totalOff > 0 && viewRange !== '1y' && (
                        <span className={`text-[9px] font-medium ${totalOff >= 40 ? 'text-amber-800 dark:text-amber-300' : 'text-amber-600 dark:text-amber-400'}`}>
                          {`${totalOff}h`}
                        </span>
                      )}
                      {totalOff > 0 && viewRange === '1y' && (
                        <div className={`h-2 w-full rounded-sm ${totalOff >= 40 ? 'bg-amber-400 dark:bg-amber-500' : 'bg-amber-300 dark:bg-amber-600'}`} />
                      )}
                      {todayOffset !== null && (
                        <div className="absolute top-0 bottom-0 w-[2px] bg-red-500 dark:bg-red-400 z-10 pointer-events-none" style={{ left: `${todayOffset}%` }} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Multi-Select Action-Bar */}
          {selectedAllocIds.size > 0 && (
            <div className="sticky top-0 z-30 flex items-center gap-3 border-b border-indigo-300 bg-indigo-50 px-4 py-2 dark:border-indigo-700 dark:bg-indigo-900/40">
              <span className="text-sm font-semibold text-indigo-700 dark:text-indigo-300">
                {selectedAllocIds.size} ausgewählt
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  onClick={() => handleBulkShift(-1)}
                  className="rounded-md bg-white px-2.5 py-1 text-xs font-medium text-gray-700 shadow-sm ring-1 ring-gray-300 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-600"
                >
                  −1 Woche
                </button>
                <button
                  onClick={() => handleBulkShift(1)}
                  className="rounded-md bg-white px-2.5 py-1 text-xs font-medium text-gray-700 shadow-sm ring-1 ring-gray-300 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-200 dark:ring-gray-600"
                >
                  +1 Woche
                </button>
              </div>
              <button
                onClick={handleBulkDelete}
                className="rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700"
              >
                Löschen
              </button>
              <button
                onClick={() => setSelectedAllocIds(new Set())}
                className="ml-auto text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
              >
                Auswahl aufheben
              </button>
            </div>
          )}

          {/* Projekte */}
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
            <SortableContext items={mainProjects.map(p => p.id)} strategy={verticalListSortingStrategy}>
              {mainProjects.map(project => (
                <SortableProjectRow
                  key={project.id}
                  project={project}
                  weeks={weeks}
                  allocations={allocations.filter(a => a.capacity_project_id === project.id)}
                  onCellClick={handleCellClick}
                  onEditProject={(p) => setProjectDialog({ open: true, editing: p })}
                  onContextMenu={(e, alloc) => {
                    e.preventDefault();
                    setContextMenu({ x: e.clientX, y: e.clientY, alloc });
                  }}
                  colClass={getColClass(viewRange)}
                  viewRange={viewRange}
                  onAllocDrop={handleAllocDrop}
                  selectedAllocIds={selectedAllocIds}
                  onToggleSelect={handleToggleSelect}
                  timeOffWeekMap={timeOffWeekMap}
                  holidayWeekMap={holidayWeekMap}
                  monthlyData={monthlyByProject[project.id]}
                  sollIstExpanded={sollIstExpanded}
                />
              ))}
            </SortableContext>

            {/* Projekte ohne Kapazität im sichtbaren Zeitraum: eingeklappte Gruppe */}
            {emptyProjects.length > 0 && (
              <div className="border-t border-dashed border-gray-200 dark:border-gray-700">
                <button
                  onClick={() => setEmptyGroupExpanded(v => !v)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-xs font-medium text-gray-500 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800/50"
                  data-testid="capacity-empty-group-toggle"
                >
                  {emptyGroupExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                  <EyeOff className="h-3.5 w-3.5" />
                  <span>Keine Kapazität in diesem Zeitraum ({emptyProjects.length})</span>
                </button>
                {emptyGroupExpanded && (
                  <SortableContext items={emptyProjects.map(p => p.id)} strategy={verticalListSortingStrategy}>
                    {emptyProjects.map(project => (
                      <SortableProjectRow
                        key={project.id}
                        project={project}
                        weeks={weeks}
                        allocations={allocations.filter(a => a.capacity_project_id === project.id)}
                        onCellClick={handleCellClick}
                        onEditProject={(p) => setProjectDialog({ open: true, editing: p })}
                        onContextMenu={(e, alloc) => {
                          e.preventDefault();
                          setContextMenu({ x: e.clientX, y: e.clientY, alloc });
                        }}
                        colClass={getColClass(viewRange)}
                        viewRange={viewRange}
                        onAllocDrop={handleAllocDrop}
                        selectedAllocIds={selectedAllocIds}
                        onToggleSelect={handleToggleSelect}
                        timeOffWeekMap={timeOffWeekMap}
                        holidayWeekMap={holidayWeekMap}
                        monthlyData={monthlyByProject[project.id]}
                        sollIstExpanded={sollIstExpanded}
                      />
                    ))}
                  </SortableContext>
                )}
              </div>
            )}
          </DndContext>

          {projects.length === 0 && (
            <div className="flex h-32 items-center justify-center text-sm text-gray-400 dark:text-gray-500">
              Noch keine Kapazitätsprojekte. Erstelle eines mit dem Button oben rechts.
            </div>
          )}
        </div>
      </div>
      )}

      {/* Fehler-Banner */}
      {actionError && (
        <div className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 flex items-center gap-3 rounded-lg border border-red-300 bg-red-50 px-4 py-2.5 shadow-lg dark:border-red-700 dark:bg-red-900/80">
          <span className="text-sm text-red-700 dark:text-red-200">{actionError}</span>
          <button onClick={() => setActionError(null)} className="text-red-500 hover:text-red-700 dark:text-red-300 dark:hover:text-red-100">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* Dialogs */}
      <AllocationDialog
        open={allocDialog.open}
        onClose={() => setAllocDialog({ open: false, projectId: '', weekStart: '' })}
        onSave={handleSaveAllocation}
        projects={projects}
        initialProjectId={allocDialog.projectId}
        initialWeek={allocDialog.weekStart}
        editAllocId={allocDialog.editAllocId}
        editMinutes={allocDialog.editMinutes}
        editScope={allocDialog.editScope}
        editAllocationType={allocDialog.editAllocationType}
      />

      <ProjectDialog
        open={projectDialog.open}
        onClose={() => setProjectDialog({ open: false, editing: null })}
        onSave={handleSaveProject}
        onDelete={projectDialog.editing ? async () => {
          await api.delete(`/api/capacity/projects/${projectDialog.editing!.id}`);
          setProjectDialog({ open: false, editing: null });
          fetchData();
        } : undefined}
        initial={projectDialog.editing}
        appLogoUrl={appLogoUrl}
      />

      {/* Context Menu */}
      {contextMenu && (() => {
        const cmAlloc = contextMenu.alloc;
        const close = () => setContextMenu(null);
        const act = async (fn: () => Promise<void>) => { await fn(); close(); };
        return (
          <>
            <div className="fixed inset-0 z-40" onClick={close} />
            <div
              className="fixed z-50 min-w-48 rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-800"
              style={{ left: contextMenu.x, top: contextMenu.y }}
              data-testid="capacity-context-menu"
            >
              <ContextMenu
                alloc={cmAlloc}
                onAction={(action, weeks) => {
                  if (action === 'edit') {
                    close();
                    setAllocDialog({ open: true, projectId: cmAlloc.capacity_project_id, weekStart: cmAlloc.week_start, editAllocId: cmAlloc.id, editMinutes: cmAlloc.minutes, editScope: 'single', editAllocationType: (cmAlloc.allocation_type as 'week' | 'day') || 'week' });
                  }
                  else if (action === 'edit_series' && cmAlloc.series_id) {
                    close();
                    setAllocDialog({ open: true, projectId: cmAlloc.capacity_project_id, weekStart: cmAlloc.week_start, editAllocId: cmAlloc.id, editMinutes: cmAlloc.minutes, editScope: 'series', editSeriesId: cmAlloc.series_id });
                  }
                  else if (action === 'edit_series_from' && cmAlloc.series_id) {
                    close();
                    setAllocDialog({ open: true, projectId: cmAlloc.capacity_project_id, weekStart: cmAlloc.week_start, editAllocId: cmAlloc.id, editMinutes: cmAlloc.minutes, editScope: 'series_from', editSeriesId: cmAlloc.series_id });
                  }
                  else if (action === 'delete') act(() => handleDeleteAllocation(cmAlloc.id));
                  else if (action === 'delete_series' && cmAlloc.series_id) act(() => handleBulkAction('delete', cmAlloc.series_id!));
                  else if (action === 'delete_from' && cmAlloc.series_id) act(() => handleBulkAction('delete_from', cmAlloc.series_id!, cmAlloc.week_start));
                  else if (action === 'shift' && cmAlloc.series_id && weeks != null) act(() => handleBulkAction('shift', cmAlloc.series_id!, undefined, weeks));
                  else if (action === 'shift_single' && weeks != null) act(() => handleShiftSingle(cmAlloc.id, weeks));
                  else if (action === 'shift_day' && weeks != null) act(() => handleShiftDay(cmAlloc.id, weeks));
                }}
              />
            </div>
          </>
        );
      })()}

      {/* Ferien-Dialog */}
      {timeOffDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm modal-safe" data-testid="capacity-timeoff-dialog">
          <div className="w-full max-w-sm rounded-xl border border-gray-200 bg-white p-6 shadow-2xl dark:border-gray-700 dark:bg-gray-900">
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-100">Ferien / freie Tage</h3>
              <button onClick={() => setTimeOffDialog(false)} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Von</label>
                  <input type="date" value={toFromDate} onChange={e => setToFromDate(e.target.value)}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-timeoff-from" />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Bis (optional)</label>
                  <input type="date" value={toToDate} min={toFromDate || undefined} onChange={e => setToToDate(e.target.value)}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-timeoff-to" />
                </div>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Typ</label>
                <select value={toType} onChange={e => setToType(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                  data-testid="capacity-timeoff-type">
                  <option value="ferien">Ferien</option>
                  <option value="feiertag">Feiertag</option>
                  <option value="krank">Krank</option>
                  <option value="sonstiges">Sonstiges</option>
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Bezeichnung</label>
                  <input type="text" value={toLabel} onChange={e => setToLabel(e.target.value)}
                    placeholder="z.B. Sommerferien"
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-timeoff-label" />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Stunden/Tag</label>
                  <input type="number" min="1" max="12" value={toHours} onChange={e => setToHours(e.target.value)}
                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200"
                    data-testid="capacity-timeoff-hours" />
                </div>
              </div>
              <p className="text-xs text-gray-400">8h = ganzer Tag, 4h = halber Tag. Wochenenden werden übersprungen.</p>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setTimeOffDialog(false)} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                Abbrechen
              </button>
              <button onClick={handleSaveTimeOff} disabled={!toFromDate}
                className="rounded-lg bg-amber-600 px-4 py-2 text-sm font-medium text-white hover:bg-amber-700 disabled:opacity-50"
                data-testid="capacity-timeoff-save">
                Speichern
              </button>
            </div>
          </div>
        </div>
      )}

      {/* BackgroundPicker */}
      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={bgUrl}
        onSelect={handleBgSelect}
      />
    </div>
  );
}
