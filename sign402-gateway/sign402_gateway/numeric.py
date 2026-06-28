from decimal import Decimal


def format_decimal(value: Decimal) -> str:
    """Render a Decimal in plain (non-exponent) form without a trailing ``.0``."""
    text = format(value.normalize(), "f")
    return text[:-2] if text.endswith(".0") else text
