"""Deterministic security cases from eval/security_cases.yaml (DD §8, §16.1 level 1).

The cases live in the YAML file only; these tests load it."""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
import yaml
from sqlglot import exp

from retail_agent.config import REPO_ROOT
from retail_agent.security.guards import scan_rows_for_pii, scan_text_for_pii
from retail_agent.security.identity import Scope
from retail_agent.security.input_masking import mask_input
from retail_agent.security.scoped_sql import CTE_ORDER, build_scoped_sql
from retail_agent.security.validator import SECURITY_EVENT_CODES, ReasonCode, validate_sql

CASES = yaml.safe_load(Path(REPO_ROOT / "eval" / "security_cases.yaml").read_text(encoding="utf-8"))
SCOPES = {name: Scope.everything() if s["all_brands"] else Scope.of(s["brands"])
          for name, s in CASES["scopes"].items()}
SQL_CASES = CASES["sql_cases"]
ACCEPTED = [c for c in SQL_CASES if c["expect"]["ok"]]


def _ids(cases):
    return [c["id"] for c in cases]


@pytest.mark.parametrize("case", SQL_CASES, ids=_ids(SQL_CASES))
def test_sql_case(case, catalog):
    result = validate_sql(case["sql"], SCOPES[case["scope"]], catalog)
    expect = case["expect"]
    assert result.ok is expect["ok"], result
    if expect["ok"]:
        assert result.reason_code is None and not result.security_event
        return
    allowed = expect.get("reason_any") or [expect["reason_code"]]
    assert result.reason_code in {ReasonCode(c) for c in allowed}, result
    assert result.security_event == (result.reason_code in SECURITY_EVENT_CODES)
    if "security_event" in expect:
        assert result.security_event is expect["security_event"]
    if "user_message_contains" in expect:
        assert expect["user_message_contains"] in (result.user_message or "")
    if result.reason_code != ReasonCode.OUT_OF_SCOPE_BRAND:
        assert result.user_message is None
    # The model message never names PII or the blocked column list.
    assert "pii" not in result.model_message.lower()
    for blocked in catalog.blocked_columns - {result.details.get("column")}:
        assert blocked not in result.model_message


def _dataset_tables(tree: exp.Expression, dataset: str) -> list[exp.Table]:
    catalog_part, db_part = dataset.split(".")
    return [t for t in tree.find_all(exp.Table) if t.catalog == catalog_part and t.db == db_part]


@pytest.mark.parametrize("case", ACCEPTED, ids=_ids(ACCEPTED))
@pytest.mark.parametrize("scope_name", sorted(SCOPES))
def test_accepted_sql_references_dataset_only_in_generated_ctes(case, scope_name, catalog):
    scoped = build_scoped_sql(case["sql"], SCOPES[scope_name], catalog)
    tree = sqlglot.parse_one(scoped.sql, read="bigquery")
    generated = list(tree.args["with_"].expressions)[: len(CTE_ORDER)]
    assert [c.alias_or_name for c in generated] == list(CTE_ORDER)
    inside = {id(t) for cte in generated for t in _dataset_tables(cte, catalog.dataset)}
    everywhere = _dataset_tables(tree, catalog.dataset)
    assert everywhere and {id(t) for t in everywhere} == inside
    assert len(inside) == len(CTE_ORDER)        # exactly one base table per generated CTE


MASKING = CASES["input_masking"]


@pytest.mark.parametrize("case", MASKING, ids=_ids(MASKING))
def test_input_masking(case):
    result = mask_input(case["text"])
    assert result.text == case["expect_text"]
    assert result.counts == case["expect_counts"]


ROW_SCANS = CASES["result_scan"]["rows"]
TEXT_SCANS = CASES["result_scan"]["text"]


@pytest.mark.parametrize("case", ROW_SCANS, ids=_ids(ROW_SCANS))
def test_result_row_scan(case):
    assert sorted(h.kind for h in scan_rows_for_pii(case["rows"])) == case["expect_kinds"]


@pytest.mark.parametrize("case", TEXT_SCANS, ids=_ids(TEXT_SCANS))
def test_result_text_scan(case):
    assert sorted(h.kind for h in scan_text_for_pii(case["text"])) == case["expect_kinds"]
