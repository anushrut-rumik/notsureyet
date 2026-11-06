"""One completion interface; every inference role can use its own provider and key."""

import json
from dataclasses import dataclass, field
from uuid import uuid4

from anthropic import AsyncAnthropic
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from agent.config import Config
from agent.inference import InferenceError, inference_error
from agent.reactions import decision_schema, reply_schema


@dataclass
class Answer:
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def message(self) -> dict:
        result = {"role": "assistant", "content": self.text or None}
        if self.calls:
            result["tool_calls"] = self.calls
        return {**result, **self.extra}


class Models:
    def __init__(self, config: Config):
        self.config, self.clients = config, {}
        self.catalogs = {}

    def client(self, name, endpoint):
        if name not in self.clients:
            key = self.config.key(endpoint)
            if not key:
                raise ValueError(f"Add {endpoint.api_key_env} to this project's .env")
            cls = AsyncAnthropic if endpoint.protocol == "anthropic" else AsyncOpenAI
            self.clients[name] = cls(
                api_key=key,
                base_url=endpoint.base_url,
                max_retries=0,
                timeout=endpoint.timeout_seconds,
                default_headers=endpoint.headers,
            )
        return self.clients[name]

    async def close(self):
        for client in self.clients.values():
            await client.close()

    async def complete(self, role: str, messages: list[dict], tools: list[dict]) -> Answer:
        client_request_id = uuid4().hex
        try:
            return await self._complete(role, messages, tools, client_request_id)
        except (APIStatusError, APIConnectionError) as exc:
            error = inference_error(exc)
            error.client_request_id = client_request_id
            raise error from None
        except InferenceError as exc:
            exc.client_request_id = client_request_id
            raise

    async def check_catalog(self, role):
        name, endpoint = self.config.endpoint(role)
        if endpoint.protocol == "anthropic":
            raise ValueError("Catalog checks require an OpenAI-compatible endpoint")
        if name not in self.catalogs:
            response = await self.client(name, endpoint).models.list()
            self.catalogs[name] = [item.model_dump() for item in response.data]
        model = next((item for item in self.catalogs[name] if item["id"] == endpoint.model), None)
        if model is None:
            raise InferenceError("model_not_found", f"Model {endpoint.model} is absent from the live model catalog.")
        return model

    async def _complete(self, role: str, messages: list[dict], tools: list[dict], client_request_id: str) -> Answer:
        name, endpoint = self.config.endpoint(role)
        native_id = f"{name}:{endpoint.model}"
        client = self.client(name, endpoint)
        schema = decision_schema() if role == "reaction" else reply_schema() if role == "chat" else None
        if endpoint.protocol == "chat_completions":
            clean = []
            for message in messages:
                clean.append(
                    {
                        key: value
                        for key, value in message.items()
                        if not key.startswith("_")
                        and (key != "reasoning_content" or message.get("_native_id") == native_id)
                    }
                )
            options = {"model": endpoint.model, "messages": clean, **endpoint.options}
            if schema and endpoint.provider == "openai":
                options["response_format"] = {
                    "type": "json_schema", "json_schema": {"name": role, "strict": True, "schema": schema},
                }
            if tools:
                options.update(tools=tools, tool_choice="auto")
            raw = await client.chat.completions.with_raw_response.create(**options)
            response = raw.parse()
            if not response.choices or response.choices[0].finish_reason not in {"stop", "tool_calls"}:
                raise InferenceError(
                    "incomplete_response", "Inference returned an incomplete answer; check the output token limit."
                )
            message = response.choices[0].message
            reasoning = getattr(message, "reasoning_content", None) or (
                getattr(message, "provider_specific_fields", None) or {}
            ).get("reasoning_content")
            if not message.content and not message.tool_calls:
                raise InferenceError("empty_response", "Inference returned no answer or tool calls.")
            return Answer(
                message.content or "",
                [call.model_dump() for call in message.tool_calls or []],
                {"reasoning_content": reasoning, "_native_id": native_id} if reasoning else {},
                {
                    "endpoint": name,
                    "model": endpoint.model,
                    "response_id": response.id,
                    "client_request_id": client_request_id,
                    "usage": response.usage.model_dump() if response.usage else {},
                    "request_id": raw.headers.get("x-request-id"),
                },
            )
        if endpoint.protocol == "responses":
            items = []
            for message in messages:
                if message.get("_native_id") == native_id and "_response_items" in message:
                    items.extend(message["_response_items"])
                    continue
                if message["role"] == "tool":
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": message["tool_call_id"],
                            "output": message["content"],
                        }
                    )
                else:
                    if message.get("content"):
                        items.append({"role": message["role"], "content": message["content"]})
                    for call in message.get("tool_calls", []):
                        items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
            options = {
                "model": endpoint.model,
                "input": items,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                **endpoint.options,
                "extra_headers": {
                    **endpoint.options.get("extra_headers", {}),
                    "X-Client-Request-Id": client_request_id,
                },
            }
            if tools:
                options["tools"] = [{"type": "function", **tool["function"], "strict": False} for tool in tools]
            if schema and endpoint.provider == "openai":
                options["text"] = {"format": {
                    "type": "json_schema", "name": role, "strict": True, "schema": schema,
                }}
            response = await client.responses.create(**options)
            if response.status != "completed" or any(
                getattr(item, "status", None) in {"incomplete", "in_progress"} for item in response.output
            ):
                raise InferenceError(
                    "incomplete_response", "Inference returned an incomplete answer; check the output token limit."
                )
            if not response.output_text and not any(item.type == "function_call" for item in response.output):
                raise InferenceError("empty_response", "Inference returned no answer or tool calls.")
            return Answer(
                response.output_text,
                [
                    {
                        "id": item.call_id,
                        "type": "function",
                        "function": {
                            "name": item.name,
                            "arguments": item.arguments,
                        },
                    }
                    for item in response.output
                    if item.type == "function_call"
                ],
                {
                    "_native_id": native_id,
                    "_response_items": [item.model_dump(exclude_none=True) for item in response.output],
                },
                {
                    "endpoint": name,
                    "model": endpoint.model,
                    "response_id": response.id,
                    "request_id": getattr(response, "_request_id", None),
                    "client_request_id": client_request_id,
                    "usage": response.usage.model_dump() if response.usage else {},
                },
            )
        system, turns = [], []
        for message in messages:
            role = message["role"]
            if role == "system":
                system.append(message["content"])
                continue
            if message.get("_native_id") == native_id and "_anthropic_blocks" in message:
                turns.append({"role": "assistant", "content": message["_anthropic_blocks"]})
                continue
            if role == "tool":
                blocks = [
                    {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
                ]
                role = "user"
            else:
                blocks = [{"type": "text", "text": message["content"]}] if message.get("content") else []
                blocks += [
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": json.loads(call["function"]["arguments"]),
                    }
                    for call in message.get("tool_calls", [])
                ]
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"].extend(blocks)
            elif blocks:
                turns.append({"role": role, "content": blocks})
        response = await client.messages.create(
            model=endpoint.model,
            system="\n\n".join(system),
            messages=turns,
            tools=[
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"]["description"],
                    "input_schema": tool["function"]["parameters"],
                }
                for tool in tools
            ],
            **{"max_tokens": 8192, **endpoint.options},
        )
        return Answer(
            "".join(block.text for block in response.content if block.type == "text"),
            [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
                for block in response.content
                if block.type == "tool_use"
            ],
            {
                "_native_id": native_id,
                "_anthropic_blocks": [block.model_dump(exclude_none=True) for block in response.content],
            },
        )
