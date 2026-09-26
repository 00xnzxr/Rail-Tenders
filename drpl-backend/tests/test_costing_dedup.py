"""Unit tests for content-based costing line-item dedup."""
from app.services.cost_breakdown_service import _dedup_lines_by_content


def _line(desc, qty, unit, source, rate):
    return {
        "description": desc, "quantity": qty, "unit": unit,
        "rate_source": source, "rate": rate,
    }


def test_screenshot_case_keeps_best_evidence():
    # The exact reported bug: same work, two sources, different SNs.
    rows = [
        {"sr_no": "2", "description": "Scrapping, cleaning and painting work of Battery Box. (ICF type coaches)",
         "quantity": 2208, "unit": "Numbers", "rate_source": "derived_estimate", "rate": 126.64},
        {"sr_no": "A2", "description": "Scrapping, cleaning and painting work of Battery Box. (ICF type coaches)",
         "quantity": 2208, "unit": "Numbers", "rate_source": "training_data", "rate": 158.33},
    ]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1
    assert len(survivors) == 1
    assert survivors[0]["rate_source"] == "training_data"


def test_distinct_qty_not_merged():
    rows = [_line("Item X", 10, "Nos", "training_data", 5),
            _line("Item X", 20, "Nos", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_distinct_unit_not_merged():
    rows = [_line("Item X", 10, "Nos", "training_data", 5),
            _line("Item X", 10, "Kg", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_tax_lines_never_merged():
    a = _line("GST @ 18%", 1, "", "", 0); a["is_tax_line"] = True
    b = _line("GST @ 18%", 1, "", "", 0); b["is_tax_line"] = True
    survivors, dropped = _dedup_lines_by_content([a, b])
    assert dropped == 0 and len(survivors) == 2


def test_empty_descriptions_never_merged():
    rows = [_line("", 10, "Nos", "training_data", 5),
            _line("   ", 10, "Nos", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_survivor_order_preserved():
    rows = [
        _line("Alpha", 1, "Nos", "web_search", 1),
        _line("Beta", 1, "Nos", "training_data", 2),
        _line("Alpha", 1, "Nos", "training_data", 3),  # dup of row 0, better source
    ]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1
    assert [s["description"] for s in survivors] == ["Alpha", "Beta"]
    # Winner for Alpha is the training_data one (rate 3), placed at Alpha's first slot.
    assert survivors[0]["rate"] == 3


def test_normalization_collapses_whitespace_and_case():
    rows = [_line("Battery  BOX  work", 5, "Nos", "derived_estimate", 1),
            _line("battery box work", 5, "nos", "training_data", 2)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1 and survivors[0]["rate_source"] == "training_data"


def test_persist_call_order_dedups_before_normalize(monkeypatch):
    """persist_from_agent_output must run content-dedup on the line list it
    passes downstream. We stub the DB-touching internals and capture the
    line_items handed to _create_breakdown_with_lines."""
    import app.services.cost_breakdown_service as svc

    captured = {}

    # Component path (skip_nit_validation=True) so the NIT partition gate is
    # bypassed — this is the route with NO other dedup, proving ours runs.
    monkeypatch.setattr(svc, "get_costing_defaults",
                        lambda db: {"overhead_percent": 0, "margin_percent": 0, "gst_percent": 18})
    monkeypatch.setattr(svc, "_normalize_line_dict",
                        lambda line, **kw: dict(line))

    class _Q:
        def filter(self, *a, **k): return self
        def all(self): return []
    class _DB:
        def query(self, *a, **k): return _Q()

    def _fake_create(db, tender_id, normalised, costing, defaults, **kw):
        captured["lines"] = normalised
        return object()
    monkeypatch.setattr(svc, "_create_breakdown_with_lines", _fake_create)

    costing = {"line_items": [
        {"description": "Battery Box work", "quantity": 2208, "unit": "Numbers",
         "rate_source": "derived_estimate", "rate": 126.64},
        {"description": "Battery Box work", "quantity": 2208, "unit": "Numbers",
         "rate_source": "training_data", "rate": 158.33},
    ]}
    svc.persist_from_agent_output(
        db=_DB(), tender_id=1, costing=costing, skip_nit_validation=True,
    )
    assert len(captured["lines"]) == 1
    assert captured["lines"][0]["rate_source"] == "training_data"
