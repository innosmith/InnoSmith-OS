import { useState, useEffect, useRef, useCallback, useMemo, Fragment } from 'react';
import { api } from '../api/client';
import { fileTypeLabel } from '../lib/fileTypeLabel';
import { clusterDocHits } from '../lib/seriesCluster';

interface SearchDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onTaskClick: (taskId: string) => void;
  onProjectClick: (projectId: string) => void;
}

interface SearchTask {
  id: string;
  title: string;
  project_id: string;
  project_name: string;
  assignee: string;
  is_completed: boolean;
  due_date: string;
}

interface SearchProject {
  id: string;
  name: string;
  color: string;
  status: string;
}

interface SearchTag {
  id: string;
  name: string;
  color: string;
}

interface CrmHit {
  id: number | string;
  name: string;
  type: string;
  detail: string | null;
  email: string | null;
  pic_url: string | null;
}

interface TogglHit {
  id: number;
  name: string;
  type: string; // "client" | "project"
  workspace_id: number | null;
}

interface BexioHit {
  id: number;
  name: string;
  type: string; // "contact"
  email: string | null;
}

interface SignaHit {
  id: number;
  title: string;
  type: string; // "rss" | "youtube" | "web"
  score: number | null;
  source: string | null;
}

interface SearchResults {
  tasks: SearchTask[];
  projects: SearchProject[];
  tags: SearchTag[];
  crm: CrmHit[];
  toggl: TogglHit[];
  bexio: BexioHit[];
  signa: SignaHit[];
}

// Fusioniertes Dokument-/E-Mail-Ergebnis (Backend /api/search/documents):
// OneDrive-Live + lokaler Hybrid-Index, dedupliziert und via RRF gerankt.
interface DocHit {
  source_type: string; // 'email' | 'onedrive' | 'upload' | 'transcript'
  id: string;
  title: string | null;
  url: string | null;
  mime_type: string | null;
  snippet: string | null;
  score: number | null;
  matched_keyword?: boolean; // true = klarer Keyword-Treffer, false = nur semantisch
}

type ResultItem =
  | { kind: 'task'; data: SearchTask }
  | { kind: 'project'; data: SearchProject }
  | { kind: 'tag'; data: SearchTag }
  | { kind: 'crm'; data: CrmHit }
  | { kind: 'toggl'; data: TogglHit }
  | { kind: 'bexio'; data: BexioHit }
  | { kind: 'signa'; data: SignaHit }
  | { kind: 'doc'; data: DocHit };

// Sektions-Überschrift je Treffertyp. Dokumente & E-Mails stehen bewusst zuoberst
// (reichhaltigste, fusionierte Quelle).
const SECTION_LABELS: Record<ResultItem['kind'], string> = {
  doc: 'Dokumente & E-Mails',
  task: 'Tasks',
  project: 'Projekte',
  tag: 'Tags',
  crm: 'CRM (Pipedrive)',
  toggl: 'Toggl Track',
  bexio: 'Bexio',
  signa: 'SIGNA Signale',
};

// Anzeige-Zeile: entweder ein Einzel-Treffer oder ein aufklappbarer Serien-Cluster
// (nur in der Dokument-/E-Mail-Sektion). Navigations-Ziel für die Tastatursteuerung:
// ein Cluster-Header ist selbst navigierbar (Enter = auf-/zuklappen).
type DisplayRow =
  | { kind: 'item'; item: ResultItem; dimmed?: boolean }
  | { kind: 'cluster'; key: string; label: string; count: number; members: DocHit[]; dimmed?: boolean }
  | { kind: 'divider'; label: string };
type NavTarget =
  | { kind: 'item'; item: ResultItem }
  | { kind: 'clusterHeader'; key: string };

// Facetten-Label pro Treffer für die globale Typ-Filterleiste. Dokumente werden
// nach echtem Dateityp aufgeschlüsselt (E-Mail, PDF, Word, …), alle übrigen Quellen
// nach ihrer Kategorie -- so ist z. B. "Kontakt" (Pipedrive) filterbar.
function itemFacet(item: ResultItem): string {
  switch (item.kind) {
    case 'doc':
      return fileTypeLabel(item.data.mime_type, item.data.title, item.data.source_type);
    case 'task':
      return 'Aufgabe';
    case 'project':
      return 'Projekt';
    case 'tag':
      return 'Tag';
    case 'crm':
      return 'Kontakt';
    case 'toggl':
      return 'Toggl';
    case 'bexio':
      return 'Bexio';
    case 'signa':
      return 'SIGNA';
  }
}

