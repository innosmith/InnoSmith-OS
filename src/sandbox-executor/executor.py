"""Docker-Run-Logik für die Code-Sandbox (Schicht 2).

Dieser Prozess läuft in einem eigenen, minimalen Container und ist der **einzige**
Ort im gesamten Stack, der Zugriff auf den Docker-Socket hat. Das grosse Backend
bekommt niemals `docker.sock`; es ruft diesen Executor über eine token-geschützte
HTTP-API (siehe ``app.py``) auf.

Sicherheitsprinzip: Der Image-Name und **alle** ``docker run``-Flags sind hier
hartkodiert. Ein Aufrufer kann also kein beliebiges ``docker run`` auslösen,
sondern nur das feste, gehärtete Sandbox-Image mit fixen Limits starten. Beliebiger
Code ist damit isoliert: kein Netz, non-root, read-only Root-FS, Ressourcenlimits,
Wegwerf-Container. Einziger Schreibpfad ist der Workspace unter ``$HOME``.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("sandbox_executor")

SANDBOX_IMAGE = os.environ.get("TP_SANDBOX_IMAGE", "taskpilot-sandbox:latest")

# WICHTIG (Snap-Docker auf der GX10): Bind-Mount-Quellpfade werden vom Host-Daemon
# aufgelöst und müssen unter $HOME liegen. Der Executor-Container mountet dieses
# Verzeichnis PFADGLEICH (Host-Pfad == Container-Pfad), damit die an ``docker run -v``
# übergebenen Pfade auf dem Host existieren (Docker-out-of-Docker Sibling-Pattern).
OUTPUT_BASE = Path(
    os.environ.get(
        "TP_SANDBOX_OUTPUT_DIR",
        str(Path.home() / ".local" / "share" / "taskpilot" / "sandbox-output"),
    )
)

# Datenraum: vom Backend im Takt geschriebene Tabellen der Fachsysteme (Parquet).
# Wird bei jedem Lauf als ``/daten`` schreibgeschützt eingehängt. Auch dieser Pfad
# muss pfadgleich sein (Host-Pfad == Container-Pfad), weil ihn der Host-Daemon
# auflöst -- dieselbe Snap-Docker-Bedingung wie bei OUTPUT_BASE.
DATENRAUM_DIR = Path(
    os.environ.get(
        "TP_DATENRAUM_DIR",
        str(Path.home() / ".local" / "share" / "taskpilot" / "datenraum"),
    )
)

# Optionales, striktes Seccomp-Profil. Leer = Docker-Standard-Seccomp-Profil greift
# weiterhin (multi-arch, bewährt). Das mitgelieferte Profil ist strenger, aber
# architektur-sensibel; daher opt-in per Env.
SECCOMP_PROFILE = os.environ.get("TP_SANDBOX_SECCOMP", "").strip()

MAX_CODE_LENGTH = 50_000
MAX_TIMEOUT_SECONDS = 900
# Obergrenze für den (persistenten) Workspace pro Scope, schützt $HOME vor Volllaufen.
MAX_WORKSPACE_BYTES = int(os.environ.get("TP_SANDBOX_MAX_WORKSPACE_BYTES", 512 * 1024 * 1024))
# Maximal gleichzeitige Sandbox-Ausführungen (verhindert Run-Flut / Ressourcen-Erschöpfung).
MAX_CONCURRENT_RUNS = int(os.environ.get("TP_SANDBOX_MAX_CONCURRENT", 4))
# Ephemere Run-/Scope-Verzeichnisse älter als das werden aufgeräumt.
RUN_TTL_SECONDS = int(os.environ.get("TP_SANDBOX_RUN_TTL", 3600))
# Persistente Konversations-Scopes: laenger, aber nicht unbefristet (Default 30 Tage).
CONV_TTL_SECONDS = int(os.environ.get("TP_SANDBOX_CONV_TTL", 30 * 24 * 3600))
# Obergrenzen fuer die zurueckgegebene Ausgabe. Was darueber liegt, faellt weg --
# aber nie stillschweigend, siehe ``gekuerzt``.
MAX_STDOUT_CHARS = int(os.environ.get("TP_SANDBOX_MAX_STDOUT", 20000))
MAX_STDERR_CHARS = int(os.environ.get("TP_SANDBOX_MAX_STDERR", 8000))
MEMORY_LIMIT = os.environ.get("TP_SANDBOX_MEMORY", "2g")


def gekuerzt(text: str, grenze: int) -> str:
    """Text auf die letzten ``grenze`` Zeichen kuerzen und die Kuerzung benennen.

    Die Ausgabe eines Laufs ist die Beweisgrundlage fuer jede Zahl, die daraus in
    eine Antwort wandert. Sie schweigend am Anfang abzuschneiden erzeugt genau die
    Fehlerart, gegen die dieser ganze Weg gebaut ist: ein Ergebnis, das vollstaendig
    aussieht und es nicht ist. Wer nur den Schwanz einer Rangliste sieht, haelt den
    groessten verbliebenen Posten fuer den groessten -- und niemand kann es ihm
    ansehen.

    Darum steht die fehlende Menge im Text. Sie ist eine Aufforderung, die Abfrage
    enger zu fassen, nicht ein Hinweis zum Weiterlesen.
    """
    if len(text) <= grenze:
        return text
    fehlend = len(text) - grenze
    return (
        f"[... {fehlend} Zeichen am Anfang abgeschnitten (Grenze: {grenze}). "
        f"Die Ausgabe ist unvollstaendig -- die Abfrage enger fassen, "
        f"aggregieren oder LIMIT setzen. ...]\n"
    ) + text[-grenze:]
CPU_LIMIT = os.environ.get("TP_SANDBOX_CPUS", "2")

_SCOPE_RE = re.compile(r"[^a-zA-Z0-9_-]")

_run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)

# --- Registrierte Scripts (Schicht 1) --------------------------------------
# Der Executor ist die einzige vertrauenswürdige Stelle: Er besitzt die Registry
# UND den Docker-Socket. Aufrufer senden nur script_id + params — nie ein Image
# oder Docker-Flags. So kann selbst ein kompromittierter MCP-/Backend-Prozess
# kein beliebiges Image starten.
SCRIPTS_REGISTRY_PATH = Path(os.environ.get("TP_SCRIPTS_REGISTRY", "/opt/executor/scripts.json"))
SECRETS_DIR = Path(os.environ.get("TP_SECRETS_DIR", "/run/secrets"))


def load_script_registry() -> dict:
    """Script-Registry laden (leer, falls nicht vorhanden)."""
    try:
        with open(SCRIPTS_REGISTRY_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Script-Registry nicht lesbar: %s", SCRIPTS_REGISTRY_PATH)
        return {"scripts": []}


def _get_secret(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    secret_file = SECRETS_DIR / name.lower()
    if secret_file.exists():
        try:
            return secret_file.read_text().strip()
        except OSError:
            return None
    return None


def _sanitize_scope(workspace_key: str | None, run_id: str) -> tuple[str, bool]:
    """Ermittelt den Scope-Ordnernamen. Persistent bei workspace_key, sonst ephemer.

    Rückgabe: (scope_name, persistent).
    """
    if workspace_key:
        clean = _SCOPE_RE.sub("_", workspace_key)[:64]
        if clean:
            return f"conv-{clean}", True
    return f"run-{run_id}", False


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _cleanup_stale() -> None:
    """Entfernt Run- und Scope-Verzeichnisse, die ihre Frist überschritten haben.

    Persistente ``conv-``-Scopes hatten bis zum 02.09.2026 **keine** Frist: sie
    entstanden bei jedem Gespräch mit ``workspace_key`` und blieben unbefristet
    liegen -- mitsamt allem, was der Agent dort an Auszügen aus Fachsystemen
    abgelegt hatte. Eine Aufbewahrung ohne Ende ist keine Aufbewahrung.
    """
    if not OUTPUT_BASE.exists():
        return
    ephemer_cutoff = time.time() - RUN_TTL_SECONDS
    conv_cutoff = time.time() - CONV_TTL_SECONDS
    for child in OUTPUT_BASE.iterdir():
        try:
            if not child.is_dir():
                continue
            if child.name.startswith(("run-", "script-")):
                cutoff = ephemer_cutoff
            elif child.name.startswith("conv-"):
                cutoff = conv_cutoff
            else:
                continue
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                logger.info("Scope nach Fristablauf entfernt: %s", child.name)
        except OSError:
            pass


def resolve_artifact(scope: str, name: str) -> Path | None:
    """Löst einen Artefaktpfad sicher auf (Path-Traversal-Schutz)."""
    # Scope darf nur die von uns vergebenen Zeichen enthalten.
    if not scope or _SCOPE_RE.sub("", scope) != scope:
        return None
    safe_name = Path(name).name
    if not safe_name or safe_name != name:
        return None
    base = (OUTPUT_BASE / scope / "workspace").resolve()
    target = (base / safe_name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


async def execute_in_sandbox(
    code: str,
    input_files: dict[str, str] | None = None,
    timeout_seconds: int = 300,
    stdin_data: str | None = None,
    workspace_key: str | None = None,
) -> dict:
    """Führt Python-Code in einem gehärteten, netzwerkisolierten Docker-Container aus.

    - ``stdin_data``: wird dem Programm als Standard-Eingabe zugeführt (für Skripte,
      die ``input()``/``sys.stdin`` lesen).
    - ``workspace_key``: bei gesetztem Schlüssel (z. B. Konversations-ID) wird ein
      persistenter Workspace wiederverwendet, sodass Dateien/Zustand über Iterationen
      erhalten bleiben.
    """
    run_id = str(uuid.uuid4())[:8]
    container_name = f"tp-sandbox-{run_id}"
    scope, persistent = _sanitize_scope(workspace_key, run_id)

    try:
        _cleanup_stale()
    except Exception:
        logger.debug("Cleanup übersprungen", exc_info=True)

    scope_dir = OUTPUT_BASE / scope
    workspace_dir = scope_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(workspace_dir, 0o777)

    # Code + Input-Dateien pro Run getrennt und read-only bereitstellen.
    run_dir = scope_dir / ".runs" / run_id
    code_dir = run_dir / "code"
    input_dir = run_dir / "input"
    code_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)

    (code_dir / "_main.py").write_text(code, encoding="utf-8")
    if input_files:
        for filename, content in input_files.items():
            safe_name = Path(filename).name
            (input_dir / safe_name).write_text(content, encoding="utf-8")

    docker_args = [
        "docker", "run",
        "--rm", "-i",
        "--name", container_name,
        "--network", "none",
        "--memory", MEMORY_LIMIT,
        "--cpus", CPU_LIMIT,
        "--pids-limit", "100",
        "--cap-drop", "ALL",
        "--read-only",
        "--tmpfs", "/tmp:size=256m",
        "-e", "HOME=/tmp",
        "-e", "MPLCONFIGDIR=/tmp",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{code_dir}:/code:ro",
        "-v", f"{input_dir}:/input:ro",
        "-v", f"{workspace_dir}:/workspace:rw",
        "-w", "/workspace",
    ]
    # Datenraum: die lokal materialisierten Tabellen der Fachsysteme, schreibgeschützt
    # und bei JEDEM Lauf. Bewusst ohne Bindung an ``workspace_key``: der Schlüssel
    # stammt beim Agenten aus den Argumenten des Modells, eine Abstimmung darüber
    # waere genau die Sorte Kopplung, die still bricht.
    if DATENRAUM_DIR.is_dir():
        docker_args += ["-v", f"{DATENRAUM_DIR}:/daten:ro"]
    # --no-new-privileges bleibt bewusst AUS: Kernel 6.17-nvidia (GX10) blockt
    # jede exec mit diesem Flag (siehe docker-compose.prod.yml).
    if SECCOMP_PROFILE and Path(SECCOMP_PROFILE).is_file():
        docker_args += ["--security-opt", f"seccomp={SECCOMP_PROFILE}"]
        logger.info("Seccomp-Profil aktiv: %s", SECCOMP_PROFILE)
    docker_args += ["--entrypoint", "python", SANDBOX_IMAGE, "/code/_main.py"]

    logger.info(
        "Sandbox-Ausführung: run_id=%s scope=%s persistent=%s timeout=%ds stdin=%dB",
        run_id, scope, persistent, timeout_seconds, len(stdin_data or ""),
    )

    start_time = asyncio.get_event_loop().time()
    stdin_bytes = (stdin_data or "").encode("utf-8")
    async with _run_semaphore:
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.error("Sandbox Timeout nach %ds (run_id=%s)", timeout_seconds, run_id)
            await _kill_container(container_name)
            _cleanup_run_dir(run_dir)
            return _err(
                f"Timeout: Code-Ausführung nach {timeout_seconds}s abgebrochen",
                run_id, scope, timeout_seconds,
            )
        except FileNotFoundError:
            logger.exception("docker-Binary nicht gefunden")
            _cleanup_run_dir(run_dir)
            return _err(
                "Docker nicht verfügbar (docker-CLI/Socket im Executor-Container fehlt)",
                run_id, scope, 0,
            )
        except Exception as e:
            logger.exception("Container-Fehler (run_id=%s)", run_id)
            _cleanup_run_dir(run_dir)
            return _err(f"Container-Fehler: {e}", run_id, scope, 0)

    duration = asyncio.get_event_loop().time() - start_time

    # Code-/Input-Verzeichnis dieses Runs wird nicht mehr gebraucht.
    _cleanup_run_dir(run_dir)

    generated_files = []
    workspace_bytes = 0
    if workspace_dir.exists():
        for f in sorted(workspace_dir.iterdir()):
            if f.is_file():
                size = f.stat().st_size
                workspace_bytes += size
                generated_files.append({"name": f.name, "size_bytes": size})

    warning = None
    if workspace_bytes > MAX_WORKSPACE_BYTES:
        warning = (
            f"Workspace über Limit ({workspace_bytes // (1024*1024)} MB > "
            f"{MAX_WORKSPACE_BYTES // (1024*1024)} MB) -- ältere Dateien ggf. aufräumen."
        )
        logger.warning("Workspace-Limit überschritten: scope=%s bytes=%d", scope, workspace_bytes)

    return {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": gekuerzt(stdout.decode("utf-8", errors="replace"), MAX_STDOUT_CHARS),
        "stderr": gekuerzt(stderr.decode("utf-8", errors="replace"), MAX_STDERR_CHARS) if stderr else "",
        "generated_files": generated_files,
        "scope": scope,
        "persistent": persistent,
        "run_id": run_id,
        "duration_seconds": round(duration, 2),
        "warning": warning,
    }


def _err(message: str, run_id: str, scope: str, duration: int) -> dict:
    return {
        "success": False,
        "error": message,
        "stdout": "",
        "stderr": "",
        "generated_files": [],
        "scope": scope,
        "run_id": run_id,
        "duration_seconds": duration,
        "warning": None,
    }


async def _kill_container(container_name: str) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "docker", "kill", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except Exception:
        pass


def _cleanup_run_dir(run_dir: Path) -> None:
    """Entfernt das ephemere Code-/Input-Verzeichnis dieses einen Runs."""
    try:
        shutil.rmtree(run_dir, ignore_errors=True)
    except Exception:
        pass


async def run_registered_script(
    script_id: str,
    params: dict | None = None,
    input_files: dict[str, str] | None = None,
) -> dict:
    """Führt ein in der Registry deklariertes Script in einem gehärteten Container aus.

    Bild, Ressourcenlimits, Netzwerk, Secrets und Timeout stammen AUSSCHLIESSLICH
    aus der Registry des Executors — der Aufrufer liefert nur script_id, params und
    optionale Eingabedateien.
    """
    params = params or {}
    registry = load_script_registry()
    script = next((s for s in registry.get("scripts", []) if s["id"] == script_id), None)
    run_id = str(uuid.uuid4())[:8]
    scope = f"script-{run_id}"

    if not script:
        available = [s["id"] for s in registry.get("scripts", [])]
        return _err(f"Script '{script_id}' nicht in Registry. Verfügbar: {available}", run_id, scope, 0)

    # Pflicht-Parameter prüfen (Defense-in-Depth; MCP prüft ebenfalls).
    for p_def in script.get("params", []):
        if p_def.get("required") and p_def["name"] not in params:
            return _err(f"Pflicht-Parameter '{p_def['name']}' fehlt ({p_def.get('description', '')})", run_id, scope, 0)

    security = script["security"]
    container_name = f"tp-script-{script_id}-{run_id}"

    try:
        _cleanup_stale()
    except Exception:
        logger.debug("Cleanup übersprungen", exc_info=True)

    scope_dir = OUTPUT_BASE / scope
    output_dir = scope_dir / "workspace"  # gleicher Artefakt-Pfad wie Sandbox (resolve_artifact)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o777)

    input_dir = scope_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    if input_files:
        for filename, content in input_files.items():
            safe_name = Path(filename).name
            (input_dir / safe_name).write_text(content, encoding="utf-8")

    docker_args = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--cap-drop", "ALL",
        "--read-only",
        "--tmpfs", "/tmp:size=256m",
        "--memory", f"{security.get('memory_limit_mb', 2048)}m",
        "--cpus", str(security.get("cpu_cores", 2)),
        "--pids-limit", "100",
        "-e", "HOME=/tmp",
        "-v", f"{output_dir}:/output:rw",
    ]
    if input_files:
        docker_args += ["-v", f"{input_dir}:/input:ro"]

    # Netzwerk: nur wenn die Registry Hosts erlaubt, sonst komplett isoliert.
    network_allow = security.get("network_allow", [])
    llm_allow = security.get("llm_endpoints_allow", [])
    if not network_allow and not llm_allow:
        docker_args += ["--network", "none"]

    # Secrets nur aus dem Executor-Umfeld (nicht vom Aufrufer).
    for secret_name in security.get("secrets_required", []):
        secret_val = _get_secret(secret_name)
        if secret_val:
            docker_args += ["-e", f"{secret_name}={secret_val}"]
        else:
            logger.warning("Secret %s nicht verfügbar für Script %s", secret_name, script_id)

    for key, value in params.items():
        docker_args += ["-e", f"PARAM_{str(key).upper()}={value}"]

    docker_args.append(script["image"])

    timeout = min(int(security.get("max_timeout_seconds", 900)), MAX_TIMEOUT_SECONDS)
    logger.info("Starte Script %s (Container %s, Timeout %ds)", script_id, container_name, timeout)

    start_time = asyncio.get_event_loop().time()
    async with _run_semaphore:
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Script %s Timeout nach %ds", script_id, timeout)
            await _kill_container(container_name)
            return _err(f"Timeout nach {timeout}s", run_id, scope, timeout)
        except FileNotFoundError:
            return _err("Docker nicht verfügbar im Executor-Container", run_id, scope, 0)
        except Exception as e:
            logger.exception("Script-Fehler (%s)", script_id)
            return _err(f"Container-Fehler: {e}", run_id, scope, 0)

    duration = asyncio.get_event_loop().time() - start_time

    output_files = []
    total = 0
    if output_dir.exists():
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                size = f.stat().st_size
                total += size
                output_files.append({"name": f.name, "size_bytes": size})

    warning = None
    max_bytes = int(security.get("output_max_size_mb", 100)) * 1024 * 1024
    if total > max_bytes:
        warning = f"Output über Limit ({total // (1024*1024)} MB > {max_bytes // (1024*1024)} MB)"

    return {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": gekuerzt(stdout.decode("utf-8", errors="replace"), MAX_STDOUT_CHARS),
        "stderr": gekuerzt(stderr.decode("utf-8", errors="replace"), MAX_STDERR_CHARS) if stderr else "",
        "generated_files": output_files,
        "scope": scope,
        "run_id": run_id,
        "duration_seconds": round(duration, 2),
        "warning": warning,
    }
