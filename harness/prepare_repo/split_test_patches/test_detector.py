"""
Test code region detection for Rust files.

Uses ast-grep for accurate parsing to detect:
- #[cfg(test)] blocks (mod, fn, impl, struct, enum, trait, const, static, type, use)
- #[cfg(test)] mod tests; (declarative/external modules)
- #![cfg(test)] file-level test-only modules
- #[test], #[bench] and other test framework attributes
- Doc tests (code blocks in /// or //! comments)
- Test-related macros (proptest!, macro_rules! with "test" in name)
"""

import re
import sys
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class RustTestDetectionError(RuntimeError):
    """Raised when Rust test/source separation cannot be proven safely."""


def _run_ast_grep_json(
    command: List[str],
    *,
    purpose: str,
    strict: bool = False,
) -> List[Dict[str, Any]]:
    """Run ast-grep and decode its JSON array.

    Empty successful output means "no matches".  Tool failures, timeouts, and
    malformed output are different: evaluation callers use ``strict=True`` so
    those conditions cannot silently turn into "there are no tests".
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise RustTestDetectionError(f"{purpose}: ast-grep failed: {exc}") from exc
        return []

    # ``ast-grep run --json`` uses exit 1 plus the valid JSON array ``[]`` for
    # a normal no-match result.  Do not confuse that with a tool failure.
    if (
        result.returncode == 1
        and result.stdout.strip() == "[]"
        and not result.stderr.strip()
    ):
        return []
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        if strict:
            raise RustTestDetectionError(
                f"{purpose}: ast-grep exited {result.returncode}: {detail}"
            )
        return []
    if not result.stdout.strip():
        return []
    try:
        matches = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if strict:
            raise RustTestDetectionError(
                f"{purpose}: ast-grep returned malformed JSON: {exc}"
            ) from exc
        return []
    if not isinstance(matches, list) or not all(isinstance(match, dict) for match in matches):
        if strict:
            raise RustTestDetectionError(f"{purpose}: ast-grep JSON is not an object array")
        return []
    return matches


# ============================================================
# Helper functions for finding block boundaries
# ============================================================


def _find_module_end_with_brace_counting(lines: List[str], start_idx: int) -> Optional[int]:
    """Find the end of a module by counting braces, starting from start_idx.

    WARNING: This is a fallback method. It does NOT handle:
    - Braces inside strings: "{ fake }"
    - Braces inside comments: // { comment }
    - Character literals: '{'

    Prefer using ast-grep based methods which use proper parsing.
    """
    brace_count = 0
    found_open = False
    for k in range(start_idx, len(lines)):
        for char in lines[k]:
            if char == "{":
                brace_count += 1
                found_open = True
            elif char == "}":
                brace_count -= 1
        if found_open and brace_count == 0:
            return k + 1  # 1-indexed
    return None


def _get_item_ranges_from_ast_grep(
    file_path: str,
    pattern: str,
    filter_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
    *,
    strict: bool = False,
) -> List[Tuple[int, int, str]]:
    """Use ast-grep to get precise item ranges.

    ast-grep properly handles strings, comments, and other edge cases
    that simple brace counting cannot handle.

    Args:
        file_path: Path to the Rust file
        pattern: ast-grep pattern (e.g., "mod $NAME { $$$ }")
        filter_fn: Optional function to filter matches (receives match dict)

    Returns:
        List of (start_line, end_line, matched_text) tuples (1-indexed)
    """
    matches = _run_ast_grep_json(
        ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
        purpose=f"match Rust pattern {pattern!r} in {file_path}",
        strict=strict,
    )
    ranges = []
    for match in matches:
        if filter_fn and not filter_fn(match):
            continue
        try:
            start_line = match["range"]["start"]["line"] + 1
            end_line = match["range"]["end"]["line"] + 1
        except (KeyError, TypeError) as exc:
            if strict:
                raise RustTestDetectionError(
                    f"match Rust pattern {pattern!r}: missing range metadata"
                ) from exc
            continue
        ranges.append((start_line, end_line, match.get("text", "")))
    return ranges



def _get_kind_ranges_by_kind(
    file_path: str,
    kinds: List[str],
    *,
    strict: bool = False,
) -> Dict[str, List[Tuple[int, int, str]]]:
    """Find several node kinds with one ast-grep parse/scan."""
    unique_kinds = list(dict.fromkeys(kinds))
    rule_ids = {f"evoclaw-kind-{index}": kind for index, kind in enumerate(unique_kinds)}
    rules = []
    for rule_id, kind in rule_ids.items():
        rules.append(
            f"""id: {rule_id}
language: rust
rule:
  kind: {kind}
