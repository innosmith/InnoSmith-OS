import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FileText, Save, AlertCircle, Check, ChevronLeft, RotateCcw, Wrench, Eye, Pencil,
} from 'lucide-react';
import { api } from '../../api/client';
import { MarkdownView } from '../MarkdownView';

interface AgentSkill {
  name: string;
  description: string;
  content: string;
  requires_toolsets: string[];
  size: number;
}

interface SkillUsageItem {
  name: string;
  view_count: number;
  last_used_at: string | null;
}

/** Blendet den führenden YAML-Frontmatter-Block (--- … ---) nur für die
 *  Vorschau aus, damit er nicht als Trennlinie plus Fliesstext erscheint.
 *  Der Bearbeiten-Modus bleibt vollständig (inkl. Frontmatter). */
function skillBody(md: string): string {
  return md.replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---\r?\n?/, '').trimStart();
}

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

export function SkillsPanel() {
  const [skills, setSkills] = useState<AgentSkill[]>([]);
  const [usage, setUsage] = useState<Record<string, SkillUsageItem>>({});
  const [usageLoaded, setUsageLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [mode, setMode] = useState<'preview' | 'edit'>('preview');

  const load = useCallback(async () => {
    try {
      const [sk, su] = await Promise.all([
        api.get<{ skills: AgentSkill[] }>('/api/intelligence/skills'),
        api.get<{ items: SkillUsageItem[] }>('/api/intelligence/skill-usage?jobs_limit=500')
          .then((r) => ({ ok: true as const, items: r.items }))
          .catch(() => ({ ok: false as const, items: [] as SkillUsageItem[] })),
      ]);
      const list = sk.skills ?? [];
      setSkills(list);
      setUsage(Object.fromEntries((su.items ?? []).map((i) => [i.name, i])));
      setUsageLoaded(su.ok);
      setSelected((cur) => cur ?? (list[0]?.name ?? null));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const current = useMemo(() => skills.find((s) => s.name === selected) ?? null, [skills, selected]);
  const dirty = current !== null && draft !== current.content;

  useEffect(() => {
    if (current) setDraft(current.content);
    setSavedNote(null);
    setError(null);
    setMode('preview');
  }, [current]);

  const selectSkill = (name: string) => {
    if (dirty && !window.confirm('Ungespeicherte Änderungen verwerfen?')) return;
    setSelected(name);
  };

  const handleSave = async () => {
    if (!current || !dirty) return;
    setSaving(true);
    setError(null);
    setSavedNote(null);
    try {
      const res = await api.patch<{ name: string; size: number; backup: string }>(
        `/api/intelligence/skills/${encodeURIComponent(current.name)}`,
        { content: draft },
      );
      setSkills((prev) => prev.map((s) => (s.name === current.name ? { ...s, content: draft, size: res.size } : s)));
      setSavedNote(`Gespeichert · Backup: ${res.backup}`);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  };

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
        <div className="mb-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Skills</h2>
          <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">
            Die Fähigkeiten des Agenten als Markdown — direkt editierbar. Jede Speicherung legt automatisch ein Backup an.
          </p>
        </div>

        {skills.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-gray-300 py-12 text-center dark:border-gray-700">
            <FileText className="mb-2 h-8 w-8 text-gray-300" />
            <p className="text-sm text-gray-500 dark:text-gray-400">Keine Skills gefunden.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[250px_1fr]">
            {/* Liste */}
            <div className={`space-y-1.5 ${selected ? 'hidden lg:block' : ''}`}>
              {skills.map((s) => {
                const u = usage[s.name];
                const active = s.name === selected;
                return (
                  <button
                    key={s.name}
                    onClick={() => selectSkill(s.name)}
                    className={`w-full rounded-xl border p-3 text-left transition-colors ${
                      active
                        ? 'border-indigo-500 bg-indigo-50 dark:border-indigo-500/60 dark:bg-indigo-900/20'
                        : 'border-gray-200 bg-white hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800/50 dark:hover:bg-gray-800'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <FileText className={`h-4 w-4 shrink-0 ${active ? 'text-indigo-600 dark:text-indigo-400' : 'text-gray-400'}`} />
                      <span className="truncate text-sm font-medium text-gray-900 dark:text-white">{s.name}</span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">{s.description}</p>
                    {usageLoaded && (
                      (u?.view_count ?? 0) > 0 ? (
                        <span className="mt-1.5 inline-block rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                          {u!.view_count}× genutzt
                        </span>
                      ) : (
                        <span className="mt-1.5 inline-block rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-700/60 dark:text-gray-400">
                          noch nie genutzt
                        </span>
                      )
                    )}
                  </button>
                );
              })}
            </div>

            {/* Editor */}
            {current && (
              <div className={`${selected ? '' : 'hidden lg:block'} flex min-w-0 flex-col`}>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <button
                      onClick={() => setSelected(null)}
                      className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 lg:hidden dark:hover:bg-gray-800"
                      title="Zurück zur Liste"
                    >
                      <ChevronLeft className="h-5 w-5" />
                    </button>
                    <h3 className="truncate text-sm font-semibold text-gray-900 dark:text-white">{current.name}</h3>
                    {dirty && <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" title="Ungespeicherte Änderungen" />}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <div className="mr-1 flex rounded-lg border border-gray-200 p-0.5 dark:border-gray-700">
                      {([
                        ['preview', 'Vorschau', Eye],
                        ['edit', 'Bearbeiten', Pencil],
                      ] as const).map(([m, label, Icon]) => (
                        <button
                          key={m}
                          onClick={() => setMode(m)}
                          className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                            mode === m
                              ? 'bg-indigo-600 text-white'
                              : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
                          }`}
                        >
                          <Icon className="h-3.5 w-3.5" /> {label}
                        </button>
                      ))}
                    </div>
                    {dirty && (
                      <button
                        onClick={() => setDraft(current.content)}
                        className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
                        title="Änderungen verwerfen"
                      >
                        <RotateCcw className="h-3.5 w-3.5" /> Zurücksetzen
                      </button>
                    )}
                    <button
                      onClick={handleSave}
                      disabled={!dirty || saving}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
                    >
                      <Save className="h-4 w-4" /> {saving ? 'Speichern…' : 'Speichern'}
                    </button>
                  </div>
                </div>

                {current.requires_toolsets.length > 0 && (
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    <Wrench className="h-3.5 w-3.5 text-gray-400" />
                    {current.requires_toolsets.map((ts) => (
                      <span key={ts} className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                        {ts}
                      </span>
                    ))}
                  </div>
                )}

                {mode === 'preview' ? (
                  <div className="min-h-[420px] flex-1 overflow-y-auto rounded-xl border border-gray-200 bg-gray-50 p-4 text-gray-800 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100">
                    <MarkdownView text={skillBody(draft)} />
                  </div>
                ) : (
                  <textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    spellCheck={false}
                    className="min-h-[420px] w-full flex-1 resize-y rounded-xl border border-gray-300 bg-gray-50 p-4 font-mono text-[13px] leading-relaxed text-gray-800 focus:border-indigo-500 focus:bg-white focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:focus:bg-gray-800"
                  />
                )}

                <div className="mt-2 flex min-h-[20px] items-center justify-between text-xs">
                  <span className="text-gray-400 dark:text-gray-500">{draft.length.toLocaleString('de-CH')} Zeichen · Markdown</span>
                  {savedNote && (
                    <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                      <Check className="h-3.5 w-3.5" /> {savedNote}
                    </span>
                  )}
                </div>

                {error && (
                  <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{error}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
