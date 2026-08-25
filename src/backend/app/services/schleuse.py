"""Die Schleuse dieses Hauses -- eine Stelle, an der Text nach draussen geht.

Die Mechanik steht in :mod:`ai9.schleuse`, die Anonymisierungsstrecke in
:mod:`app.services.anon_politik`. Hier wird beides zusammengesteckt und die
Politik dieses Deployments festgelegt. Mehr ist es nicht -- und mehr soll es
nicht sein, denn jede Zeile Entscheidung, die hier statt im Core steht, ist eine
Zeile, die der naechste Verbraucher nicht erbt.

**Warum es dieses Modul gibt:** Bis zum 25.08.2026 sass die Barriere in
``hermes_worker._schleuse_nach_draussen`` und schuetzte damit genau die Jobs, die
durch den Worker liefen. Der Chat lief daran vorbei -- wer dort ein Cloud-Modell
waehlte, schickte Prompt, Verlauf und angeheftete Dokumente im Klartext hinaus.
Nicht aus Nachlaessigkeit, sondern weil eine Barriere, die in einem Modul wohnt,
nur dessen Aufrufer kennt.
"""

from __future__ import annotations

import logging

from ai9 import schleuse as core
from ai9.schleuse import Durchlass

from app.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["Durchlass", "bilde_zurueck", "ist_lokal", "pruefe_ausgang", "politik"]


class _Maskierer:
    """Bindet die Hauspolitik (``anon_politik``) an das Core-Protokoll."""

    async def maskiere(self, text: str) -> tuple[str, str, list, list[str]]:
        from app.services import anon_politik

        return await anon_politik.maskiere(text)

    async def bilde_zurueck(self, text: str, sitzung: str) -> tuple[str, list[str]]:
        from app.services import anon_politik

        return await anon_politik.bilde_zurueck(text, sitzung)


_MASKIERER = _Maskierer()

_LOKALE_NAMEN = frozenset({"hermes", "nanobot"})
"""Historische Platzhalter fuer «das eingestellte lokale Modell».

Sie stehen in Job-Metadaten und Chat-Anfragen und meinen dasselbe wie ein leeres
Feld. Ohne sie hier gaelten sie als fremde Modelle, und jede Frage liefe durch
eine Maskierung, die niemand braucht.
"""


def politik() -> core.Politik:
    """Was in dieser Instanz erlaubt ist."""
    return core.Politik(cloud_erlaubt=True, lokale_modelle=_LOKALE_NAMEN)


def ist_lokal(modell: str) -> bool:
    """Ob dieses Modell auf der eigenen Maschine rechnet."""
    return core.ist_lokal(modell, politik())


def _rueckfall() -> str:
    """Das lokale Modell, auf das zurueckgefallen wird -- benannt, nicht geraten."""
    return get_settings().triage_model


async def pruefe_ausgang(
    *,
    text: str,
    modell: str,
    bei_restbestaenden: str = "abbrechen",
) -> Durchlass:
    """Entscheidet, ob dieser Text an dieses Modell gehen darf.

    ``bei_restbestaenden`` waehlt die Konsequenz eines Fundes, und die Wahl haengt
    an einer Frage: sieht ein Mensch den Text, bevor er hinausgeht?

    - ``"abbrechen"`` fuer unbeaufsichtigte Wege (Worker-Jobs, E-Mail-Entwuerfe).
    - ``"melden"`` fuer den Chat, wo jemand davor sitzt. Dort waere ein stiller
      Rueckfall die falsche Freundlichkeit -- er tauscht das gewaehlte Modell
      hinter dem Ruecken des Menschen.
    """
    return await core.pruefe(
        text=text,
        modell=modell,
        rueckfall=_rueckfall(),
        politik=politik(),
        maskierer=_MASKIERER,
        bei_restbestaenden=bei_restbestaenden,  # type: ignore[arg-type]
    )


async def bilde_zurueck(text: str, durchlass: Durchlass) -> tuple[str, list[str]]:
    """Setzt die echten Werte in eine auswaertige Antwort zurueck.

    Bei einem lokalen Lauf ein Durchreichen ohne Arbeit -- der Aufrufer muss also
    nicht selbst unterscheiden, und genau das verhindert den vergessenen Fall.
    """
    return await core.bilde_zurueck(text, durchlass, _MASKIERER)
