"""Add notification center tables

Revision ID: notifications_001
Revises: pdf_max_pages_200_001
Create Date: 2026-05-19

Adds two tables for the platform notification center:

* notifications              — per-user in-app rows, also tracks email delivery
                                via email_sent_at.
* notification_preferences   — system-wide admin defaults, one row per `kind`.
                                Seeded at app startup by
                                ``app.services.seed_notification_preferences``.

create_all() in app/main.py also creates these on first boot for SQLite/Postgres,
so the migration is the explicit, durable record for production deploys.
"""

from alembic import op
import sqlalchemy as sa


revision = "notifications_001"
down_revision = "pdf_max_pages_200_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("action_url", sa.String(512), nullable=True),
        sa.Column("tender_id", sa.Integer(), sa.ForeignKey("tenders.id"), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])
    op.create_index("ix_notifications_tender_id", "notifications", ["tender_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    # Hot path: bell badge — "unread for this user", newest first.
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "is_read", "created_at"],
    )

    op.create_table(
        "notification_preferences",
        sa.Column("kind", sa.String(64), primary_key=True),
        sa.Column("in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_tender_id", table_name="notifications")
    op.drop_index("ix_notifications_is_read", table_name="notifications")
    op.drop_index("ix_notifications_kind", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notification_preferences")
    op.drop_table("notifications")
