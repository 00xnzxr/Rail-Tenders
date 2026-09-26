"""Which rows the costing agent prices, and which the platform builds up.

On a row that prints a published rate the agent's figure was a guess --
0.15x-6.45x of the railway's estimate on the Mid-Life NIT -- unless the firm's
own rate data backed it, and settlement then replaced it with the railway's
rate divided by 1.25: "the rate which platform used is the same railway
rate". With no firm rate data those rows now go to the platform's own cost
build-up (costing/cost_buildup.py) instead of the agent; every other row
still goes to the agent.
"""

import asyncio
import itertools
import json
import re
import uuid

import pytest
from langchain_core.messages import AIMessage

from app.core.actor_context import actor_scope
from app.models.agent_memory import AgentMemory
from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import cost_breakdown_service as cbs
from app.services.costing import model_routing as mr

_ids = itertools.count(970000)


def _rows():
    return [
        {"boq_item_id": 1, "description": "Fitting & Repairing Charges"},
        {"boq_item_id": 2, "description": "Web to Drg No LE11185"},
        {"boq_item_id": 3, "description": "Paint set, epoxy"},            # no published rate
        {"boq_item_id": 4, "description": "Door frame", "component_of": "Schedule A item 3"},
        {"boq_item_id": 5, "description": "Sanitary fittings lot"},        # no quantity
    ]


_RATES = {1: 188800.0, 2: 2239.21, 3: None, 4: 500.0, 5: 900.0}
_QTYS = {1: 37, 2: 44, 3: 10, 4: 2, 5: None}


def _route(db, **kw):
    return mr.route_rows_for_model(
        db, _rows(), published_rates=_RATES, quantities=_QTYS,
        training_chars=kw.get("training_chars", 0),
    )


def _ids_of(rows):
    return sorted(r["boq_item_id"] for r in rows)


def test_rows_with_a_published_rate_and_no_firm_data_skip_the_model(db):
    routing = _route(db)
    assert _ids_of(routing.reference_rows) == [1, 2]
    # No published rate, a component, a row with no quantity: the model's
    # answer is the figure for each, so each keeps the model.
    assert _ids_of(routing.model_rows) == [3, 4, 5]


def test_with_training_data_every_row_goes_to_the_model(db):
    routing = _route(db, training_chars=5000)
    assert _ids_of(routing.model_rows) == [1, 2, 3, 4, 5]
    assert routing.skipped == 0


def test_the_switch_restores_the_old_behaviour(db, monkeypatch):
    from app.services import settings_service
    real = settings_service.get_setting_value
    monkeypatch.setattr(settings_service, "get_setting_value",
                        lambda d, k, default=None: False if k == "costing.model_skips_reference_rows"
                        else real(d, k, default))
    assert _route(db).skipped == 0


def test_rows_are_not_skipped_when_the_platform_cannot_build_them_up(db):
    """No key, or the build-up switched off: nothing of the platform's own
    would price a skipped row, so the agent prices every row."""
    routing = mr.route_rows_for_model(
        db, _rows(), published_rates=_RATES, quantities=_QTYS, training_chars=0,
        buildup_available=False,
    )
    assert routing.skipped == 0


def test_a_rate_card_match_keeps_the_row_with_the_model(db, monkeypatch):
    monkeypatch.setattr(mr, "_load_ratecard",
                        lambda _db: [("le11185", {"web", "le11185"})])
    routing = _route(db)
    assert 2 in _ids_of(routing.model_rows)
    assert _ids_of(routing.reference_rows) == [1]


def test_rate_card_matching_is_generous_but_not_blind():
    card = [("3920693", {"filter", "fuel", "cartridge"})]
    assert mr.ratecard_could_price("Fuel filter cartridge for DG set", card)
    assert mr.ratecard_could_price("Part No 3920693 filter", card)
    assert not mr.ratecard_could_price("Complete coach set fitting work", card)
    assert not mr.ratecard_could_price("Anything", [])


