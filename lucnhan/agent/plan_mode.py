"""Plan mode: generate a plan, get user approval, execute step by step."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    index: int
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    result: str = ""


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    approved: bool = False
    current_step: int = 0

    @property
    def is_complete(self) -> bool:
        return self.current_step >= len(self.steps)

    def get_current_step(self) -> PlanStep | None:
        if self.current_step < len(self.steps):
            return self.steps[self.current_step]
        return None

    def advance(self) -> None:
        if self.current_step < len(self.steps):
            self.current_step += 1


def parse_plan(text: str) -> list[str]:
    """Parse a numbered plan from LLM output."""
    steps = []
    # Match lines like "1. ...", "2. ...", etc.
    for line in text.split("\n"):
        line = line.strip()
        m = re.match(r"^\d+[\.\)]\s*(.+)$", line)
        if m:
            steps.append(m.group(1).strip())
        if line.upper() == "END_PLAN":
            break
    return steps


def create_plan_from_text(text: str) -> Plan:
    """Parse LLM text into a Plan object."""
    step_texts = parse_plan(text)
    steps = [PlanStep(index=i, description=desc) for i, desc in enumerate(step_texts)]
    return Plan(steps=steps)
