"""Create or update onhold's ElevenLabs voice agent from src/onhold/agent/, idempotently.

Dry run is the default: read-only lookups run and every write is printed (the
key redacted) but not sent. Nothing changes in ElevenLabs without --apply.

    ELEVENLABS_API_KEY=... uv run scripts/setup.py agent --owner-name "Alex Rivera"
    ELEVENLABS_API_KEY=... uv run scripts/setup.py agent --owner-name "Alex Rivera" --apply
    ELEVENLABS_API_KEY=... uv run scripts/setup.py phone-number --phone-number-id phnum_...

The agent is found by --agent-id, else by its name in agent.json. Pronunciation
rules for names the voice gets wrong live in a local file you pass with
--pronunciation-rules (a JSON list of alias rules); they are never committed.

Afterwards, set AGENT_ID, PHONE_NUMBER_ID, ASSISTANT_NUMBER and OWNER_NAME as
Worker secrets (docs/deploy.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from onhold import agent_spec  # noqa: E402
from onhold.elevenlabs import DEFAULT_BASE_URL, safe_id  # noqa: E402


class SetupError(SystemExit):
    pass


@dataclass
class Setup:
    http: httpx.Client | None
    apply: bool
    key: str | None = None
    out: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        if self.key:
            text = text.replace(self.key, "[redacted]")
        self.out.append(text)
        print(text)

    def get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self.http.get(path, params=params or None)
        if resp.status_code >= 400:
            raise SetupError(f"ElevenLabs GET {path} returned {resp.status_code}")
        return resp.json()

    def write(self, method: str, path: str, body: dict | None = None) -> dict:
        """Print the request; send it only with --apply."""
        self.say(f"{'APPLY' if self.apply else 'DRY RUN'}: {method} {DEFAULT_BASE_URL}{path}")
        if body is not None:
            self.say(json.dumps(body, indent=2))
        if not self.apply:
            return {}
        if self.http is None:
            raise SetupError("--apply needs ELEVENLABS_API_KEY")
        resp = self.http.request(method, path, json=body)
        if resp.status_code >= 400:
            raise SetupError(
                f"ElevenLabs {method} {path} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def voice(self, voice_id: str | None) -> str:
        v = agent_spec.load()["voice"]
        wanted = voice_id or v["voice_id"]
        owner, name = v.get("library_owner_id"), v.get("library_name")
        if self.http is None:
            self.say("(no ElevenLabs key: skipping the voice check)")
            return wanted
        token = None
        for _ in range(20):
            page = self.get("/v2/voices", page_size=100, next_page_token=token)
            for have in page.get("voices", []):
                if have.get("voice_id") == wanted or (
                    not voice_id and name and have.get("name") == name
                ):
                    self.say(f"Voice {have.get('name')!r} is in the account ({have['voice_id']}).")
                    return have["voice_id"]
            token = page.get("next_page_token")
            if not page.get("has_more") or not token:
                break
        if voice_id or not owner:
            raise SetupError(f"Voice {wanted} is not in the account; add it in ElevenLabs first")
        self.say(f"Voice {wanted} is not in the account: adding it from the Voice Library.")
        res = self.write(
            "POST", f"/v1/voices/add/{safe_id(owner)}/{safe_id(wanted)}", {"new_name": name}
        )
        return res.get("voice_id") or wanted

    def pronunciation(self, rules: list[dict]) -> list[dict]:
        if not rules:
            return []
        name = agent_spec.load()["pronunciation_dictionary_name"]
        if self.http is None:
            return [{"pronunciation_dictionary_id": "<dictionary-id>", "version_id": None}]
        found, cursor = None, None
        for _ in range(20):
            page = self.get("/v1/pronunciation-dictionaries", page_size=100, cursor=cursor)
            matches = [
                d for d in page.get("pronunciation_dictionaries", []) if d.get("name") == name
            ]
            if matches:
                found = matches[0]
                break
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        if found is None:
            self.say(f"No pronunciation dictionary named {name!r}: creating it.")
            res = self.write(
                "POST",
                "/v1/pronunciation-dictionaries/add-from-rules",
                {"name": name, "rules": rules},
            )
            return [
                {
                    "pronunciation_dictionary_id": res.get("id", "<new-dictionary-id>"),
                    "version_id": res.get("version_id"),
                }
            ]
        dict_id = found["id"]
        current = self.get(f"/v1/pronunciation-dictionaries/{safe_id(dict_id)}")
        version = current.get("latest_version_id")
        if agent_spec.normalise_rules(current.get("rules", [])) == agent_spec.normalise_rules(
            rules
        ):
            self.say(f"Pronunciation dictionary {name!r} is up to date.")
        else:
            self.say(f"Pronunciation dictionary {name!r} has different rules: replacing them.")
            res = self.write(
                "POST",
                f"/v1/pronunciation-dictionaries/{safe_id(dict_id)}/set-rules",
                {"rules": rules},
            )
            version = res.get("version_id", "<new-version-id>")
        return [{"pronunciation_dictionary_id": dict_id, "version_id": version}]

    def agent(
        self,
        *,
        owner_name: str,
        agent_id: str | None,
        voice_id: str | None,
        rules: list[dict],
        webhook_id: str | None = None,
    ) -> str | None:
        body = agent_spec.build_agent_body(
            owner_name=owner_name,
            voice_id=self.voice(voice_id),
            pronunciation_locators=self.pronunciation(rules),
            webhook_id=webhook_id,
        )
        existing = agent_id
        if not existing and self.http is not None:
            agents = self.get("/v1/convai/agents", search=body["name"], page_size=100)
            matches = [a for a in agents.get("agents", []) if a.get("name") == body["name"]]
            if len(matches) > 1:
                raise SetupError(
                    f"{len(matches)} agents are named {body['name']!r}; pass --agent-id"
                )
            existing = matches[0]["agent_id"] if matches else None
        if existing:
            self.say(f"Updating agent {existing} to match src/onhold/agent/.")
            if self.http is not None:
                live = self.get(f"/v1/convai/agents/{safe_id(existing)}")
                changes = []
                for label in ("tts", "agent.prompt"):
                    want, have = body["conversation_config"], live.get("conversation_config") or {}
                    for key in label.split("."):
                        want, have = want.get(key, {}), (have or {}).get(key) or {}
                    want = {k: v for k, v in want.items() if k != "prompt"}
                    changes += differences(want, have, label)
                self.say(
                    "Differs from the live agent: " + ", ".join(changes)
                    if changes
                    else "TTS and LLM settings match the live agent."
                )
            self.write("PATCH", f"/v1/convai/agents/{safe_id(existing)}", body)
            return existing
        self.say("No agent by that name: creating it.")
        return self.write("POST", "/v1/convai/agents/create", body).get("agent_id")

    def phone_number(self, phone_number_id: str | None) -> None:
        if self.http is None:
            raise SetupError("phone-number needs ELEVENLABS_API_KEY")
        if not phone_number_id:
            numbers = self.get("/v1/convai/phone-numbers")
            numbers = numbers if isinstance(numbers, list) else numbers.get("phone_numbers", [])
            self.say(f"ElevenLabs has {len(numbers)} imported number(s):")
            for pn in numbers:
                self.say(
                    f"  {pn.get('phone_number')}  {pn.get('label')}  id={pn.get('phone_number_id')}"
                )
            return
        pn = self.get(f"/v1/convai/phone-numbers/{safe_id(phone_number_id)}")
        self.say(
            f"PHONE_NUMBER_ID={phone_number_id}\nASSISTANT_NUMBER={pn.get('phone_number')}\n"
            f"(provider {pn.get('provider')}; inbound agent: "
            f"{(pn.get('assigned_agent') or {}).get('agent_name') or 'none'})"
        )


def differences(want, have, path: str) -> list[str]:
    if isinstance(want, dict) and isinstance(have, dict):
        out = []
        for key, value in want.items():
            out += differences(value, have.get(key), f"{path}.{key}")
        return out
    if isinstance(want, float | int) and isinstance(have, float | int):
        return [] if float(want) == float(have) else [path]
    return [] if want == have else [path]


def main(argv: list[str] | None = None, *, http: httpx.Client | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("step", choices=["agent", "phone-number"])
    p.add_argument("--apply", action="store_true", help="send writes (default: dry run)")
    p.add_argument("--owner-name", default=os.environ.get("OWNER_NAME"))
    p.add_argument("--agent-id", help="update this agent rather than finding it by name")
    p.add_argument("--voice-id", help="a voice already in your ElevenLabs account")
    p.add_argument("--pronunciation-rules", type=Path, help="local JSON list of alias rules")
    p.add_argument("--phone-number-id")
    p.add_argument("--webhook-id", help="the ElevenLabs webhook pointing at /webhooks/elevenlabs")
    args = p.parse_args(argv)

    key = os.environ.get("ELEVENLABS_API_KEY")
    if http is None and key:
        http = httpx.Client(
            base_url=DEFAULT_BASE_URL,
            headers={"xi-api-key": key, "accept": "application/json"},
            timeout=30,
        )
    if args.apply and http is None:
        print("--apply needs ELEVENLABS_API_KEY in the environment", file=sys.stderr)
        return 1
    s = Setup(http=http, apply=args.apply, key=key)
    try:
        if args.step == "phone-number":
            s.phone_number(args.phone_number_id)
            return 0
        if not args.owner_name:
            raise SetupError("--owner-name (or OWNER_NAME) is required: who the agent calls for")
        rules = json.loads(args.pronunciation_rules.read_text()) if args.pronunciation_rules else []
        agent_id = s.agent(
            owner_name=args.owner_name,
            agent_id=args.agent_id,
            voice_id=args.voice_id,
            rules=rules,
            webhook_id=args.webhook_id,
        )
        if agent_id:
            s.say(f"\nAGENT_ID={agent_id}")
        if not args.webhook_id:
            s.say(
                "\nNo --webhook-id: results will come only from the 15-minute reconcile. In "
                "ElevenLabs (Agents > Settings > Webhooks), create an HMAC webhook for "
                "https://<your host>/webhooks/elevenlabs with retries on, set its secret as "
                "ELEVENLABS_WEBHOOK_SECRET, and re-run with --webhook-id."
            )
    except SetupError as e:
        print(str(e), file=sys.stderr)
        return 1
    if not args.apply:
        print("\nDry run: nothing was sent. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
