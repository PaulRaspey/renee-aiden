"""
PII scrubber (M13 / SAFETY.md §PII and Privacy).

CSP-style tokenizer. Scrubs user-identifying strings, child names,
addresses, emails, and phone numbers before we hand text to a cloud
LLM. Mappings are reversible so responses can be unscrubbed before
they reach the user.

The scrubber runs in `PersonaCore.respond()` bracketing the LLM call:
    scrubbed, mapping = scrubber.scrub(user_text)
    resp = llm.generate(scrubbed, ...)
    real = scrubber.unscrub(resp.text, mapping)

Mapping is ephemeral per turn — not persisted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .config import PIIScrubberConfig


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
# North-American-leaning phone pattern; also accepts international-prefixed.
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)"
)


@dataclass
class ScrubResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    def unscrub(self, other_text: str) -> str:
        """Replace tokens with originals in any downstream text."""
        out = other_text
        # Sort descending so longer tokens substitute first (e.g. <ADDRESS_1>
        # before <ADDRESS_10>) — dict order alone isn't guaranteed to do that.
        for token in sorted(self.mapping.keys(), key=len, reverse=True):
            original = self.mapping[token]
            out = out.replace(token, original)
        return out


class PIIScrubber:
    """
    Regex + exact-string scrubber with stable per-run mappings.

    Design notes:
      - Name replacements are whole-word boundary matches. "Paul" won't
        scrub inside "Paulo" — the \b anchors handle this.
      - Aliases scrubbed to the same token as the canonical name so the
        LLM sees one consistent referent.
      - Child / address lists get indexed tokens (<CHILD_1>, <CHILD_2>,
        <ADDRESS_1>, ...). Order preserved across calls so test fixtures
        remain stable.
      - Emails and phones get monotonically-numbered tokens per scrub call.
    """

    def __init__(
        self,
        *,
        user_name: str = "",
        user_aliases: Optional[Iterable[str]] = None,
        child_names: Optional[Iterable[str]] = None,
        addresses: Optional[Iterable[str]] = None,
        sensitive_tokens: Optional[Iterable[str]] = None,
        scrub_emails: bool = True,
        scrub_phones: bool = True,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.user_name = user_name or ""
        self.user_aliases = [a for a in (user_aliases or []) if a]
        self.child_names = [c for c in (child_names or []) if c]
        self.addresses = [a for a in (addresses or []) if a]
        self.sensitive_tokens = [s for s in (sensitive_tokens or []) if s]
        self.scrub_emails = scrub_emails
        self.scrub_phones = scrub_phones

    @classmethod
    def from_config(cls, cfg: PIIScrubberConfig) -> "PIIScrubber":
        return cls(
            user_name=cfg.user_name,
            user_aliases=cfg.user_aliases,
            child_names=cfg.child_names,
            addresses=cfg.addresses,
            sensitive_tokens=cfg.sensitive_tokens,
            scrub_emails=cfg.scrub_emails,
            scrub_phones=cfg.scrub_phones,
            enabled=cfg.enabled,
        )

    # -------------------- scrub --------------------

    def scrub(self, text: str) -> ScrubResult:
        if not self.enabled or not text:
            return ScrubResult(text=text or "")
        mapping: dict[str, str] = {}
        out = text

        # User name + aliases all collapse to <USER>.
        names = ([self.user_name] if self.user_name else []) + list(self.user_aliases)
        # Longest first so "Paul Raspey" scrubs before "Paul".
        for name in sorted(set(n for n in names if n), key=len, reverse=True):
            pat = re.compile(rf"\b{re.escape(name)}\b")
            if pat.search(out):
                token = "<USER>"
                out = pat.sub(token, out)
                mapping[token] = self.user_name or name

        for i, child in enumerate(self.child_names, start=1):
            pat = re.compile(rf"\b{re.escape(child)}\b")
            if pat.search(out):
                token = f"<CHILD_{i}>"
                out = pat.sub(token, out)
                mapping[token] = child

        for i, addr in enumerate(self.addresses, start=1):
            if addr and addr in out:
                token = f"<ADDRESS_{i}>"
                out = out.replace(addr, token)
                mapping[token] = addr

        for i, secret in enumerate(self.sensitive_tokens, start=1):
            if secret and secret in out:
                token = f"<SENSITIVE_{i}>"
                out = out.replace(secret, token)
                mapping[token] = secret

        if self.scrub_emails:
            counter = {"n": 0}
            def email_sub(m: re.Match) -> str:
                counter["n"] += 1
                tok = f"<EMAIL_{counter['n']}>"
                mapping[tok] = m.group(0)
                return tok
            out = EMAIL_RE.sub(email_sub, out)

        if self.scrub_phones:
            counter = {"n": 0}
            def phone_sub(m: re.Match) -> str:
                counter["n"] += 1
                tok = f"<PHONE_{counter['n']}>"
                mapping[tok] = m.group(0)
                return tok
            out = PHONE_RE.sub(phone_sub, out)

        return ScrubResult(text=out, mapping=mapping)

    def unscrub(self, text: str, mapping: dict[str, str]) -> str:
        if not mapping:
            return text
        out = text
        for token in sorted(mapping.keys(), key=len, reverse=True):
            out = out.replace(token, mapping[token])
        return out
