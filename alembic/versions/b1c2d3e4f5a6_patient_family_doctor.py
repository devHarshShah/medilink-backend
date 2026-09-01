"""patient family doctor

Revision ID: b1c2d3e4f5a6
Revises: f84fe493c5bb
Create Date: 2026-09-01 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "f84fe493c5bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("family_doctor_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_patients_family_doctor_id"), "patients", ["family_doctor_id"], unique=False
    )
    op.create_foreign_key(
        "fk_patients_family_doctor_id_doctors",
        "patients",
        "doctors",
        ["family_doctor_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_patients_family_doctor_id_doctors", "patients", type_="foreignkey")
    op.drop_index(op.f("ix_patients_family_doctor_id"), table_name="patients")
    op.drop_column("patients", "family_doctor_id")
