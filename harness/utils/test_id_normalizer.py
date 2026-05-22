"""
Test ID normalizer for handling parameterized tests and subtests.

This module provides normalization of test IDs to handle cases where:
1. Random IDs are used for subtests (e.g., t.Run(stringx.Rand(), ...))
2. Parameterized tests generate different IDs across runs
3. Table-driven tests use non-deterministic naming
4. Memory addresses appear in test names (e.g., function pointers like 0xb26440)

The normalizer helps reduce false positives in test change detection by
grouping together test runs that represent the same logical test.
"""

import re
from typing import Optional, Set, Dict, List
import logging

logger = logging.getLogger(__name__)

# Pattern for Go memory addresses (e.g., 0xb26440, 0x1a2b3c4d)
# These appear when printing function pointers or interface values
_MEMORY_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{5,16}")


class TestIdNormalizer:
    """
    Normalizes test IDs to handle parameterized/table-driven tests.

    For Go tests, this removes random-looking subtest suffixes that are
    generated non-deterministically (e.g., using stringx.Rand()).

    Examples:
        TestSafeMap/eJXQ3n3q -> TestSafeMap
        TestTimingWheel_ElapsedAndSet/1f81aac35d71cbef -> TestTimingWheel_ElapsedAndSet
        TestRedisBlpopEx/blpopex -> TestRedisBlpopEx/blpopex (unchanged, meaningful name)
    """

    def __init__(self, framework: str = "go_test", enable_normalization: bool = True):
        """
        Initialize the normalizer.

        Args:
            framework: Test framework name (go_test, etc.)
            enable_normalization: Whether to enable normalization (can be disabled for debugging)
        """
        self.framework = framework
        self.enable_normalization = enable_normalization

    def normalize(self, test_id: str) -> str:
        """
        Normalize a test ID by removing random subtest suffixes.

        Args:
            test_id: Original test ID

        Returns:
            Normalized test ID
        """
        if not self.enable_normalization:
            return test_id

        if self.framework == "go_test":
            return self._normalize_go_test(test_id)

        # For other frameworks (pytest, etc.), return unchanged
        # Pytest parametrized tests use deterministic parameter names,
        # so they can be matched exactly without normalization
        return test_id

    def _normalize_go_test(self, test_id: str) -> str:
        """
        Normalize Go test ID by removing random subtest suffixes and memory addresses.

        Rules for detecting random subtests:
        - 8+ characters long
        - Only alphanumeric (no underscores, hyphens, or other special chars)
        - Doesn't start with "test" or "Test" or "benchmark" or "Benchmark"

        Also normalizes:
        - Memory addresses (0x...) which appear when printing function pointers

        Args:
            test_id: Go test ID (e.g., github.com/org/repo/package/TestName/subtest)

        Returns:
            Normalized test ID with random subtests removed and addresses normalized
        """
        # First, normalize memory addresses (e.g., 0xb26440 -> 0x<ADDR>)
        # This handles cases like TestNewEngine/..._0xb26440 where function pointers
        # are included in the test name and change between runs
        normalized_id = _MEMORY_ADDRESS_PATTERN.sub("0x<ADDR>", test_id)

        parts = normalized_id.split("/")
        if len(parts) < 2:
            return normalized_id

        last_part = parts[-1]

        # Check if last part looks like a random ID
        if self._is_random_id(last_part):
            # Remove the random subtest
            normalized = "/".join(parts[:-1])
            logger.debug(f"Normalized Go test: {test_id} -> {normalized}")
            return normalized

        return normalized_id

    def _is_random_id(self, s: str) -> bool:
        """
        Check if a string looks like a random ID.

        A random ID is:
        - 8+ characters long
        - Pure alphanumeric (no _ or -)
        - Doesn't look like a meaningful test name

        Args:
            s: String to check

        Returns:
            True if it looks like a random ID
        """
        if len(s) < 8:
            return False

        if not s.isalnum():
            return False

        # Contains underscores or hyphens? Probably meaningful
        if "_" in s or "-" in s:
            return False

        # Starts with a Go test-function prefix (Test/Benchmark/Fuzz/Example)? Probably meaningful.
        lower = s.lower()
        if (lower.startswith("test") or lower.startswith("benchmark")
                or lower.startswith("fuzz") or lower.startswith("example")):
            return False

        # Looks random!
        return True

    def normalize_test_list(self, test_ids: List[str]) -> Dict[str, List[str]]:
        """
        Group test IDs by their normalized form.

        Args:
            test_ids: List of test IDs

        Returns:
            Dict mapping normalized ID -> list of original IDs
        """
        groups: Dict[str, List[str]] = {}
        for test_id in test_ids:
            normalized = self.normalize(test_id)
            if normalized not in groups:
                groups[normalized] = []
            groups[normalized].append(test_id)
        return groups

    def get_normalized_set(self, test_ids: List[str]) -> Set[str]:
        """
        Get set of unique normalized test IDs.

        Args:
            test_ids: List of test IDs

        Returns:
            Set of normalized test IDs
        """
        return {self.normalize(test_id) for test_id in test_ids}


def normalize_go_test_id(test_id: str) -> str:
    """
    Convenience function to normalize a single Go test ID.

    Args:
        test_id: Go test ID

    Returns:
        Normalized test ID
    """
    normalizer = TestIdNormalizer(framework="go_test")
    return normalizer.normalize(test_id)
