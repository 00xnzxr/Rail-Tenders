"""The LangChain xlsx_generator tool is a third producer of cost_breakdown_xlsx
artifacts (alongside cost_breakdown_service.persist_xlsx_artifact's two call
sites). It builds the workbook and persists it to storage itself, then calls
create_artifact directly -- so the preview enqueue has to be wired in at this
call site too, or artifacts created via this tool never get a preview.
"""
import pytest

pytest.importorskip("openpyxl")

from app.services.langchain.tools import xlsx_generator_tool as tool_mod
from app.services.langchain.tools.xlsx_generator_tool import XlsxGeneratorTool
from app.worker import preview_tasks


def _rows():
    return [
        {"sr_no": 1, "description": "Excavation", "qty": 10, "unit": "cum", "rate": 250, "amount": 2500},
    ]


def test_tool_run_enqueues_preview_for_new_artifact(monkeypatch, db, tmp_path):
    monkeypatch.setattr(tool_mod, "_persist_workbook_to_storage", lambda fpath, fname: f"generated_docs/{fname}")

    queued = []
    monkeypatch.setattr(preview_tasks, "enqueue_preview_render", lambda aid: queued.append(aid))

    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))

    tool = XlsxGeneratorTool(db=db)
    result = tool._run(title="Cost Breakdown", rows=_rows(), tender_id=None, session_id=1)

    import json
    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["artifact_id"] is not None
    assert queued == [payload["artifact_id"]]


def test_tool_run_survives_enqueue_failure(monkeypatch, db, tmp_path):
    monkeypatch.setattr(tool_mod, "_persist_workbook_to_storage", lambda fpath, fname: f"generated_docs/{fname}")

    def boom(aid):
        raise RuntimeError("redis down")

    monkeypatch.setattr(preview_tasks, "enqueue_preview_render", boom)

    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))

    tool = XlsxGeneratorTool(db=db)
    result = tool._run(title="Cost Breakdown", rows=_rows(), tender_id=None, session_id=1)

    import json
    payload = json.loads(result)
    assert payload["status"] == "success"
    assert payload["artifact_id"] is not None
