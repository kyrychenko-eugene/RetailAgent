from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from retail_agent.config import (ConfigError, DEFAULT_CONFIG_PATH, ModelSpec, load_catalog, load_settings,
                                 require_api_key)


def test_both_profiles_load():
    std = load_settings(env={"LLM_PROFILE": "standard"})
    free = load_settings(env={"LLM_PROFILE": "free"})
    assert std.profile == "standard" and free.profile == "free"
    assert std.roles["expensive"][0].model != free.roles["expensive"][0].model
    for s in (std, free):
        assert set(s.roles) == {"cheap", "expensive"}
        assert s.turn.deadline_for("analysis") == 60 and s.turn.deadline_for("anything") == 30
        assert s.turn.budget_for("analysis") == 6 and s.turn.budget_for("report_library") == 0
        assert s.paths.app_db.is_absolute() and s.paths.content_dir.is_dir()


def test_default_profile_and_unknown_profile():
    assert load_settings(env={}).profile == "standard"
    with pytest.raises(ConfigError, match="nope"):
        load_settings(env={"LLM_PROFILE": "nope"})


def test_env_overrides(tmp_path: Path):
    s = load_settings(env={
        "RA_MODEL_CHEAP": "my-cheap", "RA_MODEL_EXPENSIVE": "my-exp", "RA_BYTES_CAP": "123",
        "RA_TRACES_PATH": str(tmp_path / "t.jsonl"), "RA_DATA_DIR": str(tmp_path / "d"),
        "GOOGLE_CLOUD_PROJECT": "proj-1",
    })
    base = load_settings(env={})
    assert s.roles["cheap"][0] == ModelSpec("google", "my-cheap")
    assert s.roles["cheap"][1:] == base.roles["cheap"][1:]
    assert s.roles["expensive"][0].model == "my-exp"
    assert s.bigquery.bytes_cap == 123 and s.bigquery.project == "proj-1"
    assert s.paths.traces == tmp_path / "t.jsonl"
    assert s.paths.app_db == tmp_path / "d" / "retail_agent.sqlite"
    assert s.paths.checkpoint_db == tmp_path / "d" / "checkpoints.sqlite"


def test_missing_project_is_not_an_error_at_load():
    assert load_settings(env={}).bigquery.project is None
    assert load_settings(env={"GOOGLE_CLOUD_PROJECT": "your-gcp-project-id"}).bigquery.project is None


def test_bad_bytes_cap():
    with pytest.raises(ConfigError, match="RA_BYTES_CAP"):
        load_settings(env={"RA_BYTES_CAP": "lots"})


def test_extra_fallback_only_with_key():
    without = load_settings(env={})
    with_key = load_settings(env={"ANTHROPIC_API_KEY": "sk-ant-0123456789"})
    assert all(m.provider == "google" for m in without.roles["cheap"])
    assert with_key.roles["cheap"][-1].provider == "anthropic"
    assert with_key.roles["expensive"][-1].provider == "anthropic"


def test_require_api_key():
    s = load_settings(env={})
    assert require_api_key(s, "google", {"GOOGLE_API_KEY": "abc123"}) == "abc123"
    for env in ({}, {"GOOGLE_API_KEY": ""}, {"GOOGLE_API_KEY": "your-gemini-api-key"}):
        with pytest.raises(ConfigError, match="GOOGLE_API_KEY is not set"):
            require_api_key(s, "google", env)
    with pytest.raises(ConfigError, match="Unknown provider"):
        require_api_key(s, "nobody", {})


def _config_with(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_validation_errors_name_key_and_file(tmp_path: Path):
    def drop_timeout(raw): del raw["llm"]["call_timeout_s"]
    p = _config_with(tmp_path, drop_timeout)
    with pytest.raises(ConfigError, match=r"llm\.call_timeout_s") as exc:
        load_settings(p, env={})
    assert str(p) in str(exc.value)

    def bad_type(raw): raw["turn"]["history_turns"] = "six"
    with pytest.raises(ConfigError, match=r"turn\.history_turns.*integer"):
        load_settings(_config_with(tmp_path, bad_type), env={})

    def no_default_deadline(raw): del raw["turn"]["deadline_s"]["default"]
    with pytest.raises(ConfigError, match="deadline_s.default"):
        load_settings(_config_with(tmp_path, no_default_deadline), env={})

    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.yaml", env={})


def test_relative_paths_resolve_against_config_dir(tmp_path: Path):
    p = _config_with(tmp_path, lambda raw: None)
    s = load_settings(p, env={})
    assert s.paths.traces == (tmp_path / "logs" / "traces.jsonl").resolve()


def test_catalog_loads_and_hides_blocked_columns(settings):
    cat = load_catalog(settings.paths.content_dir / "catalog.yaml")
    assert set(cat.tables) == {"products", "order_items", "orders", "users"}
    assert cat.column("USERS", "Traffic_Source").demographic
    assert cat.column("users", "email") is None
    assert "email" in cat.blocked_columns
    assert cat.allowed_columns()["orders"] == {"order_id", "user_id", "created_at"}
    compact_columns = {w for line in cat.compact_prompt().splitlines()[:-1] for w in line.replace(":", ",").split(", ")}
    for blocked in cat.blocked_columns:
        assert f"- {blocked} (" not in cat.schema_prompt()
        assert blocked not in compact_columns
    assert "retail_price (FLOAT64)" in cat.schema_prompt()
    assert cat.compact_prompt().splitlines()[0].startswith("products: id, category")


def test_catalog_errors(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("dataset: x\ntables: {}\n")
    with pytest.raises(ConfigError, match="tables"):
        load_catalog(p)
    p.write_text("dataset: x\ntables:\n  t: {source: t, columns: [{name: email, type: STRING}]}\nblocked_columns: [email]\n")
    with pytest.raises(ConfigError, match="blocked_columns"):
        load_catalog(p)
