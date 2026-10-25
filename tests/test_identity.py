"""Identity store, scopes and the seed rule (DD §7, docs/facts.md §5)."""
from __future__ import annotations

import importlib.util
import sqlite3

import pytest

from retail_agent.config import REPO_ROOT
from retail_agent.security.identity import IdentityStore, Scope, UnknownExecutiveError


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_executives", REPO_ROOT / "scripts" / "seed_executives.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scope_values():
    assert Scope.everything() == Scope(all_brands=True, brands=())
    s = Scope.of(["Levi's", "Calvin Klein", "Levi's"])
    assert s.brands == ("Calvin Klein", "Levi's") and not s.all_brands
    assert s.allows("calvin klein") and s.allows("LEVI'S") and not s.allows("Nike")
    assert not s.allows("Calvin")                    # exact match, not substring
    assert Scope.everything().allows("anything")
    with pytest.raises(ValueError):
        Scope.of([])


def test_store_roundtrip_and_replace(tmp_data_dir):
    store = IdentityStore(tmp_data_dir / "app.sqlite")
    store.upsert_executive("ceo", "Morgan Lee (CEO)", scope_all=True)
    store.upsert_executive("bm", "Sam Rivera", scope_all=False, brands=["B", "A"])
    assert store.get_scope("ceo") == Scope.everything()
    assert store.get_scope("bm") == Scope.of(["A", "B"])

    store.upsert_executive("bm", "Sam R.", scope_all=False, brands=["C"])   # replaces brand rows
    bm = store.get_executive("bm")
    assert bm.display_name == "Sam R." and bm.scope.brands == ("C",)
    assert [e.exec_id for e in store.list_executives()] == ["bm", "ceo"]

    # A second store on the same file sees the same data (tables are created only if missing).
    assert IdentityStore(tmp_data_dir / "app.sqlite").get_scope("bm") == Scope.of(["C"])


def test_store_rejects_inconsistent_scope(tmp_data_dir):
    store = IdentityStore(tmp_data_dir / "app.sqlite")
    with pytest.raises(ValueError):
        store.upsert_executive("x", "X", scope_all=True, brands=["A"])
    with pytest.raises(ValueError):
        store.upsert_executive("x", "X", scope_all=False, brands=[])
    with pytest.raises(UnknownExecutiveError):
        store.get_scope("nobody")


def test_executive_without_brands_fails_closed(tmp_data_dir):
    db = tmp_data_dir / "app.sqlite"
    IdentityStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO executives VALUES ('broken', 'B', 0)")
    with pytest.raises(UnknownExecutiveError):
        IdentityStore(db).get_scope("broken")


def test_seed_split_and_idempotent(tmp_data_dir):
    seed = _load_seed_module()
    ranked = ["Calvin Klein", "Diesel", "7 For All Mankind", "Carhartt", "True Religion", "Tommy Hilfiger"]
    b1, b2 = seed.split_brands(ranked)
    assert b1 == ["Calvin Klein", "7 For All Mankind", "True Religion"]
    assert b2 == ["Diesel", "Carhartt", "Tommy Hilfiger"]
    assert not set(b1) & set(b2)
    with pytest.raises(ValueError):
        seed.split_brands(ranked[:5])

    store = IdentityStore(tmp_data_dir / "app.sqlite")
    seed.seed(store, b1, b2)
    first = store.list_executives()
    seed.seed(store, b1, b2)
    assert store.list_executives() == first
    assert [e.exec_id for e in first] == ["brand_manager_1", "brand_manager_2", "ceo"]
    assert store.get_scope("ceo").all_brands
    assert store.get_scope("brand_manager_2") == Scope.of(b2)


def test_seed_query_ranks_counted_revenue():
    sql = _load_seed_module().top_brands_sql("ds.x")
    for fragment in ("NOT IN ('Cancelled', 'Returned')", "created_at <= CURRENT_TIMESTAMP()",
                     "brand IS NOT NULL", "ORDER BY revenue DESC, p.brand ASC", "LIMIT 6"):
        assert fragment in sql
