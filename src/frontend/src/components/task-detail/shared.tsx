import { useState } from 'react';

/* ═══════════ Shared Types ═══════════ */

export interface ActivityLogEntry {
  id: string;
  task_id: string;
  event_type: string;
  actor: string;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface AttachmentEntry {
  id: string;
  task_id: string;
  filename: string;
  filepath: string;
  mime_type: string | null;
  size: number;
  uploaded_at: string;
}

export interface ModelInfo {
  id: string;
  name: string;
  type: 'local' | 'cloud';
  provider: string;
}

export interface ModelsData {
  local: ModelInfo[];
  cloud: ModelInfo[];
}

/* ═══════════ Shared Styles ═══════════ */

export const ATTR_SELECT = 'w-full cursor-pointer rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 outline-none transition-colors hover:border-gray-300 focus:border-indigo-300 focus:ring-1 focus:ring-indigo-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600';

/* ═══════════ Shared Components ═══════════ */

export function SectionLabel({ icon: Icon, text, action }: { icon: React.FC<{ className?: string }>; text: string; action?: React.ReactNode }) {
  return (
    <h3 className="mb-2.5 flex items-center gap-2 text-[11px] font-semibold text-gray-500 dark:text-gray-400">
      <Icon className="h-3.5 w-3.5" /> {text}
      {action && <span className="ml-auto">{action}</span>}
    </h3>
  );
}

export function AttrRow({ icon: Icon, label, children, align = 'center' }: { icon: React.FC<{ className?: string }>; label: string; children: React.ReactNode; align?: 'center' | 'start' }) {
  return (
    <div className={`flex gap-3 py-1.5 ${align === 'start' ? 'items-start' : 'items-center'}`}>
      <div className="flex w-[104px] shrink-0 items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
        <Icon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
        <span className="truncate">{label}</span>
      </div>
      <div className="min-w-0 flex-1">
        {children}
      </div>
    </div>
  );
}

/* ═══════════ Recurrence ═══════════ */

type RecurrenceFrequency = 'none' | 'daily' | 'weekly' | 'monthly';

const FREQ_OPTIONS: { value: RecurrenceFrequency; label: string }[] = [
  { value: 'none', label: 'Keine' },
  { value: 'daily', label: 'Täglich' },
  { value: 'weekly', label: 'Wöchentlich' },
  { value: 'monthly', label: 'Monatlich' },
];

const WEEKDAY_OPTIONS = [
  { value: 'MON', label: 'Mo' },
  { value: 'TUE', label: 'Di' },
  { value: 'WED', label: 'Mi' },
  { value: 'THU', label: 'Do' },
  { value: 'FRI', label: 'Fr' },
  { value: 'SAT', label: 'Sa' },
  { value: 'SUN', label: 'So' },
];

const HOUR_OPTIONS = Array.from({ length: 25 }, (_, i) => {
  const h = 7 + Math.floor(i / 2);
  const m = i % 2 === 0 ? 0 : 30;
  if (h > 19) return null;
  const label = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  return { value: `${h}-${m}`, label, hour: h, minute: m };
}).filter(Boolean) as { value: string; label: string; hour: number; minute: number }[];

const MONTHDAY_OPTIONS = Array.from({ length: 31 }, (_, i) => ({
  value: i + 1,
  label: `${i + 1}.`,
}));

function parseCron(cron: string): { freq: RecurrenceFrequency; hour: number; minute: number; weekday: string; monthday: number } {
  const defaults = { freq: 'none' as RecurrenceFrequency, hour: 8, minute: 0, weekday: 'MON', monthday: 1 };
  if (!cron) return defaults;
  const parts = cron.split(/\s+/);
  if (parts.length !== 5) return defaults;
  const [min, hr, dom, , dow] = parts;
  const hour = parseInt(hr) || 8;
  const minute = parseInt(min) || 0;

  if (dom !== '*' && dom !== 'L') return { freq: 'monthly', hour, minute, weekday: 'MON', monthday: parseInt(dom) || 1 };
  if (dom === 'L') return { freq: 'monthly', hour, minute, weekday: 'MON', monthday: -1 };
  if (dow !== '*') return { freq: 'weekly', hour, minute, weekday: dow.toUpperCase(), monthday: 1 };
  return { freq: 'daily', hour, minute, weekday: 'MON', monthday: 1 };
}

function buildCron(freq: RecurrenceFrequency, hour: number, minute: number, weekday: string, monthday: number): string | null {
  if (freq === 'none') return null;
  const m = String(minute);
  const h = String(hour);
  if (freq === 'daily') return `${m} ${h} * * *`;
  if (freq === 'weekly') return `${m} ${h} * * ${weekday}`;
  if (freq === 'monthly') return `${m} ${h} ${monthday === -1 ? 'L' : monthday} * *`;
  return null;
}

export function cronToHumanDE(cron: string): string {
  const p = parseCron(cron);
  const time = `${String(p.hour).padStart(2, '0')}:${String(p.minute).padStart(2, '0')}`;
  if (p.freq === 'daily') return `Täglich um ${time}`;
  if (p.freq === 'weekly') {
    const dayLabel = WEEKDAY_OPTIONS.find((d) => d.value === p.weekday)?.label || p.weekday;
    return `Jeden ${dayLabel} um ${time}`;
  }
  if (p.freq === 'monthly') {
    if (p.monthday === -1) return `Monatlich am letzten Tag um ${time}`;
    return `Monatlich am ${p.monthday}. um ${time}`;
  }
  return cron;
}

const RECURRENCE_SELECT = 'w-full cursor-pointer rounded-lg border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 outline-none transition-colors hover:border-gray-300 focus:border-indigo-300 focus:ring-1 focus:ring-indigo-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600';

export function RecurrenceSelector({ value, isInstance, templateId, onOpenTask, onChange }: {
  value: string | null;
  isInstance: boolean;
  templateId?: string | null;
  onOpenTask?: (taskId: string) => void;
  onChange: (rule: string | null) => void;
}) {
  const parsed = parseCron(value || '');
  const [freq, setFreq] = useState<RecurrenceFrequency>(value ? parsed.freq : 'none');
  const [hour, setHour] = useState(parsed.hour);
  const [minute, setMinute] = useState(parsed.minute);
  const [weekday, setWeekday] = useState(parsed.weekday);
  const [monthday, setMonthday] = useState(parsed.monthday);

  const emit = (f: RecurrenceFrequency, h: number, m: number, wd: string, md: number) => {
    onChange(buildCron(f, h, m, wd, md));
  };

  // Instanzen erben die Regel nicht — sie sind nur ein Ableger. Bearbeiten und
  // Beenden der Serie geht ausschliesslich über die Vorlage, darum der Sprung.
  if (isInstance) {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center gap-1.5 rounded-lg bg-indigo-50 px-2 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
          <RepeatIcon className="h-3.5 w-3.5" />Instanz einer Vorlage
        </div>
        {templateId && onOpenTask && (
          <button
            type="button"
            onClick={() => onOpenTask(templateId)}
            className="text-[11px] font-medium text-indigo-600 underline-offset-2 hover:underline dark:text-indigo-400"
          >
            Serie öffnen und bearbeiten →
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <select
        value={freq}
        onChange={(e) => {
          const f = e.target.value as RecurrenceFrequency;
          setFreq(f);
          emit(f, hour, minute, weekday, monthday);
        }}
        className={RECURRENCE_SELECT}
      >
        {FREQ_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      {freq !== 'none' && (
        <div className="space-y-2">
          {freq === 'weekly' && (
            <div className="grid grid-cols-7 gap-1">
              {WEEKDAY_OPTIONS.map((d) => (
                <button
                  key={d.value}
                  type="button"
                  onClick={() => { setWeekday(d.value); emit(freq, hour, minute, d.value, monthday); }}
                  className={`rounded-md py-1 text-center text-[10px] font-medium transition-colors ${
                    weekday === d.value
                      ? 'bg-indigo-600 text-white'
                      : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {d.label}
                </button>
              ))}
            </div>
          )}

          {freq === 'monthly' && (
            <div className="space-y-1.5">
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => { setMonthday(1); emit(freq, hour, minute, weekday, 1); }}
                  className={`rounded-md px-2 py-1 text-[10px] font-medium transition-colors ${
                    monthday === 1 ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                  }`}
                >Erster</button>
                <button
                  type="button"
                  onClick={() => { setMonthday(-1); emit(freq, hour, minute, weekday, -1); }}
                  className={`rounded-md px-2 py-1 text-[10px] font-medium transition-colors ${
                    monthday === -1 ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                  }`}
                >Letzter</button>
              </div>
              {monthday !== 1 && monthday !== -1 && (
                <select
                  value={monthday}
                  onChange={(e) => { const md = Number(e.target.value); setMonthday(md); emit(freq, hour, minute, weekday, md); }}
                  className={RECURRENCE_SELECT}
                >
                  {MONTHDAY_OPTIONS.map((o) => <option key={o.value} value={o.value}>Am {o.label}</option>)}
                </select>
              )}
              {(monthday === 1 || monthday === -1) && (
                <button
                  type="button"
                  onClick={() => { setMonthday(15); emit(freq, hour, minute, weekday, 15); }}
                  className="text-[10px] text-indigo-600 hover:text-indigo-800 dark:text-indigo-400"
                >Anderen Tag wählen…</button>
              )}
            </div>
          )}

          <select
            value={`${hour}-${minute}`}
            onChange={(e) => {
              const opt = HOUR_OPTIONS.find((o) => o.value === e.target.value);
              if (opt) { setHour(opt.hour); setMinute(opt.minute); emit(freq, opt.hour, opt.minute, weekday, monthday); }
            }}
            className={RECURRENCE_SELECT}
          >
            {HOUR_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      )}

      {value && freq !== 'none' && (
        <p className="text-[10px] text-gray-400 dark:text-gray-500">{cronToHumanDE(value)}</p>
      )}
    </div>
  );
}

/* ═══════════ Utilities ═══════════ */

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatActivityEvent(eventType: string, details: Record<string, unknown> | null): string {
  const EVENT_LABELS: Record<string, string> = {
    created: 'Task erstellt', title_changed: 'Titel geändert', status_changed: 'Status geändert',
    column_changed: 'Spalte verschoben', assigned: 'Zuweisung geändert', due_date_changed: 'Fälligkeit geändert',
    completed: 'Als erledigt markiert', reopened: 'Wieder geöffnet',
  };
  let label = EVENT_LABELS[eventType] || eventType;
  if (details) {
    if (details.from && details.to) label += `: ${details.from} → ${details.to}`;
    else if (details.value) label += `: ${details.value}`;
  }
  return label;
}

/* ═══════════ Icons ═══════════ */

export function CloseIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>;
}
export function TrashIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" /></svg>;
}
export function GripIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 9h16.5m-16.5 6.75h16.5" /></svg>;
}
export function PaperclipIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m18.375 12.739-7.693 7.693a4.5 4.5 0 0 1-6.364-6.364l10.94-10.94A3 3 0 1 1 19.5 7.372L8.552 18.32m.009-.01-.01.01m5.699-9.941-7.81 7.81a1.5 1.5 0 0 0 2.112 2.13" /></svg>;
}
export function DescIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H12" /></svg>;
}
export function ChecklistIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>;
}
export function AgentSmallIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" /></svg>;
}
export function UserIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z" /></svg>;
}
export function CalendarIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 0 1 2.25-2.25h13.5A2.25 2.25 0 0 1 21 7.5v11.25m-18 0A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75m-18 0v-7.5A2.25 2.25 0 0 1 5.25 9h13.5A2.25 2.25 0 0 1 21 11.25v7.5" /></svg>;
}
export function FolderIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 0 1 4.5 9.75h15A2.25 2.25 0 0 1 21.75 12v.75m-8.69-6.44-2.12-2.12a1.5 1.5 0 0 0-1.061-.44H4.5A2.25 2.25 0 0 0 2.25 6v12a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9a2.25 2.25 0 0 0-2.25-2.25h-5.379a1.5 1.5 0 0 1-1.06-.44Z" /></svg>;
}
export function ColumnsIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9 4.5v15m6-15v15m-10.875 0h15.75c.621 0 1.125-.504 1.125-1.125V5.625c0-.621-.504-1.125-1.125-1.125H4.125C3.504 4.5 3 5.004 3 5.625v12.75c0 .621.504 1.125 1.125 1.125Z" /></svg>;
}
export function TagIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.568 3H5.25A2.25 2.25 0 0 0 3 5.25v4.318c0 .597.237 1.17.659 1.591l9.581 9.581c.699.699 1.78.872 2.607.33a18.095 18.095 0 0 0 5.223-5.223c.542-.827.369-1.908-.33-2.607L11.16 3.66A2.25 2.25 0 0 0 9.568 3Z" /><path strokeLinecap="round" strokeLinejoin="round" d="M6 6h.008v.008H6V6Z" /></svg>;
}
export function AgendaIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 6a2.25 2.25 0 0 1 2.25-2.25H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z" /></svg>;
}
export function RepeatIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182M21.015 4.356v4.992" /></svg>;
}
export function ClockIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>;
}
export function ShieldIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" /></svg>;
}
export function LockIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 1 0-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 0 0 2.25-2.25v-6.75a2.25 2.25 0 0 0-2.25-2.25H6.75a2.25 2.25 0 0 0-2.25 2.25v6.75a2.25 2.25 0 0 0 2.25 2.25Z" /></svg>;
}
export function ModalIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h12A2.25 2.25 0 0 1 20.25 6v12A2.25 2.25 0 0 1 18 20.25H6A2.25 2.25 0 0 1 3.75 18V6Z" /><path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9M7.5 12h9M7.5 15.75h5.25" /></svg>;
}
export function PanelIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h12A2.25 2.25 0 0 1 20.25 6v12A2.25 2.25 0 0 1 18 20.25H6A2.25 2.25 0 0 1 3.75 18V6Z" /><path strokeLinecap="round" strokeLinejoin="round" d="M15 3.75v16.5" /></svg>;
}
export function FullscreenIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9m11.25-5.25v4.5m0-4.5h-4.5m4.5 0L15 9m5.25 11.25v-4.5m0 4.5h-4.5m4.5 0L15 15m-11.25 5.25v-4.5m0 4.5h4.5m-4.5 0L9 15" /></svg>;
}
export function ActivityIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>;
}
export function CommentDotIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z" /></svg>;
}
export function HistoryIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182M21.015 4.356v4.992" /></svg>;
}
export function CrmLinkIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z" /></svg>;
}
export function MoreHorizontalIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0ZM12.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0ZM18.75 12a.75.75 0 1 1-1.5 0 .75.75 0 0 1 1.5 0Z" /></svg>;
}
export function CopyIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 0 1 1.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 0 0-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 0 1-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 0 0-3.375-3.375h-1.5a1.125 1.125 0 0 1-1.125-1.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H9.75" /></svg>;
}
export function LinkIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 0 1 1.242 7.244l-4.5 4.5a4.5 4.5 0 0 1-6.364-6.364l1.757-1.757m13.35-.622 1.757-1.757a4.5 4.5 0 0 0-6.364-6.364l-4.5 4.5a4.5 4.5 0 0 0 1.242 7.244" /></svg>;
}
export function MailIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75" /></svg>;
}
