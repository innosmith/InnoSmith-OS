"""Reine Text-Analyse fuer E-Mail-Entwuerfe -- ohne DB, ohne Framework.

Hier liegen die Muster und Funktionen, die aus einem E-Mail-Text Anrede, Register
und Schlussformel ableiten. Bewusst nur Standardbibliothek, damit auch das
Offline-Eval (``scripts/eval/run_llm_eval.py``) exakt dieselben Regeln anwendet,
ohne den ganzen Backend-Stack zu laden.

``app.services.learning`` re-exportiert alles hier Definierte; bestehende Importe
bleiben deshalb gueltig. Wer neue Text-Heuristiken braucht, ergaenzt sie hier --
eine zweite Regelquelle waere genau die Art von Duplikat, die stillschweigend
auseinanderlaeuft.
"""

from __future__ import annotations

import re

# Marker, ab denen der zitierte Original-Thread beginnt -- alles danach wird beim
# Diff ignoriert, damit nur die echte inhaltliche/stilistische Aenderung zaehlt.
_QUOTE_MARKERS = [
    r"\nvon:\s",
    r"\nfrom:\s",
    r"\ngesendet:\s",
    r"\nsent:\s",
    r"\nam\s.+\sschrieb",
    r"\non\s.+\swrote",
    r"\n-{3,}\s*urspr",
    r"\n_{5,}",
]

# Anrede-Marker: formell -> Sie, informell -> eher Du (feiner via Pronomen bestaetigt).
_FORMAL_GREETING = re.compile(
    r"^(sehr geehrte[rs]?|guten (?:tag|morgen|abend)|gr[üu]ezi|dear)\b", re.I
)
_INFORMAL_GREETING = re.compile(
    r"^(hallo|hoi|hi|hey|liebe[rs]?|salut|servus|hoi zäme|hoi zäme)\b", re.I
)
_ANY_GREETING = re.compile(
    r"^(sehr geehrte[rs]?|guten (?:tag|morgen|abend)|gr[üu]ezi|dear|hallo|hoi|hi|"
    r"hey|liebe[rs]?|salut|servus)\b",
    re.I,
)
# Schlussformeln (Phrase, ohne Namenszeile).
_CLOSING = re.compile(
    r"^(lg\b|liebe gr[üu]sse|freundliche gr[üu]sse|beste gr[üu]sse|herzliche gr[üu]sse|"
    r"viele gr[üu]sse|sonnige gr[üu]sse|gr[üu]sse\b|gruss\b|mit freundlichen gr[üu]ssen|"
    r"besten dank und gr[üu]sse|best regards|kind regards|warm regards|cheers|"
    r"thanks and regards|thank you)\b",
    re.I,
)