def _user(db, role="costing_research"):
    from app.models.user import User
    tag = uuid.uuid4().hex[:8]
    u = User(email=f"route-{tag}@t", name="r", hashed_password="x", role=role, is_active=True)
    db.add(u)
    db.commit()
    return u


def test_a_rate_note_a_person_wrote_keeps_every_row_with_the_model(db):
    u = _user(db)
    mem = AgentMemory(agent_key="costing_researcher", memory_type="fact",
                      content="Our LED fitting rate is Rs 450", keywords=[], created_by=u.id,
                      context="entered on the memory page")
    db.add(mem)
    db.commit()
    try:
        with actor_scope(user_id=u.id, role=u.role):
            assert _route(db).skipped == 0
    finally:
        db.delete(mem)
        db.delete(u)
        db.commit()


def test_captured_memories_do_not_count_as_firm_rate_data(db):
    """Learnings, exemplars of past costings and the agent's own writes carry
    the model's or the railway's own figures back round."""
    u = _user(db)
    mems = [
        AgentMemory(agent_key="costing_researcher", memory_type="exemplar", content="past costing",
                    keywords=[], created_by=u.id, context="Auto-captured cost_breakdown output"),
        AgentMemory(agent_key="costing_researcher", memory_type="fact", content="agent wrote this",
                    keywords=[], created_by=u.id, context="[agent] stored during a run"),
    ]
    db.add_all(mems)
    db.commit()
    try:
        with actor_scope(user_id=u.id, role=u.role):
            assert _route(db).skipped == 2
    finally:
        for m in mems:
            db.delete(m)
        db.delete(u)
        db.commit()


# ── through the batched node ─────────────────────────────────────────────────


