"""Executive identity and brand scope (contracts §2.1, DD §7).

`IdentityStore.get_scope()` is the single place the rest of the code learns about permissions.
The SQLite tables stand in for an entitlements service; user text can never change them.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executives (
    exec_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    scope_all INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS executive_brands (
    exec_id TEXT NOT NULL REFERENCES executives(exec_id),
    brand TEXT NOT NULL,
    PRIMARY KEY (exec_id, brand)
);
"""


@dataclass(frozen=True)
class Scope:
    all_brands: bool
    brands: tuple[str, ...]          # sorted; empty iff all_brands

    @classmethod
    def everything(cls) -> "Scope":
        return cls(all_brands=True, brands=())

    @classmethod
    def of(cls, brands: Iterable[str]) -> "Scope":
        unique = sorted({b for b in brands if b})
        if not unique:
            raise ValueError("A brand scope needs at least one brand")
        return cls(all_brands=False, brands=tuple(unique))

    def allows(self, brand: str) -> bool:
        if self.all_brands:
            return True
        wanted = brand.casefold()
        return any(b.casefold() == wanted for b in self.brands)


@dataclass(frozen=True)
class Executive:
    exec_id: str
    display_name: str
    scope: Scope


class UnknownExecutiveError(Exception): ...


class IdentityStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def upsert_executive(self, exec_id: str, display_name: str, *, scope_all: bool,
                         brands: Sequence[str] = ()) -> None:
        """One transaction; replaces the brand rows. scope_all=True requires brands=()."""
        if scope_all and brands:
            raise ValueError("scope_all=True requires brands=()")
        if not scope_all and not [b for b in brands if b]:
            raise ValueError(f"Executive '{exec_id}' needs scope_all=True or at least one brand")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO executives (exec_id, display_name, scope_all) VALUES (?, ?, ?) "
                "ON CONFLICT(exec_id) DO UPDATE SET display_name = excluded.display_name, "
                "scope_all = excluded.scope_all",
                (exec_id, display_name, int(scope_all)),
            )
            conn.execute("DELETE FROM executive_brands WHERE exec_id = ?", (exec_id,))
            conn.executemany(
                "INSERT INTO executive_brands (exec_id, brand) VALUES (?, ?)",
                [(exec_id, b) for b in sorted({b for b in brands if b})],
            )

    def get_executive(self, exec_id: str) -> Executive:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT exec_id, display_name, scope_all FROM executives WHERE exec_id = ?", (exec_id,)
            ).fetchone()
            if row is None:
                raise UnknownExecutiveError(f"Unknown executive '{exec_id}'")
            brands = [r[0] for r in conn.execute(
                "SELECT brand FROM executive_brands WHERE exec_id = ? ORDER BY brand", (exec_id,))]
        return Executive(exec_id=row[0], display_name=row[1], scope=self._scope(bool(row[2]), brands, exec_id))

    def get_scope(self, exec_id: str) -> Scope:
        """The single source of permissions."""
        return self.get_executive(exec_id).scope

    def list_executives(self) -> list[Executive]:
        with closing(self._connect()) as conn:
            ids = [r[0] for r in conn.execute("SELECT exec_id FROM executives ORDER BY exec_id")]
        return [self.get_executive(i) for i in ids]

    @staticmethod
    def _scope(scope_all: bool, brands: list[str], exec_id: str) -> Scope:
        if scope_all:
            return Scope.everything()
        if not brands:
            # Fail closed: an executive without brands must not silently become "all brands".
            raise UnknownExecutiveError(f"Executive '{exec_id}' has no brands assigned")
        return Scope.of(brands)
