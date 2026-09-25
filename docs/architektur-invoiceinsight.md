# invoiceInsight — Architekturbeurteilung vor dem Umbau

**Stand:** 21.09.2026 · **Anlass:** TaskPilot soll den Kreditorenprozess operativ
führen und braucht dafür ein FastAPI-Backend statt Streamlit und MCP. Der Kern
`invoiceInsight` hat mit **Swissbankers (sbKreditorenBot) einen zweiten Nutzer**.
Jede Änderung am Kern erreicht diesen Kunden beim nächsten Update.

Dieses Dokument misst die Kopplung, bevor irgendetwas geändert wird. Alle Zahlen
sind am 21.09.2026 erhoben, die Befehle stehen jeweils dabei.

---

## 1. Der Begriff «Kern» trifft zwei verschiedene Dinge

Das ist der wichtigste Befund, und er entschärft die ganze Frage.

Gemessen, welche Module sbKreditorenBot tatsächlich importiert
(`grep -rhoP "from (invoice_insight|pipeline|storage|...)" app/ src/ scripts/`,
vendor und `__pycache__` ausgenommen):

| Kernmodul | von Swissbankers genutzt | wofür |
|---|---|---|
| `pipeline/` | **ja**, 11 Importe | `smart_pipeline`, `document_classifier`, `p7m_extractor`, `confidence` |
| `utils/` | **ja**, 5 Importe | `config_validator`, `ollama_health`, `logging_setup`, `db_compat`, `accrual` |
| `storage/enums` | **ja** | Statuswerte |
| `scheduler/` | **ja** | Dauerbetrieb |
| `app/auth_cookie` | **ja**, 1 Import | Anmeldung |
| `ingestion/` | **nein** | eigenes `src/ingestion/` (Postfach statt Ordner) |
| `app/data_service.py` | **nein** | eigenes `src/dashboard/data_service.py` |
| `app/export_service.py` | nein | — |
| `matching/`, `analysis/`, `report/` | nein | — |

Daraus folgt eine Trennung, die es im Repo nicht gibt, in der Nutzung aber sehr
wohl:

> **Die Extraktionsmaschine ist gemeinsames Gut. Die Auswertungsschicht ist
> InnoSmith-spezifisch und liegt nur zufällig im selben Repo.**

`app/data_service.py` — die Datei mit 19 `@st.cache_data` und dem
Architekturbruch — wird vom Kunden **nicht** benutzt. Swissbankers hat dafür
eine eigene. Das heisst: das FastAPI-Backend, das TaskPilot braucht, lässt sich
bauen, **ohne eine einzige Zeile anzufassen, die der Kunde ausführt.**

---

## 2. Wie weit ist die Kundenkopie vom Kern entfernt

sbKreditorenBot bindet den Kern als **Kopie** unter `vendor/invoiceInsight` ein
(9.2 MB, Verzeichnisdatum 12.05.2026), nicht als Paketabhängigkeit. In
`requirements.txt` steht die Paketinstallation nur als Kommentar für die
lokale Entwicklung.

Unterschied Kopie gegen Kern heute (`diff -rq`, ohne `__pycache__`):

| Verzeichnis | abweichende Dateien |
|---|---|
| `pipeline/` | 5 |
| `app/` | 2 |
| `utils/` | 1 |
| `storage/`, `matching/`, `ingestion/`, `scheduler/`, `accrual/`, `prompts/`, `analysis/`, `report/` | 0 |

Im Kern liegen seit dem Stand der Kopie **6 Commits**: Syslog-Formatter,
Standard-LLM auf `qwen3.6`, LSV-Regex, strukturierte Logfelder, blätterbarer
Vollexport, Tabellenlesen.

**Das Risiko ist damit benannt und begrenzt:** die Kopie ist vier Monate alt und
acht Dateien entfernt. Wer heute den Kern umbaut, vergrössert diesen Abstand —
aber die Kopie ist ein bewusster Schnitt, kein Automatismus. Ein Update nach
Swissbankers ist ein Entscheid, kein Nebeneffekt.

---

## 3. Die drei Mängel, nach Betroffenheit sortiert

### 3.1 Der Kern importiert Instanz-Code — betrifft den Kunden nicht