@pytest.fixture
def tender(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Mid-Life", source_url="x"))
    db.add_all([
        BOQItem(tender_id=tid, sr_no=1, item_code="1", schedule_name="O", quantity=37,
                unit="Per Coach", estimated_rate=188800.0, description="Fitting & Repairing Charges"),
        BOQItem(tender_id=tid, sr_no=2, item_code="2", schedule_name="A", quantity=44,
                unit="Numbers", estimated_rate=2239.21, description="Web to Drg No LE11185"),
        BOQItem(tender_id=tid, sr_no=3, item_code="3", schedule_name="A", quantity=10,
                unit="Set", estimated_rate=None, description="Paint set, epoxy"),
    ])
    db.commit()
    return tid


#: What the fake platform build-up prices each row at, by item code.
_BUILT = {"1": 162000.0, "2": 1650.0}


def _run_node(db, tid, monkeypatch, training_context="", key="sk-test"):
    from app.services.costing import cost_buildup as cb
    from app.services.langchain.graphs import enhanced_costing_agent as eca

    seen: list[int] = []
    built: list[int] = []
    real_render = eca._render_bidding_schedule_block

    def _render(batch, withhold_published=False, schedule_titles=None):
        seen.extend(int(r["boq_item_id"]) for r in batch)
        return real_render(batch, withhold_published=withhold_published,
                           schedule_titles=schedule_titles)

    async def _fake_invoke(agent, payload, config, attempts=3):
        text = payload["messages"][0].content
        ids = sorted({int(x) for x in re.findall(r'"boq_item_id":\s*(\d+)', text)})
        items = [{"boq_item_id": i, "rate": 1000.0, "rate_source": "derived_estimate",
                  "cost_buildup_note": "5 kg @ Rs 200/kg (build-up)"} for i in ids]
        body = json.dumps({"line_items": items})
        return {"messages": [AIMessage(content=f"COSTING_JSON_START\n{body}\nCOSTING_JSON_END")]}

    async def _fake_buildup(rows, *, breakdown_id, tender_id, tender_context, schedules,
                            published, api_key, settings, overhead_pct, margin_pct, gst_pct,
                            deadline, anonymization_map=None, basis=None):
        from app.core.database import SessionLocal
        priced = []
        for r in rows:
            code = db.query(BOQItem).get(int(r["boq_item_id"])).item_code
            built.append(int(r["boq_item_id"]))
            priced.append(cb.PricedBuildup(
                int(r["boq_item_id"]), _BUILT[code], 0.0, _BUILT[code], 0.0,
                f"{cb.BUILDUP_MARKER} for one unit: labour Rs {_BUILT[code]:,.0f}.",
                "Platform build-up", "medium",
            ))
        s = SessionLocal()
        try:
            cbs.merge_batch_rates(s, breakdown_id, cb.as_batch_lines(priced), platform_verified=True)
        finally:
            s.close()
        return {"rows": len(rows), "priced": len(priced), "failed": 0, "second_looks": 0}

    monkeypatch.setattr(eca, "_render_bidding_schedule_block", _render)
    monkeypatch.setattr(eca, "_ainvoke_with_retry", _fake_invoke)
    monkeypatch.setattr(eca, "_build_costing_agent",
                        lambda *a, **k: {"agent": object(), "callback": None})
    monkeypatch.setattr(eca, "_start_market_research", lambda *a, **k: None)
    monkeypatch.setattr(eca, "_start_rate_basis", lambda *a, **k: None)
    monkeypatch.setattr(eca, "_buildup_api_key", lambda _db: key)
    monkeypatch.setattr(cb, "run_platform_buildup", _fake_buildup)

    async def _no_warm(*a, **k):
        return None

    monkeypatch.setattr(eca, "_warm_prompt_cache", _no_warm)
    state = {"tender_id": tid, "mode": "pipeline", "analysis_result": {},
             "training_context": training_context, "proposal_session_id": None,
             "anonymization_map": {}}
    out = asyncio.run(eca.run_costing_batched_node(state, db))
    return seen, built, out


def _codes(db, ids):
    return {int(db.query(BOQItem).get(i).item_code) for i in ids}


def test_rows_without_firm_data_are_built_up_by_the_platform_not_the_agent(db, tender, monkeypatch):
    seen, built, out = _run_node(db, tender, monkeypatch)
    items = {it["item_code"]: it for it in out["costing_result"]["line_items"]}
    assert _codes(db, seen) == {3}, "the agent was sent a row the platform builds up"
    assert _codes(db, built) == {1, 2}

    # The platform's own figure, never the railway's rate scaled.
    for code in ("1", "2"):
        assert items[code]["rate"] == pytest.approx(_BUILT[code])
        assert items[code]["cost_buildup_note"].startswith(cbs.BASIS_BUILDUP)
        assert items[code]["rate"] != pytest.approx(items[code]["tender_rate"] / 1.25)
    # The row with no published rate keeps the agent's build-up.
    assert items["3"]["rate"] == pytest.approx(1000.0)
    assert items["3"]["cost_buildup_note"].startswith(cbs.BASIS_BUILDUP)
    assert out["costing_result"]["_needs_input_remaining"] == 0
    assert any("built up the cost of 2 row(s)" in a for a in out["costing_result"]["assumptions"])


def test_with_firm_training_data_the_agent_prices_first_and_the_rest_is_built_up(db, tender, monkeypatch):
    seen, built, out = _run_node(db, tender, monkeypatch, training_context="rate card: epoxy Rs 900/set")
    assert _codes(db, seen) == {1, 2, 3}
    # The agent's figures cite no firm rate, so the platform builds those rows up
    # rather than trusting a blind figure or falling back on the railway's.
    assert _codes(db, built) == {1, 2}
    items = {it["item_code"]: it for it in out["costing_result"]["line_items"]}
    assert items["1"]["rate"] == pytest.approx(_BUILT["1"])
    assert items["1"]["cost_buildup_note"].startswith(cbs.BASIS_BUILDUP)


def test_with_no_way_to_build_up_every_row_goes_to_the_agent(db, tender, monkeypatch):
    seen, built, out = _run_node(db, tender, monkeypatch, key=None)
    assert _codes(db, seen) == {1, 2, 3}
    assert built == []
