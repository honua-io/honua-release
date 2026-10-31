"""Latest-model arms stay beside the shipped Sonnet 4.6 primary."""

from decimal import Decimal
from pathlib import Path

from constants import ARMS, PRICES_USD_PER_MTOK, PRIMARY_ARM, SHIPPED_TEMPERATURE


def test_primary_arm_is_unchanged_shipped_sonnet_4_6():
    assert PRIMARY_ARM["id"] == "primary"
    assert PRIMARY_ARM["model"] == "claude-sonnet-4-6"
    assert PRIMARY_ARM["bedrock"] == "us.anthropic.claude-sonnet-4-6"
    assert PRIMARY_ARM["temperature"] == SHIPPED_TEMPERATURE
    assert PRIMARY_ARM["max_actions"] == 40
    assert ARMS["primary"] is PRIMARY_ARM
    assert PRICES_USD_PER_MTOK["primary"] == {"input": "3", "output": "15"}


def test_latest_models_are_priced_exploratory_arms():
    expected = {
        "opus-5-5": ("claude-opus-5-5", "us.anthropic.claude-opus-5-5", 4.40, 22.00, 0.22),
        "sonnet-5-5": ("claude-sonnet-5-5", "us.anthropic.claude-sonnet-5-5", 2.20, 11.00, 0.11),
        "gpt-6.1-sol": ("gpt-6.1-sol", "us.openai.gpt-6.1-sol", 2.20, 11.00, 0.11),
        "gpt-6.1-terra": ("gpt-6.1-terra", "us.openai.gpt-6.1-terra", 1.10, 5.50, 0.055),
    }
    assert set(expected) <= set(ARMS)
    for arm_id, (model, bedrock, inp, out, cached) in expected.items():
        arm = ARMS[arm_id]
        assert arm["id"] == arm_id
        assert arm["model"] == model
        assert arm["bedrock"] == bedrock
        assert arm["temperature"] == SHIPPED_TEMPERATURE
        assert arm["max_actions"] == 40
        price = arm["price"]
        assert price["input_per_mtok"] == inp
        assert price["output_per_mtok"] == out
        assert price["cached_input_per_mtok"] == cached
        assert price["as_of"] == "2026-10"
        ledger = PRICES_USD_PER_MTOK[arm_id]
        assert Decimal(ledger["input"]) == Decimal(str(inp))
        assert Decimal(ledger["output"]) == Decimal(str(out))


def _arm_input_block(text: str) -> str:
    start = text.index("      arm:\n")
    lines = [text[start:].splitlines()[0]]
    for line in text[start:].splitlines()[1:]:
        if line.startswith("      ") and not line.startswith("        "):
            break
        lines.append(line)
    return "\n".join(lines) + "\n"


def test_dispatch_arm_choices_match_the_registry():
    text = (
        Path(__file__).resolve().parents[3] / ".github/workflows/agent-ops.yml"
    ).read_text()
    block = _arm_input_block(text)
    options = [
        line.strip()[2:]
        for line in block.splitlines()
        if line.strip().startswith("- ")
    ]
    assert options == list(ARMS)
    assert "default: primary" in block
    assert "exploratory" not in block
    assert "scope:" in text


def test_older_models_are_not_arms():
    listed = " ".join(
        f"{arm['id']} {arm['model']} {arm['bedrock']}" for arm in ARMS.values()
    )
    for retired in ("haiku", "sonnet-4-5", "gpt-5", "claude-3", "gpt-4"):
        assert retired not in listed
