"""Probe: one-message round trip via the Google (Gemini) provider.

Graceful skip if no key is configured — the failover chain still works
with anthropic + openai only.
"""

from __future__ import annotations

import sys
import time

from execution.probes._common import finish, env_flag, log_progress


PROBE = "google"


def main() -> None:
    if not (env_flag("GOOGLE_API_KEY") or env_flag("GEMINI_API_KEY")):
        log_progress(PROBE, "SKIP", "no GOOGLE_API_KEY / GEMINI_API_KEY in env")
        print(f"[probe:{PROBE}] SKIP — no Google API key configured")
        sys.exit(0)

    try:
        from app.services.langchain.llm_factory import get_chat_model
    except Exception as e:
        finish(PROBE, ok=False, detail=f"import failed: {e}")
        return

    try:
        model = get_chat_model(provider="google", max_tokens=64)
    except Exception as e:
        finish(PROBE, ok=False, detail=f"get_chat_model raised: {e}")
        return

    t0 = time.time()
    try:
        result = model.invoke("Reply with the single word 'probe-ok'.")
    except Exception as e:
        finish(PROBE, ok=False, detail=f"invoke raised: {e}")
        return

    elapsed_ms = int((time.time() - t0) * 1000)
    text = getattr(result, "content", "") or ""
    if not text:
        finish(PROBE, ok=False, detail=f"empty response after {elapsed_ms}ms")
        return
    finish(PROBE, ok=True, detail=f"{elapsed_ms}ms, {len(text)} chars")


if __name__ == "__main__":
    main()
