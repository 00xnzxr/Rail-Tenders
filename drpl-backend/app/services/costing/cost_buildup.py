"""The platform's own cost for a schedule row: an independent build-up.

Sahil's team bids on these tenders with the costing as it comes -- nobody
reviews the sheet -- and a sheet whose rates are the railway's rates tells a
bidder nothing. The Mid-Life Rehabilitation NIT (Liluah, 286 rows) came back
twice that way: first every row was the NIT rate times 0.77 (the prompt had
invited "strip the margin off the published rate"), then every row without a
verified market price was the NIT rate divided by 1.25 (settlement replaced
the model's figure with the railway's estimate, because the model's blind
build-ups ran 0.15x-6.45x of it). Two rows had a market price. The other 284
were the railway's number, scaled.

Those blind build-ups were bad for reasons that can be fixed, not because a
row cannot be costed:

* the model never saw the schedule banner, so it could not know that
  schedule B is "COST OF LABOUR" -- it priced "Web to Drg No LE11185" as a
  steel part in both A (the web, Rs 279.66) and B (the labour to fit it,
  Rs 2,239.21);
* sixty rows went to one call of a small model under a 44k-character prompt,
  with no current prices and no wage rates, and web search had fallen back to
  a library that returned dictionary pages;
* the model did its own arithmetic.

So this module builds each row's cost the way an estimator does, and the
platform -- not the model -- does the sums:

1. **Rate basis**, researched once per costing with the server-side web
   search and verified: the central minimum wages for the work's area (the
   floor every contractor on a railway contract must pay), loaded with the
   statutory costs, and current prices for the material families the tender
   needs (steel, stainless, aluminium, rubber, glass, ...). A figure whose page
   the search did not return is not used; wages then fall back to the last
   central notification the platform holds (`FALLBACK_WAGES`).
2. **Build-up** per row, a few rows of one schedule per call, on a strong
   model with the schedule banner, the tender's scope and the rate basis in
   front of it: what one unit is, the materials in it (quantity x price, the
   price from the rate basis, a cited page, or the model's estimate), the
   labour hours by skill, consumables and transport. The railway's rate is
   withheld so the figure is the work's, not the railway's.
3. **The platform prices it**: hours x the loaded wage for their skill,
   materials at the rate-basis price where the model named it, plus the rest.
4. **Checked against the railway's figure, never replaced by it.** The
   railway's estimate is built from last accepted rates, so a build-up more
   than a factor of two away from its cost (published rate less overhead and
   margin, and less GST where the banner says the rate includes it) has
   usually misread the row -- a set taken for one part, labour taken for
   material. Such a row gets a second, independent look told only that it is
   far above or far below; of the two build-ups the one nearer the benchmark
   stands. Both are the platform's own work; the railway's number is never
   the figure.

Every row states its build-up in plain words, and the Summary counts them.
Switch: `costing.platform_buildup`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

#: How the platform marks a build-up it made, so settlement can tell it from
#: the costing agent's own `derived_estimate` (which it no longer trusts on a
#: row with a published rate). Merged only with `platform_verified=True`.
BUILDUP_MARKER = "Platform cost build-up"

SKILLS = ("unskilled", "semi_skilled", "skilled", "highly_skilled")
_SKILL_WORDS = {
    "unskilled": "unskilled",
    "semi_skilled": "semi-skilled",
    "skilled": "skilled",
    "highly_skilled": "highly skilled",
}

#: Central-sphere minimum wages, construction category, Area A, basic + VDA
#: per day, in force 01-04-2026 to 30-09-2026 (Chief Labour Commissioner).
#: Used only when the costing's own research cannot verify current figures.
FALLBACK_WAGES = {"unskilled": 827.0, "semi_skilled": 918.0, "skilled": 1008.0, "highly_skilled": 1094.0}
FALLBACK_WAGES_SOURCE = (
    "central minimum wages (Chief Labour Commissioner), construction category, "
    "Area A, in force from 01-04-2026"
)
_HOURS_PER_DAY = 8.0

#: A build-up is taken to agree with the railway's figure within this ratio
#: of its cost. The same band settlement holds evidence to.
BAND = (0.5, 2.0)

#: Material families the rate basis can research, chosen per tender from the
#: words in its rows: (key, what to price, unit, trigger).
_MATERIAL_FAMILIES: tuple[tuple[str, str, str, str], ...] = (
    ("ms_steel", "Mild steel plates and rolled sections, IS 2062 E250", "kg",
     r"\bms\b|mild steel|is\s*:?\s*2062|\be250\b|\be350\b|\be410\b|chequered|angle|channel"
     r"|flat\b|plate|bracket|member|web\b|flange|sole bar|pillar|frame"),
    ("corrosion_resistant_steel", "Corrosion-resistant steel plate, IRS M-41 (Corten-A type)", "kg",
     r"irs\s*[-:]?\s*m\s*-?\s*41|corrosion resist|corten|weathering"),
    ("ferritic_stainless", "Ferritic stainless steel sheet/plate X2CrNi12 (IS 409M / 3CR12)", "kg",
     r"x2\s*cr\s*ni\s*12|c-?\s*k\s*201|409\s*m|3cr12|ferritic"),
    ("austenitic_stainless", "Austenitic stainless steel sheet AISI 304", "kg",
     r"\bss\b|stainless|\b304\b"),
    ("aluminium_extrusion", "Aluminium extruded sections 6063-T6, anodised or powder-coated", "kg",
     r"alumin"),
    ("frp_panel", "FRP (glass-fibre reinforced plastic) moulded panel, 3-4 mm", "sqm",
     r"\bfrp\b"),
    ("honeycomb_panel", "Aluminium honeycomb sandwich panel, 25 mm", "sqm",
     r"honey\s*comb"),
    ("epdm_rubber", "EPDM rubber extruded profile", "kg",
     r"rubber|epdm|gasket"),
    ("toughened_glass", "Toughened safety glass, 5-6 mm", "sqm",
     r"glass(?!\s*wool)"),
    ("laminate", "Decorative laminate sheet, 1 mm", "sqm",
     r"laminat|sunmica"),
    ("plywood", "Compreg / marine plywood, 16-19 mm", "sqm",
     r"plywood|compreg|wood"),
    ("glass_wool", "Resin-bonded glass wool insulation, 50 mm", "sqm",
     r"glass\s*wool|thermal insulation"),
    ("paint", "Polyurethane / epoxy paint", "litre",
     r"paint|primer|enamel|powder coat"),
    ("welding_consumables", "Welding electrodes / MIG wire", "kg",
     r"weld|fabricat|member|bracket|frame"),
)
#: Broad per-unit price ranges (Rs) a verified figure must fall in; outside
#: them the page priced a different unit (a tonne, a coil, a sheet).
_PLAUSIBLE_PRICE = {
    "kg": (15.0, 3000.0),
    "sqm": (30.0, 60000.0),
    "litre": (50.0, 5000.0),
}
_WAGE_RANGE = (300.0, 3000.0)


# ── Settings ──────────────────────────────────────────────────────────────────

@dataclass
class BuildupSettings:
    enabled: bool = True
    model: str = "claude-opus-5"
    concurrency: int = 8
    rows_per_call: int = 8
    second_look: bool = True
    loading_pct: float = 30.0
    max_searches_per_call: int = 4


def read_settings(db) -> BuildupSettings:
    from app.services.settings_service import get_setting_value

    s = BuildupSettings()
    try:
        s.enabled = bool(get_setting_value(db, "costing.platform_buildup", True))
        s.model = str(get_setting_value(db, "costing.buildup_model", s.model) or s.model)
        s.concurrency = max(1, min(12, int(get_setting_value(db, "costing.buildup_concurrency", s.concurrency) or s.concurrency)))
        s.rows_per_call = max(1, min(20, int(get_setting_value(db, "costing.buildup_rows_per_call", s.rows_per_call) or s.rows_per_call)))
        s.second_look = bool(get_setting_value(db, "costing.buildup_second_look", True))
        s.loading_pct = float(get_setting_value(db, "costing.labour_statutory_loading_pct", s.loading_pct) or 0.0)
    except Exception as e:
        logger.debug(f"[cost build-up] settings unreadable, defaults used: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return s


# ── The rate basis ────────────────────────────────────────────────────────────

@dataclass
class LabourRate:
    skill: str
    daily_wage: float
    hourly_cost: float
    source: str
    verified: bool


@dataclass
class MaterialPrice:
    key: str
    name: str
    unit: str
    price: float
    source: str
    as_of: str
    verified: bool


@dataclass
class RateBasis:
    labour: dict[str, LabourRate]
    materials: dict[str, MaterialPrice] = field(default_factory=dict)
    location: str = ""
    wage_area: str = ""
    loading_pct: float = 30.0
    wages_source: str = FALLBACK_WAGES_SOURCE

    def labour_words(self) -> str:
        rates = ", ".join(
            f"{_SKILL_WORDS[s]} Rs {self.labour[s].hourly_cost:,.2f}"
            for s in SKILLS if s in self.labour
        )
        return (f"{rates} per hour ({self.wages_source}, plus {self.loading_pct:g}% "
                f"statutory costs: PF, ESI, bonus, leave)")

    def render(self) -> str:
        lines = ["RATE BASIS (verified by the platform; use these prices where they apply)"]
        if self.location:
            lines.append(f"Work location: {self.location}" + (f" -- {self.wage_area}" if self.wage_area else ""))
        lines.append("Labour, cost to the contractor per hour worked (the platform prices your hours at these):")
        for s in SKILLS:
            r = self.labour.get(s)
            if r:
                lines.append(f"  - {s}: Rs {r.hourly_cost:,.2f}/h (daily wage Rs {r.daily_wage:,.0f} + {self.loading_pct:g}% statutory)")
        if self.materials:
            lines.append("Materials, current price ex-GST (key -> price):")
            for m in self.materials.values():
                lines.append(f"  - basis:{m.key} = {m.name}: Rs {m.price:,.2f} per {m.unit}"
                             + (f" ({m.as_of})" if m.as_of else ""))
        else:
            lines.append("Materials: no verified prices this time -- use your best knowledge of current "
                         "Indian prices and set price_basis to \"estimate\".")
        return "\n".join(lines)


def _loaded_hourly(daily: float, loading_pct: float) -> float:
    return round(daily * (1.0 + max(0.0, loading_pct) / 100.0) / _HOURS_PER_DAY, 2)


def fallback_basis(loading_pct: float) -> RateBasis:
    return RateBasis(
        labour={
            s: LabourRate(s, w, _loaded_hourly(w, loading_pct), FALLBACK_WAGES_SOURCE, False)
            for s, w in FALLBACK_WAGES.items()
        },
        loading_pct=loading_pct,
        wages_source=FALLBACK_WAGES_SOURCE,
    )


def material_families_for(rows: list[dict]) -> list[tuple[str, str, str]]:
    """The (key, name, unit) families the rows' words call for."""
    text = " ".join((r.get("description") or "") for r in rows).lower()
    out = []
    for key, name, unit, trigger in _MATERIAL_FAMILIES:
        if re.search(trigger, text):
            out.append((key, name, unit))
    return out


