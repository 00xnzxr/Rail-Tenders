"""
One-shot migration: copy every file under settings.upload_dir/ into R2,
preserving the relative path as the object key. Also normalises `file_path`
columns in the database to drop any `./uploads/` or `uploads/` prefix so
they line up with R2 object keys.

Usage:
    # Dry-run (uploads nothing, only prints the plan):
    python -m scripts.migrate_uploads_to_r2 --dry-run

    # Upload files but skip DB updates:
    python -m scripts.migrate_uploads_to_r2 --files-only

    # Update DB paths but skip file uploads (if already migrated):
    python -m scripts.migrate_uploads_to_r2 --db-only

    # Full migration (files + DB):
    python -m scripts.migrate_uploads_to_r2

The script is idempotent:
  * Files already present in R2 (checked via head_object) are skipped.
  * The SQL UPDATE uses regexp_replace against both "./uploads/" and
    "uploads/" prefixes — re-running is a no-op once keys are clean.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path


# (table_name, column_name) pairs whose values store upload paths that must
# be normalised to R2 object keys. Column names vary per table — introspection
# below skips any that don't exist in the live schema.
PATH_COLUMNS: list[tuple[str, str]] = [
    ("tender_documents", "file_path"),
    ("proposal_documents", "file_path"),
    ("chat_attachments", "file_path"),
    ("command_center_artifacts", "file_path"),
    ("generated_documents", "generated_file_path"),
    ("letterhead_templates", "letterhead_pdf_path"),
    ("letterhead_templates", "header_image_path"),
    ("letterhead_templates", "footer_image_path"),
    ("letterhead_templates", "watermark_image_path"),
    ("letterhead_templates", "logo_path"),
    ("digital_signatures", "signature_image_path"),
    ("digital_signatures", "stamp_image_path"),
    ("proposal_templates", "original_file_path"),
    ("document_workspaces", "original_file_path"),
    ("document_format_templates", "original_file_path"),
    ("agent_test_documents", "file_path"),
    ("costing_templates", "sample_pdf_path"),
]


def migrate_files(dry_run: bool) -> tuple[int, int]:
    """Walk upload_dir and upload each file to R2. Returns (uploaded, skipped)."""
    from app.core.config import get_settings
    from app.services.storage_service import get_storage_service

    settings = get_settings()
    storage = get_storage_service()
    base = Path(settings.upload_dir).resolve()

    if not base.exists():
        print(f"[skip] upload_dir does not exist: {base}")
        return 0, 0

    uploaded = 0
    skipped = 0
    errors = 0

    for abs_path in base.rglob("*"):
        if not abs_path.is_file():
            continue

        rel = abs_path.relative_to(base).as_posix()
        key = storage.normalize_key(rel)

        try:
            if storage.file_exists_sync(key):
                skipped += 1
                continue
        except Exception as e:
            print(f"[warn] head_object failed for {key}: {e}")

        if dry_run:
            print(f"[plan] {abs_path} -> {key}")
            uploaded += 1
            continue

        try:
            data = abs_path.read_bytes()
            ctype = mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
            storage.upload_file_sync(key, data, content_type=ctype)
            uploaded += 1
            print(f"[ok] {key} ({len(data)} bytes)")
        except Exception as e:
            errors += 1
            print(f"[err] {key}: {e}")

    print(f"[files] uploaded={uploaded} skipped={skipped} errors={errors}")
    return uploaded, skipped


def migrate_db_paths(dry_run: bool) -> int:
    """Strip `./uploads/` / `uploads/` / absolute-upload-dir prefixes from every
    known path column. Also rewrites backslashes to forward slashes."""
    from sqlalchemy import inspect, text

    from app.core.config import get_settings
    from app.core.database import SessionLocal

    settings = get_settings()
    abs_upload_dir = str(Path(settings.upload_dir).resolve()).replace("\\", "/")

    db = SessionLocal()
    updated_total = 0
    try:
        inspector = inspect(db.bind)
        existing_tables = set(inspector.get_table_names())
        # Cache column lookups per table to avoid repeated inspection calls.
        columns_by_table: dict[str, set[str]] = {}

        def table_has_column(table: str, column: str) -> bool:
            if table not in existing_tables:
                return False
            cols = columns_by_table.get(table)
            if cols is None:
                cols = {c["name"] for c in inspector.get_columns(table)}
                columns_by_table[table] = cols
            return column in cols

        # Prefix regex covers: absolute upload dir, ./uploads/, uploads/.
        prefix_pattern = fr"^({abs_upload_dir}/|\./uploads/|uploads/)"

        for table, column in PATH_COLUMNS:
            if not table_has_column(table, column):
                print(f"[db] skip {table}.{column} (not in schema)")
                continue

            match_sql = text(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {column} IS NOT NULL AND ({column} ~ :p OR {column} LIKE :bs)"
            )
            preview = db.execute(match_sql, {"p": prefix_pattern, "bs": "%\\%"}).scalar()

            if dry_run:
                print(f"[db plan] {table}.{column}: would update {preview} rows")
                continue

            if not preview:
                print(f"[db] {table}.{column}: 0 rows to update")
                continue

            # Two passes: strip prefix, then normalise backslashes.
            strip_sql = text(
                f"UPDATE {table} SET {column} = regexp_replace({column}, :p, '') "
                f"WHERE {column} ~ :p"
            )
            slash_sql = text(
                f"UPDATE {table} SET {column} = replace({column}, '\\', '/') "
                f"WHERE {column} LIKE :bs"
            )
            stripped = db.execute(strip_sql, {"p": prefix_pattern}).rowcount or 0
            slashed = db.execute(slash_sql, {"bs": "%\\%"}).rowcount or 0
            updated_total += stripped + slashed
            print(f"[db] {table}.{column}: stripped={stripped} slashed={slashed}")

        if not dry_run:
            db.commit()
    finally:
        db.close()

    return updated_total


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate uploads/ to Cloudflare R2")
    parser.add_argument("--dry-run", action="store_true", help="Show plan, make no changes")
    parser.add_argument("--files-only", action="store_true", help="Upload files, skip DB updates")
    parser.add_argument("--db-only", action="store_true", help="Update DB paths, skip file uploads")
    args = parser.parse_args()

    from app.core.config import get_settings
    settings = get_settings()
    if settings.storage_backend != "r2" and not args.dry_run and not args.db_only:
        print(
            "[abort] storage_backend is not 'r2' — set STORAGE_BACKEND=r2 "
            "(and the R2_* credentials) before running this migration."
        )
        return 2

    if not args.db_only:
        migrate_files(dry_run=args.dry_run)

    if not args.files_only:
        migrate_db_paths(dry_run=args.dry_run)

    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
