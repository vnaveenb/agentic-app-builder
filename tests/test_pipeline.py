"""Integration tests — require live API keys and running services."""

import pytest


@pytest.mark.integration
def test_full_pipeline_python() -> None:
    """End-to-end pipeline test with Python runtime. Requires GOOGLE_API_KEY."""
    # This test requires a live LLM connection
    # Run manually: pytest tests/test_pipeline.py -m integration -v
    pytest.skip("Integration test — run manually with API keys")


@pytest.mark.integration
def test_full_pipeline_static() -> None:
    """End-to-end pipeline test with static runtime."""
    pytest.skip("Integration test — run manually with API keys")