_BASIS_TOOL = {
    "name": "submit_rate_basis",
    "description": "Submit the verified wages and material prices, once, when research is done.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "work_location": {"type": "string"},
            "wage_area": {"type": "string"},
            "wages": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": list(SKILLS)},
                    "daily_wage_inr": {"type": "number"},
                    "source_url": {"type": "string"},
                    "effective": {"type": "string"},
                },
                "required": ["skill", "daily_wage_inr", "source_url", "effective"],
                "additionalProperties": False,
            }},
            "materials": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "price_inr": {"type": "number"},
                    "unit": {"type": "string"},
                    "source_url": {"type": "string"},
                    "as_of": {"type": "string"},
                },
                "required": ["key", "price_inr", "unit", "source_url", "as_of"],
                "additionalProperties": False,
            }},
        },
        "required": ["work_location", "wage_area", "wages", "materials"],
        "additionalProperties": False,
    },
}

_BASIS_SYSTEM = (
    "You research the cost basis an Indian contractor needs to estimate a tender: labour "
    "wages and current material prices. Use web search, then call submit_rate_basis once.\n"
    "Wages: the central-sphere minimum wages (Chief Labour Commissioner) that apply to "
    "contract labour at the work location -- the area classification (A, B or C) of that "
    "city, the latest notification in force, basic plus VDA per day, for unskilled, "
    "semi-skilled, skilled and highly skilled workers. Give the page URL and effective date.\n"
    "Materials: the current price in India, excluding GST, per the unit asked for, from a "
    "dealer or manufacturer price list, a market report or an Indian seller's listing. "
    "Convert to the unit asked (per tonne to per kg, per sheet to per sqm) and give the "
    "page URL and its date. Leave out a material you cannot find a page for.\n"
    "Search for the wage category or material only -- never name a tender, a railway "
    "unit, an organisation or a firm."
)


