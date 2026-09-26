from app.services.auto_scoring_helpers import parse_score_reply, is_below_threshold


def test_parse_normal_reply():
    score, reason = parse_score_reply("MATCH: 90\nREASON: Core railway electrical AMC")
    assert score == 0.9
    assert reason == "Core railway electrical AMC"


def test_parse_clamps_and_handles_percent_sign():
    score, _ = parse_score_reply("MATCH: 120%")
    assert score == 1.0
    score2, _ = parse_score_reply("MATCH: -5")
    assert score2 == 0.0


def test_parse_unparseable_returns_neutral():
    score, reason = parse_score_reply("I cannot score this.")
    assert score == 0.5
    assert reason == ""


def test_below_threshold_true_when_under():
    assert is_below_threshold(4_000_000, 5_000_000) is True


def test_below_threshold_false_when_over_or_equal():
    assert is_below_threshold(5_000_000, 5_000_000) is False
    assert is_below_threshold(9_000_000, 5_000_000) is False


def test_below_threshold_false_when_none():
    assert is_below_threshold(None, 5_000_000) is False
