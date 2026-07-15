from harness.utils.snapshot import (
    ATOMIC_ROOT_MANIFEST_GROUPS,
    ManifestOverlay,
    RECURSIVE_BUILD_MANIFEST_NAMES,
    ROOT_BUILD_FILES,
    expand_atomic_manifest_overlay,
    find_build_manifests,
    get_snapshot_paths,
    is_build_manifest,
    is_go_manifest_in_scope,
    make_snapshot_metadata,
    should_include_snapshot_file,
)
from harness.utils.src_filter import SrcFileFilter


def _filter() -> SrcFileFilter:
    return SrcFileFilter(src_dirs=["core"], test_dirs=["**/*_test.go"])


def test_root_build_files_cover_go_and_maven():
    assert {"go.mod", "go.sum", "go.work", "go.work.sum", "pom.xml"} <= set(ROOT_BUILD_FILES)
    assert {"pom.xml", "go.mod", "go.sum", "go.work", "go.work.sum"} <= (
        RECURSIVE_BUILD_MANIFEST_NAMES
    )
    assert frozenset({"go.mod", "go.sum"}) in ATOMIC_ROOT_MANIFEST_GROUPS


def test_atomic_go_manifest_overlay_captures_unchanged_companion():
    overlay = ManifestOverlay.create("base", upserts=["go.mod"])

    expanded = expand_atomic_manifest_overlay(
        overlay,
        {"go.mod", "go.sum", "README.md"},
    )

    assert expanded.upserts == frozenset({"go.mod", "go.sum"})
    assert expanded.deletes == frozenset()


def test_atomic_go_manifest_overlay_preserves_real_tombstone():
    overlay = ManifestOverlay.create("base", deletes=["go.sum"])

    expanded = expand_atomic_manifest_overlay(overlay, {"go.mod"})

    assert expanded.upserts == frozenset({"go.mod"})
    assert expanded.deletes == frozenset({"go.sum"})


def test_exact_go_projection_is_limited_to_declared_source_roots():
    overlay = ManifestOverlay.create(
        "base",
        upserts=["go.mod", "tools/goctl/go.mod", "core/plugin/go.mod"],
    )
    expanded = expand_atomic_manifest_overlay(
        overlay,
        {
            "go.mod",
            "go.sum",
            "tools/goctl/go.mod",
            "tools/goctl/go.sum",
            "core/plugin/go.mod",
            "core/plugin/go.sum",
        },
        ["core"],
    )

    assert expanded.upserts == frozenset(
        {"go.mod", "go.sum", "core/plugin/go.mod", "core/plugin/go.sum"}
    )
    assert is_go_manifest_in_scope("go.mod", [])
    assert is_go_manifest_in_scope("core/plugin/go.mod", ["core"])
    assert not is_go_manifest_in_scope("tools/goctl/go.mod", ["core"])


def test_snapshot_metadata_records_explicit_go_present_set(tmp_path):
    snapshot = tmp_path / "source_snapshot.tar"
    snapshot.write_bytes(b"snapshot")
    overlay = ManifestOverlay.create(
        "base",
        upserts=["go.mod", "go.sum", "module/pom.xml"],
    )

    metadata = make_snapshot_metadata(
        tag="agent-impl-M001",
        snapshot_file=snapshot,
        manifest_overlay=overlay,
    )

    assert metadata["go_manifest_projection"] == {
        "schema_version": 1,
        "present": ["go.mod", "go.sum"],
    }


def test_snapshot_paths_add_only_changed_existing_root_build_files():
    paths = get_snapshot_paths(
        ["core", "missing"],
        existing_root_files={"go.mod", "pom.xml"},
        existing_src_dirs={"core"},
        extra_build_manifests={"go.mod", "pom.xml"},
    )
    assert paths == ["core", "go.mod", "pom.xml"]

    # Unchanged BASE manifests must be supplied by milestone END, not the tar.
    assert get_snapshot_paths(
        ["core"],
        existing_root_files={"go.mod", "pom.xml"},
        existing_src_dirs={"core"},
    ) == ["core"]


