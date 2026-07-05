// Serien-Clustering für die Dokument-/E-Mail-Sektion der Suche.
//
// Zweck: Serien fast identischer Treffer (z. B. Monatsrechnungen "RE-00604 …",
// "RE-00575 …") oder E-Mail-Threads mit gleichem Betreff ("AW: …" / "RE: …")
// verdichten sich zu EINER aufklappbaren Zeile, statt die Liste zu fluten.
//
// Bewusst rein clientseitig und ranking-treu: die Reihenfolge folgt dem
// Score-Ranking (ein Cluster steht an der Position seines besten Mitglieds), es
// wird nichts umsortiert oder versteckt (Cluster sind aufklappbar).

// Minimale Sicht auf einen Dokument-Treffer -- entkoppelt von der DocHit-Definition
// im SearchDialog, damit die Hilfsfunktion pur und unabhängig testbar bleibt.
export interface SeriesDoc {
  source_type: string;
  id: string;
  title: string | null;
}

export type DisplayRow<T extends SeriesDoc> =
  | { kind: 'single'; hit: T }
  | { kind: 'cluster'; key: string; label: string; count: number; members: T[] };

// Antwort-/Weiterleitungs-Präfixe (DE/EN), iterativ am Betreff-Anfang entfernt.
const REPLY_PREFIX = /^\s*(?:AW|RE|WG|FW|FWD|ANTW|VS)\s*:\s*/i;

// Monatstoken (DE Kurz-/Langform), die eine Datei-Serie sonst pro Monat aufspalten.
const MONTH_TOKENS = new Set([
  'jan', 'januar', 'feb', 'februar', 'mär', 'maer', 'mrz', 'märz', 'maerz',
  'apr', 'april', 'mai', 'jun', 'juni', 'jul', 'juli', 'aug', 'august',
  'sep', 'sept', 'september', 'okt', 'oktober', 'nov', 'november', 'dez', 'dezember',
]);

function stripExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(0, dot) : name;
}

function normalizeSubject(subject: string): string {
  let s = subject;
  // Präfixe können sich stapeln ("AW: RE: …") -> iterativ abtragen.
  let prev: string;
  do {
    prev = s;
    s = s.replace(REPLY_PREFIX, '');
  } while (s !== prev);
  return s.trim().toLowerCase();
}

function normalizeFilename(name: string): string {
  const stem = stripExtension(name);
  const tokens = stem
    .toLowerCase()
    .split(/[\s._-]+/)
    .map((t) => t.replace(/\d+/g, '')) // Ziffernfolgen entfernen (Rechnungs-Nr., Jahr)
    .filter((t) => t && !MONTH_TOKENS.has(t));
  return tokens.join(' ').trim();
}

/**
 * Normalisierter Serienschlüssel eines Treffers. Gleicher Schlüssel = gleiche Serie.
 * E-Mails werden über den (präfixbereinigten) Betreff zum Thread zusammengefasst,
 * Dateien über den ziffern-/monatsbereinigten Namensstamm.
 */
export function seriesKey(hit: SeriesDoc): string {
  const title = (hit.title || '').trim();
  if (!title) return `${hit.source_type}:${hit.id}`; // ohne Titel: nicht clusterbar
  if (hit.source_type === 'email') {
    const norm = normalizeSubject(title);
    return norm ? `email:${norm}` : `email:${hit.id}`;
  }
  const norm = normalizeFilename(title);
  return norm ? `${hit.source_type}:${norm}` : `${hit.source_type}:${hit.id}`;
}

/**
 * Verdichtet eine (bereits gerankte) Trefferliste zu Display-Zeilen: Serien mit
 * mindestens ``minSize`` Mitgliedern werden zu einer Cluster-Zeile, alle übrigen
 * bleiben Einzelzeilen. Die Reihenfolge folgt dem ersten Vorkommen je Schlüssel
 * (also dem bestplatzierten Mitglied), die Mitglieder behalten ihre Rangfolge.
 */
export function clusterDocHits<T extends SeriesDoc>(
  hits: T[],
  minSize = 3,
): DisplayRow<T>[] {
  const groups = new Map<string, T[]>();
  const order: string[] = [];
  for (const hit of hits) {
    const key = seriesKey(hit);
    let bucket = groups.get(key);
    if (!bucket) {
      bucket = [];
      groups.set(key, bucket);
      order.push(key);
    }
    bucket.push(hit);
  }

  const rows: DisplayRow<T>[] = [];
  for (const key of order) {
    const members = groups.get(key)!;
    if (members.length >= minSize) {
      rows.push({
        kind: 'cluster',
        key,
        label: members[0].title || '(ohne Titel)',
        count: members.length,
        members,
      });
    } else {
      for (const hit of members) rows.push({ kind: 'single', hit });
    }
  }
  return rows;
}
