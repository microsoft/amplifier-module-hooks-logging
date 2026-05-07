"""Regression tests for _sanitize_for_json Decimal handling.

Root cause: _sanitize_for_json called value.model_dump() on Pydantic models,
which returns native Python Decimal objects. Those survive into the sanitized
record and crash json.dumps() at JSONL write time.

Fix: Use model_dump(mode='json') so Pydantic converts Decimal->str before
returning. Plain model_dump() preserves Python types that are not
JSON-serializable.

None != Decimal('0'): unknown cost is semantically distinct from confirmed-free.
The fix must not conflate them.
"""

import json
from decimal import Decimal

from pydantic import BaseModel

from amplifier_module_hooks_logging import _sanitize_for_json


# Minimal Pydantic model mirroring Usage.cost_usd from amplifier-core.
# Defined locally — no runtime dep on amplifier-core needed for this test.
class _Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal | None = None


class TestSanitizeForJsonDecimal:
    """_sanitize_for_json must produce json.dumps-safe output for Pydantic
    models that carry Decimal fields (e.g. Usage.cost_usd from M2 stamping)."""

    def test_pydantic_model_with_decimal_field_is_json_safe(self):
        """Core regression: Pydantic model with Decimal must not crash json.dumps."""
        usage = _Usage(input_tokens=1000, output_tokens=200, cost_usd=Decimal("0.047"))
        result = _sanitize_for_json({"usage": usage, "provider": "anthropic"})
        # Must not raise TypeError: Object of type Decimal is not JSON serializable
        json.dumps(result)

    def test_decimal_field_serialized_as_string(self):
        """cost_usd Decimal must become str, preserving its exact representation."""
        usage = _Usage(input_tokens=1000, output_tokens=200, cost_usd=Decimal("0.047"))
        result = _sanitize_for_json({"usage": usage})
        assert isinstance(result["usage"]["cost_usd"], str)
        assert result["usage"]["cost_usd"] == "0.047"

    def test_decimal_zero_is_string_not_none(self):
        """Decimal('0') must serialize as '0', never as None.

        Semantic contract:
          None  = rate data unavailable (unknown cost, e.g. self-hosted models)
          '0'   = confirmed free (zero billable tokens)
        These are distinct. Conflating them corrupts cost accounting silently.
        """
        usage = _Usage(cost_usd=Decimal("0"))
        result = _sanitize_for_json({"usage": usage})
        assert result["usage"]["cost_usd"] is not None, (
            "Decimal('0') must not become None — unknown cost != confirmed free"
        )
        assert result["usage"]["cost_usd"] == "0"

    def test_none_cost_usd_stays_none(self):
        """None cost_usd (unknown/not applicable) must remain None after sanitization."""
        usage = _Usage(cost_usd=None)
        result = _sanitize_for_json({"usage": usage})
        assert result["usage"]["cost_usd"] is None

    def test_raw_dict_with_decimal_also_safe(self):
        """Plain dicts containing Decimal values must also be json.dumps-safe.

        Covers the case where a provider emits a raw dict (not a Pydantic model)
        containing a Decimal before str() conversion was applied.
        """
        data = {"cost_usd": Decimal("0.047"), "tokens": 1000}
        result = _sanitize_for_json(data)
        json.dumps(result)
        assert isinstance(result["cost_usd"], str)
        assert result["cost_usd"] == "0.047"
