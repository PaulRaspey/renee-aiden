"""Tests for src.safety.pii_scrubber."""
from __future__ import annotations

from src.safety.config import PIIScrubberConfig
from src.safety.pii_scrubber import PIIScrubber, ScrubResult


def _scrubber(**kwargs) -> PIIScrubber:
    defaults = dict(
        user_name="Paul Raspey",
        user_aliases=["PJ", "Paul"],
        child_names=["Henry", "Aria"],
        addresses=["123 Example Dr, Dallas TX"],
        sensitive_tokens=["Closer Capital"],
        scrub_emails=True,
        scrub_phones=True,
        enabled=True,
    )
    defaults.update(kwargs)
    return PIIScrubber(**defaults)


def test_disabled_scrubber_returns_text_untouched():
    s = _scrubber(enabled=False)
    r = s.scrub("Paul Raspey and PJ.")
    assert r.text == "Paul Raspey and PJ."
    assert r.mapping == {}


def test_scrubs_user_name_and_aliases_to_single_token():
    s = _scrubber()
    r = s.scrub("Paul Raspey said PJ called Paul back.")
    assert "<USER>" in r.text
    assert "Paul" not in r.text
    assert r.mapping["<USER>"] == "Paul Raspey"


def test_does_not_scrub_name_substring_matches():
    s = _scrubber()
    r = s.scrub("Paulo went to the store. Also, PJ did not.")
    # "Paulo" is NOT "Paul" with word boundaries.
    assert "Paulo" in r.text
    # PJ becomes <USER>.
    assert "<USER>" in r.text


def test_scrubs_child_names_to_indexed_tokens():
    s = _scrubber()
    r = s.scrub("Henry ate dinner with Aria.")
    assert "Henry" not in r.text and "Aria" not in r.text
    assert "<CHILD_1>" in r.text
    assert "<CHILD_2>" in r.text


def test_scrubs_address_and_sensitive_tokens():
    s = _scrubber()
    r = s.scrub("We met at 123 Example Dr, Dallas TX after Closer Capital called.")
    assert "<ADDRESS_1>" in r.text
    assert "<SENSITIVE_1>" in r.text
    assert "Closer Capital" not in r.text


def test_scrubs_email_addresses():
    s = _scrubber()
    r = s.scrub("Email me at paul@example.com or pj@other.org.")
    assert "<EMAIL_1>" in r.text and "<EMAIL_2>" in r.text
    assert "paul@example.com" not in r.text


def test_scrubs_phone_numbers():
    s = _scrubber()
    r = s.scrub("Call 214-555-0101 or (972) 555 0199 anytime.")
    assert "<PHONE_1>" in r.text and "<PHONE_2>" in r.text
    assert "214-555-0101" not in r.text


def test_unscrub_round_trips_all_tokens():
    s = _scrubber()
    original = (
        "Paul Raspey at 123 Example Dr, Dallas TX, reach him at "
        "paul@example.com or 214-555-0101. Also Henry and Aria."
    )
    scrubbed = s.scrub(original)
    # Mapping should have entries; none of the originals should be visible.
    assert scrubbed.mapping
    assert "<USER>" in scrubbed.text
    restored = s.unscrub(scrubbed.text, scrubbed.mapping)
    assert restored == original


def test_unscrub_prefers_longer_tokens_first():
    # Edge case: if both <CHILD_1> and <CHILD_10> existed, sorted by length
    # desc ensures the longer one substitutes first.
    scrubbed = ScrubResult(
        text="<CHILD_1> then <CHILD_10> end",
        mapping={"<CHILD_1>": "Henry", "<CHILD_10>": "Zed"},
    )
    out = scrubbed.unscrub(scrubbed.text)
    assert out == "Henry then Zed end"


def test_from_config_loads_fields():
    cfg = PIIScrubberConfig(
        enabled=True,
        user_name="Jane Doe",
        user_aliases=["JD"],
        child_names=[],
        addresses=[],
        scrub_emails=False,
        scrub_phones=False,
        sensitive_tokens=["SecretCo"],
    )
    s = PIIScrubber.from_config(cfg)
    r = s.scrub("Jane Doe met JD at SecretCo, not via me@x.com.")
    assert r.mapping.get("<USER>") == "Jane Doe"
    assert "<SENSITIVE_1>" in r.text
    # Email not scrubbed because config said no.
    assert "me@x.com" in r.text
