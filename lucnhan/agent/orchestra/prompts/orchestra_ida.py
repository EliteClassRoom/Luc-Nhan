"""IDA-specific Orchestra prompts."""

from __future__ import annotations

ORCHESTRA_IDA_PROMPT = """

## IDA Pro Analysis Context

When analyzing binaries in IDA Pro:

- **Decompiler tools**: `decompile_function`, `get_function_info`, `read_disassembly`
- **Xref tools**: `xrefs_to`, `xrefs_from`, `function_xrefs`
- **Type tools**: `set_type`, `set_function_prototype`, `create_struct`, `create_enum`
- **String tools**: `list_strings`, `search_strings`, `get_string_at`
- **Function tools**: `list_functions`, `search_functions`, `get_function_info`, `rename_function`
- **Annotation tools**: `set_comment`, `set_function_comment`

## Delegation Recommendations

| Task Type | Recommended Tools | Model |
|-----------|------------------|-------|
| String extraction | `list_strings`, `search_strings` | haiku |
| Xref chasing | `xrefs_to`, `xrefs_from`, `function_xrefs` | sonnet |
| Function decompilation | `decompile_function` | sonnet |
| Type analysis | `set_type`, `set_function_prototype` | sonnet |
| Quick rename | `rename_function` | haiku |
| Full analysis | All tools above | sonnet |

## Context Tips

- Pass the current function address and name in context
- Pass relevant xref targets/addresses
- Pass any known string constants or structure names
"""
