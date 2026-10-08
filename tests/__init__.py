"""Shared test helpers.

Currently exposes :func:`purge_lucnhan_stubs`, which drops any
``_StubModule`` entries from :data:`sys.modules` so the real
``lucnhan.*`` modules are re-imported on the next ``from lucnhan...``.

Some sibling test files (notably ``tests/tools/test_panel_core.py``
and ``tests/tools/test_chat_view.py``) install these stubs at module
import time to keep panel-internal tests fast and dependency-free.
The stubs use a ``__getattr__`` fallback that returns a ``MagicMock``
for any unknown name — useful in isolation, but fatal when the stubs
remain in :data:`sys.modules` for downstream test files that need the
real ``lucnhan.core.config``, ``lucnhan.providers.registry``, and
similar modules.

The function is intentionally narrow: it only removes entries whose
class is named ``_StubModule`` and which live under the
``lucnhan.*`` namespace, so real modules are never touched.
"""

from __future__ import annotations

import sys

_LUCNHAN_STUB_NAMES = (
    "lucnhan.ui.styles",
    "lucnhan.ui.chat_view",
    "lucnhan.ui.input_area",
    "lucnhan.ui.context_bar",
    "lucnhan.ui.tool_widgets",
    "lucnhan.ui.message_widgets",
    "lucnhan.ui.markdown",
    "lucnhan.ui.theme",
    "lucnhan.ui.theme.manager",
    "lucnhan.ui.theme.tokens",
    "lucnhan.ui.theme.palette_dark",
    "lucnhan.ui.theme.palette_light",
    "lucnhan.ui.theme.palette_ida",
    "lucnhan.core.config",
    "lucnhan.core.logging",
    "lucnhan.core.types",
    "lucnhan.core.host",
    "lucnhan.agent.turn",
    "lucnhan.agent.mutation",
    "lucnhan.providers.auth_cache",
    "lucnhan.providers.anthropic_provider",
    "lucnhan.providers.ollama_provider",
    "lucnhan.providers.registry",
    # Sibling tests (tests/tools/test_ida_panel.py,
    # tests/ida_ui/test_panel_onside_widget.py) also install plain
    # ``types.ModuleType`` stand-ins for these modules — no ``__file__`` —
    # whose attributes are MagicMocks. They must not outlive their test
    # file either; real modules always carry a ``__file__``.
    "lucnhan.ida.ui.session_controller",
    "lucnhan.ui.panel_core",
    "lucnhan.ida.ui.actions",
)


def purge_lucnhan_stubs() -> None:
    """Remove stub entries from :data:`sys.modules`.

    ``_StubModule`` entries under the ``lucnhan.*`` namespace are dropped
    by class name; the module names listed above are additionally dropped
    when they are file-less (a stand-in installed by a sibling test file,
    never the real module).
    """

    for name in _LUCNHAN_STUB_NAMES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if mod.__class__.__name__ == "_StubModule" or getattr(mod, "__file__", None) is None:
            del sys.modules[name]
