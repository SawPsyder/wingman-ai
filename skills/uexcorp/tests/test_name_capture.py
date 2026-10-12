"""Byte-identity gate for the name-resolution refactor.

The frozen file is the result on 6801004. Steps 1-6 must match it,
including notes, derived flags, and System One call counts. The socket
guard must stay at 0.
"""

from __future__ import annotations

import json
import socket

from skills.uexcorp.tests.name_capture import _CAPTURE_PATH, build_capture


def test_name_capture_matches_the_freeze_and_opens_no_sockets():
    connects = {"n": 0}
    original = socket.socket.connect

    def _blocked(self, *args, **kwargs):
        connects["n"] += 1
        raise AssertionError("name capture opened a socket")

    socket.socket.connect = _blocked
    try:
        from skills.uexcorp.uexcorp.naming.cache import bump_pool_version

        cold = build_capture()
        warm = build_capture()
        bump_pool_version()
        again = build_capture()
    finally:
        socket.socket.connect = original
    assert connects["n"] == 0
    frozen = json.loads(_CAPTURE_PATH.read_text(encoding="utf-8"))
    assert cold["queries"] == frozen["queries"]
    assert cold == frozen
    assert warm == frozen
    assert again == frozen
