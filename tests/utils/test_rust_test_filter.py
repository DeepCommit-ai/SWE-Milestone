from types import SimpleNamespace

import pytest

from harness.prepare_repo.split_test_patches import test_detector
from harness.prepare_repo.split_test_patches.test_detector import (
    RustTestDetectionError,
    _cfg_expr_has_test,
    find_test_code_ranges,
)
from harness.utils import rust_test_filter
from harness.utils.rust_test_filter import (
    RustTestFilterError,
    find_test_ranges_from_content,
    merge_src_with_gt_tests,
    remove_test_regions,
)


@pytest.mark.parametrize(
    ("expression", "test_only"),
    [
        ("test", True),
        ("all(unix, test)", True),
        ("all(not(debug_assertions), test)", True),
        ("any(all(test, unix), all(test, windows))", True),
        ("not(not(test))", True),
        ("any(windows, test)", False),
        ("all(any(windows, test), feature = \"x\")", False),
        ("not(test)", False),
        ("unix", False),
    ],
)
def test_cfg_is_test_only_only_when_expression_implies_test(expression, test_only):
    assert _cfg_expr_has_test(expression) is test_only


def test_cfg_any_test_does_not_remove_production_item(tmp_path):
    source = """#[cfg(any(windows, test))]
fn production_on_windows() {}
"""
    path = tmp_path / "lib.rs"
    path.write_text(source)

    assert find_test_code_ranges(str(path), strict=True) == []


def test_async_test_uses_exact_ast_boundary_and_keeps_next_item(tmp_path):
    source = """#[cfg(test)]
async fn test_only() {
    let closing_brace_in_a_string = "}";
}

fn production() { println!("keep"); }
"""
    path = tmp_path / "lib.rs"
    path.write_text(source)

    ranges = find_test_code_ranges(str(path), strict=True)
    assert ranges == [(1, 4, "#[cfg(test)] fn")]
    assert remove_test_regions(source, [(1, 4)]) == "\nfn production() { println!(\"keep\"); }\n"


def test_stacked_attributes_and_outer_docs_stay_with_test_item(tmp_path):
    source = """/// test documentation
#[allow(dead_code)]
#[cfg(test)]
#[ignore]
#[test]
fn test_only() {}
fn production() {}
"""
    path = tmp_path / "lib.rs"
    path.write_text(source)

    assert find_test_code_ranges(str(path), strict=True) == [
        (1, 6, "#[cfg(test)] fn")
    ]
    assert remove_test_regions(source, [(1, 6)]) == "fn production() {}\n"


def test_rstest_case_attribute_with_nested_brackets_finds_the_function(tmp_path):
    source = """mod tests {
    #[rstest]
    #[case::multiple(&["one", "two"], "three")]
    fn parameterized(#[case] input: &[&str], #[case] expected: &str) {
        assert!(!input.is_empty() && !expected.is_empty());
    }
}
"""
    path = tmp_path / "lib.rs"
    path.write_text(source)

    ranges = find_test_code_ranges(str(path), strict=True)

    assert ranges == [(2, 6, "#[rstest] fn")]


def test_inner_module_docs_are_not_consumed_with_test_item():
    source = """//! production module documentation

#[test]
fn test_only() {}

fn production() {}
"""
    filtered = remove_test_regions(source, [(3, 4)])

    assert "//! production module documentation" in filtered
    assert "fn test_only" not in filtered
    assert "fn production" in filtered


def test_nested_test_region_fails_closed_instead_of_being_ignored():
    source = """mod production {
    #[test]
    fn nested_test() {}
}
"""

    with pytest.raises(RustTestFilterError, match="nested Rust test regions"):
        find_test_ranges_from_content(source, "src/lib.rs", only_root_level=True)


def test_nested_test_helper_is_replaced_in_its_original_impl_scope():
    agent = """struct Value(i32);

impl Value {
    fn production(&self) -> i32 { self.0 + 1 }

    #[cfg(test)]
    fn test_value(&self) -> i32 { 999 }
}
"""
    ground_truth = """struct Value(i32);

impl Value {
    fn production(&self) -> i32 { self.0 }

    #[cfg(test)]
    fn test_value(&self) -> i32 { self.0 }
}
"""

    merged, stats = merge_src_with_gt_tests(agent, ground_truth, "src/lib.rs")

    assert "fn production(&self) -> i32 { self.0 + 1 }" in merged
    assert "fn test_value(&self) -> i32 { self.0 }" in merged
    assert "999" not in merged
    assert stats["nested_test_regions_replaced"] == 1


def test_new_nested_gt_helper_is_inserted_into_matching_scope():
    agent = """struct Value(i32);

impl Value {
    fn production(&self) -> i32 { self.0 + 1 }
}
"""
    ground_truth = """struct Value(i32);

impl Value {
    fn production(&self) -> i32 { self.0 }

    #[cfg(test)]
    fn test_value(&self) -> i32 { self.0 }
}
"""

    merged, stats = merge_src_with_gt_tests(agent, ground_truth, "src/lib.rs")

    assert "fn production(&self) -> i32 { self.0 + 1 }" in merged
    assert "fn test_value(&self) -> i32 { self.0 }" in merged
    assert merged.index("fn test_value") < merged.rindex("}")
    assert stats["nested_test_regions_inserted"] == 1


def test_cfg_test_field_inside_function_is_replaced_in_place():
    agent = """struct Context { line_term: u8, bytes: u8 }
fn make() -> Context {
    Context {
        #[cfg(test)]
        line_term: 99,
        bytes: 1,
    }
}
"""
    ground_truth = """struct Context { line_term: u8, bytes: u8 }
fn make() -> Context {
    Context {
        #[cfg(test)]
        line_term: 10,
        bytes: 2,
    }
}
"""

    merged, stats = merge_src_with_gt_tests(agent, ground_truth, "src/lib.rs")

    assert "line_term: 10" in merged
    assert "line_term: 99" not in merged
    assert "bytes: 1" in merged
    assert stats["nested_test_regions_replaced"] == 1


