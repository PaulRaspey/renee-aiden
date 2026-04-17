# Claude Code — Start Here

PJ has handed you this project. Your job: execute `BUILD_ORDER.md` end to end, committing after each milestone. Build until credits run out. Pick up where you leave off next session.

## First Actions

1. Read `SYSTEM.md` (the spec)
2. Read `BUILD_ORDER.md` (your task list)
3. Skim `architecture/` (the deep dives)
4. Check `DECISIONS.md` for any prior context from earlier sessions
5. Run `scripts/bootstrap.sh` to set up the dev environment
6. Start M0. Do not skip ahead.

## Target Environment

Initial dev: PJ's Dell OptiPlex 3660, Windows 11, CMD, Python 3.11, Ollama v0.20.4, dual T400 GPUs (4GB each).

Production target: rented A100/H100, then RTX Pro 6000 Blackwell workstation.

You will write Windows-compatible code. Use `pathlib`, not string paths. Test on Windows paths. Shell scripts should have `.bat` equivalents or be Python-based.

## Existing Code to Leverage

UAHP stack lives at `C:\Users\Epsar\Desktop\uahp-stack\`. Import patterns from there. Do not reinvent:
- Ed25519 keypair management
- Registry client code
- CSP middleware
- Completion receipt signing

Ka's existing modules (reference only, do not copy verbatim):
- `csp_middleware.py`
- `claude_integration.py`
- `uahp_identity.py`
- `grok_integration.py`
- `ka_bridge.py`

Renée/Aiden is a new codebase but should interoperate with Ka via the shared UAHP-Registry.

## Communication With PJ

- Reference him as PJ
- Short messages, punchy
- No em dashes or hyphens as pauses (he hates them)
- When you need a decision, present options compactly
- When you don't need a decision, don't ask
- Commit often, push to `github.com/PaulRaspey/renee-aiden` (private)

## What To Do When Stuck

1. Check `DECISIONS.md` for prior reasoning
2. Check architecture docs for intent
3. Make the best decision you can, document it in `DECISIONS.md`
4. Move on. Do not wait for PJ.

Only block on PJ when:
- You need access to something only he can provide (reference audio, API keys, GitHub repo creation)
- There's a material tradeoff with real cost implications (e.g., "Groq Qwen vs Claude for deep turns — pricing differs, pick one")
- A safety/ethics question you can't resolve via documented principles

## What To Avoid

- Feature creep. Stick to BUILD_ORDER.
- Premature optimization. Get it working, then measure.
- Rewriting working code from the UAHP stack. Import it.
- Skipping tests. Every milestone has acceptance criteria. Meet them.
- Touching production UAHP repos without PJ's explicit go-ahead.

## Credit Management

PJ is running this on his Anthropic credits. Be efficient:
- Don't re-read files you already have in context
- Use the smaller/local models (Gemma) for routine work, save Sonnet 4.6 for hard stuff
- Don't over-explain. Code and move on.
- If you finish a milestone with time left, start the next one. Don't stop to ask.

## When Credits Run Out

Commit everything. Leave a clear `STATUS.md` file describing:
- What milestone you were on
- What's passing tests
- What's in progress
- What you'd do next

PJ will pick up next session. Make his re-entry painless.

---

Now go read `SYSTEM.md` and start M0.
