/**
 * Der Monatssammelbeleg: die Nutzungsrechnungen eines Lieferanten, als eine Buchung.
 *
 * Cursor schickt bis zu sieben Rechnungen am Tag. Sie werden nicht einzeln
 * freigegeben, sondern je Kalendermonat: eine Buchung in USD zum
 * BAZG-Monatsmittel, datiert auf den Monatsletzten, mit dem Beleg angehängt --
 * einer Übersicht und der ersten Seite jeder Rechnung. Die Einzelrechnungen
 * kommen dabei ins Archiv.
 *
 * Die Vorschau fragt Bexio, das BAZG und das Modul. Darum lädt sie erst, wenn
 * ein Monat aufgeklappt wird, und nicht für jede Zeile beim Öffnen der Seite.
 */
import { useEffect, useState } from 'react';
import { AlertTriangle, BookCheck, CheckCircle2, ChevronDown, ChevronRight, FileText, Layers } from 'lucide-react';
import { api } from '../../api/client';
import type { StyleCtx } from './creditors-types';
import { geld } from './CreditorsEingang';
import { PruefungAnzeige, type Buchungsbericht } from './Nachholen';
import { useBelegUrl } from './belegDatei';

export interface SammelMonat {
  lieferant_schluessel: string;
  anzeigename: string;
  jahr: number;
  monat: number;
  /** «September 2026» */
  bezeichnung: string;
  anzahl: number;
  betrag?: number | null;
  waehrung?: string | null;
  /** Nach dem Monatsletzten -- erst dann steht der BAZG-Kurs fest. */
  abgeschlossen: boolean;
}

interface Sammelvorschau {
  bezeichnung: string;
  nachtrag: boolean;
  positionen: {
    beleg_id: string;
    dateiname: string;
    nummer?: string | null;
    datum?: string | null;
    betrag?: number | null;
  }[];
  waehrung?: string | null;
  betrag?: number | null;
  bezugsteuer?: number | null;
  kurs?: number | null;
  betrag_chf?: number | null;
  bezugsteuer_chf?: number | null;
  buchbar: boolean;
  /** Nichts verstösst; ohne Vollständigkeitsbeleg fehlt nur noch die Bestätigung. */
  bereit: boolean;
  /** Lückenlose Nummern, und die nächste Rechnung ist aus dem Folgemonat bekannt. */
  vollstaendig: boolean;
  vollstaendig_grund: string;
  verstoesse: string[];
  hinweise: string[];
  luecken: string[];
  datum?: string | null;
  sollkonto?: string | null;
  habenkonto?: string | null;
  steuercode?: string | null;
  buchungstext?: string | null;
  referenz?: string | null;
  ablageziel?: string | null;
}

const pfadVon = (m: SammelMonat) => `/api/creditors/sammelbeleg/${m.lieferant_schluessel}/${m.jahr}/${m.monat}`;

interface Props {
  monate: SammelMonat[];
  styleCtx: StyleCtx;
  onGebucht: (titel: string, r: Buchungsbericht) => void;
  onGeaendert: () => Promise<void> | void;
}