# ── Talking to the model ──────────────────────────────────────────────────────

_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Tried in order when the configured model cannot be used by this account
#: (not found, not permitted). Without it an account that cannot call the
#: configured model would lose every build-up and put every row back on the
#: railway's figure -- the result this module exists to replace.
_MODEL_FALLBACKS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")


def model_chain(first: str) -> list[str]:
    out = [first] if first else []
    out += [m for m in _MODEL_FALLBACKS if m not in out]
    return out


def model_unavailable(exc: BaseException) -> bool:
    """The account cannot use this model at all (as opposed to a failed call)."""
    try:
        import anthropic
        if isinstance(exc, (anthropic.NotFoundError, anthropic.PermissionDeniedError)):
            return True
        if isinstance(exc, anthropic.BadRequestError):
            msg = str(exc).lower()
            return "model" in msg and ("not found" in msg or "not available" in msg
                                       or "does not exist" in msg or "not supported" in msg)
    except Exception:
        pass
    return False


def _supports_adaptive(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith(("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-6",
                         "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6"))


def _web_search_tool(model: str, max_uses: int) -> dict:
    new = _supports_adaptive(model)
    return {
        "type": "web_search_20260209" if new else "web_search_20250305",
        "name": "web_search",
        "max_uses": max(1, int(max_uses)),
        "user_location": {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"},
    }


@dataclass
class _CallResult:
    tool_input: Optional[dict]
    text: str
    searched_urls: set
    stop_reason: str


async def _call(
    client, *, model: str, system: str, user_blocks: list, tools: list, tool_name: str,
    max_tokens: int, effort: str, agent_name: str, deadline: float,
) -> _CallResult:
    """One model turn, continued through `pause_turn`, with usage logged.

    Returns the submitted tool input (or None) and every URL the server-side
    search returned in the turn -- a cited page is trusted only if it is one
    of those.
    """
    from app.services.ai_service import _log_usage
    from app.services.langchain.provider_config import web_search_requests_of

    messages: list = [{"role": "user", "content": user_blocks}]
    urls: set = set()
    texts: list[str] = []
    # Request features this account or model may refuse with a 400. Each
    # refusal drops the one feature it names and the call is sent again --
    # a feature the platform cannot use must not cost every row its build-up.
    opts = {
        "fallbacks": model.startswith("claude-opus-5"),
        "thinking": _supports_adaptive(model),
        "effort": _supports_adaptive(model),
        "strict": True,
        "search_type": None,  # None = the tool as given
    }
    stop = ""
    for _attempt in range(10):
        left = deadline - time.monotonic()
        if left <= 5:
            raise asyncio.TimeoutError("cost build-up deadline")
        call_tools = []
        for t in tools:
            t = dict(t)
            if not opts["strict"]:
                t.pop("strict", None)
            if opts["search_type"] and str(t.get("type", "")).startswith("web_search"):
                t["type"] = opts["search_type"]
            call_tools.append(t)
        params = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=call_tools,
            messages=messages,
        )
        if opts["thinking"]:
            params["thinking"] = {"type": "adaptive"}
        if opts["effort"]:
            params["output_config"] = {"effort": effort}
        # An explicit timeout: the SDK refuses a non-streaming request whose
        # max_tokens could run past ten minutes unless the caller sets one,
        # and this call is bounded by the costing's own deadline anyway.
        params["timeout"] = max(30.0, min(900.0, left))
        started = time.monotonic()
        try:
            if opts["fallbacks"]:
                resp = await asyncio.wait_for(
                    client.beta.messages.create(**params, betas=[_FALLBACK_BETA], fallbacks="default"),
                    timeout=left,
                )
            else:
                resp = await asyncio.wait_for(client.messages.create(**params), timeout=left)
        except Exception as e:  # noqa: BLE001 -- a refused feature is dropped; anything else is raised
            dropped = _drop_refused_feature(e, opts)
            if not dropped:
                raise
            logger.warning(f"[cost build-up] {model} refused {dropped}; sending the call without it")
            continue
        try:
            u = resp.usage
            await asyncio.to_thread(
                _log_usage, None, "anthropic", model, agent_name,
                {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "web_search_requests": web_search_requests_of(u)},
                int((time.monotonic() - started) * 1000), True,
            )
        except Exception:
            pass
        tool_input = None
        for block in resp.content or []:
            btype = getattr(block, "type", "")
            if btype == "web_search_tool_result":
                content = getattr(block, "content", None)
                if isinstance(content, list):
                    urls.update((getattr(item, "url", "") or "") for item in content)
            elif btype == "text":
                texts.append(getattr(block, "text", "") or "")
                # A page the search returned and the model quoted from.
                for cit in getattr(block, "citations", None) or []:
                    if getattr(cit, "type", "") == "web_search_result_location":
                        urls.add(getattr(cit, "url", "") or "")
            elif btype == "tool_use" and getattr(block, "name", "") == tool_name:
                tool_input = getattr(block, "input", None)
        stop = getattr(resp, "stop_reason", "") or ""
        if tool_input is not None:
            urls.discard("")
            return _CallResult(tool_input if isinstance(tool_input, dict) else None,
                               "".join(texts), urls, stop)
        if stop == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        break
    urls.discard("")
    # No tool call: a JSON object in the text is accepted as the same answer.
    parsed = _json_object("".join(texts))
    return _CallResult(parsed, "".join(texts), urls, stop)


def _drop_refused_feature(exc: BaseException, opts: dict) -> Optional[str]:
    """Turn off the request feature a 400 names, and say which; None when the
    error is not one of those (a credit or rate problem, a real bad request)."""
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if status not in (400, 422) and "invalid_request" not in msg:
        # The fallback option is the one refusal that has arrived as other
        # errors (an SDK or a proxy that does not know the parameter).
        if opts.get("fallbacks") and "fallback" in msg:
            opts["fallbacks"] = False
            return "the refusal fallbacks option"
        return None
    if "credit balance" in msg:
        return None
    if opts.get("fallbacks") and "fallback" in msg:
        opts["fallbacks"] = False
        return "the refusal fallbacks option"
    if opts.get("strict") and "strict" in msg:
        opts["strict"] = False
        return "strict tool schemas"
    if opts.get("search_type") is None and "web_search" in msg:
        opts["search_type"] = "web_search_20250305"
        return "the dynamic web search tool"
    if opts.get("effort") and ("effort" in msg or "output_config" in msg):
        opts["effort"] = False
        return "the effort setting"
    if opts.get("thinking") and "thinking" in msg:
        opts["thinking"] = False
        return "adaptive thinking"
    return None


def _json_object(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


async def research_rate_basis(
    client, model: str, *, context: str, families: list[tuple[str, str, str]],
    loading_pct: float, deadline: float, max_searches: int = 10,
) -> RateBasis:
    """Wages and material prices, each kept only when its page is one the
    search returned and the figure is in a plausible range; wages that fail
    fall back to `FALLBACK_WAGES`."""
    basis = fallback_basis(loading_pct)
    wanted = "\n".join(f"  - {k}: {name}, price per {unit}" for k, name, unit in families) or "  (none)"
    user = (
        f"TENDER CONTEXT\n{context}\n\n"
        f"Today's date: {date.today().isoformat()}.\n"
        "Find (1) the central minimum wages that apply at the work location, and "
        f"(2) the current price of each material below:\n{wanted}\n"
        "Then call submit_rate_basis."
    )
    res = None
    for m in model_chain(model):
        try:
            res = await _call(
                client, model=m, system=_BASIS_SYSTEM,
                user_blocks=[{"type": "text", "text": user}],
                tools=[_web_search_tool(m, max_searches), _BASIS_TOOL],
                tool_name="submit_rate_basis", max_tokens=16000, effort="medium",
                agent_name="cost_buildup_basis", deadline=deadline,
            )
            break
        except Exception as e:  # noqa: BLE001 -- the fallback basis stands
            if model_unavailable(e):
                logger.warning(f"[cost build-up] {m} is not available to this account; trying the next model")
                continue
            logger.warning(f"[cost build-up] rate basis research failed ({type(e).__name__}: {e}); "
                           f"using the last central wage notification")
            return basis
    if res is None:
        return basis
    data = res.tool_input or {}
    basis.location = str(data.get("work_location") or "")[:160]
    basis.wage_area = str(data.get("wage_area") or "")[:120]
    verified_wages: dict[str, tuple[float, str, str]] = {}
    for w in data.get("wages") or []:
        skill = str(w.get("skill") or "")
        daily = _num(w.get("daily_wage_inr"))
        url = str(w.get("source_url") or "").strip()
        if skill in SKILLS and daily and _WAGE_RANGE[0] <= daily <= _WAGE_RANGE[1] and url in res.searched_urls:
            verified_wages[skill] = (daily, url, str(w.get("effective") or "")[:40])
    if len(verified_wages) == len(SKILLS):
        eff = next(iter(verified_wages.values()))[2]
        src_url = next(iter(verified_wages.values()))[1]
        basis.wages_source = (
            f"central minimum wages{' for ' + basis.wage_area if basis.wage_area else ''}"
            f"{', in force from ' + eff if eff else ''} ({src_url})"
        )
        basis.labour = {
            s: LabourRate(s, d, _loaded_hourly(d, loading_pct), u, True)
            for s, (d, u, _e) in verified_wages.items()
        }
    names = {k: (name, unit) for k, name, unit in families}
    for m in data.get("materials") or []:
        key = str(m.get("key") or "")
        price = _num(m.get("price_inr"))
        url = str(m.get("source_url") or "").strip()
        if key not in names or not price or url not in res.searched_urls:
            continue
        name, unit = names[key]
        lo, hi = _PLAUSIBLE_PRICE.get(unit, (0.01, 1e9))
        if not lo <= price <= hi or _unit(str(m.get("unit") or unit)) != _unit(unit):
            continue
        basis.materials[key] = MaterialPrice(key, name, unit, round(price, 2), url,
                                             str(m.get("as_of") or "")[:40], True)
    logger.info(
        f"[cost build-up] rate basis: wages {'verified' if len(verified_wages) == len(SKILLS) else 'from the last central notification'}"
        f" ({basis.location or 'location not found'}); {len(basis.materials)} of {len(families)} "
        f"material price(s) verified"
    )
    return basis


# ── The build-up itself ───────────────────────────────────────────────────────

_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "boq_item_id": {"type": "integer"},
        "work_type": {"type": "string", "enum": ["material_supply", "labour_only", "supply_and_fit", "bought_out_item"]},
        "one_unit_is": {"type": "string"},
        "materials": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "quantity": {"type": "number"},
                "unit": {"type": "string"},
                "unit_price_inr": {"type": "number"},
                "price_basis": {"type": "string"},
            },
            "required": ["item", "quantity", "unit", "unit_price_inr", "price_basis"],
            "additionalProperties": False,
        }},
        "labour": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": list(SKILLS)},
                "hours": {"type": "number"},
                "task": {"type": "string"},
            },
            "required": ["skill", "hours", "task"],
            "additionalProperties": False,
        }},
        "other_costs": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "string"}, "amount_inr": {"type": "number"}},
            "required": ["item", "amount_inr"],
            "additionalProperties": False,
        }},
        "assumptions": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["boq_item_id", "work_type", "one_unit_is", "materials", "labour",
                 "other_costs", "assumptions", "confidence"],
    "additionalProperties": False,
}

