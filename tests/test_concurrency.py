"""Concurrency and load tests.

These exist specifically to exercise what the unit tests in test_core.py
structurally cannot: real OS threads, genuine thread churn, and a reload
racing against in-flight classifications -- the actual scenarios the
thread-local-cookie design in _core.py is built to survive.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from libmagic import Magic

from .conftest import PDF_BYTES, TEXT_BYTES

if TYPE_CHECKING:
    from pathlib import Path


def test_concurrent_classification_from_many_threads(compiled_mgc: Path) -> None:
    """Many real threads hammering one shared Magic instance concurrently.

    Correctness under concurrency, not just "doesn't crash": every thread
    must get the classification matching what it actually submitted, even
    though all threads share the same Magic object and its _base_flags.
    """
    m = Magic(magic_file=str(compiled_mgc))
    iterations = 200
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(pdf: bool) -> None:
        data = PDF_BYTES if pdf else TEXT_BYTES
        expected = "application/pdf" if pdf else None
        try:
            for _ in range(iterations):
                result = m.from_buffer(data)
                if pdf:
                    assert result.mime_type == expected
                else:
                    assert result.mime_type.startswith("text/")
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert all(not t.is_alive() for t in threads)


def test_high_thread_churn_does_not_crash(compiled_mgc: Path) -> None:
    """Many short-lived threads, each opening its own cookie and exiting.

    This is exactly the pattern that leaked native handles before the
    _Cookie wrapper fix: a thread pool that grows/shrinks under load
    (gunicorn/uwsgi workers, a Celery pool) creates a fresh thread-local
    cookie per new thread. The regression this guards against is a crash
    or hang under that churn, not a memory measurement -- Python has no
    portable way to observe libmagic's internal allocations, but repeated
    open+use+thread-exit at volume is the realistic proxy for "does the
    cleanup path hold up," and it's exactly what caused the real segfault
    caught while building this fix (a bogus cookie reaching a real
    magic_close in a different, mocked test).
    """
    m = Magic(magic_file=str(compiled_mgc))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = m.from_buffer(PDF_BYTES)
            assert result.mime_type == "application/pdf"
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    for _ in range(300):
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)

    assert not errors, errors


def test_thread_pool_executor_sustained_throughput(compiled_mgc: Path) -> None:
    """A ThreadPoolExecutor, the shape real web/task frameworks actually use."""
    m = Magic(magic_file=str(compiled_mgc))
    samples = [PDF_BYTES, TEXT_BYTES] * 250

    def classify(data: bytes) -> str:
        return m.from_buffer(data).mime_type

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(classify, samples))

    assert results[0::2] == ["application/pdf"] * 250
    assert all(r.startswith("text/") for r in results[1::2])


def test_reload_races_against_concurrent_classification(
    compiled_mgc: Path, tmp_path: Path,
) -> None:
    """Atomic .mgc replacement while other threads are actively classifying.

    Proves the documented safety claim in Magic's docstring end-to-end,
    not just via the single-threaded mtime-change unit test in
    test_core.py: a concurrent reload must never corrupt or crash a
    classification in flight on another thread.
    """
    live = tmp_path / "live.mgc"
    live.write_bytes(compiled_mgc.read_bytes())
    m = Magic(magic_file=str(live))

    stop = threading.Event()
    errors: list[BaseException] = []
    lock = threading.Lock()

    def classify_loop() -> None:
        try:
            while not stop.is_set():
                result = m.from_buffer(PDF_BYTES)
                assert result.mime_type == "application/pdf"
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    readers = [threading.Thread(target=classify_loop) for _ in range(4)]
    for t in readers:
        t.start()

    try:
        for _ in range(20):
            tmp = live.with_suffix(".tmp")
            tmp.write_bytes(compiled_mgc.read_bytes())
            tmp.replace(live)  # atomic, matches the README's documented contract
            time.sleep(0.01)
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)

    assert not errors, errors
    assert all(not t.is_alive() for t in readers)
