"""Regression test for the PocketTTS synthesis lock across event loops.

No pytest. Run from the project root:

    venv/bin/python -m tests.test_pocket_tts_event_loop

Exits non-zero on the first failed assertion; prints "ALL OK" on success.

Why this exists
---------------
PocketTTS is a long-lived singleton, but synthesis is awaited from different
event loops over the app's lifetime -- each push-to-talk interaction runs on its
own loop (see the asyncio.new_event_loop() handlers in wingman_core). A plain
asyncio.Lock() created in __init__ binds to the loop it is first used on and
raises "<Lock ...> is bound to a different event loop" when awaited on any other
loop, which broke TTS on the second/third interaction. _gen_lock() returns a
lock bound to the *current* running loop instead.

This test builds a bare PocketTTS (bypassing the heavy model-loading __init__),
drives _gen_lock() under real contention across two separate loops, and asserts
no cross-loop error -- and, as a control, that the old single-lock pattern really
does fail that way.

Note: an asyncio.Lock only binds to a loop on the *contended* path (when a
coroutine actually has to wait), which is why the real failure log shows the
lock as "[locked, waiters:1]". So every round below runs a holder + a waiter to
force that contention -- an uncontended acquire would never reproduce the bug.
"""

import asyncio


def _make_bare_shared():
    """A PocketTTS with only the lock fields initialised (no model load)."""
    from providers.pocket_tts import PocketTTS

    obj = object.__new__(PocketTTS)
    obj._async_gen_lock = None
    obj._async_gen_lock_loop = None
    return obj


async def _contended_round(get_lock) -> None:
    """Run a holder + a waiter so the lock takes its contended (binding) path."""
    held = asyncio.Event()

    async def holder() -> None:
        async with get_lock():
            held.set()
            await asyncio.sleep(0.05)

    async def waiter() -> None:
        await held.wait()
        async with get_lock():  # must wait -> hits _get_loop() -> loop binding
            pass

    await asyncio.gather(holder(), waiter())


def check_lock_works_across_loops() -> None:
    shared = _make_bare_shared()

    # Loop A, then loop B (a brand new loop) -- the exact sequence that crashed.
    asyncio.run(_contended_round(shared._gen_lock))
    lock_a = shared._async_gen_lock
    asyncio.run(_contended_round(shared._gen_lock))  # must NOT raise
    lock_b = shared._async_gen_lock

    assert lock_a is not lock_b, "a new running loop should get a fresh lock"


def check_old_pattern_was_broken() -> None:
    """Control: prove a single shared asyncio.Lock really does fail across loops,
    so the test above is actually exercising the failure mode it claims to."""
    lock = asyncio.Lock()

    asyncio.run(_contended_round(lambda: lock))  # binds the lock to loop A

    raised = False
    try:
        asyncio.run(_contended_round(lambda: lock))  # loop B -> should blow up
    except RuntimeError as exc:
        raised = "different event loop" in str(exc)
    assert raised, "expected the single-lock pattern to fail across loops"


def main() -> None:
    check_old_pattern_was_broken()
    check_lock_works_across_loops()
    print("ALL OK")


if __name__ == "__main__":
    main()