""".rstrip()
        )
    matches = _run_ast_grep_json(
        [
            "ast-grep",
            "scan",
            "--inline-rules",
            "\n---\n".join(rules),
            "--json",
            file_path,
        ],
        purpose=f"find Rust node kinds {unique_kinds!r} in {file_path}",
        strict=strict,
    )
    ranges_by_kind: Dict[str, List[Tuple[int, int, str]]] = {
        kind: [] for kind in unique_kinds
    }
    for match in matches:
        kind = rule_ids.get(match.get("ruleId", ""))
        if kind is None:
            if strict:
                raise RustTestDetectionError(
                    f"find Rust node kinds: unexpected rule id {match.get('ruleId')!r}"
                )
            continue
        try:
            start_line = match["range"]["start"]["line"] + 1
            end_line = match["range"]["end"]["line"] + 1
        except (KeyError, TypeError) as exc:
            if strict:
                raise RustTestDetectionError(
                    f"find Rust {kind} nodes: missing range metadata"
                ) from exc
            continue
        ranges_by_kind[kind].append((start_line, end_line, match.get("text", "")))
    return ranges_by_kind


def _get_block_items_with_precise_ranges(
    file_path: str, *, strict: bool = False
) -> Dict[str, List[Tuple[int, int]]]:
    """Get precise ranges for block items (mod, impl, fn, trait, struct, enum).

    Uses ast-grep for accurate parsing that handles strings/comments correctly.

    Returns:
        Dict mapping item type to list of (start_line, end_line) tuples (1-indexed)
    """
    items = {
        "mod": [],
        "impl": [],
        "fn": [],
        "trait": [],
        "struct": [],
        "enum": [],
        "use": [],
        "const": [],
        "static": [],
        "type": [],
        "macro_definition": [],
        "macro_invocation": [],
        "extern_crate": [],
        "union": [],
        "foreign_mod": [],
        "field_initializer": [],
        "field_declaration": [],
        "enum_variant": [],
        "match_arm": [],
        "let_declaration": [],
        "expression_statement": [],
        "block": [],
    }

    # Node kinds cover modifiers that pattern snippets easily miss.  In
    # particular, ``fn $NAME(...)`` does not match ``async fn`` and the old
    # fuzzy lookup could then consume the next production function.
    kinds = {
        "mod": "mod_item",
        "impl": "impl_item",
        "fn": "function_item",
        "trait": "trait_item",
        "struct": "struct_item",
        "enum": "enum_item",
        "use": "use_declaration",
        "const": "const_item",
        "static": "static_item",
        "type": "type_item",
        "macro_definition": "macro_definition",
        "macro_invocation": "macro_invocation",
        "extern_crate": "extern_crate_declaration",
        "union": "union_item",
        "foreign_mod": "foreign_mod_item",
        "field_initializer": "field_initializer",
        "field_declaration": "field_declaration",
        "enum_variant": "enum_variant",
        "match_arm": "match_arm",
        "let_declaration": "let_declaration",
        "expression_statement": "expression_statement",
        "block": "block",
    }

    ranges_by_kind = _get_kind_ranges_by_kind(
        file_path, list(kinds.values()), strict=strict
    )
    for item_type, kind in kinds.items():
        items[item_type] = [
            (start, end) for start, end, _ in ranges_by_kind[kind]
        ]

    return items


def _find_item_end_from_ranges(
    item_line: int, item_ranges: List[Tuple[int, int]]
) -> Optional[int]:
    """Find the end line for an item given its start line using pre-computed ranges.

    Args:
        item_line: The line number where the item starts (1-indexed)
        item_ranges: List of (start_line, end_line) tuples from ast-grep
    Returns:
        The end line number, or None if not found
    """
    for start, end in item_ranges:
        # ``item_line`` is already the actual item line after attributes were
        # skipped.  A fuzzy forward search can select an unrelated production
        # item when ast-grep did not recognize the intended declaration.
        if start == item_line:
            return end
    return None


def _find_block_end(
    item_line: int,
    item_type: str,
    block_ranges: Dict[str, List[Tuple[int, int]]],
    lines: List[str],
    fallback: bool = True,
) -> Optional[int]:
    """Find the end of a block item using ast-grep ranges with fallback.

    Args:
        item_line: The 0-indexed line where the item starts
        item_type: Type of item (mod, impl, fn, trait, struct, enum)
        block_ranges: Pre-computed ranges from _get_block_items_with_precise_ranges
        lines: File lines for fallback brace counting
        fallback: Whether to fall back to brace counting if ast-grep fails

    Returns:
        1-indexed end line number, or None if not found
    """
    # Convert to 1-indexed for range lookup
    item_line_1idx = item_line + 1

    if item_type in block_ranges:
        end_line = _find_item_end_from_ranges(item_line_1idx, block_ranges[item_type])
        if end_line:
            return end_line

    # Fallback to brace counting (less accurate but works when ast-grep fails)
    if fallback:
        return _find_module_end_with_brace_counting(lines, item_line)

    return None


def _find_statement_end(
    item_line: int,
    item_type: str,
    item_ranges: Dict[str, List[Tuple[int, int]]],
    lines: List[str],
    *,
    fallback: bool,
) -> Optional[int]:
    """Find an exact semicolon item boundary, with an opt-in legacy fallback."""
    end_line = _find_item_end_from_ranges(item_line + 1, item_ranges.get(item_type, []))
    if end_line is not None:
        return end_line
    if fallback:
        return _find_single_statement_end(lines, item_line)
    return None


def _find_attributed_nested_node_end(
    attr_line: int,
    item_line: int,
    item_ranges: Dict[str, List[Tuple[int, int]]],
) -> Optional[int]:
    """Find cfg-attributed fields/variants/statements by exact AST start."""
    candidates = []
    for item_type in (
        "field_initializer",
        "field_declaration",
        "enum_variant",
        "match_arm",
        "let_declaration",
        "expression_statement",
        "block",
    ):
        for start, end in item_ranges[item_type]:
            # Some Rust nodes include their outer attribute in the node range
            # (field_initializer), while top-level items begin after it.
            if start in (attr_line + 1, item_line + 1):
                candidates.append((end - start, end))
    return min(candidates)[1] if candidates else None


# ============================================================
# Helper functions for parsing Rust syntax
# ============================================================


def _strip_visibility(line: str) -> str:
    """Remove visibility modifiers from a line.

    Handles: pub, pub(crate), pub(super), pub(self), pub(in path)
    """
    stripped = line.strip()
    # Match pub, pub(crate), pub(super), pub(self), pub(in ...)
    vis_pattern = r"^pub\s*(\([^)]*\))?\s*"
    return re.sub(vis_pattern, "", stripped)


def _scan_attribute_end(
    lines: List[str], start_line: int, start_column: int
) -> Optional[Tuple[int, int]]:
    """Find the matching outer ``]`` for one Rust attribute.

    Attribute arguments may contain nested square brackets, for example
    ``#[case(&["a", "b"])]``.  Looking for the first ``]`` mistakes the array
    terminator for the attribute terminator and makes the following function
    appear malformed.  Scan the bracket nesting while ignoring quoted strings
    and block/line comments.  The returned column is immediately after the
    closing bracket.
    """
    line = lines[start_line]
    attr_start = line.find("#[", start_column)
    inner_start = line.find("#![", start_column)
    candidates = [value for value in (attr_start, inner_start) if value >= 0]
    if not candidates:
        return None
    marker = min(candidates)
    bracket = line.find("[", marker)
    depth = 0
    quote: Optional[str] = None
    escaped = False
    in_block_comment = False

    for line_index in range(start_line, len(lines)):
        current = lines[line_index]
        column = bracket if line_index == start_line else 0
        while column < len(current):
            char = current[column]
            following = current[column + 1] if column + 1 < len(current) else ""

            if in_block_comment:
                if char == "*" and following == "/":
                    in_block_comment = False
                    column += 2
                else:
                    column += 1
                continue
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                column += 1
                continue
            if char == "/" and following == "/":
                break
            if char == "/" and following == "*":
                in_block_comment = True
                column += 2
                continue
            if char == '"':
                quote = char
                column += 1
                continue
            # Treat a compact character literal as quoted, without confusing a
            # Rust lifetime such as ``'a`` for an unterminated string.
            if char == "'":
                char_end = column + 2
                if following == "\\":
                    char_end += 1
                if char_end < len(current) and current[char_end] == "'":
                    quote = char
                    column += 1
                    continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return line_index, column + 1
            column += 1
    return None


def _strip_leading_attrs_from_line(line: str) -> Tuple[str, bool]:
    """Strip leading attributes from a single line.

    Returns (remainder, complete). If complete is False, an attribute starts
    on this line but does not close on the same line.
    """
    i = 0
    n = len(line)
    while True:
        while i < n and line[i].isspace():
            i += 1
        if line.startswith("#[", i) or line.startswith("#![", i):
            end = _scan_attribute_end([line], 0, i)
            if end is None:
                return "", False
            _, i = end
            continue
        break
    return line[i:].lstrip(), True


def _strip_leading_attrs(line: str) -> str:
    """Return line content after stripping any leading same-line attributes."""
    remainder, complete = _strip_leading_attrs_from_line(line)
    if not complete:
        return ""
    return remainder


def _skip_to_item(lines: List[str], start_idx: int) -> int:
    """Skip empty lines and attribute lines to find the actual item.

    Returns the index of the first non-attribute, non-empty line.
    """
    j = start_idx
    in_comment_block = False
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        if in_comment_block:
            if "*/" in line:
                in_comment_block = False
            j += 1
            continue
        if not stripped:
            j += 1
            continue
        if stripped.startswith("#[") or stripped.startswith("#!["):
            end = _scan_attribute_end(lines, j, 0)
            if end is None:
                return len(lines)
            end_line, end_column = end
            remainder, complete = _strip_leading_attrs_from_line(
                lines[end_line][end_column:]
            )
            remainder = remainder.lstrip()
            if complete and remainder and not remainder.startswith(("//", "/*")):
                return end_line
            j = end_line + 1
            continue
        if stripped.startswith("//"):
            j += 1
            continue
        if stripped.startswith("/*"):
            close = stripped.find("*/", 2)
            if close == -1:
                in_comment_block = True
                j += 1
                continue
            if stripped[close + 2:].strip():
                # A closed /* ... */ sharing the line with the item it
                # precedes: this IS the item line, don't skip past it.
                break
            j += 1
            continue
        # Found the item
        break
    return j


def _is_fn_line(line: str) -> bool:
    """Check if line starts a function definition."""
    stripped = _strip_leading_attrs(line)
    if not stripped:
        return False
    stripped = _strip_visibility(stripped)
    return bool(
        re.match(
            r'^(?:(?:async|const|unsafe|default)\s+|extern(?:\s+"[^"]*")?\s+)*fn\s+',
            stripped,
        )
    )


def _find_single_statement_end(lines: List[str], start_idx: int) -> int:
    """Find end of a single statement (use, const, static, type alias).

    Handles multi-line statements by looking for the semicolon.
    """
    for k in range(start_idx, min(start_idx + 50, len(lines))):
        if ";" in lines[k]:
            return k + 1  # 1-indexed
    return start_idx + 1  # Fallback to single line


def _find_first_attr_line(lines: List[str], attr_line: int) -> int:
    """Find the first attribute line by looking backwards.

    When we find #[test], there may be other attributes above it like
    #[ignore], #[should_panic], etc.  Outer doc comments are attributes in
    Rust too, so keep them attached to the test item.  Inner docs (``//!`` and
    ``/*!``) document the enclosing scope and must not be consumed.

    Returns 0-indexed line number.
    """
    first = attr_line
    k = attr_line - 1
    while k >= 0:
        stripped = lines[k].strip()
        if not stripped:
            # Empty line - continue looking
            k -= 1
            continue
        if stripped.startswith("#["):
            # Another attribute - include it
            first = k
            k -= 1
        elif stripped.startswith("///"):
            first = k
            k -= 1
        elif stripped.endswith("*/"):
            # Walk one outer block-doc comment backwards as a unit. Only
            # lines inside THIS block may be crossed: stop at the line that
            # opens it, and bail on another block's terminator — scanning
            # past the opener would swallow unrelated production code into
            # the test range.
            block_end = k
            if "/*" not in lines[k]:
                k -= 1
                while k >= 0 and "/*" not in lines[k]:
                    if "*/" in lines[k]:
                        k = -1
                        break
                    k -= 1
            if k >= 0 and "/**" in lines[k] and "/*!" not in lines[k]:
                first = k
                k -= 1
            else:
                k = block_end
                break
        else:
            # Non-attribute, non-empty line - stop
            break
    return first


def _extract_parenthesized_content(text: str, open_idx: int) -> Optional[str]:
    """Extract content inside matching parentheses starting at open_idx."""
    depth = 0
    in_string = None
    i = open_idx
    start = None
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                start = i + 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:i]
        i += 1
    return None


