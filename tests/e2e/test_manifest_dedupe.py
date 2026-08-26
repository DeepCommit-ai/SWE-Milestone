"""Duplicate TOML root-key resolution after the three-way manifest merge.

The agent and the evaluator ENV-PATCH can each add the same dependency key in
different hunks; git merge-file keeps both lines (textually clean) and cargo
then rejects the manifest. Resolution follows the evaluator-wins conflict
policy and fails closed when the surviving line cannot be attributed to the
prepared manifest.
"""

from harness.e2e.evaluator import _resolve_toml_duplicate_root_keys


PREPARED = """[workspace.dependencies]
byteyarn = "0.5"
serde = { version = "1" }
"""


def test_evaluator_prepared_line_wins_over_agent_duplicate():
    merged = """[workspace.dependencies]
byteyarn = "0.5"
serde = { version = "1" }
byteyarn = "0.5.1"
"""
    new_text, resolutions, errors = _resolve_toml_duplicate_root_keys(merged, PREPARED)
    assert errors == []
    assert resolutions == ["[workspace.dependencies] byteyarn (evaluator-prepared line kept)"]
    assert new_text.count("byteyarn") == 1
    assert 'byteyarn = "0.5"' in new_text
    assert 'byteyarn = "0.5.1"' not in new_text


def test_identical_duplicates_collapse_without_prepared_attribution():
    merged = """[dependencies]
memchr = "2"
memchr = "2"
"""
    new_text, resolutions, errors = _resolve_toml_duplicate_root_keys(merged, PREPARED)
    assert errors == []
    assert resolutions == ["[dependencies] memchr (identical duplicates)"]
    assert new_text.count("memchr") == 1


def test_agent_internal_duplicate_fails_closed():
    merged = """[dependencies]
foo = "1"
foo = "2"
"""
    new_text, resolutions, errors = _resolve_toml_duplicate_root_keys(merged, PREPARED)
    assert resolutions == []
    assert len(errors) == 1 and "foo" in errors[0]
    assert new_text == merged  # untouched on error


def test_same_key_in_different_tables_is_not_a_duplicate():
    merged = """[dependencies]
byteyarn = "0.5"

[workspace.dependencies]
byteyarn = "0.5"
"""
    new_text, resolutions, errors = _resolve_toml_duplicate_root_keys(merged, PREPARED)
    assert resolutions == [] and errors == []
    assert new_text == merged


def test_dotted_workspace_key_conflicts_with_plain_key():
    merged = """[workspace.dependencies]
byteyarn = "0.5"
byteyarn.workspace = true
"""
    # dotted form counts as the same root key; neither line pair is identical,
    # plain form matches prepared -> it survives.
    new_text, resolutions, errors = _resolve_toml_duplicate_root_keys(merged, PREPARED)
    assert errors == []
    assert 'byteyarn = "0.5"' in new_text
    assert "byteyarn.workspace" not in new_text