_BUILDUP_TOOL = {
    "name": "submit_cost_buildups",
    "description": "Submit the cost build-up of every row, once, when all rows are done.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"rows": {"type": "array", "items": _ROW_SCHEMA}},
        "required": ["rows"],
        "additionalProperties": False,
    },
}

BUILDUP_SYSTEM = """You are the estimating engineer of an Indian contractor that bids on Indian Railways workshop and works contracts. For each tender row you are given, you build up what ONE unit costs the contractor to deliver: the firm's own cost, before overhead, profit and GST.

READING A ROW
- The schedule heading says what its rows price. "Cost of Material" / "Materials": the contractor supplies the item (makes or buys it) and delivers it; no fitting labour unless the row says so. "Cost of Labour" / "Labour": the contractor's workmen do the job the row names -- dismantle, cut out, repair, fit, weld, finish, test -- and the item itself is NOT in the cost; it is priced in a material schedule. "Material & Labour", "Supply and fitment", "Supply and installation": both.
- One unit is what the Unit column says: one Number, one Set (everything the description lists), one Coach (all of that work on one coach), one Kg / Metre / Sqm.
- A drawing number (RCF / ICF / RDSO: LE..., LW..., LA..., CG..., MI..., 2.10113...) names a part made to that drawing. You do not have the drawing: work out what the part is and where it sits in an LHB or ICF coach, estimate its material, dimensions and weight, and state what you assumed.
- An item made to an RDSO / RCF / EDTS specification by approved vendors (light fittings, cables, transformers, battery boxes, detectors, valves, appliances) costs more than a generic product of the same kind: price the approved-vendor class.

BUILDING THE COST
- materials: every material and bought-out part in one unit, each with quantity, unit and price per unit. Where the RATE BASIS lists a price for that material, use it and set price_basis to its key (for example "basis:ms_steel"). A price you found with web search: set price_basis to the page URL. Otherwise price_basis is "estimate". Include cutting wastage (5-10%) in the quantity.
- Weights: steel plate kg = length (m) x width (m) x thickness (mm) x 7.85; stainless 7.9; aluminium 2.7.
- labour: hours by skill (unskilled, semi_skilled, skilled, highly_skilled) for all the work one unit needs -- marking, cutting, forming, welding, grinding, fitting, finishing, testing, handling. Do not price labour in rupees: the platform prices your hours at the RATE BASIS wages.
- other_costs: consumables (welding wire and electrodes, gases, grinding discs, paint touch-up), small tools and tackles, testing, packing and transport to the workshop, in rupees for one unit.
- No overhead, profit, GST or contingency anywhere: the platform adds them.

RULES
- Every row gets a build-up. When a description is thin, make the most reasonable assumption for an LHB / ICF coach and say so in assumptions.
- The railway's own rate for each row is deliberately not shown to you. Do not guess it or work back from it: build the cost from the work.
- Web search only for current prices of bought-out items and materials the RATE BASIS does not cover, a few searches at most. Search for the product or material only -- never name a tender, a railway unit, an organisation or a firm.
- assumptions: one or two sentences a bidder can check -- the size, weight, scope or hours you assumed.
- When every row is done, call submit_cost_buildups once with all of them."""


