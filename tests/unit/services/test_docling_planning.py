"""Sizing the Docling process pool, and surviving it being wrong.

The constant these tests defend used to be a guess — `_WORKER_MEMORY_MB = 2048`
against a worker the kernel killed at 8,045 MB — and it was untestable, because
`_default_workers()` read `/proc/meminfo` and `os.cpu_count()` itself. The only
way to exercise the decision was to run it on the machine whose memory it was
misjudging. `plan_conversion` takes those as arguments instead.

The failure that prompted all of this: 32 cores, 64 GB, a 1,224-page book. The
old sizing returned 13 workers, gave each of them 102 pages, and lost the
document ten minutes in.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from research_engine.modules.docling_converter import (
    _MIN_PAGES_PER_TASK,
    _reduce,
    plan_conversion,
)

#: The machine and the book that produced the OOM.
SERVER = {"cpu_count": 32, "available_mb": 59_168}
CAMPAIGNS = 1_224


def plan(total_pages: int = CAMPAIGNS, **kwargs):
    settings = {**SERVER, "per_worker_mb": 8_192, "pages_per_task": 50}
    settings.update(kwargs)
    return plan_conversion(total_pages, **settings)


class TestTaskSizeIsIndependentOfWorkerCount:
    """The property the whole change exists to establish.

    Task size was `ceil(total_pages / num_workers)`, so how much one worker held
    was a property of the document rather than of the configuration, and no
    single `_WORKER_MEMORY_MB` could be right for both a 320-page thesis and a
    1,224-page book. Fixing the task size makes the budget a measurable constant.

    Measured, this buys less than it appears to: peak RSS is flat from 25 pages
    to 200 because cost follows content, not volume. The worker count is the
    lever that moves total memory.
    """

    @pytest.mark.parametrize("available_mb", [12_000, 30_000, 59_168, 512_000])
    def test_pages_per_task_does_not_move_with_available_memory(self, available_mb):
        widths = {end - start + 1 for start, end in plan(available_mb=available_mb).ranges}
        # The last range is short unless the split is exact; every other one is
        # the configured size whatever the machine looks like.
        assert widths <= {50, CAMPAIGNS % 50}

    def test_fewer_workers_means_more_tasks_not_bigger_ones(self):
        roomy = plan(available_mb=512_000)
        cramped = plan(available_mb=12_000)

        assert cramped.workers < roomy.workers
        assert cramped.ranges == roomy.ranges

    def test_the_server_case_no_longer_hands_a_worker_a_hundred_pages(self):
        """The specific arrangement that was killed."""
        result = plan()

        assert result.workers < 13
        assert max(end - start + 1 for start, end in result.ranges) <= 50


class TestWorkerCount:
    def test_memory_is_the_binding_constraint_on_a_big_box(self):
        # 59,168 - 4,096 reserved = 55,072 usable, over 8,192 per worker = 6.
        assert plan().workers == 6

    def test_cores_bind_when_memory_is_plentiful(self):
        """Needs a document with more tasks than cores, or the task cap binds
        first: Campaigns at 50 pages a task is only 25 tasks."""
        assert plan(total_pages=10_000, available_mb=512_000).workers == 30  # 32 - 2

    def test_a_starved_machine_gets_one_worker_not_the_old_floor_of_two(self):
        """`max(2, ...)` guaranteed parallelism on the machine least able to
        afford it. Parallelism is an optimisation; finishing is not."""
        assert plan(available_mb=4_096).workers == 1

    def test_workers_never_exceed_tasks(self):
        """Eight pages is one task; spinning up thirty workers to run it wastes
        ten seconds of model loading apiece."""
        assert plan(total_pages=8, available_mb=512_000).workers == 1

    def test_an_explicit_override_wins_over_the_computed_answer(self):
        result = plan(override=3, available_mb=512_000)

        assert result.workers == 3
        assert "explicit" in result.reason

    def test_an_override_is_still_capped_at_one_worker_per_task(self):
        assert plan(total_pages=60, override=99).workers == 2

    def test_an_absurd_override_cannot_produce_zero_workers(self):
        assert plan(override=0).workers == 1

    def test_the_reason_names_both_constraints(self):
        """It is logged at INFO. The old sizing logged at DEBUG, so during the
        run that ran out of memory none of these numbers were printed."""
        reason = plan().reason

        assert "cores allow" in reason
        assert "memory allows" in reason


class TestRanges:
    @pytest.mark.parametrize("total_pages", [1, 7, 50, 51, 99, 100, 1_224])
    @pytest.mark.parametrize("pages_per_task", [5, 25, 50, 400])
    def test_ranges_tile_the_document_exactly(self, total_pages, pages_per_task):
        """No gap and no overlap, or pages go missing or convert twice."""
        ranges = plan(total_pages=total_pages, pages_per_task=pages_per_task).ranges

        covered = [page for start, end in ranges for page in range(start, end + 1)]
        assert covered == list(range(1, total_pages + 1))

    def test_pages_are_one_indexed(self):
        """Docling's `page_range` is 1-indexed; a 0 start silently shifts every
        page number in the resulting locators."""
        assert plan(total_pages=10, pages_per_task=5).ranges[0][0] == 1

    def test_a_task_size_below_the_floor_is_raised_to_it(self):
        result = plan(total_pages=100, pages_per_task=1)

        assert max(end - start + 1 for start, end in result.ranges) == _MIN_PAGES_PER_TASK


class TestReductionLadder:
    """What the retry does when a worker is killed anyway.

    Concurrency comes down first because it cannot change the output. Task size
    comes down only once there is one worker left, since a different set of page
    ranges is a different set of conversion seams.
    """

    def test_concurrency_halves_before_task_size(self):
        assert _reduce(8, 50) == (4, 50)
        assert _reduce(2, 50) == (1, 50)

    def test_task_size_halves_only_once_one_worker_remains(self):
        assert _reduce(1, 50) == (1, 25)
        assert _reduce(1, 25) == (1, 12)

    def test_it_gives_up_rather_than_retrying_a_doomed_conversion(self):
        """At one worker and the floor task size there is nothing left to give,
        and a corrupt PDF fails identically however small the batch."""
        assert _reduce(1, _MIN_PAGES_PER_TASK) is None

    def test_the_ladder_terminates(self):
        state: tuple[int, int] | None = (30, 400)
        seen = []
        while state is not None:
            seen.append(state)
            state = _reduce(*state)
            assert len(seen) < 50, "ladder does not converge"

        assert seen[-1][0] == 1


class FakePool:
    """Stands in for `_run_pool`, failing the way a killed worker fails.

    A worker reclaimed by the kernel's OOM killer does not raise `MemoryError`
    anywhere the caller can see it. The pool notices its child is gone and raises
    `BrokenProcessPool`, whose message names no cause at all — and it takes every
    still-pending range down with it while the finished ones remain perfectly
    good.
    """

    def __init__(self, *, fail_times: int, survivors: int = 0) -> None:
        self.fail_times = fail_times
        self.survivors = survivors
        self.attempts: list[tuple[int, int]] = []  # (workers, ranges to convert)
        self.reused: list[int] = []  # ranges handed in from the previous attempt

    def __call__(self, source_path, ranges, *, workers, ocr, completed=None):
        from research_engine.modules.docling_converter import PoolBroken

        completed = dict(completed or {})
        todo = [r for r in ranges if r not in completed]
        self.attempts.append((workers, len(todo)))
        self.reused.append(len(completed))

        if len(self.attempts) <= self.fail_times:
            # Some ranges finished before the pool broke; those results survive.
            for page_range in todo[: self.survivors]:
                completed[page_range] = ("text", [], [], 4_096.0)
            raise PoolBroken(completed, RuntimeError("terminated abruptly"))

        for page_range in todo:
            completed[page_range] = ("text", [], [], 4_096.0)
        return completed


@pytest.fixture
def convert(monkeypatch):
    """`_convert_parallel` with the machine probes pinned to the server's shape."""
    from research_engine.modules import docling_converter as dc

    monkeypatch.setattr(dc, "_usable_cpus", lambda: 32)
    monkeypatch.setattr(dc, "_safe_available_memory_mb", lambda: 59_168)
    monkeypatch.setattr(dc, "_WORKER_MEMORY_MB", 8_192)

    def run(pool, *, total_pages=CAMPAIGNS, max_workers=None, pages_per_task=50):
        monkeypatch.setattr(dc, "_run_pool", pool)
        return dc._convert_parallel(
            PurePosixPath("/tmp/campaigns.pdf"),
            ocr=False,
            total_pages=total_pages,
            max_workers=max_workers,
            pages_per_task=pages_per_task,
        )

    return run


