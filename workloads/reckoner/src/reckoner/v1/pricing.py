"""The one source of pinned published v1 prices (USD per million tokens).

Protocol worst cases, per-attempt reservations, settled costs and owner
reconciliation all read these values. A different price is a new dated entry and a
new protocol, never a silent change. Usage-priced estimates, not invoices.
"""

from decimal import Decimal

PUBLISHED = {
    "typesafe": {
        "model": "jev-1.13.0",
        "input_per_million": Decimal("0.042"),
        "output_per_million": Decimal(0),
        "effective": "2026-09-30",
        "source_url": "https://docs.typesafe.ai/models",
    },
    "anthropic": {
        "model": "anthropic/claude-haiku-4-5-20251001",
        "input_per_million": Decimal(1),
        "output_per_million": Decimal(5),
        "effective": "2026-09-25",
        "source_url": "https://platform.claude.com/docs/en/about-claude/pricing",
    },
}
MILLION = Decimal(1000000)


def rates(provider: str) -> tuple[Decimal, Decimal]:
    entry = PUBLISHED[provider]
    return entry["input_per_million"], entry["output_per_million"]


def cost(provider: str, usage: dict) -> Decimal:
    input_price, output_price = rates(provider)
    return (
        Decimal(usage["input_tokens"]) * input_price
        + Decimal(usage["output_tokens"]) * output_price
    ) / MILLION


def matches(provider: str, table: dict) -> bool:
    """True when a price-table document carries exactly the pinned published rates."""
    input_price, output_price = rates(provider)
    try:
        return (
            table["model"] == PUBLISHED[provider]["model"]
            and table["currency"] == "USD"
            and Decimal(table["input_per_million"]) == input_price
            and Decimal(table["output_per_million"]) == output_price
        )
    except (KeyError, TypeError, ArithmeticError):
        return False
