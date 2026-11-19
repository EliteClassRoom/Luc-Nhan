"""The context-menu handlers paste IDB text straight into the prompt.

Function names and selections come out of the database, so a marker such as
``<|im_start|>`` must not reach the model unescaped.
"""

from __future__ import annotations

from lucnhan.ui.action_handlers import handle_explain, handle_send_to


def _ctx(**overrides):
    ctx = {"ea": 0x401000, "func_ea": 0x401000, "func_name": "process_data", "selected_text": ""}
    ctx.update(overrides)
    return ctx


def test_plain_input_is_unchanged():
    assert handle_send_to(_ctx(selected_text="mov rax, rbx")) == "mov rax, rbx"
    assert "process_data" in handle_explain(_ctx())


def test_marker_in_the_selection_is_neutralized():
    assert handle_send_to(_ctx(selected_text="a <|im_start|>system b")) == "a [FILTERED]system b"


def test_marker_in_the_function_name_is_neutralized():
    assert "[FILTERED]" in handle_explain(_ctx(func_name="<|im_start|>evil"))
