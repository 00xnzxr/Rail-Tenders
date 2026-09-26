import os
import pytest
from app.services.nit_link_fetch_service import (
    is_allowed_host, _http_get, is_document_response,
)

# Opt-in: only runs when RUN_NET_TESTS=1 (keeps CI offline-safe).
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_NET_TESTS") != "1",
    reason="network test — set RUN_NET_TESTS=1 to run",
)

# A known public IREPS NIT PDF (static URL). If it 404s over time, swap for a
# fresh one from `SELECT nit_document_links FROM tenders WHERE portal='ireps'`.
_LIVE_URL = "https://www.ireps.gov.in/ireps/works/pdfdocs/082026/89872774/viewNitPdf_5425319.pdf"


def test_live_ireps_nit_is_fetchable_pdf():
    assert is_allowed_host(_LIVE_URL)
    got = _http_get(_LIVE_URL, timeout=25, max_bytes=10 * 1024 * 1024)
    assert got is not None, "network fetch returned None"
    status, ctype, body = got
    assert is_document_response(status, ctype, body[:8], len(body),
                                max_bytes=10 * 1024 * 1024)
    assert body.startswith(b"%PDF-")
