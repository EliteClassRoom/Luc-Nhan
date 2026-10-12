"""Host-derived secondary surfaces must remain readable without recoloring Qt."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests import purge_lucnhan_stubs


@pytest.mark.parametrize(
    ("text", "base", "alternate"),
    [
        ("#f8f8f2", "#282a36", "#f5f5f5"),
        ("#202020", "#ffffff", "#282a36"),
        ("#f8f8f2", "#282a36", "#343746"),
        ("#202020", "#ffffff", "#eeeeee"),
    ],
)
def test_host_secondary_surface_remains_readable(text: str, base: str, alternate: str) -> None:
    purge_lucnhan_stubs()
    from lucnhan.ui.theme import palette_ida
    from lucnhan.ui.theme.manager import asdict_fields
    from lucnhan.ui.theme.tokens import hex_luminance

    colors = dict.fromkeys(asdict_fields()[:12], base)
    colors.update(text=text, window_text=text, button_text=text, alt_base=alternate)
    # Only the Qt input boundary is replaced; derivation and contrast math are real.
    with patch.object(palette_ida, "_read_qpalette_colors", return_value=colors.copy()):
        tokens = palette_ida.derive_ida_tokens(None)

    text_lum = hex_luminance(text)
    alternate_lum = hex_luminance(alternate)
    original_contrast = (max(text_lum, alternate_lum) + 0.05) / (min(text_lum, alternate_lum) + 0.05)
    surface_lum = hex_luminance(tokens.alt_base)
    contrast = (max(text_lum, surface_lum) + 0.05) / (min(text_lum, surface_lum) + 0.05)
    assert contrast >= 4.5, f"{text} on {tokens.alt_base} = {contrast:.2f}:1"
    if original_contrast >= 4.5:
        assert tokens.alt_base == alternate, "readable native secondary colors must be preserved"
    assert tokens.text == text and tokens.base == base
