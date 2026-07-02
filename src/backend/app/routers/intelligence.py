"""Intelligence-Router: Sender-Profile, Agent-Skills, Triage-Statistiken.

Liefert aggregierte Einblicke in das Systemwissen von TaskPilot,
damit der Benutzer sieht, was der Agent gelernt hat.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
from app.database import get_db
from app.models import AgentEpisode, AgentFeedback, AgentJob, EmailTriage, LearnedRule, User

logger = logging.getLogger("taskpilot.intelligence")

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


# ── Sender-Profile ───────────────────────────────────────

class SenderProfile(BaseModel):
    email: str
    name: str | None = None
    total_emails: int
    auto_reply_count: int
    task_count: int
    fyi_count: int
    reply_rate: float
    last_seen: str | None = None


class SenderProfilesResponse(BaseModel):
    profiles: list[SenderProfile]
    total_senders: int


@router.get("/sender-profiles", response_model=SenderProfilesResponse)
async def get_sender_profiles(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> SenderProfilesResponse:
    """Top-Absender mit Triage-Verteilung."""
    query = (
        select(
            EmailTriage.sender_email,
            EmailTriage.sender_name,
            func.count().label("total"),
            func.count().filter(EmailTriage.triage_class == "auto_reply").label("auto_reply"),
            func.count().filter(EmailTriage.triage_class == "task").label("task"),
            func.count().filter(EmailTriage.triage_class == "fyi").label("fyi"),
            func.count().filter(EmailTriage.reply_expected.is_(True)).label("reply_expected"),
            func.max(EmailTriage.created_at).label("last_seen"),
        )
        .where(EmailTriage.sender_email.isnot(None))
        .group_by(EmailTriage.sender_email, EmailTriage.sender_name)
        .order_by(func.count().desc())
        .limit(limit)
    )

    result = await db.execute(query)
    rows = result.all()

    profiles = []
    for row in rows:
        total = row.total or 1
        reply_count = (row.auto_reply or 0) + (row.reply_expected or 0)
        profiles.append(SenderProfile(
            email=row.sender_email or "",
            name=row.sender_name,
            total_emails=total,
            auto_reply_count=row.auto_reply or 0,
            task_count=row.task or 0,
            fyi_count=row.fyi or 0,
            reply_rate=round(reply_count / total, 2) if total > 0 else 0.0,
            last_seen=row.last_seen.isoformat() if row.last_seen else None,
        ))

    total_q = await db.execute(
        select(func.count(func.distinct(EmailTriage.sender_email)))
        .where(EmailTriage.sender_email.isnot(None))
    )
    total_senders = total_q.scalar_one_or_none() or 0

    return SenderProfilesResponse(profiles=profiles, total_senders=total_senders)


# ── Triage-Statistiken ──────────────────────────────────

class TriageStats(BaseModel):
    total: int
    auto_reply: int
    task: int
    fyi: int
    reply_expected_count: int
    avg_per_day: float
    period_days: int


@router.get("/triage-stats", response_model=TriageStats)
async def get_triage_stats(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> TriageStats:
    """Triage-Statistiken der letzten N Tage."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = select(
        func.count().label("total"),
        func.count().filter(EmailTriage.triage_class == "auto_reply").label("auto_reply"),
        func.count().filter(EmailTriage.triage_class == "task").label("task"),
        func.count().filter(EmailTriage.triage_class == "fyi").label("fyi"),
        func.count().filter(EmailTriage.reply_expected.is_(True)).label("reply_expected"),
    ).where(EmailTriage.created_at >= cutoff)

    result = await db.execute(query)
    row = result.one()

    total = row.total or 0
    avg = round(total / max(days, 1), 1)

    return TriageStats(
        total=total,
        auto_reply=row.auto_reply or 0,
        task=row.task or 0,
        fyi=row.fyi or 0,
        reply_expected_count=row.reply_expected or 0,
        avg_per_day=avg,
        period_days=days,
    )


# ── Lern-KPIs (Self-Learning, sichtbar im Cockpit) ───────

