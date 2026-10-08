"""Theme subsystem: ThemeMode enum, ThemeTokens dataclass, and ThemeManager singleton.

Public entry points:
- ThemeManager.instance() -> ThemeManager
- ThemeMode (enum)
- ThemeTokens (frozen dataclass)
- is_dark_tokens(tokens) -> bool
- blend_hex(h1, h2, t) -> str
- hex_luminance(hex) -> float
"""
