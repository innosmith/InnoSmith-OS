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
# Enthaelt ``Unklar`` -- das ist die Menge der in Outlook gueltigen Kategorien und
# damit das Vokabular fuer die manuelle Korrektur im Cockpit. Was der AGENT waehlen
# darf, steht in ``AGENT_LABELS``.
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

# Das Vokabular des Agenten -- ohne ``Unklar``.
#
# ``Unklar`` war bis August 2026 waehlbar, und das Modell nutzte es als bequemen
# Ausweg statt als Notausgang: gemessen 20 % aller kategorisierten Mails in den
# Wochen 31-33/2026 (vorher 0 %), praktisch keine davon mit ``needs_review``.
# Damit war der Fall doppelt unsichtbar -- keine Aufgabe, keine Sichtungsmarke,
# nur eine nichtssagende Outlook-Kategorie. Betroffen waren echte Kundenthreads
# ("AW: Offerte KI-Basisschulungen", "WG: Offene Fragen TaxCheck-GSW").
#
# Gleichzeitig blieben ``Offerten/Verträge``, ``Networking/Leads`` und ``Signale``
# seit Woche 27 ohne eine einzige Vergabe: das Modell wich aus, statt sich zu
# entscheiden.
#
# ``Unklar`` bleibt erreichbar, aber nur noch als Urteil des BACKENDS ueber den
# Lauf (siehe ``normalize_agent_label``) -- und dann zwingend mit
# ``needs_review``. Die Unsicherheit des Modells gehoert ins Feld ``confidence``,
# nicht ins Label: sie ist dort eine Zahl, die das Unsicherheits-Gate auswerten
# kann, statt einer Kategorie, die nur wie eine Aussage aussieht.
AGENT_LABELS: tuple[str, ...] = tuple(
    label for label in TRIAGE_LABELS if label != FALLBACK_LABEL
)

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
_AGENT_BY_CASEFOLD: dict[str, str] = {label.casefold(): label for label in AGENT_LABELS}


def normalize_label(value: object) -> str | None:
    """Prueft ein Label gegen das vollstaendige Vokabular (inkl. ``Unklar``).

    Fuer Eingaben von MENSCHEN: die Label-Korrektur im Cockpit und die
    Outlook-Kategorienliste. Toleriert ausschliesslich Gross-/Kleinschreibung und
    umgebende Leerzeichen -- also Tippvarianten derselben Zeichenkette, keine
    Bedeutungsvarianten.

    Fuer Ausgaben des MODELLS ist ``normalize_agent_label`` zustaendig.
    """
    if not isinstance(value, str):
        return None
    return _BY_CASEFOLD.get(value.strip().casefold())


def normalize_agent_label(value: object) -> str | None:
    """Prueft ein LLM-Label gegen ``AGENT_LABELS`` -- ``Unklar`` gilt als ungueltig.

    Gibt ``None`` zurueck, wenn das Label nicht waehlbar ist; der Aufrufer
    entscheidet dann fail-closed (``FALLBACK_LABEL`` + ``needs_review``). Ein
    vom Modell geliefertes ``Unklar`` laeuft damit in denselben Pfad wie ein
    erfundenes Label: die Mail wird zur Sichtung markiert, statt mit einer
    Pseudo-Kategorie als erledigt zu gelten.
    """
    if not isinstance(value, str):
        return None
    return _AGENT_BY_CASEFOLD.get(value.strip().casefold())


def folder_for_label(label: str | None) -> str | None:
    """Zielordner eines Labels, oder ``None`` wenn die Mail in der Inbox bleibt."""
    if not label:
        return None
    return LABEL_FOLDERS.get(label)


