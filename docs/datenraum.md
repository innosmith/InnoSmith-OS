# Der Datenraum

Ein lokaler Speicher, in dem die Tabellen der Fachsysteme als Parquet-Dateien
liegen. Er beantwortet Auswertungsfragen — Umsatz, offene Posten, Stunden,
Deals — ohne dass ein Fachsystem live befragt werden muss.

## Warum

Am 1. September 2026 lief die Frage «Wie viel Umsatz habe ich mit GSW gemacht?»
nach acht Werkzeugrunden ins Zeitlimit. Die Analyse förderte drei Befunde zutage,
die alle dieselbe Eigenschaft haben: **sie erzeugen eine plausible Zahl statt
einer Fehlermeldung.**

| Befund | Wirkung |
|---|---|
| `GET /kb_invoice?contact_id=X` nimmt den Filter entgegen und ignoriert ihn | Der Umsatz aller 50 Kunden wird einem einzigen zugewiesen |
| Keine Paginierung | 50 von 652 Rechnungen, Ergebnis wirkt vollständig |
| `kb_item_status_id` undekodiert | Entwürfe zählen als Umsatz |

Ein schnellerer Agent hätte diese Fehler nicht behoben, sondern nur früher eine
falsche Zahl geliefert. Die Antwort ist deshalb keine Optimierung der Abfragen,
sondern eine andere Arbeitsteilung.

## Wie es funktioniert

```
Bexio · Toggl · Pipedrive
        │  Sync-Worker im Backend (stündlich bzw. nachts)
        ▼
~/.local/share/taskpilot/datenraum/*.parquet  +  _katalog.json
        │  Executor hängt bei JEDEM Sandbox-Lauf ein
        ▼
Sandbox: /daten (read-only) → duckdb / pandas → nur das Ergebnis
```

Der Agent liest den Katalog, schreibt **eine** Abfrage und bekommt die fertige
Zahl. Aus acht Runden wird eine, und die Rohdaten erreichen das Modell nie.

## Tabellen

| Tabelle | Quelle | Takt |
|---|---|---|
| `bexio_rechnungen` | Buchhaltung | stündlich |
| `bexio_kontakte` | Buchhaltung | stündlich |
| `toggl_zeiteintraege` | Zeiterfassung, rollende 24 Monate | stündlich |
| `toggl_projekte` | Zeiterfassung | stündlich |
| `pipedrive_deals` | CRM, alle Status | nächtlich |
| `pipedrive_personen` | CRM | nächtlich |
| `pipedrive_organisationen` | CRM | nächtlich |

## Die Entscheidung steht in der Spalte, nicht in der Abfrage

`bexio_rechnungen` führt `ist_umsatz` als Wahrheitswert mit. Damit ist «was zählt
als Umsatz» einmal deklariert und nicht in jeder Abfrage neu erfunden — dieselbe
Frage ergibt nächste Woche dieselbe Zahl.

Die zugrundeliegende Statustabelle steht in `src/bexio/rechnungen.py` und nur
dort. Bestätigt am 2. September 2026 gegen den Echtbestand:

| Kennung | Bedeutung | zählt als Umsatz | Anzahl |
|---|---|---|---|
| 7 | Entwurf | nein — nie gestellt | 1 |
| 8 | offen | ja — fakturiert, nicht bezahlt | 11 |
| 9 | bezahlt | ja | 640 |

Eine unbekannte Kennung zählt **nicht** als Umsatz, bleibt aber als
`unbekannt_<id>` in den Daten und wird im Katalog gemeldet. Ein stiller Verlust
sähe aus wie «es gibt nichts».

Wer nach Geldeingang statt nach Umsatz fragt, nutzt die Spalte `bezahlt`.

## Der Bruttobetrag steht nicht in `total_gross`

Die vierte Falle der Bexio-Rohdaten ist die stillste, weil der naheliegende
Feldname der falsche ist: **`total_gross` ist nicht der Betrag inklusive
Mehrwertsteuer**, sondern die Positionssumme vor Rabatt. Bei jeder rabattfreien
Rechnung stimmt sie deshalb exakt mit `total_net` überein — bei 565 von 651
Rechnungen des Bestands.

