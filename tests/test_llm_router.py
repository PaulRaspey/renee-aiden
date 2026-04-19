"""Fallback behaviour of the LLM router.

Purpose: the voice loop must never stall when Gemma on the T400 runs
out of VRAM or the Ollama daemon is stopped. When the chosen backend
fails, the router must cascade to the next available backend (Groq by
default), and only fall through to a canned response when every
backend has failed.
"""
from __future__ import annotations

import time

import pytest

from src.persona.llm_router import LLMResponse, LLMRouter, OLLAMA_UNAVAILABLE_FALLBACK


class _FakeClient:
    """Base for Groq/Ollama/Anthropic client stubs."""


class _GroqClient(_FakeClient):
    def __init__(self, text: str = "Groq reply", usage_prompt: int = 5, usage_completion: int = 3):
        self.text = text
        self.chat = self.ChatNs(self)

    class ChatNs:
        def __init__(self, outer):
            self.outer = outer
            self.completions = self
        def create(self, **kw):
            class Msg: content = None
            Msg.content = self.outer.text
            class Choice: message = Msg
            class Usage: prompt_tokens = 5; completion_tokens = 3
            class Resp: choices = [Choice]; usage = Usage
            return Resp


class _OllamaClientFails(_FakeClient):
    def chat(self, **kw):
        raise ConnectionError("gemma daemon not reachable")


class _OllamaClientOK(_FakeClient):
    def chat(self, **kw):
        return {"message": {"content": "Gemma reply"}}


@pytest.fixture
def router_with_clients():
    """Build an LLMRouter without touching any real client class."""
    r = LLMRouter.__new__(LLMRouter)
    r.groq_model = "qwen3-32b"
    r.ollama_model = "gemma4:e4b"
    r.anthropic_model = "claude-sonnet"
    r.ollama_host = "http://localhost:11434"
    r.groq_client = None
    r.ollama_client = None
    r.anthropic_client = None
    return r


def test_cascades_from_failed_ollama_to_groq(router_with_clients):
    r = router_with_clients
    r.ollama_client = _OllamaClientFails()
    r.groq_client = _GroqClient("saved by groq")
    t0 = time.time()
    resp = r.generate("sys", [{"role": "user", "content": "hi"}], backend="ollama")
    elapsed = time.time() - t0
    assert isinstance(resp, LLMResponse)
    assert resp.text == "saved by groq"
    assert resp.backend == "groq"
    # Must not stall: a local fallback on a healthy Groq path should
    # finish in well under a second in this fake setup.
    assert elapsed < 1.0


def test_cascades_to_canned_when_all_backends_fail(router_with_clients):
    r = router_with_clients
    r.ollama_client = _OllamaClientFails()

    class GroqDead:
        class chat:
            class completions:
                @staticmethod
                def create(**kw): raise RuntimeError("groq 500")
    r.groq_client = GroqDead()
    resp = r.generate("sys", [{"role": "user", "content": "hi"}], backend="ollama")
    assert resp.text == OLLAMA_UNAVAILABLE_FALLBACK
    assert resp.model == "fallback"


def test_no_fallback_when_allow_fallback_false(router_with_clients):
    r = router_with_clients
    r.ollama_client = _OllamaClientFails()
    r.groq_client = _GroqClient("should not be called")
    with pytest.raises(ConnectionError):
        r.generate(
            "sys", [{"role": "user", "content": "hi"}],
            backend="ollama", allow_fallback=False,
        )


def test_decide_backend_prefers_groq_when_key_set(router_with_clients):
    r = router_with_clients
    r.groq_client = _GroqClient()
    r.ollama_client = _OllamaClientOK()
    assert r.decide_backend("hi") == "groq"


def test_decide_backend_uses_ollama_when_only_local_available(router_with_clients):
    r = router_with_clients
    r.ollama_client = _OllamaClientOK()
    assert r.decide_backend("hi") == "ollama"


def test_available_backends_orders_preferred_first(router_with_clients):
    r = router_with_clients
    r.groq_client = _GroqClient()
    r.ollama_client = _OllamaClientOK()
    order = r._available_backends("ollama")
    assert order[0] == "ollama"
    assert "groq" in order
    # ollama must not appear twice.
    assert order.count("ollama") == 1