@dataclass
class PricedBuildup:
    boq_item_id: int
    unit_cost: float
    materials_total: float
    labour_total: float
    other_total: float
    note: str
    source_ref: str
    confidence: str
    ratio: Optional[float] = None
    first_cost: Optional[float] = None  # the first build-up, when a second look replaced it

    @property
    def in_band(self) -> bool:
        return self.ratio is None or BAND[0] <= self.ratio <= BAND[1]


_UNIT_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "m": "m", "mtr": "m", "mtrs": "m", "metre": "m", "metres": "m", "meter": "m", "meters": "m", "rm": "m",
    "sqm": "sqm", "sq.m": "sqm", "sq m": "sqm", "m2": "sqm", "sq. m": "sqm", "square metre": "sqm",
    "litre": "litre", "litres": "litre", "liter": "litre", "l": "litre", "ltr": "litre", "ltrs": "litre",
}


def _unit(u: str) -> str:
    """A unit as a comparable word: "per kg", "Rs/kg", "KGS." -> "kg"."""
    t = (u or "").strip().lower()
    t = re.sub(r"^(?:rs\.?|inr|₹)\s*", "", t)
    t = re.sub(r"^(?:per|/)\s*", "", t)
    t = re.sub(r"[^a-z0-9. ]", "", t).strip(" .")
    return _UNIT_ALIASES.get(t, t)


def _money(v: float) -> str:
    return f"Rs {v:,.2f}" if abs(v) < 100 else f"Rs {v:,.0f}"


