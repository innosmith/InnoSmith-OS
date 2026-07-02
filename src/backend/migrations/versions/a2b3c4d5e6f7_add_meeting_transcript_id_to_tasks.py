"""tasks: meeting_transcript_id (strukturierte Meeting-Quelle)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-02 23:15:00.000000

Task-Vorschläge aus Meeting-Transkripten sollen ihre Quelle strukturiert
referenzieren (Badge + Link zum Protokoll), analog zu ``email_message_id`` /
``email_conversation_id``. FK mit ON DELETE SET NULL, damit ein gelöschtes
Transkript den Task nicht mitreisst.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('meeting_transcript_id', UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'tasks_meeting_transcript_id_fkey',
        'tasks', 'meeting_transcripts',
        ['meeting_transcript_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('idx_tasks_meeting_transcript', 'tasks', ['meeting_transcript_id'])


def downgrade() -> None:
    op.drop_index('idx_tasks_meeting_transcript', table_name='tasks')
    op.drop_constraint('tasks_meeting_transcript_id_fkey', 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'meeting_transcript_id')
