"""Executive identity and brand scope (contracts §2.1). Implemented in Stage 2."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Scope:
    all_brands: bool
    brands: tuple[str, ...]          # sorted; empty iff all_brands

    @classmethod
    def everything(cls) -> "Scope":
        raise NotImplementedError

    @classmethod
    def of(cls, brands: Iterable[str]) -> "Scope":
        raise NotImplementedError

    def allows(self, brand: str) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class Executive:
    exec_id: str
    display_name: str
    scope: Scope


class UnknownExecutiveError(Exception): ...


class IdentityStore:
    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def upsert_executive(self, exec_id: str, display_name: str, *, scope_all: bool,
                         brands: Sequence[str] = ()) -> None:
        raise NotImplementedError

    def get_executive(self, exec_id: str) -> Executive:
        raise NotImplementedError

    def get_scope(self, exec_id: str) -> Scope:
        raise NotImplementedError

    def list_executives(self) -> list[Executive]:
        raise NotImplementedError
