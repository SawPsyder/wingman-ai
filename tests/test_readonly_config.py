"""Standalone test for ReadOnlyConfigView + FacadeError.

Run: PYTHONPATH=. venv/bin/python tests/test_readonly_config.py
Expected last line: ALL OK
"""

from types import MappingProxyType

from pydantic import BaseModel

from wingmen.facade import FacadeError, ReadOnlyConfigView, _ReadOnlyList


# --- a small nested model hierarchy mirroring the real config shape ---

class Inner(BaseModel):
    voice: str = "alloy"
    streaming: bool = False


class VoiceEntry(BaseModel):
    provider: str
    voice: str


class Outer(BaseModel):
    name: str = "Computer"
    openai: Inner = Inner()
    voices: list[VoiceEntry] = []
    flags: dict = {}


def expect_facade_error(fn, what):
    try:
        fn()
    except FacadeError:
        return
    raise AssertionError(f"expected FacadeError when {what}, but no error was raised")


def main():
    model = Outer(
        openai=Inner(voice="nova", streaming=True),
        voices=[VoiceEntry(provider="openai", voice="echo"),
                VoiceEntry(provider="azure", voice="jane")],
        flags={"x": 1, "nested": Inner(voice="shimmer")},
    )
    view = ReadOnlyConfigView(model)

    # 1. scalar reads pass through
    assert view.name == "Computer", view.name

    # 2. nested model reads pass through (and stay live)
    assert view.openai.voice == "nova"
    assert view.openai.streaming is True
    model.openai.voice = "fable"  # mutate underlying directly
    assert view.openai.voice == "fable", "view should reflect live config, not a snapshot"

    # 3. top-level write raises
    expect_facade_error(lambda: setattr(view, "name", "Hacked"), "setting a top-level field")
    assert model.name == "Computer", "underlying must be untouched"

    # 4. nested write raises (the key hole: config.openai.tts_voice = ...)
    expect_facade_error(lambda: setattr(view.openai, "voice", "Hacked"),
                        "setting a nested field")
    assert model.openai.voice == "fable"

    # 5. delete raises
    expect_facade_error(lambda: delattr(view, "name"), "deleting a field")

    # 6. list is read-only but iterable/indexable
    assert len(view.voices) == 2
    assert view.voices[0].voice == "echo"
    assert [v.provider for v in view.voices] == ["openai", "azure"]
    expect_facade_error(lambda: view.voices.__setitem__(0, VoiceEntry(provider="x", voice="y")),
                        "assigning a list element")
    # 6b. and elements grabbed from the list are themselves read-only
    expect_facade_error(lambda: setattr(view.voices[0], "voice", "Hacked"),
                        "mutating a model fetched from a read-only list")
    assert model.voices[0].voice == "echo"

    # 7. dict becomes a read-only mapping; nested models inside stay wrapped
    assert isinstance(view.flags, MappingProxyType)
    assert view.flags["x"] == 1
    assert isinstance(view.flags["nested"], ReadOnlyConfigView)
    expect_facade_error(lambda: setattr(view.flags["nested"], "voice", "Hacked"),
                        "mutating a model nested inside a dict")
    try:
        view.flags["x"] = 2  # MappingProxyType blocks writes with TypeError
        raise AssertionError("expected the read-only mapping to block writes")
    except TypeError:
        pass

    # 8. methods still callable for reads (model_dump returns a plain copy)
    dumped = view.openai.model_dump()
    assert dumped["voice"] == "fable"
    dumped["voice"] = "whatever"  # mutating the copy must not touch live config
    assert model.openai.voice == "fable"

    # 9. equality compares the underlying model
    assert view == model
    assert view.openai == model.openai

    # 10. deepcopy of a read-only view yields a real MUTABLE detached model copy
    import copy
    snap = copy.deepcopy(view.openai)
    assert isinstance(snap, Inner) and not isinstance(snap, ReadOnlyConfigView)
    snap.voice = "changed"          # must be mutable
    assert snap.voice == "changed"
    assert model.openai.voice == "fable"  # live config untouched

    print("ALL OK")


if __name__ == "__main__":
    main()