def test_only_changed_root_build_file_survives_source_filter():
    src_filter = _filter()
    assert not should_include_snapshot_file("go.mod", src_filter)
    assert not should_include_snapshot_file("pom.xml", src_filter)
    assert should_include_snapshot_file(
        "go.mod", src_filter, extra_build_manifests={"go.mod"}
    )
    assert should_include_snapshot_file(
        "pom.xml", src_filter, extra_build_manifests={"pom.xml"}
    )
    assert should_include_snapshot_file("core/main.go", src_filter)
    assert not should_include_snapshot_file("core/main_test.go", src_filter)


def test_only_changed_nested_maven_manifests_bypass_filters():
    src_filter = SrcFileFilter(
        src_dirs=["core"],
        test_dirs=["dubbo-test/**", "**/src/test/**"],
    )
    changed = {
        "modules/service/pom.xml",
        "dubbo-test/dependencies/pom.xml",
    }
    assert not should_include_snapshot_file("modules/untouched/pom.xml", src_filter)
    assert should_include_snapshot_file(
        "modules/service/pom.xml", src_filter, extra_build_manifests=changed
    )
    assert should_include_snapshot_file(
        "dubbo-test/dependencies/pom.xml", src_filter, extra_build_manifests=changed
    )
    assert not should_include_snapshot_file("dubbo-test/dependencies/config.xml", src_filter)


def test_unchanged_pom_inside_source_directory_is_still_excluded():
    src_filter = SrcFileFilter(src_dirs=["modules"], test_dirs=[])
    assert src_filter.should_include_in_snapshot("modules/service/pom.xml")
    assert not should_include_snapshot_file("modules/service/pom.xml", src_filter)


def test_find_build_manifests_normalizes_and_rejects_unrelated_files():
    assert find_build_manifests(
        [
            "./pom.xml",
            "modules/a/pom.xml",
            "tools/goctl/go.mod",
            "tools/goctl/go.sum",
            "tools/workspace/go.work",
            "tools/workspace/go.work.sum",
            "core/main.go",
            "docs/example.xml",
        ]
    ) == {
        "pom.xml",
        "modules/a/pom.xml",
        "tools/goctl/go.mod",
        "tools/goctl/go.sum",
        "tools/workspace/go.work",
        "tools/workspace/go.work.sum",
    }
    assert is_build_manifest("go.mod")
    assert is_build_manifest("tools/goctl/go.mod")
    assert is_build_manifest("nested/pom.xml")
    assert not is_build_manifest("nested/settings.xml")


def test_recursive_go_test_fixture_manifest_is_not_agent_authority():
    src_filter = SrcFileFilter(
        src_dirs=["core", "tools"],
        test_dirs=["**/testdata/**", "**/*_test.go"],
    )
    assert find_build_manifests(
        [
            "tools/goctl/go.mod",
            "tools/goctl/go.work",
            "core/testdata/module/go.mod",
            "core/testdata/module/go.work",
        ],
        src_filter,
    ) == {"tools/goctl/go.mod", "tools/goctl/go.work"}


def test_snapshot_paths_add_reactor_poms_outside_source_dirs_once():
    paths = get_snapshot_paths(
        ["core", "modules"],
        existing_root_files={"pom.xml"},
        existing_src_dirs={"core", "modules"},
        extra_build_manifests={
            "pom.xml",
            "modules/service/pom.xml",  # already covered by modules/
            "dubbo-distribution/dubbo-bom/pom.xml",
            "dubbo-test/dependencies-all/pom.xml",
        },
    )
    assert paths == [
        "core",
        "modules",
        "pom.xml",
        "dubbo-distribution/dubbo-bom/pom.xml",
        "dubbo-test/dependencies-all/pom.xml",
    ]
