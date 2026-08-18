"""Der Schreib-Auftrag fuer Antwort-Entwuerfe -- eine einzige Quelle.

Bewusst ohne schwere Importe (nur Standardbibliothek), damit sowohl das Backend
(``hermes_worker._build_draft_prompt``) als auch das Offline-Eval
(``scripts/eval/run_llm_eval.py``) denselben Text verwenden koennen.

Hintergrund: Das Eval pflegte jahrelang eine eigene, kuerzere Fassung des
Schreib-Auftrags. Es mass damit ein System, das so nie in Produktion lief -- und
konnte deshalb Qualitaetsprobleme des echten Pfads grundsaetzlich nicht finden.
Wer diesen Text aendert, aendert beide Seiten gleichzeitig.
"""

GATHER_TASK_TEMPLATE = """## AUFGABE: FACHKONTEXT ZUSAMMENTRAGEN (noch NICHT schreiben)

Zu dieser E-Mail soll gleich ein Antwort-Entwurf entstehen. Deine einzige Aufgabe
jetzt: das Sachwissen beschaffen, das eine inhaltlich tragfähige Antwort braucht.
Formuliere KEINE Antwort, erstelle KEINEN Entwurf, verschiebe nichts.

**Heute:** {today} (Europe/Zurich)
**Betreff:** {subject}
**Von:** {from_name} <{from_addr}>

**E-MAIL-INHALT:**
{body_block}
{briefing_block}
### Wo welches Wissen liegt
Fakten, die sich fortlaufend ändern, holst du aus dem **Fachsystem** -- nicht aus
älteren E-Mails. Ein Mailfund dazu ist ein Hinweis mit Datum, keine Tatsache:

- Kapazität, Auslastung, geplante und erfasste Stunden → **get_capacity_overview**
- Ferien, Feiertage, Krankheit, «ab wann wieder erreichbar» → **get_absences**
- Freie Termine und Kalender → **find_free_slots**, **list_calendar_events**
{extra_systems}- Projektstand, frühere Zusagen, Dokumente, Mailverlauf → **semantic_search_documents**

### Vorgehen
1. Überlege, welches Sachwissen für eine gute Antwort fehlt: Projektstand, frühere
   Zusagen, Zahlen, Termine, Vorgehensweisen, offene Punkte. Betrifft die Mail
   Stunden, Kapazität, Verfügbarkeit oder Abwesenheit, rufe **zuerst** das
   zuständige Fachsystem oben auf.
2. Suche mit **semantic_search_documents** danach. Nutze **mehrere schmale Abfragen**
   statt einer überladenen -- die Stichwortsuche verknüpft alle Begriffe mit UND.
   Bewährte Bausteine: Name des Absenders, Firmenname ohne Domain-Endung
   («{sender_org}»), Projekt-/Produktname, konkretes Fachstichwort aus der Mail.
   Beispiel: erst «{sender_org} {topic_hint}», dann «{topic_hint}» allein.
3. Reicht ein Treffer nicht, formuliere die Abfrage **neu** und suche erneut.
   Wiederhole nie eine Abfrage, die du schon gestellt hast -- sie liefert dasselbe
   Ergebnis. **Höchstens {max_rounds} Suchvorgänge**; danach arbeitest du mit dem
   Gefundenen und gibst das Dossier aus.
{extra_tools}4. Findest du nichts Verwertbares, ist das ein gültiges Ergebnis. Erfinde nichts.

### Ergebnis
Gib ein knappes Dossier in Markdown aus -- nur belegte Fakten, keine Höflichkeiten,
keine Antwortformulierungen:

**Sachstand:** Was ist zum Thema bekannt (Projektstand, Zahlen, Entscheide)?
**Frühere Zusagen:** Was wurde diesem Kontakt gegenüber bereits zugesagt oder geklärt?
**Offene Punkte:** Was ist ungeklärt und gehört in die Antwort?
**Stand womöglich veraltet:** Zeitraumbezogene Angaben (Budget, Kapazität, Termine,
Verfügbarkeit) aus Quellen, die älter sind als der laufende Monat -- mit ihrem Datum
und dem Zeitraum, auf den sie sich bezogen.
**Nicht gefunden:** Wonach du gesucht hast, ohne Treffer.

Jede Aussage mit Quelle **und Datum** in Klammern -- die Suchtreffer liefern das Feld
`date`. Ohne Datum ist eine zeitraumbezogene Angabe nicht verwertbar. Was du nicht
belegen kannst, lässt du weg.
"""


