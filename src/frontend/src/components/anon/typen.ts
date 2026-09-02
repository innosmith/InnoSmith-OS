/** Die gemeinsame Sprache der Anonymisierung.
 *
 * Vor dieser Datei gab es drei Auftritte desselben Features -- Modal im Chat,
 * Tor vor der Cloud, Umschalter in der Finanzanalyse -- und jeder hatte eigene
 * Farben, eigene Beschriftungen und eine eigene Vorstellung davon, was ein
 * Fund ist. Für den Nutzer waren das drei Features. Was hier steht, gilt
 * überall, und darum sieht überall dasselbe gleich aus.
 */

/** Ein Fund: was im Text stand und was jetzt dort steht.
 *
 * Die Feldnamen sind die des Backends (`diff` aus `/api/content/anonymize`) und
 * bleiben es. Eine Umbenennung an der Grenze hiesse, zwei Namen für dieselbe
 * Sache zu pflegen.
 */
export interface Fundstelle {
  original: string;
  fake: string;
  entity_type: string;
}

export const ENTITAET_TEXT: Record<string, string> = {
  PERSON: 'Person',
  ORG: 'Organisation',
  LOCATION: 'Ort',
  EMAIL: 'E-Mail',
  PHONE: 'Telefon',
  IBAN: 'IBAN',
  AHV: 'AHV-Nummer',
  UID: 'UID / MWST',
  URL: 'Webadresse',
  TERM: 'Eigener Begriff',
  UNKNOWN: 'Unbekannt',
};

/** Was maskiert wird -- zur Anzeige, nicht zur Auswahl.
 *
 * Bis zum 24.08.2026 konnte man Typen abwählen, und die Voreinstellung liess
 * AHV- und UID-Nummern ganz aus. Wer anonymisiert, gibt den Text anschliessend
 * einem fremden Sprachmodell; eine Wahl, die dabei still schiefgehen kann, ist
 * keine Freiheit, sondern eine Falle. Massgeblich ist
 * `app/services/anon_politik.py`, das hier ist die Beschriftung dazu.
 */
export const ENTITAETEN = ['PERSON', 'ORG', 'LOCATION', 'EMAIL', 'PHONE', 'IBAN', 'AHV', 'UID'];

export const ENTITAET_FARBE: Record<string, string> = {
  PERSON: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  ORG: 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300',
  LOCATION: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  EMAIL: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  PHONE: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  IBAN: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  AHV: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  UID: 'bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-300',
  TERM: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300',
  UNKNOWN: 'bg-gray-100 text-gray-800 dark:bg-gray-900/40 dark:text-gray-300',
};

export function artName(typ: string): string {
  return ENTITAET_TEXT[typ] ?? typ;
}

/** Nimmt einzelne Maskierungen zurück und gibt den Text aus, der wirklich geht.
 *
 * Eine Rücknahme, die nur die Tabelle einfärbt, wäre eine Lüge: Kopiert würde
 * weiterhin der maskierte Text. Darum entsteht hier der sichtbare Text neu, und
 * genau dieser wird kopiert und exportiert.
 */
export function wendeZuruecknahmenAn(
  text: string,
  fundstellen: Fundstelle[],
  zurueckgenommen: Set<string>,
): string {
  if (zurueckgenommen.size === 0) return text;
  let ergebnis = text;
  for (const f of fundstellen) {
    if (!zurueckgenommen.has(f.fake)) continue;
    ergebnis = ergebnis.split(f.fake).join(f.original);
  }
  return ergebnis;
}

/** Prüft, ob eine hochgeladene Datei überhaupt ein Schlüssel ist.
 *
 * Ohne diese Prüfung schickt ein Fehlgriff -- die falsche JSON-Datei -- den
 * Text unverändert zurück, und das Ergebnis sieht aus wie ein sauberer Rückweg.
 */
export function istSchluesseldatei(wert: unknown): wert is { mappings: Record<string, string> } {
  if (typeof wert !== 'object' || wert === null) return false;
  const m = (wert as { mappings?: unknown }).mappings;
  return typeof m === 'object' && m !== null && !Array.isArray(m);
}
