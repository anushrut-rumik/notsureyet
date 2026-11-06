import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["generic", "openai"] = "generic"
    protocol: Literal["chat_completions", "responses", "anthropic"] = "chat_completions"
    base_url: str
    model: str
    api_key_env: str = ""
    options: dict = Field(default_factory=dict)
    timeout_seconds: float = Field(default=180, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_request(self):
        if self.protocol in {"chat_completions", "responses"}:
            effective = {**self.options, **self.options.get("extra_body", {})}
            if effective.get("stream") or effective.get("background") or effective.get("n", 1) != 1:
                raise ValueError("The agent requires non-streaming requests with one answer")
            reserved = {"model", "messages", "input", "tools", "tool_choice", "store", "include"}
            if reserved.intersection(self.options) or reserved.intersection(self.options.get("extra_body", {})):
                raise ValueError("Endpoint options cannot override the model, messages, or agent tools")
        if any(name.lower() in {"authorization", "x-api-key"} for name in self.headers):
            raise ValueError("Store credentials in api_key_env, not headers")
        return self


class Config:
    def __init__(self, root: Path = ROOT, *, environ: dict | None = None):
        self.root = root.resolve()
        self.env = {**dotenv_values(self.root / ".env"), **(os.environ if environ is None else environ)}
        path = Path(self.env.get("AGENT_INFERENCE_CONFIG") or self.root / "inference.toml")
        if not path.is_absolute():
            path = self.root / path
        data = tomllib.loads(path.read_text())
        self.endpoints = {name: Endpoint.model_validate(value) for name, value in data["endpoints"].items()}
        self.routes = data["routes"]
        self.routes["default"]
        for name in self.routes.values():
            self.endpoints[name]
        self.data_dir = self.root / ".local"
        self.workspace = self.root / "workspace"
        self.db_path = self.data_dir / "agent.sqlite3"
        self.task_backend = self.env.get("AGENT_TASK_BACKEND") or "arkiv"
        if self.task_backend not in {"sqlite", "arkiv"}:
            raise ValueError("AGENT_TASK_BACKEND must be arkiv or sqlite")
        self.steps_per_run = int(self.env.get("AGENT_STEPS_PER_RUN") or 12)
        self.max_parallel_tasks = int(self.env.get("AGENT_PARALLEL_TASKS") or 2)
        self.chat_inference_timeout = float(self.env.get("AGENT_CHAT_INFERENCE_TIMEOUT_SECONDS") or 45)
        if self.chat_inference_timeout <= 0:
            raise ValueError("AGENT_CHAT_INFERENCE_TIMEOUT_SECONDS must be positive")
        self.tool_modules = [s.strip() for s in (self.env.get("AGENT_TOOL_MODULES") or "").split(",") if s.strip()]

    def endpoint(self, role: str) -> tuple[str, Endpoint]:
        name = self.routes.get(role, self.routes["default"])
        return name, self.endpoints[name]

    def key(self, endpoint: Endpoint) -> str:
        return (self.env.get(endpoint.api_key_env) or "").strip() if endpoint.api_key_env else "local-no-auth"

    def status(self) -> list[dict]:
        return [
            {
                "role": role,
                "endpoint": name,
                "model": self.endpoints[name].model,
                "protocol": self.endpoints[name].protocol,
                "provider": self.endpoints[name].provider,
                "base_url": self.endpoints[name].base_url,
                "key_variable": self.endpoints[name].api_key_env,
                "key_present": bool(self.key(self.endpoints[name])),
            }
            for role, name in self.routes.items()
        ]
