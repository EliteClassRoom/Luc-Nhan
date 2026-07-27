"""OrchestraMainAgent — AOrchestra-style orchestrator with delegate_task tools."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator
from typing import Any

from ...core.config import RikuganConfig
from ...core.logging import log_debug, log_error, log_info
from ...core.types import Message, Role
from ...providers.base import LLMProvider
from ...skills.registry import SkillRegistry
from ...state.session import SessionState
from ...tools.registry import ToolRegistry
from ..subagent_manager import SubagentManager
from ..system_prompt import build_system_prompt
from ..turn import TurnEvent, TurnEventType
from .context import build_subagent_context
from .orchestra_config import OrchestraConfig, SubAgentSpec
from .prompts import (
    ORCHESTRA_BASE_PROMPT,
    ORCHESTRA_IDA_PROMPT,
    build_available_tools_list,
    build_pricing_table,
)
from .subagent_factory import SubAgentFactory
from .tools import COMPLETE_SCHEMA, SUBMIT_SCHEMA


class OrchestraMainAgent:
    """Orchestra-style orchestrator that delegates tasks to specialized sub-agents.

    This agent uses a four-tuple φ = ⟨I, C, T, M⟩ approach:
    - I (Instruction): What to do
    - C (Context): Relevant context from the main task
    - T (Tools): Which tools to make available
    - M (Model): Which model to use

    It provides orchestration tools (delegate_task, submit, complete) and
    manages the lifecycle of sub-agents.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        config: RikuganConfig,
        session: SessionState,
        orchestra_config: OrchestraConfig,
        skill_registry: SkillRegistry | None = None,
        host_name: str = "IDA Pro",
        parent_loop: Any = None,
    ) -> None:
        self.provider = provider
        self.tools = tool_registry
        self.config = config
        self.session = session
        self.orchestra_config = orchestra_config
        self.skills = skill_registry
        self.host_name = host_name
        self._cancelled = threading.Event()
        self._running = False

        self._subagent_manager = SubagentManager(
            provider=provider,
            tool_registry=tool_registry,
            config=config,
            host_name=host_name,
            skill_registry=skill_registry,
        )

        self._factory = SubAgentFactory(
            manager=self._subagent_manager,
            config=orchestra_config,
        )

        self._approval_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._user_answer_queue: queue.Queue[str] = queue.Queue(maxsize=1)

        self._delegation_count = 0
        self._subtask_history: list[dict[str, Any]] = []

        self._pending_delegate_id: str | None = None
        self._pending_delegate_spec: dict[str, Any] | None = None

        # Share the approval queue with the parent AgentLoop so the UI can
        # route orchestra approvals through the same channel as tool approvals.
        self._approval_queue = parent_loop.get_approval_queue() if parent_loop else queue.Queue(maxsize=1)

    def cancel(self) -> None:
        """Cancel the current run."""
        self._cancelled.set()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            from ...core.errors import CancellationError

            raise CancellationError("Orchestra run cancelled")

    def submit_approval(self, decision: str) -> None:
        """Submit approval decision: 'approve', 'deny'."""
        self._approval_queue.put(decision)

    def poll_subagent_event(self) -> TurnEvent | None:
        """Poll for sub-agent events."""
        return self._subagent_manager.poll_event()

    def running_count(self) -> int:
        """Number of running sub-agents."""
        return self._subagent_manager.running_count()

    def completed_count(self) -> int:
        """Number of completed sub-agents."""
        return self._subagent_manager.completed_count()

    def get_delegation_tree(self) -> list:
        """Get the delegation tree for UI display."""
        return self._subagent_manager.tree()

    def _build_system_prompt(self) -> str:
        """Build the orchestra system prompt."""
        profile = self.config.get_active_profile()
        binary_info = None
        current_address = None
        current_function = None

        if self.config.auto_context and not profile.hide_binary_metadata:
            try:
                binary_info = self.tools.execute("get_binary_info", {})
            except Exception as exc:
                log_debug(f"Orchestra auto_context get_binary_info failed: {exc}")
            try:
                current_address = self.tools.execute("get_cursor_position", {})
                current_function = self.tools.execute("get_current_function", {})
            except Exception as exc:
                log_debug(f"Orchestra auto_context cursor/function failed: {exc}")

        base_prompt = build_system_prompt(
            host_name=self.host_name,
            binary_info=binary_info,
            current_function=current_function,
            current_address=current_address,
            tool_names=self.tools.list_names(),
            skill_summary=self.skills.get_summary_for_prompt() if self.skills else None,
            profile=profile,
            hide_strings=bool(getattr(self.config, "hide_strings", False)),
        )

        pricing_table = build_pricing_table(self.orchestra_config.model_pricing)
        tools_list = build_available_tools_list(self.orchestra_config.default_tools)

        history_lines: list[str] = []
        for entry in self._subtask_history[-10:]:
            name = entry.get("name", "?")
            status = entry.get("status", "")
            result = entry.get("result", "")[:200]
            history_lines.append(f"- **{name}** ({status}): {result}")

        history_str = "\n".join(history_lines) if history_lines else "No subtasks completed yet."

        orchestra_prompt = ORCHESTRA_BASE_PROMPT.format(
            pricing_table=pricing_table,
            available_tools_list=tools_list,
            subtask_history=history_str,
        )

        if self.host_name == "IDA Pro":
            orchestra_prompt += ORCHESTRA_IDA_PROMPT

        return base_prompt + "\n\n" + orchestra_prompt

    def _get_tools_schema(self) -> list:
        """Get the tool schema including orchestra orchestration tools."""
        base_schema = list(self.tools.to_provider_format())

        # Build model enum dynamically from orchestra config to avoid
        # hardcoding model names in the schema.
        model_enum = self.orchestra_config.sub_models or []

        delegate_schema = {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": (
                    "Delegate a subtask to a specialized sub-agent. "
                    "The sub-agent will be created with the four-tuple φ = <I, C, T, M>: "
                    "- I: Your instruction describing the task "
                    "- C: Context you provide with relevant binary information "
                    "- T: Tools you specify from the available tool list "
                    "- M: Model you select for the sub-agent "
                    "Optionally set 'mode' to run the sub-agent in a specific mode "
                    "(exploration, plan, research) for structured workflows. "
                    "This tool requires user approval before the sub-agent can be spawned."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Brief name for this subtask (displayed in UI).",
                        },
                        "instruction": {
                            "type": "string",
                            "description": "Detailed instruction for the sub-agent explaining what to do.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Relevant context from the main task (binary info, position, etc.).",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of tool names to make available to the sub-agent.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Model to use for this sub-agent.",
                            "enum": model_enum,
                        },
                        "max_steps": {
                            "type": "integer",
                            "description": "Maximum turns for the sub-agent (default: 20).",
                            "default": 20,
                        },
                        "mode": {
                            "type": "string",
                            "description": (
                                "Mode to run the sub-agent in. "
                                "Valid values: 'exploration' or 'explore' (autonomous read-only investigation), "
                                "'plan' (generate plan, get approval, execute steps), "
                                "'research' (exploration + write markdown notes), "
                                "'normal' or '' (standard agent loop). "
                                "Defaults to 'normal' if not specified."
                            ),
                            "enum": ["exploration", "explore", "plan", "research", "normal", ""],
                            "default": "",
                        },
                    },
                    "required": ["task", "instruction", "tools", "model"],
                },
            },
        }

        orchestra_tools = [
            delegate_schema,
            SUBMIT_SCHEMA,
            COMPLETE_SCHEMA,
        ]

        profile = self.config.get_active_profile()
        if profile.denied_tools:
            denied = set(profile.denied_tools)
            base_schema = [t for t in base_schema if t.get("function", {}).get("name") not in denied]
        # Config: hide string-list/search tools when analysis strings are disabled
        if getattr(self.config, "hide_strings", False):
            _hidden = {"list_strings", "search_strings"}
            base_schema = [t for t in base_schema if t.get("function", {}).get("name") not in _hidden]
        seen: set = set()
        deduped: list = []
        for t in base_schema + orchestra_tools:
            name = t.get("function", t).get("name", "")
            if name and name not in seen:
                seen.add(name)
                deduped.append(t)

        return deduped

    @staticmethod
    def _is_error_result(result: str) -> bool:
        """Detect error results from orchestra tool handlers.

        All orchestra handlers signal errors by returning strings prefixed
        with 'Error:' or 'Delegation denied'. The dispatch loop uses this
        single helper to convert a result string into an is_error flag for
        TurnEvent.tool_result_event.
        """
        return result.startswith("Error:") or result.startswith("Delegation denied")

    def _handle_delegate_task(self, tc_id: str, args: dict[str, Any]) -> Generator[TurnEvent, None, str]:
        """Handle delegate_task tool call — requires user approval."""
        task = args.get("task", "")
        instruction = args.get("instruction", "")
        context = args.get("context", "")
        tools = args.get("tools", [])
        model = args.get("model", "")
        max_steps = args.get("max_steps", 20)
        mode = args.get("mode", "")

        if not instruction:
            return "Error: 'instruction' is required for delegate_task."
        if not task:
            return "Error: 'task' is required for delegate_task."

        if self._delegation_count >= self.orchestra_config.max_delegations:
            return f"Error: Maximum delegations ({self.orchestra_config.max_delegations}) reached."

        self._pending_delegate_id = tc_id
        self._pending_delegate_spec = {
            "task": task,
            "instruction": instruction,
            "context": context,
            "tools": tools,
            "model": model,
            "max_steps": max_steps,
            "mode": mode,
        }

        yield TurnEvent(
            type=TurnEventType.USER_QUESTION,
            tool_call_id=tc_id,
            text=f"Sub-agent delegation request: {task}",
            metadata={
                "options": ["approve", "deny"],
                "allow_text": False,
                "orchestra_delegate": True,
                "delegate_spec": self._pending_delegate_spec,
            },
        )

        self._check_cancelled()

        try:
            decision = self._approval_queue.get(timeout=300)
        except queue.Empty:
            from ...core.errors import CancellationError

            raise CancellationError("Orchestra approval timeout (user did not respond)") from None

        self._pending_delegate_id = None
        self._pending_delegate_spec = None

        if decision.lower() != "approve":
            self._delegation_count += 1
            return f"Delegation denied by user: {task}"

        context_to_pass = build_subagent_context(
            main_context=context,
            subtask_history=self._subtask_history,
            max_chars=self.orchestra_config.context_window,
            enable_sharing=self.orchestra_config.enable_context_sharing,
        )

        complete_task = f"{instruction}\n\n## Context\n{context_to_pass}"

        agent_id = self._factory.spawn(
            SubAgentSpec(
                instruction=complete_task,
                tools=tools,
                model=model,
                max_steps=max_steps,
                name=task,
                mode=mode,
            )
        )

        mode_info = f", mode={mode}" if mode else ""
        self._delegation_count += 1
        self._subtask_history.append(
            {
                "agent_id": agent_id,
                "name": task,
                "status": "running",
                "result": "",
            }
        )

        return f"Delegation approved: {task} (agent_id: {agent_id[:12]}{mode_info})"

    def _handle_submit(self, tc_id: str, args: dict[str, Any]) -> str:
        """Handle submit tool call."""
        self._check_cancelled()
        reasoning = args.get("reasoning", "")
        result = args.get("result", "")

        if not reasoning:
            return "Error: 'reasoning' is required for submit."

        output = f"## Final Result\n\n{reasoning}"
        if result:
            output += f"\n\n## Answer\n\n{result}"

        return output

    def _handle_complete(self, tc_id: str, args: dict[str, Any]) -> str:
        """Handle complete tool call."""
        self._check_cancelled()
        answer = args.get("answer", "")

        if not answer:
            return "Error: 'answer' is required for complete."

        return f"## Answer\n\n{answer}"

    def run(self, user_message: str) -> Generator[TurnEvent, None, None]:
        """Run the orchestra orchestrator. Yields TurnEvents."""
        self._cancelled.clear()
        self._running = True
        self.session.is_running = True

        try:
            self.session.add_message(Message(role=Role.USER, content=user_message))
            system_prompt = self._build_system_prompt()
            tools_schema = self._get_tools_schema()

            log_info(f"Orchestra started: {len(tools_schema)} tools")

            yield TurnEvent.turn_start(turn_number=1)

            full_response = ""
            tool_calls_batch: list[dict[str, Any]] = []

            stream = self.provider.chat_stream(
                messages=self.session.get_messages_for_provider(context_window=0),
                tools=tools_schema,
                temperature=self.config.provider.temperature,
                max_tokens=self.config.provider.max_tokens,
                system=system_prompt,
            )

            current_tool_call: dict[str, Any] | None = None
            current_tool_args = ""

            for chunk in stream:
                self._check_cancelled()

                if chunk.text:
                    full_response += chunk.text
                    yield TurnEvent.text_delta(chunk.text)

                if chunk.is_tool_call_start and chunk.tool_call_id:
                    current_tool_call = {
                        "id": chunk.tool_call_id,
                        "name": chunk.tool_name or "",
                        "arguments": "",
                    }
                    current_tool_args = ""
                    yield TurnEvent.tool_call_start(chunk.tool_call_id, chunk.tool_name or "")

                if chunk.tool_args_delta and current_tool_call:
                    current_tool_args += chunk.tool_args_delta
                    yield TurnEvent.tool_call_args_delta(current_tool_call["id"], chunk.tool_args_delta)

                if chunk.is_tool_call_end and current_tool_call:
                    try:
                        parsed_args = json.loads(current_tool_args) if current_tool_args else {}
                    except json.JSONDecodeError:
                        parsed_args = {}
                    current_tool_call["arguments"] = parsed_args
                    tool_calls_batch.append(current_tool_call)
                    yield TurnEvent.tool_call_done(
                        current_tool_call["id"],
                        current_tool_call["name"],
                        current_tool_args,
                    )
                    current_tool_call = None

            if full_response:
                yield TurnEvent.text_done(full_response)

            yield TurnEvent.turn_end(turn_number=1)

            for tc in tool_calls_batch:
                self._check_cancelled()
                tool_name = tc["name"]
                tool_args = tc["arguments"]

                if tool_name == "delegate_task":
                    result = yield from self._handle_delegate_task(tc["id"], tool_args)
                    is_error = self._is_error_result(result)
                    yield TurnEvent.tool_result_event(tc["id"], tool_name, result, is_error)

                elif tool_name == "submit":
                    result = self._handle_submit(tc["id"], tool_args)
                    is_error = self._is_error_result(result)
                    yield TurnEvent.tool_result_event(tc["id"], tool_name, result, is_error)

                elif tool_name == "complete":
                    result = self._handle_complete(tc["id"], tool_args)
                    is_error = self._is_error_result(result)
                    yield TurnEvent.tool_result_event(tc["id"], tool_name, result, is_error)

                elif tool_name in self.tools.list_names():
                    try:
                        result = self.tools.execute(tool_name, tool_args)
                        is_error = False
                    except Exception as e:
                        result = f"Error: {e}"
                        is_error = True
                        log_error(f"Orchestra tool execution error: {tool_name}: {e}")
                    yield TurnEvent.tool_result_event(tc["id"], tool_name, result, is_error)

                else:
                    result = f"Error: Unknown orchestration tool: {tool_name}"
                    yield TurnEvent.tool_result_event(tc["id"], tool_name, result, True)

            # Drain all subagent events until no agents are running and the
            # queue is empty. Use a short timeout on the blocking get so we
            # don't deadlock if the last event was already drained by another
            # consumer; the running_count() check is the real loop guard.
            while self._subagent_manager.running_count() > 0:
                self._check_cancelled()
                event = self._subagent_manager.wait_event(timeout=0.1)
                if event is None:
                    continue
                yield event
                if event.type == TurnEventType.SUBAGENT_COMPLETED:
                    for entry in self._subtask_history:
                        if entry.get("agent_id") == event.metadata.get("agent_id"):
                            entry["status"] = "completed"
                            entry["result"] = event.text
                            break
            # Final drain: any events queued after the last agent finished.
            while True:
                event = self._subagent_manager.poll_event()
                if event is None:
                    break
                yield event
                if event.type == TurnEventType.SUBAGENT_COMPLETED:
                    for entry in self._subtask_history:
                        if entry.get("agent_id") == event.metadata.get("agent_id"):
                            entry["status"] = "completed"
                            entry["result"] = event.text
                            break

        except Exception as e:
            log_error(f"Orchestra error: {e}")
            yield TurnEvent.error_event(str(e))
        finally:
            self._running = False
            self.session.is_running = False
