from app.services.auto_scoring_helpers import compute_segment

KW = dict(discard_below=0.40, bidable_at=0.60, value_threshold=5_000_000)


def test_unscored_is_none():
    assert compute_segment(None, 9_000_000, **KW) is None


def test_low_score_discarded():
    assert compute_segment(0.30, 9_000_000, **KW) == "discarded"


def test_mid_score_not_bidable():
    assert compute_segment(0.50, 9_000_000, **KW) == "not_bidable"


def test_high_score_high_value_to_bid():
    assert compute_segment(0.80, 9_000_000, **KW) == "to_bid"


def test_high_score_low_value_not_bidable():
    assert compute_segment(0.80, 4_000_000, **KW) == "not_bidable"


def test_high_score_unknown_value_to_bid():
    assert compute_segment(0.80, None, **KW) == "to_bid"


def test_boundaries():
    # exactly discard_below -> not_bidable (>= discard_below, < bidable_at)
    assert compute_segment(0.40, 9_000_000, **KW) == "not_bidable"
    # exactly bidable_at with ok value -> to_bid
    assert compute_segment(0.60, 9_000_000, **KW) == "to_bid"
