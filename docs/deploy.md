# Deploying onhold

About an hour, once. You need:

- an **ElevenLabs** account on a paid plan (Starter is enough for a few calls a
  week; see [RESEARCH.md](RESEARCH.md#d-plan-tier-and-pricing));
- a **Twilio** number that nothing else answers, on an upgraded (not trial)
  account;
- a **Cloudflare** account (the free plan is enough) with a domain on it and
  **Zero Trust** enabled;
- [uv](https://docs.astral.sh/uv/) and Node.js (for `npx wrangler`).

Keep secrets out of your shell history and out of the repository. Each command
below that needs one reads it from the environment.

## 1. ElevenLabs

1. **API key.** In ElevenLabs, create a restricted key with *ElevenAgents:
   Write* and *Voices: Read*, plus *Pronunciation Dictionaries: Write* if you
   will add pronunciation rules. Give it a credit quota as a spending cap.
2. **Phone number.** Under *Agents > Phone numbers*, import the Twilio number
   (inbound and outbound, no agent assigned). Leave SMS off if the number
   sends texts through Twilio for anything else. Then read back its id:

   ```bash
   ELEVENLABS_API_KEY=… uv run scripts/setup.py phone-number
   ELEVENLABS_API_KEY=… uv run scripts/setup.py phone-number --phone-number-id phnum_…
   ```

   The second prints `PHONE_NUMBER_ID` and `ASSISTANT_NUMBER`.
3. **The agent.** A dry run first, then for real:

   ```bash
   ELEVENLABS_API_KEY=… uv run scripts/setup.py agent --owner-name "Your Name"
   ELEVENLABS_API_KEY=… uv run scripts/setup.py agent --owner-name "Your Name" --apply
   ```

   This adds the default voice from the Voice Library, creates the agent from
   `src/onhold/agent/` and prints `AGENT_ID`. Re-running updates the agent to
   match the files. To update an agent you already have, pass `--agent-id`.
   If the voice says your name wrong, put alias rules in a local file, such as
   `[{"type": "alias", "string_to_replace": "Nguyen", "alias": "Win"}]`, and
   pass `--pronunciation-rules FILE`. Don't commit that file.
4. **Safety tests.** Optional, but worth it once:

   ```bash
   ELEVENLABS_API_KEY=… AGENT_ID=agent_… OWNER_NAME="Your Name" uv run scripts/safety_tests.py --apply
   ```

   The results are in the agent's *Tests* tab.

## 2. Cloudflare: the Worker and its database

```bash
npx wrangler login
npx wrangler d1 create onhold
npx wrangler d1 migrations apply onhold --remote
```

In `wrangler.jsonc`, put the `database_id` it printed in `d1_databases` and
your own hostname in `routes`, on a zone in your Cloudflare account. It is a
custom domain, so the deploy creates the DNS record and certificate.

Set the secrets. Each command prompts for its value:

```bash
for name in ELEVENLABS_API_KEY AGENT_ID PHONE_NUMBER_ID ASSISTANT_NUMBER OWNER_NAME; do
  npx wrangler secret put "$name"
done
npx wrangler secret put CALLBACK_NUMBER     # optional: the number the agent gives for callbacks
npx wrangler secret put PUSHOVER_TOKEN      # optional, with PUSHOVER_USER
npx wrangler secret put PUSHOVER_USER
```

Then deploy:

```bash
uv run pywrangler deploy
curl https://onhold.example.com/healthz
```

`/healthz` answers `503` and names the setting still missing,
`ELEVENLABS_WEBHOOK_SECRET`, until step 4. `/mcp` refuses everything until
step 3.

## 3. Cloudflare Access in front of /mcp

In Zero Trust:

1. **Access application.** A self-hosted application on the whole hostname,
   `onhold.example.com`. Turn on **Managed OAuth** with Dynamic Client
   Registration, and allow the redirect URIs
   `https://claude.ai/api/mcp/auth_callback`,
   `https://claude.com/api/mcp/auth_callback` and loopback URIs (for Claude
   Code). Add one **Allow** policy that includes exactly your email, and one
   login method.
2. **Bypass applications** for the two paths ElevenLabs and monitoring must
   reach without a login: `onhold.example.com/webhooks/elevenlabs` and
   `onhold.example.com/healthz`, each with a **Bypass** policy that includes
   everyone. The webhook checks ElevenLabs' signature on every request.
3. Copy the application's **AUD tag**, then set:

   ```bash
   npx wrangler secret put ACCESS_TEAM_DOMAIN   # myteam.cloudflareaccess.com
   npx wrangler secret put ACCESS_AUD
   npx wrangler secret put OWNER_EMAIL          # the email your Access login uses
   ```

The Worker checks the Access token itself on every `/mcp` request: the
signature against your team's keys, the issuer, this application's AUD and,
with `OWNER_EMAIL` set, that it is you. Without the Access settings `/mcp`
refuses everything.

## 4. The post-call webhook

In ElevenLabs, *Agents > Settings > Webhooks*:

1. Create a webhook with HMAC authentication for
   `https://onhold.example.com/webhooks/elevenlabs`. Turn **retries on**.
2. Set its secret on the Worker: `npx wrangler secret put ELEVENLABS_WEBHOOK_SECRET`.
3. Attach it to the agent, with the transcript and call-initiation-failure
   events, by re-running setup with its id:

   ```bash
   ELEVENLABS_API_KEY=… uv run scripts/setup.py agent --owner-name "Your Name" --agent-id agent_… --webhook-id … --apply
   ```

`/healthz` should now answer `{"ok": true, "paused": false}`. If a webhook is
ever lost, the 15-minute reconcile cron fills in the report from ElevenLabs.

## 5. Claude

Add `https://onhold.example.com/mcp` as a custom connector (*Settings >
Connectors > Add custom connector*) and sign in through Access. For Claude
Code:

```bash
claude mcp add --transport http onhold https://onhold.example.com/mcp
```

## 6. Prove it

1. Ask Claude to draft a call to your own phone. Read the preview.
2. Approve it. Answer, let the agent ask its questions, and answer some of them.
3. Within a couple of minutes you should get a Pushover message, and
   `get_call` should show `goal_met` or `partial`, with your answers and the
   six safety checks.
4. Draft and approve a second call to your phone, and don't answer it. It must
   come back `not_reached` with the reason `no_answer` or `voicemail`.
5. Check `no_sensitive_info_shared` on the first call. It is graded against the
   brief's `may_share` list, which assumes ElevenLabs fills dynamic variables
   into evaluation prompts. If it flags something that was on the list, the
   assumption is wrong; `owner_facts_shared` still shows what was said.

## Optional: let the agent see when you're free, and hold a time

onhold never reads a calendar, and nothing in this repository connects to one.
If you want the voice agent to offer times on a call, or to note a time the
other side holds for you, you can give *your* agent that in ElevenLabs. That's
agent configuration you own, not an onhold feature.

With [Rally](https://github.com/pid1/rally), create a connection for the agent
with the **Busy-only** preset (see Rally's `docs/mcp.md`). It gets three tools
and nothing else:

- `check_availability` and `find_free_slots`: merged busy and free blocks,
  rounded to 15 minutes, with no titles, places or attendees.
- `book_appointment`: puts a time on the family calendar marked **pending,
  not yet reviewed**, until someone in the family confirms, edits or deletes
  it. The agent can't cancel, move or confirm anything.

Set it up like this:

- **Lock it down.** Make it a token connection for this one agent, and use the
  ElevenLabs IP preset on its allowlist. For US workspaces those addresses are
  `34.67.146.145` and `34.59.11.47`
  ([source](https://elevenlabs.io/docs/eleven-api/resources/ip-allowlisting)).
  Never give it any other tools.
- **Attach it in ElevenLabs.** Add it as a custom MCP server (Streamable HTTP,
  `Authorization: Bearer <token>`) that allows only those three tools, and
  attach it to the onhold agent. `scripts/setup.py` sends `mcp_server_ids: []`,
  so re-running it detaches the server. Attach it again afterwards.
- **Change the guardrails on purpose.** The agent's standing rules forbid
  sharing anyone's schedule and booking anything (`STANDING_DO_NOT_SHARE` in
  `brief.py`, and the Guardrails in `system_prompt.md`). Leave them as they
  are and the agent won't read out times or use the booking tool. Allow it per
  call in the brief: `may_share` ("Free times this week from the availability
  tool") and the goal ("If they offer a time, ask them to hold it and record
  it with book_appointment as pending"). Expect the sensitive-information check
  to look closely at those calls.

## Pausing
## Pausing

Set `"PAUSED": "true"` under `vars` in `wrangler.jsonc` and redeploy. Every `place_call` and `schedule_call` is then refused.
Cancel anything already scheduled with `cancel_call`.

## Running it somewhere else

The same app runs under uvicorn with the settings in the environment and a
SQLite database:

```bash
ONHOLD_DB=/data/onhold.db uv run uvicorn onhold.app:asgi_app --app-dir src --port 8080
```

You then need your own HTTPS front end, Cloudflare Access (or a proxy that
adds an equivalent signed token) in front of `/mcp`, and cron entries for the
two jobs:

```bash
ONHOLD_DB=/data/onhold.db uv run --directory src python -m onhold.jobs reconcile   # every 15 minutes
ONHOLD_DB=/data/onhold.db uv run --directory src python -m onhold.jobs purge       # once a day
```