export default function Sammelbelege({ monate, styleCtx, onGebucht, onGeaendert }: Props) {
  const { cardClass, textPrimary, textSecondary, textMuted } = styleCtx;
  const [offen, setOffen] = useState<string | null>(null);

  return (
    <div className={`${cardClass} overflow-hidden`}>
      <div className={`flex items-center gap-2 border-b border-gray-100 px-4 py-3 text-sm font-semibold dark:border-gray-800 ${textPrimary}`}>
        <Layers className="h-4 w-4 shrink-0" />
        Sammelbelege
        <span className={`text-xs font-normal ${textMuted}`}>
          — je Monat eine Buchung; die Rechnungen werden nicht einzeln freigegeben
        </span>
      </div>
      <ul className="divide-y divide-gray-100 dark:divide-gray-800">
        {monate.map(m => {
          const schluessel = pfadVon(m);
          const aufgeklappt = offen === schluessel;
          return (
            <li key={schluessel}>
              <button
                onClick={() => setOffen(aufgeklappt ? null : schluessel)}
                className="flex min-h-11 w-full items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50"
              >
                {aufgeklappt
                  ? <ChevronDown className={`h-4 w-4 shrink-0 ${textMuted}`} />
                  : <ChevronRight className={`h-4 w-4 shrink-0 ${textMuted}`} />}
                <span className="min-w-0 flex-1">
                  <span className={`block truncate text-sm font-medium ${textPrimary}`}>
                    {m.anzeigename} · {m.bezeichnung}
                  </span>
                  <span className={`block text-[11px] ${textMuted}`}>
                    {m.anzahl} Rechnung{m.anzahl === 1 ? '' : 'en'} ·{' '}
                    {m.abgeschlossen ? 'Monat abgeschlossen — bereit zur Prüfung' : 'Monat läuft noch'}
                  </span>
                </span>
                <span className={`shrink-0 text-xs tabular-nums ${textSecondary}`}>
                  {geld(m.betrag, m.waehrung)}
                </span>
              </button>
              {aufgeklappt && (
                <Monat m={m} styleCtx={styleCtx} onGebucht={onGebucht} onGeaendert={onGeaendert} />
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Monat({ m, styleCtx, onGebucht, onGeaendert }: {
  m: SammelMonat;
  styleCtx: StyleCtx;
  onGebucht: Props['onGebucht'];
  onGeaendert: Props['onGeaendert'];
}) {
  const { textPrimary, textSecondary, textMuted } = styleCtx;
  const [v, setV] = useState<Sammelvorschau | null>(null);
  const [fehler, setFehler] = useState<string | null>(null);
  const [laeuft, setLaeuft] = useState(false);
  const [zeigen, setZeigen] = useState(false);
  const [bestaetigt, setBestaetigt] = useState(false);
  /** Hochgezählt, wenn die Vorschau neu gelesen werden soll -- nach einer abgewiesenen Freigabe. */
  const [runde, setRunde] = useState(0);
  const pfad = pfadVon(m);
  const pdf = useBelegUrl(zeigen ? `${pfad}/pdf` : null);

  useEffect(() => {
    let gueltig = true;
    api.get<Sammelvorschau>(pfad)
      .then(antwort => { if (gueltig) { setV(antwort); setFehler(null); } })
      .catch((e: Error) => { if (gueltig) setFehler(e.message); });
    return () => { gueltig = false; };
  }, [pfad, runde]);

  const freigeben = async () => {
    if (!v) return;
    // Die Rückfrage nennt, was geschieht -- nicht «Sind Sie sicher?».
    const ok = window.confirm(
      `Sammelbeleg freigeben, in Bexio buchen und ablegen?\n\n${v.buchungstext}\n` +
      `${v.datum} · Soll ${v.sollkonto} / Haben ${v.habenkonto} · ${v.steuercode || 'ohne Steuercode'}\n` +
      `${geld(v.betrag, v.waehrung)} zu ${v.kurs} = ${geld(v.betrag_chf, 'CHF')}\n` +
      `Bezugsteuer ${geld(v.bezugsteuer_chf, 'CHF')}\n\n` +
      `Der Beleg kommt nach\n${v.ablageziel}\n` +
      `und die ${v.positionen.length} Rechnungen in den Jahresordner.`,
    );
    if (!ok) return;
    setLaeuft(true);
    setFehler(null);
    try {
      const r = await api.post<Buchungsbericht>(`${pfadVon(m)}/freigeben`, {
        vollstaendig_bestaetigt: !v.vollstaendig && bestaetigt,
      });
      onGebucht(v.buchungstext || `${m.anzeigename} ${m.bezeichnung}`, r);
      await onGeaendert();
    } catch (e) {
      setFehler((e as Error).message);
      setRunde(n => n + 1);
    }
    setLaeuft(false);
  };

  const zeileKlasse = `text-[11px] uppercase tracking-wide ${textMuted}`;
  const w = v?.waehrung || m.waehrung;
  const freigebbar = !!v && v.bereit && (v.vollstaendig || bestaetigt);
  return (
    <div className="space-y-3 px-4 pb-4">
      {fehler && (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300">
          {fehler}
        </p>
      )}
      {!v && !fehler && <p className={`text-xs ${textMuted}`}>Prüfe gegen Bexio, BAZG und das Archiv…</p>}

      {v && (
        <>
          {v.nachtrag && (
            <p className={`text-xs font-medium ${textPrimary}`}>
              Nachtrag — der {v.bezeichnung} ist schon gebucht; hier steht, was seither kam.
            </p>
          )}
          {v.betrag != null && (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-lg bg-gray-50 p-3 text-sm sm:grid-cols-4 dark:bg-gray-800/60">
              <div>
                <dt className={zeileKlasse}>Total</dt>
                <dd className={`tabular-nums ${textPrimary}`}>{geld(v.betrag, w)}</dd>
              </div>
              <div>
                <dt className={zeileKlasse}>Bezugsteuer 8.1 %</dt>
                <dd className={`tabular-nums ${textPrimary}`}>{geld(v.bezugsteuer, w)}</dd>
              </div>
              <div>
                <dt className={zeileKlasse}>Aufwand CHF</dt>
                <dd className={`tabular-nums ${textPrimary}`}>
                  {v.kurs != null ? geld(v.betrag_chf, 'CHF') : 'Kurs erst nach Monatsende'}
                </dd>
              </div>
              <div>
                <dt className={zeileKlasse}>Bezugsteuer CHF</dt>
                <dd className={`tabular-nums ${textPrimary}`}>
                  {v.kurs != null ? geld(v.bezugsteuer_chf, 'CHF') : '–'}
                </dd>
              </div>
            </dl>
          )}

          {m.abgeschlossen && v.positionen.length > 0 && (
            v.vollstaendig ? (
              <p className="flex items-start gap-1.5 text-xs text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Vollständig: {v.vollstaendig_grund}
              </p>
            ) : (
              <div className="space-y-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
                <p className="flex items-start gap-1.5">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  Vollständigkeit nicht belegt: {v.vollstaendig_grund}
                </p>
                {v.bereit && (
                  <label className="flex min-h-11 cursor-pointer items-center gap-2 font-medium lg:min-h-0">
                    <input
                      type="checkbox"
                      checked={bestaetigt}
                      onChange={e => setBestaetigt(e.target.checked)}
                      className="h-4 w-4 shrink-0"
                    />
                    Safari-Download nach Monatsende gelaufen und synchronisiert
                  </label>
                )}
              </div>
            )
          )}

          <PruefungAnzeige
            p={{
              buchbar: v.bereit,
              verstoesse: v.verstoesse,
              hinweise: v.hinweise,
              datum: v.datum,
              sollkonto: v.sollkonto,
              habenkonto: v.habenkonto,
              betrag: v.betrag,
              waehrung: v.waehrung,
              kurs: v.kurs,
              betrag_chf: v.betrag_chf,
              steuercode: v.steuercode,
              buchungstext: v.buchungstext,
              referenz: v.referenz,
              ablageziel: v.ablageziel,
            }}
            styleCtx={styleCtx}
          />

          <details className="text-xs">
            <summary className={`cursor-pointer ${textSecondary}`}>
              {v.positionen.length} Rechnung{v.positionen.length === 1 ? '' : 'en'} im Beleg
            </summary>
            <table className="mt-2 w-full tabular-nums">
              <tbody>
                {v.positionen.map(p => (
                  <tr key={p.beleg_id} className="border-t border-gray-100 dark:border-gray-800">
                    <td className={`py-1 pr-3 ${textMuted}`}>
                      {p.datum ? new Date(p.datum).toLocaleDateString('de-CH') : '–'}
                    </td>
                    <td className={`py-1 pr-3 ${textPrimary}`}>{p.nummer || p.dateiname}</td>
                    <td className={`py-1 text-right ${textPrimary}`}>{geld(p.betrag, w)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>

          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => setZeigen(z => !z)}
              disabled={v.kurs == null || !v.positionen.length}
              title={v.kurs == null ? 'Der Beleg entsteht erst mit dem BAZG-Monatsmittel' : undefined}
              className="flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800 lg:min-h-0"
            >
              <FileText className="h-4 w-4" /> {zeigen ? 'Beleg ausblenden' : 'Beleg ansehen'}
            </button>
            <button
              onClick={freigeben}
              disabled={!freigebbar || laeuft}
              title={
                freigebbar
                  ? 'Bucht den Monat in Bexio und legt Beleg und Rechnungen ab'
                  : v.bereit
                    ? 'Erst bestätigen, dass der Download nach Monatsende gelaufen ist'
                    : 'Ein Verstoss hält die Buchung an — Gründe oben'
              }
              className="flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50 sm:flex-none lg:min-h-0"
            >
              <BookCheck className="h-4 w-4" />
              {laeuft ? 'Bucht und legt ab…' : 'Freigeben und buchen'}
            </button>
          </div>

          {zeigen && (
            pdf.fehler ? (
              <p className="text-xs text-red-700 dark:text-red-300">{pdf.fehler}</p>
            ) : pdf.url ? (
              <iframe src={pdf.url} title={`Sammelbeleg ${v.bezeichnung}`} className="h-[70vh] w-full rounded-lg border-0" />
            ) : (
              <p className={`text-xs ${textMuted}`}>Stelle den Beleg zusammen…</p>
            )
          )}
        </>
      )}
    </div>
  );
}