DOSSIER_TEMPLATE = """
---

## FACHKONTEXT AUS DER RECHERCHE -- BELEGT, NUTZE IHN

Das Folgende wurde eigens für diese Antwort recherchiert und ist belegt. **Greife
die konkreten Punkte auf** -- Projektstand, Termine, Zahlen, frühere Zusagen,
offene Fragen. Eine Antwort, die den bekannten Sachstand ignoriert und stattdessen
allgemein bleibt («melde mich später», «schauen wir dann»), ist der häufigste und
teuerste Fehler: der Empfänger hat den Kontext, du auch -- also zeig es.

{dossier}

Drei Leitplanken:

1. **Konkret werden.** Wo oben ein Termin, eine Zahl oder ein Entscheid steht,
   gehört er in die Antwort. Ein Abschnitt «nicht gefunden» heisst nur: dazu
   schweigst du oder fragst nach -- er ist kein Grund, auch das Bekannte wegzulassen.
2. **Nichts erfinden.** Was oben nicht steht, schreibst du nicht als Tatsache.
   Übernimm nichts wörtlich und zitiere keine Quellenangaben -- der Empfänger sieht
   nur deine Antwort, nicht das Dossier.
3. **Andere Kunden bleiben ungenannt.** Die Recherche durchsucht das ganze Archiv,
   also auch Material aus anderen Mandaten. Erfahrung daraus darfst du
   verallgemeinert einbringen («das habe ich in vergleichbaren Projekten schon
   gemacht»), nie mit Namen, Zahlen oder Details eines Dritten.
4. **Alte Zahlen bleiben alt.** Was unter «Stand womöglich veraltet» steht oder ein
   Datum aus einem vergangenen Zeitraum trägt, nennst du nur mit diesem Bezug
   («im Juli standen dafür noch 14h») -- nie als heutigen Stand. Und du sagst nie
   zu, welches Budget noch abrufbar ist: Stundenzahlen aus der Planung sind kein
   Vertragskontingent. Im Zweifel fragst du nach, statt zuzusagen.
"""


NO_CONTEXT_HINT = """
---

## FACHKONTEXT: NICHTS GEFUNDEN

Die Recherche fand kein belegtes Sachwissen zu diesem Thema. Formuliere entsprechend
zurückhaltend: keine erfundenen Projektstände, Zahlen oder Zusagen. Wenn die Anfrage
inhaltliche Substanz verlangt, die du nicht hast, ist eine kurze, ehrliche Antwort
(Rückfrage oder Zwischenbescheid) besser als eine erfundene.
"""


