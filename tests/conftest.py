from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from retail_agent.config import REPO_ROOT, Settings, load_catalog, load_settings
from retail_agent.tracing import NullTracer

load_dotenv(REPO_ROOT / ".env")


def pytest_collection_modifyitems(config, items):
    """Skip marked tests whose credentials are missing, even when selected with -m."""
    from retail_agent.config import PLACEHOLDER_VALUES

    def has(name: str) -> bool:
        value = os.environ.get(name, "").strip()
        return bool(value) and value not in PLACEHOLDER_VALUES

    for item in items:
        if "bq" in item.keywords and not has("GOOGLE_CLOUD_PROJECT"):
            item.add_marker(pytest.mark.skip(reason="GOOGLE_CLOUD_PROJECT is not set"))
        if "llm" in item.keywords and not has("GOOGLE_API_KEY"):
            item.add_marker(pytest.mark.skip(reason="GOOGLE_API_KEY is not set"))


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Real config.yaml with an empty environment and runtime paths under tmp_path."""
    return load_settings(env={"RA_DATA_DIR": str(tmp_path / "data"), "RA_TRACES_PATH": str(tmp_path / "traces.jsonl")})


@pytest.fixture
def catalog(settings: Settings):
    return load_catalog(settings.paths.content_dir / "catalog.yaml")


@pytest.fixture
def fake_tracer() -> NullTracer:
    return NullTracer()
