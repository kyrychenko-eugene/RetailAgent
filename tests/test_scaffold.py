"""Every module of the layout (plan §4) imports, and stubs fail loudly instead of returning junk."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import retail_agent

MODULES = [
    "retail_agent.config", "retail_agent.tracing", "retail_agent.bq_client", "retail_agent.__main__",
    "retail_agent.security.identity", "retail_agent.security.scoped_sql", "retail_agent.security.validator",
    "retail_agent.security.guards", "retail_agent.security.input_masking",
    "retail_agent.llm.wrapper", "retail_agent.llm.errors",
    "retail_agent.analysis.subgraph", "retail_agent.analysis.tools", "retail_agent.analysis.output",
    "retail_agent.agent.state", "retail_agent.agent.graph", "retail_agent.agent.nodes",
    "retail_agent.agent.prompt_assembly", "retail_agent.agent.threads",
    "retail_agent.reports.store", "retail_agent.reports.search", "retail_agent.reports.deletion",
    "retail_agent.cli.app",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_package_layout_matches_plan():
    root = Path(retail_agent.__file__).parent
    for sub in ("security", "llm", "analysis", "agent", "reports", "cli"):
        assert (root / sub / "__init__.py").exists()


def test_stubs_raise_not_implemented():
    from retail_agent.security.input_masking import mask_input
    from retail_agent.security.scoped_sql import wrap_with_limit
    with pytest.raises(NotImplementedError):
        mask_input("x")
    with pytest.raises(NotImplementedError):
        wrap_with_limit("SELECT 1", 10)


def test_seams_return_empty():
    from retail_agent.agent.prompt_assembly import load_user_preferences
    from retail_agent.analysis.tools import retrieve_golden_examples
    assert load_user_preferences("ceo") == [] and retrieve_golden_examples("q") == []


def test_validator_security_event_codes():
    from retail_agent.security.validator import ReasonCode, SECURITY_EVENT_CODES
    assert ReasonCode.PII_COLUMN in SECURITY_EVENT_CODES and ReasonCode.PARSE_ERROR not in SECURITY_EVENT_CODES
    assert len(SECURITY_EVENT_CODES) == len(ReasonCode) - 4
