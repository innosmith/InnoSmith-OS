"""Der Schreib-Auftrag fuer Antwort-Entwuerfe -- eine einzige Quelle.

Bewusst ohne schwere Importe (nur Standardbibliothek), damit sowohl das Backend
(``hermes_worker._build_draft_prompt``) als auch das Offline-Eval
(``scripts/eval/run_llm_eval.py``) denselben Text verwenden koennen.

Hintergrund: Das Eval pflegte jahrelang eine eigene, kuerzere Fassung des
Schreib-Auftrags. Es mass damit ein System, das so nie in Produktion lief -- und
konnte deshalb Qualitaetsprobleme des echten Pfads grundsaetzlich nicht finden.
Wer diesen Text aendert, aendert beide Seiten gleichzeitig.
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
   wenn du wirklich zusätzliche Details brauchst.
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