```
invoiceInsight/app/data_service.py:1818
    from src.db_data_adapter import load_from_database
```

`src.db_data_adapter` liegt in **InnoSmithInvoices**, nicht im Kern. Ein Kunde,
der nur den Kern installiert, hat diese Datei nicht. Dasselbe Muster in
`app/components/research_tab.py:169`.

Betroffen ist ausschliesslich `app/data_service.py`, und die nutzt Swissbankers
nicht. **Reparatur ohne Kundenrisiko möglich.**

### 3.2 Streamlit in der Fachlogik — betrifft den Kunden nicht

19 Funktionen in `app/data_service.py` tragen `@st.cache_data`: KPIs,
Anomalien, Trends, Cashflow, Datenqualität. Das sind fachlich wertvolle
Funktionen hinter einem Oberflächen-Dekorator.

`app/export_service.py` mit `export_page()` ist bereits streamlit-frei und
API-tauglich. `pipeline/`, `storage/`, `matching/`, `ingestion/`, `scheduler/`,
`accrual/`, `prompts/`, `utils/` ebenfalls.

Streamlit steht in `pyproject.toml` schon heute als optionales Extra
`[dashboard]`, nicht in den Kernabhängigkeiten. Die Absicht war also richtig,
nur die Umsetzung ist in `data_service.py` durchgerutscht.

### 3.3 Identität am Pfad statt am Inhalt — betrifft den Kunden nicht

```
InnoSmithInvoices/src/extract.py:127  _get_known_paths()
```

Dokumente werden am Dateipfad wiedererkannt. Verschiebt sich eine Datei,
entsteht eine zweite Zeile zur selben Rechnung. Heute vier Doppelungen bei
1'133 Belegen.

Der `sha256_hash` **wird berechnet** (`ingestion/folder_source.py:124`) und für
die Wiedererkennung **nicht verwendet**.

Diese Stelle liegt in der **Instanz**, nicht im Kern. Und der Kern-`ingestion/`
wird von Swissbankers nicht benutzt — die ziehen Belege aus dem Postfach über
eigenen Code. **Die Korrektur ist in beiden möglichen Lagen kundenfrei.**

Dringlichkeit: der Kreditorenprozess verschiebt planmässig (Eingang →
Bearbeitung → Archiv). Was heute vier Doppelungen sind, wächst dann mit jedem
Beleg.

---

## 4. Was daraus folgt

Alle drei Mängel liegen **ausserhalb** der Fläche, die Swissbankers ausführt.
Das war vor der Messung nicht bekannt und ändert die Lage: es braucht keinen
Kompromiss zwischen «sauber bauen» und «Kunden nicht gefährden».

Was trotzdem gilt und nicht übergangen werden darf:

1. **Die Grenze muss explizit werden, sonst hält sie nicht.** Dass Swissbankers
   `app/data_service.py` nicht nutzt, ist heute ein Zufall der Nutzung, keine
   Eigenschaft der Architektur. Ohne eine benannte Trennung wandert beim
   nächsten Umbau wieder etwas über die Linie.

2. **Ein Test muss die Grenze bewachen.** Der Import `from src.db_data_adapter`
   im Kern ist unbemerkt entstanden und hat Monate überlebt. Eine Prüfung, die
   Importe aus Instanz-Namensräumen im Kern verbietet, hätte ihn am Tag der
   Entstehung gemeldet.

3. **Die Kundenkopie braucht einen definierten Abgleich.** Vier Monate und acht
   Dateien Abstand sind heute harmlos. Ohne Verfahren wird daraus in einem Jahr
   eine Gabelung, und dann ist «der Kern» eine Fiktion.

## 5. Zur Rolle von MCP

Der bestehende MCP-Server (`InnoSmithInvoices/src/mcp_server.py`, 18 Werkzeuge,
11 Ressourcen) ist **kein Bestandteil des Ziels**. Er wird hier nur erwähnt,
weil seine Werkzeugliste zeigt, welche Abfragen im Betrieb tatsächlich gebraucht
werden — das ist brauchbare Vorarbeit für den Zuschnitt der REST-Endpunkte, mehr
nicht. Das Ziel ist ein FastAPI-Backend; MCP läuft weiter oder wird abgestellt,
das ist eine spätere und eigene Entscheidung.