def html_to_text(html: str | None) -> str:
    """Sehr einfache HTML->Text-Konvertierung fuer den Stil-Diff.

    Block-Elemente werden zu Zeilenumbruechen. Ohne das kleben Listeneintraege
    aneinander (``…innosmith.cloud</li><li>A-Record…`` -> ``cloudA-Record``) und
    eine anschliessende Wortgrenzen-Pruefung findet die Angabe nicht mehr.
    """
    if not html:
        return ""
    txt = re.sub(r"(?i)<br\s*/?>", "\n", html)
    txt = re.sub(
        r"(?i)</?(?:p|li|ul|ol|div|tr|table|h[1-6]|blockquote)\s*/?>", "\n", txt
    )
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = (
        txt.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def strip_quoted_history(text_body: str) -> str:
    """Entfernt den zitierten Original-Thread (Reply-Anhang) fuer einen sauberen Diff."""
    lowered = text_body.lower()
    cut = len(text_body)
    for marker in _QUOTE_MARKERS:
        m = re.search(marker, lowered)
        if m and m.start() < cut:
            cut = m.start()
    return text_body[:cut].strip()


def _body_lines(text_body: str) -> list[str]:
    """Nicht-leere Zeilen des bereinigten Textes (HTML entfernt, Zitat abgeschnitten)."""
    plain = html_to_text(text_body)
    clean = strip_quoted_history(plain) or plain
    return [ln.strip() for ln in clean.splitlines() if ln.strip()]


def extract_salutation_signature(text_body: str) -> dict:
    """Leitet Anrede, Register (Du/Sie) und Schlussformel aus einer echten Antwort ab.

    Rein und damit testbar. Nutzt bewusst nur robuste Muster (Zeilenanfang), damit
    keine Halluzination entsteht -- fehlt ein Signal, bleibt der Schluessel aussen vor.
    Returns z. B. ``{"greeting": "Hallo Peter", "register": "du", "closing": "LG"}``.
    """
    plain = html_to_text(text_body)
    clean = strip_quoted_history(plain) or plain
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    result: dict[str, str] = {}
    if not lines:
        return result

    # Anrede: erste passende Zeile in den ersten drei Zeilen.
    greeting_line = None
    for ln in lines[:3]:
        if _ANY_GREETING.match(ln):
            greeting_line = ln.rstrip(",").strip()
            break
    if greeting_line:
        result["greeting"] = greeting_line[:80]

    # Register: primaer aus der Anrede, sonst aus Pronomen-Haeufigkeit.
    lowered = clean.lower()
    if greeting_line and _FORMAL_GREETING.match(greeting_line):
        result["register"] = "sie"
    elif (
        greeting_line
        and _INFORMAL_GREETING.match(greeting_line)
        and not greeting_line.lower().startswith(("liebe", "lieber"))
    ):
        result["register"] = "du"
    else:
        du = len(re.findall(r"\b(du|dich|dir|dein[e]?[nmrs]?)\b", lowered))
        sie = len(re.findall(r"\b(ihnen|ihre[nmrs]?)\b", lowered))
        if du or sie:
            result["register"] = "du" if du >= sie else "sie"

    # Schlussformel: letzte passende Zeile in den letzten sechs Zeilen.
    for ln in reversed(lines[-6:]):
        if _CLOSING.match(ln):
            result["closing"] = ln.rstrip(",").strip()[:60]
            break
    return result


# ── Faktenbindung ────────────────────────────────────────────
#
# Ein Entwurf darf nur Angaben enthalten, die in einer Quelle stehen. Am
# 04.08.2026 fuellte der Schreib-Pass den Platzhalter «[IP deiner Challenge-Box]»
# aus einer geloeschten Entwurfs-Quelle mit einer frei erfundenen IP-Adresse --
# und das Self-Grading meldete trotzdem 1.0, weil es nur Tool-Aufrufe zaehlt.
#
# Geprueft werden bewusst nur Angaben mit hohem Schaden und geringer
# Fehlalarm-Quote: Adressen, Hostnames, Mengen mit Einheit, Ports. Prosa-Zahlen
# («in zwei Wochen») bleiben aussen vor -- eine Pruefung, die dauernd Fehlalarme
# liefert, wird ignoriert und schuetzt dann gar nichts mehr.

_FACT_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# FQDN: mindestens ein Label plus TLD aus Buchstaben. Deckt auch technische Namen
# mit Unterstrich ab (``_acme-challenge.app1.gsw.ch``).
_FACT_HOST = re.compile(r"\b(?:[a-z0-9_][a-z0-9_-]*\.)+[a-z]{2,24}\b", re.I)
_FACT_QUANTITY = re.compile(
    r"\b(\d[\d'’.,]*)\s*(?:chf|eur|fr\.|%|h\b|std\.?|stunden|tage?n?|wochen?|"
    r"monate?n?|jahre?n?)",
    re.I,
)
_FACT_PORT = re.compile(r"\bports?\s+(\d{2,5})\b", re.I)
_EMAIL_ADDRESS = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
# Bekannte HTML-Tags -- namentlich, damit unbekannte spitze Klammern uebrig bleiben
# und als Platzhalter erkennbar sind.
_HTML_TAG = re.compile(
    r"</?(?:p|br|div|span|ul|ol|li|strong|b|em|i|u|a|table|tbody|thead|tfoot|tr|td|th"
    r"|h[1-6]|html|head|body|meta|link|style|font|hr|img|blockquote|pre|code|small"
    r"|sup|sub|o:p|v:[a-z]+|w:[a-z]+)\b[^>]*>",
    re.I,
)
# Unausgefuellte Platzhalter: ``[IP deiner Box]``, ``<challenge-zone>``, ``TODO``.
# Die spitze Variante verlangt zwei Wortteile und kein ``=`` -- sonst wuerde jedes
# HTML-Tag (``<strong>``, ``<a href=…>``) als Platzhalter gelten.
_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{2,60}\]"
    r"|<(?![^>]*=)[A-Za-zÄÖÜ][A-Za-z0-9]*(?:[ _-][A-Za-z0-9äöüÄÖÜ]+)+>"
    r"|\b(?:TODO|FIXME|XXX|TBD)\b"
)


def _normalize_for_match(text_body: str) -> str:
    """Vereinheitlicht Text fuer den Abgleich: klein, ohne Tausendertrennzeichen."""
    return re.sub(r"[’']", "", html_to_text(text_body).lower())


