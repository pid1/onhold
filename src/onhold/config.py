"""Settings, read from the Worker's environment (secrets and vars) or os.environ.

Nothing personal is in the repository. The owner's name, the callback number
and every id and key are set per deployment with ``wrangler secret put`` (or
in ``.dev.vars`` locally); see ``.dev.vars.example``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from onhold.brief import check_number

REQUIRED = (
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_WEBHOOK_SECRET",
    "AGENT_ID",
    "PHONE_NUMBER_ID",
    "ASSISTANT_NUMBER",
    "OWNER_NAME",
)


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. Names the setting, never its value."""


def _get(source: Any, name: str) -> str | None:
    if isinstance(source, dict):
        value = source.get(name)
    else:  # the Worker's env binding object
        value = getattr(source, name, None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    elevenlabs_api_key: str
    webhook_secret: str
    agent_id: str
    phone_number_id: str
    assistant_number: str
    owner_name: str
    callback_number: str | None = None
    pushover_token: str | None = None
    pushover_user: str | None = None
    # Cloudflare Access: the team domain (myteam.cloudflareaccess.com) and the
    # application's AUD tag. Both required for /mcp; without them it refuses.
    access_team_domain: str | None = None
    access_aud: str | None = None
    # If set, the only Access identity allowed to use /mcp.
    owner_email: str | None = None
    paused: bool = False
    allowed_country_codes: tuple[str, ...] = ("1",)
    ringing_timeout_secs: int = 45
    max_duration_secs: int = 900
    draft_ttl_secs: int = 1800
    transcript_days: int = 30

    @classmethod
    def load(cls, source: Any) -> Settings:
        missing = [n for n in REQUIRED if not _get(source, n)]
        if missing:
            raise ConfigError(f"missing settings: {', '.join(missing)}")
        assistant = check_number(_get(source, "ASSISTANT_NUMBER"))
        callback = _get(source, "CALLBACK_NUMBER")
        countries = tuple(
            c.strip().lstrip("+")
            for c in (_get(source, "ALLOWED_COUNTRY_CODES") or "1").split(",")
            if c.strip()
        )
        return cls(
            elevenlabs_api_key=_get(source, "ELEVENLABS_API_KEY"),
            webhook_secret=_get(source, "ELEVENLABS_WEBHOOK_SECRET"),
            agent_id=_get(source, "AGENT_ID"),
            phone_number_id=_get(source, "PHONE_NUMBER_ID"),
            assistant_number=assistant,
            owner_name=_get(source, "OWNER_NAME"),
            callback_number=check_number(callback) if callback else None,
            pushover_token=_get(source, "PUSHOVER_TOKEN"),
            pushover_user=_get(source, "PUSHOVER_USER"),
            access_team_domain=_get(source, "ACCESS_TEAM_DOMAIN"),
            access_aud=_get(source, "ACCESS_AUD"),
            owner_email=_get(source, "OWNER_EMAIL"),
            paused=(_get(source, "PAUSED") or "").lower() in ("1", "true", "yes"),
            allowed_country_codes=countries,
        )
