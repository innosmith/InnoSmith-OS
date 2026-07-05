// Kurze, menschenlesbare Dateityp-Labels für die Suchergebnisse (Dokument-Sektion).
// Leitet ein Badge-Label aus MIME-Typ, Dateiname und Quelle ab -- MIME hat Vorrang,
// Dateiendung ist der Fallback (die Microsoft Search API liefert nicht immer MIME).

const MIME_LABELS: Record<string, string> = {
  'application/pdf': 'PDF',
  'application/msword': 'Word',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word',
  'application/vnd.ms-excel': 'Excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel',
  'application/vnd.ms-powerpoint': 'PowerPoint',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PowerPoint',
  'text/markdown': 'Markdown',
  'text/csv': 'CSV',
  'text/plain': 'Text',
  'application/json': 'JSON',
  'application/zip': 'Archiv',
  'message/rfc822': 'E-Mail',
};

const EXT_LABELS: Record<string, string> = {
  pdf: 'PDF',
  doc: 'Word',
  docx: 'Word',
  xls: 'Excel',
  xlsx: 'Excel',
  ppt: 'PowerPoint',
  pptx: 'PowerPoint',
  md: 'Markdown',
  markdown: 'Markdown',
  csv: 'CSV',
  txt: 'Text',
  json: 'JSON',
  xml: 'XML',
  yaml: 'YAML',
  yml: 'YAML',
  html: 'HTML',
  zip: 'Archiv',
  rar: 'Archiv',
  '7z': 'Archiv',
  gz: 'Archiv',
};

function extLabel(filename: string | null | undefined): string | null {
  if (!filename) return null;
  const dot = filename.lastIndexOf('.');
  if (dot < 0 || dot === filename.length - 1) return null;
  const ext = filename.slice(dot + 1).toLowerCase();
  return EXT_LABELS[ext] ?? null;
}

/**
 * Liefert ein kurzes Badge-Label für ein Dokument-Suchergebnis.
 *
 * Reihenfolge: Quelle (E-Mail/Transkript) > MIME-Typ > Dateiendung > generischer
 * Fallback («Dokument»).
 */
export function fileTypeLabel(
  mime: string | null | undefined,
  filename: string | null | undefined,
  sourceType?: string | null,
): string {
  if (sourceType === 'email') return 'E-Mail';
  if (sourceType === 'transcript') return 'Transkript';

  const normMime = (mime || '').split(';')[0].trim().toLowerCase();
  if (normMime && MIME_LABELS[normMime]) return MIME_LABELS[normMime];

  const byExt = extLabel(filename);
  if (byExt) return byExt;

  return 'Dokument';
}
