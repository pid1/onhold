"""A small, explicit async client for the ElevenLabs Agents endpoints onhold uses.

Endpoints (checked 2026-11-05 against https://elevenlabs.io/docs/api-reference):

* ``POST /v1/convai/twilio/outbound-call``          place a call now
* ``POST /v1/convai/batch-calling/submit``          schedule a call (one recipient)
* ``GET  /v1/convai/batch-calling/{id}``            scheduled call status
* ``POST /v1/convai/batch-calling/{id}/cancel``     cancel a scheduled call
* ``GET  /v1/convai/conversations/{id}``            status, transcript, analysis
* agents and phone numbers, for ``scripts/setup.py``

The API key travels only in the ``xi-api-key`` header. Error messages carry
the status code and ElevenLabs' ``detail``, never request headers.

Async because on Cloudflare Workers httpx runs over the runtime's ``fetch``,
which has no synchronous form.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.elevenlabs.io"


class ElevenLabsError(RuntimeError):
    def __init__(self, status: int, detail: Any, path: str):
        self.status = status
        self.detail = detail
        self.path = path
        super().__init__(f"ElevenLabs {path} returned {status}: {detail}")


class ElevenLabs:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"xi-api-key": api_key, "accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._http.request(method, path, json=json, params=params or None)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text[:300])
            except ValueError:
                detail = resp.text[:300]
            raise ElevenLabsError(resp.status_code, detail, path)
        if not resp.content:
            return {}
        return resp.json()

    # Calls -----------------------------------------------------------------

    async def outbound_call(
        self,
        *,
        agent_id: str,
        phone_number_id: str,
        to_number: str,
        dynamic_variables: dict[str, str],
        ringing_timeout_secs: int,
    ) -> dict:
        return await self.request(
            "POST",
            "/v1/convai/twilio/outbound-call",
            json={
                "agent_id": agent_id,
                "agent_phone_number_id": phone_number_id,
                "to_number": to_number,
                "call_recording_enabled": False,
                "conversation_initiation_client_data": {"dynamic_variables": dynamic_variables},
                "telephony_call_config": {
                    "ringing_timeout_secs": ringing_timeout_secs,
                    "twilio_call_recording_enabled": False,
                },
            },
        )

    async def submit_batch(
        self,
        *,
        call_name: str,
        agent_id: str,
        phone_number_id: str,
        to_number: str,
        dynamic_variables: dict[str, str],
        scheduled_time_unix: int,
        timezone: str,
        ringing_timeout_secs: int,
    ) -> dict:
        return await self.request(
            "POST",
            "/v1/convai/batch-calling/submit",
            json={
                "call_name": call_name,
                "agent_id": agent_id,
                "agent_phone_number_id": phone_number_id,
                "scheduled_time_unix": scheduled_time_unix,
                "timezone": timezone,
                "target_concurrency_limit": 1,
                "recipients": [
                    {
                        "phone_number": to_number,
                        "conversation_initiation_client_data": {
                            "dynamic_variables": dynamic_variables
                        },
                    }
                ],
                "telephony_call_config": {
                    "ringing_timeout_secs": ringing_timeout_secs,
                    "twilio_call_recording_enabled": False,
                },
            },
        )

    async def get_batch(self, batch_id: str) -> dict:
        return await self.request("GET", f"/v1/convai/batch-calling/{safe_id(batch_id)}")

    async def cancel_batch(self, batch_id: str) -> dict:
        return await self.request("POST", f"/v1/convai/batch-calling/{safe_id(batch_id)}/cancel")

    async def get_conversation(self, conversation_id: str) -> dict:
        return await self.request("GET", f"/v1/convai/conversations/{safe_id(conversation_id)}")

    # Setup -----------------------------------------------------------------

    async def list_agents(self, search: str) -> dict:
        return await self.request(
            "GET", "/v1/convai/agents", params={"search": search, "page_size": 100}
        )

    async def list_phone_numbers(self) -> list:
        data = await self.request("GET", "/v1/convai/phone-numbers")
        return data if isinstance(data, list) else data.get("phone_numbers", [])


def safe_id(value: str) -> str:
    """Refuse IDs that could change the request path."""
    if not value or not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError(f"not a valid ElevenLabs id: {value!r}")
    return value
