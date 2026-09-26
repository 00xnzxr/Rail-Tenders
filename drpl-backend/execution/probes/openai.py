"""Probe: one-message round trip via the OpenAI provider (failover path)."""

from __future__ import annotations

import time

from execution.probes._common import finish, env_flag


PROBE = "openai"


def main() -> None:
    if not env_flag("OPENAI_API_KEY"):
        finish(PROBE, ok=False, detail="OPENAI_API_KEY not set in env (failover path inert)")

    try:
        from app.services.langchain.llm_factory import get_chat_model
    except Exception as e:
        finish(PROBE, ok=False, detail=f"import failed: {e}")
        return

    try:
        model = get_chat_model(provider="openai", max_tokens=64)
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