def _new_part(text_body: str) -> str:
    """Sichtbarer, NEU geschriebener Teil eines Entwurfs (ohne zitierten Thread).

    Geprueft wird nur, was der Schreib-Pass selbst formuliert hat. Der zitierte
    Original-Thread darunter stammt vom Gegenueber und braucht keinen Beleg.
    """
    plain = html_to_text(text_body)
    return strip_quoted_history(plain) or plain


def factual_tokens(text_body: str) -> list[str]:
    """Zieht ueberpruefbare Angaben aus einem Text (Adressen, Hostnames, Mengen, Ports).

    Rein und deterministisch. E-Mail-Adressen werden vorher entfernt, damit deren
    Domain-Teil nicht als Hostname gilt -- eine Signatur-Adresse ist kein Fakt, den
    der Entwurf belegen muesste. Rueckgabe kleingeschrieben und dedupliziert, in
    Fundreihenfolge.
    """
    plain = _EMAIL_ADDRESS.sub(" ", _new_part(text_body))
    found: list[str] = []
    for pattern in (_FACT_IPV4, _FACT_HOST):
        for match in pattern.finditer(plain):
            found.append(match.group(0))
    for pattern in (_FACT_QUANTITY, _FACT_PORT):
        for match in pattern.finditer(plain):
            found.append(match.group(1))

    out: list[str] = []
    seen: set[str] = set()
    for token in found:
        key = re.sub(r"[’']", "", token.lower()).rstrip(".,;:")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def ungrounded_values(draft_body: str, sources) -> list[str]:
    """Angaben des Entwurfs, die in keiner der Quellen vorkommen.

    ``sources`` ist eine Sammlung von Texten, die der Schreib-Pass tatsaechlich
    gesehen hat (Prompt, Dossier, Thread, Tool-Ergebnisse). Steht eine Adresse oder
    Menge in keinem davon, hat das Modell sie erfunden. Der Abgleich ist ein
    Substring-Test auf normalisiertem Text: bewusst nachsichtig, damit Formatierung
    (Tausendertrennzeichen, Gross-/Kleinschreibung) keinen Fehlalarm ausloest.
    """
    tokens = factual_tokens(draft_body)
    if not tokens:
        return []
    haystack = " \n".join(_normalize_for_match(s) for s in sources if s)
    if not haystack.strip():
        return list(tokens)
    return [t for t in tokens if t not in haystack]


def placeholder_markers(text_body: str) -> list[str]:
    """Unausgefuellte Platzhalter im Text (``[...]``, ``<...>``, ``TODO``).

    Ein Platzhalter im Entwurf ist immer ein Fehler: entweder geht er so an den
    Empfaenger, oder das Modell fuellt ihn beim naechsten Lauf mit einer Erfindung.
    Bekannte HTML-Tags werden namentlich entfernt statt per Wildcard -- sonst
    verschwindet ``<challenge-zone>`` mit ihnen.
    """
    plain = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", text_body or "")
    plain = _HTML_TAG.sub(" ", plain)
    plain = strip_quoted_history(plain) or plain
    out: list[str] = []
    seen: set[str] = set()
    for match in _PLACEHOLDER.finditer(plain):
        token = match.group(0).strip()
        if token.lower() in seen:
            continue
        seen.add(token.lower())
        out.append(token[:80])
    return out


def has_content_between_greeting_and_closing(text_body: str) -> bool:
    """Prueft strukturell, ob ein Antwort-Entwurf einen Inhaltsteil enthaelt.

    Eine echte Antwort besteht aus Anrede, Inhalt und Schluss. Fehlt der mittlere
    Teil, ist es ein Platzhalter -- genau das Muster des Vorfalls vom 03.08.2026,
    bei dem ein Entwurf nur aus «Liebe Grüsse» und «LG Anthony» bestand.

    Bewusst strukturell statt laengenbasiert: im Bestand ist die kuerzeste korrekte
    Antwort 205 Zeichen lang, der Platzhalter 196 -- jede Laengenschwelle verwirft
    das Falsche. Nutzt dieselben Muster wie ``extract_salutation_signature``.

    Rein und damit testbar. Die Funktion soll Platzhalter erkennen, nicht Entwuerfe
    inhaltlich bewerten.
    """
    lines = _body_lines(text_body)
    if not lines:
        return False

    # Fuehrende Anrede abschneiden.
    start = 1 if _ANY_GREETING.match(lines[0]) else 0
    # Ab der letzten Schlussformel alles abschneiden -- damit faellt auch eine
    # nachgestellte Namenszeile weg ("Freundliche Grüsse" / "Anthony Smith").
    end = len(lines)
    for i in range(len(lines) - 1, start - 1, -1):
        if _CLOSING.match(lines[i]):
            end = i
            break
    return bool(lines[start:end])