def _split_top_level_args(text: str) -> List[str]:
    """Split a comma-separated argument list at top level (no nested parens)."""
    args = []
    depth = 0
    in_string = None
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _tokenize_cfg_expr(text: str) -> List[Tuple[str, str]]:
    """Tokenize a cfg expression for a minimal parser."""
    tokens = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append(("IDENT", text[start:i]))
            continue
        if ch in ("(", ")", ",", "="):
            tokens.append((ch, ch))
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            tokens.append(("STRING", ""))
            continue
        i += 1
    return tokens


def _parse_cfg_meta(tokens: List[Tuple[str, str]], pos: int) -> Tuple[Optional[Tuple], int]:
    """Parse a cfg meta item from tokens."""
    if pos >= len(tokens) or tokens[pos][0] != "IDENT":
        return None, pos
    name = tokens[pos][1]
    pos += 1
    if pos < len(tokens) and tokens[pos][0] == "=":
        pos += 1
        if pos < len(tokens) and tokens[pos][0] in ("IDENT", "STRING"):
            pos += 1
        return ("name_value", name), pos
    if pos < len(tokens) and tokens[pos][0] == "(":
        pos += 1
        args = []
        while pos < len(tokens) and tokens[pos][0] != ")":
            if tokens[pos][0] == ",":
                pos += 1
                continue
            node, pos = _parse_cfg_meta(tokens, pos)
            if node is not None:
                args.append(node)
            else:
                pos += 1
        if pos < len(tokens) and tokens[pos][0] == ")":
            pos += 1
        return ("list", name, args), pos
    return ("word", name), pos


