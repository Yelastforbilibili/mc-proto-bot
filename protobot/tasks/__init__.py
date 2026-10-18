"""Task layer: scripts of steps with retries and a report.

```python
task = Task("strip mine").add(BreakBlocks([(2, -59, 0), (3, -59, 0)]))
report = await bot.tasks.run(task)
print(report, report.failed_step)
```
"""

from __future__ import annotations

from .base import (
    CallStep,
    GoalStep,
    Step,
    StepReport,
    StepStatus,
    Task,
    TaskContext,
    TaskError,
    TaskReport,
    TaskRunner,
)
from .builtin import BreakBlocks, PlaceBlocks, WaitStep

__all__ = [
    "BreakBlocks",
    "CallStep",
    "GoalStep",
    "PlaceBlocks",
    "Step",
    "StepReport",
    "StepStatus",
    "Task",
    "TaskContext",
    "TaskError",
    "TaskReport",
    "TaskRunner",
    "WaitStep",
]
