/**
 * Freigegeben, aber nicht fertig: was bei der Freigabe nicht geklappt hat.
 *
 * Im Normalfall bleibt diese Liste leer -- «Freigeben» bucht und legt ab. Hier
 * steht nur, was dabei scheiterte: Bexio nahm die Buchung nicht an, OneDrive
 * war nicht erreichbar, am Ziel lag schon eine Datei. Die Freigabe steht, und
 * nachgeholt wird genau der fehlende Schritt -- nie eine zweite Buchung.
 */
import { useState } from 'react';
import {
  AlertTriangle, Archive, BookCheck, ChevronDown, ChevronRight, ExternalLink, Info, XCircle,
} from 'lucide-react';
import { api } from '../../api/client';
import type { StyleCtx } from './creditors-types';
import { geld, type EingangsBeleg } from './CreditorsEingang';
import { belegOeffnen } from './belegDatei';

export interface Buchungspruefung {
  buchbar: boolean;
  verstoesse: string[];
  hinweise: string[];
  datum?: string | null;
  sollkonto?: string | null;
  habenkonto?: string | null;
  betrag?: number | null;
  waehrung?: string | null;
  kurs?: number | null;
  betrag_chf?: number | null;
  steuercode?: string | null;
  buchungstext?: string | null;
  referenz?: string | null;
  ablageziel?: string | null;
}

export interface Buchungsbericht {
  freigegeben: boolean;
  gebucht: boolean;
  bexio_referenz?: string | null;
  beleg_angehaengt: boolean;
  journal_zeilen: number;
  abgelegt?: string | null;
  meldungen: string[];
}

/** Was nach Bexio und ins Archiv geht, dazu Verstösse und Hinweise. */
export function PruefungAnzeige({ p, styleCtx }: { p: Buchungspruefung; styleCtx: StyleCtx }) {
  const { textPrimary, textSecondary, textMuted } = styleCtx;
  const zeileKlasse = `text-[11px] uppercase tracking-wide ${textMuted}`;
  return (
    <>
      {p.buchbar && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg bg-gray-50 p-3 text-sm sm:grid-cols-2 dark:bg-gray-800/60">
          <div className="sm:col-span-2">
            <dt className={zeileKlasse}>Buchungstext</dt>
            <dd className={`break-words ${textPrimary}`}>{p.buchungstext}</dd>
          </div>
          <div>
            <dt className={zeileKlasse}>Datum · Referenz</dt>
            <dd className={textPrimary}>{p.datum} · {p.referenz || '–'}</dd>
          </div>
          <div>
            <dt className={zeileKlasse}>Soll / Haben · Steuer</dt>
            <dd className={`tabular-nums ${textPrimary}`}>
              {p.sollkonto} / {p.habenkonto} · {p.steuercode || 'ohne Steuercode'}
            </dd>
          </div>
          <div>
            <dt className={zeileKlasse}>Betrag</dt>
            <dd className={`tabular-nums ${textPrimary}`}>{geld(p.betrag, p.waehrung)}</dd>
          </div>
          <div>
            <dt className={zeileKlasse}>BAZG-Monatsmittel · in CHF</dt>
            <dd className={`tabular-nums ${textPrimary}`}>
              {p.waehrung === 'CHF' ? '–' : p.kurs} · {geld(p.betrag_chf, 'CHF')}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className={zeileKlasse}>Ablage</dt>
            <dd className={`break-all ${textPrimary}`}>{p.ablageziel}</dd>
          </div>
        </dl>
      )}
      {p.verstoesse.map(v => (
        <p key={v} className="flex items-start gap-1.5 text-xs text-red-700 dark:text-red-300">
          <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {v}
        </p>
      ))}
      {p.hinweise.map(h => (
        <p key={h} className={`flex items-start gap-1.5 text-xs ${textSecondary}`}>
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" /> {h}
        </p>
      ))}
    </>
  );
}