def price_buildup(row: dict, answer: dict, basis: RateBasis, searched_urls: set) -> Optional[PricedBuildup]:
    """The platform's arithmetic on the model's build-up, and its plain-words
    note. None when the build-up prices nothing."""
    try:
        bid = int(row["boq_item_id"])
    except (KeyError, TypeError, ValueError):
        return None
    unit_label = (row.get("unit") or "unit").strip()
    mat_parts: list[tuple[str, float]] = []
    mats_total = 0.0
    cited: list[str] = []
    for m in answer.get("materials") or []:
        qty = _num(m.get("quantity"))
        price = _num(m.get("unit_price_inr"))
        if qty is None or price is None or qty < 0 or price < 0:
            continue
        item = str(m.get("item") or "material").strip()[:90]
        unit = str(m.get("unit") or "").strip()[:12]
        basis_ref = str(m.get("price_basis") or "").strip()
        words = "estimated price"
        key = basis_ref[6:] if basis_ref.lower().startswith("basis:") else ""
        if key and key in basis.materials and _unit(unit) == _unit(basis.materials[key].unit):
            price = basis.materials[key].price
            words = "rate basis"
            cited.append(basis.materials[key].source)
        elif basis_ref.lower().startswith("http"):
            if basis_ref in searched_urls:
                words = "market price"
                cited.append(basis_ref)
            else:
                words = "estimated price"
        amount = qty * price
        mats_total += amount
        mat_parts.append((f"{qty:g} {unit} {item} @ {_money(price)} = {_money(amount)} ({words})", amount))
    hours: dict[str, float] = {}
    for lab in answer.get("labour") or []:
        skill = str(lab.get("skill") or "")
        h = _num(lab.get("hours"))
        if skill in SKILLS and h is not None and 0 <= h <= 50000:
            hours[skill] = hours.get(skill, 0.0) + h
    labour_total = sum(h * basis.labour[s].hourly_cost for s, h in hours.items() if s in basis.labour)
    other_parts: list[str] = []
    other_total = 0.0
    for o in answer.get("other_costs") or []:
        amt = _num(o.get("amount_inr"))
        if amt is None or amt < 0:
            continue
        other_total += amt
        other_parts.append(f"{str(o.get('item') or 'other').strip()[:50]} {_money(amt)}")
    unit_cost = round(mats_total + labour_total + other_total, 2)
    if unit_cost <= 0:
        return None

    work = {
        "material_supply": "material supplied", "labour_only": "labour only",
        "supply_and_fit": "supply and fitting", "bought_out_item": "bought-out item",
    }.get(str(answer.get("work_type") or ""), "")
    head = (
        f"{BUILDUP_MARKER} for one {unit_label}"
        + (f" ({work})" if work else "")
        + f": materials {_money(mats_total)} + labour {_money(labour_total)} + other "
        f"{_money(other_total)} = {_money(unit_cost)}."
    )
    lines = [head]
    one = str(answer.get("one_unit_is") or "").strip()
    if one:
        lines.append(f"One unit: {one[:200]}")
    if mat_parts:
        shown = sorted(mat_parts, key=lambda p: -p[1])[:6]
        rest = mat_parts[6:] and sum(a for _t, a in sorted(mat_parts, key=lambda p: -p[1])[6:])
        lines.append("Materials: " + "; ".join(t for t, _a in shown)
                     + (f"; and {len(mat_parts) - 6} more ({_money(rest)})" if len(mat_parts) > 6 else "") + ".")
    if hours:
        hs = " + ".join(
            f"{h:g} h {_SKILL_WORDS[s]} @ Rs {basis.labour[s].hourly_cost:,.2f}/h"
            for s, h in hours.items() if s in basis.labour
        )
        lines.append(
            f"Labour: {hs} = {_money(labour_total)} ({basis.wages_source}, plus "
            f"{basis.loading_pct:g}% statutory costs: PF, ESI, bonus, leave)."
        )
    if other_parts:
        lines.append("Other: " + ", ".join(other_parts) + ".")
    assumptions = str(answer.get("assumptions") or "").strip()
    if assumptions:
        lines.append(f"Assumed: {assumptions[:400]}")
    note = "\n".join(lines)
    uniq = list(dict.fromkeys(c for c in cited if c))
    source_ref = (
        f"Platform build-up: materials {_money(mats_total)} + labour {_money(labour_total)} "
        f"+ other {_money(other_total)}"
        + (f"; prices: {', '.join(uniq[:3])}" if uniq else "")
        + f"; wages: {basis.wages_source}"
    )[:1000]
    conf = str(answer.get("confidence") or "medium")
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    return PricedBuildup(bid, unit_cost, round(mats_total, 2), round(labour_total, 2),
                         round(other_total, 2), note, source_ref, conf)


def benchmark_cost(
    published: Optional[float], *, overhead_pct: float, margin_pct: float,
    gst_pct: float = 0.0, taxes_inclusive: Optional[bool] = None,
) -> Optional[float]:
    """What the railway's published rate implies the work costs: the rate less
    the org's overhead and margin, and less GST when the schedule's banner says
    its rates include taxes. None when nothing is published."""
    try:
        p = float(published)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    ref = p / (1.0 + (float(overhead_pct or 0) + float(margin_pct or 0)) / 100.0)
    if taxes_inclusive:
        ref /= (1.0 + float(gst_pct or 0) / 100.0)
    return ref


def choose(first: Optional[PricedBuildup], second: Optional[PricedBuildup]) -> Optional[PricedBuildup]:
    """Of the platform's own build-ups for a row, the one to stand: a second
    look inside the band, else the one nearer the benchmark."""
    cands = [c for c in (second, first) if c is not None]
    if not cands:
        return None
    inside = [c for c in cands if c.in_band]
    pick = inside[0] if inside else min(cands, key=lambda c: abs(math.log(c.ratio)) if c.ratio else 0.0)
    if pick is second and first is not None:
        pick.first_cost = first.unit_cost
    return pick


# ── One call: a few rows of the tender ────────────────────────────────────────

def _row_block(row: dict, anonymization_map: Optional[dict]) -> str:
    from app.services.costing.market_price_research import anonymize_description

    desc = re.sub(r"\s+", " ", (row.get("description") or "")).strip().strip('"')
    desc = anonymize_description(desc, anonymization_map)[:1500]
    qty = row.get("quantity")
    return (
        f"- boq_item_id {row.get('boq_item_id')} | Sr {row.get('sr_no')} | Unit: "
        f"{(row.get('unit') or 'unit').strip()} | Tender quantity: {qty if qty is not None else 'not stated'}\n"
        f"  {desc}"
    )


def render_rows(rows: list[dict], schedules: dict, anonymization_map: Optional[dict]) -> str:
    """The rows grouped under their schedule banners."""
    out: list[str] = []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("schedule_name") or "").strip().upper(), []).append(r)
    for code, items in groups.items():
        ctx = schedules.get(code)
        if ctx is not None:
            out.append(f"SCHEDULE {code}: {ctx.title}")
            out.append(f"  (this schedule prices {ctx.plain_work()})")
        else:
            out.append(f"SCHEDULE {code or '(none)'}")
        out.extend(_row_block(r, anonymization_map) for r in items)
        out.append("")
    return "\n".join(out).strip()


