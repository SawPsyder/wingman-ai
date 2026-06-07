"""Concurrency stress test for services.persistent_memory.PersistentMemoryService.

No pytest. Run from the project root:

    venv/bin/python -m tests.test_persistent_memory_concurrency

Exits non-zero on the first failed assertion or crash; prints "ALL OK" on success.

Why this exists
---------------
The service opens its SQLite connection with ``check_same_thread=False`` and the
async API fans every operation out onto the default thread pool via
``asyncio.to_thread``. A single ``sqlite3.Connection`` is NOT safe for concurrent
use across threads: two threads inside ``execute()``/``commit()`` at the same time
corrupt SQLite's internal heap and the process dies with a native SIGSEGV
(EXC_BAD_ACCESS) -- no Python traceback, no graceful shutdown, just a stray
"leaked semaphore" warning from the multiprocessing resource_tracker.

Against the UNFIXED code this stress loop reliably segfaults or raises sqlite3
errors ("database is locked", "Recursive use of cursors not allowed", etc.).
With per-instance locking it runs clean and the row count stays consistent.
"""

import os
import tempfile
import threading

import services.persistent_memory as pm
from services.persistent_memory import PersistentMemoryService, EMBEDDING_DIMENSIONS


class _FakeLocalAI:
    """Minimal stand-in: deterministic, cheap embeddings, no model needed.

    Each content string starts with a unique integer index ("<idx>: ..."). We
    return a one-hot vector at that index, so every fact is orthogonal to every
    other (cosine 0) and the service's fact-dedup (threshold 0.9) never collapses
    them -- the final row count is then an exact, order-independent invariant.
    """

    def embed(self, texts):
        out = []
        for text in texts:
            idx = int(text.split(":", 1)[0])
            vec = [0.0] * EMBEDDING_DIMENSIONS
            vec[idx % EMBEDDING_DIMENSIONS] = 1.0
            out.append(vec)
        return out


def check_concurrent_access() -> None:
    threads_count = 16
    iterations = 40

    tmp_dir = tempfile.mkdtemp(prefix="wingman_mem_test_")
    # Redirect the service's db directory to our temp dir.
    pm.get_persistent_memory_dir = lambda: tmp_dir

    service = PersistentMemoryService("StressTester", _FakeLocalAI())
    service.initialize()

    errors: list[BaseException] = []
    barrier = threading.Barrier(threads_count)

    def worker(worker_id: int) -> None:
        # Maximise the chance of overlapping execute()/commit() on the shared
        # connection by releasing all threads at the same instant.
        barrier.wait()
        try:
            for i in range(iterations):
                # Globally unique index -> orthogonal embedding -> never deduped.
                unique_idx = worker_id * iterations + i
                service.add_memory_sync(
                    entry_type="fact",
                    content=f"{unique_idx}: worker {worker_id} fact {i}",
                )
                service.search_sync("0: probe query", limit=5, entry_type="fact")
                service.get_all(entry_type="fact")
        except BaseException as exc:  # noqa: BLE001 - we want everything
            errors.append(exc)

    workers = [
        threading.Thread(target=worker, args=(w,)) for w in range(threads_count)
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert not errors, f"Concurrent access raised: {errors[:3]}"

    # Every add was a unique fact (dedup can't collapse them), so the row count
    # must be exactly threads_count * iterations -- proof no writes were lost or
    # double-counted by a corrupted/raced transaction.
    rows = service.get_all(entry_type="fact")
    expected = threads_count * iterations
    assert len(rows) == expected, f"Expected {expected} rows, got {len(rows)}"

    service.close()
    # Best-effort cleanup of the temp db.
    try:
        for name in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, name))
        os.rmdir(tmp_dir)
    except OSError:
        pass


def main() -> None:
    check_concurrent_access()
    print("ALL OK")


if __name__ == "__main__":
    main()
