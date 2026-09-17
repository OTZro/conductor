"""local_state.manual_stage — the user's manual board-stage override.

The repo's first real migration. Existing DBs were stamped when there were no
revisions (empty version table), so `upgrade head` walks base → this revision and
adds the column; DBs created fresh after this change already carry the column via
create_all and get stamped AT this head, so it never re-runs there (db.py contract).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_manual_stage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("local_state", sa.Column("manual_stage", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("local_state", "manual_stage")
