"""acompanhamento do usuario

O que o usuario decidiu sobre um cargo -- de olho, inscrito, descartado -- em tabela
propria, longe dos fatos lidos do edital.

Revision ID: b1c4f0a7d2e3
Revises: 88f2c95541a9
Create Date: 2026-09-14 21:05:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c4f0a7d2e3"
down_revision: str | None = "88f2c95541a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tracking",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("position_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position_id"),
    )


def downgrade() -> None:
    op.drop_table("tracking")
