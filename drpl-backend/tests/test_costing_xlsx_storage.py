"""Tests for costing XLSX persistence.

The costing agent's xlsx_generator tool used to set the artifact's `file_path`
to a LOCAL temp path and never upload the workbook to storage. In production the
agent runs in the RQ worker container while downloads are served by the web
container, so the local file was never found — every download fell through to a
full workbook regeneration (slow). The tool must upload the workbook to storage
so downloads serve the stored bytes directly.
"""

import pytest

pytest.importorskip("openpyxl")

from app.services.storage_service import get_storage_service, StorageService
from app.services.langchain.tools.xlsx_generator_tool import (
    _persist_workbook_to_storage,
    build_cost_xlsx,
)


def _tiny_rows():
    return [
        {"sr_no": 1, "description": "Excavation", "qty": 10, "unit": "cum", "rate": 250, "amount": 2500},
        {"sr_no": 2, "description": "Concrete", "qty": 5, "unit": "cum", "rate": 6000, "amount": 30000},
    ]


def test_persist_workbook_uploads_to_storage(tmp_path):
    fpath = str(tmp_path / "cost_local.xlsx")
    build_cost_xlsx(fpath, "Cost Breakdown", _tiny_rows())

    fname = "cost_unit_test.xlsx"
    key = _persist_workbook_to_storage(fpath, fname)

    storage = get_storage_service()
    # Persisted under the same generated_docs/ prefix the download route expects.
    assert key == f"generated_docs/{fname}"
    # And it is actually retrievable from storage (so downloads never regenerate).
    assert storage.file_exists_sync(StorageService.normalize_key(key)) is True
    data = storage.download_file_sync(StorageService.normalize_key(key))
    assert data[:2] == b"PK"  # xlsx is a zip

    storage.delete_file_sync(StorageService.normalize_key(key))
