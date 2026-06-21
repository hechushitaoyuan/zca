"""Pure Start Plan request transformations derived from the ZCode client flow."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

_SYSTEM_BLOCKS_PATH = Path(__file__).with_name("zcode_system.json")
with _SYSTEM_BLOCKS_PATH.open("r", encoding="utf-8") as _stream:
    ZCODE_SYSTEM_BLOCKS: list[dict[str, Any]] = json.load(_stream)

MODEL_NAME_MAP = {
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}


def decode_jwt_user_id(token: str | None) -> str | None:
    """Read the non-secret user identifier claim without logging the JWT."""
    if not token:
        return None
    try:
        payload = token.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = claims.get("user_id") or claims.get("sub")
    return str(value) if value else None


def _normalize_user_system(system: object) -> list[dict[str, Any]]:
    if isinstance(system, str):
        return [{"type": "text", "text": system.strip()}] if system.strip() else []
    if not isinstance(system, list):
        return []

    blocks: list[dict[str, Any]] = []
    for item in system:
        if isinstance(item, str) and item.strip():
            blocks.append({"type": "text", "text": item.strip()})
        elif isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                block: dict[str, Any] = {"type": "text", "text": text.strip()}
                if isinstance(item.get("cache_control"), dict):
                    block["cache_control"] = copy.deepcopy(item["cache_control"])
                blocks.append(block)
    return blocks


def _apply_cache_control(messages: object) -> None:
    if not isinstance(messages, list):
        return
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and "cache_control" not in last:
                last["cache_control"] = {"type": "ephemeral"}
        return


def prepare_start_plan_body(body: dict, user_id: str | None = None) -> dict:
    """Return a transformed copy suitable for the ZCode Start Plan endpoint.

    The client's ``stream`` value is deliberately left unchanged.
    """
    prepared = copy.deepcopy(body)
    model = prepared.get("model")
    if isinstance(model, str):
        prepared["model"] = MODEL_NAME_MAP.get(model.lower(), model)

    raw_system = prepared.get("system")
    if (
        isinstance(raw_system, list)
        and raw_system[: len(ZCODE_SYSTEM_BLOCKS)] == ZCODE_SYSTEM_BLOCKS
    ):
        raw_system = raw_system[len(ZCODE_SYSTEM_BLOCKS) :]
    user_blocks = _normalize_user_system(raw_system)
    prepared["system"] = copy.deepcopy(ZCODE_SYSTEM_BLOCKS) + user_blocks

    _apply_cache_control(prepared.get("messages"))

    if user_id:
        metadata = prepared.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["user_id"] = user_id
        prepared["metadata"] = metadata
    return prepared
