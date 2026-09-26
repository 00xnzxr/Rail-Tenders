"""Tax-line detection must flag tax PROVISION rows, not work items that merely
mention GST.

Regression: Command Center session 285 (tender 3809). The tender's only BOQ row
was "Supply & Fitment SS Metal RMPU Trough … (Rates are inclusive of GST @ 18%)".
A bare `\\bGST\\b` match flagged it `is_tax_line=True`; the batched costing node
excludes tax rows from `cost_rows`, so ZERO rows were costable, no batch ran,
and the user got a single "[NEEDS RATE]" line with no error anywhere.
"""
import pytest

from app.services.boq_parser_service import _detect_tax_line


# The exact description from tender 3809's only BOQ row (boq_items.id=3784).
S285_ROW = (
    "Supply & Fitment SS Metal RMPU Trough below RMPU in place of FRP Trough. "
    "(Rates are inclusive of GST @ 18%)"
)


@pytest.mark.parametrize("description", [
    S285_ROW,
    # Same qualifier in the shapes IREPS NITs actually use.
    "Conversion Work of Hybrid Coaches to Automobile Carrier Brake Van "
    "(Rates are inclusive of GST @ 18%)",
    "Supply and fitment of brake blocks, including GST",
    "Stripping and Oxy cutting Work in Conversion Work of ICF Coaches "
    "(incl. of GST)",
    "Air Brake fitting in Conversion work (Material + Labour) — excluding GST",
    "Fitment of complete Pull Rod for Hand Brake, inclusive of all taxes and GST",
    # No tax mention at all.
    "Check, Servicing cum CAMC of traction alternator including repairs",
    "Chequered Plate 6 x 682 x 19017 mm (80%) IS:2062 E410 BR",
    "",
    None,
])
def test_work_items_are_not_tax_lines(description):
    assert _detect_tax_line(description) is False


@pytest.mark.parametrize("description", [
    # The shape the model docstring names as the target of this flag.
    "Provision of GST @ 18% on SCHEDULE-A",
    "Provision of GST @ 18% on SCHEDULE-B",
    "GST @ 18%",
    "GST",
    "IGST @ 18% on Schedule B",
    "CGST 9%",
    "SGST 9%",
    "Add: GST @ 18%",
    "Add GST 18% on Schedule A",
    "18. GST @ 18%",
    "Provision of tax",
    "Total GST payable",
])
def test_tax_provision_rows_are_flagged(description):
    assert _detect_tax_line(description) is True
