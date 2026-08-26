import csv
import threading
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
import logging

logger = logging.getLogger("e2e.dag")


class DAGManager:
    """Manages milestone dependencies and execution state.

    Thread Safety:
        This class is thread-safe. All public methods that read or modify state
        are protected by an internal RLock. Use snapshot methods (get_state_snapshot,
        get_completed_snapshot, etc.) when you need consistent reads across multiple
        state variables.
    """

    def __init__(
        self,
        dependencies_csv: Path,
        selected_ids_file: Optional[Path] = None,
        ignore_weak_dependencies: bool = False,
        additional_dependencies_csv: Optional[Path] = None,
    ):
        self.dependencies_csv = dependencies_csv
        self.selected_ids_file = selected_ids_file
        self.ignore_weak_dependencies = ignore_weak_dependencies
        self.additional_dependencies_csv = additional_dependencies_csv
        self.adj_list: Dict[str, List[str]] = {}  # source -> [targets]
        self.reverse_adj_list: Dict[str, List[str]] = {}  # target -> [sources]
        self.dependencies_info: Dict[Tuple[str, str], Dict[str, str]] = {}  # (source, target) -> info
        self.all_milestones: Set[str] = set()

        # State tracking
        self._completed_milestones: Set[str] = set()
        self._failed_milestones: Set[str] = set()
        self._skipped_milestones: Set[str] = set()  # For milestones unreachable due to failure
        self._submitted_milestones: Set[str] = set()  # For milestones submitted but not yet evaluated

        # Thread safety lock (RLock for reentrant calls like mark_failed -> _update_skipped)
        self._lock = threading.RLock()

        self._load_dag()

    # === Thread-safe property accessors (return copies for safety) ===

    @property
    def completed_milestones(self) -> Set[str]:
        """Thread-safe access to completed milestones (returns a copy)."""
        with self._lock:
            return set(self._completed_milestones)

    @property
    def failed_milestones(self) -> Set[str]:
        """Thread-safe access to failed milestones (returns a copy)."""
        with self._lock:
            return set(self._failed_milestones)

    @property
    def skipped_milestones(self) -> Set[str]:
        """Thread-safe access to skipped milestones (returns a copy)."""
        with self._lock:
            return set(self._skipped_milestones)

    @property
    def submitted_milestones(self) -> Set[str]:
        """Thread-safe access to submitted milestones (returns a copy)."""
        with self._lock:
            return set(self._submitted_milestones)

    def _load_selected_ids(self) -> Optional[Set[str]]:
        """Load selected milestone IDs from file if it exists.

        Returns:
            Set of selected milestone IDs, or None if file doesn't exist.
        """
        if self.selected_ids_file is None:
            # Try default location (same directory as dependencies.csv)
            default_path = self.dependencies_csv.parent / "selected_milestone_ids.txt"
            if default_path.exists():
                self.selected_ids_file = default_path
            else:
                return None

        if not self.selected_ids_file.exists():
            return None

        selected = set()
        with open(self.selected_ids_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith("#"):
                    selected.add(line)

        logger.info(f"Loaded {len(selected)} selected milestone IDs from {self.selected_ids_file}")
        return selected

    def _load_dag(self):
        """Load dependencies from CSV, optionally filtered by selected IDs."""
        if not self.dependencies_csv.exists():
            raise FileNotFoundError(f"Dependencies file not found: {self.dependencies_csv}")

        # Load selected IDs (if file exists)
        selected_ids = self._load_selected_ids()

        # First pass: collect all milestones from dependencies
        milestones_from_deps: Set[str] = set()

        with open(self.dependencies_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                source = row["source_id"]
                target = row["target_id"]
                if source.startswith("#"):
                    continue

                # If selected_ids is specified, filter to only include selected milestones
                if selected_ids is not None:
                    if source not in selected_ids or target not in selected_ids:
                        continue

                milestones_from_deps.add(source)
                milestones_from_deps.add(target)

                if source not in self.adj_list:
                    self.adj_list[source] = []
                self.adj_list[source].append(target)

                if target not in self.reverse_adj_list:
                    self.reverse_adj_list[target] = []
                self.reverse_adj_list[target].append(source)

                self.dependencies_info[(source, target)] = row

        # Load additional dependencies from trial-level CSV (if provided)
        additional_count = 0
        if self.additional_dependencies_csv and self.additional_dependencies_csv.exists():
            with open(self.additional_dependencies_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    source = row["source_id"]
                    target = row["target_id"]
                    if source.startswith("#"):
                        continue

                    if selected_ids is not None:
                        if source not in selected_ids or target not in selected_ids:
                            continue

                    milestones_from_deps.add(source)
                    milestones_from_deps.add(target)

                    if source not in self.adj_list:
                        self.adj_list[source] = []
                    self.adj_list[source].append(target)

                    if target not in self.reverse_adj_list:
                        self.reverse_adj_list[target] = []
                    self.reverse_adj_list[target].append(source)

                    self.dependencies_info[(source, target)] = row
                    additional_count += 1

            logger.info(f"Loaded {additional_count} additional dependencies from {self.additional_dependencies_csv}")

        # Determine final milestone set
        if selected_ids is not None:
            # Use selected IDs - includes milestones without dependencies
            self.all_milestones = selected_ids
            # Log milestones that are in selected_ids but have no dependencies (root nodes)
            root_nodes = selected_ids - milestones_from_deps
            if root_nodes:
                logger.info(f"Root milestones (no dependencies): {sorted(root_nodes)}")
        else:
            # Fallback: use milestones from dependencies.csv
            self.all_milestones = milestones_from_deps

        logger.info(
            f"Loaded DAG with {len(self.all_milestones)} milestones and {len(self.dependencies_info)} dependencies."
        )

    def get_next_runnable(self) -> List[str]:
        """Get list of milestones that are ready to run.

        CSV format: source_id, target_id means target depends on source.
        A milestone is runnable if all its dependencies (sources) are satisfied:
        - Strong dependency: source must be COMPLETED
        - Weak dependency: source must be FINISHED (completed OR failed)

        Thread-safe: Uses internal lock to ensure consistent state reads.
        """
        with self._lock:
            runnable = []

            for ms in self.all_milestones:
                # Skip if already processed or submitted
                if ms in self._completed_milestones or ms in self._failed_milestones or ms in self._skipped_milestones:
                    continue
                if ms in self._submitted_milestones:
                    continue

                # Check dependencies (ms depends on these sources)
                # reverse_adj_list[ms] = sources that ms depends on (ms is target)
                dependencies = self.reverse_adj_list.get(ms, [])
                all_deps_met = True
                for dep in dependencies:
                    # Key is (source, target) - here dep is source, ms is target
                    strength = self.dependencies_info.get((dep, ms), {}).get("strength", "Strong")

                    if strength.lower() == "weak":
                        # Weak dependency handling
                        if self.ignore_weak_dependencies:
                            # ignore_weak_dependencies=True: ignore failure, just need FINISHED (completed or failed)
                            if dep not in self._completed_milestones and dep not in self._failed_milestones:
                                all_deps_met = False
                                break
                        else:
                            # ignore_weak_dependencies=False: weak dep must be COMPLETED (like strong dep)
                            if dep not in self._completed_milestones:
                                all_deps_met = False
                                break
                    else:
                        # Strong dependency (default): Dep must be COMPLETED
                        if dep not in self._completed_milestones:
                            all_deps_met = False
                            break

                if all_deps_met:
                    runnable.append(ms)

            # Sort for determinism
            return sorted(runnable)

    def mark_complete(self, milestone_id: str):
        """Mark a milestone as successfully completed.

        Thread-safe: Uses internal lock.
        """
        with self._lock:
            if milestone_id in self.all_milestones:
                self._completed_milestones.add(milestone_id)
                self._submitted_milestones.discard(milestone_id)  # Remove from submitted
                logger.info(f"Marked {milestone_id} as COMPLETED")
            else:
                logger.warning(f"Attempted to mark unknown milestone {milestone_id} as complete")

    def mark_failed(self, milestone_id: str):
        """Mark a milestone as failed. This may block dependent milestones.

        Thread-safe: Uses internal lock (RLock allows reentrant call to _update_skipped).
        """
        with self._lock:
            if milestone_id in self.all_milestones:
                self._failed_milestones.add(milestone_id)
                self._submitted_milestones.discard(milestone_id)  # Remove from submitted
                logger.info(f"Marked {milestone_id} as FAILED")
                self._update_skipped()
            else:
                logger.warning(f"Attempted to mark unknown milestone {milestone_id} as failed")

    def mark_submitted(self, milestone_id: str):
        """Mark a milestone as submitted (awaiting evaluation).

        This removes the milestone from the task queue immediately,
        so the agent doesn't see it anymore and won't try to resubmit.

        Thread-safe: Uses internal lock.
        """
        with self._lock:
            if milestone_id in self.all_milestones:
                self._submitted_milestones.add(milestone_id)
                logger.info(f"Marked {milestone_id} as SUBMITTED (removed from task queue)")
            else:
                logger.warning(f"Attempted to mark unknown milestone {milestone_id} as submitted")

    def restore_state(
        self,
        completed: set,
        failed: set,
        skipped: set,
        submitted: Optional[set] = None,
    ) -> None:
        """Restore DAG state from persisted data.

        Used for resuming interrupted trials. Only restores milestones that
        are part of the current DAG (intersection with all_milestones).

        Thread-safe: Uses internal lock.

        Args:
            completed: Set of completed milestone IDs
            failed: Set of failed milestone IDs
            skipped: Set of skipped milestone IDs
            submitted: Optional set of submitted milestone IDs
        """
        with self._lock:
            # Only restore milestones that exist in current DAG
            completed_set = completed & self.all_milestones
            failed_set = failed & self.all_milestones
            skipped_set = skipped & self.all_milestones
            submitted_set = (submitted & self.all_milestones) if submitted else set()

            # Ensure terminal states take precedence
            submitted_set = submitted_set - (completed_set | failed_set | skipped_set)

            self._completed_milestones = completed_set
            self._failed_milestones = failed_set
            self._skipped_milestones = skipped_set
            self._submitted_milestones = submitted_set

            # Log any milestones that were ignored
            ignored = (completed | failed | skipped | (submitted or set())) - self.all_milestones
            if ignored:
                logger.warning(f"Ignoring {len(ignored)} unknown milestones from restore: {sorted(ignored)}")

            logger.info(f"Restored DAG state:")
            logger.info(f"  Completed: {len(self._completed_milestones)}")
            logger.info(f"  Failed: {len(self._failed_milestones)}")
            logger.info(f"  Skipped: {len(self._skipped_milestones)}")
            logger.info(f"  Submitted: {len(self._submitted_milestones)}")

    def _update_skipped(self):
        """Identify and mark milestones that can never run due to failures.

        CSV format: source_id, target_id means target depends on source.
        When a source fails:
        - Strong dependency: target is ALWAYS skipped
        - Weak dependency: target is skipped ONLY if ignore_weak_dependencies=False

        Note: This method assumes the caller already holds self._lock (called from mark_failed).
        """
        # BFS from failed nodes to mark downstream (targets) as skipped
        queue = list(self._failed_milestones | self._skipped_milestones)
        visited = set(queue)

        while queue:
            current = queue.pop(0)
            # Find all targets that depend on current (current is source)
            # adj_list[current] = targets that depend on current
            dependents = self.adj_list.get(current, [])
            for ds in dependents:
                if ds in self._skipped_milestones or ds in self._failed_milestones or ds in self._completed_milestones:
                    continue

                if ds in visited:
                    continue

                # Check dependency strength
                # Key is (source, target) - current is source, ds is target
                strength = self.dependencies_info.get((current, ds), {}).get("strength", "Strong")
                is_weak = strength.lower() == "weak"

                # Determine if we should skip this dependent
                should_skip = False
                if not is_weak:
                    # Strong dependency: always skip
                    should_skip = True
                    reason = f"Strong dependency {current} failed"
                elif not self.ignore_weak_dependencies:
                    # Weak dependency + ignore_weak=False: also skip
                    should_skip = True
                    reason = f"Weak dependency {current} failed (ignore_weak=False)"

                if should_skip:
                    self._skipped_milestones.add(ds)
                    visited.add(ds)
                    queue.append(ds)
                    logger.info(f"Marked {ds} as SKIPPED ({reason})")

    def is_done(self) -> bool:
        """Check if all milestones are in a terminal state (completed, failed, or skipped).

        Thread-safe: Uses internal lock.
        """
        with self._lock:
            processed = self._completed_milestones | self._failed_milestones | self._skipped_milestones
            return processed == self.all_milestones

    def get_state_snapshot(self) -> dict:
        """Get a consistent snapshot of all DAG state.

        Thread-safe: Returns copies of all state sets in a single lock acquisition.
        Use this when you need to read multiple state variables consistently.

        Returns:
            Dict with keys: completed, failed, skipped, submitted (all are set copies)
        """
        with self._lock:
            return {
                "completed": set(self._completed_milestones),
                "failed": set(self._failed_milestones),
                "skipped": set(self._skipped_milestones),
                "submitted": set(self._submitted_milestones),
            }
