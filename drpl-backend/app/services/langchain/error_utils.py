"""
DRPL Backend - Error Utilities
User-friendly error message formatting for LLM/API errors.

Converts raw API exceptions (429 rate limit, 503 overloaded, auth failures, etc.)
into clean, user-facing messages. Also provides helpers for error classification
used by retry logic in the execution and streaming layers.
"""

import re
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern groups for error classification
# ---------------------------------------------------------------------------

_RATE_LIMIT_PATTERNS = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"\b429\b"),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"resource.?exhausted", re.IGNORECASE),
    re.compile(r"tokens per minute", re.IGNORECASE),
    re.compile(r"requests per minute", re.IGNORECASE),
]

_OVERLOADED_PATTERNS = [
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"\b529\b"),
    re.compile(r"\b503\b"),
    re.compile(r"service.?unavailable", re.IGNORECASE),
]

_AUTH_PATTERNS = [
    re.compile(r"\b401\b"),
    re.compile(r"\b403\b"),
    re.compile(r"authentication", re.IGNORECASE),
    re.compile(r"invalid.?api.?key", re.IGNORECASE),
    re.compile(r"permission.?denied", re.IGNORECASE),
]

_MISSING_API_KEY_PATTERNS = [
    re.compile(r"api.?key.*not.?(configured|set|found)", re.IGNORECASE),
    re.compile(r"no.*api.?key.*(configured|set|found)", re.IGNORECASE),
    re.compile(r"missing.*api.?key", re.IGNORECASE),
    re.compile(r"pinned provider", re.IGNORECASE),
]

_MODEL_NOT_FOUND_PATTERNS = [
    re.compile(r"model.?not.?found", re.IGNORECASE),
    re.compile(r"model.*does\s*not\s*exist", re.IGNORECASE),
    re.compile(r"unknown\s*model", re.IGNORECASE),
    re.compile(r"invalid\s*model", re.IGNORECASE),
    re.compile(r"model_not_found", re.IGNORECASE),
    re.compile(r"the\s*model\s*`[^`]+`\s*does\s*not\s*exist", re.IGNORECASE),
]

_BILLING_PATTERNS = [
    re.compile(r"credit.?balance.?(is\s*)?too.?low", re.IGNORECASE),
    re.compile(r"insufficient.?(credits?|quota|balance)", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
    re.compile(r"please\s+upgrade", re.IGNORECASE),
    re.compile(r"exceeded\s+your\s+current\s+quota", re.IGNORECASE),
]

_CONTEXT_TOO_LONG_PATTERNS = [
    re.compile(r"context.?length", re.IGNORECASE),
    re.compile(r"maximum.?tokens", re.IGNORECASE),
    re.compile(r"too.?long", re.IGNORECASE),
    re.compile(r"content.?too.?large", re.IGNORECASE),
]

_TIMEOUT_PATTERNS = [
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"timed?\s*out", re.IGNORECASE),
    re.compile(r"deadline.?exceeded", re.IGNORECASE),
]