def _cfg_meta_possible_values(node: Tuple, *, test_value: bool) -> frozenset[bool]:
    """Return conservative possible values of a cfg expression.

    Non-``test`` predicates are unknown because the detector does not know the
    target platform/features.  Keeping both possible values lets us answer the
    question that matters safely: can this item exist when ``cfg(test)`` is
    false?
    """
    if not node:
        return frozenset({False, True})
    if node[0] == "word":
        if node[1] == "test":
            return frozenset({test_value})
        return frozenset({False, True})
    if node[0] == "name_value":
        return frozenset({False, True})
    if node[0] == "list":
        name = node[1]
        args = node[2]
        if name == "not":
            if len(args) != 1:
                return frozenset({False, True})
            return frozenset(
                not value
                for value in _cfg_meta_possible_values(args[0], test_value=test_value)
            )
        child_values = [
            _cfg_meta_possible_values(arg, test_value=test_value) for arg in args
        ]
        if name == "all":
            can_be_true = all(True in values for values in child_values)
            can_be_false = any(False in values for values in child_values)
            return frozenset(
                value for value, possible in ((True, can_be_true), (False, can_be_false)) if possible
            )
        if name == "any":
            can_be_true = any(True in values for values in child_values)
            can_be_false = all(False in values for values in child_values)
            return frozenset(
                value for value, possible in ((True, can_be_true), (False, can_be_false)) if possible
            )
        return frozenset({False, True})
    return frozenset({False, True})


def _cfg_expr_has_test(expr: str) -> bool:
    """Return whether a cfg condition can only be true during tests.

    Merely containing the word ``test`` is insufficient.  For example,
    ``any(windows, test)`` also enables production Windows code and therefore
    must never be removed.  In contrast, ``all(unix, test)`` implies test mode.
    """
    tokens = _tokenize_cfg_expr(expr)
    node, pos = _parse_cfg_meta(tokens, 0)
    if node is None or pos != len(tokens):
        return False
    return True not in _cfg_meta_possible_values(node, test_value=False)


def _extract_cfg_condition(attr_text: str, attr_name: str) -> Optional[str]:
    """Extract cfg or cfg_attr condition expression from attribute text."""
    m = re.search(r"\b" + re.escape(attr_name) + r"\s*\(", attr_text)
    if not m:
        return None
    content = _extract_parenthesized_content(attr_text, m.end() - 1)
    if content is None:
        return None
    if attr_name == "cfg_attr":
        parts = _split_top_level_args(content)
        if not parts:
            return None
        return parts[0]
    return content


def _is_cfg_test_attr(attr_text: str, attr_name: str) -> bool:
    """Return True if the attribute condition makes its item test-only."""
    cond = _extract_cfg_condition(attr_text, attr_name)
    if not cond:
        return False
    return _cfg_expr_has_test(cond)


def _has_item_after_column(line: str, col: int) -> bool:
    """Return True if there's an item start after col on the line."""
    if col < 0 or col >= len(line):
        return False
    remainder = line[col:].lstrip()
    if not remainder or remainder.startswith("//") or remainder.startswith("/*"):
        return False
    remainder, complete = _strip_leading_attrs_from_line(remainder)
    if not complete:
        return False
    remainder = remainder.lstrip()
    if not remainder:
        return False
    no_vis = _strip_visibility(remainder)
    return (
        no_vis.startswith("mod ")
        or no_vis.startswith("use ")
        or no_vis.startswith("impl ")
        or no_vis.startswith("const ")
        or no_vis.startswith("static ")
        or no_vis.startswith("type ")
        or no_vis.startswith("trait ")
        or no_vis.startswith("struct ")
        or no_vis.startswith("enum ")
        or no_vis.startswith("union ")
        or no_vis.startswith("macro_rules!")
        or no_vis.startswith("macro ")
        or no_vis.startswith("extern ")
        or bool(re.match(r"^(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*!", no_vis))
        or _is_fn_line(remainder)
    )


