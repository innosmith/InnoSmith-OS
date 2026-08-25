"""Der Core, gegen den entwickelt wird, und der, der ins Abbild kommt.

Entwickelt wird gegen die Arbeitskopie von AI9 (Editable-Install), gebaut wird
gegen ``AI9_REF`` in ``docker/build.sh``. Laufen die auseinander, laeuft im
Container ein anderer Core als hier -- und das faellt sonst erst dort auf.

Der Anlass ist ein konkreter Fund vom 25.08.2026: Das Vendor-Verzeichnis stand
auf 0.4.4, waehrend das Backend bereits ``ai9.modelle`` aus 0.5.0 importierte.
Ein Abbild aus diesem Stand waere beim ersten Import gescheitert. Bis dahin
kopierte das Bauskript ungeprueft, was gerade dalag.
"""

import re
from pathlib import Path

import ai9


def test_core_entspricht_dem_geforderten_schild():
    """Eine feste Zahl im Test waere nur Buchhaltung. Interessant ist die Luecke."""
    bauskript = Path(__file__).resolve().parents[3] / "docker" / "build.sh"
    treffer = re.search(r'AI9_REF="\$\{AI9_REF:-v([^}"]+)\}"', bauskript.read_text())

    assert treffer, "AI9_REF nicht in docker/build.sh gefunden"
    assert ai9.__version__ == treffer.group(1)
