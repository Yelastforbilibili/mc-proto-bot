"""Goal layer: declarative objectives driven by :class:`GoalRunner`.

Three layers stack here, and each stays usable on its own:

* ``protobot.actions`` — one packet-level interaction with a confirmed result;
* ``protobot.goals`` — *what* should be true, retried until it is;
* ``protobot.tasks`` — a script of steps with retries and a report.

```python
report = await bot.goals.run(BreakBlockGoal((2, -59, 0)))
if not report.ok:
    print(report.detail)
```
"""

from __future__ import annotations

from .base import Goal, GoalError, GoalReport, GoalStatus, block_center
from .builtin import (
    AttackEntityGoal,
    BreakBlockGoal,
    CraftGoal,
    PlaceBlockGoal,
    ReachGoal,
    WaitGoal,
)
from .composite import AnyOfGoal, SequenceGoal
from .runner import DEFAULT_TICK_INTERVAL, GoalRunner

__all__ = [
    "AnyOfGoal",
    "AttackEntityGoal",
    "BreakBlockGoal",
    "CraftGoal",
    "DEFAULT_TICK_INTERVAL",
    "Goal",
    "GoalError",
    "GoalReport",
    "GoalRunner",
    "GoalStatus",
    "PlaceBlockGoal",
    "ReachGoal",
    "SequenceGoal",
    "WaitGoal",
    "block_center",
]