class TestRecovery:
    def test_a_killed_worker_costs_precision_not_the_document(self, convert):
        """The behaviour that was missing.

        `_convert_parallel` had no `try` at all, so this exact exception ended a
        1,224-page conversion ten minutes in and reported a message naming no
        cause.
        """
        pool = FakePool(fail_times=1)

        _text, _sections, _pages, report = convert(pool)

        assert report["halvings"] == 1
        assert [workers for workers, _ in pool.attempts] == [6, 3]

    def test_the_retry_keeps_what_already_converted(self, convert):
        """Eleven of twelve workers finished in the run this was written for,
        and every one of their results was discarded."""
        pool = FakePool(fail_times=1, survivors=20)

        convert(pool)

        assert pool.reused == [0, 20]
        assert [pending for _, pending in pool.attempts] == [25, 5]

    def test_shrinking_the_task_size_discards_the_reusable_work(self, convert):
        """A different split is a different set of page ranges, so results keyed
        by the old ones address nothing."""
        pool = FakePool(fail_times=3, survivors=2)

        convert(pool)

        # Reuse grows while only the worker count moves, then resets when the
        # fourth attempt re-splits the document.
        assert pool.reused == [0, 2, 4, 0]

    def test_it_keeps_climbing_down_while_workers_remain(self, convert):
        pool = FakePool(fail_times=3)

        _text, _sections, _pages, report = convert(pool)

        assert [workers for workers, _ in pool.attempts] == [6, 3, 1, 1]
        assert report["workers"] == 1

    def test_task_size_only_shrinks_once_concurrency_is_exhausted(self, convert):
        """Fewer workers cannot change the output; smaller tasks can, because a
        different split is a different set of conversion seams."""
        pool = FakePool(fail_times=3)

        convert(pool)

        pending_at_each_attempt = [pending for _, pending in pool.attempts]
        assert pending_at_each_attempt[:3] == [25, 25, 25]  # 1224 / 50
        assert pending_at_each_attempt[3] == 49  # 1224 / 25, only after 1 worker

    def test_it_gives_up_instead_of_retrying_forever(self, convert):
        """A corrupt PDF fails identically at every size. Retrying it costs a
        full conversion per attempt while looking like progress."""
        from research_engine.modules.docling_converter import PoolBroken

        pool = FakePool(fail_times=99)

        with pytest.raises(PoolBroken):
            convert(pool)

        assert len(pool.attempts) < 15

    def test_a_clean_run_reports_what_it_cost(self, convert):
        """`peak_worker_mb` is the measurement `_WORKER_MEMORY_MB` exists to
        predict. Nothing measured it before, so the constant drifted four times
        off with nothing to notice."""
        _text, _sections, _pages, report = convert(FakePool(fail_times=0))

        assert report["peak_worker_mb"] == 4_096.0
        assert report["halvings"] == 0
        assert report["pages_per_task"] == 50


