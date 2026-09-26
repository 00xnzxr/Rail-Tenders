"""A streamed LangChain call is metered like any other.

`astream_events` (every general-assistant turn) returns a ChatResult with no
`llm_output`; usage and the model name live only on the message. The
callback read llm_output alone, so those runs were logged as provider
"unknown", 0 tokens, $0 -- and the monthly budget never counted them.
"""
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.services.langchain import callback_handler as cb


def _streamed_result():
    msg = AIMessage(
        content="hello",
        usage_metadata={
            "input_tokens": 1500, "output_tokens": 200, "total_tokens": 1700,
            "input_token_details": {"cache_read": 1000, "cache_creation": 0},
        },
        response_metadata={"model_name": "claude-haiku-4-5", "stop_reason": "end_turn"},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output=None)


def test_streamed_usage_is_logged_with_its_model_and_tokens(monkeypatch):
    logged = []
    monkeypatch.setattr(cb, "_log_usage", lambda *a, **k: logged.append(k))
    handler = cb.DRPLCallbackHandler(db=None, agent_name="general_assistant")
    rid = uuid4()
    handler.on_llm_start({}, [], run_id=rid)
    handler.on_llm_end(_streamed_result(), run_id=rid)

    assert logged, "nothing was logged"
    row = logged[-1]
    assert row["provider"] == "anthropic" and row["model"] == "claude-haiku-4-5"
    assert row["usage"]["input_tokens"] == 500          # fresh input only
    assert row["usage"]["cache_read_input_tokens"] == 1000
    assert row["usage"]["output_tokens"] == 200
    assert handler.total_cost > 0


def test_an_llm_output_result_is_read_as_before(monkeypatch):
    logged = []
    monkeypatch.setattr(cb, "_log_usage", lambda *a, **k: logged.append(k))
    handler = cb.DRPLCallbackHandler(db=None, agent_name="x")
    rid = uuid4()
    handler.on_llm_start({}, [], run_id=rid)
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
        llm_output={"model_name": "claude-haiku-4-5",
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    handler.on_llm_end(result, run_id=rid)
    assert logged[-1]["usage"]["input_tokens"] == 10
    assert logged[-1]["model"] == "claude-haiku-4-5"
