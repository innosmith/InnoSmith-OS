"""Vertrag zwischen Bare-Metal-Imports und dem Backend-Image.

Vorfall (03.08.2026): Mit dem Kapazitäts-Feature kam ``src/capacity/`` als Shared
Library dazu. ``COPY src/capacity/ /app/capacity/`` wurde im Dockerfile ergänzt, der
``PYTHONPATH`` nicht. Bare-Metal lief alles, weil die Router ihr Verzeichnis selbst
per ``sys.path.insert`` nachziehen -- im Container zeigt derselbe Pfad aber auf ``/``
statt auf ``src/``. Der Fehler fiel erst beim Prod-Start auf:
``ModuleNotFoundError: No module named 'capacity_report'``.

Der Test spiegelt die Regel: Wer sich bare-metal ein Shared-Lib-Verzeichnis auf den
``sys.path`` legt, braucht es im Image auf dem ``PYTHONPATH``.
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_APP = _REPO / "src" / "backend" / "app"
_DOCKERFILE = _REPO / "docker" / "Dockerfile.backend"

# Beide im Code üblichen Schreibweisen für «drei Ebenen hoch nach src/».
_SHARED_LIB_RE = re.compile(
    r'(?:parents\[3\]|_SRC)\s*/\s*"([\w-]+)"'
    r'|"\.\."\s*,\s*"\.\."\s*,\s*"\.\."\s*,\s*"([\w-]+)"'
)


def _shared_libs_used_by_backend() -> set[str]:
    names: set[str] = set()
    for py in _APP.rglob("*.py"):
        for match in _SHARED_LIB_RE.finditer(py.read_text(encoding="utf-8")):
            names.add(match.group(1) or match.group(2))
    return names


def _pythonpath_entries() -> list[str]:
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ENV PYTHONPATH="):
            return line.split("=", 1)[1].split(":")
    pytest.fail("Kein ENV PYTHONPATH in docker/Dockerfile.backend gefunden")


def test_shared_libs_are_detected():
    """Schutz vor einem stillschweigend leeren Test, falls sich das Muster ändert."""
    libs = _shared_libs_used_by_backend()
    assert {"email-graph", "toggl", "capacity"} <= libs


def test_every_shared_lib_is_copied_into_the_image():
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    for lib in sorted(_shared_libs_used_by_backend()):
        assert f"COPY src/{lib}/ /app/{lib}/" in dockerfile, (
            f"src/{lib}/ wird vom Backend importiert, aber nicht ins Image kopiert"
        )


def test_every_shared_lib_is_on_the_image_pythonpath():
    entries = _pythonpath_entries()
    for lib in sorted(_shared_libs_used_by_backend()):
        assert f"/app/{lib}" in entries, (
            f"/app/{lib} fehlt im PYTHONPATH von docker/Dockerfile.backend -- "
            f"im Container greift der sys.path-Fallback der Router nicht"
        )
