"""
DRPL Backend - One-time Cleanup Script
Removes leaked CHECKLIST_JSON_START/END markers and MACHINE-READABLE headers
from existing chat messages and artifacts in the database.

Safe to run multiple times (idempotent).

Run:  python cleanup_checklist_markers.py
  or: ./venv/Scripts/python.exe cleanup_checklist_markers.py
"""

import re
import sys

from sqlalchemy import text

from app.core.database import engine

# Same regex pattern as _strip_checklist_markers in chat_agent_wrappers.py
MARKER_PATTERN = re.compile(
    r'\n*\s*'
    r'(?:[-#*\s]*MACHINE[-_ ]?READABLE[^\n]*\n)?'
    r'\s*CHECKLIST_JSON_START\s*\n?'
    r'.*?'
    r'\n?\s*CHECKLIST_JSON_END\s*\n*',
    flags=re.DOTALL | re.IGNORECASE,
)

# Fallback: CHECKLIST_JSON_START without a matching END (truncated output)
# Strips everything from the START marker to the end of the string
TRUNCATED_MARKER_PATTERN = re.compile(
    r'\n*\s*'
    r'(?:[-#*\s]*MACHINE[-_ ]?READABLE[^\n]*\n)?'
    r'\s*CHECKLIST_JSON_START\s*\n?'
    r'.*',
    flags=re.DOTALL | re.IGNORECASE,
)

# Fallback: catch orphaned MACHINE-READABLE headers without a JSON block
HEADER_ONLY_PATTERN = re.compile(
    r'\n*\s*[-#*\s]*MACHINE[-_ ]?READABLE\s+CHECKLIST\s+DATA[^\n]*',
    flags=re.IGNORECASE,
)


def strip_markers(content: str) -> str:
    """Remove checklist markers from content string."""
    cleaned = MARKER_PATTERN.sub('', content)
    # Handle truncated blocks (START without END)
    cleaned = TRUNCATED_MARKER_PATTERN.sub('', cleaned)
    cleaned = HEADER_ONLY_PATTERN.sub('', cleaned)
    return cleaned.rstrip()


def clean_table(conn, table_name: str, extra_where: str = "") -> tuple[int, int]:
    """
    Scan a table for rows containing CHECKLIST_JSON_START in the content column.
    Returns (rows_found, rows_cleaned).
    """
    where_clause = "content LIKE '%CHECKLIST_JSON_START%'"
    if extra_where:
        where_clause += f" AND {extra_where}"

    rows = conn.execute(
        text(f"SELECT id, content FROM {table_name} WHERE {where_clause}")
    ).fetchall()

    found = len(rows)
    cleaned = 0

    for row in rows:
        row_id, content = row[0], row[1]
        if not content:
            continue

        new_content = strip_markers(content)
        if new_content != content:
            conn.execute(
                text(f"UPDATE {table_name} SET content = :content WHERE id = :id"),
                {"content": new_content, "id": row_id},
            )
            cleaned += 1
            print(f"  Cleaned {table_name}.id={row_id} (removed {len(content) - len(new_content)} chars)")

    return found, cleaned


def main():
    print("=" * 60)
    print("DRPL Checklist Marker Cleanup")
    print("=" * 60)

    tables = [
        ("agent_conversation_history", ""),
        ("command_center_artifacts", ""),  # Clean ALL artifact types, not just checklist
        ("proposal_messages", ""),
    ]

    total_found = 0
    total_cleaned = 0

    with engine.begin() as conn:
        for table_name, extra_where in tables:
            print(f"\nScanning {table_name}...")
            found, cleaned = clean_table(conn, table_name, extra_where)
            total_found += found
            total_cleaned += cleaned
            print(f"  Found: {found} rows with markers, Cleaned: {cleaned} rows")

    print("\n" + "=" * 60)
    print(f"DONE. Total found: {total_found}, Total cleaned: {total_cleaned}")
    print("=" * 60)

    return 0 if total_cleaned >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
