import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  Plus, Pencil, Trash2, Check, X, ScrollText, Zap, AlertCircle,
  Power, PowerOff, ArrowRight,
} from 'lucide-react';
import { api } from '../../api/client';
import { useSSE } from '../../hooks/useSSE';

type RuleType = 'llm' | 'deterministic';

interface RuleCondition {
  field: string;
  op: string;
  value: string;
}

interface RuleAction {
  triage_class?: string;
  category?: string | null;
  folder?: string | null;
}

interface LearnedRule {
  id: string;
  scope: string;
  rule_text: string;
  status: string;
  rule_type: RuleType;
  match_conditions: RuleCondition[];
  action: RuleAction;
  priority: number;
  applied_count: number;
  created_at: string | null;
  approved_at: string | null;
}

const SCOPE_LABELS: Record<string, string> = {
  triage: 'Triage',
  draft: 'Entwurf',
  chat: 'Chat',
  general: 'Allgemein',
  task: 'Aufgabe',
  calendar: 'Kalender',
};

const LLM_SCOPES = ['triage', 'draft', 'chat', 'task', 'general'] as const;
const FIELD_LABELS: Record<string, string> = { sender: 'Absender', domain: 'Domain', subject: 'Betreff' };
const OP_LABELS: Record<string, string> = { equals: 'ist gleich', contains: 'enthält' };
const FIELDS = ['sender', 'domain', 'subject'] as const;
const OPS = ['equals', 'contains'] as const;

