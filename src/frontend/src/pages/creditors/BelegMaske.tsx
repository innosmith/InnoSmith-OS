/**
 * Korrekturmaske für einen einzelnen Kreditorenbeleg.
 *
 * Die Maske schickt **nur geänderte Felder**. Das ist keine Sparsamkeit,
 * sondern die Bedingung dafür, dass die Sperre gegen erneute Extraktion
 * schmal bleibt: jedes mitgeschickte Feld gilt danach als von Hand gesetzt
 * und wird von keinem künftigen Modelllauf mehr angefasst. Wer die ganze
 * Maske sendet, friert den ganzen Beleg ein.
 */
import { useMemo, useState } from 'react';
import { X, Lock, FileText, ExternalLink, Check, Undo2 } from 'lucide-react';
import type { StyleCtx } from './creditors-types';
import { api } from '../../api/client';

/** Was die Schnittstelle entgegennimmt (BelegKorrektur), in der Reihenfolge,
 *  in der ein Mensch eine Rechnung liest. */
type Art = 'text' | 'lang' | 'zahl' | 'ganzzahl' | 'datum' | 'wahl' | 'janein';

interface Feld {
  name: string;
  titel: string;
  art: Art;
  optionen?: string[];
  hinweis?: string;
}

interface Gruppe {
  titel: string;
  felder: Feld[];
}

const DOKUMENTTYPEN = [
  'RECHNUNG', 'GUTSCHRIFT', 'LASTSCHRIFT', 'VORAUSRECHNUNG', 'MAHNUNG',
  'KONTOAUSZUG', 'SPESEN', 'INFORMATION', 'UNBEKANNT',
];
const ZAHLUNGSARTEN = ['RECHNUNG', 'LASTSCHRIFT', 'KREDITKARTE', 'TWINT', 'UNBEKANNT'];
const ZYKLEN = [
  'MONTHLY', 'QUARTERLY', 'SEMI_ANNUALLY', 'YEARLY', 'MULTI_YEAR',
  'ONE_TIME', 'USAGE_BASED', 'UNKNOWN',
];
const WAEHRUNGEN = ['CHF', 'EUR', 'USD', 'GBP'];

const GRUPPEN: Gruppe[] = [
  {
    titel: 'Einstufung',
    felder: [
      { name: 'document_type', titel: 'Dokumenttyp', art: 'wahl', optionen: DOKUMENTTYPEN },
      { name: 'category', titel: 'Kategorie', art: 'text' },
      { name: 'industry', titel: 'Branche', art: 'text' },
    ],
  },
  {
    titel: 'Rechnung',
    felder: [
      { name: 'supplier_name_display', titel: 'Kreditor (Anzeige)', art: 'text' },
      { name: 'supplier_name', titel: 'Kreditor (wie gelesen)', art: 'text' },
      { name: 'invoice_number', titel: 'Rechnungsnummer', art: 'text' },
      { name: 'invoice_date', titel: 'Rechnungsdatum', art: 'datum' },
      { name: 'due_date', titel: 'Fällig am', art: 'datum' },
      { name: 'product_description', titel: 'Produkt / Leistung', art: 'lang' },
    ],
  },
  {
    titel: 'Beträge',
    felder: [
      { name: 'total_amount', titel: 'Gesamtbetrag', art: 'zahl' },
      { name: 'currency', titel: 'Währung', art: 'wahl', optionen: WAEHRUNGEN },
      { name: 'amount_chf', titel: 'Betrag in CHF', art: 'zahl' },
      { name: 'net_amount', titel: 'Netto', art: 'zahl' },
      { name: 'vat_amount', titel: 'MwSt-Betrag', art: 'zahl' },
      { name: 'vat_rate', titel: 'MwSt-Satz (%)', art: 'zahl' },
    ],
  },
  {
    titel: 'Zahlung und Takt',
    felder: [
      { name: 'payment_method', titel: 'Zahlungsart', art: 'wahl', optionen: ZAHLUNGSARTEN },
      { name: 'billing_cycle', titel: 'Abrechnungszyklus', art: 'wahl', optionen: ZYKLEN },
      {
        name: 'annual_cost_chf',
        titel: 'Jahreskosten CHF',
        art: 'zahl',
        hinweis: 'Bei Verbrauchsabrechnung bleibt das Feld leer — ein Wert hier wäre erfunden.',
      },
      { name: 'renewal_date', titel: 'Verlängerung am', art: 'datum' },
      { name: 'payment_terms', titel: 'Zahlungsfrist', art: 'text' },
      { name: 'discount_percent', titel: 'Skonto (%)', art: 'zahl' },
      { name: 'discount_days', titel: 'Skonto (Tage)', art: 'ganzzahl' },
    ],
  },
  {
    titel: 'Leistungszeitraum',
    felder: [
      { name: 'service_period_start', titel: 'Beginn', art: 'datum' },
      { name: 'service_period_end', titel: 'Ende', art: 'datum' },
    ],
  },
  {
    titel: 'Lieferant',
    felder: [
      { name: 'supplier_address', titel: 'Adresse', art: 'lang' },
      { name: 'supplier_country', titel: 'Land', art: 'text' },
      { name: 'supplier_uid', titel: 'UID', art: 'text' },
      { name: 'supplier_iban', titel: 'IBAN', art: 'text' },
      { name: 'supplier_bic', titel: 'BIC', art: 'text' },
      { name: 'is_foreign', titel: 'Ausland', art: 'janein' },
    ],
  },
  {
    titel: 'Empfänger',
    felder: [
      { name: 'recipient_company', titel: 'Firma', art: 'text' },
      { name: 'contact_person', titel: 'Kontaktperson', art: 'text' },
      { name: 'license_count', titel: 'Lizenzen', art: 'ganzzahl' },
    ],
  },
];

