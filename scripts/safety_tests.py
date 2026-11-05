"""Turn src/onhold/agent/safety_tests.json into ElevenLabs agent tests, and run them.

Dry run is the default: prints the test definitions. With --apply it creates
or updates each test (matched by name) and runs them all on the agent. Results
appear in the ElevenLabs dashboard under the agent's Tests tab.

    OWNER_NAME="Alex Rivera" uv run scripts/safety_tests.py
    ELEVENLABS_API_KEY=... AGENT_ID=agent_... OWNER_NAME=... uv run scripts/safety_tests.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from onhold import agent_spec  # noqa: E402
from onhold.elevenlabs import DEFAULT_BASE_URL, safe_id  # noqa: E402

PREFIX = "onhold safety: "


def load_cases() -> dict:
    return json.loads((agent_spec.AGENT_DIR / "safety_tests.json").read_text())


def test_bodies(owner_name: str) -> list[dict]:
    spec = load_cases()
    variables = {"owner_name": owner_name, **agent_spec.PLACEHOLDERS}
    first = agent_spec.render(agent_spec.first_message(), variables)

    def fill(text: str) -> str:
        return text.replace("{owner_name}", owner_name)

    bodies = []
    for c in spec["cases"]:
        bodies.append(
            {
                "type": "llm",
                "name": PREFIX + c["id"],
                "chat_history": [
                    {"role": "agent", "time_in_call_secs": 0, "message": first},
                    {"role": "user", "time_in_call_secs": 8, "message": fill(c["utterance"])},
                ],
                "dynamic_variables": variables,
                "success_condition": "Return True only if the agent's reply does this: "
                + fill(c["expected"]),
                "success_examples": [{"response": fill(c["success_example"]), "type": "success"}],
                "failure_examples": [{"response": fill(c["failure_example"]), "type": "failure"}],
            }
        )
    for s in spec.get("simulations", []):
        bodies.append(
            {
                "type": "simulation",
                "name": PREFIX + s["id"],
                "dynamic_variables": variables,
                "simulation_scenario": fill(s["scenario"]),
                "simulation_max_turns": s["max_turns"],
                "success_conditions": [fill(x) for x in s["success_conditions"]],
            }
        )
    return bodies


def main(argv: list[str] | None = None, *, http: httpx.Client | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--apply", action="store_true", help="create and run the tests")
    p.add_argument("--repeat", type=int, default=None, help="repeat_count, 2-20")
    p.add_argument("--owner-name", default=os.environ.get("OWNER_NAME") or "Alex Rivera")
    args = p.parse_args(argv)

    bodies = test_bodies(args.owner_name)
    if not args.apply:
        for b in bodies:
            print(f"DRY RUN: POST /v1/convai/agent-testing/create  {b['name']} ({b['type']})")
        print(json.dumps(bodies[0], indent=2))
        print(f"\n{len(bodies)} tests. Dry run: nothing was sent. Re-run with --apply.")
        return 0

    agent_id = os.environ.get("AGENT_ID")
    key = os.environ.get("ELEVENLABS_API_KEY")
    if http is None:
        if not key or not agent_id:
            print("--apply needs ELEVENLABS_API_KEY and AGENT_ID", file=sys.stderr)
            return 1
        http = httpx.Client(base_url=DEFAULT_BASE_URL, headers={"xi-api-key": key}, timeout=30)

    def call(method, path, **kw):
        r = http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"ElevenLabs {method} {path} returned {r.status_code}")
        return r.json() if r.content else {}

    existing = {
        t["name"]: t["id"]
        for t in call("GET", "/v1/convai/agent-testing", params={"page_size": 100}).get("tests", [])
    }
    ids = []
    for b in bodies:
        test_id = existing.get(b["name"])
        if test_id:
            call("PUT", f"/v1/convai/agent-testing/{safe_id(test_id)}", json=b)
        else:
            test_id = call("POST", "/v1/convai/agent-testing/create", json=b)["id"]
        ids.append(test_id)
    run = {"tests": [{"test_id": i} for i in ids]}
    if args.repeat:
        run["repeat_count"] = args.repeat
    res = call("POST", f"/v1/convai/agents/{safe_id(agent_id)}/run-tests", json=run)
    print(f"Started {len(ids)} tests: invocation {res.get('id')}. See the agent's Tests tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
