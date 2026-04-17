# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** M0 complete, M2/M3/M4 landing now
**Last commit:** `M0: scaffolding, UAHP identity, first tests`
**Next milestone:** M2 acceptance → M3 acceptance → M4 acceptance
**Blockers:** None

## What's Done

- [x] Full architecture specification
- [x] All seven stack deep dives
- [x] Persona configs for Renée and Aiden
- [x] Prosody rules starter
- [x] Humanness probe starter set
- [x] Bootstrap script
- [x] Safety framework
- [x] Copyright handling doc
- [x] Her script analysis pipeline spec
- [x] Git repo initialized, pushed to github.com/PaulRaspey/renee-aiden (private)
- [x] M0: UAHP identity primitives + agent-per-component keyfiles
- [x] M0: src scaffolding and dependency install
- [ ] M0: Audio I/O round-trip test (DEFERRED — text-first path per PJ)

## What's Next

- [ ] M2 acceptance: 20-prompt opinion consistency + pushback + no sycophancy
- [ ] M3 acceptance: mood drift over time, patience drop on disagreement, recovery on idle
- [ ] M4 acceptance: callback test, memory surfaces earlier details contextually
- [ ] M1 (ASR) — deferred until voice path comes back
- [ ] M5 (TTS) — deferred, needs reference audio + cloud GPU

## Known Risks

- T400 GPUs (4GB each) too weak for realtime stack. PJ plans rented GPU for M5 onward.
- Groq API key required for Qwen 3 32B. PJ has this at `~/.bridge_key`.
- XTTS-v2 model download is large (~2GB), first run will be slow.
- No reference audio for Renée/Aiden yet. PJ to record between M4 and M5.
- PyPI `uahp==0.5.4` wheel ships broken (imports missing modules). Vendored local
  identity shim in `src/identity/uahp_identity.py`. Swap in fixed upstream later
  without API change.

## Notes for PJ

- Start a chat: `python -m src.cli.chat` (Renée) or `python -m src.cli.chat --persona aiden`.
- Commands in the REPL: `/mood`, `/memories`, `/retrieve <query>`, `/receipt`, `/quit`.
- Mood persists in `state/<persona>_mood.db`, memories in `state/<persona>_memory.db`.
- Identities persist in `state/identities/*.key.json`. They are signing keys. Do NOT
  commit — `.gitignore` already covers `state/`.
- Upload *Her* script when ready for M12. Script text only, no audio.
- Record reference voice sessions ASAP — see `architecture/01_voice.md` for spec.
- Budget first month rent on RunPod around $400-500 for dev iteration.