# ============================================================
# Doc test detection
# ============================================================


def _find_doc_test_ranges(lines: List[str]) -> List[Tuple[int, int, str]]:
    """Find doc test code blocks in documentation comments.

    Doc tests are code examples in /// or //! comments that get executed by `cargo test --doc`.
    They are marked by triple backticks: ```rust or just ```

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    ranges = []
    i = 0
    in_doc_block = False
    block_start = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check for doc comment lines
        is_doc_comment = stripped.startswith("///") or stripped.startswith("//!")

        if is_doc_comment:
            # Extract content after the doc comment marker (/// or //! are both 3 chars)
            content = stripped[3:].strip()

            # Check for code block markers
            if content.startswith("```"):
                if not in_doc_block:
                    # Start of doc test block
                    in_doc_block = True
                    block_start = i
                else:
                    # End of doc test block
                    in_doc_block = False
                    ranges.append((block_start + 1, i + 1, "doc test"))

        elif in_doc_block:
            # Non-doc-comment line while in a doc block means the block ended
            # (shouldn't normally happen in valid Rust)
            in_doc_block = False

        i += 1

    return ranges


# ============================================================
# Macro test detection
# ============================================================


def _find_macro_test_ranges(
    file_path: str, *, strict: bool = False
) -> List[Tuple[int, int, str]]:
    """Find test-related macro invocations.

    Detects:
    - Common test macro invocations (e.g., test!, test_case!, proptest!)

    NOTE: We intentionally do NOT detect macro_rules! definitions even if their
    name contains "test". A macro_rules! definition is just a definition, not
    actual test code. The #[test] functions inside the macro body only become
    real tests when the macro is invoked. Detecting macro_rules! definitions
    would incorrectly remove them, breaking code that depends on those macros.

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    ranges = []

    # Common test macro invocations
    test_macro_patterns = [
        "proptest! { $$$ }",
        "test! { $$$ }",
        "test_case! { $$$ }",
        "quickcheck! { $$$ }",
    ]

    for pattern in test_macro_patterns:
        matches = _run_ast_grep_json(
            ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
            purpose=f"find Rust test macro {pattern!r} in {file_path}",
            strict=strict,
        )
        for match in matches:
            try:
                start_line = match["range"]["start"]["line"] + 1
                end_line = match["range"]["end"]["line"] + 1
            except (KeyError, TypeError) as exc:
                if strict:
                    raise RustTestDetectionError(
                        f"find Rust test macro {pattern!r}: missing range metadata"
                    ) from exc
                continue
            macro_name = pattern.split("!")[0]
            ranges.append((start_line, end_line, f"{macro_name}! macro"))

    return ranges


# ============================================================
# Root-level detection using declaration_list
# ============================================================


def _find_declaration_list_ranges(
    file_path: str, *, strict: bool = False
) -> List[Tuple[int, int]]:
    """
    Find all declaration_list ranges in a Rust file using ast-grep.

    declaration_list is the content body of mod, impl, and trait blocks.
    Any item inside a declaration_list is NOT at file root level.

    This provides a unified way to detect nesting - instead of checking
    for each block type separately (mod, impl, trait), we just check if
    something is inside any declaration_list.

    Args:
        file_path: Path to the Rust file

    Returns:
        List of (start_line, end_line) tuples (1-indexed, inclusive)
    """
    return [
        (start, end)
        for start, end, _ in _get_kind_ranges_by_kind(
            file_path, ["declaration_list"], strict=strict
        )["declaration_list"]
    ]


def _is_inside_any_block(line_number: int, block_ranges: List[Tuple[int, int]]) -> bool:
    """
    Check if a line is inside any block (declaration_list).

    Args:
        line_number: 1-indexed line number
        block_ranges: List of (start, end) tuples for declaration_list blocks

    Returns:
        True if the line is inside any block (not at file root level)
    """
    for start, end in block_ranges:
        if start <= line_number <= end:
            return True
    return False


# ============================================================
# Range merging
# ============================================================


def _merge_overlapping_ranges(ranges: List[Tuple[int, int, str]]) -> List[Tuple[int, int, str]]:
    """Merge overlapping ranges, keeping the largest."""
    if not ranges:
        return []

    # Sort by start line
    sorted_ranges = sorted(ranges, key=lambda x: (x[0], -x[1]))

    merged = [sorted_ranges[0]]
    for start, end, reason in sorted_ranges[1:]:
        last_start, last_end, last_reason = merged[-1]
        if start <= last_end:
            # Overlapping - keep the larger range
            if end > last_end:
                merged[-1] = (last_start, end, last_reason)
        else:
            merged.append((start, end, reason))

    return merged


# ============================================================
# Main detection function
# ============================================================