/** Ein Bericht bleibt stehen, auch wenn der Beleg danach aus der Liste fällt. */
export function BerichtAnzeige({ r }: { r: Buchungsbericht }) {
  // Fertig ist, was im Archiv liegt. Eine Sammelbeleg-Rechnung wird nie
  // einzeln gebucht und ist mit der Ablage trotzdem erledigt.
  const fertig = !!r.abgelegt;
  return (
    <div className="text-sm">
      <p className={`font-medium ${fertig ? 'text-emerald-700 dark:text-emerald-400' : 'text-amber-700 dark:text-amber-300'}`}>
        {r.gebucht
          ? `Gebucht (${r.bexio_referenz})`
          : r.abgelegt ? 'Nur abgelegt' : r.freigegeben ? 'Freigegeben, nicht gebucht' : 'Nicht gebucht'}
        {r.beleg_angehaengt ? ', Beleg angehängt' : ''}
        {r.journal_zeilen ? `, ${r.journal_zeilen} Journalzeilen` : ''}
        {r.abgelegt ? ` — abgelegt unter ${r.abgelegt}` : ''}
      </p>
      {r.meldungen.map(m => (
        <p key={m} className="mt-1 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {m}
        </p>
      ))}
      {r.freigegeben && !fertig && (
        <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
          Der Beleg steht unter «Nachholen».
        </p>
      )}
    </div>
  );
}

interface Props {
  belege: EingangsBeleg[];
  styleCtx: StyleCtx;
  onGeaendert: () => Promise<void> | void;
}

function zustand(b: EingangsBeleg): string {
  if (b.gebucht_am) return 'gebucht, noch nicht abgelegt';
  if (b.nur_ablegen) return 'Sammelbeleg — noch nicht abgelegt';
  return 'freigegeben, nicht gebucht';
}

