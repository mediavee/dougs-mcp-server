"""Euro amounts quantized to the cent, half up, from their decimal text.

A float is read back through its shortest decimal (`str(1.005)` is ``1.005``), which is the
value the caller typed. The result stays a JSON number: the API already accepts one, and a
cent integer or a string would be a different contract.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_HUNDRED = Decimal("100")
_ONE = Decimal("1")


def cents(amount: int | float | str) -> int:
    """Integer cents, half up."""
    if isinstance(amount, bool) or not isinstance(amount, int | float | str):
        raise ValueError(f"amount must be numeric, got {amount!r}")
    try:
        dec = Decimal(amount) if isinstance(amount, int | str) else Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError(f"amount must be numeric, got {amount!r}") from exc
    return int((dec * _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP))


def euros(amount: int | float | str) -> int | float:
    """The same amount rounded half up to the cent. Whole integer euros stay integers."""
    value = cents(amount)
    if isinstance(amount, int) and value % 100 == 0:
        return value // 100
    return value / 100


def format_cents(value: int) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"