def _same_item_key(row: dict) -> tuple:
    """Rows that name the same item, in the same schedule and unit, cost the
    same: the Mid-Life NIT lists "Door Arrangements R.H and L.H ... LA51100"
    twice in schedule A at Rs 61,360 each."""
    desc = re.sub(r"[^a-z0-9]+", "", (row.get("description") or "").lower())
    return ((row.get("schedule_name") or "").strip().upper(), desc,
            (row.get("unit") or "").strip().lower())


def distinct_rows(rows: list[dict]) -> tuple[list[dict], dict[int, list[int]]]:
    """One row per distinct item, and {kept boq_item_id: [its twins' ids]}."""
    kept: dict[tuple, dict] = {}
    twins: dict[int, list[int]] = {}
    for r in rows:
        if r.get("boq_item_id") is None:
            continue
        key = _same_item_key(r)
        first = kept.get(key)
        if first is None or not key[1]:
            kept[key if key[1] else (key, r["boq_item_id"])] = r
            twins.setdefault(int(r["boq_item_id"]), [])
        else:
            twins.setdefault(int(first["boq_item_id"]), []).append(int(r["boq_item_id"]))
    return list(kept.values()), twins


def group_rows(rows: list[dict], per_call: int) -> list[list[dict]]:
    """Rows in calls of at most `per_call`, one schedule per call, keeping a
    call's descriptions to a readable length."""
    out: list[list[dict]] = []
    by_sched: dict[str, list[dict]] = {}
    for r in rows:
        by_sched.setdefault((r.get("schedule_name") or "").strip().upper(), []).append(r)
    for items in by_sched.values():
        cur: list[dict] = []
        chars = 0
        for r in items:
            n = len(r.get("description") or "")
            if cur and (len(cur) >= per_call or chars + n > 6000):
                out.append(cur)
                cur, chars = [], 0
            cur.append(r)
            chars += n
        if cur:
            out.append(cur)
    return out


async def _estimate(
    client, model: str, *, context_block: str, rows: list[dict], schedules: dict,
    anonymization_map: Optional[dict], max_searches: int, deadline: float,
    feedback: Optional[dict] = None,
) -> tuple[dict, set]:
    """{boq_item_id: answer} for the rows the model built up, and the URLs
    its searches returned."""
    body = render_rows(rows, schedules, anonymization_map)
    if feedback:
        fb = "\n".join(
            f"- boq_item_id {bid}: your first build-up came to Rs {cost:,.2f} per unit, "
            f"which is FAR {direction} the benchmark."
            for bid, (cost, direction) in feedback.items()
        )
        body = (
            "SECOND LOOK. The platform compared your first build-up of each row below with the "
            "benchmark it holds for that row (which you do not see); they disagree by more than "
            f"a factor of two:\n{fb}\n\n"
            "Re-derive each build-up from scratch. Check, in this order: what one unit is (a Set "
            "can be a whole coach set; Per Coach is all that work on one coach); whether the row "
            "is material, labour or both (read the schedule heading); the full scope the "
            "description implies; sizes and weights; hours. Do not scale your first answer -- "
            "rebuild it. If after checking you are sure the first build-up was right, give it "
            "again and say why in assumptions.\n\n" + body
        )
    user_blocks = [
        {"type": "text", "text": context_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"ROWS TO BUILD UP ({len(rows)})\n{body}\n\n"
                                 "Build up every row above, then call submit_cost_buildups."},
    ]
    res = await _call(
        client, model=model, system=BUILDUP_SYSTEM, user_blocks=user_blocks,
        tools=[_web_search_tool(model, max_searches), _BUILDUP_TOOL],
        tool_name="submit_cost_buildups", max_tokens=32000,
        effort="high", agent_name="cost_buildup", deadline=deadline,
    )
    wanted = {int(r["boq_item_id"]) for r in rows if r.get("boq_item_id") is not None}
    answers: dict = {}
    for a in (res.tool_input or {}).get("rows") or []:
        try:
            bid = int(a.get("boq_item_id"))
        except (TypeError, ValueError):
            continue
        if bid in wanted and bid not in answers:
            answers[bid] = a
    return answers, res.searched_urls


# ── Orchestration ─────────────────────────────────────────────────────────────

def context_block(tender_context: str, basis: RateBasis) -> str:
    return f"TENDER CONTEXT\n{tender_context.strip()}\n\n{basis.render()}"


def as_batch_lines(priced: list[PricedBuildup]) -> list[dict]:
    """The build-ups in the shape `merge_batch_rates` takes."""
    out = []
    for p in priced:
        note = p.note
        if p.first_cost is not None:
            note += f"\nRe-derived: a first build-up of {_money(p.first_cost)} was checked again from scratch."
        out.append({
            "boq_item_id": p.boq_item_id,
            "rate": p.unit_cost,
            "rate_source": "derived_estimate",
            "source_ref": p.source_ref,
            "cost_buildup_note": note,
            "confidence": p.confidence,
            "needs_input": False,
            "source_url": None,
        })
    return out


