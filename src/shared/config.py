from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


ENV_MAPPINGS: dict[tuple[str, ...], str] = {
    ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
    ("telegram", "supergroup_id"): "TELEGRAM_SUPERGROUP_ID",
    ("telegram", "security", "allowed_users"): "TELEGRAM_OWNER_ID",
    ("hub", "url"): "HUB_URL",
    ("auth", "token"): "INTERCOM_TOKEN",
}


class IntercomConfig(BaseModel):
    mode: str = "standalone"
    machine: dict[str, Any] = Field(default_factory=lambda: {"id": "unknown"})
    telegram: dict[str, Any] = Field(default_factory=dict)
    hub: dict[str, Any] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)
    agent_launcher: dict[str, Any] = Field(default_factory=dict)
    dispatcher: dict[str, Any] = Field(default_factory=dict)
    projects: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_hub(self) -> bool:
        return self.mode in ("hub", "standalone")

    @property
    def is_daemon(self) -> bool:
        return self.mode in ("daemon", "standalone")

    @property
    def machine_id(self) -> str:
        return self.machine.get("id", "unknown")


def _set_nested(d: dict, keys: tuple[str, ...], value: Any) -> None:
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    if keys[-1] == "allowed_users":
        d[keys[-1]] = [int(value)] if isinstance(value, str) else value
    elif keys[-1] == "supergroup_id":
        d[keys[-1]] = int(value)
    else:
        d[keys[-1]] = value


def load_config(path: str) -> IntercomConfig:
    config_path = Path(path)
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    for keys, env_var in ENV_MAPPINGS.items():
        env_value = os.environ.get(env_var)
        if env_value:
            _set_nested(data, keys, env_value)

    return IntercomConfig(**data)