def _worker_that_dies_on_page_51(path, start, end, *, ocr, device):
    """A worker that vanishes the way an OOM-killed one does.

    `os._exit` skips every handler and every flush, so the parent learns only
    that its child is gone — which is exactly what SIGKILL from the OOM reaper
    looks like, and why the resulting message names no cause.
    """
    import os

    if start == 51:
        os._exit(1)
    return (f"pages {start}-{end}", [], [], 100.0)


def _worker_marking_a_second_pass(path, start, end, *, ocr, device):
    """Distinguishable from the first pass, so a reconversion is visible.

    Module level because the callable itself is pickled to the worker; a nested
    function cannot make the trip.
    """
    return (f"SECOND PASS {start}-{end}", [], [], 100.0)


class TestPoolSurvivesARealDeadWorker:
    """Against a real process pool, not a fake.

    Whether a completed future keeps its result once its executor is poisoned is
    a claim about CPython's behaviour, and the recovery path is built entirely on
    it being true.

    Relies on `fork`, still the default start method on Linux through 3.13, so
    the monkeypatched module attribute is inherited by the children.
    """

    def test_the_ranges_that_finished_are_recoverable(self, monkeypatch):
        from research_engine.modules import docling_converter as dc

        monkeypatch.setattr(dc, "_convert_page_range", _worker_that_dies_on_page_51)
        ranges = [(1, 25), (26, 50), (51, 75), (76, 100)]

        with pytest.raises(dc.PoolBroken) as caught:
            dc._run_pool(PurePosixPath("/tmp/x.pdf"), ranges, workers=1, ocr=False)

        assert (1, 25) in caught.value.completed
        assert (51, 75) not in caught.value.completed

    def test_a_second_attempt_only_converts_what_is_missing(self, monkeypatch):
        """The whole point of carrying the results out of the failure."""
        from research_engine.modules import docling_converter as dc

        monkeypatch.setattr(dc, "_convert_page_range", _worker_that_dies_on_page_51)
        ranges = [(1, 25), (26, 50), (51, 75), (76, 100)]
        try:
            dc._run_pool(PurePosixPath("/tmp/x.pdf"), ranges, workers=1, ocr=False)
        except dc.PoolBroken as broken:
            survived = broken.completed

        monkeypatch.setattr(dc, "_convert_page_range", _worker_marking_a_second_pass)
        results = dc._run_pool(
            PurePosixPath("/tmp/x.pdf"), ranges, workers=1, ocr=False,
            completed=survived,
        )

        assert set(results) == set(ranges)
        # Carried over from the first attempt, not converted again.
        assert results[(1, 25)][0] == "pages 1-25"
        # Never finished the first time, so it is converted now.
        assert results[(51, 75)][0] == "SECOND PASS 51-75"

    def test_assembly_follows_page_order_not_completion_order(self):
        """Results accumulate across retries, so a document assembled in the
        order the dict happens to hold would interleave its own chapters."""
        from research_engine.modules.docling_converter import _assemble

        ranges = [(1, 25), (26, 50), (51, 75)]
        out_of_order = {
            (51, 75): ("third", [], [], 10.0),
            (1, 25): ("first", [], [], 30.0),
            (26, 50): ("second", [], [], 20.0),
        }

        (text, _sections, _pages), peak = _assemble(ranges, out_of_order)

        assert text == "first\n\nsecond\n\nthird"
        assert peak == 30.0


