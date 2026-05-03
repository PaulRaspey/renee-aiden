# v0.6.0-launcher — single-button operator stack

Stacks 11 commits on `feat/session-capture` (started from
`v0.5.5-uahp-gap` + the original Part 2 capture work). Closes the
deferred Part 2 audio_tap wiring, builds out a full single-button
launcher, and wires the trust + handoff bridges (Beacon, Memory
Bridge) end-to-end.

Tests: 657 → 823 (+166), zero regressions.

## Highlights

### Closes UAHP Part 2

- Pod-side `register_audio_tap` per-connection wiring in
  `src/server/audio_bridge.py`. Phone-path sessions now auto-record
  when `RENEE_RECORD=1` is set on the pod.
- Topic propagation end-to-end: `--topic` → URL `?topic=` → PWA
  `set_topic` WS frame → `orchestrator.set_session_topic` →
  topic-aware `greet_on_connect`. Topic also lands in the
  conversation log header.

### Single-button launcher

- `scripts/start_session.bat` (and `.ps1`) — one keypress launches
  Tailscale check, pod wake (with STARTING retry), dashboard,
  mobile proxy with HTTPS + QR. Ctrl+C tears everything down,
  prints cost, runs triage, prompts for 1-5 score.
- Flags: `--topic`, `--gpu cheap|default|best`, `--auto-provision`
  (with `--with-volume-setup`), `--with-beacon`,
  `--with-memory-bridge`, `--no-triage-on-stop`,
  `--no-score-prompt`.
- Pre-flight gates: Tailscale (with TAILSCALE_AUTHKEY auto-up),
  RunPod status, Beacon health (soft), daily-cap budget.

### Trust stack

- `src/uahp/beacon_client.py` — register + heartbeat to Beacon. Wired
  into `cloud_startup.py`.
- `src/server/beacon_receiver.py` — HMAC-SHA256 verifier + JSONL
  journal for incoming agent.death webhooks. Dashboard endpoints
  `/api/beacon/webhook` (POST) + `/api/beacon/deaths` (GET) +
  Health-tab UI.
- `renee beacon-setup --url <beacon> [--agent-id ... --webhook-url ...]`
  fetches the public key + optionally PATCHes the agent's webhook URL.

### Handoff to Claude

- `src/client/memory_bridge_client.py` — POSTs `/v1/handoffs` at
  session end with topic + cost + pod_id + session_dir summary.
  Skipped silently when `MEMORY_BRIDGE_URL` + token aren't set.

### Cost tracking

- `src/client/cost_ledger.py` — SQLite ledger of pod up/down events.
- `/api/cost` (current session) + `/api/cost/history` (today/month
  totals) endpoints.
- Header badge on the dashboard SPA, polled every 30s.
- Cost summary printed on launcher Ctrl+C.

### Phone-side status page

- `/status` route on the proxy_server with pod state, cost, daily cap,
  Beacon liveness, current topic.
- Topic input + set button (POSTs `/api/topic`).
- "Stop the pod" button (POSTs `/api/sleep`).

### Operator CLI

- `renee dashboard` — open M15 dashboard in browser
- `renee logs [--day Y-M-D] [-n N] [-f]` — tail conversation logs
- `renee preflight` — run all pre-flight gates without starting a session
- `renee migrate-secrets` — env → keyring (one-time)
- `renee beacon-setup --url ...` — fetch key + register webhook
- `renee backup [--check] [--force]` — one-shot backup
- `renee fetch-logs` — SFTP conversation logs from pod
- `renee version` — build + dependency snapshot
- `renee.api` Python surface: `pod_status`, `wake_pod`,
  `triage_session`, `cost_summary`, etc. (no more shelling out)

### Other additions

- `renee.secrets` — keyring-backed get/set with env fallback
- `src/client/backup.py` — tar.gz of chain root + manifests
  (excludes WAVs); retention pruning; `enabled` from
  `deployment.yaml`
- `src/client/pod_manager.py` — `provision()` with auto-volume-setup;
  `GPU_TIERS` map; `_persist_pod_id` YAML rewrite
- Phone PWA cert-overlay (`src/client/web/client.js`) on repeated
  WS-handshake failure
- `tests/integration/test_chaos.py` — failure-mode tests
- `tests/integration/test_path_b_transcode.py` — real ffmpeg
  webm→PCM round-trip

## Known caveats

1. `renee fetch-logs` requires `paramiko` installed.
2. Beacon co-process spawn (`--with-beacon`) requires `pnpm install`
   in the Beacon repo.
3. `provision()` `network_volume_id` is best-effort (older RunPod
   SDK versions reject it; falls back to bare-kwargs create).
4. Phone status page untested on real iOS Safari; logic verified by
   tests.

## Commits in this stack

- `e2e541e` feat(uahp): Beacon heartbeat client + cloud_startup wiring (Step 25)
- `5fe579e` docs: STATUS Step 25 + SESSION_TONIGHT runbook
- `d780d6b` feat(scripts): one-button session launcher
- `7a261c6` feat: session launcher v2 (single-button startup, 10 features)
- `32cf708` feat(uahp): pod-side audio_tap wiring + topic propagation (#1, #2)
- `27d14e7` feat: launcher v3 — Python API, secrets, ledger, status page, chaos tests
- `7838fe7` docs: STATUS Steps 27 & 28 + SESSION_TONIGHT flag/env reference
- `84aaa2c` feat: operator round 4 — dashboard/logs CLI, cap pre-flight, memory bridge, beacon receiver
- `db22576` feat: surface beacon/cost in dashboard, beacon-setup CLI, topic UI on phone
- `1a79cf4` feat: round 6 — eval prompt, daily cap on phone, topic in log, backup hook
- `a3db613` feat: round 7 — STATUS log, runbook flags, renee backup + preflight CLI
