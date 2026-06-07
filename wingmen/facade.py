"""Facade infrastructure shared by the skill-facing WingmanContext.

Houses the single `FacadeError` exception and the `ReadOnlyConfigView` proxy that
lets skills *read* the live Wingman config freely while making any *write* impossible.

Skills that legitimately need to change something use a sanctioned capability
(e.g. ``ctx.tts.set_voice(...)``) instead of mutating config by reference.
"""

from types import MappingProxyType
from typing import Any

from pydantic import BaseModel


class FacadeError(Exception):
    """Raised when a skill tries to do something the facade does not allow.

    The message always names the sanctioned capability to use instead, so a skill
    author gets actionable feedback (we don't gate the catalog on this).
    """


class ReadOnlyConfigView:
    """A recursive, read-only proxy over a pydantic model (e.g. ``WingmanConfig``).

    Attribute reads pass through to the live model — so the view never goes stale —
    and nested models / lists / dicts are wrapped recursively so a skill cannot grab
    a mutable inner object and write through it. Any attempt to *set* an attribute
    raises :class:`FacadeError`.

    Example::

        view = ReadOnlyConfigView(wingman.config)
        view.openai.tts_voice            # reads the live value
        view.features.tts_provider       # nested read, also live
        view.openai.tts_voice = "nova"   # -> FacadeError
    """

    __slots__ = ("_model",)

    def __init__(self, model: BaseModel) -> None:
        # Bypass our own __setattr__ to store the wrapped model.
        object.__setattr__(self, "_model", model)

    # --- reads pass through (recursively wrapped) ---

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally; since the only real
        # slot is `_model`, every config attribute access lands here.
        if name.startswith("__") and name.endswith("__"):
            # Let dunder lookups (e.g. during copy/pickle) fail normally.
            raise AttributeError(name)
        value = getattr(object.__getattribute__(self, "_model"), name)
        return _wrap(value)

    # --- writes are forbidden ---

    def __setattr__(self, name: str, value: Any) -> None:
        raise FacadeError(
            f"Wingman config is read-only for skills — cannot set '{name}'. "
            f"Use the matching facade capability instead (e.g. ctx.tts.set_voice(...), "
            f"ctx.audio.set_output_device(...), ctx.commands.*)."
        )

    def __delattr__(self, name: str) -> None:
        raise FacadeError(
            f"Wingman config is read-only for skills — cannot delete '{name}'."
        )

    # --- ergonomics ---

    def __repr__(self) -> str:
        return f"ReadOnlyConfigView({object.__getattribute__(self, '_model')!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ReadOnlyConfigView):
            other = object.__getattribute__(other, "_model")
        return object.__getattribute__(self, "_model") == other

    def __hash__(self) -> int:
        return id(object.__getattribute__(self, "_model"))


class _ReadOnlyList:
    """Read-only, recursively-wrapping view over a list. Indexing/iteration/len work;
    mutation raises :class:`FacadeError`."""

    __slots__ = ("_items",)

    def __init__(self, items: list) -> None:
        object.__setattr__(self, "_items", items)

    def __getitem__(self, index: Any) -> Any:
        result = object.__getattribute__(self, "_items")[index]
        if isinstance(index, slice):
            return tuple(_wrap(v) for v in result)
        return _wrap(result)

    def __iter__(self):
        return (_wrap(v) for v in object.__getattribute__(self, "_items"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_items"))

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, ReadOnlyConfigView):
            item = object.__getattribute__(item, "_model")
        return item in object.__getattribute__(self, "_items")

    def __setitem__(self, *_: Any) -> None:
        raise FacadeError("This config list is read-only for skills.")

    def __delitem__(self, *_: Any) -> None:
        raise FacadeError("This config list is read-only for skills.")

    def __repr__(self) -> str:
        return f"_ReadOnlyList({object.__getattribute__(self, '_items')!r})"


def _wrap(value: Any) -> Any:
    """Wrap a value so it cannot be mutated through the read-only view."""
    if isinstance(value, BaseModel):
        return ReadOnlyConfigView(value)
    if isinstance(value, list):
        return _ReadOnlyList(value)
    if isinstance(value, dict):
        return MappingProxyType({k: _wrap(v) for k, v in value.items()})
    # Scalars, enums, tuples, None, callables (e.g. model_dump) pass through as-is.
    return value