const STATUS_META: Record<string, { label: string; cls: string }> = {
  active: { label: 'Aktiv', cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300' },
  proposed: { label: 'Vorschlag', cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' },
  rejected: { label: 'Verworfen', cls: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400' },
  archived: { label: 'Deaktiviert', cls: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400' },
};

type TypeFilter = 'all' | RuleType;
type StatusFilter = 'all' | 'active' | 'proposed' | 'inactive';

function errMsg(e: unknown): string {
  if (e instanceof Error) {
    try {
      const j = JSON.parse(e.message);
      if (j?.detail) return String(j.detail);
    } catch { /* kein JSON */ }
    return e.message.slice(0, 200);
  }
  return 'Unbekannter Fehler';
}

export function RulesManager() {
  const [rules, setRules] = useState<LearnedRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [editorRule, setEditorRule] = useState<LearnedRule | 'new' | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get<{ rules: LearnedRule[] }>('/api/intelligence/rules?limit=200');
      setRules(res.rules ?? []);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useSSE((event) => { if (event === 'learned_rules_changed') load(); });

  const filtered = useMemo(() => rules.filter((r) => {
    if (typeFilter !== 'all' && r.rule_type !== typeFilter) return false;
    if (statusFilter === 'active' && r.status !== 'active') return false;
    if (statusFilter === 'proposed' && r.status !== 'proposed') return false;
    if (statusFilter === 'inactive' && !['rejected', 'archived'].includes(r.status)) return false;
    return true;
  }), [rules, typeFilter, statusFilter]);

  const counts = useMemo(() => ({
    proposed: rules.filter((r) => r.status === 'proposed').length,
    active: rules.filter((r) => r.status === 'active').length,
  }), [rules]);

  const mutate = async (fn: () => Promise<void>, id: string) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusyId(null);
    }
  };

  const setStatus = (r: LearnedRule, status: string) =>
    mutate(() => api.patch(`/api/intelligence/rules/${r.id}`, { status }), r.id);

  const remove = (r: LearnedRule) =>
    mutate(() => api.delete(`/api/intelligence/rules/${r.id}`), r.id);

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <div className="h-9 w-9 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl">
      <div className="rounded-2xl border border-white/40 bg-white/70 p-5 shadow-sm backdrop-blur-sm sm:p-6 dark:border-gray-800 dark:bg-gray-900/70">
        {/* Kopf */}
        <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Regeln</h2>
            <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">
              Leitregeln fürs Modell und deterministische Overrides — gepflegt, einsehbar, freigegeben.
            </p>
          </div>
          <button
            onClick={() => setEditorRule('new')}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-indigo-700"
          >
            <Plus className="h-4 w-4" /> Neue Regel
          </button>
        </div>

        {/* Filter */}
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <SegFilter
            value={typeFilter}
            onChange={setTypeFilter}
            options={[
              { id: 'all', label: 'Alle Typen' },
              { id: 'llm', label: 'Leitregeln' },
              { id: 'deterministic', label: 'Deterministisch' },
            ]}
          />
          <span className="h-5 w-px bg-gray-200 dark:bg-gray-700" />
          <SegFilter
            value={statusFilter}
            onChange={setStatusFilter}
            options={[
              { id: 'all', label: 'Alle' },
              { id: 'active', label: `Aktiv${counts.active ? ` (${counts.active})` : ''}` },
              { id: 'proposed', label: `Vorschläge${counts.proposed ? ` (${counts.proposed})` : ''}` },
              { id: 'inactive', label: 'Inaktiv' },
            ]}
          />
        </div>

        {error && (
          <div className="mb-4 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{error}</span>
            <button onClick={() => setError(null)} className="underline">Schliessen</button>
          </div>
        )}

        {/* Liste */}
        {filtered.length === 0 ? (
          <EmptyState onCreate={() => setEditorRule('new')} hasRules={rules.length > 0} />
        ) : (
          <div className="space-y-2.5">
            {filtered.map((rule) => (
              <RuleCard
                key={rule.id}
                rule={rule}
                busy={busyId === rule.id}
                onEdit={() => setEditorRule(rule)}
                onDelete={() => remove(rule)}
                onSetStatus={(s) => setStatus(rule, s)}
              />
            ))}
          </div>
        )}
      </div>

      {editorRule && (
        <RuleEditor
          rule={editorRule === 'new' ? null : editorRule}
          onClose={() => setEditorRule(null)}
          onSaved={() => { setEditorRule(null); load(); }}
        />
      )}
    </div>
  );
}

/* ── Filter-Segment ── */

function SegFilter<T extends string>({
  value, onChange, options,
}: { value: T; onChange: (v: T) => void; options: { id: T; label: string }[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <button
          key={o.id}
          onClick={() => onChange(o.id)}
          className={`rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors ${
            value === o.id
              ? 'bg-indigo-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ── Regel-Karte ── */

function RuleCard({
  rule, busy, onEdit, onDelete, onSetStatus,
}: {
  rule: LearnedRule;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onSetStatus: (status: string) => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isDet = rule.rule_type === 'deterministic';
  const st = STATUS_META[rule.status] ?? { label: rule.status, cls: 'bg-gray-100 text-gray-500' };

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-3.5 transition-shadow hover:shadow-sm dark:border-gray-700 dark:bg-gray-800/60">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ${
              isDet
                ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300'
                : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
            }`}>
              {isDet ? <Zap className="h-3 w-3" /> : <ScrollText className="h-3 w-3" />}
              {isDet ? 'Deterministisch' : 'Leitregel'}
            </span>
            {!isDet && (
              <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300">
                {SCOPE_LABELS[rule.scope] ?? rule.scope}
              </span>
            )}
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${st.cls}`}>{st.label}</span>
            {isDet && rule.applied_count > 0 && (
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-300">
                {rule.applied_count}× angewandt
              </span>
            )}
          </div>

          <p className="text-sm font-medium text-gray-800 dark:text-gray-100">{rule.rule_text}</p>

          {isDet && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
              {(rule.match_conditions ?? []).map((c, i) => (
                <span key={i} className="rounded-md bg-gray-100 px-2 py-1 font-medium text-gray-600 dark:bg-gray-700/70 dark:text-gray-300">
                  {FIELD_LABELS[c.field] ?? c.field} {OP_LABELS[c.op] ?? c.op} <span className="font-mono text-gray-800 dark:text-gray-100">{c.value}</span>
                </span>
              ))}
              <ArrowRight className="h-3.5 w-3.5 text-gray-400" />
              <span className="rounded-md bg-emerald-50 px-2 py-1 font-medium text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-300">
                {rule.action?.triage_class === 'task' ? 'Aufgabe' : 'FYI'}
                {rule.action?.category ? ` · Kategorie ${rule.action.category}` : ''}
                {rule.action?.folder ? ` · Move ${rule.action.folder}` : ''}
              </span>
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <button
            onClick={onEdit}
            disabled={busy}
            className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-indigo-50 hover:text-indigo-600 disabled:opacity-50 dark:hover:bg-indigo-950/30 dark:hover:text-indigo-400"
            title="Bearbeiten"
          >
            <Pencil className="h-4 w-4" />
          </button>
          {confirmDelete ? (
            <button
              onClick={onDelete}
              disabled={busy}
              className="rounded-lg bg-red-100 px-2 py-1 text-xs font-medium text-red-600 hover:bg-red-200 disabled:opacity-50 dark:bg-red-900/30 dark:text-red-400"
              title="Endgültig löschen"
            >
              Sicher?
            </button>
          ) : (
            <button
              onClick={() => { setConfirmDelete(true); setTimeout(() => setConfirmDelete(false), 3000); }}
              disabled={busy}
              className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-red-50 hover:text-red-500 disabled:opacity-50 dark:hover:bg-red-950/30 dark:hover:text-red-400"
              title="Löschen"
            >
              <Trash2 className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      {/* Status-Aktionen */}
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t border-gray-100 pt-2.5 dark:border-gray-700/60">
        {rule.status === 'proposed' && (
          <>
            <button
              onClick={() => onSetStatus('active')}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              <Check className="h-3.5 w-3.5" /> Freigeben
            </button>
            <button
              onClick={() => onSetStatus('rejected')}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              <X className="h-3.5 w-3.5" /> Verwerfen
            </button>
          </>
        )}
        {rule.status === 'active' && (
          <button
            onClick={() => onSetStatus('archived')}
            disabled={busy}
            className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <PowerOff className="h-3.5 w-3.5" /> Deaktivieren
          </button>
        )}
        {(rule.status === 'archived' || rule.status === 'rejected') && (
          <button
            onClick={() => onSetStatus('active')}
            disabled={busy}
            className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            <Power className="h-3.5 w-3.5" /> Aktivieren
          </button>
        )}
      </div>
    </div>
  );
}

/* ── Leerzustand ── */

function EmptyState({ onCreate, hasRules }: { onCreate: () => void; hasRules: boolean }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-gray-300 py-12 text-center dark:border-gray-700">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-indigo-50 dark:bg-indigo-900/30">
        <ScrollText className="h-6 w-6 text-indigo-500" />
      </div>
      <p className="text-sm font-medium text-gray-700 dark:text-gray-200">
        {hasRules ? 'Keine Regeln in dieser Ansicht' : 'Noch keine Regeln'}
      </p>
      <p className="mt-1 max-w-sm text-xs text-gray-500 dark:text-gray-400">
        {hasRules
          ? 'Passe die Filter an oder lege eine neue Regel an.'
          : 'Lege eine Leitregel fürs Modell oder eine deterministische Override (z. B. Absender → Ordner) an.'}
      </p>
      <button
        onClick={onCreate}
        className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-indigo-700"
      >
        <Plus className="h-4 w-4" /> Neue Regel
      </button>
    </div>
  );
}

/* ── Editor (Anlegen / Bearbeiten) ── */

function RuleEditor({
  rule, onClose, onSaved,
}: { rule: LearnedRule | null; onClose: () => void; onSaved: () => void }) {
  const isEdit = rule !== null;
  const [ruleType, setRuleType] = useState<RuleType>(rule?.rule_type ?? 'llm');
  const [ruleText, setRuleText] = useState(rule?.rule_text ?? '');
  const [scope, setScope] = useState(rule?.scope ?? 'triage');
  const [priority, setPriority] = useState(rule?.priority ?? 100);
  const [conditions, setConditions] = useState<RuleCondition[]>(
    rule?.match_conditions?.length ? rule.match_conditions : [{ field: 'domain', op: 'equals', value: '' }],
  );
  const [triageClass, setTriageClass] = useState(rule?.action?.triage_class ?? 'fyi');
  const [category, setCategory] = useState(rule?.action?.category ?? '');
  const [folder, setFolder] = useState(rule?.action?.folder ?? '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const isDet = ruleType === 'deterministic';

  const addCondition = () => setConditions((c) => [...c, { field: 'subject', op: 'contains', value: '' }]);
  const removeCondition = (i: number) => setConditions((c) => c.filter((_, idx) => idx !== i));
  const updateCondition = (i: number, patch: Partial<RuleCondition>) =>
    setConditions((c) => c.map((cond, idx) => (idx === i ? { ...cond, ...patch } : cond)));

  const handleSave = async () => {
    setErr(null);
    if (!ruleText.trim()) {
      setErr(isDet ? 'Bitte eine kurze Bezeichnung für die Regel angeben.' : 'Bitte den Regeltext angeben.');
      return;
    }
    const cleanConds = conditions.map((c) => ({ ...c, value: c.value.trim() })).filter((c) => c.value);
    if (isDet && cleanConds.length === 0) {
      setErr('Eine deterministische Regel braucht mindestens eine Bedingung mit Wert.');
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, unknown> = {
        rule_text: ruleText.trim(),
        scope: isDet ? 'triage' : scope,
      };
      if (isDet) {
        body.match_conditions = cleanConds;
        body.action = {
          triage_class: triageClass,
          category: category.trim() || null,
          folder: folder.trim() || null,
        };
        body.priority = priority;
      }
      if (isEdit) {
        await api.patch(`/api/intelligence/rules/${rule!.id}`, body);
      } else {
        body.rule_type = ruleType;
        await api.post('/api/intelligence/rules', body);
      }
      onSaved();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm modal-safe" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-full w-full max-w-xl lg:max-h-[90vh] flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="flex items-center justify-between border-b border-gray-100 p-4 dark:border-gray-800">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            {isEdit ? 'Regel bearbeiten' : 'Neue Regel'}
          </h2>
          <button onClick={onClose} className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4">
          {/* Typ-Auswahl (nur beim Anlegen) */}
          {!isEdit && (
            <div>
              <Label>Regeltyp</Label>
              <div className="grid grid-cols-2 gap-2">
                <TypeOption
                  active={!isDet}
                  onClick={() => setRuleType('llm')}
                  icon={<ScrollText className="h-4 w-4" />}
                  title="Leitregel"
                  desc="Text fürs Modell, je nach Kontext"
                />
                <TypeOption
                  active={isDet}
                  onClick={() => setRuleType('deterministic')}
                  icon={<Zap className="h-4 w-4" />}
                  title="Deterministisch"
                  desc="Bedingung → Aktion, ohne Modell"
                />
              </div>
            </div>
          )}

          {/* Bezeichnung / Text */}
          <div>
            <Label>{isDet ? 'Bezeichnung' : 'Regeltext'}</Label>
            <textarea
              value={ruleText}
              onChange={(e) => setRuleText(e.target.value)}
              rows={isDet ? 2 : 3}
              placeholder={isDet
                ? 'z. B. Newsletter von example.ch ablegen'
                : 'z. B. Terminzusagen sind reine Info (fyi), niemals eine Aufgabe.'}
              className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
            />
          </div>

          {/* Scope (nur Leitregel) */}
          {!isDet && (
            <div>
              <Label>Kontext (wo die Regel wirkt)</Label>
              <div className="flex flex-wrap gap-1.5">
                {LLM_SCOPES.map((s) => (
                  <button
                    key={s}
                    onClick={() => setScope(s)}
                    className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${
                      scope === s
                        ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400'
                        : 'border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-800'
                    }`}
                  >
                    {SCOPE_LABELS[s]}
                  </button>
                ))}
              </div>
              <p className="mt-1.5 text-xs text-gray-400 dark:text-gray-500">
                „Allgemein" wirkt in jedem Kontext. „Triage"/„Entwurf" greifen bei der E-Mail-Verarbeitung, „Chat" im Agent-Chat, „Aufgabe" bei delegierten Agent-Aufgaben.
              </p>
            </div>
          )}

          {/* Deterministisch: Bedingungen + Aktion */}
          {isDet && (
            <>
              <div>
                <Label>Bedingungen (alle müssen zutreffen)</Label>
                <div className="space-y-2">
                  {conditions.map((c, i) => (
                    <div key={i} className="flex items-center gap-1.5">
                      <select
                        value={c.field}
                        onChange={(e) => updateCondition(i, { field: e.target.value })}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      >
                        {FIELDS.map((f) => <option key={f} value={f}>{FIELD_LABELS[f]}</option>)}
                      </select>
                      <select
                        value={c.op}
                        onChange={(e) => updateCondition(i, { op: e.target.value })}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      >
                        {OPS.map((o) => <option key={o} value={o}>{OP_LABELS[o]}</option>)}
                      </select>
                      <input
                        value={c.value}
                        onChange={(e) => updateCondition(i, { value: e.target.value })}
                        placeholder={c.field === 'subject' ? 'Text…' : 'z. B. example.ch'}
                        className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-sm text-gray-900 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      />
                      <button
                        onClick={() => removeCondition(i)}
                        disabled={conditions.length === 1}
                        className="rounded-lg p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-30 dark:hover:bg-red-950/30"
                        title="Bedingung entfernen"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  onClick={addCondition}
                  className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-700 dark:text-indigo-400"
                >
                  <Plus className="h-3.5 w-3.5" /> Bedingung hinzufügen
                </button>
              </div>

              <div>
                <Label>Aktion</Label>
                <div className="flex gap-2">
                  {(['fyi', 'task'] as const).map((tc) => (
                    <button
                      key={tc}
                      onClick={() => setTriageClass(tc)}
                      className={`flex-1 rounded-lg border px-3 py-1.5 text-sm transition-colors ${
                        triageClass === tc
                          ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400'
                          : 'border-gray-200 text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-800'
                      }`}
                    >
                      {tc === 'fyi' ? 'Nur ablegen (FYI)' : 'Aufgabe erstellen'}
                    </button>
                  ))}
                </div>
                {triageClass === 'task' && (
                  <p className="mt-1.5 flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    Erstellt automatisch eine Aufgabe — bewusst sparsam einsetzen.
                  </p>
                )}
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <input
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                    placeholder="Outlook-Kategorie (optional)"
                    className="rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-sm text-gray-900 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                  <input
                    value={folder}
                    onChange={(e) => setFolder(e.target.value)}
                    placeholder="Zielordner (optional)"
                    className="rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-sm text-gray-900 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                </div>
              </div>

              <div className="flex items-center gap-2">
                <Label className="mb-0">Priorität</Label>
                <input
                  type="number"
                  value={priority}
                  onChange={(e) => setPriority(parseInt(e.target.value, 10) || 0)}
                  className="w-24 rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                />
                <span className="text-xs text-gray-400 dark:text-gray-500">kleiner = zuerst geprüft</span>
              </div>
            </>
          )}

          {err && (
            <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{err}</span>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-gray-100 p-4 dark:border-gray-800">
          <button
            onClick={onClose}
            className="rounded-lg px-3.5 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            Abbrechen
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
          >
            {saving ? 'Speichern…' : isEdit ? 'Speichern' : 'Regel anlegen'}
          </button>
        </div>
      </div>
    </div>
  );
}

function Label({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <label className={`mb-1.5 block text-xs font-semibold text-gray-600 dark:text-gray-400 ${className}`}>{children}</label>;
}

function TypeOption({
  active, onClick, icon, title, desc,
}: { active: boolean; onClick: () => void; icon: ReactNode; title: string; desc: string }) {
  return (
    <button
      onClick={onClick}
      className={`flex flex-col items-start gap-1 rounded-xl border p-3 text-left transition-colors ${
        active
          ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/20'
          : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800'
      }`}
    >
      <span className={`flex items-center gap-1.5 text-sm font-semibold ${active ? 'text-indigo-700 dark:text-indigo-300' : 'text-gray-700 dark:text-gray-200'}`}>
        {icon} {title}
      </span>
      <span className="text-xs text-gray-500 dark:text-gray-400">{desc}</span>
    </button>
  );
}
