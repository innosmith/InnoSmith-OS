/**
 * Kanonische Outlook-Kategorien der Triage.
 *
 * Spiegel von `src/backend/app/services/triage_labels.py` -- die Python-Seite ist die
 * Quelle der Wahrheit, weil dort validiert und geschrieben wird. Der Test
 * `test_triage_labels_frontend_in_sync` liest diese Datei und schlaegt fehl, wenn die
 * beiden Listen auseinanderlaufen. Nicht per Hand ergaenzen, ohne die Python-Seite
 * mitzuziehen: ein Label, das Outlook nicht kennt, wird von Graph stillschweigend
 * ignoriert.
 */
export const TRIAGE_LABELS = [
  'Signale',
  'System',
  'Wichtig',
  'Offerten/Verträge',
  'Networking/Leads',
  'Finanzen',
  'Kalender',
  'Newsletter',
  'Junk',
  'Unklar',
] as const;

export type TriageLabel = (typeof TRIAGE_LABELS)[number];
