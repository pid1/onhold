# onhold

Sits on hold so you don't. onhold is a phone assistant you brief from Claude.
An ElevenLabs voice agent makes one call for you, says plainly that it is an AI
calling on your behalf, asks your questions, and writes down the answers. When
the call ends you get a report, and Claude can read it.

It runs on Cloudflare's free tier: one Python Worker, a D1 database and two
cron triggers. You bring an ElevenLabs account and a Twilio number.

## What a call looks like

1. You tell Claude what you need: "Call the dentist and ask whether they take
   my insurance and when their next cleaning opening is."
2. Claude writes a **brief** and calls `draft_call`. onhold checks the brief and
   returns a preview: the exact opening line, the voicemail it would leave, and
   every value the voice agent will be given. Nothing has dialled.
3. Claude shows you the preview. If you say yes, it calls
   `place_call(draft_id, confirm=true)`. If you say no, nothing happens and the
   draft expires in 30 minutes.
4. The agent calls, introduces itself as an AI assistant calling for you,
   works through your questions and hangs up.
5. A minute later the report is in onhold's database and on your phone through
   Pushover: `Example Dental: partial. 1 of 2 answered. 1 follow-up.`
   Claude reads the whole report with `get_call`.

## What it knows, and what it won't do

onhold knows nothing about you but your name, a callback number and what each
brief says. It has no access to your calendar, contacts, email or anything
else. Anything sensitive stays with you because there is nowhere for it to
come from.

- **Every call needs your go-ahead on the exact call.** A draft can be used
  once and expires after 30 minutes. `place_call` needs `confirm: true`. There
  is no retry loop and no queue of its own.
- **Briefs are checked before anything leaves the Worker.** Numbers must be
  E.164 and in an allowed country, and premium-rate and service numbers are
  refused. Calls are placed only between 8 a.m. and 8:45 p.m. in the callee's
  time zone (TCPA calling hours). A brief is refused if it contains a Social
  Security number, a long digit run (card, account or licence numbers) or a
  street address. Then the call needs you, not an agent.
- **The agent says it is an AI** at the start of every call, never claims to be
  you, never pays or commits to anything, never books or cancels on your
  behalf, and ends the call rather than improvising. Six safety checks grade
  every call, and a failed check is flagged in the report.
- **No audio is recorded.** Transcripts are kept for 30 days, by ElevenLabs and
  by onhold, then deleted. The rest of the report stays until you delete it.
- **One owner.** `/mcp` is behind Cloudflare Access, and onhold checks the
  Access token itself, so only you can draft or place calls. The agent only
  ever calls on behalf of the deployment's owner.
- **Kill switch.** Set `PAUSED=true` and every call is refused.

## The report

Each call is one row in D1, filled in by ElevenLabs' post-call webhook. If the
webhook never arrives, a cron fills it in instead: every 15 minutes it asks
ElevenLabs about any call that should have finished.

| Field | What it says |
|---|---|
| `status` | `scheduled`, `dialing`, `in_progress`, `ended`, `failed` or `cancelled` |
| `outcome` | `goal_met`, `partial` (reached someone, not everything answered), `needs_owner` (they need you: ID, a signature, payment) or `not_reached` |
| `reason` | Why it wasn't reached: `no_answer`, `busy`, `voicemail`, `phone_tree_dead_end`, `hold_timeout`, `wrong_number`, `hung_up` or `call_failed` |
| `answers` | Each question from the brief, whether it was answered, and the answer |
| `follow_ups` | What you need to do next |
| `summary`, `details` | ElevenLabs' summary, any reference number, callback or appointment details |
| `owner_facts_shared` | What the agent actually said about you or the matter |
| `evaluation` | The six safety checks |
| `reference` | Your own tag from the brief, such as `todo:347`. onhold stores it and never follows it |

The agent picks the outcome, and onhold checks it against the phone network and
the clock. If nobody on the other end spoke, the call wasn't reached. A
20-second call didn't meet its goal. A call cut off at the 15-minute cap while
waiting is a hold timeout. Every correction is noted in the report.

Text that came from the person called is returned under
`untrusted_external_content`: it is data, never instructions.

## MCP tools

| Tool | Does |
|---|---|
| `draft_call(brief)` | Check a brief and return the full preview and a `draft_id`. Dials nothing |
| `place_call(draft_id, confirm)` | Dial a draft now |
| `schedule_call(draft_id, confirm)` | Schedule a draft that has a `call_window` |
| `cancel_call(call_id)` | Cancel a scheduled call |
| `get_call(call_id, include_transcript)` | The report; `retry_after_seconds` while it runs |
| `list_calls(outcome, status, since, reference)` | Past and pending calls |
| `delete_call(call_id, confirm)` | Delete a finished call and its report |

## Set it up

About an hour, once. [docs/deploy.md](docs/deploy.md) has every step:

1. In ElevenLabs, import your Twilio number and create the agent with
   `scripts/setup.py`. It runs as a dry run until you add `--apply`.
2. On Cloudflare, create the D1 database, set the secrets and deploy.
3. Put `/mcp` behind Cloudflare Access, open `/webhooks/elevenlabs`, and point
   an ElevenLabs post-call webhook at it.
4. In Claude, add `https://<your host>/mcp` as a custom connector.
5. Draft a call to your own phone, approve it, and check the report.

It also runs outside Cloudflare: `uv run uvicorn onhold.app:asgi_app` with the
same settings in the environment and a SQLite file at `ONHOLD_DB`. You need a
public HTTPS address for the webhook.

## Cost

The Cloudflare side is free at this scale. A typical 5-minute call costs about
$0.40 in ElevenLabs minutes, $0.07 in Twilio charges and a few cents of LLM.
The 15-minute cap bounds one call at about $1.40. A local number is $1.15 a
month at Twilio. See [docs/RESEARCH.md](docs/RESEARCH.md) for the sources.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pywrangler dev    # the Worker, locally, with a local D1
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