def find_test_code_ranges(
    file_path: str,
    include_doc_tests: bool = False,
    only_root_level: bool = False,
    *,
    strict: bool = False,
) -> List[Tuple[int, int, str]]:
    """
    Find all test code regions in a Rust file using ast-grep.

    Detects:
    - #[cfg(...)] conditions that logically require test mode
    - #[cfg(test)] mod/use/fn/impl/const/static/type statements
    - #[cfg(test)] mod tests; (declarative/external module)
    - #![cfg(test)] file-level modules (cfg_attr never makes an item test-only)
    - #[test], #[bench] functions
    - Async test frameworks: tokio, async_std, actix_rt, smol, etc.
    - Other test frameworks: rstest, quickcheck, test_case, wasm_bindgen_test
    - Doc tests (code blocks in /// or //! comments) - optional, disabled by default
    - Test-related macros (proptest!, test!, macro_rules! with "test" in name)

    Args:
        file_path: Path to the Rust source file
        include_doc_tests: Whether to include doc tests (code blocks in /// comments).
            Default False because doc tests are typically part of source documentation
            and their changes are coupled with API changes, so they should be treated
            as src code for test/src separation purposes.
        only_root_level: If True, only return test regions at file root level.
            Test regions nested inside mod/impl/trait blocks are excluded.
            This prevents extraction of nested tests that would lose context
            when moved to file end (causing E0428, E0061 errors).
        strict: Raise :class:`RustTestDetectionError` when ast-grep or parsing
            fails instead of treating the file as if it contained no tests.

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    if not Path(file_path).exists() or not file_path.endswith(".rs"):
        return []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        ranges = []

        # Pre-compute block ranges using ast-grep for precise end detection
        # This avoids the pitfalls of simple brace counting (strings, comments, etc.)
        block_ranges = _get_block_items_with_precise_ranges(file_path, strict=strict)

        def _item_start_after_attr(match: Dict) -> int:
            end_line = match["range"]["end"]["line"]
            end_col = match["range"]["end"].get("column", 0)
            if end_line < len(lines) and _has_item_after_column(lines[end_line], end_col):
                return _skip_to_item(lines, end_line)
            return _skip_to_item(lines, end_line + 1)

        # ============================================================
        # Pattern Group 1: #[cfg(...test...)] - various item types
        # ============================================================
        # Use ast-grep to find #[cfg($$$)] and filter for test-related
        # Note: $$$ matches multiple tokens, needed for cfg(all(test, ...))
        #
        # IMPORTANT: Only #[cfg(test)] makes an item test-only.
        # #[cfg_attr(test, X)] does NOT make the item test-only - it just adds
        # attribute X during tests. The item itself exists in all builds.
        # Example: #[cfg_attr(test, derive(EnumIter))] pub enum Type { ... }
        #   - The enum exists in release builds (without EnumIter)
        #   - The enum exists in test builds (with EnumIter)
        #   - Removing this would break the build!
        for attr_name in ("cfg",):  # Removed "cfg_attr" - it doesn't make items test-only
            matches = _run_ast_grep_json(
                [
                    "ast-grep", "run", "--pattern", f"#[{attr_name}($$$)]",
                    "--lang", "rust", "--json", file_path,
                ],
                purpose=f"find #[{attr_name}] attributes in {file_path}",
                strict=strict,
            )
            for match in matches:
                matched_text = match.get("text", "")
                if not _is_cfg_test_attr(matched_text, attr_name):
                    continue

                cfg_line = match["range"]["start"]["line"]  # 0-indexed
                j = _item_start_after_attr(match)
                if j >= len(lines):
                    if strict:
                        raise RustTestDetectionError(
                            f"test-only cfg attribute at {file_path}:{cfg_line + 1} has no item"
                        )
                    continue

                next_line = _strip_leading_attrs(lines[j]).strip()
                if not next_line:
                    if strict:
                        raise RustTestDetectionError(
                            f"cannot identify item after test-only cfg at {file_path}:{cfg_line + 1}"
                        )
                    continue
                next_line_no_vis = _strip_visibility(next_line)
                start_line = _find_first_attr_line(lines, cfg_line) + 1
                end_line = None
                reason = ""

                # Determine item type and find end using precise ast-grep ranges.
                if next_line_no_vis.startswith("mod "):
                    if ";" in next_line and "{" not in next_line:
                        end_line = _find_item_end_from_ranges(
                            j + 1, block_ranges["mod"]
                        )
                        if end_line is None and not strict:
                            end_line = _find_single_statement_end(lines, j)
                        reason = f"#[{attr_name}(test)] mod (external)"
                    else:
                        end_line = _find_block_end(
                            j, "mod", block_ranges, lines, fallback=not strict
                        )
                        reason = f"#[{attr_name}(test)] mod"
                elif next_line_no_vis.startswith("use "):
                    end_line = _find_statement_end(
                        j, "use", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] use"
                elif _is_fn_line(lines[j]):
                    end_line = _find_block_end(j, "fn", block_ranges, lines, fallback=not strict)
                    reason = f"#[{attr_name}(test)] fn"
                elif next_line_no_vis.startswith("impl "):
                    end_line = _find_block_end(j, "impl", block_ranges, lines, fallback=not strict)
                    reason = f"#[{attr_name}(test)] impl"
                elif next_line_no_vis.startswith("const "):
                    end_line = _find_statement_end(
                        j, "const", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] const"
                elif next_line_no_vis.startswith("static "):
                    end_line = _find_statement_end(
                        j, "static", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] static"
                elif next_line_no_vis.startswith("type "):
                    end_line = _find_statement_end(
                        j, "type", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] type"
                elif next_line_no_vis.startswith("trait "):
                    end_line = _find_block_end(j, "trait", block_ranges, lines, fallback=not strict)
                    reason = f"#[{attr_name}(test)] trait"
                elif next_line_no_vis.startswith("struct "):
                    end_line = (
                        _find_block_end(j, "struct", block_ranges, lines, fallback=not strict)
                        if "{" in next_line
                        else _find_item_end_from_ranges(j + 1, block_ranges["struct"])
                    )
                    reason = f"#[{attr_name}(test)] struct"
                elif next_line_no_vis.startswith("enum "):
                    end_line = _find_block_end(j, "enum", block_ranges, lines, fallback=not strict)
                    reason = f"#[{attr_name}(test)] enum"
                elif next_line_no_vis.startswith("union "):
                    end_line = _find_block_end(
                        j, "union", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] union"
                elif next_line_no_vis.startswith(("macro_rules!", "macro ")):
                    end_line = _find_block_end(
                        j, "macro_definition", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] macro definition"
                elif re.match(
                    r"^(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*!", next_line_no_vis
                ):
                    end_line = _find_block_end(
                        j, "macro_invocation", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] macro invocation"
                elif next_line_no_vis.startswith("extern crate "):
                    end_line = _find_statement_end(
                        j, "extern_crate", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] extern crate"
                elif next_line_no_vis.startswith("extern "):
                    end_line = _find_block_end(
                        j, "foreign_mod", block_ranges, lines, fallback=not strict
                    )
                    reason = f"#[{attr_name}(test)] foreign module"

                if end_line is None:
                    end_line = _find_attributed_nested_node_end(
                        cfg_line, j, block_ranges
                    )
                    if end_line is not None:
                        reason = f"#[{attr_name}(test)] nested node"

                if end_line is not None:
                    ranges.append((start_line, end_line, reason))
                elif strict:
                    raise RustTestDetectionError(
                        f"cannot determine test-only item boundary at {file_path}:{cfg_line + 1}"
                    )

        # Inner attribute: #![cfg(test)] marks entire file as test-only
        matches = _run_ast_grep_json(
            ["ast-grep", "run", "--pattern", "#![cfg($$$)]", "--lang", "rust", "--json", file_path],
            purpose=f"find inner cfg attributes in {file_path}",
            strict=strict,
        )
        for match in matches:
            matched_text = match.get("text", "")
            if _is_cfg_test_attr(matched_text, "cfg"):
                ranges.append((1, len(lines), "#![cfg(test)] file"))

        # ============================================================
        # Pattern Group 2: Test function attributes
        # ============================================================
        test_fn_attrs = [
            # Standard test attributes
            ("test", False),
            ("bench", False),
            # Async runtime test attributes
            ("tokio::test", True),
            ("async_std::test", True),
            ("actix_rt::test", True),
            ("smol_potat::test", True),
            ("futures_test::test", True),
            # Other test frameworks
            ("rstest", True),
            ("quickcheck", True),
            ("wasm_bindgen_test", True),
        ]

        for attr_name, allow_args in test_fn_attrs:
            patterns = [f"#[{attr_name}]"]
            if allow_args:
                patterns.append(f"#[{attr_name}($$$)]")

            for pattern in patterns:
                matches = _run_ast_grep_json(
                    ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
                    purpose=f"find Rust test attribute {pattern!r} in {file_path}",
                    strict=strict,
                )
                for match in matches:
                    attr_line = match["range"]["start"]["line"]  # 0-indexed
                    j = _item_start_after_attr(match)
                    if j >= len(lines) or not _is_fn_line(lines[j]):
                        if strict:
                            raise RustTestDetectionError(
                                f"test attribute {pattern!r} at {file_path}:{attr_line + 1} "
                                "is not attached to a recognizable function"
                            )
                        continue

                    first_attr = _find_first_attr_line(lines, attr_line)
                    start_line = first_attr + 1
                    end_line = _find_block_end(
                        j, "fn", block_ranges, lines, fallback=not strict
                    )
                    if end_line:
                        ranges.append((start_line, end_line, f"#[{attr_name}] fn"))
                    elif strict:
                        raise RustTestDetectionError(
                            f"cannot determine test function boundary at "
                            f"{file_path}:{attr_line + 1}"
                        )

        # ============================================================
        # Pattern Group 3: Parameterized test attributes (with arguments)
        # ============================================================
        # #[test_case(...)] - need to match with arguments
        matches = _run_ast_grep_json(
            ["ast-grep", "run", "--pattern", "#[test_case($$$)]", "--lang", "rust", "--json", file_path],
            purpose=f"find #[test_case] attributes in {file_path}",
            strict=strict,
        )
        # Group by function (multiple test_case on same fn)
        processed_fns = set()
        for match in matches:
            attr_line = match["range"]["start"]["line"]
            j = _item_start_after_attr(match)
            if j >= len(lines):
                if strict:
                    raise RustTestDetectionError(
                        f"#[test_case] at {file_path}:{attr_line + 1} has no function"
                    )
                continue
            if j in processed_fns:
                continue
            if not _is_fn_line(lines[j]):
                if strict:
                    raise RustTestDetectionError(
                        f"#[test_case] at {file_path}:{attr_line + 1} is not attached "
                        "to a recognizable function"
                    )
                continue
            processed_fns.add(j)
            first_attr = _find_first_attr_line(lines, attr_line)
            start_line = first_attr + 1
            end_line = _find_block_end(j, "fn", block_ranges, lines, fallback=not strict)
            if end_line:
                ranges.append((start_line, end_line, "#[test_case] fn"))
            elif strict:
                raise RustTestDetectionError(
                    f"cannot determine #[test_case] function boundary at "
                    f"{file_path}:{attr_line + 1}"
                )

        # #[rstest] #[case(...)] combinations
        matches = _run_ast_grep_json(
            ["ast-grep", "run", "--pattern", "#[case($$$)]", "--lang", "rust", "--json", file_path],
            purpose=f"find #[case] attributes in {file_path}",
            strict=strict,
        )
        processed_fns = set()
        for match in matches:
            attr_line = match["range"]["start"]["line"]
            j = _item_start_after_attr(match)
            if j >= len(lines):
                if strict:
                    raise RustTestDetectionError(
                        f"#[case] at {file_path}:{attr_line + 1} has no function"
                    )
                continue
            if j in processed_fns:
                continue
            if not _is_fn_line(lines[j]):
                if strict:
                    raise RustTestDetectionError(
                        f"#[case] at {file_path}:{attr_line + 1} is not attached "
                        "to a recognizable function"
                    )
                continue
            processed_fns.add(j)
            first_attr = _find_first_attr_line(lines, attr_line)
            start_line = first_attr + 1
            end_line = _find_block_end(j, "fn", block_ranges, lines, fallback=not strict)
            if end_line:
                ranges.append((start_line, end_line, "#[rstest/case] fn"))
            elif strict:
                raise RustTestDetectionError(
                    f"cannot determine #[case] function boundary at "
                    f"{file_path}:{attr_line + 1}"
                )

        # #[fixture] for rstest
        matches = _run_ast_grep_json(
            ["ast-grep", "run", "--pattern", "#[fixture]", "--lang", "rust", "--json", file_path],
            purpose=f"find #[fixture] attributes in {file_path}",
            strict=strict,
        )
        for match in matches:
            attr_line = match["range"]["start"]["line"]
            j = _item_start_after_attr(match)
            if j >= len(lines) or not _is_fn_line(lines[j]):
                if strict:
                    raise RustTestDetectionError(
                        f"#[fixture] at {file_path}:{attr_line + 1} is not attached "
                        "to a recognizable function"
                    )
                continue
            start_line = _find_first_attr_line(lines, attr_line) + 1
            end_line = _find_block_end(j, "fn", block_ranges, lines, fallback=not strict)
            if end_line:
                ranges.append((start_line, end_line, "#[fixture] fn"))
            elif strict:
                raise RustTestDetectionError(
                    f"cannot determine #[fixture] function boundary at "
                    f"{file_path}:{attr_line + 1}"
                )

        # ============================================================
        # Pattern Group 4: Doc tests (code blocks in documentation)
        # ============================================================
        # Only include doc tests if explicitly requested.
        # Doc tests are part of source documentation and their changes are
        # typically coupled with API changes, so by default we treat them as src.
        if include_doc_tests:
            doc_test_ranges = _find_doc_test_ranges(lines)
            ranges.extend(doc_test_ranges)

        # ============================================================
        # Pattern Group 5: Test-related macros
        # ============================================================
        macro_test_ranges = _find_macro_test_ranges(file_path, strict=strict)
        ranges.extend(macro_test_ranges)

        # Merge overlapping ranges
        ranges = _merge_overlapping_ranges(ranges)

        # Filter to only root-level test regions if requested
        if only_root_level:
            declaration_list_ranges = _find_declaration_list_ranges(file_path, strict=strict)
            ranges = [
                (start, end, reason)
                for start, end, reason in ranges
                if not _is_inside_any_block(start, declaration_list_ranges)
            ]

        return ranges

    except RustTestDetectionError:
        raise
    except Exception as e:
        if strict:
            raise RustTestDetectionError(
                f"find_test_code_ranges failed for {file_path}: {e}"
            ) from e
        print(f"Warning: find_test_code_ranges failed for {file_path}: {e}", file=sys.stderr)
        return []


def find_test_module_ranges(file_path: str, include_doc_tests: bool = False) -> List[Tuple[int, int]]:
    """
    Find all test code regions in a Rust file.

    Args:
        file_path: Path to the Rust source file
        include_doc_tests: Whether to include doc tests. Default False.

    Returns list of (start_line, end_line) tuples (1-indexed).
    """
    ranges = find_test_code_ranges(file_path, include_doc_tests=include_doc_tests)
    return [(start, end) for start, end, _ in ranges]


def find_test_ranges_from_content(
    content: str,
    file_path: str,
    include_doc_tests: bool = False,
    only_root_level: bool = False,
    *,
    strict: bool = False,
) -> List[Tuple[int, int]]:
    """Find test code ranges from file content string.

    Uses ast-grep for accurate parsing by writing content to a temp file.
    Falls back to simple text parsing if ast-grep is unavailable.

    Args:
        content: File content as a string
        file_path: Original file path (used to determine file type)
        include_doc_tests: Whether to include doc tests. Default False.
        only_root_level: If True, only return test regions at file root level.
            Test regions nested inside mod/impl/trait blocks are excluded.
        strict: Raise when precise detection fails.  When true, the lossy text
            fallback is intentionally disabled.
    """
    if not file_path.endswith(".rs"):
        return []

    import tempfile
    import os

    # Try ast-grep approach first (more accurate)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            ranges = find_test_code_ranges(
                tmp_path,
                include_doc_tests=include_doc_tests,
                only_root_level=only_root_level,
                strict=strict,
            )
            return [(start, end) for start, end, _ in ranges]
        finally:
            os.unlink(tmp_path)

    except RustTestDetectionError:
        raise
    except Exception as exc:
        if strict:
            raise RustTestDetectionError(
                f"failed to inspect Rust content for {file_path}: {exc}"
            ) from exc
        # Fallback to simple text parsing
        pass

    # Fallback: simple text parsing for #[cfg(test)] mod blocks
    lines = content.split("\n")
    ranges = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#![cfg") and _is_cfg_test_attr(line, "cfg"):
            ranges.append((1, len(lines)))
            return ranges

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()
        if stripped.startswith("#[cfg") and not stripped.startswith("#[cfg_attr"):
            attr_name = "cfg"
            if not _is_cfg_test_attr(raw_line, attr_name):
                i += 1
                continue

            remainder, complete = _strip_leading_attrs_from_line(raw_line)
            remainder = remainder.lstrip()
            if complete and remainder and not remainder.startswith(("//", "/*")):
                j = i
            else:
                j = _skip_to_item(lines, i + 1)

            if j < len(lines):
                next_line = _strip_leading_attrs(lines[j])
                next_line = _strip_visibility(next_line)
                if next_line.startswith("mod "):
                    start_line = i + 1  # 1-indexed
                    end_line = _find_module_end_with_brace_counting(lines, j)
                    if end_line:
                        ranges.append((start_line, end_line))
                        i = end_line - 1
        i += 1

    return ranges
