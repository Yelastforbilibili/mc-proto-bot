"""Record ticks and replay them, so two implementations can be compared.

A tick is a value now (``PhysicsEngine.step``); this keeps a transcript of those
values.  The point is not logging: it is being able to say "these forty ticks,
re-run, must land exactly here" -- against a probe, against the server's own
``/trace`` arc, or against a future version of the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import MovementInput, PhysicsEngine, PhysicsState, StepResult
from .geometry import Vec3
from .world import CollisionWorld


@dataclass(frozen=True, slots=True)
class Frame:
    """One recorded tick: what went in, what it started from, what came out."""

    controls: MovementInput
    before: PhysicsState
    after: PhysicsState


class Recorder:
    """Collects frames from a live engine so they can be replayed later."""

    def __init__(self) -> None:
        self.frames: list[Frame] = []

    def record(
        self,
        engine: PhysicsEngine,
        state: PhysicsState,
        controls: MovementInput,
        world: CollisionWorld,
    ) -> StepResult:
        """Advance one tick as a value and keep it in the transcript."""

        before = state.copy()
        result = engine.step(state, controls, world)
        self.frames.append(
            Frame(controls=controls, before=before, after=result.state.copy())
        )
        return result

    def replay(self, engine: PhysicsEngine, world: CollisionWorld) -> list[str]:
        """Re-run every frame and describe the ones that disagree.

        Returns human-readable problems (empty when the replay is exact), because
        a mismatch here is a bug report about the engine, not an exception the
        caller can do anything about.
        """

        problems: list[str] = []
        for index, frame in enumerate(self.frames):
            replayed = engine.step(frame.before, frame.controls, world)
            if not _same_position(replayed.state.position, frame.after.position):
                problems.append(
                    f"frame {index}: expected {_show(frame.after.position)} "
                    f"but landed on {_show(replayed.state.position)}"
                )
                continue
            # The position is what a server measures, but the velocity is what the
            # next tick will move by: a divergence that stops at the velocity is
            # still a divergence, and it is the one that shows up two ticks later.
            if not _same_position(replayed.state.velocity, frame.after.velocity):
                problems.append(
                    f"frame {index}: expected velocity {_show(frame.after.velocity)} "
                    f"but got {_show(replayed.state.velocity)}"
                )
                continue
            if replayed.collided != frame.after.horizontal_collision:
                problems.append(
                    f"frame {index}: collision flag differs "
                    f"({replayed.collided} vs {frame.after.horizontal_collision})"
                )
        return problems


def _same_position(left: Vec3, right: Vec3) -> bool:
    return (
        abs(left.x - right.x) <= 1e-9
        and abs(left.y - right.y) <= 1e-9
        and abs(left.z - right.z) <= 1e-9
    )


def _show(position: Vec3) -> str:
    return f"({position.x:.4f}, {position.y:.4f}, {position.z:.4f})"
