"""Label-Vokabular und Move-Politik der E-Mail-Triage (Single Source of Truth).

Vor dieser Zentralisierung lag die Liste dreifach verstreut: im Skill-Markdown,
in der Ordner-Allowlist des Graph-MCP-Servers und im Ermessen des Modells. Das
Ergebnis waren 80 verschiedene Outlook-Kategorien, von denen 70 frei erfunden
waren, waehrend das strukturierte ``label``-Feld in 64 % der Faelle leer blieb.

Die zehn Labels sind gegen die echte Outlook-Kategorienliste
(``/outlook/masterCategories``) verifiziert und existieren dort exakt so. Anthony
pflegt dort zusaetzlich manuelle Kategorien (``Privates``, ``Transfer``,
``Home Office``, ``Lunch``, ``Admininistratives``, ``Unbekannt``); die vergibt er
selbst, sie gehoeren nicht ins Vokabular des Agenten.

Bewusst KEINE Synonym- oder Aliastabelle: Ein unbekanntes Label ist ein Fehler,
der sichtbar werden soll (``FALLBACK_LABEL`` + ``needs_review``), kein Fall fuer
stilles Zurechtbiegen. Synonyme waeren Symptombehandlung und nie vollstaendig.
"""

# Reihenfolge = Prioritaet in der Skill-Stufenleiter (siehe triage-rules.md).
TRIAGE_LABELS: tuple[str, ...] = (
    "Signale",
    "System",
    "Wichtig",
    "Offerten/Verträge",
    "Networking/Leads",
    "Finanzen",
    "Kalender",
    "Newsletter",
    "Junk",
    "Unklar",
)

# Fail-closed-Ziel: unbekanntes oder fehlendes Label landet hier, nie im Raten.
FALLBACK_LABEL = "Unklar"

# Sentinel des Fallback-Pfades: "keine Kategorie setzen" (nicht raten).
NO_CATEGORY = "Unklassifiziert"

# Labels, die einen Move in einen Inbox-Unterordner rechtfertigen.
#
# Die Unterordner sind Anthonys Tagesverfahren: Was nicht heute Aufmerksamkeit
# braucht, wandert tagsueber hierhin; die Inbox-Wurzel bleibt fuer das Dringliche.
# Geleert wird spaeter ins Archiv -- die niedrigen Fuellstaende belegen, dass das
# Verfahren laeuft, nicht dass die Ordner ungenutzt sind.
#
# Zwei bewusste Auslassungen:
#
# ``Finanzen`` -- reine Sichtmarke in der Inbox ("wichtig, aber nicht am selben
# Tag"). Ein eigener Unterordner waere neben dem Sammelarchiv redundant, und
# verschoben hiesse aus dem Blick.
#
# ``Kalender`` -- nach ``Inbox/Kalender`` verschiebt ausschliesslich der
# deterministische Pfad in ``triage.py`` (``_handle_meeting_response``), und der
# greift nur bei echten ANTWORTEN auf Einladungen (``meetingAccepted``,
# ``meetingDeclined``, ``meetingTentativelyAccepted``). Ueber das LLM-Label darf
# nichts nach Kalender verschwinden: eine Einladung, eine Absage des Veranstalters
# oder die Terminkonflikt-Meldung eines Kunden verlangt eine Reaktion und muss
# sichtbar bleiben -- auch wenn das Modell sie als ``fyi`` einstuft.
#
# ``Junk`` zeigt auf ``Inbox/Junk`` und NICHT auf den Well-Known-Ordner
# "Junk Email". Das ist Absicht: ``Inbox/Junk`` ist Anthonys Sichtungsordner (er
# entscheidet dort selbst ueber Loeschen), waehrend "Junk Email" Outlooks eigene
# Spam-Quarantaene ist. ``move_to_folder()`` loest Zielnamen daher bewusst unter
# ``inbox/childFolders`` auf -- kein Defekt, nicht "korrigieren".
#
# Diese Karte deckt sich absichtlich NICHT mit der Ordner-Allowlist von
# ``move_email_to_folder`` in ``src/mcp-graph/server.py``: die erlaubt weiterhin auch
# ``Kalender``. Sie gilt nur fuer Chat-Agenten (Server ``graphAdmin``), die auf
# ausdrueckliche Anweisung verschieben. Der Triage-Agent hat gar keine Move-Tools
# mehr -- ``graph`` laeuft im ``safe``-Modus.
LABEL_FOLDERS: dict[str, str] = {
    "System": "System",
    "Newsletter": "Newsletter",
    "Junk": "Junk",
}

_BY_CASEFOLD: dict[str, str] = {label.casefold(): label for label in TRIAGE_LABELS}


def normalize_label(value: object) -> str | None:
    """Prueft ein LLM-Label gegen das Vokabular.

    Toleriert ausschliesslich Gross-/Kleinschreibung und umgebende Leerzeichen --
    also Tippvarianten derselben Zeichenkette, keine Bedeutungsvarianten. Gibt
    ``None`` zurueck, wenn das Label nicht im Vokabular steht; der Aufrufer
    entscheidet dann fail-closed (``FALLBACK_LABEL`` + ``needs_review``).
    """
    if not isinstance(value, str):
        return None
    return _BY_CASEFOLD.get(value.strip().casefold())


def folder_for_label(label: str | None) -> str | None:
    """Zielordner eines Labels, oder ``None`` wenn die Mail in der Inbox bleibt."""
    if not label:
        return None
    return LABEL_FOLDERS.get(label)


def move_target(
    label: str | None,
    triage_class: str | None,
    inference_class: str | None,
    *,
    needs_review: bool = False,
) -> str | None:
    """Entscheidet, ob eine LLM-klassifizierte Mail verschoben wird -- und wohin.

    Gibt den Zielordner zurueck oder ``None`` (Mail bleibt ungelesen in der Inbox).
    Drei Bedingungen muessen zusammen erfuellt sein; jede einzelne ist an
    Messdaten begruendet:

    1. Das Label hat ueberhaupt einen Zielordner (``LABEL_FOLDERS``).
    2. ``triage_class == 'fyi'`` -- Handlungsbedarf ist damit bereits modelliert.
       Alles, wofuer eine Aufgabe oder ein Entwurf entstand, bleibt sichtbar.
    3. ``inferenceClassification == 'other'`` -- Outlooks eigene Einordnung. In 30
       Tagen waren das 102 Mails, davon 100 % ``fyi``, keine mit Task oder Entwurf.
       Rueckwirkend auf die 251 tatsaechlich verschobenen Mails angewendet, haette
       diese Bedingung jeden der beklagten Fehlmoves verhindert: die betroffenen
       Mails echter Kontakte trugen ausnahmslos ``focused``.

    ``needs_review`` (unbekanntes Label, fehlgeschlagener Parse) verhindert den
    Move zusaetzlich -- was das System nicht verstanden hat, raeumt es nicht weg.

    Wiederkehrendes Systemrauschen laeuft NICHT hierueber, sondern ueber
    deterministische Absender-Regeln (``learned_rules``), die vor dem LLM greifen
    und ihren Zielordner selbst tragen. Diese Funktion ist die Rueckfallebene fuer
    alles, was das LLM klassifiziert hat.
    """
    if needs_review:
        return None
    if triage_class != "fyi":
        return None
    if (inference_class or "").strip().casefold() != "other":
        return None
    return folder_for_label(label)
