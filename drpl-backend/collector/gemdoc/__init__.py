"""
DRPL Collector - the GeM bid-document reader.

One PDF per bid carries everything the listing does not: the estimated value,
the EMD, the ePBG, the qualifying conditions, the consignee and the delivery
schedule. ``bidplus.gem.gov.in/showbidDocument/{b_id}`` serves it, and every
field in it is a stated fact printed from a fixed template -- so it is read
here rather than summarised by a model.

    from collector.gemdoc import parse_bid_document
    detail = parse_bid_document(pdf_bytes, bid_number="GEM/2026/B/7977682")
    detail.estimated_value        # 15094560.0
    detail.emd_required           # False
    detail.eligibility_text()     # the block DRPL's tender card renders
"""

from collector.gemdoc.normalize import NormalizedDocument, extract_text
from collector.gemdoc.parser import (
    Consignee,
    GemBidDocument,
    parse_bid_document,
    parse_text,
)

__all__ = [
    "Consignee",
    "GemBidDocument",
    "NormalizedDocument",
    "extract_text",
    "parse_bid_document",
    "parse_text",
]