def move_target(
    label: str | None,
    triage_class: str | None,
    *,
    needs_review: bool = False,
    known_correspondent: bool | None = None,
) -> str | None:
    """Entscheidet, ob eine LLM-klassifizierte Mail verschoben wird -- und wohin.

    Gibt den Zielordner zurueck oder ``None`` (Mail bleibt ungelesen in der Inbox).
    Drei Bedingungen muessen zusammen erfuellt sein, plus eine Bremse:

    1. Das Label hat ueberhaupt einen Zielordner (``LABEL_FOLDERS``).
    2. ``triage_class == 'fyi'`` -- Handlungsbedarf ist damit bereits modelliert.
       Alles, wofuer eine Aufgabe oder ein Entwurf entstand, bleibt sichtbar.
    3. ``known_correspondent is False`` -- Anthony hat dieser Adresse nie selbst
       geschrieben. Wem er schreibt, dem raeumt das System nicht die Post weg.

    Bedingung 3 ist die strukturelle Fassung einer Regel, die vorher nur im Skill
    stand ("Hat ein Mensch die Mail geschrieben, ist sie NIE System"). Gemessen half
    die Formulierung, garantierte aber nichts: in 25 Tagen wurden 49 Mails
    namentlicher Absender als ``System`` + ``fyi`` eingeordnet und damit aus der
    Inbox geraeumt, darunter "Projekt NITL -- Bitte um Rueckmeldung" und zweimal
    "AW: DRINGEND: PRDAI01 -- Archiv-Mount". Ein Verbot im Prompt ist eine Bitte;
    hier entscheidet das Backend.

    Der Nachweis ist ein Fakt aus dem Postfach (``sentitems``), kein Muster: keine
    Adresslisten, keine Betreff-Praefixe, keine Sprache, kein Anbieter. Er pflegt
    sich selbst -- ein neuer Kunde ist geschuetzt, sobald Anthony ihm einmal
    geantwortet hat. Gemessen an den echten Faellen trennt er vollstaendig: alle
    zehn geprueften Schadensfaelle sind Adressen mit eigener gesendeter Post, alle
    neun geprueften Maschinenabsender ohne.

    Zwei Alternativen sind an den Daten gescheitert und gehoeren nicht zurueck:
    Adress- und Betreffmuster (sprach- und anbieterabhaengig, wachsen endlos) sowie
    die publizierten Bulk-Header ``List-Id``/``List-Unsubscribe``/``Precedence``
    nach RFC 2919/2369/2076 -- letztere klingen sauber, lagen aber nur bei 4 von 10
    System-Mails an und fehlen bei echter Maschinenpost wie
    ``payments-noreply@google.com``.

    Bewusst OHNE Label-Sonderfall: auch ``Junk`` haengt am selben Nachweis. Eine
    unaufgeforderte Verkaufsanfrage kommt von einer Adresse, an die Anthony nie
    geschrieben hat, und wird darum weiterhin einsortiert -- Phishing, das die
    Adresse eines echten Kontakts faelscht, bleibt dagegen sichtbar.

    ``needs_review`` ist die Bremse und traegt die gesamte Unsicherheit des Laufs:
    verworfenes Label, Confidence unter der Schwelle, fehlende oder nicht
    numerische Confidence. Was das System nicht verstanden hat, raeumt es nicht weg.

    Eine dritte Bedingung ``inferenceClassification == 'other'`` gab es hier bis
    August 2026, eingefuehrt gegen Fehlmoves echter Korrespondenz. Sie wirkte, aber
    aus dem falschen Grund und viel zu breit: Outlooks Fokus-Heuristik stuft in
    diesem Postfach 1126 von 1426 Mails als ``focused`` ein, darunter
    LinkedIn-Einladungen, Synology-Meldungen und Leadinfo-Reports. In 30 Tagen
    blieben dadurch 99 ``System``- und 12 ``Newsletter``-Mails in der Inbox liegen,
    waehrend nur 46 bzw. 17 verschoben wurden -- das Gate filterte die Mehrheit
    statt der Ausnahmen. Die Fehlmoves, gegen die es gedacht war, waren in Wahrheit
    Label-Fehler des Modells (eine Kundenmail als ``System``); die gehoeren in die
    Klassifikation und in ``needs_review``, nicht in ein Herkunftssignal von
    Exchange. NICHT wieder einfuehren.

    Wiederkehrendes Systemrauschen laeuft NICHT hierueber, sondern ueber
    deterministische Absender-Regeln (``learned_rules``), die vor dem LLM greifen
    und ihren Zielordner selbst tragen. Diese Funktion ist die Rueckfallebene fuer
    alles, was das LLM klassifiziert hat.
    """
    if needs_review:
        return None
    if triage_class != "fyi":
        return None
    target = folder_for_label(label)
    if target is None:
        return None
    # ``None`` heisst "nicht ermittelt" und wird wie "ist ein Kontakt" behandelt:
    # ein fehlgeschlagener Nachweis darf keinen Move freigeben. Betrifft Replays mit
    # lueckenhaften Metadaten und Ausfaelle der Graph-Suche.
    if known_correspondent is not False:
        return None
    return target


def move_suppressed_reason(
    label: str | None,
    triage_class: str | None,
    *,
    needs_review: bool = False,
    known_correspondent: bool | None = None,
) -> str | None:
    """Warum wurde ein an sich verschiebbares Label NICHT verschoben?

    Nur fuer Log und Cockpit: macht die Entscheidung von ``move_target``
    nachvollziehbar, statt sie im Nichts verlaufen zu lassen. Gibt ``None``
    zurueck, wenn gar kein Move zur Debatte stand oder er stattgefunden hat.
    """
    if folder_for_label(label) is None or triage_class != "fyi" or needs_review:
        return None
    if known_correspondent is True:
        return "eigene Korrespondenz mit dieser Adresse"
    if known_correspondent is None:
        return "Korrespondenz nicht pruefbar"
    return None