Der Fehler ist deshalb gefährlich, weil er sich selbst erklärt. Am 02.09.2026
meldete der Agent für GSW «Netto 18'500, Brutto 18'500» und schob nach: «keine
MWST ausgewiesen oder alle Rechnungen als ohne Steuer erfasst». Die Zahl war
falsch, die Begründung plausibel, und niemand hätte nachgerechnet. Aufgefallen
ist es an einer Rechnung über netto 5'625.00, auf die 6'080.65 bezahlt wurden —
genau 8.1 % mehr.

| Spalte | Bexio-Feld | Bedeutung |
|---|---|---|
| `netto` | `total_net` | ohne Mehrwertsteuer — die übliche Umsatzzahl |
| `mwst` | `total_taxes` | ausgewiesene Steuer |
| `brutto` | `total` | inklusive Mehrwertsteuer |

Weil «keine Mehrwertsteuer» eine Aussage über die Daten bleiben muss und nicht
die Ausrede eines Modells für eine falsche Spalte werden darf, zählt der Abgleich
steuerfreie Rechnungen und meldet sie im Katalog als `rechnungen_ohne_mwst`. Es
sind 25 von 652.

## Der Wächter gegen die leere Spalte

Der Bruttofehler war kein Einzelfall, sondern eine Gattung: eine Spalte, die es
gibt und auf die man sich nicht verlassen kann. Bei Toggl trat sie in ihrer
schärfsten Form auf — **alle 2639 Zeiteinträge hatten weder Datum noch Kunde**.

Der Grund: die Reports-API v3 antwortet gruppiert. Eine Zeile trägt `project_id`,
`description` und darunter ein Feld `time_entries` mit den eigentlichen Buchungen.
`start` und `client_id` gibt es auf der obersten Ebene nicht — der erste Entwurf
las beide dort. Die Tabelle war vollzählig, jede Zeitfrage wäre trotzdem falsch
beantwortet worden: nach Stunden für einen Kunden gefragt, hätte sie null ergeben.

Deshalb prüft `tabelle_schreiben` seit dem 02.09.2026 jede geschriebene Tabelle
auf Spalten, die über den **ganzen** Bestand leer sind, protokolliert sie und
führt sie im Katalog als `unbrauchbar_weil_durchgehend_leer`. Einzelne Fehlwerte
sind normal und werden nicht gemeldet. Dass eine Spalte nirgends etwas trägt, ist
es nie: entweder wird sie falsch befüllt oder sie gehört nicht in die Tabelle.

Zwei weitere Befunde derselben Gattung, beide behoben:

- **Archivierte Toggl-Kunden fehlten.** `GET /workspaces/{id}/clients` liefert sie
  nur mit `status=both`. Ohne das standen 195 Einträge ohne Kundennamen da,
  obwohl das Projekt sehr wohl einem zugeordnet war — und für eine Auswertung über
  zwei Jahre ist genau der abgeschlossene Kunde der interessante Fall.
- **Pipedrive lieferte nur Kennungen.** `pipeline_id` und `stage_id` sind Zahlen;
  undekodiert kann niemand nach «Proposal» filtern, sondern nur nach «5». Beide
  werden jetzt zu `trichter` und `phase` aufgelöst, gelöschte Deals fliegen raus,
  und `gewonnen_am` steht neben `abgeschlossen_am`, weil Jahresauswertungen
  gewonnener Deals das genauere Feld brauchen.

Die Gegenprobe nach der Korrektur: bei **allen 651 gestellten Rechnungen** gilt
`brutto = bezahlt + offen` und `brutto = netto + mwst`, ohne eine Abweichung. Und
Toggl bestätigt Bexio unabhängig — 74.5 erfasste Stunden für GSW zu 18'625 CHF
gegen 18'500 CHF fakturiert.

## Die richtige Zahl schützt nicht vor der falschen Aufschlüsselung

