"""
LLM backend routing.

Routes per turn between:
  - Groq Qwen 3 32B (default deep turns, low latency cloud)
  - Ollama Gemma (local, fast simple turns)
  - Anthropic Sonnet (deep reasoning / long-form)

For text-first M2 we only need Groq + Ollama. Anthropic is wired but optional.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


logger = logging.getLogger("renee.llm_router")


OLLAMA_UNAVAILABLE_FALLBACK = (
    "I'm having trouble thinking right now. Give me a moment."
)


REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


try:
    from groq import Groq
except ImportError:  # pragma: no cover
    Groq = None  # type: ignore

try:
    import ollama  # type: ignore
except ImportError:  # pragma: no cover
    ollama = None  # type: ignore

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore


Backend = Literal["groq", "ollama", "anthropic"]


@dataclass
class LLMResponse:
    text: str
    backend: Backend
    model: str
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0


def _read_bridge_key() -> str | None:
    home = Path(os.path.expanduser("~"))
    candidates = [home / ".bridge_key", Path(".bridge_key")]
    for c in candidates:
        try:
            if not c.exists():
                continue
            # utf-8-sig strips a UTF-8 BOM if present (Notepad-style files)
            raw = c.read_text(encoding="utf-8-sig").strip().splitlines()
            if not raw:
                continue
            first = raw[0].strip()
            # file could be "gsk_xxx" directly or "GROQ_API_KEY=gsk_xxx"
            if "=" in first:
                first = first.split("=", 1)[1]
            key = first.strip().strip('"').strip("'")
            # final sanity: only ASCII permitted for HTTP header values
            try:
                key.encode("ascii")
            except UnicodeEncodeError:
                key = key.encode("ascii", errors="ignore").decode("ascii")
            return key or None
        except Exception:
            continue
    return None


class LLMRouter:
    def __init__(
        self,
        groq_model: str | None = None,
        ollama_model: str | None = None,
        anthropic_model: str | None = None,
        ollama_host: str | None = None,
    ):
        self.groq_model = groq_model or os.environ.get("GROQ_MODEL", "qwen/qwen3-32b")
        self.ollama_model = ollama_model or os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
        self.anthropic_model = anthropic_model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.ollama_host = ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

        groq_key = os.environ.get("GROQ_API_KEY") or _read_bridge_key()
        self.groq_client = Groq(api_key=groq_key) if (Groq and groq_key) else None
        self.anthropic_client = anthropic.Anthropic() if (anthropic and os.environ.get("ANTHROPIC_API_KEY")) else None
        self.ollama_client = ollama.Client(host=self.ollama_host) if ollama else None

    def decide_backend(self, user_text: str, expected_depth: str = "normal") -> Backend:
        # Prefer Groq whenever GROQ_API_KEY is set — on the pod, Ollama
        # isn't running and its client constructor is non-lazy about the
        # object but lazy about the connection, so routing to Ollama
        # there blows up at generate() time. Deep turns also prefer
        # cloud reasoning over the local fast model.
        if expected_depth == "deep":
            if self.groq_client is not None:
                return "groq"
            if self.anthropic_client is not None:
                return "anthropic"
            if self.ollama_client is not None:
                return "ollama"
            return "groq"

        if self.groq_client is not None:
            return "groq"
        if self.ollama_client is not None:
            return "ollama"
        return "anthropic"

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        backend: Backend | None = None,
        temperature: float = 0.85,
        max_tokens: int = 400,
        user_text: str | None = None,
    ) -> LLMResponse:
        if backend is None:
            backend = self.decide_backend(user_text or "", "normal")

        t0 = time.time()
        if backend == "groq":
            if self.groq_client is None:
                raise RuntimeError("Groq client unavailable (no GROQ_API_KEY / ~/.bridge_key).")
            full = [{"role": "system", "content": system_prompt}] + messages
            # Handle TPM rate limits on the free/on-demand tier by parsing the
            # recommended retry-after hint from Groq's 429 body and sleeping.
            attempts = 0
            while True:
                attempts += 1
                try:
                    resp = self.groq_client.chat.completions.create(
                        model=self.groq_model,
                        messages=full,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        reasoning_effort="none",  # qwen3 — voice, not CoT
                    )
                    break
                except Exception as e:
                    msg = str(e)
                    if "rate_limit_exceeded" not in msg or attempts > 6:
                        raise
                    wait = 3.0
                    m = re.search(r"try again in ([0-9.]+)s", msg)
                    if m:
                        try:
                            wait = float(m.group(1)) + 0.5
                        except ValueError:
                            pass
                    time.sleep(min(wait, 30.0))
            latency = (time.time() - t0) * 1000
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            return LLMResponse(
                text=text,
                backend="groq",
                model=self.groq_model,
                latency_ms=latency,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )

        if backend == "ollama":
            if self.ollama_client is None:
                logger.warning(
                    "Ollama backend requested but ollama package not installed; "
                    "returning fallback response."
                )
                return LLMResponse(
                    text=OLLAMA_UNAVAILABLE_FALLBACK,
                    backend="ollama",
                    model=self.ollama_model,
                    latency_ms=(time.time() - t0) * 1000,
                )
            full = [{"role": "system", "content": system_prompt}] + messages
            try:
                resp = self.ollama_client.chat(
                    model=self.ollama_model,
                    messages=full,
                    options={"temperature": temperature, "num_predict": max_tokens},
                )
            except Exception as e:
                # Ollama's client raises ConnectionError/httpx.ConnectError when
                # the local daemon isn't running. Rather than letting that
                # bubble up as a traceback in the conversation log, degrade
                # gracefully with a fixed response so the user hears something
                # coherent while the ops side gets fixed.
                logger.warning(
                    "Ollama backend unavailable (%s: %s); returning fallback response.",
                    type(e).__name__, e,
                )
                return LLMResponse(
                    text=OLLAMA_UNAVAILABLE_FALLBACK,
                    backend="ollama",
                    model=self.ollama_model,
                    latency_ms=(time.time() - t0) * 1000,
                )
            latency = (time.time() - t0) * 1000
            text = resp.get("message", {}).get("content", "") or ""
            return LLMResponse(
                text=text,
                backend="ollama",
                model=self.ollama_model,
                latency_ms=latency,
            )

        if backend == "anthropic":
            if self.anthropic_client is None:
                raise RuntimeError("Anthropic client unavailable.")
            resp = self.anthropic_client.messages.create(
                model=self.anthropic_model,
                system=system_prompt,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            latency = (time.time() - t0) * 1000
            text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
            return LLMResponse(
                text=text,
                backend="anthropic",
                model=self.anthropic_model,
                latency_ms=latency,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )

        raise ValueError(f"Unknown backend: {backend}")