type Wert = string | number | boolean | null;

/** ISO-Zeitstempel auf das, was ein Datumsfeld anzeigen kann. */
function alsDatum(wert: unknown): string {
  if (typeof wert !== 'string' || !wert) return '';
  return wert.slice(0, 10);
}

function anzeigewert(feld: Feld, roh: unknown): string {
  if (roh == null) return '';
  if (feld.art === 'datum') return alsDatum(roh);
  return String(roh);
}

/** Aus dem Eingabefeld zurück in das, was die Schnittstelle erwartet.
 *  Leer heisst ausdrücklich `null` — «hier steht nichts» ist eine Aussage. */
function alsNutzlast(feld: Feld, eingabe: string): Wert {
  if (eingabe === '') return null;
  if (feld.art === 'zahl') {
    const n = Number(eingabe.replace(',', '.'));
    return Number.isNaN(n) ? eingabe : n;
  }
  if (feld.art === 'ganzzahl') {
    const n = Number.parseInt(eingabe, 10);
    return Number.isNaN(n) ? eingabe : n;
  }
  if (feld.art === 'janein') return eingabe === 'ja';
  return eingabe;
}

function alsFormular(feld: Feld, roh: unknown): string {
  if (feld.art === 'janein') {
    if (roh === true) return 'ja';
    if (roh === false) return 'nein';
    return '';
  }
  return anzeigewert(feld, roh);
}

/** Fehler der Gegenseite den Feldern zuordnen. Bei 422 trägt jeder Eintrag
 *  ein `loc`; alles andere bleibt als allgemeine Meldung stehen. */
function fehlerZuordnen(detail: unknown): { jeFeld: Record<string, string>; allgemein: string } {
  const jeFeld: Record<string, string> = {};
  if (Array.isArray(detail)) {
    for (const e of detail) {
      const eintrag = e as { loc?: unknown[]; msg?: string };
      const feld = eintrag.loc?.filter(t => typeof t === 'string').pop();
      if (typeof feld === 'string' && eintrag.msg) jeFeld[feld] = eintrag.msg;
    }
    if (Object.keys(jeFeld).length > 0) return { jeFeld, allgemein: '' };
  }
  return { jeFeld, allgemein: typeof detail === 'string' ? detail : JSON.stringify(detail) };
}

interface Props {
  belegId: number;
  beleg: Record<string, unknown>;
  styleCtx: StyleCtx;
  onClose: () => void;
  onGespeichert: (neu: Record<string, unknown>) => void;
}

