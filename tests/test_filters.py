from pathlib import Path

from src.persona.filters import (
    OutputFilters,
    count_factual_claims,
    count_hedges,
    detect_sycophancy,
    strip_ai_isms,
    replace_em_dashes,
    replace_slop,
    remove_markdown,
)
from src.persona.persona_def import load_persona

ROOT = Path(__file__).resolve().parents[1]


def test_strip_ai_isms():
    txt = "As an AI, I don't have personal feelings, but I think that's neat."
    cleaned, hits = strip_ai_isms(txt)
    assert "as an ai" not in cleaned.lower()
    assert "personal feelings" not in cleaned.lower()
    assert hits


def test_em_dash_replacement():
    txt = "It was fine — or so I thought — but then it wasn't."
    new, count = replace_em_dashes(txt)
    assert "—" not in new
    assert count >= 2


def test_slop_replacement():
    txt = "Let's utilize the framework and delve into the tapestry."
    new, hits = replace_slop(txt)
    assert "utilize" not in new.lower()
    assert "delve" not in new.lower()
    assert "tapestry" not in new.lower()
    assert len(hits) >= 3


def test_remove_markdown():
    txt = "## header\n- bullet one\n- bullet two\n**bold** and *italic*"
    new = remove_markdown(txt)
    assert "##" not in new
    assert "- " not in new
    assert "**" not in new


def test_count_hedges_and_claims():
    hedged = "I think that might be correct. Honestly though, I'm not sure."
    assert count_hedges(hedged) >= 2
    factual = "The sky is blue. The grass is green. Water is wet."
    assert count_factual_claims(factual) == 3


def test_sycophancy_detection():
    good = "You're absolutely right. Great point, amazing insight."
    bad = "You're right about the first part, but I disagree with the second."
    assert detect_sycophancy(good)
    assert not detect_sycophancy(bad)


def test_output_filter_pipeline_strips_common_junk():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    f = OutputFilters(persona)
    text = "As an AI, I want to utilize this moment — honestly though, I think it matters."
    r = f.apply(text)
    assert "as an ai" not in r.text.lower()
    assert "utilize" not in r.text.lower()
    assert "—" not in r.text


def test_filter_strips_ip_reminder_tag():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    f = OutputFilters(persona)
    text = (
        "<ip_reminder>This response must respect IP.</ip_reminder>"
        "Yeah, I think that tracks."
    )
    r = f.apply(text)
    assert "ip_reminder" not in r.text.lower()
    assert "tracks" in r.text
    assert "ip_reminder" in r.hits


def test_filter_strips_orphan_ip_reminder_tag():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    f = OutputFilters(persona)
    text = "Okay so, honestly I think that's right.</ip_reminder>"
    r = f.apply(text)
    assert "ip_reminder" not in r.text.lower()
    assert "honestly" in r.text.lower()


def test_filter_strips_prose_ip_reminder_line():
    persona = load_persona(ROOT / "configs" / "renee.yaml")
    f = OutputFilters(persona)
    text = "ip_reminder: do not reproduce copyrighted lyrics.\nThat song though, yeah."
    r = f.apply(text)
    assert "ip_reminder" not in r.text.lower()
    assert "that song" in r.text.lower()