_NETWORK_PATTERNS = [
    re.compile(r"network\s*error", re.IGNORECASE),
    re.compile(r"connection\s*(refused|reset|aborted|closed)", re.IGNORECASE),
    re.compile(r"connect\s*error", re.IGNORECASE),
    re.compile(r"name\s*resolution\s*failed", re.IGNORECASE),
    re.compile(r"dns\s*(resolution|lookup)\s*failed", re.IGNORECASE),
    re.compile(r"no\s*route\s*to\s*host", re.IGNORECASE),
    re.compile(r"remote\s*(host|server|end).*closed", re.IGNORECASE),
    re.compile(r"ssl.*error", re.IGNORECASE),
    re.compile(r"certificate\s*verify\s*failed", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_rate_limit_error(error: Exception) -> bool:
    """
    Check if an exception represents a rate limit / quota error.

    Used by retry logic to decide whether to wait-and-retry vs. fail immediately.
    """
    error_str = str(error)
    error_class = type(error).__name__

    # Direct class match (Anthropic, OpenAI, Google SDKs)
    if error_class in ("RateLimitError", "ResourceExhausted"):
        return True

    return any(p.search(error_str) for p in _RATE_LIMIT_PATTERNS)


def is_overloaded_error(error: Exception) -> bool:
    """Check if an exception represents a service overloaded / unavailable error."""
    error_str = str(error)
    return any(p.search(error_str) for p in _OVERLOADED_PATTERNS)


def is_network_error(error: Exception) -> bool:
    """Check if an exception represents a network connectivity error."""
    error_str = str(error)
    error_class = type(error).__name__
    if error_class in ("ConnectionError", "ConnectError", "NetworkError",
                       "ConnectTimeout", "ReadTimeout"):
        return True
    return any(p.search(error_str) for p in _NETWORK_PATTERNS)


def is_transient_error(error: Exception) -> bool:
    """Check if an error is transient (rate limit, overloaded, or network) and worth retrying."""
    return is_rate_limit_error(error) or is_overloaded_error(error) or is_network_error(error)


def is_db_transaction_error(error: Exception) -> bool:
    """True for recoverable SQLAlchemy session/transaction errors.

    These (PendingRollbackError, InFailedSqlTransaction, OperationalError,
    InterfaceError) are resolved by rolling back the session and re-running the
    operation — the work itself is fine, the session was just in a bad state.
    """
    error_class = type(error).__name__
    error_str = str(error)
    return error_class in (
        "PendingRollbackError", "OperationalError",
        "InterfaceError", "InFailedSqlTransaction",
    ) or bool(re.search(
        r"(pending.?rollback|transaction.*aborted|database.*unavailable|"
        r"current transaction is aborted)",
        error_str, re.IGNORECASE,
    ))


def is_recoverable_error(error: Exception) -> bool:
    """True if an error is worth an automatic rollback-and-retry.

    Union of transient provider errors (rate limit / overload / network) and
    recoverable DB transaction errors. Used by agents that auto-retry a failed
    run when the failure is the kind a retry can actually fix.
    """
    return is_transient_error(error) or is_db_transaction_error(error)


def format_user_error(error: Exception) -> str:
    """
    Convert a raw API/LLM exception into a clean, user-friendly message.

    The raw error details are logged for debugging but never shown to the user.
    """
    error_str = str(error)
    error_class = type(error).__name__

    # Log the full error for debugging (always)
    logger.debug(f"Formatting user error — {error_class}: {error_str[:500]}")

    # --- Rate limit ---
    if error_class in ("RateLimitError", "ResourceExhausted") or \
       any(p.search(error_str) for p in _RATE_LIMIT_PATTERNS):
        return (
            "The AI service is currently experiencing high demand. "
            "Please wait a moment and try again."
        )

    # --- Overloaded / 503 / 529 ---
    if any(p.search(error_str) for p in _OVERLOADED_PATTERNS):
        return (
            "The AI service is temporarily overloaded. "
            "Please try again in a few moments."
        )

    # --- Missing API key for the configured provider ---
    # Check before generic auth — gives a more actionable message when the
    # admin has pinned an agent to a provider whose API key isn't set.
    if any(p.search(error_str) for p in _MISSING_API_KEY_PATTERNS):
        return (
            "No API key is configured for the selected AI provider. "
            "Set it in Admin → Platform Settings, or change the agent's provider."
        )

    # --- Model not found / invalid model name ---
    if any(p.search(error_str) for p in _MODEL_NOT_FOUND_PATTERNS):
        return (
            "The configured AI model isn't recognized by the provider. "
            "Open the agent in Agent Builder and pick a valid model from the dropdown."
        )

    # --- Billing / insufficient credits ---
    if any(p.search(error_str) for p in _BILLING_PATTERNS):
        return (
            "The AI provider account has insufficient credits or has hit a billing limit. "
            "Top up the provider account or switch the agent to a provider with available credits."
        )

    # --- Auth / permission errors ---
    if any(p.search(error_str) for p in _AUTH_PATTERNS):
        return (
            "There's an issue with the AI service configuration. "
            "Please contact your administrator."
        )

    # --- Context / input too long ---
    if any(p.search(error_str) for p in _CONTEXT_TOO_LONG_PATTERNS):
        return (
            "The input is too long for the AI model to process. "
            "Try with a shorter message or fewer documents."
        )

    # --- Timeout ---
    if any(p.search(error_str) for p in _TIMEOUT_PATTERNS):
        return (
            "The request took too long to complete. "
            "Please try again — it may work with a simpler query."
        )

    # --- Network / connectivity errors ---
    if error_class in ("ConnectionError", "ConnectError", "NetworkError",
                       "ConnectTimeout", "ReadTimeout") or \
       any(p.search(error_str) for p in _NETWORK_PATTERNS):
        return (
            "A connection error occurred while reaching the AI service. "
            "Please check your internet connection and try again."
        )

    # --- Database / transaction errors ---
    if error_class in ("PendingRollbackError", "OperationalError",
                       "InterfaceError", "InFailedSqlTransaction") or \
       re.search(r"(pending.?rollback|transaction.*aborted|database.*unavailable)", error_str, re.IGNORECASE):
        return (
            "A temporary database error occurred. "
            "Please try again."
        )

    # --- Agent step budget (LangGraph recursion limit) ---
    if error_class == "GraphRecursionError" or re.search(
        r"recursion.?limit", error_str, re.IGNORECASE
    ):
        return (
            "The assistant used up its step budget before it finished. "
            "Say Continue to pick up from the steps that completed."
        )

    # --- Generic fallback — hide raw details ---
    logger.warning(f"Unclassified error shown to user: {error_class}: {error_str[:200]}")
    return "Something went wrong while processing your request. Please try again."
