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
    """Sehr einfache HTML->Text-Konvertierung fuer den Stil-Diff."""
    if not html:
        return ""
    txt = re.sub(r"(?i)<br\s*/?>", "\n", html)
    txt = re.sub(r"(?i)</p>", "\n", txt)
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