Am 02.09.2026 meldete der Agent aus Pipedrive 125 gewonnene Deals über 2'079'710
CHF — beides exakt richtig. Die Rangliste darunter war es nicht: das Amt für
Grundstücke und Gebäude erschien mit 23 Deals über 605'000 CHF, tatsächlich sind
es **3 Deals über 181'000 CHF**. Eine Gesamtsumme zu prüfen genügt also nicht;
sie kann stimmen, während jede Zeile darunter falsch ist.

Aus dem Vorfall folgen drei Regeln, die im Katalog stehen und damit bei jeder
Auswertung mitgeliefert werden:

1. **Gruppiert wird in der Abfrage, nicht im Kopf.** Wer Einzelzeilen ausgibt und
   danach selbst zusammenzählt, produziert Zahlen, die nirgends belegt sind. Jede
   Zahl der Antwort muss so in der Ausgabe des Laufs stehen.
2. **Ein Deal-Wert ist kein Umsatz.** Für dasselbe Amt stehen 181'000 CHF
   gewonnene Deals gegen 227'789 CHF tatsächlich fakturierten Nettoumsatz in
   Bexio. Beide Zahlen sind richtig und beantworten verschiedene Fragen.
3. **Ein Kundenname im CRM ist Freitext.** Derselbe Kunde steht dort unter
   mehreren Schreibweisen — «Mittelschul- und Berufsbildungsamt Kt. Bern» (8
   Deals) und «… Kanton Bern» (2 Deals) sind eine Organisation. Eine Summe je
   Kunde ist deshalb eine Summe je Schreibweise, und das gehört gesagt statt
   stillschweigend geraten.

Die vierte Lehre betrifft nicht die Daten, sondern die Nachvollziehbarkeit: Der
Ablauf eines Chatlaufs deckelte bei 200 Ereignissen, und 140 Denkschritte hatten
den Deckel gefüllt, bevor der erste Werkzeugaufruf kam. Die Abfrage, die die
falsche Rangliste erzeugt hatte, war damit nicht mehr auffindbar. Denkschritte
und Werkzeugaufrufe haben seither getrennte Budgets
(`app/routers/chat.py`, `Ablaufspeicher`), und bei Sandbox-Läufen werden Code und
Ausgabe mitgeschrieben. Ohne den Code ist eine Zahl nicht prüfbar — man sieht,
dass gerechnet wurde, aber nicht was.

Derselbe Auftrag ohne Denkmodus scheiterte an **achtzehn** Code-Ausführungen in
Folge, überwiegend an Syntaxfehlern; mit Denkmodus «kurz» genügten drei
Werkzeugaufrufe für ein nachprüfbar richtiges Ergebnis. Scheitert ein Lauf ohne
Denkmodus an der Ausführung, hängt die Antwort deshalb einen Hinweis an
(`denkmodus_hinweis`). Ausgelöst wird er am gescheiterten Lauf, nicht am Wortlaut
der Frage: ob es sich um eine Auswertung handelt, wäre aus dem Text nur zu raten,
ein gescheiterter Sandbox-Lauf ist eine Tatsache.

## Ein Datum als Text ist kein Datum

Bis zum 02.09.2026 lagen alle Datumsspalten als Zeichenketten im Parquet. Die
Frage «wie viel Umsatz pro Jahr» endete deshalb nicht in einer Zahl, sondern in
`Binder Error: No function matches date_part(STRING_LITERAL, VARCHAR)`. Wer ein
Datum als Text ablegt, verlagert eine Umwandlung, die einmal richtig zu lösen
ist, in jede einzelne Abfrage — und dort wird sie geraten.

`ZEITSPALTEN` in `services/datenraum.py` deklariert je Tabelle, welche Spalten
ein Datum tragen; `zeitspalten_setzen` überführt sie beim Schreiben in echte
`DATE`- bzw. `TIMESTAMP`-Typen. Deklariert, nicht erschlossen: eine Spalte
`datum` daran zu erkennen, dass sie «datum» heisst, wäre bei `beginn`,
`faellig_am` oder `gewonnen_am` sofort gescheitert. Der Zieltyp dagegen folgt den
Werten — ein reines Datum wird `DATE`, ein Zeitstempel bleibt Zeitstempel. Lässt
sich eine Spalte nicht umwandeln, bleibt sie Text und der Vorgang wird
protokolliert; eine unbequeme Spalte ist besser als eine stillschweigend geleerte.

