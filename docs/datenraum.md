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
| `bexio_journal` | Buchhaltung, **alle** Buchungen — die einzige vollständige Ausgabenquelle | stündlich |
| `bexio_rechnungen` | Buchhaltung, Debitoren | stündlich |
| `bexio_kontakte` | Buchhaltung | stündlich |
| `bexio_kreditoren` | Buchhaltung, Lieferantenrechnungen (rund 22 % des Aufwands) | stündlich |
| `bexio_konten` | Kontenplan — löst Kontokennungen im Journal auf | stündlich |
| `bexio_geschaeftsjahre` | Geschäftsjahre mit Abschlussstand | stündlich |
| `toggl_zeiteintraege` | Zeiterfassung, rollende 24 Monate | stündlich |
| `toggl_projekte` | Zeiterfassung | stündlich |
| `pipedrive_deals` | CRM, alle Status | nächtlich |
| `pipedrive_personen` | CRM | nächtlich |
| `pipedrive_organisationen` | CRM | nächtlich |
| `invoiceinsight_rechnungen` | Belegauswertung der Kreditoren | nächtlich |
| `kundenschluessel` | gepflegte Zuordnung: welche Kennungen dieselbe Kundschaft meinen | nach jedem Abgleich |

## Drei Systeme, drei Namen, keine gemeinsame Kennung

Die teuerste Falle des Datenraums betrifft keine Spalte, sondern einen Namen. In
Toggl heisst die Kundschaft `AGG`. In der Buchhaltung heisst dieselbe Kundschaft
`Bau- und Verkehrsdirektion des Kantons Bern (BVD) Amt für Grundstücke und
Gebäude`. Im CRM `Amt für Grundstücke und Gebäude AGG`. Die Kennungsräume
überschneiden sich ohnehin nicht — eine Toggl-Kennung ist achtstellig, eine
Bexio-Kennung dreistellig —, und über den Namen finden nur **8 von 16**
Toggl-Kunden ihr Gegenstück in der Buchhaltung.

