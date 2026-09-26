"""Tender Scope Profile + scope-driven Tender columns (Phase 7)

Revision ID: tender_scope_profile_001
Revises: agent_user_customized_001
Create Date: 2026-04-30

Adds:
  * tender_scope_profiles table — singleton-by-name config that defines
    what tenders count as a fit for DRPL (keyword groups, exclusions,
    ministries, value range, relevance threshold).
  * tenders.search_match_keyword — which scope keyword surfaced the row
    (populated by GeM auto-search driver in the extension).
  * tenders.is_eligible_indicator — IREPS blue-tick / arrow visual buyer
    hint, distinct from AI eligibility_status.
  * tenders.fit_reasoning — free-text justification from the scope-aware
    relevance agent run on ingest.
"""

from alembic import op
import sqlalchemy as sa


revision = "tender_scope_profile_001"
down_revision = "agent_user_customized_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tender_scope_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("keyword_groups", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("exclusion_terms", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("target_ministries", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("value_min", sa.Float(), nullable=True),
        sa.Column("value_max", sa.Float(), nullable=True),
        sa.Column("relevance_threshold", sa.Float(), nullable=False, server_default=sa.text("0.6")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_tender_scope_profiles_name",
        "tender_scope_profiles",
        ["name"],
        unique=True,
    )

    op.add_column(
        "tenders",
        sa.Column("search_match_keyword", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_tenders_search_match_keyword",
        "tenders",
        ["search_match_keyword"],
    )
    op.add_column(
        "tenders",
        sa.Column("is_eligible_indicator", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "tenders",
        sa.Column("fit_reasoning", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("tenders", "fit_reasoning")
    op.drop_column("tenders", "is_eligible_indicator")
    op.drop_index("ix_tenders_search_match_keyword", table_name="tenders")
    op.drop_column("tenders", "search_match_keyword")
    op.drop_index("ix_tender_scope_profiles_name", table_name="tender_scope_profiles")
    op.drop_table("tender_scope_profiles")
