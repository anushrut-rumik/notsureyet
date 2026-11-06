"""Repeatable catalog and opt-in live tool-round-trip checks for inference routes."""

import json
from uuid import uuid4

from openai import APIConnectionError, APIStatusError

from agent.inference import InferenceError, inference_error
from agent.providers import Models


async def doctor(config, *, live=False):
    models, results = Models(config), []
    try:
        for role in ("chat", "planner", "worker"):
            name, endpoint = config.endpoint(role)
            result = {"role": role, "endpoint": name, "model": endpoint.model}
            try:
                await models.check_catalog(role)
                result.update(ok=True, model_available=True)
                if live:
                    result.update(await probe(models, role))
            except (APIStatusError, APIConnectionError) as exc:
                result.update(ok=False, **inference_error(exc).details())
            except InferenceError as exc:
                result.update(ok=False, **exc.details())
            except ValueError:
                result.update(ok=False, code="configuration_error")
            results.append(result)
    finally:
        await models.close()
    return {"ok": all(row["ok"] for row in results), "live_inference": live, "routes": results}


async def probe(models, role):
    nonce = "probe-" + uuid4().hex
    tools = [
        {
            "type": "function",
            "function": {
                "name": "integration_probe",
                "description": "Return the integration test's verification token.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
    ]
    messages = [
        {
            "role": "system",
            "content": "This is an integration test. First call integration_probe exactly once. "
            "After it returns, reply with only its token. Do not invent a token.",
        },
        {"role": "user", "content": "Run the integration probe now."},
    ]
    first = await models.complete(role, messages, tools)
    if len(first.calls) != 1 or first.calls[0]["function"]["name"] != "integration_probe":
        raise InferenceError("probe_failed", "The model did not issue the expected tool call.")
    try:
        arguments = json.loads(first.calls[0]["function"]["arguments"])
    except (ValueError, TypeError):
        raise InferenceError("probe_failed", "The model returned invalid tool arguments.") from None
    if arguments != {}:
        raise InferenceError("probe_failed", "The model returned unexpected tool arguments.")
    messages.extend(
        [
            first.message(),
            {
                "role": "tool",
                "tool_call_id": first.calls[0]["id"],
                "content": json.dumps({"token": nonce}),
            },
        ]
    )
    second = await models.complete(role, messages, tools)
    if second.calls or second.text.strip() != nonce:
        raise InferenceError("probe_failed", "The model did not correctly use the returned tool result.")
    return {"tool_round_trip": True, "requests": [first.metadata, second.metadata]}