class LearningStats(BaseModel):
    period_days: int
    drafts_sent: int
    drafts_edited: int
    drafts_clean: int
    edit_rate: float
    triage_reclass: int
    rejected: int
    thumbs_up: int
    thumbs_down: int
    episodes_total: int
    episodes_corrected: int
    rules_proposed: int
    rules_active: int


class LearningSignal(BaseModel):
    feedback_type: str
    source: str
    sender_email: str | None = None
    reason: str | None = None
    created_at: str | None = None


class LearningOverview(BaseModel):
    stats: LearningStats
    recent: list[LearningSignal]


@router.get("/learning", response_model=LearningOverview)
async def get_learning_overview(
    days: int = 7,
    recent_limit: int = 15,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearningOverview:
    """Aggregierte Lern-KPIs + jüngste Lernsignale.

    Macht für Berater (und Showcase) sichtbar, was der Agent diese Woche aus
    Korrekturen gelernt hat: Edit-Rate von Entwürfen, Reklassifikationen,
    Daumen-Feedback, episodisches Gedächtnis und gelernte Regeln.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    fb_q = await db.execute(
        select(
            func.count().filter(AgentFeedback.feedback_type == "draft_edit").label("draft_edit"),
            func.count().filter(AgentFeedback.feedback_type == "approved_clean").label("approved_clean"),
            func.count().filter(AgentFeedback.feedback_type == "triage_reclass").label("triage_reclass"),
            func.count().filter(AgentFeedback.feedback_type == "rejected").label("rejected"),
            func.count().filter(AgentFeedback.feedback_type == "thumbs_up").label("thumbs_up"),
            func.count().filter(AgentFeedback.feedback_type == "thumbs_down").label("thumbs_down"),
        ).where(AgentFeedback.created_at >= cutoff)
    )
    fb = fb_q.one()

    drafts_edited = fb.draft_edit or 0
    drafts_clean = fb.approved_clean or 0
    drafts_sent = drafts_edited + drafts_clean
    edit_rate = round(drafts_edited / drafts_sent, 2) if drafts_sent else 0.0

    ep_q = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(AgentEpisode.was_corrected.is_(True)).label("corrected"),
        ).where(AgentEpisode.created_at >= cutoff)
    )
    ep = ep_q.one()

    rules_q = await db.execute(
        select(
            func.count().filter(LearnedRule.status == "proposed").label("proposed"),
            func.count().filter(LearnedRule.status == "active").label("active"),
        )
    )
    rules = rules_q.one()

    recent_q = await db.execute(
        select(AgentFeedback)
        .where(AgentFeedback.created_at >= cutoff)
        .order_by(AgentFeedback.created_at.desc())
        .limit(recent_limit)
    )
    recent = [
        LearningSignal(
            feedback_type=r.feedback_type,
            source=r.source,
            sender_email=r.sender_email,
            reason=r.reason,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in recent_q.scalars().all()
    ]

    return LearningOverview(
        stats=LearningStats(
            period_days=days,
            drafts_sent=drafts_sent,
            drafts_edited=drafts_edited,
            drafts_clean=drafts_clean,
            edit_rate=edit_rate,
            triage_reclass=fb.triage_reclass or 0,
            rejected=fb.rejected or 0,
            thumbs_up=fb.thumbs_up or 0,
            thumbs_down=fb.thumbs_down or 0,
            episodes_total=ep.total or 0,
            episodes_corrected=ep.corrected or 0,
            rules_proposed=rules.proposed or 0,
            rules_active=rules.active or 0,
        ),
        recent=recent,
    )


# ── Gelernte Regeln (HITL-Freigabe) ──────────────────────

class RuleCondition(BaseModel):
    field: str
    op: str
    value: str


class RuleAction(BaseModel):
    triage_class: str = "fyi"
    category: str | None = None
    folder: str | None = None


class LearnedRuleOut(BaseModel):
    id: str
    scope: str
    rule_text: str
    evidence: dict
    status: str
    autonomy_hint: str | None = None
    rule_type: str = "llm"
    match_conditions: list[dict] = []
    action: dict = {}
    priority: int = 100
    applied_count: int = 0
    created_at: str | None = None
    approved_at: str | None = None


class LearnedRulesResponse(BaseModel):
    rules: list[LearnedRuleOut]


class LearnedRuleCreate(BaseModel):
    rule_text: str
    rule_type: str = "llm"
    scope: str = "triage"
    match_conditions: list[RuleCondition] = []
    action: RuleAction | None = None
    priority: int = 100
    # Manuell angelegte Regeln sind sofort aktiv (Mensch = Autoritaet).
    status: str = "active"
    autonomy_hint: str | None = None


class LearnedRuleUpdate(BaseModel):
    rule_text: str | None = None
    scope: str | None = None
    status: str | None = None
    match_conditions: list[RuleCondition] | None = None
    action: RuleAction | None = None
    priority: int | None = None
    autonomy_hint: str | None = None


def _rule_out(r: LearnedRule) -> LearnedRuleOut:
    raw_conditions = r.match_conditions if isinstance(r.match_conditions, list) else []
    return LearnedRuleOut(
        id=str(r.id),
        scope=r.scope,
        rule_text=r.rule_text,
        evidence=r.evidence or {},
        status=r.status,
        autonomy_hint=r.autonomy_hint,
        rule_type=r.rule_type or "llm",
        match_conditions=raw_conditions,
        action=r.action if isinstance(r.action, dict) else {},
        priority=r.priority if r.priority is not None else 100,
        applied_count=r.applied_count or 0,
        created_at=r.created_at.isoformat() if r.created_at else None,
        approved_at=r.approved_at.isoformat() if r.approved_at else None,
    )


def _validate_rule_payload(
    rule_type: str,
    scope: str,
    status: str,
    conditions: list[RuleCondition],
    action: RuleAction | None,
) -> None:
    """Prueft die Regel-Bausteine gegen die erlaubten Werte (fail-fast)."""
    from fastapi import HTTPException

    from app.services.rules import (
        ACTION_TRIAGE_CLASSES,
        RULE_FIELDS,
        RULE_OPS,
        RULE_SCOPES,
        RULE_STATUSES,
        RULE_TYPES,
    )

    if rule_type not in RULE_TYPES:
        raise HTTPException(status_code=400, detail=f"Ungültiger rule_type: {rule_type}")
    if scope not in RULE_SCOPES:
        raise HTTPException(status_code=400, detail=f"Ungültiger scope: {scope}")
    if status not in RULE_STATUSES:
        raise HTTPException(status_code=400, detail=f"Ungültiger status: {status}")
    if rule_type == "deterministic":
        if not conditions:
            raise HTTPException(
                status_code=400,
                detail="Deterministische Regel braucht mindestens eine Bedingung.",
            )
        for c in conditions:
            if c.field not in RULE_FIELDS:
                raise HTTPException(status_code=400, detail=f"Ungültiges Feld: {c.field}")
            if c.op not in RULE_OPS:
                raise HTTPException(status_code=400, detail=f"Ungültiger Operator: {c.op}")
            if not c.value.strip():
                raise HTTPException(status_code=400, detail="Bedingungs-Wert darf nicht leer sein.")
        if action and action.triage_class not in ACTION_TRIAGE_CLASSES:
            raise HTTPException(
                status_code=400,
                detail=f"Ungültige Aktion triage_class: {action.triage_class}",
            )


@router.get("/rules", response_model=LearnedRulesResponse)
async def list_learned_rules(
    status: str | None = None,
    rule_type: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearnedRulesResponse:
    """Listet gelernte/gepflegte Regeln (optional nach Status/Typ gefiltert)."""
    stmt = select(LearnedRule)
    if status:
        stmt = stmt.where(LearnedRule.status == status)
    if rule_type:
        stmt = stmt.where(LearnedRule.rule_type == rule_type)
    stmt = stmt.order_by(
        LearnedRule.status, LearnedRule.priority, LearnedRule.created_at.desc()
    ).limit(limit)
    result = await db.execute(stmt)
    return LearnedRulesResponse(rules=[_rule_out(r) for r in result.scalars().all()])


@router.post("/rules", response_model=LearnedRuleOut, status_code=201)
async def create_learned_rule(
    payload: LearnedRuleCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearnedRuleOut:
    """Legt eine Regel manuell an (LLM-Leitregel oder deterministische Override)."""
    if not payload.rule_text.strip():
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="rule_text darf nicht leer sein.")
    _validate_rule_payload(
        payload.rule_type, payload.scope, payload.status,
        payload.match_conditions, payload.action,
    )
    rule = LearnedRule(
        rule_text=payload.rule_text.strip(),
        rule_type=payload.rule_type,
        scope=payload.scope,
        status=payload.status,
        autonomy_hint=payload.autonomy_hint,
        match_conditions=[c.model_dump() for c in payload.match_conditions],
        action=payload.action.model_dump() if payload.action else {},
        priority=payload.priority,
        evidence={"source": "manual"},
    )
    if payload.status == "active":
        rule.approved_at = datetime.now(timezone.utc)
    db.add(rule)
    await db.flush()
    return _rule_out(rule)


async def _get_rule_or_404(db: AsyncSession, rule_id: str) -> LearnedRule:
    import uuid as _uuid

    from fastapi import HTTPException

    try:
        rid = _uuid.UUID(rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungültige Regel-ID") from exc
    rule = (await db.execute(select(LearnedRule).where(LearnedRule.id == rid))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden")
    return rule


@router.patch("/rules/{rule_id}", response_model=LearnedRuleOut)
async def update_learned_rule(
    rule_id: str,
    payload: LearnedRuleUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearnedRuleOut:
    """Bearbeitet eine Regel (Text, Scope, Status, Bedingungen, Aktion, Priorität)."""
    rule = await _get_rule_or_404(db, rule_id)

    new_scope = payload.scope if payload.scope is not None else rule.scope
    new_status = payload.status if payload.status is not None else rule.status
    new_conditions = (
        payload.match_conditions
        if payload.match_conditions is not None
        else [RuleCondition(**c) for c in (rule.match_conditions or []) if isinstance(c, dict)]
    )
    new_action = payload.action
    if new_action is None and isinstance(rule.action, dict) and rule.action:
        new_action = RuleAction(**rule.action)
    _validate_rule_payload(rule.rule_type, new_scope, new_status, new_conditions, new_action)

    if payload.rule_text is not None:
        rule.rule_text = payload.rule_text.strip()
    if payload.scope is not None:
        rule.scope = payload.scope
    if payload.priority is not None:
        rule.priority = payload.priority
    if payload.autonomy_hint is not None:
        rule.autonomy_hint = payload.autonomy_hint
    if payload.match_conditions is not None:
        rule.match_conditions = [c.model_dump() for c in payload.match_conditions]
    if payload.action is not None:
        rule.action = payload.action.model_dump()
    if payload.status is not None:
        rule.status = payload.status
        # Beim (Re-)Aktivieren den Freigabe-Zeitpunkt setzen, sonst zuruecksetzen.
        rule.approved_at = datetime.now(timezone.utc) if payload.status == "active" else None
    await db.flush()
    return _rule_out(rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_learned_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> None:
    """Löscht eine Regel endgültig."""
    rule = await _get_rule_or_404(db, rule_id)
    await db.delete(rule)
    await db.flush()


@router.post("/rules/{rule_id}/approve", response_model=LearnedRuleOut)
async def approve_learned_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearnedRuleOut:
    """Gibt eine vorgeschlagene Regel frei -> ab jetzt im Triage-Prompt aktiv."""
    import uuid as _uuid

    from fastapi import HTTPException

    try:
        rid = _uuid.UUID(rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungueltige Regel-ID") from exc
    rule = (await db.execute(select(LearnedRule).where(LearnedRule.id == rid))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden")
    rule.status = "active"
    rule.approved_at = datetime.now(timezone.utc)
    await db.flush()
    return _rule_out(rule)


@router.post("/rules/{rule_id}/reject", response_model=LearnedRuleOut)
async def reject_learned_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> LearnedRuleOut:
    """Verwirft eine vorgeschlagene Regel (kein Einfluss auf den Agenten)."""
    import uuid as _uuid

    from fastapi import HTTPException

    try:
        rid = _uuid.UUID(rule_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ungueltige Regel-ID") from exc
    rule = (await db.execute(select(LearnedRule).where(LearnedRule.id == rid))).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Regel nicht gefunden")
    rule.status = "rejected"
    await db.flush()
    return _rule_out(rule)


# ── Agent-Skills ─────────────────────────────────────────

class AgentSkill(BaseModel):
    name: str
    description: str
    content: str = ""
    requires_toolsets: list[str] = []
    size: int = 0


class AgentSkillsResponse(BaseModel):
    skills: list[AgentSkill]


@router.get("/skills", response_model=AgentSkillsResponse)
async def get_agent_skills(
    _user: User = Depends(require_role("owner")),
) -> AgentSkillsResponse:
    """Listet die verfügbaren Hermes-Skills (native SKILL.md) mit Beschreibung."""
    from app.services.hermes_config import discover_skills

    skills = [
        AgentSkill(
            name=s["name"],
            description=s["description"],
            content=s["content"],
            requires_toolsets=s["requires_toolsets"],
            size=s["size"],
        )
        for s in discover_skills()
    ]
    return AgentSkillsResponse(skills=skills)


# ── Skill-Editor (Markdown bearbeiten, mit Backup) ───────

class SkillUpdate(BaseModel):
    content: str


class SkillUpdateResponse(BaseModel):
    name: str
    size: int
    backup: str


def _resolve_skill_md_path(name: str):
    """Löst einen Skill-Namen sicher auf seinen ``SKILL.md``-Pfad auf (oder None).

    Erlaubt nur existierende Skills (kein Anlegen), keine Pfad-Trenner/``..``.
    Trifft erst per Verzeichnisname, sonst per Frontmatter-Name.
    """
    from app.services.hermes_config import _parse_skill_frontmatter, get_hermes_home

    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    skills_dir = get_hermes_home() / "skills"
    direct = skills_dir / name / "SKILL.md"
    if direct.exists():
        return direct
    for skill_file in skills_dir.glob("*/SKILL.md"):
        try:
            fm = _parse_skill_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if str(fm.get("name") or skill_file.parent.name).strip() == name:
            return skill_file
    return None


def _backup_skill(path) -> str:
    """Sicherheitsnetz vor dem Überschreiben: timestamped ``.bak`` + best-effort Git-Commit.

    Der Bind-Mount macht Skill-Änderungen sofort produktiv -- darum IMMER eine
    Sicherung anlegen. Die ``.bak``-Kopie ist garantiert; ist ``~/.hermes`` ein
    Git-Repo, wird zusätzlich ein Commit versucht (best-effort, blockiert nie).
    """
    import shutil
    import subprocess

    from app.services.hermes_config import get_hermes_home

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".{ts}.bak")
    shutil.copy2(path, bak)
    home = get_hermes_home()
    if (home / ".git").exists():
        try:
            subprocess.run(
                ["git", "-C", str(home), "add", "-A"],
                check=False, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "-C", str(home), "commit", "-m", f"Skill-Backup {path.parent.name} {ts}"],
                check=False, capture_output=True, timeout=10,
            )
        except Exception:  # noqa: BLE001 - Git ist nur Bonus, .bak ist die Garantie
            logger.warning("Git-Backup für Skill-Edit fehlgeschlagen (Kopie .bak existiert)")
    return bak.name


@router.patch("/skills/{name}", response_model=SkillUpdateResponse)
async def update_agent_skill(
    name: str,
    payload: SkillUpdate,
    _user: User = Depends(require_role("owner")),
) -> SkillUpdateResponse:
    """Speichert den bearbeiteten Inhalt eines bestehenden Skills (mit Backup)."""
    from fastapi import HTTPException

    from app.services.hermes_config import get_hermes_home

    if len(payload.content) > 100_000:
        raise HTTPException(status_code=413, detail="Skill-Inhalt zu gross (max. 100000 Zeichen).")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Skill-Inhalt darf nicht leer sein.")

    path = _resolve_skill_md_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Skill nicht gefunden")
    # Pfad-Whitelist: aufgelöster Pfad muss unter skills/ liegen.
    skills_dir = (get_hermes_home() / "skills").resolve()
    if skills_dir not in path.resolve().parents:
        raise HTTPException(status_code=400, detail="Ungültiger Skill-Pfad")

    backup = _backup_skill(path)
    path.write_text(payload.content, encoding="utf-8")
    logger.info("Skill '%s' bearbeitet (Backup: %s)", name, backup)
    return SkillUpdateResponse(name=name, size=len(payload.content.encode("utf-8")), backup=backup)


# ── Skill-Nutzungs-Analytics (Show-Demo) ─────────────────

class SkillUsageItem(BaseModel):
    name: str
    description: str
    requires_toolsets: list[str] = []
    view_count: int = 0
    last_used_at: str | None = None
    agent_created: bool = False


class SkillUsageResponse(BaseModel):
    items: list[SkillUsageItem]
    total_invocations: int
    jobs_scanned: int
    period_jobs: int


@router.get("/skill-usage", response_model=SkillUsageResponse)
async def get_skill_usage(
    jobs_limit: int = 500,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> SkillUsageResponse:
    """Echte Skill-Nutzung des Agenten, abgeleitet aus den Job-Traces.

    Zwei Signale werden pro Skill kombiniert (jeweils ueber die letzten
    ``jobs_limit`` Jobs, inkl. letzter Nutzung):

    1. **Explizite Loads** -- der Agent laedt einen Skill via
       ``skill_view``/``skill_manage`` (so arbeitet der Triage-Worker, der
       ``email-triage``/``email-style`` explizit anweist).
    2. **Tool-Heuristik** -- im Chat injiziert das Backend den Skill-Inhalt
       direkt in den System-Prompt; der Agent ruft dann die MCP-Tools (z. B.
       ``mcp_pipedrive_*``) direkt auf, ohne ``skill_view``. Damit solche
       Einsaetze nicht unsichtbar bleiben, ordnen wir genutzte MCP-Toolsets
       ueber ``requires_toolsets`` ihrem Skill zu -- aber nur fuer **eindeutige**
       Toolsets (genau ein Skill setzt sie voraus). Generische Toolsets wie
       ``graph``/``taskpilot`` werden von mehreren Skills geteilt und taugen
       nicht als verlaessliches Signal, sonst kaeme es zu Fehlzuordnungen.

    Hermes' ``.usage.json`` wird nur fuer agent-erstellte Skills gepflegt --
    unsere Skills sind manuell authored, daher ist der Job-Trace die
    verlaessliche Quelle. Macht im Agenten-Cockpit sichtbar, welche Skills der
    Agent wie oft nutzt.
    """
    import json as _json

    from app.services.hermes_config import discover_skills, get_hermes_home

    skills = discover_skills()

    # Reverse-Map Toolset -> Skills, die ihn voraussetzen. Nur eindeutige
    # Toolsets (genau ein Skill) dienen als Nutzungssignal fuer die Heuristik.
    toolset_skills: dict[str, list[str]] = {}
    for s in skills:
        for ts_name in (s.get("requires_toolsets") or []):
            toolset_skills.setdefault(str(ts_name), []).append(s["name"])
    distinctive_skill: dict[str, str] = {
        ts_name: names[0] for ts_name, names in toolset_skills.items() if len(names) == 1
    }

    rows = await db.execute(
        select(AgentJob.metadata_json, AgentJob.completed_at, AgentJob.created_at)
        .order_by(AgentJob.created_at.desc())
        .limit(jobs_limit)
    )

    counts: dict[str, int] = {}
    last_used: dict[str, str] = {}
    jobs_scanned = 0

    def _bump(sk: str, iso: str | None) -> None:
        counts[sk] = counts.get(sk, 0) + 1
        if iso and (sk not in last_used or iso > last_used[sk]):
            last_used[sk] = iso

    for meta, completed_at, created_at in rows.all():
        jobs_scanned += 1
        trace = (meta or {}).get("trace") or []
        ts = completed_at or created_at
        iso = ts.isoformat() if ts else None
        explicit_in_job: set[str] = set()
        used_toolsets: set[str] = set()
        for ev in trace:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") != "tool_start":
                continue
            name = ev.get("name")
            if name in ("skill_view", "skill_manage"):
                sk = ev.get("skill")
                if not sk:
                    continue
                _bump(sk, iso)
                explicit_in_job.add(sk)
            elif isinstance(name, str) and name.startswith("mcp_"):
                # Tool-Schema: mcp_<toolset>_<tool> -> Toolset = 2. Segment.
                parts = name.split("_", 2)
                if len(parts) >= 2 and parts[1]:
                    used_toolsets.add(parts[1])

        # Tool-Heuristik: pro eindeutigem genutzten Toolset den zugehoerigen
        # Skill einmal je Job gutschreiben -- sofern er nicht ohnehin explizit
        # geladen wurde (kein Doppelzaehlen).
        for ts_name in used_toolsets:
            sk = distinctive_skill.get(ts_name)
            if sk and sk not in explicit_in_job:
                _bump(sk, iso)

    # Provenance (agent-erstellt?) aus .usage.json -- ohne Hermes-Import direkt lesen.
    usage_map: dict = {}
    usage_path = get_hermes_home() / "skills" / ".usage.json"
    if usage_path.exists():
        try:
            usage_map = _json.loads(usage_path.read_text(encoding="utf-8")) or {}
        except Exception:
            usage_map = {}

    items: list[SkillUsageItem] = []
    total = 0
    for s in skills:
        name = s["name"]
        vc = counts.get(name, 0)
        total += vc
        rec = usage_map.get(name) if isinstance(usage_map, dict) else None
        agent_created = bool(
            isinstance(rec, dict)
            and (rec.get("created_by") == "agent" or rec.get("agent_created") is True)
        )
        items.append(SkillUsageItem(
            name=name,
            description=s["description"],
            requires_toolsets=s["requires_toolsets"],
            view_count=vc,
            last_used_at=last_used.get(name),
            agent_created=agent_created,
        ))

    items.sort(key=lambda i: (i.view_count, i.name.lower()), reverse=True)

    return SkillUsageResponse(
        items=items,
        total_invocations=total,
        jobs_scanned=jobs_scanned,
        period_jobs=jobs_limit,
    )


# ── Hermes-Brain (zentraler Runtime-Status) ──────────────

class BrainFile(BaseModel):
    name: str
    content: str
    size: int
    last_modified: str | None = None


class BrainStatus(BaseModel):
    runtime: str = "hermes"
    model: str | None = None
    mcp_servers: list[str] = []
    skills: list[str] = []
    user_profile: BrainFile | None = None
    memory: BrainFile | None = None
    soul: BrainFile | None = None


def _read_brain_file(path) -> BrainFile | None:
    from datetime import datetime, timezone as _tz

    if not path.exists() or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        stat = path.stat()
        return BrainFile(
            name=path.name,
            content=content[:50000],
            size=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=_tz.utc).isoformat(),
        )
    except OSError:
        return None


@router.get("/brain", response_model=BrainStatus)
async def get_brain(
    _user: User = Depends(require_role("owner")),
) -> BrainStatus:
    """Aggregierter Hermes-Status: Modell, MCP-Server, Skills, Memory, Identität.

    Single Source of Truth für den Intelligence-Tab im Frontend.
    """
    import yaml
    from app.services.hermes_config import discover_skills, get_hermes_home

    home = get_hermes_home()
    config_path = home / "config.yaml"

    model: str | None = None
    mcp_servers: list[str] = []
    if config_path.exists():
        try:
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            model = (cfg.get("model") or {}).get("default")
            mcp_servers = sorted((cfg.get("mcp_servers") or {}).keys())
        except Exception:
            pass

    skills: list[str] = [s["name"] for s in discover_skills()]

    return BrainStatus(
        runtime="hermes",
        model=model,
        mcp_servers=mcp_servers,
        skills=skills,
        user_profile=_read_brain_file(home / "memories" / "USER.md"),
        memory=_read_brain_file(home / "memories" / "MEMORY.md"),
        soul=_read_brain_file(home / "SOUL.md"),
    )
