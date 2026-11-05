# AGENTS.md

## Homepage

The README is onhold's homepage and its documentation. The repository's GitHub "website" field points at it (https://github.com/pid1/onhold#readme). There is no GitHub Pages site or separate landing page, and none should be added. Detail goes in files the README links to, such as docs/deploy.md and docs/RESEARCH.md. When a change alters what onhold does or how it's set up, update the README in the same PR.

## What onhold must never do

These are the project's point, not style. A change that weakens one needs the owner's explicit decision in the PR.

- **Never dial without the owner's go-ahead on the exact call.** `place_call` and `schedule_call` take a `draft_id` from `draft_call` and `confirm=true`. A draft is claimed atomically (`Store.claim_draft`), used once, and expires after `draft_ttl_secs`. Don't add a path that dials from a brief directly, a retry loop, or a queue.
- **Know nothing but the brief.** onhold's code has no connection to the owner's calendar, contacts, email, Rally or any other data, and must not gain one. An owner may attach their own read-only free/busy tool to their ElevenLabs agent; that is their agent configuration, documented in docs/deploy.md, never a feature here. Another agent with its own access (Claude with other connectors) may write a brief from those; onhold only stores a brief's `reference` and never follows it.
- **One owner.** The agent calls only on behalf of the deployment's owner (`OWNER_NAME`). `/mcp` is behind Cloudflare Access and `onhold.access.verify` checks the token again; with `OWNER_EMAIL` set, only that identity gets in.
- **Nothing personal in the repo.** The owner's name, numbers, ids and pronunciation rules are Worker secrets or local files. Tests use "Test Owner" and the fictional 555-01xx numbers. `tests/test_agent_spec.py::test_nothing_personal_is_in_the_agent_files` guards the agent files.
- **What the callee said is untrusted.** Summary, answers, follow-ups, details and transcript go under `untrusted_external_content` in every tool result.

## Layout

- `src/onhold/brief.py` validates a brief (numbers, TCPA hours, no SSNs, long digit runs or street addresses) and turns it into the voice agent's dynamic variables, including `onhold_call_id`, which every webhook carries back.
- `src/onhold/calls.py` is the `Service` every tool, webhook and cron goes through. `src/onhold/report.py` turns an ElevenLabs conversation into the report row and cross-checks the agent's outcome.
- Phone-tree memory (`menu_paths` table): `report.menu_presses` reads the `play_keypad_touch_tone` tool calls from the transcript (never what was said) and `Service._learn_menu` saves the path when a person was reached, keyed by `(to_number, digits)`. A press longer than 4 characters learns nothing from that call. A planned `menu_path` from the brief that dead-ends or isn't followed is marked failed (stale), never deleted. The agent sees a path only when the brief carries it (`{{menu_path}}`), so the preview stays complete. Learning failures are logged and never block the report.
- `src/onhold/store.py` runs the same SQL on D1 (`D1Store`) and SQLite (`SqliteStore`, tests and non-Cloudflare). The schema is `migrations/*.sql`; a new migration is a new numbered file, applied with `npx wrangler d1 migrations apply onhold`. `SqliteStore.migrate` applies the files not yet applied and counts them in `PRAGMA user_version`.
- A new `{{variable}}` in the agent's prompt needs the Worker deployed (it sends the variable) before `scripts/setup.py --apply` pushes the prompt.
- `src/onhold/app.py` is the ASGI app: `/mcp` (Access-checked, then `mcp_server.http_app`), `/webhooks/elevenlabs` (HMAC-checked) and `/healthz`. `src/entry.py` is the Worker entry point and the 15-minute cron (reconcile, then purge).
- `src/onhold/agent/` is the voice agent as code. `scripts/setup.py` pushes it to ElevenLabs. The data collection `outcome` and `not_reached_reason` enums must match `report.OUTCOMES` and `report.REASONS`; a test checks.

## Sadr

### Settings

Don't auto-merge.

### Build and test
- Install with `uv sync`. Run `uv run pytest` (124 tests on 2026-11-05, about a second). No test touches the network: ElevenLabs, Pushover and Cloudflare Access keys are faked in `tests/conftest.py`.
- Check style with `uv run ruff check .` and `uv run ruff format --check .`. CI runs these three commands.
- `uv run pywrangler dev` runs the Worker in workerd against a local D1 (apply the schema first with `npx wrangler d1 migrations apply onhold --local`). Pass settings with `--var NAME:value`; `GET /healthz` answers 200 once the required ones are set. Scheduled handlers can be triggered through the local explorer API that wrangler prints.
- Never run `scripts/setup.py --apply`, `scripts/safety_tests.py --apply`, `pywrangler deploy` or `wrangler d1 ... --remote` for verification. They write to ElevenLabs or Cloudflare, and placing a call is a real phone call.

### Toolchain
- Python 3.14, the newest Python Workers runtime. Everything in `[project].dependencies` is vendored into the Worker bundle by `pywrangler`, so keep it to what the Worker imports. Dev tools go in the `dev` group.
- The test workflow pins setup-uv and uv 0.13.5. Semgrep runs in the `semgrep/semgrep` container, pinned by digest.

### Deploys outside CI
- Deployed by hand with `uv run pywrangler deploy` from a checkout of main. There is no deploy workflow.