export default function Nachholen({ belege, styleCtx, onGeaendert }: Props) {
  const { cardClass, textPrimary, textSecondary, textMuted } = styleCtx;
  const [offen, setOffen] = useState<string | null>(null);
  const [pruefung, setPruefung] = useState<Record<string, Buchungspruefung>>({});
  const [laeuft, setLaeuft] = useState<string | null>(null);
  const [fehler, setFehler] = useState<Record<string, string>>({});
  const [bericht, setBericht] = useState<Record<string, Buchungsbericht>>({});

  const fehlerSetzen = (id: string, text: string | null) =>
    setFehler(vorher => {
      const rest = { ...vorher };
      if (text) rest[id] = text; else delete rest[id];
      return rest;
    });

  const pruefen = async (b: EingangsBeleg) => {
    setLaeuft(b.id);
    fehlerSetzen(b.id, null);
    try {
      const p = await api.get<Buchungspruefung>(`/api/creditors/eingang/${b.id}/pruefung`);
      setPruefung(vorher => ({ ...vorher, [b.id]: p }));
    } catch (e) {
      fehlerSetzen(b.id, (e as Error).message);
    }
    setLaeuft(null);
  };

  const umschalten = (b: EingangsBeleg) => {
    const neu = offen === b.id ? null : b.id;
    setOffen(neu);
    if (neu && !b.gebucht_am && !b.nur_ablegen && !pruefung[b.id]) void pruefen(b);
  };

  const buchen = async (b: EingangsBeleg, p: Buchungspruefung) => {
    // Die Rückfrage nennt, was geschieht -- nicht «Sind Sie sicher?».
    const ok = window.confirm(
      `In Bexio buchen?\n\n${p.buchungstext}\n` +
      `${p.datum} · Soll ${p.sollkonto} / Haben ${p.habenkonto}` +
      `${p.steuercode ? ` · ${p.steuercode}` : ''}\n` +
      `${geld(p.betrag, p.waehrung)}` +
      `${p.waehrung !== 'CHF' ? ` zu ${p.kurs} = ${geld(p.betrag_chf, 'CHF')}` : ''}\n\n` +
      `Danach wird die Rechnung abgelegt unter\n${p.ablageziel}`,
    );
    if (!ok) return;
    setLaeuft(b.id);
    fehlerSetzen(b.id, null);
    try {
      const r = await api.post<Buchungsbericht>(`/api/creditors/eingang/${b.id}/buchen`, {});
      setBericht(vorher => ({ ...vorher, [b.id]: r }));
      await onGeaendert();
    } catch (e) {
      fehlerSetzen(b.id, (e as Error).message);
      void pruefen(b);
    }
    setLaeuft(null);
  };

  const ablegen = async (b: EingangsBeleg) => {
    setLaeuft(b.id);
    fehlerSetzen(b.id, null);
    try {
      const r = await api.post<Buchungsbericht>(`/api/creditors/eingang/${b.id}/ablegen`, {});
      setBericht(vorher => ({ ...vorher, [b.id]: r }));
      await onGeaendert();
    } catch (e) {
      fehlerSetzen(b.id, (e as Error).message);
    }
    setLaeuft(null);
  };

  return (
    <div className={`${cardClass} overflow-hidden`}>
      <div className={`flex items-center gap-2 border-b border-gray-100 px-4 py-3 text-sm font-semibold dark:border-gray-800 ${textPrimary}`}>
        <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500" />
        Nachholen: {belege.length} freigegeben, aber nicht fertig
      </div>

      {Object.entries(bericht).map(([id, r]) => (
        <div key={id} className="border-b border-gray-100 px-4 py-3 dark:border-gray-800">
          <BerichtAnzeige r={r} />
        </div>
      ))}

      <ul className="divide-y divide-gray-100 dark:divide-gray-800">
        {belege.map(b => {
          const aufgeklappt = offen === b.id;
          const p = pruefung[b.id];
          const beschaeftigt = laeuft === b.id;
          return (
            <li key={b.id}>
              <button
                onClick={() => umschalten(b)}
                className="flex min-h-11 w-full items-center gap-3 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50"
              >
                {aufgeklappt
                  ? <ChevronDown className={`h-4 w-4 shrink-0 ${textMuted}`} />
                  : <ChevronRight className={`h-4 w-4 shrink-0 ${textMuted}`} />}
                <span className="min-w-0 flex-1">
                  <span className={`block truncate text-sm font-medium ${textPrimary}`}>
                    {b.buchungstext || b.lieferant || b.dateiname}
                  </span>
                  <span className={`block text-[11px] ${textMuted}`}>
                    {b.datum || '–'} · {zustand(b)}
                  </span>
                </span>
                <span className={`shrink-0 text-xs tabular-nums ${textSecondary}`}>
                  {geld(b.betrag, b.waehrung)}
                </span>
              </button>

              {aufgeklappt && (
                <div className="space-y-3 px-4 pb-4">
                  {b.beleg_id != null && (
                    <button
                      onClick={() => {
                        belegOeffnen(`/api/creditors/beleg/${b.beleg_id}/datei`)
                          .catch((e: Error) => fehlerSetzen(b.id, e.message));
                      }}
                      className={`inline-flex items-center gap-1 text-xs underline ${textSecondary}`}
                    >
                      <ExternalLink className="h-3.5 w-3.5" /> Beleg ansehen
                    </button>
                  )}

                  {fehler[b.id] && (
                    <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300">
                      {fehler[b.id]}
                    </p>
                  )}

                  {!b.gebucht_am && !b.nur_ablegen && (
                    <>
                      {beschaeftigt && !p && <p className={`text-xs ${textMuted}`}>Prüfe gegen Bexio und BAZG…</p>}
                      {p && (
                        <>
                          <PruefungAnzeige p={p} styleCtx={styleCtx} />
                          <button
                            onClick={() => buchen(b, p)}
                            disabled={!p.buchbar || beschaeftigt}
                            title={p.buchbar ? undefined : 'Ein Verstoss hält die Buchung an — Gründe oben'}
                            className="flex min-h-11 w-full items-center justify-center gap-1.5 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto lg:min-h-0"
                          >
                            <BookCheck className="h-4 w-4" />
                            {beschaeftigt ? 'Bucht…' : 'Buchung nachholen'}
                          </button>
                        </>
                      )}
                    </>
                  )}

                  {(b.gebucht_am || b.nur_ablegen) && (
                    <>
                      {b.gebucht_am && (
                        <p className={`text-xs ${textSecondary}`}>
                          Gebucht ({b.bexio_referenz}), aber noch nicht im Archiv. Die Rechnung
                          liegt weiter im Eingang.
                        </p>
                      )}
                      {b.nur_ablegen && !b.gebucht_am && (
                        <p className={`text-xs ${textSecondary}`}>
                          Gebucht wird der Sammelbeleg des Monats; diese Rechnung kommt nur ins Archiv
                          {b.dateiname_ziel ? ` als «${b.dateiname_ziel}»` : ''}.
                        </p>
                      )}
                      <button
                        onClick={() => ablegen(b)}
                        disabled={beschaeftigt}
                        className="flex min-h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium hover:bg-gray-50 disabled:opacity-50 sm:w-auto dark:border-gray-700 dark:hover:bg-gray-800 lg:min-h-0"
                      >
                        <Archive className="h-4 w-4" /> {beschaeftigt ? 'Legt ab…' : 'Ablage nachholen'}
                      </button>
                    </>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
