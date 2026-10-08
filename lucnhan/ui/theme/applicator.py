"""Theme binding helper for Luc Nhan widgets.

The previous design relied on every widget subclass remembering to
write the same boilerplate::

    self._apply_styles()
    ThemeManager.instance().themeChanged.connect(self._apply_styles)

…and to mirror the disconnects in ``shutdown()`` / ``closeEvent()``
to keep panel teardown from printing Shiboken ``RuntimeWarning``
``Failed to disconnect`` messages.  The helper centralises both sides
of that contract so widget code only declares *what* should run on a
theme change, not *how* it gets wired to the singleton.

Usage::

    from .theme.applicator import bind_theme, disconnect_theme

    class MyWidget(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            ...
            # Run ``self._apply_styles()`` now, and again on every
            # ``themeChanged`` emit.
            bind_theme(self, self._apply_styles)

        def shutdown(self) -> None:
            # Idempotent: tolerates teardown races.
            disconnect_theme(self)

Implementation notes
-------------------
* The connected callable is stashed on the widget as
  ``widget._theme_apply_callback`` so it cannot be garbage-collected
  out from under Qt (the previous inline pattern stored a bound method
  reference too, but adding the attribute makes the lookup uniform
  for ``disconnect_theme`` and survives PySide6's ``connect`` slot
  caching).
* Apply callbacks must accept the emitted tokens argument. Qt's
  ``Signal`` always passes one positional argument even when the
  signal is declared with no parameters, so widget methods that
  take no args will miscount.  The helper detects zero-arg callables
  and wraps them so they continue to work without forcing every
  caller to remember the underscore-prefix convention.
* Disconnect swallows the same disconnect-time errors that
  ``panel_core.shutdown`` swallows (``RuntimeError``, ``TypeError``,
  ``SystemError``, ``ImportError``) and silences the ``RuntimeWarning``
  PySide6 emits when the slot was never connected.
"""

from __future__ import annotations

# ``inspect.signature`` is cheap but importing it eagerly pulls a lot
# of stdlib; we only need a positional-arity check, so ``getattr`` on
# the function's ``__code__`` (Python implementation detail but
# stable since 3.0) keeps the helper dependency-light.
import inspect
from collections.abc import Callable
from typing import Any


def _accepts_tokens_arg(fn: Callable[..., Any]) -> bool:
    """True when ``fn`` can be called with one positional argument.

    We deliberately ignore keyword-only parameters — Qt always emits
    its single token as a positional argument, so a callback that
    requires the argument as a keyword would be unusable here.

    ``*args`` (VAR_POSITIONAL) does count — it absorbs the emitted
    token.  A function that is genuinely zero-argument, however,
    does *not* absorb the token and must be wrapped so the
    signature is compatible with Qt's ``emit(tokens)`` call.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Builtins / C-impls — assume the user knew what they were
        # doing and accept one arg.
        return True
    has_var_positional = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_var_positional:
        return True
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 1


def _wrap_for_emit(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``fn`` unchanged if it accepts the emitted token; else
    wrap so the token is silently dropped.

    Qt emits ``themeChanged(tokens)`` with exactly one positional
    argument. Zero-arg apply callbacks (e.g. widget methods written
    before the token-aware convention) would otherwise miscount and
    raise ``TypeError`` on every emit.
    """
    if _accepts_tokens_arg(fn):
        return fn

    def _wrapper(*_args: Any, **_kwargs: Any) -> Any:
        return fn()

    _wrapper.__name__ = getattr(fn, "__name__", "theme_apply_callback")
    return _wrapper


_UNBOUND = object()  # sentinel: no bound apply callback yet


def _has_bound_callback(widget: Any) -> bool:
    """Return True when ``widget`` was previously passed to ``bind_theme``.

    Uses ``vars(widget)`` so MagicMock-style auto-attribute classes
    (which would otherwise return a truthy sentinel for *any*
    attribute access) are correctly reported as unbound.
    """
    try:
        return "_theme_apply_callback" in vars(widget)
    except TypeError:
        # ``vars()`` raises on objects without ``__dict__`` (e.g.
        # some builtins).  Fall back to a sentinel-guarded
        # ``getattr`` — the sentinel is unique to this module so
        # the comparison is reliable.
        return getattr(widget, "_theme_apply_callback", _UNBOUND) is not _UNBOUND


