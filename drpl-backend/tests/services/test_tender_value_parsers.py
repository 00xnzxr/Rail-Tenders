from datetime import datetime
from app.services.tender_value_parsers import parse_indian_currency, parse_tender_date


def test_currency_plain_with_symbol_and_commas():
    assert parse_indian_currency("₹ 1,12,55,870.40") == 11255870.40

def test_currency_rs_with_trailing_slash():
    assert parse_indian_currency("Rs. 2,25,000/-") == 225000.0

def test_currency_lakh_unit():
    assert parse_indian_currency("INR 45.6 Lakh") == 4560000.0

def test_currency_crore_unit():
    assert parse_indian_currency("2.5 Cr") == 25000000.0

def test_currency_bare_number():
    assert parse_indian_currency("349603.68") == 349603.68

def test_currency_none_and_garbage():
    assert parse_indian_currency(None) is None
    assert parse_indian_currency("") is None
    assert parse_indian_currency("as per tender") is None
    assert parse_indian_currency("N/A") is None

def test_currency_digit_bearing_prose_returns_none():
    for s in ["valid for 90 days", "Schedule A-1", "2 bidders",
              "GST 18%", "Tender No 2026/45/12", "Page 1 of 5", "18%"]:
        assert parse_indian_currency(s) is None, s

def test_currency_zero_is_none():
    assert parse_indian_currency("Rs 0") is None
    assert parse_indian_currency("0") is None

def test_currency_bare_integer_without_signal_is_none():
    # A plain integer with no ₹/Rs/comma/lakh/2dp signal is not trusted.
    assert parse_indian_currency("225000") is None

def test_currency_grouped_integer_ok():
    assert parse_indian_currency("2,25,000") == 225000.0

def test_currency_standalone_cr_abbrev_is_none():
    for s in ["Contract Ref: CR/2026/45", "Cr. No. 45", "CR 12", "as per corrigendum Cr 3"]:
        assert parse_indian_currency(s) is None, s

def test_currency_lakh_crore_still_parse():
    assert parse_indian_currency("INR 45.6 Lakh") == 4560000.0
    assert parse_indian_currency("2.5 Cr") == 25000000.0
    assert parse_indian_currency("2.5 crore") == 25000000.0

def test_date_slash_with_time_and_hrs():
    d = parse_tender_date("21/07/2026 15:00 hrs")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21 and d.hour == 15

def test_date_dd_mon_yyyy():
    d = parse_tender_date("21-Jul-2026")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21

def test_date_iso():
    d = parse_tender_date("2026-07-21T15:00:00")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21

def test_date_none_and_garbage():
    assert parse_tender_date(None) is None
    assert parse_tender_date("") is None
    assert parse_tender_date("refer portal") is None
