"""OpenAI Realtime GA API client for voice sessions.

Provides two functions used by the voice plugin:
  - create_client_secret: Creates an ephemeral token via /client_secrets
  - exchange_sdp: Exchanges WebRTC SDP offer/answer via /calls
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException

OPENAI_REALTIME_BASE = "https://api.openai.com/v1/realtime"
CLIENT_SECRETS_ENDPOINT = f"{OPENAI_REALTIME_BASE}/client_secrets"
SDP_EXCHANGE_ENDPOINT = f"{OPENAI_REALTIME_BASE}/calls"


@dataclass
class VoiceConfig:
    """Configuration for a voice session."""

    model: str
    voice: str
    instructions: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    openai_api_key: str = ""


async def create_client_secret(config: VoiceConfig) -> dict[str, Any]:
    """Create an ephemeral client secret via the GA Realtime API.

    Returns ``{"client_secret": {"value": "ek_..."}, "session_id": "sess_..."}``.
    """
    headers = {
        "Authorization": f"Bearer {config.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "session": {
            "type": "realtime",
            "model": config.model,
            "instructions": config.instructions,
        }
    }
    # Only include tools if non-empty; voice is set via session.update
    # on the data channel, not at client_secrets creation time.
    if config.tools:
        payload["session"]["tools"] = config.tools

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            CLIENT_SECRETS_ENDPOINT,
            json=payload,
            headers=headers,
        )

    if resp.is_error:
        import logging

        logging.getLogger(__name__).error(
            "create_client_secret failed %s: %s", resp.status_code, resp.text
        )
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    return {
        "client_secret": {"value": data["value"]},
        "session_id": data.get("id", "unknown"),
    }


async def exchange_sdp(
    sdp_offer: str, ephemeral_token: str, model: str
) -> dict[str, str]:
    """Exchange WebRTC SDP offer for an answer via the GA Realtime API.

    Returns ``{"sdp": "<answer>", "call_id": "<id>"}``.  The *call_id* is
    extracted from the ``Location`` response header and identifies the
    WebRTC call for sideband connections.
    """
    headers = {
        "Authorization": f"Bearer {ephemeral_token}",
        "Content-Type": "application/sdp",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            SDP_EXCHANGE_ENDPOINT,
            content=sdp_offer,
            headers=headers,
            params={"model": model},
        )

    # The GA API may return 200 or 201 for SDP exchanges.
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    # Extract call_id from the Location header (e.g. ".../calls/<call_id>")
    location = resp.headers.get("location", "")
    call_id = location.split("/")[-1] if location else ""

    return {"sdp": resp.text, "call_id": call_id}