export function SearchDialog({
  isOpen,
  onClose,
  onTaskClick,
  onProjectClick,
}: SearchDialogProps) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResults | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  // Fusionierte Dokument-/E-Mail-Suche -- laeuft automatisch (debounced) parallel zum
  // schnellen Instant-Pfad, kein bewusster Trigger noetig.
  const [docHits, setDocHits] = useState<DocHit[]>([]);
  const [docLoading, setDocLoading] = useState(false);
  // Optionaler Typ-Filter für die (potenziell lange) Dokument-/E-Mail-Sektion.
  // null = "Alle". Die Facetten werden dynamisch aus den Treffern abgeleitet.
  const [typeFilter, setTypeFilter] = useState<string | null>(null);
  // Aufgeklappte Serien-Cluster (Schlüssel aus seriesCluster.ts). Default: alle zu.
  const [expandedSeries, setExpandedSeries] = useState<Set<string>>(new Set());
  const inputRef = useRef<HTMLInputElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Alle Treffer als flache Liste in Anzeige-Reihenfolge (Dokumente & E-Mails zuerst),
  // quellenübergreifend. Basis für Facetten, Filter, Gruppierung und Tastaturnavigation.
  const baseItems = useMemo<ResultItem[]>(() => {
    const items: ResultItem[] = [];
    for (const h of docHits) items.push({ kind: 'doc', data: h });
    if (results) {
      for (const t of results.tasks) items.push({ kind: 'task', data: t });
      for (const p of results.projects) items.push({ kind: 'project', data: p });
      for (const tag of results.tags) items.push({ kind: 'tag', data: tag });
      for (const c of (results.crm || [])) items.push({ kind: 'crm', data: c });
      for (const t of (results.toggl || [])) items.push({ kind: 'toggl', data: t });
      for (const b of (results.bexio || [])) items.push({ kind: 'bexio', data: b });
      for (const s of (results.signa || [])) items.push({ kind: 'signa', data: s });
    }
    return items;
  }, [results, docHits]);

  // Globale Facetten (Chip-Leiste) über ALLE Treffertypen. E-Mail und Kontakt zuerst
  // (häufigste Such-Intents), dann nach Häufigkeit, dann alphabetisch.
  const facets = useMemo(() => {
    const counts = new Map<string, number>();
    for (const it of baseItems) {
      const label = itemFacet(it);
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    const priority = (l: string) => (l === 'E-Mail' ? 0 : l === 'Kontakt' ? 1 : 2);
    return Array.from(counts.entries())
      .sort((a, b) => {
        const pa = priority(a[0]);
        const pb = priority(b[0]);
        if (pa !== pb) return pa - pb;
        if (b[1] !== a[1]) return b[1] - a[1];
        return a[0].localeCompare(b[0], 'de');
      })
      .map(([label, count]) => ({ label, count }));
  }, [baseItems]);

  // Sichtbare Treffer nach aktivem Filter (null = alle Typen).
  const visibleItems = useMemo(
    () => (typeFilter ? baseItems.filter((it) => itemFacet(it) === typeFilter) : baseItems),
    [baseItems, typeFilter],
  );

  // Eindeutiger Kontakt: genau eine Person im CRM-Ergebnis. Wird zusätzlich zuoberst
  // angeheftet, damit sofort sichtbar ist, um wen es geht (Klick -> Pipedrive). Bei
  // 0 oder mehreren Personen keine Anheftung (nicht eindeutig).
  const pinnedContact = useMemo<CrmHit | null>(() => {
    const persons = (results?.crm || []).filter((c) => c.type === 'person');
    return persons.length === 1 ? persons[0] : null;
  }, [results]);

  // Gruppierung folgt der Item-Reihenfolge; Sektionen erscheinen in der Reihenfolge
  // ihres ersten Treffers und enthalten nur die aktuell sichtbaren (gefilterten) Items.
  const grouped = useMemo(() => {
    const sections: { label: string; items: ResultItem[] }[] = [];
    const byLabel = new Map<string, ResultItem[]>();
    for (const it of visibleItems) {
      const label = SECTION_LABELS[it.kind];
      let bucket = byLabel.get(label);
      if (!bucket) {
        bucket = [];
        byLabel.set(label, bucket);
        sections.push({ label, items: bucket });
      }
      bucket.push(it);
    }
    return sections;
  }, [visibleItems]);

  // Anzeige-Modell: In der Dokument-/E-Mail-Sektion werden Serien (Rechnungs-Reihen,
  // E-Mail-Threads) zu aufklappbaren Cluster-Zeilen verdichtet; übrige Sektionen 1:1.
  const displaySections = useMemo(() => {
    // Baut Cluster-/Einzelzeilen für eine Doc-Trefferliste (optional abgeschwächt).
    const buildDocRows = (hits: DocHit[], dimmed: boolean): DisplayRow[] =>
      clusterDocHits<DocHit>(hits).map((r) =>
        r.kind === 'cluster'
          ? { kind: 'cluster', key: r.key, label: r.label, count: r.count, members: r.members, dimmed }
          : { kind: 'item', item: { kind: 'doc', data: r.hit }, dimmed },
      );

    const sections = grouped.map((section) => {
      if (section.items.length > 0 && section.items[0].kind === 'doc') {
        const hits = section.items.map((it) => (it as { kind: 'doc'; data: DocHit }).data);
        // Keyword-First-Differenzierung: klare Treffer (Begriff steht wirklich drin)
        // oben; rein-semantische Treffer darunter, abgeschwächt, unter einem Trenner.
        // Backend liefert bereits keyword-first sortiert -> reine Partition, kein Reorder.
        const strong = hits.filter((h) => h.matched_keyword);
        const related = hits.filter((h) => !h.matched_keyword);
        const rows: DisplayRow[] = [...buildDocRows(strong, false)];
        if (related.length > 0) {
          // Trenner nur, wenn es auch klare Treffer darüber gibt; sonst (rein
          // konzeptuelle Query) die verwandten Treffer normal, nicht abgeschwächt.
          if (strong.length > 0) {
            rows.push({ kind: 'divider', label: 'Möglicherweise verwandt' });
          }
          rows.push(...buildDocRows(related, strong.length > 0));
        }
        return { label: section.label, rows };
      }
      return {
        label: section.label,
        rows: section.items.map((item): DisplayRow => ({ kind: 'item', item })),
      };
    });
    // Eindeutigen Kontakt zuoberst anheften (zusätzlich zur regulären CRM-Sektion),
    // solange der aktive Filter Kontakte nicht ausblendet.
    if (pinnedContact && (!typeFilter || typeFilter === 'Kontakt')) {
      sections.unshift({
        label: 'Kontakt',
        rows: [{ kind: 'item', item: { kind: 'crm', data: pinnedContact } }],
      });
    }
    return sections;
  }, [grouped, pinnedContact, typeFilter]);

  // Flache Navigations-Ziele in Render-Reihenfolge (Tastatur/activeIndex). Ein
  // eingeklappter Cluster = 1 Ziel (Header); ausgeklappt = Header + Mitglieder.
  const navTargets = useMemo(() => {
    const targets: NavTarget[] = [];
    for (const section of displaySections) {
      for (const row of section.rows) {
        if (row.kind === 'item') {
          targets.push({ kind: 'item', item: row.item });
        } else if (row.kind === 'cluster') {
          targets.push({ kind: 'clusterHeader', key: row.key });
          if (expandedSeries.has(row.key)) {
            for (const m of row.members) targets.push({ kind: 'item', item: { kind: 'doc', data: m } });
          }
        }
        // 'divider' ist rein visuell und nicht navigierbar -> überspringen.
      }
    }
    return targets;
  }, [displaySections, expandedSeries]);

  const toggleSeries = useCallback((key: string) => {
    setExpandedSeries((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setActiveIndex(0);
  }, []);

  // Aktiven Filter zuruecksetzen, sobald der gewaehlte Typ nicht mehr vorkommt
  // (neue Query/neue Treffer) -- verhindert eine leere, "haengende" Filterung.
  useEffect(() => {
    if (typeFilter && !facets.some((f) => f.label === typeFilter)) setTypeFilter(null);
  }, [facets, typeFilter]);

  const selectFilter = useCallback((label: string | null) => {
    setTypeFilter(label);
    setActiveIndex(0);
    requestAnimationFrame(() => listRef.current?.scrollTo({ top: 0 }));
  }, []);

  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setResults(null);
      setDocHits([]);
      setTypeFilter(null);
      setExpandedSeries(new Set());
      setActiveIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [isOpen]);

  useEffect(() => {
    const q = query.trim();
    // Neue Query -> aufgeklappte Cluster zurücksetzen (Schlüssel wären ohnehin stale).
    setExpandedSeries(new Set());
    if (q.length < 2) {
      // Unter 2 Zeichen: keine Suche (weder Instant noch Dokumente/Semantik).
      setResults(null);
      setDocHits([]);
      setActiveIndex(0);
      return;
    }

    // Ein gemeinsamer Debounce (400 ms) startet BEIDE Pfade parallel: den schnellen
    // Instant-Pfad (Tasks, CRM, ...) und die fusionierte Dokument-/E-Mail-Suche
    // (OneDrive-Live + semantischer Index). Ein gemeinsamer AbortController schuetzt
    // vor veralteten Antworten; beide rendern progressiv, sobald sie eintreffen.
    const controller = new AbortController();
    const encoded = encodeURIComponent(q);
    const timeout = setTimeout(() => {
      setLoading(true);
      setDocLoading(true);
      api
        .get<SearchResults>(`/api/search?q=${encoded}`, { signal: controller.signal })
        .then((data) => {
          if (!controller.signal.aborted) {
            setResults(data);
            setActiveIndex(0);
          }
        })
        .catch((err) => {
          if (!controller.signal.aborted) {
            console.error('[SearchDialog] API-Fehler:', err);
            setResults(null);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });

      api
        .get<{ results: DocHit[] }>(
          `/api/search/documents?q=${encoded}&limit=100`,
          { signal: controller.signal },
        )
        .then((data) => {
          if (!controller.signal.aborted) setDocHits(data.results || []);
        })
        .catch((err) => {
          if (!controller.signal.aborted) {
            console.error('[SearchDialog] Dokument-Suche-Fehler:', err);
            setDocHits([]);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setDocLoading(false);
        });
    }, 400);

    return () => {
      clearTimeout(timeout);
      controller.abort();
    };
  }, [query]);

  const activateItem = useCallback(
    (item: ResultItem) => {
      if (item.kind === 'doc') {
        const h = item.data;
        if (h.source_type === 'email') {
          window.location.href = `/inbox?email=${encodeURIComponent(h.id)}`;
        } else if (h.url) {
          window.open(h.url, '_blank');
        }
        onClose();
        return;
      }
      if (item.kind === 'task') {
        onTaskClick(item.data.id);
        onClose();
      } else if (item.kind === 'project') {
        onProjectClick(item.data.id);
        onClose();
      } else if (item.kind === 'crm') {
        const crm = item.data;
        const pathMap: Record<string, string> = { person: 'person', deal: 'deal', organization: 'organization' };
        const path = pathMap[crm.type] || crm.type;
        window.open(`https://innosmith.pipedrive.com/${path}/${crm.id}`, '_blank');
        onClose();
      } else if (item.kind === 'toggl') {
        const t = item.data;
        const wsId = t.workspace_id || 0;
        if (t.type === 'client') {
          window.open(`https://track.toggl.com/${wsId}/clients`, '_blank');
        } else {
          window.open(`https://track.toggl.com/${wsId}/projects/${t.id}/team`, '_blank');
        }
        onClose();
      } else if (item.kind === 'bexio') {
        const b = item.data;
        if (b.type === 'contact') {
          window.open(`https://office.bexio.com/index.php/kontakt/show/id/${b.id}`, '_blank');
        }
        onClose();
      } else if (item.kind === 'signa') {
        window.location.href = `/signale`;
        onClose();
      }
    },
    [onTaskClick, onProjectClick, onClose],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex((prev) => (prev + 1) % Math.max(navTargets.length, 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex((prev) => (prev - 1 + navTargets.length) % Math.max(navTargets.length, 1));
        return;
      }
      if (e.key === 'Enter') {
        // Enter aktiviert das markierte Ziel: Treffer öffnen ODER Serien-Cluster
        // auf-/zuklappen. Die Suche läuft ohnehin automatisch beim Tippen.
        const target = navTargets[activeIndex];
        if (target) {
          e.preventDefault();
          if (target.kind === 'clusterHeader') toggleSeries(target.key);
          else activateItem(target.item);
        }
      }
    },
    [navTargets, activeIndex, activateItem, toggleSeries, onClose],
  );

  useEffect(() => {
    const el = listRef.current?.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  if (!isOpen) return null;

  let runningIndex = -1;

  // Einzel-Doc-Zeile (fusioniertes Dokument / E-Mail). Wird sowohl für Einzeltreffer
  // als auch für aufgeklappte Cluster-Mitglieder (``indented``) verwendet.
  const renderDocRow = (h: DocHit, idx: number, indented: boolean, dimmed = false) => {
    const isActive = idx === activeIndex;
    const isEmail = h.source_type === 'email';
    const typeLabel = fileTypeLabel(h.mime_type, h.title, h.source_type);
    return (
      <button
        key={`doc-${h.source_type}-${h.id}`}
        data-active={isActive}
        onClick={() => activateItem({ kind: 'doc', data: h })}
        onMouseEnter={() => setActiveIndex(idx)}
        className={`flex min-h-11 w-full items-start gap-3 py-2.5 text-left transition-colors ${
          indented ? 'pl-11 pr-4' : 'px-4'
        } ${dimmed && !isActive ? 'opacity-60' : ''} ${
          isActive
            ? 'bg-indigo-50 dark:bg-indigo-950/40'
            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
        }`}
      >
        <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
          isEmail
            ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
            : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
        }`}>
          {isEmail ? <MailIcon className="h-4 w-4" /> : <FileSearchIcon className="h-4 w-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
            {h.title || '(ohne Titel)'}
          </p>
          {h.snippet && (
            <p className="mt-0.5 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
              {h.snippet}
            </p>
          )}
        </div>
        <span className={`mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
          isEmail
            ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
            : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
        }`}>
          {typeLabel}
        </span>
      </button>
    );
  };

  // CRM-Zeile (Pipedrive). Identisches Markup für die reguläre CRM-Sektion UND den
  // oben angehefteten eindeutigen Kontakt -- Klick öffnet den Datensatz in Pipedrive.
  const renderCrmRow = (crm: CrmHit, idx: number) => {
    const isActive = idx === activeIndex;
    const typeLabels: Record<string, string> = { person: 'Kontakt', deal: 'Deal', organization: 'Organisation' };
    const typeColors: Record<string, string> = {
      person: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
      deal: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
      organization: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300',
    };
    return (
      <button
        key={`${crm.type}-${crm.id}`}
        data-active={isActive}
        onClick={() => activateItem({ kind: 'crm', data: crm })}
        onMouseEnter={() => setActiveIndex(idx)}
        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
          isActive
            ? 'bg-indigo-50 dark:bg-indigo-950/40'
            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
        }`}
      >
        {crm.pic_url ? (
          <img
            src={crm.pic_url}
            alt=""
            className="h-9 w-9 shrink-0 rounded-full object-cover"
          />
        ) : (
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
            <CrmSearchIcon className="h-4 w-4" />
          </span>
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
            {crm.name}
          </p>
          <p className="truncate text-xs text-gray-400 dark:text-gray-500">
            {crm.detail || crm.email || ''}
          </p>
        </div>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${typeColors[crm.type] || 'bg-gray-100 text-gray-600'}`}>
          {typeLabels[crm.type] || crm.type}
        </span>
        <ExternalLinkIcon className="h-3.5 w-3.5 shrink-0 text-gray-300 dark:text-gray-600" />
      </button>
    );
  };

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 px-4 pt-[env(safe-area-inset-top,0px)] pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === backdropRef.current) onClose();
      }}
    >
      <div
        className="mt-2 flex max-h-[min(85dvh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-1rem))] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl sm:mt-[12dvh] dark:border-gray-700 dark:bg-gray-900"
        onKeyDown={handleKeyDown}
      >
        {/* Suchfeld */}
        <div className="flex items-center gap-3 border-b border-gray-200 px-4 py-3 dark:border-gray-700">
          <SearchIcon className="h-5 w-5 shrink-0 text-gray-400 dark:text-gray-500" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder='Suchen…'
            title='Phrase: "Vorname Nachname"'
            className="flex-1 bg-transparent text-sm text-gray-900 outline-none placeholder:text-gray-400 dark:text-white dark:placeholder:text-gray-500"
          />
          {(loading || docLoading) && (
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
          )}
          <kbd className="hidden rounded-md border border-gray-200 px-1.5 py-0.5 text-[10px] font-medium text-gray-400 sm:inline-block dark:border-gray-600 dark:text-gray-500">
            ESC
          </kbd>
        </div>

        {/* Optionale globale Typ-Filter über alle Treffertypen (nur bei >=2 Arten) */}
        {facets.length >= 2 && (
          <div className="flex items-center gap-1.5 overflow-x-auto border-b border-gray-100 px-4 py-2 dark:border-gray-800">
            <FilterIcon className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
            <button
              onClick={() => selectFilter(null)}
              className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
                typeFilter === null
                  ? 'bg-indigo-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
              }`}
            >
              Alle <span className="opacity-60">{baseItems.length}</span>
            </button>
            {facets.map((f) => (
              <button
                key={f.label}
                onClick={() => selectFilter(f.label)}
                className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
                  typeFilter === f.label
                    ? 'bg-indigo-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
                }`}
              >
                {f.label} <span className="opacity-60">{f.count}</span>
              </button>
            ))}
          </div>
        )}

        {/* Ergebnisse */}
        {/* Die Liste füllt die Panel-Höhe; ein fixes 50dvh liess mobil zwei Drittel
            des Dialogs leer, während Treffer abgeschnitten waren. */}
        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto sm:max-h-[60dvh]">
          {!query.trim() && (
            <div className="flex flex-col items-center gap-2 px-4 py-10 text-center text-sm text-gray-400 dark:text-gray-500">
              <span className="hidden items-center gap-1.5 sm:flex">
                <kbd className="rounded-md border border-gray-200 px-1.5 py-0.5 text-[10px] font-medium dark:border-gray-600">
                  /
                </kbd>
                zum Suchen
              </span>
              <span className="sm:hidden">Tippe zum Suchen</span>
            </div>
          )}

          {query.trim().length >= 2 && !loading && !docLoading && grouped && grouped.length === 0 && (
            <div className="px-4 py-10 text-center text-sm text-gray-400 dark:text-gray-500">
              Keine Ergebnisse für &laquo;{query}&raquo;
            </div>
          )}

          {displaySections.map((section) => (
              <div key={section.label}>
                <div className="sticky top-0 z-10 bg-gray-50/90 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500 backdrop-blur-sm dark:bg-gray-800/90 dark:text-gray-400">
                  {section.label}
                </div>
                {section.rows.map((row) => {
                  // Trenner "Möglicherweise verwandt": rein visuell, nicht navigierbar,
                  // erhöht runningIndex NICHT (sonst würde die Tastatur-Navigation zählen).
                  if (row.kind === 'divider') {
                    return (
                      <div
                        key={`divider-${section.label}-${row.label}`}
                        className="flex items-center gap-2 px-4 pb-1 pt-3 text-[11px] font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500"
                      >
                        <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" />
                        {row.label}
                        <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" />
                      </div>
                    );
                  }

                  // Serien-Cluster: aufklappbare Kopfzeile + (bei Expansion) Mitglieder.
                  if (row.kind === 'cluster') {
                    runningIndex++;
                    const headerIdx = runningIndex;
                    const headerActive = headerIdx === activeIndex;
                    const expanded = expandedSeries.has(row.key);
                    const dimmed = row.dimmed && !headerActive;
                    // Repräsentant der Serie: alle Mitglieder teilen den source_type
                    // (Serienschlüssel ist danach präfixt) -> Typ-Icon/Badge wie in
                    // den Einzelzeilen, damit man dem Cluster den Typ direkt ansieht.
                    const rep = row.members[0];
                    const clusterIsEmail = rep.source_type === 'email';
                    const clusterTypeLabel = fileTypeLabel(rep.mime_type, rep.title, rep.source_type);
                    return (
                      <Fragment key={`cluster-${row.key}`}>
                        <button
                          data-active={headerActive}
                          onClick={() => toggleSeries(row.key)}
                          onMouseEnter={() => setActiveIndex(headerIdx)}
                          className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                            dimmed ? 'opacity-60' : ''
                          } ${
                            headerActive
                              ? 'bg-indigo-50 dark:bg-indigo-950/40'
                              : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                          }`}
                        >
                          <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
                            clusterIsEmail
                              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                              : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
                          }`}>
                            {clusterIsEmail ? <MailIcon className="h-4 w-4" /> : <FileSearchIcon className="h-4 w-4" />}
                          </span>
                          <ChevronIcon className={`h-4 w-4 shrink-0 text-gray-400 transition-transform dark:text-gray-500 ${expanded ? 'rotate-90' : ''}`} />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
                              {row.label}
                            </p>
                            <p className="truncate text-xs text-gray-400 dark:text-gray-500">
                              {expanded ? 'Serie zuklappen' : `${row.count} ähnliche Treffer`}
                            </p>
                          </div>
                          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            clusterIsEmail
                              ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                              : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
                          }`}>
                            {clusterTypeLabel}
                          </span>
                          <span className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                            {row.count}
                          </span>
                        </button>
                        {expanded &&
                          row.members.map((m) => {
                            runningIndex++;
                            return renderDocRow(m, runningIndex, true, row.dimmed);
                          })}
                      </Fragment>
                    );
                  }

                  const item = row.item;
                  const rowDimmed = row.dimmed ?? false;
                  runningIndex++;
                  const idx = runningIndex;
                  const isActive = idx === activeIndex;

                  if (item.kind === 'task') {
                    const task = item.data;
                    return (
                      <button
                        key={task.id}
                        data-active={isActive}
                        onClick={() => activateItem(item)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                        }`}
                      >
                        <span
                          className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 ${
                            task.is_completed
                              ? 'border-green-400 bg-green-400 text-white'
                              : 'border-gray-300 dark:border-gray-600'
                          }`}
                        >
                          {task.is_completed && <CheckIcon className="h-3 w-3" />}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p
                            className={`truncate text-sm font-medium ${
                              task.is_completed
                                ? 'text-gray-400 line-through dark:text-gray-600'
                                : 'text-gray-900 dark:text-white'
                            }`}
                          >
                            {task.title}
                          </p>
                          <p className="truncate text-xs text-gray-400 dark:text-gray-500">
                            {task.project_name}
                            {task.due_date && ` · ${formatDate(task.due_date)}`}
                          </p>
                        </div>
                        {task.assignee === 'agent' && (
                          <AgentBadge />
                        )}
                      </button>
                    );
                  }

                  if (item.kind === 'project') {
                    const project = item.data;
                    return (
                      <button
                        key={project.id}
                        data-active={isActive}
                        onClick={() => activateItem(item)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                        }`}
                      >
                        <span
                          className="h-3 w-3 shrink-0 rounded-full"
                          style={{ backgroundColor: project.color }}
                        />
                        <span className="truncate text-sm font-medium text-gray-900 dark:text-white">
                          {project.name}
                        </span>
                        <span className="ml-auto text-xs text-gray-400 dark:text-gray-500">
                          {project.status === 'active' ? 'Aktiv' : project.status}
                        </span>
                      </button>
                    );
                  }

                  // tag
                  if (item.kind === 'tag') {
                    const tag = item.data;
                    return (
                      <div
                        key={tag.id}
                        data-active={isActive}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex items-center gap-3 px-4 py-2.5 transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : ''
                        }`}
                      >
                        <span
                          className="inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium"
                          style={{
                            backgroundColor: tag.color + '20',
                            color: tag.color,
                          }}
                        >
                          {tag.name}
                        </span>
                      </div>
                    );
                  }

                  // crm
                  if (item.kind === 'crm') {
                    return renderCrmRow(item.data, idx);
                  }

                  // toggl
                  if (item.kind === 'toggl') {
                    const t = item.data;
                    const togglTypeLabels: Record<string, string> = { client: 'Kunde', project: 'Projekt' };
                    const togglTypeColors: Record<string, string> = {
                      client: 'bg-pink-100 text-pink-700 dark:bg-pink-900/40 dark:text-pink-300',
                      project: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
                    };
                    return (
                      <button
                        key={`toggl-${t.type}-${t.id}`}
                        data-active={isActive}
                        onClick={() => activateItem(item)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                        }`}
                      >
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-pink-100 text-pink-700 dark:bg-pink-900/40 dark:text-pink-300">
                          <TogglIcon className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
                            {t.name}
                          </p>
                        </div>
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${togglTypeColors[t.type] || 'bg-gray-100 text-gray-600'}`}>
                          {togglTypeLabels[t.type] || t.type}
                        </span>
                        <ExternalLinkIcon className="h-3.5 w-3.5 shrink-0 text-gray-300 dark:text-gray-600" />
                      </button>
                    );
                  }

                  // bexio
                  if (item.kind === 'bexio') {
                    const b = item.data;
                    return (
                      <button
                        key={`bexio-${b.type}-${b.id}`}
                        data-active={isActive}
                        onClick={() => activateItem(item)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                        }`}
                      >
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300">
                          <BexioIcon className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
                            {b.name}
                          </p>
                          {b.email && (
                            <p className="truncate text-xs text-gray-400 dark:text-gray-500">
                              {b.email}
                            </p>
                          )}
                        </div>
                        <span className="shrink-0 rounded-full bg-teal-100 px-2 py-0.5 text-[10px] font-medium text-teal-700 dark:bg-teal-900/40 dark:text-teal-300">
                          Kontakt
                        </span>
                        <ExternalLinkIcon className="h-3.5 w-3.5 shrink-0 text-gray-300 dark:text-gray-600" />
                      </button>
                    );
                  }

                  // signa
                  if (item.kind === 'signa') {
                    const s = item.data;
                    const signaTypeColors: Record<string, string> = {
                      rss: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
                      youtube: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
                      web: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
                    };
                    return (
                      <button
                        key={`signa-${s.id}`}
                        data-active={isActive}
                        onClick={() => activateItem(item)}
                        onMouseEnter={() => setActiveIndex(idx)}
                        className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                          isActive
                            ? 'bg-indigo-50 dark:bg-indigo-950/40'
                            : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                        }`}
                      >
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                          <SignaSearchIcon className="h-4 w-4" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-gray-900 dark:text-white">
                            {s.title}
                          </p>
                          {s.source && (
                            <p className="truncate text-xs text-gray-400 dark:text-gray-500">
                              {s.source}
                            </p>
                          )}
                        </div>
                        {s.score != null && (
                          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold ${
                            s.score >= 8
                              ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
                              : s.score >= 6
                                ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                                : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                          }`}>
                            {s.score.toFixed(1)}
                          </span>
                        )}
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${signaTypeColors[s.type] || 'bg-gray-100 text-gray-600'}`}>
                          {s.type}
                        </span>
                      </button>
                    );
                  }

                  // doc (fusioniertes Dokument / E-Mail mit Snippet-Passage)
                  if (item.kind === 'doc') {
                    return renderDocRow(item.data, idx, false, rowDimmed);
                  }

                  return null;
                })}
              </div>
            ))}
        </div>

        {/* Footer-Hinweise (nur Desktop) */}
        {navTargets.length > 0 && (
          <div className="hidden items-center gap-4 border-t border-gray-200 px-4 py-2 text-[11px] text-gray-400 sm:flex dark:border-gray-700 dark:text-gray-500">
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-gray-200 px-1 py-0.5 text-[10px] dark:border-gray-600">↑↓</kbd>
              Navigieren
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-gray-200 px-1 py-0.5 text-[10px] dark:border-gray-600">↵</kbd>
              Öffnen
            </span>
            <span className="flex items-center gap-1">
              <kbd className="rounded border border-gray-200 px-1 py-0.5 text-[10px] dark:border-gray-600">ESC</kbd>
              Schliessen
            </span>
            <span className="ml-auto truncate text-gray-400/80 dark:text-gray-500/80">
              Leerzeichen = alle Begriffe · &quot;…&quot; = exakte Phrase · OR = einer von mehreren
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diff = date.getTime() - now.getTime();
  const days = Math.ceil(diff / (1000 * 60 * 60 * 24));

  if (days === 0) return 'Heute';
  if (days === 1) return 'Morgen';
  if (days === -1) return 'Gestern';

  return date.toLocaleDateString('de-DE', { day: 'numeric', month: 'short' });
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
    </svg>
  );
}

function FilterIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 0 1-.659 1.591l-5.432 5.432a2.25 2.25 0 0 0-.659 1.591v2.927a2.25 2.25 0 0 1-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 0 0-.659-1.591L3.659 7.409A2.25 2.25 0 0 1 3 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0 1 12 3Z" />
    </svg>
  );
}

function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={3} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
    </svg>
  );
}

function AgentBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-violet-50 px-2 py-0.5 text-[10px] font-medium text-violet-600 dark:bg-violet-950 dark:text-violet-300">
      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" />
      </svg>
      Agent
    </span>
  );
}

function CrmSearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z" />
    </svg>
  );
}

function ExternalLinkIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
    </svg>
  );
}

function TogglIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function BexioIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z" />
    </svg>
  );
}

function SignaSearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.348 14.652a3.75 3.75 0 0 1 0-5.304m5.304 0a3.75 3.75 0 0 1 0 5.304m-7.425 2.121a6.75 6.75 0 0 1 0-9.546m9.546 0a6.75 6.75 0 0 1 0 9.546M5.106 18.894c-3.808-3.807-3.808-9.98 0-13.788m13.788 0c3.808 3.807 3.808 9.98 0 13.788M12 12h.008v.008H12V12Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" />
    </svg>
  );
}

function ChevronIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
    </svg>
  );
}

function MailIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75" />
    </svg>
  );
}

function FileSearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m.75 12 3 3m0 0 3-3m-3 3v-6m-1.5-9H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
    </svg>
  );
}