DRAFT_TASK_TEMPLATE = """## AUFGABE: ANTWORT-ENTWURF SCHREIBEN

Diese E-Mail wurde als **auto_reply** eingestuft. Schreibe jetzt den bestmöglichen
Antwort-Entwurf im persönlichen Stil von Anthony Smith. Klassifiziere NICHT neu,
verschiebe nichts, erstelle keinen Task -- schreibe nur den Entwurf.

**Heute:** {today} (Europe/Zurich)
**E-Mail Message-ID:** {email_id}
**Betreff:** {subject}
**Von:** {from_name} <{from_addr}>

**E-MAIL-INHALT (vollständig, bereinigt -- darauf beziehst du dich):**
{body_block}

### Vorgehen
1. Der vollständige E-Mail-Inhalt steht oben. Rufe get_email("{email_id}") nur auf,
   wenn du wirklich zusätzliche Details brauchst -- dann mit **genau dieser ID**,
   Zeichen für Zeichen kopiert. Die Thread-ID gehört ausschliesslich zu get_thread:
   aus zwei IDs eine dritte zu mischen ergibt eine ID, die auf nichts zeigt.
{thread_load}2. Nutze die Stil-Anker oben («SO SCHREIBT ANTHONY») und -- für diesen konkreten
   Kontakt -- **search_my_replies("{from_addr}")** als Ton-/Register-Kalibrierung
   (Anrede, Länge, Schlussformel). Orientiere dich daran, **kopiere aber nicht
   wörtlich** -- schreibe passend zum aktuellen Inhalt neu.
3. **Spiegle das Register** des Absenders (Du/Sie und Grussform, siehe Schreibstil).
   Schreibt er «Hallo Anthony», antworte «Hallo [Vorname]», nicht «Lieber/Liebe».
{calendar_step}4. Formuliere natürlich und flüssig, halte dich an den Self-Review im email-style-Skill.
5. **{draft_tool}** mit **reply_to_id="{email_id}"** (Antwort im selben Thread,
   NIE ein neuer Thread). Empfänger NICHT manuell überschreiben -- To + CC der
   Diskussion werden automatisch übernommen. Das Backend erzwingt die Thread-
   Zugehörigkeit ohnehin deterministisch.

Der Entwurf braucht zwingend einen Inhaltsteil zwischen Anrede und Schlussformel.
Eine blosse Grussformel ist kein Entwurf.

Gib nach dem Aufruf eine kurze Bestätigung aus (kein JSON nötig).
"""


def render_gather_task(
    *,
    today: str,
    subject: str,
    from_name: str,
    from_addr: str,
    body_block: str,
    briefing_block: str = "",
    sender_org: str = "",
    topic_hint: str = "",
    max_rounds: int = 5,
    extra_tools: str = "",
    extra_systems: str = "",
) -> str:
    """Rendert den Sammel-Auftrag (Pass 2a). Rein und damit ohne Seiteneffekte testbar."""
    return GATHER_TASK_TEMPLATE.format(
        today=today,
        subject=subject,
        from_name=from_name,
        from_addr=from_addr,
        body_block=body_block,
        briefing_block=briefing_block,
        sender_org=sender_org or from_addr.split("@")[-1].split(".")[0],
        topic_hint=topic_hint or subject,
        max_rounds=max_rounds,
        extra_tools=extra_tools,
        extra_systems=extra_systems,
    )


def render_dossier_block(dossier: str) -> str:
    """Bettet das Rechercheergebnis in den Schreib-Prompt ein.

    Leeres oder ergebnisloses Dossier fuehrt zum Zurueckhaltungs-Hinweis statt zu
    einem leeren Abschnitt: ohne belegten Kontext soll der Schreib-Pass nichts
    erfinden, sondern kurz und ehrlich antworten.
    """
    text = (dossier or "").strip()
    if not text:
        return NO_CONTEXT_HINT
    return DOSSIER_TEMPLATE.format(dossier=text)


def render_draft_task(
    *,
    today: str,
    email_id: str,
    subject: str,
    from_name: str,
    from_addr: str,
    body_block: str,
    thread_load: str = "",
    calendar_step: str = "",
    draft_tool: str = "create_draft",
) -> str:
    """Rendert den Schreib-Auftrag. Rein und damit ohne Seiteneffekte testbar."""
    return DRAFT_TASK_TEMPLATE.format(
        today=today,
        email_id=email_id,
        subject=subject,
        from_name=from_name,
        from_addr=from_addr,
        body_block=body_block,
        thread_load=thread_load,
        calendar_step=calendar_step,
        draft_tool=draft_tool,
    )
