import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  Check,
  Copy,
  Download,
  FileText,
  Image as ImageIcon,
  MessageSquarePlus,
  ShieldCheck,
  Upload,
} from 'lucide-react';
import { api, getToken } from '../api/client';
import { legeUebergabeAb } from '../lib/anonUebergabe';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { ExportDialog } from '../components/ExportDialog';
import { Fundstellenliste } from '../components/anon/Fundstellenliste';
import { Maskenvermerk } from '../components/anon/Maskenvermerk';
import { Textvorschau } from '../components/anon/Textvorschau';
import {
  Entitaetenlegende,
  RestbestandWarnung,
  RueckstandWarnung,
} from '../components/anon/Warnungen';
import {
  artName,
  istSchluesseldatei,
  wendeZuruecknahmenAn,
  type Fundstelle,
} from '../components/anon/typen';

interface Anonymisiert {
  session_id: string;
  anonymized_text: string;
  diff: Fundstelle[];
  restbestaende: string[];
}

interface Rueckabgebildet {
  original_text: string;
  rueckstaende: string[];
}

type Richtung = 'hin' | 'zurueck';
type Quelle = 'text' | 'dokument';

export default function DatenschutzPage() {
  const navigate = useNavigate();

  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);

  const [richtung, setRichtung] = useState<Richtung>('hin');
  const [quelle, setQuelle] = useState<Quelle>('text');
  const [eingabe, setEingabe] = useState('');
  const [begriffe, setBegriffe] = useState('');
  const [datei, setDatei] = useState<File | null>(null);
  const [ziehtDruber, setZiehtDruber] = useState(false);
  const [ergebnis, setErgebnis] = useState<Anonymisiert | null>(null);
  const [zurueckgenommen, setZurueckgenommen] = useState<Set<string>>(new Set());
  const [laeuft, setLaeuft] = useState(false);
  const [fehler, setFehler] = useState('');
  const [kopiert, setKopiert] = useState(false);
  const [exportOffen, setExportOffen] = useState(false);

  const [antwort, setAntwort] = useState('');
  const [schluessel, setSchluessel] = useState<Record<string, unknown> | null>(null);
  const [schluesselName, setSchluesselName] = useState('');
  const [zurueck, setZurueck] = useState<Rueckabgebildet | null>(null);
  const [rueckLaeuft, setRueckLaeuft] = useState(false);
  const [rueckFehler, setRueckFehler] = useState('');
  const [rueckKopiert, setRueckKopiert] = useState(false);

  const dateiFeld = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .get<Record<string, string | null>>('/api/settings')
      .then(s => setBgUrl(s.datenschutz_background_url ?? null))
      .catch(() => {});
  }, []);

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { datenschutz_background_url: url });
    setBgUrl(url);
  };

  const bereit = quelle === 'text' ? eingabe.trim().length > 0 : datei !== null;

  const sichtbarerText = ergebnis
    ? wendeZuruecknahmenAn(ergebnis.anonymized_text, ergebnis.diff, zurueckgenommen)
    : '';
  const aktiveMarken = (ergebnis?.diff ?? [])
    .filter(f => !zurueckgenommen.has(f.fake))
    .map(f => f.fake);

  const titelFuerErsatz = (nadel: string) => {
    const fund = ergebnis?.diff.find(f => f.fake === nadel);
    if (!fund) return nadel;
    return `${artName(fund.entity_type)}: war «${fund.original}»`;
  };

  const anonymisieren = useCallback(async () => {
    setFehler('');
    setLaeuft(true);
    try {
      const eigene = begriffe
        .split('\n')
        .map(z => z.trim())
        .filter(Boolean);

      let daten: Anonymisiert;
      if (quelle === 'dokument' && datei) {
        const form = new FormData();
        form.append('file', datei);
        form.append('begriffe', begriffe);
        daten = await api.upload<Anonymisiert>('/api/content/anonymize/file', form);
      } else {
        daten = await api.post<Anonymisiert>('/api/content/anonymize', {
          text: eingabe,
          begriffe: eigene,
        });
      }
      setErgebnis(daten);
      setZurueckgenommen(new Set());
      setZurueck(null);
    } catch (e) {
      setFehler((e as Error).message || 'Anonymisierung fehlgeschlagen');
    } finally {
      setLaeuft(false);
    }
  }, [begriffe, datei, eingabe, quelle]);

  const umschalten = (fake: string) => {
    setZurueckgenommen(alt => {
      const neu = new Set(alt);
      if (neu.has(fake)) neu.delete(fake);
      else neu.add(fake);
      return neu;
    });
  };

  const kopieren = async (text: string, setzen: (v: boolean) => void) => {
    await navigator.clipboard.writeText(text);
    setzen(true);
    window.setTimeout(() => setzen(false), 2000);
  };

  const schluesselSichern = async () => {
    if (!ergebnis) return;
    try {
      const token = getToken();
      const resp = await fetch(`/api/content/mapping-keys/${ergebnis.session_id}/download`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) throw new Error('Schlüssel nicht verfügbar');
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `schluessel-${ergebnis.session_id.slice(0, 8)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setFehler((e as Error).message);
    }
  };

  const imChatWeiterverwenden = () => {
    if (!ergebnis) return;
    legeUebergabeAb({ text: sichtbarerText, sessionId: ergebnis.session_id });
    navigate('/agenten/chat');
  };

  const schluesselLaden = async (gewaehlt: File | null) => {
    setRueckFehler('');
    if (!gewaehlt) {
      setSchluessel(null);
      setSchluesselName('');
      return;
    }
    try {
      const gelesen = JSON.parse(await gewaehlt.text()) as unknown;
      if (!istSchluesseldatei(gelesen)) {
        setRueckFehler('Die Datei ist kein Schlüssel: Feld «mappings» fehlt.');
        setSchluessel(null);
        setSchluesselName('');
        return;
      }
      setSchluessel(gelesen as Record<string, unknown>);
      setSchluesselName(gewaehlt.name);
    } catch {
      setRueckFehler('Die Datei ist kein gültiges JSON.');
      setSchluessel(null);
      setSchluesselName('');
    }
  };

  const zurueckbilden = async () => {
    setRueckFehler('');
    setRueckLaeuft(true);
    try {
      const koerper = schluessel
        ? { text: antwort, keys: schluessel }
        : { text: antwort, session_id: ergebnis?.session_id };
      setZurueck(await api.post<Rueckabgebildet>('/api/content/deanonymize', koerper));
    } catch (e) {
      setRueckFehler((e as Error).message || 'Rückübersetzung fehlgeschlagen');
    } finally {
      setRueckLaeuft(false);
    }
  };

  const rueckBereit = antwort.trim().length > 0 && (Boolean(ergebnis) || schluessel !== null);

  const seitenStil = bgUrl
    ? bgUrl.startsWith('gradient:')
      ? { background: bgUrl.slice('gradient:'.length) }
      : { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
    : undefined;

  return (
    <div className="h-full overflow-y-auto" style={seitenStil} data-testid="datenschutz-page">
      <div className="mx-auto max-w-[1600px] px-4 py-6 pb-16 sm:px-6">
        {/* Kopf */}
        <header className="mb-6 flex items-start gap-4 rounded-2xl border border-gray-200/70 bg-white/80 p-5 backdrop-blur-sm dark:border-gray-700/70 dark:bg-gray-900/70">
          <div className="inline-flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-400">
            <ShieldCheck className="h-6 w-6" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-2">
              <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Datenschutz</h1>
              <button
                onClick={() => setBgPickerOpen(true)}
                className="shrink-0 rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
                title="Hintergrund ändern"
              >
                <ImageIcon className="h-5 w-5" />
              </button>
            </div>
            <p className="mt-1 max-w-2xl text-sm leading-relaxed text-gray-600 dark:text-gray-300">
              Hinweg: Namen, Firmen, Orte, Kontaktdaten und Kennungen ersetzen, bevor ein Modell den
              Text sieht. Rückweg: die Antwort zurückübersetzen und prüfen, dass kein erfundener
              Name stehen bleibt.
            </p>
            <div className="mt-3">
              <Entitaetenlegende />
            </div>
          </div>
        </header>

        {/* Zwei Wege, gleichberechtigt */}
        <div
          role="tablist"
          className="mb-5 inline-flex rounded-xl border border-gray-200 bg-white/80 p-1 backdrop-blur-sm dark:border-gray-700 dark:bg-gray-900/70"
        >
          <Umschalter
            aktiv={richtung === 'hin'}
            onKlick={() => setRichtung('hin')}
            beschriftung="Anonymisieren"
          />
          <Umschalter
            aktiv={richtung === 'zurueck'}
            onKlick={() => setRichtung('zurueck')}
            beschriftung="Zurückübersetzen"
          />
        </div>

        {richtung === 'hin' ? (
          <div className="space-y-5">
            <Karte titel="1. Text oder Dokument">
              <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                PDF, Word, Text und Markdown. Ein Scan ohne Textebene enthält keinen lesbaren Text —
                dann den Text von Hand einfügen.
              </p>

              {fehler && (
                <div
                  role="alert"
                  className="mb-4 rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-300"
                >
                  {fehler}
                </div>
              )}

              <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_24rem]">
                <div className="min-w-0">
                  <div
                    role="tablist"
                    className="mb-4 inline-flex rounded-lg border border-gray-200 p-1 dark:border-gray-600"
                  >
                    <Umschalter
                      aktiv={quelle === 'text'}
                      onKlick={() => setQuelle('text')}
                      beschriftung="Text einfügen"
                      klein
                    />
                    <Umschalter
                      aktiv={quelle === 'dokument'}
                      onKlick={() => setQuelle('dokument')}
                      beschriftung="Dokument ablegen"
                      klein
                    />
                  </div>

                  {quelle === 'text' ? (
                    <textarea
                      value={eingabe}
                      onChange={e => setEingabe(e.target.value)}
                      rows={14}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                      placeholder="Mandantenschreiben, Aktennotiz, Offerte …"
                    />
                  ) : (
                    <div
                      onDragOver={e => {
                        e.preventDefault();
                        setZiehtDruber(true);
                      }}
                      onDragLeave={() => setZiehtDruber(false)}
                      onDrop={e => {
                        e.preventDefault();
                        setZiehtDruber(false);
                        const f = e.dataTransfer.files?.[0];
                        if (f) setDatei(f);
                      }}
                      onClick={() => dateiFeld.current?.click()}
                      className={`flex min-h-[14rem] cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors ${
                        ziehtDruber
                          ? 'border-indigo-400 bg-indigo-50 dark:bg-indigo-900/20'
                          : 'border-gray-300 hover:border-gray-400 dark:border-gray-600 dark:hover:border-gray-500'
                      }`}
                    >
                      <Upload className="mb-2 h-6 w-6 text-gray-400" />
                      {datei ? (
                        <p className="text-sm font-medium text-gray-700 dark:text-gray-200">
                          {datei.name}{' '}
                          <span className="font-normal text-gray-400">
                            ({(datei.size / 1024).toFixed(0)} KB)
                          </span>
                        </p>
                      ) : (
                        <p className="text-sm text-gray-500 dark:text-gray-400">
                          Datei hierher ziehen oder klicken — PDF, DOCX, MD, TXT
                        </p>
                      )}
                      <input
                        ref={dateiFeld}
                        type="file"
                        accept=".md,.txt,.docx,.pdf"
                        className="hidden"
                        onChange={e => setDatei(e.target.files?.[0] || null)}
                      />
                    </div>
                  )}
                </div>

                {/* Nur für diesen Durchgang, und darum hier und nicht in den
                    Einstellungen: Der Name eines Gegenparts, der in genau diesem
                    Schreiben steht, gehört nicht in eine dauerhafte Liste, die
                    niemand mehr durchsieht. */}
                <details
                  open
                  className="min-w-0 self-start rounded-lg border border-gray-200 p-3 dark:border-gray-600"
                >
                  <summary className="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300">
                    Eigene Begriffe für diesen Durchgang
                    {begriffe.trim() && (
                      <span className="ml-2 rounded-full bg-indigo-100 px-2 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                        gesetzt
                      </span>
                    )}
                  </summary>
                  <p className="mb-2 mt-2 text-xs leading-snug text-gray-500 dark:text-gray-400">
                    Kennungen, in denen die Erkennung keinen Namen liest — eine Abkürzung, ein
                    Projektname. Ein Begriff pro Zeile. Er wird überall ersetzt, auch mitten im
                    Wort, aber nur in genau dieser Schreibweise.
                  </p>
                  <textarea
                    value={begriffe}
                    onChange={e => setBegriffe(e.target.value)}
                    rows={8}
                    aria-label="Eigene Begriffe für diesen Durchgang"
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    placeholder={'InnoSmith\nProjekt Nordwind'}
                  />
                </details>
              </div>

              <button
                type="button"
                onClick={anonymisieren}
                disabled={laeuft || !bereit}
                className="mt-4 inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-50"
              >
                <ShieldCheck className="h-4 w-4" />
                {laeuft ? 'Wird geprüft …' : 'Anonymisieren'}
              </button>
            </Karte>

            {ergebnis && (
              <Karte titel="2. Vorschau prüfen">
                <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                  Markierte Stellen sind ersetzt — mit der Maus darüber steht, was dort stand. Eine
                  falsch maskierte Stelle können Sie zurücknehmen; sie erscheint wieder im Klartext
                  und wird so mitkopiert.
                </p>

                {/* Text und Fundstellen nebeneinander: die Liste ist die Legende zum
                    Text, und wer eine Zurücknahme prüft, will beides zugleich sehen. */}
                <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_26rem]">
                  <div className="min-w-0">
                    <div className="mb-3">
                      <RestbestandWarnung restbestaende={ergebnis.restbestaende} />
                    </div>

                    <Textvorschau
                      text={sichtbarerText}
                      nadeln={aktiveMarken}
                      titelFuer={titelFuerErsatz}
                      warnNadeln={ergebnis.restbestaende}
                      warnTitelFuer={n => `${n} — echter Wert, nicht ersetzt`}
                      maxHoehe="max-h-[34rem]"
                    />

                    {zurueckgenommen.size > 0 && (
                      <p className="mt-2 text-sm text-amber-700 dark:text-amber-400">
                        {zurueckgenommen.size} Stelle{zurueckgenommen.size === 1 ? '' : 'n'} wieder
                        im Klartext — das geht mit dem kopierten Text an das Modell.
                      </p>
                    )}

                    <div className="mt-4 flex flex-wrap gap-2">
                      <button
                        onClick={() => kopieren(sichtbarerText, setKopiert)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        {kopiert ? (
                          <Check className="h-4 w-4 text-emerald-500" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                        {kopiert ? 'Kopiert' : 'Text kopieren'}
                      </button>
                      <button
                        onClick={() => setExportOffen(true)}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        <FileText className="h-4 w-4" />
                        Als Word …
                      </button>
                      <button
                        onClick={schluesselSichern}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        <Download className="h-4 w-4" />
                        Schlüssel sichern
                      </button>
                      <button
                        onClick={imChatWeiterverwenden}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        <MessageSquarePlus className="h-4 w-4" />
                        Im Chat weiterverwenden
                      </button>
                      <button
                        onClick={() => setRichtung('zurueck')}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        <ArrowLeft className="h-4 w-4" />
                        Antwort zurückübersetzen
                      </button>
                    </div>
                  </div>

                  <div className="min-w-0 xl:sticky xl:top-4 xl:self-start">
                    <h3 className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">
                      {ergebnis.diff.length} Ersetzungen
                    </h3>
                    <Fundstellenliste
                      fundstellen={ergebnis.diff}
                      zurueckgenommen={zurueckgenommen}
                      onUmschalten={umschalten}
                      maxHoehe="max-h-[34rem]"
                    />

                    <p className="mt-4 text-xs text-gray-400 dark:text-gray-500">
                      Die Zuordnung liegt zwei Stunden im Arbeitsspeicher. Nach einem Neustart
                      braucht der Rückweg die gesicherte Schlüsseldatei.
                    </p>
                  </div>
                </div>
              </Karte>
            )}
          </div>
        ) : (
          <Karte titel="Antwort zurückübersetzen">
            <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
              Die Antwort des Modells einfügen. Erfundene Werte werden durch die echten ersetzt.
              Was sich nicht eindeutig zuordnen lässt, bleibt stehen und wird markiert.
            </p>

            {rueckFehler && (
              <div
                role="alert"
                className="mb-4 rounded-lg border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-800 dark:bg-rose-900/20 dark:text-rose-300"
              >
                {rueckFehler}
              </div>
            )}

            {/* Eingefügte Antwort links, Ergebnis rechts: der Vergleich ist die
                eigentliche Prüfarbeit und verlangt beide Fassungen im Blick. */}
            <div className="grid gap-6 xl:grid-cols-2">
              <div className="min-w-0">
                <p className="mb-3 text-sm">
                  {schluessel ? (
                    <span className="text-gray-700 dark:text-gray-300">
                      Schlüsseldatei geladen{schluesselName ? ` (${schluesselName})` : ''}.
                    </span>
                  ) : ergebnis ? (
                    <span className="text-emerald-700 dark:text-emerald-400">
                      Schlüssel der laufenden Sitzung wird verwendet.
                    </span>
                  ) : (
                    <span className="text-amber-700 dark:text-amber-400">
                      Keine Sitzung — Schlüsseldatei wählen, oder zuerst anonymisieren.
                    </span>
                  )}
                </p>

                <label className="mb-4 block text-sm">
                  <span className="mb-1 block font-medium text-gray-700 dark:text-gray-300">
                    Schlüsseldatei (nach Neustart oder Ablauf)
                  </span>
                  <input
                    type="file"
                    accept=".json,application/json"
                    onChange={e => void schluesselLaden(e.target.files?.[0] ?? null)}
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 file:mr-3 file:rounded file:border-0 file:bg-indigo-50 file:px-3 file:py-1 file:text-sm file:font-medium file:text-indigo-600 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </label>

                <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
                  Antwort des Modells
                </label>
                <textarea
                  value={antwort}
                  onChange={e => setAntwort(e.target.value)}
                  rows={16}
                  className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  placeholder="Antwort einfügen …"
                />

                <button
                  type="button"
                  onClick={() => void zurueckbilden()}
                  disabled={rueckLaeuft || !rueckBereit}
                  className="mt-4 inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-50"
                >
                  <ArrowLeft className="h-4 w-4" />
                  {rueckLaeuft ? 'Wird übersetzt …' : 'Zurückübersetzen'}
                </button>
              </div>

              <div className="min-w-0">
                {zurueck ? (
                  <div className="space-y-3">
                    <RueckstandWarnung rueckstaende={zurueck.rueckstaende} />
                    <Textvorschau
                      text={zurueck.original_text}
                      warnNadeln={zurueck.rueckstaende}
                      warnTitelFuer={() => 'Erfundener Wert — steht noch im Text'}
                      maxHoehe="max-h-[34rem]"
                    />
                    <button
                      onClick={() => kopieren(zurueck.original_text, setRueckKopiert)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      {rueckKopiert ? (
                        <Check className="h-4 w-4 text-emerald-500" />
                      ) : (
                        <Copy className="h-4 w-4" />
                      )}
                      {rueckKopiert
                        ? 'Kopiert'
                        : zurueck.rueckstaende.length === 0
                          ? 'Text kopieren'
                          : 'Trotzdem kopieren'}
                    </button>
                  </div>
                ) : (
                  <div className="flex min-h-[16rem] items-center justify-center rounded-lg border-2 border-dashed border-gray-200 px-6 text-center text-sm text-gray-400 xl:h-full dark:border-gray-700 dark:text-gray-500">
                    Der zurückübersetzte Text erscheint hier.
                  </div>
                )}
              </div>
            </div>
          </Karte>
        )}

        {/* Der Vermerk hier unten ist derselbe wie im Chat -- eine Sprache, drei Orte. */}
        {richtung === 'hin' && ergebnis && ergebnis.diff.length > 0 && (
          <div className="mt-5">
            <Maskenvermerk fundstellen={ergebnis.diff} restbestaende={ergebnis.restbestaende} />
          </div>
        )}
      </div>

      {exportOffen && ergebnis && (
        <ExportDialog
          isOpen={true}
          onClose={() => setExportOffen(false)}
          rawContent={sichtbarerText}
        />
      )}

      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={bgUrl}
        onSelect={handleBgSelect}
      />
    </div>
  );
}

function Karte({ titel, children }: { titel: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-gray-200/70 bg-white/85 p-5 backdrop-blur-sm dark:border-gray-700/70 dark:bg-gray-900/75">
      <h2 className="mb-1 font-semibold text-gray-900 dark:text-white">{titel}</h2>
      {children}
    </section>
  );
}

function Umschalter({
  aktiv,
  onKlick,
  beschriftung,
  klein = false,
}: {
  aktiv: boolean;
  onKlick: () => void;
  beschriftung: string;
  klein?: boolean;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={aktiv}
      onClick={onKlick}
      className={`rounded-lg font-medium transition-colors ${klein ? 'px-3 py-1 text-xs' : 'px-4 py-1.5 text-sm'} ${
        aktiv
          ? 'bg-indigo-600 text-white'
          : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700'
      }`}
    >
      {beschriftung}
    </button>
  );
}
