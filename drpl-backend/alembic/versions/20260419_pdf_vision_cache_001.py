"""Add document_page_vision_cache table for per-page Claude vision results

Revision ID: pdf_vision_cache_001
Revises: pending_clarifications_001
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa

revision = "pdf_vision_cache_001"
down_revision = "pending_clarifications_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_page_vision_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("page_sha1", sa.String(length=40), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "source_key", "page_index", "page_sha1",
            name="uq_page_vision_cache_key",
        ),
    )
    op.create_index(
        "ix_page_vision_cache_source_key",
        "document_page_vision_cache",
        ["source_key"],
    )


def downgrade():
    op.drop_index("ix_page_vision_cache_source_key", table_name="document_page_vision_cache")
    op.drop_table("document_page_vision_cache")