def bind_theme(widget: Any, apply_fn: Callable[..., Any]) -> Callable[..., Any]:
    """Subscribe ``apply_fn`` to ``ThemeManager.themeChanged`` for ``widget``.

    The same callable is also invoked synchronously, so widget
    construction never paints a stale palette (the helper runs once
    *before* it connects the signal so an early emit — e.g. if the
    user toggles the theme in another panel before this widget's
    first paint — is observed correctly).

    The wrapper returned is stored on ``widget._theme_apply_callback``
    so a later :func:`disconnect_theme` can find the exact same
    callable that was connected (Qt's ``disconnect`` only matches by
    identity, not by ``__qualname__``).
    """
    if widget is None or apply_fn is None:
        return apply_fn
    # Avoid double-bind if the widget calls ``bind_theme`` more than
    # once (e.g. via ``__init__`` + ``showEvent``).  An already-bound
    # widget keeps its original callback so we never end up with
    # duplicate emissions.  ``_has_bound_callback`` uses ``vars``
    # so MagicMock widgets (whose ``getattr`` auto-vivifies
    # attributes) are correctly reported as unbound on first
    # ``bind_theme`` call.
    if _has_bound_callback(widget):
        return widget._theme_apply_callback

    wrapper = _wrap_for_emit(apply_fn)
    # Run once so the widget starts with the active palette.
    try:
        wrapper()
    except Exception:
        # Theme code runs during early UI construction and Qt teardown
        # where raising would destabilize the host — log via the
        # module-level best-effort hook and continue.  Failures are
        # surfaced by the connected slot anyway.
        pass

    try:
        from .manager import ThemeManager

        ThemeManager.instance().themeChanged.connect(wrapper)
    except Exception:
        # Real-PySide6 ``connect`` only fails when the manager is in
        # a degraded state (already-torn-down singleton, etc.).  The
        # wrapper is still recorded so a later
        # :func:`disconnect_theme` is idempotent and doesn't try to
        # disconnect a slot that was never connected.
        pass

    try:
        widget._theme_apply_callback = wrapper
    except Exception:
        # Some Qt classes (notably ``QObject`` subclasses) refuse
        # attribute assignment; we still want to return the wrapper
        # so callers can disconnect through the manager signal.
        pass
    return wrapper


def disconnect_theme(widget: Any) -> None:
    """Disconnect the widget's theme apply callback (idempotent).

    Safe to call from ``shutdown()`` / ``closeEvent()`` /
    ``deleteLater()`` paths — swallows the broad set of
    disconnect-time errors that ``panel_core.shutdown`` already
    handles, and silences the PySide6 ``RuntimeWarning`` that fires
    when the slot was never connected.

    Does not delete ``widget._theme_apply_callback`` — leaving it in
    place keeps a re-bind (e.g. widget re-shown in a dockable form)
    idempotent.  Callers that want to fully drop the reference can
    ``del widget._theme_apply_callback`` themselves after this
    returns.
    """
    callback = getattr(widget, "_theme_apply_callback", None)
    if callback is None:
        return
    try:
        import warnings as _warnings

        from .manager import ThemeManager

        with _warnings.catch_warnings():
            _warnings.filterwarnings(
                "ignore",
                category=RuntimeWarning,
                message=".*Failed to disconnect.*",
            )
            ThemeManager.instance().themeChanged.disconnect(callback)
    except (RuntimeError, TypeError, SystemError, ImportError, AttributeError):
        # Disconnect may legitimately fail when the signal was never
        # connected (the headless fallback path), when the manager
        # was reset under us, or when Shiboken/PySide6 is mid-teardown.
        # None of those are bugs we can fix from here.
        pass


__all__ = ["bind_theme", "disconnect_theme"]