async def run_platform_buildup(
    rows: list[dict],
    *,
    breakdown_id: int,
    tender_id,
    tender_context: str,
    schedules: dict,
    published: dict,
    api_key: str,
    settings: BuildupSettings,
    overhead_pct: float,
    margin_pct: float,
    gst_pct: float,
    deadline: float,
    anonymization_map: Optional[dict] = None,
    basis: Optional[RateBasis] = None,
) -> dict:
    """Build up and merge every row it can before `deadline` (a monotonic
    time). `published` maps boq_item_id -> the skeleton's tender_rate.
    Returns counts; a row it could not finish is left as it was."""
    import anthropic

    from app.core.database import SessionLocal
    from app.services import cost_breakdown_service as cbs

    stats = {"rows": len(rows), "priced": 0, "second_looks": 0, "outside_band": 0,
             "failed": 0, "calls": 0}
    if not rows:
        return stats
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
    t0 = time.monotonic()
    if basis is None:
        basis = await research_rate_basis(
            client, settings.model, context=tender_context,
            families=material_families_for(rows), loading_pct=settings.loading_pct,
            deadline=min(deadline, time.monotonic() + 240),
        )
    ctx_block = context_block(tender_context, basis)
    slots = asyncio.Semaphore(settings.concurrency)
    chain = model_chain(settings.model)
    current = {"model": chain[0]}
    # One writer at a time: merges run off the event loop, and SQLite (tests,
    # local runs) takes one writer; on Postgres it costs nothing.
    merge_lock = asyncio.Lock()
    by_id = {int(r["boq_item_id"]): r for r in rows if r.get("boq_item_id") is not None}

    def _ratio(bid: int, cost: float) -> Optional[float]:
        row = by_id.get(bid) or {}
        ctx = schedules.get((row.get("schedule_name") or "").strip().upper())
        ref = benchmark_cost(
            published.get(bid), overhead_pct=overhead_pct, margin_pct=margin_pct,
            gst_pct=gst_pct, taxes_inclusive=getattr(ctx, "taxes_inclusive", None),
        )
        return (cost / ref) if ref else None

    def _merge(priced: list[PricedBuildup]) -> None:
        if not priced:
            return
        if time.monotonic() > deadline + 5:
            # The costing has moved on to finalising (or its outer timer has
            # fired and a recovery is settling the sheet): a late write would
            # land on a row already settled.
            logger.warning(f"[cost build-up] tender {tender_id}: {len(priced)} build-up(s) "
                           f"finished after the deadline and were not written")
            return
        session = SessionLocal()
        try:
            cbs.merge_batch_rates(session, breakdown_id, as_batch_lines(priced), platform_verified=True)
        except Exception as e:
            session.rollback()
            logger.warning(f"[cost build-up] tender {tender_id}: merge failed: {e}")
        finally:
            session.close()

    async def run_group(group: list[dict], feedback: Optional[dict] = None) -> dict:
        async with slots:
            if deadline - time.monotonic() < 45:
                return {}
            stats["calls"] += 1
            attempt = 0
            while True:
                model = current["model"]
                try:
                    answers, urls = await _estimate(
                        client, model, context_block=ctx_block, rows=group,
                        schedules=schedules, anonymization_map=anonymization_map,
                        max_searches=settings.max_searches_per_call, deadline=deadline,
                        feedback=feedback,
                    )
                    break
                except asyncio.TimeoutError:
                    return {}
                except Exception as e:  # noqa: BLE001 -- one group's failure is its own
                    if model_unavailable(e):
                        nxt = chain[chain.index(model) + 1] if model in chain[:-1] else None
                        if nxt is None:
                            return {}
                        if current["model"] == model:
                            logger.warning(f"[cost build-up] tender {tender_id}: {model} is not "
                                           f"available to this account; building up on {nxt}")
                            current["model"] = nxt
                        continue
                    attempt += 1
                    logger.warning(f"[cost build-up] tender {tender_id}: a call for "
                                   f"{len(group)} row(s) failed ({type(e).__name__}: {e})"
                                   + ("; retrying" if attempt == 1 else ""))
                    if attempt >= 2 or deadline - time.monotonic() < 90:
                        return {}
            out = {}
            for r in group:
                bid = int(r["boq_item_id"])
                a = answers.get(bid)
                p = price_buildup(r, a, basis, urls) if a else None
                if p is not None:
                    p.ratio = _ratio(bid, p.unit_cost)
                    out[bid] = p
            return out

    # A row that names the same item as another in its schedule is built up
    # once and given the same figure.
    distinct, twins = distinct_rows(rows)

    def _with_twins(priced: list[PricedBuildup]) -> list[PricedBuildup]:
        out: list[PricedBuildup] = []
        for p in priced:
            out.append(p)
            sr = (by_id.get(p.boq_item_id) or {}).get("sr_no")
            for twin in twins.get(p.boq_item_id, []):
                out.append(dataclasses.replace(
                    p, boq_item_id=twin, ratio=_ratio(twin, p.unit_cost),
                    note=p.note + (f"\nThe same item as Sr {sr} of this schedule, built up once."
                                   if sr is not None else ""),
                ))
        return out

    groups = group_rows(distinct, settings.rows_per_call)
    first: dict[int, PricedBuildup] = {}

    async def first_pass(g):
        got = await run_group(g)
        first.update(got)
        # Saved as it lands: a costing cut short keeps every row already built.
        async with merge_lock:
            await asyncio.to_thread(_merge, _with_twins(list(got.values())))

    await asyncio.gather(*(first_pass(g) for g in groups))

    final: dict[int, PricedBuildup] = dict(first)
    outliers = {bid: p for bid, p in first.items() if not p.in_band}
    if outliers and settings.second_look and deadline - time.monotonic() > 150:
        feedback_groups = group_rows([by_id[b] for b in outliers], max(1, min(6, settings.rows_per_call)))
        stats["second_looks"] = len(outliers)

        async def second_pass(g):
            fb = {int(r["boq_item_id"]): (outliers[int(r["boq_item_id"])].unit_cost,
                                          "ABOVE" if outliers[int(r["boq_item_id"])].ratio > 1 else "BELOW")
                  for r in g}
            got = await run_group(g, feedback=fb)
            changed = []
            for bid, p2 in got.items():
                pick = choose(outliers.get(bid), p2)
                if pick is not None and pick is not outliers.get(bid):
                    final[bid] = pick
                    changed.append(pick)
            async with merge_lock:
                await asyncio.to_thread(_merge, _with_twins(changed))

        await asyncio.gather(*(second_pass(g) for g in feedback_groups))

    stats["priced"] = len(final) + sum(len(twins.get(b, [])) for b in final)
    stats["failed"] = len(rows) - stats["priced"]
    stats["outside_band"] = sum(1 for p in final.values() if not p.in_band)
    stats["wages_verified"] = all(r.verified for r in basis.labour.values())
    stats["materials_verified"] = len(basis.materials)
    logger.info(
        f"[cost build-up] tender {tender_id}: {stats['priced']} of {len(rows)} row(s) built up "
        f"in {time.monotonic() - t0:.0f}s ({stats['calls']} call(s)); {stats['second_looks']} "
        f"checked twice, {stats['outside_band']} still more than 2x from the railway's figure, "
        f"{stats['failed']} not finished"
    )
    return stats