## Zwei Grenzen, die niemand sah

Zwei weitere Ursachen desselben Laufs lagen nicht in den Daten, sondern in
Zahlen, die falsch angenommen waren:

**Das Kontextfenster misst 65'536 Token, nicht 131'072.** Der Werkzeug-Aufschub
(`tools.tool_search`) war mit 15 % konfiguriert in der Annahme des doppelten
Fensters — die Schwelle lag damit bei 9'830 Token und also *unter* den 14'170,
die unsere Werkzeuge wiegen. Die Brücke blieb an, obwohl sie abgeschaltet sein
sollte. Jetzt 25 % (16'384 Token), und der Test rechnet gegen
`LOCAL_CONTEXT_LENGTH` statt gegen eine notierte Zahl, damit die nächste
Fensteränderung nicht wieder still danebengreift.

**Die Sandbox schnitt ihre Ausgabe schweigend ab.** Bei über 20'000 Zeichen
lieferte der Executor kommentarlos die letzten 20'000. Wer nur den Schwanz einer
Rangliste sieht, hält den grössten verbliebenen Posten für den grössten, und
nichts weist darauf hin — dieselbe Fehlerart wie die leere Spalte. `gekuerzt`
benennt die fehlende Menge jetzt im Text und fordert dazu auf, die Abfrage enger
zu fassen.

## Der teuerste Fehler ist der, aus dem niemand aussteigt

Am 02.09.2026 lief eine CRM-Auswertung in das Zeitlimit von 600 Sekunden. Die
Ursache war ein Tippfehler des Modells: es schrieb `gewonn_en_am` statt
`gewonnen_am` und fand achtzehn Sandbox-Läufe lang nicht zurück — obwohl DuckDB
in **jeder** Fehlermeldung unter «Candidate bindings» den richtigen Namen nannte.
Zwischendurch versuchte es `gewonn_am`, `gewenn_am` und einmal
`read_csv_auto` auf einer Parquet-Datei.

Das Bittere: Die Antwort lag nach dem **zweiten** Aufruf vollständig vor — die
komplette, korrekte Kundenrangliste stand in der Ausgabe. Alles danach war
Beiwerk, das der Agent sich selbst aufgetragen hatte (eine Monats- und
Jahresverteilung, nach der niemand gefragt hatte). Nach zehn Minuten bekam der
Nutzer nichts.

Die Schleifenwächter hätten das verhindern sollen, standen aber auf 8
gleichartigen Fehlschlägen, und zwei zufällige Teilerfolge setzten den Zähler
zurück. Sie stehen jetzt auf 5 (`same_tool_failure`) und 3 (`exact_failure`) —
weit genug vor `MAX_AGENT_TIMEOUT`, dass das Modell aus dem bereits Erreichten
noch eine Antwort schreiben kann. In zwei ausgewerteten Läufen mit zusammen 36
Sandbox-Aufrufen hat es sich nach dem dritten Fehlschlag in Folge kein einziges
Mal mehr gefangen; die Grenze kostet also nichts.

Dazu zwei Ergänzungen: Der Katalog weist darauf hin, die gestellte Frage zu
beantworten statt ungefragte Auswertungen anzuhängen, und bei einem unbekannten
Spaltennamen den Vorschlag aus der Fehlermeldung zu übernehmen statt neu zu
raten. Und die Zeitlimit-Meldung nennt jetzt die Zahl der gescheiterten
Code-Läufe (`zeitlimit_grund`) — «Zeitlimit überschritten» allein legt nahe, die
Aufgabe sei zu gross gewesen, und das führt zur falschen Konsequenz.

## Beispiel

```python
import duckdb, json

json.load(open('/daten/_katalog.json'))['stand']   # den Stand in der Antwort nennen

duckdb.sql("""
    SELECT kunde,
           count(*)                 AS rechnungen,
           round(sum(netto), 2)     AS umsatz_netto,
           round(sum(brutto), 2)    AS umsatz_brutto,
           round(sum(offen), 2)     AS noch_offen
    FROM '/daten/bexio_rechnungen.parquet'
    WHERE ist_umsatz AND kunde ILIKE '%GSW%'
    GROUP BY kunde
""")
```

Unscharfe Namen werden mit `ILIKE` über die echten Daten aufgelöst — nicht
geraten und nicht über einen vorgeschalteten Suchaufruf.

## Datenschutz

Der Datenraum enthält den vollständigen Kundenbestand dreier Fachsysteme. Er ist
an vier Stellen gebunden:

1. **Owner-gebunden.** Jeder Pfad in `routers/code_execute.py` verlangt
   `require_role("owner")`; `routers/tasks.py` verweigert einem Member
   `assignee='agent'`. Ein Member kann weder selbst Code ausführen noch einen
   Agenten dazu bringen.
2. **Ohne Netz.** Die Sandbox läuft mit `--network none` und ohne Zugangsdaten.
   Was sie liest, kann sie nirgendwohin senden; ihr einziger Rückkanal ist das
   Ergebnis, das ein Mensch im Chat sieht.
3. **Nur lesend.** `/daten` ist read-only eingehängt.
4. **Kein Rohdatenexport ins Modell.** Der Katalog nennt Tabellen, Spalten und
   Stand — nie Zeilen. Damit wird die Modellwahl wieder eine Frage der Qualität
   statt des Datenschutzes.

Die POSIX-Rechte im Wegwerf-Container sind bewusst **nicht** die Schutzgrenze:
der Sandbox-Benutzer muss lesen können. Die Grenze ist das Heimverzeichnis auf
dem Wirt.

**Aufbewahrung.** Wird eine Quelle abgeschaltet, entfernt der nächste
Vollabgleich ihre Tabellen (`verwaiste_tabellen_entfernen`). Persistente
Sandbox-Arbeitsverzeichnisse (`conv-*`) verfallen nach 30 Tagen
(`TP_SANDBOX_CONV_TTL`) — vorher blieben sie unbefristet liegen. Für ein
Löschbegehren räumt `datenraum_leeren()` alles ab; der nächste Abgleich baut aus
den Quellsystemen neu auf.

## Betrieb

| Variable | Bedeutung |
|---|---|
| `TP_DATENRAUM_ENABLED` | Worker an/aus (Default: an) |
| `TP_DATENRAUM_DIR` | Verzeichnis; muss für Backend und Executor **derselbe Pfad** sein |
| `TP_DATENRAUM_FULL_HOUR` | Stunde des nächtlichen Vollabgleichs (Default: 3) |

Der pfadgleiche Mount ist keine Kosmetik: Snap-Docker löst Bind-Mount-Quellen auf
dem Wirt auf. Stünde im Backend-Container `/home/taskpilot/...`, könnte der
Host-Daemon den Pfad nicht auflösen — dieselbe Bedingung wie beim
Sandbox-Ausgabeverzeichnis.

**Ein Teilausfall bleibt ein Teilausfall.** Fällt Toggl aus, behalten die
Bexio-Tabellen ihren Stand; der Fehler landet im Katalog unter
`quellen.<name>.letzter_fehler`. Eine leer zurückgekommene Tabelle ersetzt nie
eine vorhandene.

## Grenzen

- Der Stand ist bis zu eine Stunde alt. Für taggenaue Fragen gibt es
  `datenraum_auffrischen(quelle)`; es liefert nur Metadaten, nie Zeilen.
- Toggl deckt 24 rollende Monate ab, nicht die gesamte Historie.
- Beträge stehen in der Währung der Rechnung; `waehrung` ist eine eigene Spalte.
  Eine Summe über mehrere Währungen ist die Verantwortung der Abfrage.