def test_cfg_test_block_expression_is_replaced_in_place():
    agent = """fn locale() -> Option<String> {
    choose(
        #[cfg(not(test))]
        { system_locale },
        #[cfg(test)]
        { || Some("agent".to_owned()) },
    )
}
"""
    ground_truth = """fn locale() -> Option<String> {
    choose(
        #[cfg(not(test))]
        { system_locale },
        #[cfg(test)]
        { || Some("ground-truth".to_owned()) },
    )
}
"""

    merged, stats = merge_src_with_gt_tests(agent, ground_truth, "src/lib.rs")

    assert 'Some("ground-truth".to_owned())' in merged
    assert 'Some("agent".to_owned())' not in merged
    assert "{ system_locale }" in merged
    assert stats["nested_test_regions_replaced"] == 1


def test_missing_agent_scope_for_nested_gt_test_fails_closed():
    agent = """struct Value(i32);
fn production(value: &Value) -> i32 { value.0 }
"""
    ground_truth = """struct Value(i32);
impl Value {
    #[cfg(test)]
    fn test_value(&self) -> i32 { self.0 }
}
"""

    with pytest.raises(RustTestFilterError, match="agent scope"):
        merge_src_with_gt_tests(agent, ground_truth, "src/lib.rs")


def test_ast_grep_failure_is_not_reported_as_no_tests(monkeypatch, tmp_path):
    path = tmp_path / "lib.rs"
    path.write_text("#[test]\nfn test_only() {}\n")

    monkeypatch.setattr(
        test_detector.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr="synthetic parser failure"
        ),
    )

    with pytest.raises(RustTestDetectionError, match="synthetic parser failure"):
        find_test_code_ranges(str(path), strict=True)


def test_container_batch_records_detector_exception_as_failure(monkeypatch):
    monkeypatch.setattr(
        rust_test_filter,
        "replace_agent_tests_with_ground_truth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RustTestFilterError("synthetic detector failure")
        ),
    )

    result = rust_test_filter.process_rust_files_in_container(
        "container", "M001", ["src/lib.rs"]
    )

    assert result["failed"] == 1
    assert result["processed"] == 0
    assert "failed closed" in result["details"][0]["reason"]


# ── Regression: */ backward-walk must never cross out of its own comment ──


def test_expand_doc_comments_ignores_plain_block_comment_above_test():
    """A plain /* ... */ above a test must not drag an earlier /** doc (and
    the production code between them) into the removal range."""
    lines = [
        "/** Doc for foo */",
        "pub fn foo() {}",
        "",
        "/* helper note */",
        "#[test]",
        "fn my_test() {}",
    ]
    start, end = rust_test_filter._expand_range_to_include_doc_comments(lines, 5, 6)
    assert (start, end) == (5, 6)


def test_expand_doc_comments_still_attaches_multiline_doc_block():
    lines = ["/**", " * doc", " */", "#[test]", "fn t() {}"]
    start, end = rust_test_filter._expand_range_to_include_doc_comments(lines, 4, 5)
    assert (start, end) == (1, 5)


def test_find_first_attr_line_stops_at_plain_block_comment():
    lines = [
        "/** Doc for MyStruct */",
        "pub struct MyStruct {}",
        "pub fn helper() {}",
        "/* tests below */",
        "#[cfg(test)]",
        "mod tests {}",
    ]
    assert test_detector._find_first_attr_line(lines, 4) == 4


def test_find_first_attr_line_attaches_contiguous_doc_block():
    lines = ["/**", " doc", " */", "#[test]", "fn t() {}"]
    assert test_detector._find_first_attr_line(lines, 3) == 0


# ── Regression: inline `/* c */ item` after an attribute keeps the item ──


def test_skip_to_item_keeps_item_after_inline_block_comment():
    lines = ["#[cfg(test)]", "/* c */ struct Foo;"]
    assert test_detector._skip_to_item(lines, 1) == 1


def test_skip_to_item_still_skips_pure_comment_lines():
    lines = ["#[cfg(test)]", "/* c */", "struct Foo;"]
    assert test_detector._skip_to_item(lines, 1) == 2


# ── Regression: GT read must fail closed on non-missing-path git errors ──


def test_git_ref_read_missing_path_is_new_file(monkeypatch):
    monkeypatch.setattr(
        rust_test_filter,
        "_run_docker_exec",
        lambda *_a, **_k: (
            False,
            "",
            "fatal: path 'src/x.rs' does not exist in 'milestone-M1-end'",
        ),
    )
    assert rust_test_filter._read_file_from_git_ref("c", "src/x.rs", "m") is None


def test_git_ref_read_bad_ref_fails_closed(monkeypatch):
    monkeypatch.setattr(
        rust_test_filter,
        "_run_docker_exec",
        lambda *_a, **_k: (False, "", "fatal: invalid object name 'milestone-M1-end'"),
    )
    with pytest.raises(RustTestFilterError):
        rust_test_filter._read_file_from_git_ref("c", "src/x.rs", "m")


def test_git_ref_read_success_returns_content(monkeypatch):
    monkeypatch.setattr(
        rust_test_filter, "_run_docker_exec", lambda *_a, **_k: (True, "fn a() {}", "")
    )
    assert rust_test_filter._read_file_from_git_ref("c", "src/x.rs", "m") == "fn a() {}"
