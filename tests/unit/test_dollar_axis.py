"""Cost in dollars, from the registry through to a Codex node's usage record."""

from __future__ import annotations

from orchestra.control.backend_usage import derive_cost_usd, load_pricing_registry


def test_the_model_we_actually_run_has_a_price() -> None:
    """Every Codex node priced as unavailable while this entry was missing."""
    registry = load_pricing_registry()
    price = registry.models["gpt-5.4"]

    assert price.input_per_million_usd == 2.50
    assert price.output_per_million_usd == 15.00
    assert price.cached_input_per_million_usd == 0.25


def test_cached_input_is_a_discount_not_a_surcharge() -> None:
    """Cached tokens are a subset of the input, billed at a tenth of the rate.

    A Codex session re-sends its transcript each turn, so this is most of the
    input. Billing it at the full rate -- or adding it on top -- would report a
    cost several times the real one and put the cost axis of a Pareto frontier
    in the wrong order.
    """
    cost, quality = derive_cost_usd(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=900_000,
        model_name="gpt-5.4",
        provider_cost_usd=None,
    )

    # 100k uncached at $2.50/M + 900k cached at $0.25/M
    assert cost == 0.25 + 0.225
    assert quality == "derived"

    uncached, _ = derive_cost_usd(
        prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=0,
        model_name="gpt-5.4", provider_cost_usd=None,
    )
    assert uncached == 2.50
    assert cost < uncached


def test_a_real_run_shaped_workload_prices_out() -> None:
    """The pyjwt single arm's measured usage, priced end to end."""
    cost, quality = derive_cost_usd(
        prompt_tokens=4_071_665,
        completion_tokens=42_076,
        cached_tokens=0,
        model_name="gpt-5.4",
        provider_cost_usd=None,
    )

    assert quality == "derived"
    # No cache reported means this is the ceiling, not the invoice.
    assert 10.0 < cost < 11.0


def test_an_unpriced_model_reports_unavailable_rather_than_zero() -> None:
    """A missing price must not read as a free configuration."""
    cost, quality = derive_cost_usd(
        prompt_tokens=1000, completion_tokens=100, cached_tokens=0,
        model_name="gpt-5-codex", provider_cost_usd=None,
    )

    assert cost is None
    assert quality == "unavailable"


def test_codex_backend_stamps_the_model_and_cached_tokens() -> None:
    """The snapshot the cost derivation reads must carry both.

    Neither was recorded before: the backend reported raw input tokens with no
    model name, so the registry lookup could not even be attempted.
    """
    from orchestra.llm.usage import LLMUsage
    from orchestra.runtime.committer import _snapshot_from_result
    from orchestra.runtime.state import NodeExecutionResult

    snapshot = _snapshot_from_result(
        NodeExecutionResult(
            node_id="agent_1",
            backend_id="codex_sdk",
            succeeded=True,
            usage=LLMUsage(prompt_tokens=1000, completion_tokens=100, cached_tokens=800),
            latency_ms=10,
            backend_metadata={"model_name": "gpt-5.4", "backend_kind": "codex_sdk"},
        )
    )

    assert snapshot.model_name == "gpt-5.4"
    assert snapshot.cached_tokens == 800

    cost, quality = derive_cost_usd(
        prompt_tokens=snapshot.prompt_tokens,
        completion_tokens=snapshot.completion_tokens,
        cached_tokens=snapshot.cached_tokens,
        model_name=snapshot.model_name,
        provider_cost_usd=snapshot.provider_cost_usd,
    )
    assert cost is not None and quality == "derived"
