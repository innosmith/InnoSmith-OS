"""add user_id scope to personal agent/mail/knowledge tables

Revision ID: c7e1a2b3d4f5
Revises: b3d5f7a9c1e2
Create Date: 2026-07-13 00:20:00.000000

Mehrbenutzer-Fundament: Jede persoenliche Agenten-/Mail-/Wissens-Tabelle erhaelt
eine ``user_id`` (FK auf ``users``), damit Daten pro handelndem Principal getrennt
sind. Bestandsdaten werden auf den Owner zurueckgefuehrt (Backfill). Global-eindeutige
Constraints (message_id, graph_id, email, ...) werden auf Composite-Uniques
``(user_id, ...)`` umgestellt, weil sonst zwei User dieselbe E-Mail/denselben
Absender nicht unabhaengig fuehren koennten.

Bewusst NICHT hier: ``index_status`` (reiner Laufzeit-Zustand des einen Indexer-
Daemons) und die geteilten Kollaborations-Tabellen (projects/tasks/tags/...).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c7e1a2b3d4f5"
down_revision: Union[str, Sequence[str], None] = "b3d5f7a9c1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Persoenliche Tabellen -> user_id + FK + Index + Backfill auf Owner.
_TABLES = [
    "agent_jobs",
    "email_triage",
    "chat_triage",
    "sender_profiles",
    "learned_rules",
    "sent_mail_examples",
    "agent_feedback",
    "agent_episodes",
    "followup_suggestions",
    "meeting_transcripts",
    "semantic_documents",
]

# Alte (globale) Unique-Constraints -> neue Composite-Constraints (user_id, ...).
# Namen der Alt-Constraints folgen der Postgres-Default-Konvention <tabelle>_<spalten>_key.
_UNIQUE_MIGRATIONS = [
    ("email_triage", "email_triage_message_id_key",
     "uq_email_triage_user_message", "(user_id, message_id)"),
    ("chat_triage", "chat_triage_message_id_key",
     "uq_chat_triage_user_message", "(user_id, message_id)"),
    ("sender_profiles", "sender_profiles_email_key",
     "uq_sender_profiles_user_email", "(user_id, email)"),
    ("followup_suggestions", "followup_suggestions_conversation_id_key",
     "uq_followup_suggestions_user_conversation", "(user_id, conversation_id)"),
    ("meeting_transcripts", "meeting_transcripts_transcript_id_key",
     "uq_meeting_transcripts_user_transcript", "(user_id, transcript_id)"),
    ("sent_mail_examples", "sent_mail_examples_graph_id_key",
     "uq_sent_mail_examples_user_graph", "(user_id, graph_id)"),
    ("semantic_documents", "semantic_documents_source_type_source_id_chunk_index_key",
     "uq_semantic_documents_user_source_chunk",
     "(user_id, source_type, source_id, chunk_index)"),
]


def upgrade() -> None:
    # 1) user_id-Spalte + FK + Index anlegen, Bestand auf den Owner backfillen.
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE"
        )
        op.execute(
            f"UPDATE {table} SET user_id = "
            f"(SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1) "
            f"WHERE user_id IS NULL"
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user ON {table}(user_id)")

    # 2) Globale Unique-Constraints durch Composite (user_id, ...) ersetzen.
    for table, old_name, new_name, cols in _UNIQUE_MIGRATIONS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old_name}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {new_name} UNIQUE {cols}"
        )


def downgrade() -> None:
    # Composite-Uniques zurueck auf die alten globalen Constraints.
    for table, old_name, new_name, cols in _UNIQUE_MIGRATIONS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {new_name}")
        # Einspaltige Alt-Uniques wiederherstellen (Spaltenname aus cols ableiten).
        single = cols.replace("(user_id, ", "(").strip()
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {old_name} UNIQUE {single}")

    for table in _TABLES:
        op.execute(f"DROP INDEX IF EXISTS idx_{table}_user")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS user_id")
