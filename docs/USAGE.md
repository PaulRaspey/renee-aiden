# Using Renée / Aiden (text mode)

This is a quick guide for the text-only path that's live today. Voice lands in M5+.

## First run

```cmd
cd C:\Users\Epsar\Desktop\renee-aiden
.venv\Scripts\activate
copy .env.template .env
```

Edit `.env` only if you want to override the Groq model or the Ollama host. The
bridge key in `~/.bridge_key` is read automatically.

## Talking to her

```cmd
python -m src.cli.chat
```

Or the launcher:

```cmd
scripts\chat.bat
scripts\chat.bat aiden
```

Arguments:

| flag | purpose |
|---|---|
| `--persona renee\|aiden` | which persona (default: renee) |
| `--backend groq\|ollama\|anthropic` | force a backend (default: router decides) |
| `--no-memory` | skip the memory stack, persona core only |
| `--config-dir` | alt persona config dir |
| `--state-dir` | alt state dir (mood, memory, identities) |
| `--max-history` | turns of rolling context (default 12) |

## Commands inside the session

| command | what it does |
|---|---|
| `/mood` | print the current six-axis mood vector and a one-line summary |
| `/memories` | last 10 stored memories with tier and valence |
| `/retrieve <query>` | preview retrieval without generating a response |
| `/receipt` | show the UAHP completion receipt for the last turn |
| `/stats` | session latency p50/p95/mean, backend mix, filter hits, tokens |
| `/save` | snapshot the conversation to `state/sessions/<persona>_<ts>.json` |
| `/load` | load the most recent saved session for this persona into history |
| `/baddayreset` | clear an active bad-day floor (mostly for testing) |
| `/quit` | exit |

## State layout

```
state/
  identities/                 # signing keys per agent (persona, memory, mood, ...)
  renee_mood.db               # Renée's persisted mood, drift log, bad-day state
  renee_memory.db             # her memory store
  aiden_mood.db               # Aiden's persisted mood
  aiden_memory.db             # his memory store
  metrics.db                  # per-turn telemetry for the eval harness
  sessions/                   # /save snapshots
```

`state/` is excluded from git via `.gitignore`. The identities files are signing
keys. Treat them like secrets.

## Verifying it's actually Renée

- `/mood` should change over time. If you're rude for a few minutes, patience
  drops. Leave her alone for a few hours and patience comes back up. Energy
  follows a circadian curve (lower at 3am, higher at noon).
- `/memories` should accumulate. Open a new session next week and `/retrieve`
  about something you told her last Tuesday. She should pull it up.
- `/receipt` should show a different signature every turn.
- `/stats` lets you track quality. `filter hits` should be low. `sycophancy`
  should be zero. p50 latency should stay under 2s on Groq.

## Running the acceptance suite

```cmd
python -m tests.acceptance.run_acceptance
```

Takes 8-10 minutes because of Groq's free-tier TPM limit. Writes
`tests/acceptance/last_run.md`. The suite validates:

- No sycophancy across 20 turns
- Pushback on 3 wrongness probes
- Opinion consistency across paraphrased prompts
- Reality anchor respected
- Circadian mood oscillation
- Patience drops under anger, recovers on idle
- Seeded memories surface naturally in follow-up turns

## Running the telemetry report

```cmd
python -m src.eval.report                  :: renee, all time
python -m src.eval.report --since 24h      :: last day only
python -m src.eval.report --persona aiden  :: aiden instead
```

Reads `state/metrics.db`. No server, just a text dump.

## Troubleshooting

### "model 'gemma4:e4b' not found"
Ollama host is reachable but the model isn't pulled. This is non-fatal: the
memory extractor falls back to a heuristic parser, and the main LLM stays on
Groq. To fix: `ollama pull gemma4:e4b` (or set `OLLAMA_MODEL` to something you
do have pulled).

### Rate limit retries
Groq's free tier is 6000 TPM for qwen3-32b. The router retries with backoff
when it sees 429. If you're hitting this all the time, upgrade at
https://console.groq.com/settings/billing.

### UTF-8 BOM on `~/.bridge_key`
Notepad saves with BOM by default. The router already strips it. If you see
a `UnicodeEncodeError` mentioning ASCII, the file got edited with something
that inserted Unicode whitespace. Re-save as plain ASCII.

### State got weird
Nuke `state/<persona>_mood.db`, `state/<persona>_memory.db`, and the session
starts fresh. Keep `state/identities/` if you want the same agent IDs.