Und das ist die schlechtere Hälfte, denn wer in `bexio_rechnungen` nach `%AGG%`
sucht, bekommt null Zeilen. Nicht null als Fehler, sondern null als Ergebnis:
**0 CHF Umsatz**, sauber formatiert, ohne Warnung. Tatsächlich sind es 227'789 CHF
auf 49 Rechnungen. Es trifft ausgerechnet die grössten Kunden, weil gerade deren
Namen zu Kürzeln werden: MBA (676'880 CHF), BFH, AUE, WA-AUE. Was funktioniert,
sind die kurzen Firmennamen — `T+R`, `GSW`, `be-advanced`. Ein Verfahren, das bei
GSW klappt und bei MBA lautlos versagt, ist unbrauchbar, weil man ihm den
Unterschied nicht ansieht.

Schlimmer als die Null ist der Beinahe-Treffer. Bexio führt einen **zweiten**
Kontakt namens `Amt für Grundstücke und Gebäude (AGG` — mit null Rechnungen. Wer
brav die Kontakte nach dem Kürzel durchsucht, die `kunden_id` nimmt und sauber
verknüpft, hat alles richtig gemacht und bekommt trotzdem 0 CHF. Der Join ist
fehlerfrei, das Ergebnis falsch, und nichts daran sieht verdächtig aus. 138 der
188 Kontakte haben überhaupt keine Rechnung: einen Kontakt zu finden heisst nicht,
Umsatz gefunden zu haben.

Eine Ähnlichkeitssuche löst das nicht, sie verschiebt es nur. `LIKE '%agg%'`
trifft auch `Jaggi Lorenz`, und keine Stammform bringt `AGG` mit einer
Direktionsbezeichnung zusammen. Die Menge der Arten, wie drei Systeme dieselbe
Organisation benennen, ist offen — sie hat keine endliche Regel.

Deshalb ist die Zuordnung ein **Stammdatum**: `docs/kundenschluessel.yaml`, von
einem Modell einmal vorgeschlagen, vom Menschen bestätigt, versioniert. 40
Kundschaften, 102 Kennungen. Der Sync-Worker macht daraus die Tabelle
`kundenschluessel`, und der Join läuft über sie:

```sql
SELECT k.name, round(sum(r.netto), 2) AS umsatz
FROM '/daten/bexio_rechnungen.parquet' r
JOIN '/daten/kundenschluessel.parquet' k
  ON k.system = 'bexio' AND k.fremd_id = r.kunden_id
WHERE r.ist_umsatz AND k.schluessel = 'agg'
GROUP BY 1
```

Die Zeilenform ist lang statt breit — eine Zeile je Kennung —, damit der Join
eine gewöhnliche Verknüpfung bleibt und kein Entpacken einer Liste verlangt. Die
`1:n`-Fälle sind dabei kein Sonderfall, sondern der Normalfall: Gemeinde Köniz
führt in Bexio zwei Direktionen, T+R steht im CRM dreimal, MBA zweimal plus
einmal auf Englisch.

### Der Preis ist Pflege, und dagegen stehen zwei Wächter

Eine gepflegte Datei verfällt, und ihr Verfall wäre wieder ein stilles Versagen:
eine nicht zugeordnete Kundschaft fällt aus jedem Join heraus, ohne dass jemand
es merkt. Zwei Prüfungen halten dagegen.

**Jede Kennung wird gegen die Daten geprüft.** Steht in der Datei eine
`kunden_id`, die es im Bestand nicht gibt, wird sie verworfen und gemeldet statt
eine tote Verknüpfung zu erzeugen — die sähe aus wie eine Zuordnung und trüge
keine.

**Was fehlt, wird benannt.** `nicht_zugeordnet` nennt je System die
Kundschaften ohne Schlüssel. Entscheidend war hier die Einschränkung auf das, was
wirtschaftlich zählt: gemeldet wird in Bexio, wer eine gestellte Rechnung hat, im
CRM, wer einen Auftrag gewonnen hat. Ohne diese Einschränkung meldete der Wächter
146 Bexio-Kontakte, überwiegend Lieferanten, die richtigerweise nie einen
Schlüssel bekommen. Eine Warnliste, die zu neunzig Prozent aus Rauschen besteht,
wird nicht gelesen — und wirkt damit wie gar keine. Mit ihr sind es 17, und die
sind einzeln prüfbar.

Was sich nicht eindeutig zuordnen liess, steht unter `offen` und wird **nicht
geraten**: die CRM-Organisation `Kanton Bern` etwa, mit 155'000 CHF gewonnenen
Aufträgen, hinter der ein Amt steht, das niemand benannt hat. Unsicherheit ist ein
zulässiges Ergebnis; ein geratener Schlüssel wäre ein Fehler mit Zinseszins.

Der Toggl-Kunde `WA-AUE` stand ebenfalls dort und ist inzwischen beantwortet: das
Kürzel trägt den Namen des Amts für Umwelt und Energie, weil dieses die inhaltliche
Kontrolle hatte — die Rechnung ging an die Wyss Academy, also zählt der Aufwand
dorthin. Genau diese Art Wissen steht in keinem der drei Systeme.

### Die Datei ist der Prüfpfad, nicht die Bedienoberfläche

Die erste Fassung verlangte, 40 Einträge von Hand zu bestätigen. Das war die
falsche Zumutung, und die Zahlen zeigen warum: seit 2023 kamen 5, 1, 1 und 5 neue
Rechnungskunden pro Jahr dazu, und nur ein Bruchteil davon heisst in zwei Systemen
verschieden. Für zwei bis vier Änderungen im Jahr einen Menschen YAML editieren zu
lassen, heisst eine Pflege einzurichten, die verfällt.

Seither führen drei Wege in die Datei, und nur der letzte kostet Aufmerksamkeit:

| Weg | Wer | Ergebnis |
|---|---|---|
| `vorschlagen()` nach jedem Abgleich | lokales Modell | Kennung unter `vorgeschlagen`, gilt als ungeprüft |
| Werkzeug `kundschaft_zuordnen` | Mensch, im Gespräch | `bestaetigt: true` |
| `frage_notieren()` | Modell, wenn unsicher | Eintrag unter `offen` |

Die tragende Grenze verläuft zwischen den ersten beiden: **eine Maschine darf
hinzufügen, nie ändern oder entfernen.** Eine neue Kennung war noch nie Gegenstand
einer menschlichen Entscheidung — sie zu ergänzen überschreibt nichts. Eine
bestehende zu korrigieren hiesse, ein Urteil zu überstimmen.

Daraus folgt, dass `bestaetigt` am Eintrag nicht genügt. Ergänzt eine Maschine
eine Kennung an einem geprüften Eintrag, gäbe es ohne ein zweites Feld nur zwei
hässliche Möglichkeiten: die Ergänzung gilt still als bestätigt (ein Urteil, das
niemand gefällt hat), oder der ganze Eintrag verliert seine Bestätigung (ein
Urteil, das jemand gefällt hat, wird zurückgenommen). Deshalb nennt
`vorgeschlagen` die einzelnen Kennungen, und `aufbauen` setzt genau für diese
Zeilen `bestaetigt = false`.

Beim ersten Lauf gegen die echten Daten schlug das Modell **eine** Zuordnung vor —
Pipedrive 490 heisst wörtlich «Finanzidustrie» wie der Eintrag, den es traf — und
stellte für den Rest sieben Fragen. Das ist das erwünschte Verhältnis: es ordnet
zu, wo der Name es hergibt, und fragt, wo er es nicht hergibt.

Die Fragen erreichen den Menschen im Katalog, und zwar **im Wortlaut**. Der erste
Entwurf schrieb dort nur ein Etikett («pipedrive 652 (Kanton Bern)») — damit wäre
die Frage im Katalog liegengeblieben und hätte nie jemanden erreicht. Eine Frage,
die niemand hört, ist so gut wie keine.

### Die zweite Hälfte desselben Fehlers

Die 600'000 CHF, die das System für AGG einmal meldete, hatten noch eine zweite
Ursache. Der Filter lief nicht nur über den Organisationsnamen, sondern über den
**Freitext des Deal-Titels** — und ein Deal der Organisation `Kanton Bern` trägt
`AGG` im Titel. So kamen 150'000 CHF dazu, die einer anderen Kundschaft gehören.

Darum verbietet ein Test, dass ein Rezept auf `kunde`, `lieferant` oder
`organisation` mit `LIKE` filtert. Ein Rezept ist die geprüfte Referenz; eines,
das den Namensfilter vorführt, lehrt genau das Falsche.

### Umsatz-Lesart: drei Zahlen, drei Fragen

Bleibt der Fehler darunter — die falsche Tabelle. Für dieselbe Kundschaft stehen
drei richtige Zahlen nebeneinander:

| Frage | Tabelle | AGG am 03.09.2026 |
|---|---|---|
| Was steht im Verkaufstrichter? | `pipedrive_deals`, gewonnen | 181'000 CHF |
| Was wurde fakturiert? | `bexio_rechnungen`, `ist_umsatz` | **227'789 CHF** |
| Wie viel Aufwand steckt drin? | `toggl_zeiteintraege`, verrechenbar | 59'160 CHF |

Umsatz ist die mittlere. Ein gewonnener Deal ist eine Absicht, erfasste Zeit ist
Aufwand. Über zwei davon zu summieren ergibt eine Zahl, die es nicht gibt — das
Gegenstück zur `ausgaben_lesart` auf der Einnahmenseite, und aus demselben Grund
im Katalog als `umsatz_lesart` deklariert.

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

## Das Journal ist die Ausgabenwahrheit, nicht die Kreditorenliste

Bis zum 03.09.2026 stand im Katalog, für Ausgabensummen sei `bexio_kreditoren`
zuständig. Das war falsch, und zwar auf die teuerste Art: die Zahl kam, sie sah
plausibel aus, und sie war zu klein.

| Jahr | Aufwand insgesamt | davon über den Kreditorenweg | Anteil |
|---|---|---|---|
| 2025 | 401'459 CHF | 88'177 CHF | 22 % |
| 2026 (bis Sept.) | 266'476 CHF | 56'056 CHF | 21 % |

Beide Spalten stammen aus **derselben** Tabelle und demselben Datumsbegriff — das
ist der Punkt. Die rechte Spalte ist der Aufwand mit Habenkonto `2000`, nicht die
Summe über `bexio_kreditoren`.

Der Grund ist kein Fehler in Bexio, sondern der Zahlweg. Eine mit Karte bezahlte
Rechnung wird nicht als Lieferantenrechnung erfasst, sondern direkt gebucht:

```
Soll 6570 Software / Haben 2120 Kontokorrent Gesellschafter
Soll 6570 Software / Haben 2203 Bezugsteuer        (bei Auslandsleistungen)
```

Cursor steht 2026 mit 31 Rechnungen und 12'924 CHF im Journal — und mit **null**
in `bexio_kreditoren`. Von 299 Aufwandsbuchungen des Jahres 2026 tragen 34 die
Herkunft `lieferantenrechnung`; 245 sind manuell erfasst. Manuell ist hier der
Normalfall, nicht die Ausnahme.

Daraus folgt die Aufteilung, die der Katalog als `ausgaben_lesart` mitgibt:

| Frage | Tabelle | Filter |
|---|---|---|
| Was hat uns X gekostet? | `bexio_journal` | `WHERE ist_aufwand`, gruppiert nach `soll_konto` |
| Was ist offen, wann fällig? | `bexio_kreditoren` | `WHERE ist_offen` |
| Wofür genau, welche MwSt, wann läuft das Abo aus? | `invoiceinsight_rechnungen` | `WHERE dokumenttyp = 'RECHNUNG'` |

### Die Kreditorenliste ist in beide Richtungen falsch

Die erste Fassung dieser Seite behauptete, `bexio_kreditoren` summiere 2025 auf
88'177 CHF. Das war eine Verwechslung: die Tabelle summiert auf **156'934 CHF**,
die 88'177 stammen aus dem Journal. Ein Agent hätte die Aussage in zehn Sekunden
widerlegt — und danach dem übrigen Katalog zu Recht misstraut.

Die naive Summe irrt **gleichzeitig nach oben und nach unten**:

- **Zu hoch**, weil 68'796 CHF davon gar kein Aufwand sind, sondern Bilanzbuchungen:
  Rückzahlungen an den Inhaber (`2120`, 34'500 CHF), MWST-Abrechnung (`2201`,
  23'771 CHF), beschlossene Ausschüttung (`2261`, 10'500 CHF).
- **Zu tief**, weil alles fehlt, was nie als Lieferantenrechnung erfasst wurde.

Beide Fehler heben sich teilweise auf, und genau deshalb sah 156'934 plausibel
aus. Filtert man nach Aufwandskonten — erste Ziffer von `konto_nr` zwischen 4 und
8 —, stimmen die Tabellen überein:

| Jahr | `bexio_kreditoren`, aufwandskontiert | Journal, Haben 2000 | Differenz |
|---|---|---|---|
| 2023 | 113'767 CHF | 113'767 CHF | 0 |
| 2024 | 81'475 CHF | 81'475 CHF | 0 |
| 2025 | 88'138 CHF | 88'177 CHF | 39 CHF |
| 2026 | 56'056 CHF | 56'056 CHF | 0 |

Die Systeme widersprechen sich also nicht. Die naive Summe stellt bloss eine
andere Frage als die, die gemeint war.

### Die Bezugsteuer verdoppelt die Anzahl, nicht den Betrag

Eine Leistung aus dem Ausland erzeugt **zwei** Aufwandsbuchungen auf demselben
Sollkonto: den Rechnungsbetrag gegen den Zahlweg und die Bezugsteuer gegen
`2203`. Cursor 2026 steht deshalb mit 62 Buchungen da und hat 31 Rechnungen —
11'956 CHF plus 968 CHF Steuer, zusammen 12'924 CHF.

Für Summen ist beides richtig: dieses Konto zieht keine Vorsteuer ab, die Steuer
ist echter Aufwand. Für Anzahlen ist es falsch. Wer Rechnungen zählen will,
schliesst `haben_konto_nr = '2203'` aus — der Katalog gibt die Abfrage fertig mit.

### Die Währungsfalle erwischte den Autor dieser Seite

Beim Nachmessen der obigen Zahlen summierte ich `betrag` statt `betrag_chf` und
schrieb daraufhin vier korrigierte Werte in diese Datei, die alle zu hoch waren.
Aufgefallen ist es erst, als ein fertiges Rezept für Cursor 2026 11'956 CHF
lieferte, wo meine eigene Abfrage 16'164 sagte.

Das ist bemerkenswert, weil die Deklaration die Falle bereits benannte
(«über mehrere Währungen NICHT summierbar») und sie trotzdem nicht verhinderte.
250 der 5262 Buchungen lauten auf Fremdwährung; über den ganzen Bestand macht das
4'450 CHF aus, bei einem einzelnen Lieferanten aber ein Viertel.

Zwei Folgerungen sind in den Katalog eingeflossen. Erstens nennt die Deklaration
jetzt die **gemessene Abweichung** statt nur die Eigenschaft — eine Warnung mit
Zahl bleibt haften, eine ohne nicht. Zweitens sind die fertigen Rezepte nicht
bloss eine Hilfe für schwache Modelle: sie waren hier die Instanz, die den Fehler
fand. Was im Rezept steht, ist geprüft; was daneben entsteht, ist es nicht.

### Die Sollseite entscheidet

Jede Buchung nennt zwei Konten. `1021 Geschäftskonto` als Sollkonto ist ein
Geldeingang, kein Aufwand. Deshalb ist `ist_aufwand` als Eigenschaft der
**Sollseite** definiert (Kontoklasse 4000–8999) und nicht der Buchung. Wer beide
Seiten summiert, zählt jeden Betrag doppelt.

`haben_konto_nr` beantwortet dafür die zweite Frage: **wie** bezahlt wurde. `2120`
heisst, der Inhaber hat vorgeschossen; `2000`, es lief über eine
Lieferantenrechnung; `1021` und `1020`, direkt ab Bankkonto. Damit ist auch «was
habe ich privat vorgeschossen» eine Abfrage statt einer Schätzung: 2025 waren das
24'255 CHF in 130 Buchungen.

### Blättern wurde geprüft, nicht angenommen

`/4.0/purchase/bills` nimmt `offset` entgegen und ignoriert ihn. Ob das
Journal dieselbe Falle stellt, liess sich nicht durch Hinsehen entscheiden: der
grösste Jahrgang hat 809 Buchungen bei einem Limit von 2000, es wurde also nie
geblättert. `_blaettern_pruefen` fordert deshalb einmal zwei kleine Seiten an und
vergleicht die Kennungen. Zusätzlich bricht `get_journal` ab, sobald eine Seite
keine neue Kennung mehr bringt — ohne diesen Wächter liefe die Schleife endlos,
falls `offset` je wirkungslos wird.

### Zwei Stammdatentabellen, die je einen stillen Fehler verhindern

`bexio_konten` löst Kontokennungen auf. Ohne sie ist `debit_account_id: 227` eine
nackte Zahl.

`bexio_geschaeftsjahre` sagt, ob ein Jahr abgeschlossen ist. 2025 steht mit
401'459 CHF als volles Jahr da, 2026 mit 266'476 CHF als Teiljahr von acht
Monaten. Nebeneinandergestellt sieht das nach einem Einbruch von einem Drittel
aus, wo bloss Monate fehlen — arithmetisch richtig, inhaltlich falsch.

## Zwei Kreditorentabellen, und keine ist die bessere

Kreditoren stehen an zwei Orten, und der Reflex, den «richtigen» zu wählen, führt
in beiden Richtungen in die Irre. Sie beantworten verschiedene Fragen:

| | `bexio_kreditoren` | `invoiceinsight_rechnungen` |
|---|---|---|
| Was es ist | die Buchhaltung | die Belegauswertung |
| Beantwortet | **wie viel** | **wofür** |
| Zeilen (3.9.2026) | 435 | 1111 |
| Zeitraum | 2019–2026 | 2018–2026 |
| Währungen | CHF, EUR | CHF, USD, EUR |
| Mehrwertsteuer | keine (siehe unten) | ausgewiesen, wo die Rechnung eine trug |
| Detailgrad | Betrag und Aufwandskonto | Produkt, Kategorie, Abrechnungszyklus, Erneuerung |
| Verbindlich | ja | nein |

### Es sind dieselben Belege — belegweise nachgewiesen

Der Verdacht, hier würden Äpfel mit Birnen verglichen, hat sich nicht bestätigt.
Bei Lieferanten, die auf Rechnung fakturieren, decken sich die Belege fast exakt:

| Lieferant | in Bexio | davon in InvoiceInsight, gleiches Datum und gleicher Betrag |
|---|---|---|
| T+R AG | 16 | 15 |
| bexio AG | 8 | 7 (über acht Jahre) |

Wo die Bestände auseinanderlaufen, gibt es genau drei Gründe — und keiner davon
ist ein Rechenfehler:

1. **Zahlweg.** Mit Karte bezahlte Abos stehen nicht in `bexio_kreditoren`,
   sondern im Journal gegen `2120 Kontokorrent Gesellschafter`. Sie erreichen die
   Buchhaltung also sehr wohl, nur an anderer Stelle. Cursor 2026: 12'924 CHF im
   Journal, null in `bexio_kreditoren`.
2. **Periode.** `invoiceinsight_rechnungen.datum` ist das Rechnungsdatum,
   `bexio_journal.datum` das Buchungsdatum. Über den Jahreswechsel landet derselbe
   Beleg dadurch in zwei verschiedenen Jahren.
3. **Sammelbuchung.** Cursor 2026: 129 Einzelrechnungen in der Belegauswertung
   gegen 31 Buchungsvorgänge in Bexio.

Dazu kommen fehlende Belege in der Belegauswertung — Ausgleichskasse 2022: Bexio
13, InvoiceInsight 8.

Daraus folgt die Regel, die im Katalog als `ausgaben_lesart` beim Agenten
ankommt: **eine Tabelle wählen und dazusagen, welche.** Eine Summe je Lieferant
über zwei Tabellen hinweg zählt doppelt.

Nützlich ist gerade die Unterdeckung: Wie viel läuft am Kreditorenbot vorbei? Das
ist eine beantwortbare Frage und nicht ein Datenfehler.

## Bei den Kreditoren gibt es keine Mehrwertsteuer

Bei den Debitoren war `total_gross` ein falsch benannter Bruttobetrag. Bei den
Kreditoren ist die Falle die spiegelbildliche: `gross` und `net` sind bei **allen
435** Rechnungen identisch, und `tax_id` ist über den ganzen Bestand leer. Dieses
Konto verbucht Kreditoren ohne Vorsteuer.

Es gibt deshalb genau **einen** Betrag und bewusst keine Steuerspalte. Wer sie
vermisst, findet die Vorsteuer in `invoiceinsight_rechnungen.mwst` — dort, wo der
Beleg sie auswies.

Dort bedeutet ein leeres Feld allerdings **zweierlei**, und die beiden Fälle sind
nicht unterscheidbar: entweder trug der Beleg keine Steuer (AHV, BVG, Steuern und
Versicherungen sind befreit) oder sie war nicht lesbar. Bei Auslandsrechnungen ist
leer der Normalfall — dort entsteht die Schweizer Steuer erst in der Buchhaltung
als Bezugsteuer, gegen Habenkonto `2203`, und steht nie auf dem Beleg. Der Katalog
sagt das, statt einen leeren Wert für eine Null auszugeben.

## Der QR-Code ist die einzige Zahl, die niemand geschätzt hat

Ein Teil der Belege trägt einen Schweizer QR-Einzahlungsschein. Was dort steht —
Betrag, Zahlungsempfänger, IBAN, Referenz — ist **maschinell gelesen**, nicht vom
Modell aus einem Bild erschlossen. Diese Felder lagen bis zum 03.09.2026 in der
Datenbank und nicht im Export.

Jetzt gehen sie mit, und zwar ausdrücklich **nicht** als zweite Wahrheit: für
Summen bleibt `betrag_chf` zuständig. `qr_betrag` ist die Gegenprobe. Weicht er
ab, ist das ein Prüffall — der einzige Weg, eine Extraktionsungenauigkeit
überhaupt zu bemerken, ohne den Beleg von Hand aufzuschlagen.

Dazu kommt `beleg_datei`: der Dateiname des PDF, damit eine Zahl auf ihren Beleg
zeigt. Der Pfad bleibt draussen — er liegt auf einer fremden Maschine, und wer ihn
mitschickt, verspricht etwas, das der Empfänger nicht öffnen kann.

## Erklärungspflicht statt Nachpflege

Am 03.09.2026 waren 8 von 33 Spalten der Belegauswertung im Katalog erklärt. Der
Rest war da, benutzbar und unerklärt — und genau dort entstehen die stillen
Fehler: eine Zahl summiert man falsch, ein Datum vergleicht man falsch, eine
Wahrheit filtert man falsch, und in allen drei Fällen kommt ein plausibles
Ergebnis heraus statt einer Fehlermeldung. Bei Text passiert das nicht: wer den
falschen Namen liest, sieht es.

Nachpflegen hält nicht. Deshalb ist es eine Invariante:

> Jede Zahl-, Datums- und Wahrheitsspalte im Katalog braucht einen Eintrag in
> `SPALTEN_BEDEUTUNG`. Kennungen (`*_id`) sind ausgenommen — sie sind Identität,
> nicht Messung. Ihr Textzwilling ist es nicht: die Wahl zwischen `lieferant` und
> `lieferant_id` ist eine Entscheidung, und die falsche gruppiert nach
> Schreibweise.

`test_datenraum_konsistenz.py::TestErklaerungspflicht` prüft das gegen den
tatsächlichen Katalog, in beide Richtungen: keine unerklärte Spalte, und keine
Erklärung, die ins Leere zeigt. Eine neue Spalte bricht den Test, bis jemand
entschieden hat, was sie bedeutet.

## `offset` blättert nicht, `page` schon

`GET /4.0/purchase/bills` nimmt `offset` entgegen und ignoriert es. Am 3.
September 2026 gemessen: `offset=50` lieferte exakt dieselben 50 Zeilen wie
`offset=0`. Dieselbe Gattung wie der wirkungslose `contact_id`-Filter — eine
plausible Antwort statt einer Fehlermeldung. Wer `offset` benutzt, lädt fünfmal
die erste Seite und hält 100 Zeilen für 435.

Geblättert wird über `page`. Die Sollzahl steht in `paging.item_count` und wird
nach jedem Abgleich gegen die eingesammelten Zeilen geprüft; eine Abweichung
landet als `kreditoren_unvollstaendig` im Katalog.

Drei Dinge stehen zudem **nur im Einzelabruf** und in keiner Listenantwort: die
Lieferantenkennung `supplier_id`, die Kontierung, und `base_currency_amount` — der
CHF-Gegenwert einer Fremdwährungsrechnung. Der Abgleich holt deshalb jede Rechnung
einzeln nach, acht parallel; für 435 Rechnungen dauert das rund acht Sekunden.

Nach dem freien Lieferantennamen zu gruppieren wäre dieselbe Falle, die bei
Pipedrive den Umsatz eines Amts vervielfacht hat. Die stabile Kennung ist
`lieferant_id`.

## `jahreskosten_chf` ist eine Hochrechnung, keine Ausgabe

Die gefährlichste Spalte von `invoiceinsight_rechnungen`: Eine Monatsrechnung über
20 Franken steht dort mit 240. Die Spalte beantwortet «was kostet uns das im Jahr»
und niemals «was haben wir ausgegeben». Für Ausgaben gilt `betrag_chf`.

Ebenso: Nicht jede Zeile ist eine Rechnung. `dokumenttyp` kennt auch
`KONTOAUSZUG`, `MAHNUNG`, `GUTSCHRIFT` und `VORAUSRECHNUNG`. Ausgabensummen
gehören auf `RECHNUNG` eingeschränkt.

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

### Null zählt wie leer

Der Wächter fing zunächst nur `NULL` und leeren Text — und ging deshalb an
`bexio_kreditoren.offen_betrag` vorbei. Bexio füllt `pending_amount` an dieser
Schnittstelle nicht: die Spalte steht bei **allen 435 Lieferantenrechnungen auf
0.00**, auch bei den drei tatsächlich offenen über 4'496 CHF. Auf die Frage «wie
viel schulde ich meinen Lieferanten» hätte `sum(offen_betrag)` geantwortet: nichts.

Fachlich ist das derselbe Fehler wie eine leere Spalte — eine Auskunft, die
versprochen und nicht gegeben wird —, technisch war es einer zu wenig. Seit dem
03.09.2026 gilt eine Zahlspalte auch dann als unbrauchbar, wenn **kein einziger**
Wert von null abweicht. Zwei Abgrenzungen gehören dazu, sonst würde der Wächter
selbst zur Fehlerquelle:

- Ein einziger abweichender Wert rettet die Spalte. Sonst verschwände `offen`
  aus dem Katalog, sobald einmal alle Rechnungen bezahlt sind.
- Wahrheitsspalten sind ausgenommen. «Überall `false`» ist eine Aussage und keine
  Lücke: dass gerade nichts überfällig ist, will man wissen und nicht als Defekt
  gemeldet bekommen.

Weil der Wächter erst beim nächsten Abgleich anschlägt, steht die Warnung
zusätzlich in der Deklaration — mitsamt dem richtigen Weg,
`WHERE ist_offen` über `betrag_chf`.

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