class TestParallelGate:
    """When splitting across processes is worth doing at all."""

    def test_a_pamphlet_converts_in_process(self):
        from research_engine.modules.docling_converter import _wants_parallel

        assert not _wants_parallel(12, 50)

    def test_a_book_is_split(self):
        from research_engine.modules.docling_converter import _wants_parallel

        assert _wants_parallel(CAMPAIGNS, 50)

    def test_a_document_that_makes_only_one_task_is_not_split(self):
        """A pool of one worker running one range is not parallelism.

        It is the single-process path plus a model load, minus the accelerator —
        the parallel path is always CPU, because forked workers cannot share
        VRAM usefully.
        """
        from research_engine.modules.docling_converter import _wants_parallel

        assert not _wants_parallel(30, 50)
        assert not _wants_parallel(50, 50)
        assert _wants_parallel(51, 50)

    def test_a_small_task_size_cannot_lower_the_document_floor(self):
        """The gate is the *larger* of the two conditions.

        An operator who drops the task size to 5 after a memory scare is asking
        for smaller tasks, not for a 15-page pamphlet to be split across three
        processes that each load a model to convert five pages.
        """
        from research_engine.modules.docling_converter import _wants_parallel

        assert not _wants_parallel(15, 5)  # below the floor, whatever the split
        assert _wants_parallel(21, 5)  # clears the floor, and makes 5 tasks


def _report_own_oom_score(path, start, end, *, ocr, device):
    """Reads back what the pool initializer set on this worker."""
    with open("/proc/self/oom_score_adj") as handle:
        return (handle.read().strip(), [], [], 0.0)


class TestOomPreference:
    """Who the kernel reaps decides whether recovery is possible at all.

    The ladder needs a parent alive to run it. Left to its own scoring the OOM
    killer may well choose the parent — it holds the whole document's text — and
    then nothing retries and nothing explains why. Raising the workers' own score
    makes them the preferred victims, which needs no privileges.
    """

    def test_a_worker_is_a_more_attractive_victim_than_its_parent(self, monkeypatch):
        import os

        from research_engine.modules import docling_converter as dc

        with open("/proc/self/oom_score_adj") as handle:
            parent_score = int(handle.read().strip())
        monkeypatch.setattr(dc, "_convert_page_range", _report_own_oom_score)

        results = dc._run_pool(
            PurePosixPath("/tmp/x.pdf"), [(1, 50)], workers=1, ocr=False
        )

        worker_score = int(results[(1, 50)][0])
        assert worker_score == dc._WORKER_OOM_SCORE_ADJ
        assert worker_score > parent_score
        assert os.getpid() and parent_score < 1000  # the parent was left alone

    def test_it_does_not_raise_where_proc_is_unwritable(self, monkeypatch):
        """A sandbox that forbids the write must not cost a conversion."""
        from research_engine.modules import docling_converter as dc

        def forbidden(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("builtins.open", forbidden)
        dc._prefer_killing_this_worker()  # must not raise
