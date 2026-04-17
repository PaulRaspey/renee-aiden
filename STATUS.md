# Build Status

Claude Code updates this file at the end of each work session. PJ reads it first on return.

---

## Current State

**Phase:** Pre-M0 (architecture complete, implementation not started)
**Last commit:** initial architecture drop
**Next milestone:** M0 — Foundation
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

## What's In Progress

Nothing. Ready for Claude Code to pick up.

## What's Next (M0)

- [ ] Create GitHub repo `PaulRaspey/renee-aiden` (private)
- [ ] Run `scripts/bootstrap.py`
- [ ] Set up Python venv
- [ ] Install UAHP and dependencies
- [ ] Wire basic UAHP identities for Renée and Aiden
- [ ] Implement `src/voice/audio_io.py` with mic capture and VAD
- [ ] Round-trip test: speak, transcribe, fixed response playback

## Known Risks

- T400 GPUs (4GB each) too weak for realtime stack. PJ plans rented GPU for M5 onward.
- Groq API key required for Qwen 3 32B. PJ has this at `~/.bridge_key`.
- XTTS-v2 model download is large (~2GB), first run will be slow.
- No reference audio for Renée/Aiden yet. PJ to record between M4 and M5.

## Notes for PJ

- Architecture is complete. Read `SYSTEM.md` to orient.
- Hand `CLAUDE_CODE_HANDOFF.md` to Claude Code to start.
- Upload *Her* script when ready for M12. Script text only, no audio.
- Record reference voice sessions ASAP — see `architecture/01_voice.md` for spec.
- Budget first month rent on RunPod around $400-500 for dev iteration.
