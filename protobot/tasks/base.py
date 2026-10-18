"""Tasks: scripts composed of steps, with retries and a report.

The task layer exists because a single goal cannot express "mine three blocks
and tell me how many worked".  A :class:`Task` is an ordered list of
:class:`Step` objects; a :class:`TaskRunner` gives each step its own retry budget
and records what happened, so a failure names the step that failed instead of
leaving the caller to infer it from a silent stop.

Steps receive a :class:`TaskContext` instead of closing over the bot, which is
what lets the same task run against a live connection and a test double.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..errors import ProtoBotError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..client import Bot

__all__ = [
    "CallStep",
    "GoalStep",
    "Step",
    "StepReport",
    "StepStatus",
    "Task",
    "TaskContext",
    "TaskError",
    "TaskReport",
    "TaskRunner",
]


class TaskError(ProtoBotError):
    """A step cannot be completed; the message says which one and why."""


class StepStatus(str, Enum):
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepReport:
    """Outcome of one step, including its retries."""

    name: str
    status: StepStatus
    elapsed: float
    attempts: int = 1
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is StepStatus.DONE


@dataclass
class TaskContext:
    """What a step is given: the bot, shared scratch values, and a log."""

    bot: Bot
    data: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(message)


@dataclass(frozen=True, slots=True)
class TaskReport:
    """Outcome of a whole task."""

    name: str
    ok: bool
    elapsed: float
    steps: tuple[StepReport, ...] = ()
    detail: str = ""

    @property
    def failed_step(self) -> StepReport | None:
        for step in self.steps:
            if not step.ok:
                return step
        return None

    def __str__(self) -> str:
        done = sum(1 for step in self.steps if step.ok)
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.name}: {done}/{len(self.steps)} steps in {self.elapsed:.2f}s{suffix}"


class Step(ABC):
    """One unit of work with a name and an outcome."""

    name = "step"

    @abstractmethod
    async def run(self, ctx: TaskContext) -> StepReport:
        """Perform the step and report whether it succeeded."""

    def describe(self) -> str:
        return self.name

    def _report(self, status: StepStatus, started: float, detail: str = "") -> StepReport:
        return StepReport(
            self.name,
            status,
            time.monotonic() - started,
            detail=detail,
        )


class CallStep(Step):
    """Wrap a plain function as a step.

    The callable may be sync or async and receives the context.  Returning
    ``False`` or a non-empty string marks the step failed (the string becomes the
    detail), which gives quick escape hatches without a subclass.
    """

    def __init__(self, name: str, fn) -> None:  # noqa: ANN001 - callable
        self.name = name
        self.fn = fn

    async def run(self, ctx: TaskContext) -> StepReport:
        started = time.monotonic()
        try:
            result = self.fn(ctx)
            if asyncio.iscoroutine(result):
                result = await result
        except TaskError:
            raise
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            return self._report(
                StepStatus.FAILED, started, f"{type(error).__name__}: {error}"
            )
        if result is False:
            return self._report(StepStatus.FAILED, started, "returned False")
        if isinstance(result, str) and result:
            return self._report(StepStatus.FAILED, started, result)
        return self._report(StepStatus.DONE, started)

    def describe(self) -> str:
        return self.name


class GoalStep(Step):
    """Run a goal to completion as one step."""

    def __init__(self, goal, *, timeout: float = 120.0, name: str | None = None) -> None:  # noqa: ANN001
        self.goal = goal
        self.timeout = timeout
        self.name = name or f"goal: {goal.describe()}"

    async def run(self, ctx: TaskContext) -> StepReport:
        started = time.monotonic()
        report = await ctx.bot.goals.run(self.goal, timeout=self.timeout)
        return self._report(
            StepStatus.DONE if report.ok else StepStatus.FAILED,
            started,
            report.detail or str(report),
        )

    def describe(self) -> str:
        return self.name


class Task:
    """An ordered list of steps with a name for reports."""

    def __init__(self, name: str, *steps: Step) -> None:
        self.name = name
        self.steps: list[Step] = list(steps)

    def add(self, step: Step) -> Task:
        """Append a step and return ``self`` so tasks read as a chain."""

        self.steps.append(step)
        return self

    def extend(self, *steps: Step) -> Task:
        self.steps.extend(steps)
        return self

    def add_goal(self, goal, *, timeout: float = 120.0, name: str | None = None) -> Task:  # noqa: ANN001
        return self.add(GoalStep(goal, timeout=timeout, name=name))

    def add_call(self, name: str, fn) -> Task:  # noqa: ANN001
        return self.add(CallStep(name, fn))

    def describe(self) -> str:
        return " -> ".join(step.describe() for step in self.steps) or "(empty)"

    def __repr__(self) -> str:
        return f"<Task {self.name} ({len(self.steps)} steps)>"


class TaskRunner:
    """Execute tasks, retrying failed steps before giving up."""

    def __init__(self, bot: Bot, *, retries: int = 1, retry_delay: float = 0.3) -> None:
        if retries < 0:
            raise ValueError("retries cannot be negative")
        if retry_delay < 0.0:
            raise ValueError("retry_delay cannot be negative")
        self.bot = bot
        self.retries = retries
        self.retry_delay = retry_delay

    async def run(self, task: Task, *, timeout: float = 300.0) -> TaskReport:
        """Run every step in order, stopping at the first exhausted step."""

        if timeout <= 0.0:
            raise ValueError("timeout must be positive")
        started = time.monotonic()
        context = TaskContext(bot=self.bot)
        reports: list[StepReport] = []
        await self.bot.events.emit("task_start", task)
        try:
            for step in task.steps:
                if time.monotonic() - started >= timeout:
                    reports.append(
                        StepReport(step.describe(), StepStatus.FAILED, 0.0, 0, "task timeout")
                    )
                    break
                report = await self._run_step(step, context)
                reports.append(report)
                if not report.ok:
                    break
        finally:
            result = TaskReport(
                task.name,
                all(step.ok for step in reports) and len(reports) == len(task.steps),
                time.monotonic() - started,
                tuple(reports),
                "" if all(step.ok for step in reports) else _first_failure(reports),
            )
            await self.bot.events.emit("task_done", result)
        return result

    async def _run_step(self, step: Step, context: TaskContext) -> StepReport:
        attempts = 0
        started = time.monotonic()
        while True:
            attempts += 1
            try:
                report = await step.run(context)
            except TaskError as error:
                report = StepReport(
                    step.describe(), StepStatus.FAILED, time.monotonic() - started, attempts, str(error)
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a broken step must not take the task down
                # A dropped connection surfaces here as ``ConnectionClosed`` and a
                # goal runner that is already busy as ``RuntimeError``; both are
                # step failures, and the caller reads them from the report
                # instead of handling an exception per call site.
                report = StepReport(
                    step.describe(),
                    StepStatus.FAILED,
                    time.monotonic() - started,
                    attempts,
                    f"{type(error).__name__}: {error}",
                )
            if report.ok or attempts > self.retries:
                if attempts > 1:
                    report = StepReport(
                        report.name,
                        report.status,
                        report.elapsed,
                        attempts,
                        report.detail,
                    )
                await self.bot.events.emit("task_step", report, context)
                return report
            await asyncio.sleep(self.retry_delay)


def _first_failure(reports: list[StepReport]) -> str:
    for report in reports:
        if not report.ok:
            return f"{report.name}: {report.detail or 'failed'}"
    return ""