export default function BelegMaske({ belegId, beleg, styleCtx, onClose, onGespeichert }: Props) {
  const { textPrimary, textSecondary, textMuted } = styleCtx;
  const [entwurf, setEntwurf] = useState<Record<string, string>>({});
  const [speichert, setSpeichert] = useState(false);
  const [feldfehler, setFeldfehler] = useState<Record<string, string>>({});
  const [meldung, setMeldung] = useState<{ art: 'ok' | 'fehler'; text: string } | null>(null);

  const gesperrt = useMemo(
    () => new Set((beleg.gesperrt as string[] | undefined) ?? []),
    [beleg.gesperrt],
  );
  const geaendert = Object.keys(entwurf);
  const offen = geaendert.length > 0;

  const setzen = (feld: Feld, eingabe: string) => {
    const ursprung = alsFormular(feld, beleg[feld.name]);
    setEntwurf(vorher => {
      const nachher = { ...vorher };
      if (eingabe === ursprung) delete nachher[feld.name];
      else nachher[feld.name] = eingabe;
      return nachher;
    });
    setFeldfehler(vorher => {
      if (!(feld.name in vorher)) return vorher;
      const nachher = { ...vorher };
      delete nachher[feld.name];
      return nachher;
    });
  };

  const verwerfen = () => {
    setEntwurf({});
    setFeldfehler({});
    setMeldung(null);
  };

  const speichern = async () => {
    const alleFelder = GRUPPEN.flatMap(g => g.felder);
    const nutzlast: Record<string, Wert> = {};
    for (const [name, eingabe] of Object.entries(entwurf)) {
      const feld = alleFelder.find(f => f.name === name);
      if (feld) nutzlast[name] = alsNutzlast(feld, eingabe);
    }
    setSpeichert(true);
    setFeldfehler({});
    setMeldung(null);
    try {
      const ergebnis = await api.patch<{ geaendert: string[]; gesperrt: string[] }>(
        `/api/creditors/beleg/${belegId}`,
        nutzlast,
      );
      const frisch = await api.get<Record<string, unknown>>(`/api/creditors/beleg/${belegId}`);
      onGespeichert(frisch);
      setEntwurf({});
      setMeldung({
        art: 'ok',
        text: ergebnis.geaendert.length
          ? `${ergebnis.geaendert.length} Feld${ergebnis.geaendert.length === 1 ? '' : 'er'} gespeichert`
          : 'Nichts geändert — Beleg ist als geprüft vermerkt',
      });
    } catch (e) {
      const roh = e as { detail?: unknown; message?: string };
      const { jeFeld, allgemein } = fehlerZuordnen(roh.detail ?? roh.message ?? 'Unbekannter Fehler');
      setFeldfehler(jeFeld);
      setMeldung({
        art: 'fehler',
        text: allgemein || 'Bitte die markierten Felder prüfen.',
      });
    }
    setSpeichert(false);
  };

  const kreditor = (beleg.supplier_name_display ?? beleg.supplier_name ?? '–') as string;
  const dateiname = (beleg.filename ?? '') as string;
  const pruefer = beleg.verified_by as string | null;

  return (
    <div className="flex max-h-[inherit] flex-col">
      {/* Kopf */}
      <div className="flex items-start justify-between gap-3 border-b border-gray-200 px-4 py-4 sm:px-6 dark:border-gray-800">
        <div className="min-w-0">
          <h2 className={`truncate text-lg font-bold sm:text-xl ${textPrimary}`}>{kreditor}</h2>
          <p className={`mt-0.5 truncate text-xs ${textMuted}`}>
            Beleg {belegId}
            {dateiname ? ` · ${dateiname}` : ''}
          </p>
          {pruefer && (
            <p className={`mt-1 flex items-center gap-1 text-xs ${textMuted}`}>
              <Lock className="h-3 w-3" />
              Zuletzt geprüft von {pruefer}
            </p>
          )}
        </div>
        <button
          onClick={onClose}
          aria-label="Schliessen"
          className={`shrink-0 rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 ${textMuted}`}
        >
          <X className="h-5 w-5" />
        </button>
      </div>

      {/* Felder */}
      <div className="flex-1 overflow-auto px-4 py-4 sm:px-6">
        <div className="space-y-6">
          {GRUPPEN.map(gruppe => (
            <section key={gruppe.titel}>
              <h3 className={`mb-2 text-[11px] font-semibold uppercase tracking-wide ${textMuted}`}>
                {gruppe.titel}
              </h3>
              <div className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
                {gruppe.felder.map(feld => {
                  const ursprung = alsFormular(feld, beleg[feld.name]);
                  const wert = entwurf[feld.name] ?? ursprung;
                  const istGeaendert = feld.name in entwurf;
                  const fehler = feldfehler[feld.name];
                  const rahmen = fehler
                    ? 'border-red-400 dark:border-red-500'
                    : istGeaendert
                      ? 'border-amber-400 dark:border-amber-500'
                      : 'border-gray-300 dark:border-gray-700';
                  const spanne = feld.art === 'lang' ? 'sm:col-span-2 xl:col-span-3' : '';

                  return (
                    <div key={feld.name} className={spanne}>
                      <label
                        htmlFor={`feld-${feld.name}`}
                        className={`mb-1 flex items-center gap-1.5 text-[11px] uppercase tracking-wide ${textMuted}`}
                      >
                        {feld.titel}
                        {gesperrt.has(feld.name) && (
                          <span title="Von Hand gesetzt — bleibt bei erneuter Extraktion stehen">
                            <Lock className="h-3 w-3 text-amber-600 dark:text-amber-400" />
                          </span>
                        )}
                      </label>

                      {feld.art === 'lang' ? (
                        <textarea
                          id={`feld-${feld.name}`}
                          rows={3}
                          value={wert}
                          onChange={e => setzen(feld, e.target.value)}
                          className={`w-full rounded-lg border ${rahmen} bg-white px-3 py-2 text-sm ${textPrimary} focus:border-indigo-500 focus:outline-none dark:bg-gray-950`}
                        />
                      ) : feld.art === 'wahl' || feld.art === 'janein' ? (
                        <select
                          id={`feld-${feld.name}`}
                          value={wert}
                          onChange={e => setzen(feld, e.target.value)}
                          className={`w-full rounded-lg border ${rahmen} bg-white px-3 py-2 text-sm ${textPrimary} focus:border-indigo-500 focus:outline-none dark:bg-gray-950`}
                        >
                          <option value="">— leer —</option>
                          {(feld.art === 'janein' ? ['ja', 'nein'] : (feld.optionen ?? [])).map(o => (
                            <option key={o} value={o}>{o}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          id={`feld-${feld.name}`}
                          type={feld.art === 'datum' ? 'date' : feld.art === 'zahl' || feld.art === 'ganzzahl' ? 'number' : 'text'}
                          step={feld.art === 'zahl' ? '0.01' : undefined}
                          inputMode={feld.art === 'zahl' || feld.art === 'ganzzahl' ? 'decimal' : undefined}
                          value={wert}
                          onChange={e => setzen(feld, e.target.value)}
                          className={`w-full rounded-lg border ${rahmen} bg-white px-3 py-2 text-sm ${textPrimary} focus:border-indigo-500 focus:outline-none dark:bg-gray-950`}
                        />
                      )}

                      {fehler && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{fehler}</p>}
                      {!fehler && feld.hinweis && (
                        <p className={`mt-1 text-[11px] ${textMuted}`}>{feld.hinweis}</p>
                      )}
                      {!fehler && istGeaendert && (
                        <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
                          vorher: {ursprung || '— leer —'}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          ))}

          {/* Was die Maschine gelesen hat — nicht korrigierbar, weil es den
              Lauf beschreibt und nicht die Rechnung. */}
          <section>
            <h3 className={`mb-2 text-[11px] font-semibold uppercase tracking-wide ${textMuted}`}>
              Herkunft
            </h3>
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
              {([
                ['Konfidenz', beleg.confidence_score != null ? `${beleg.confidence_score}%` : '–'],
                ['Modell', (beleg.extraction_model as string) || '–'],
                ['Vision', beleg.vision_used ? 'ja' : 'nein'],
                ['Status', (beleg.status as string) || '–'],
              ] as [string, string][]).map(([titel, wert]) => (
                <div key={titel}>
                  <p className={`text-[10px] ${textMuted}`}>{titel}</p>
                  <p className={`text-xs ${textSecondary}`}>{wert}</p>
                </div>
              ))}
            </div>
            {typeof beleg.file_path === 'string' && beleg.file_path && (
              <div className={`mt-3 flex items-center gap-2 text-xs ${textMuted}`}>
                <FileText className="h-3.5 w-3.5 shrink-0" />
                <span className="break-all">{beleg.file_path}</span>
                <a
                  href="http://invoice.innosmith.ai"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="ml-auto flex shrink-0 items-center gap-1 text-indigo-600 hover:underline dark:text-indigo-400"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  InvoiceInsight
                </a>
              </div>
            )}
          </section>
        </div>
      </div>

      {/* Speicherleiste — erscheint nur, wenn es etwas zu speichern gibt.
          Eine dauerhaft sichtbare Leiste lädt zum Klicken ohne Änderung ein,
          und jeder solche Klick setzt einen Prüfvermerk, den niemand meinte. */}
      {(offen || meldung) && (
        <div className="sticky bottom-0 flex flex-wrap items-center gap-3 border-t border-gray-200 bg-white/95 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur sm:px-6 dark:border-gray-800 dark:bg-gray-900/95">
          {meldung && (
            <p
              className={`text-xs ${meldung.art === 'ok' ? 'text-green-700 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}
            >
              {meldung.text}
            </p>
          )}
          {offen && (
            <span className={`text-xs ${textSecondary}`}>
              {geaendert.length} Feld{geaendert.length === 1 ? '' : 'er'} geändert
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            {offen && (
              <button
                onClick={verwerfen}
                disabled={speichert}
                className={`flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium hover:bg-gray-100 disabled:opacity-50 dark:hover:bg-gray-800 ${textSecondary}`}
              >
                <Undo2 className="h-3.5 w-3.5" />
                Verwerfen
              </button>
            )}
            {offen && (
              <button
                onClick={speichern}
                disabled={speichert}
                className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-60"
              >
                {speichert ? (
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
                ) : (
                  <Check className="h-3.5 w-3.5" />
                )}
                Korrektur speichern
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
