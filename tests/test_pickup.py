"""Tests for the pickup layer's stopping rules.

The interesting case is not "the inventory is full" but "the inventory is full
*and* every stack of the item is at its limit": then a drop can never arrive,
so the caller gets an error instead of waiting for a count that cannot grow.
"""

import unittest
from types import SimpleNamespace

from protobot.pickup import InventoryFull, PickupMixin


class _Stack(SimpleNamespace):
    """The two fields the pickup layer reads off an ItemStack."""


class _Menu:
    def __init__(self, stacks: list[_Stack], free: int | None) -> None:
        self._stacks = stacks
        self._free = free

    def free(self) -> int | None:
        return self._free

    @property
    def items(self) -> dict[int, _Stack]:
        """The decoded stacks, like the real ``Menu.items`` property."""

        return dict(enumerate(self._stacks))

    def count(self, name: str) -> int:
        return sum(stack.count for stack in self._stacks if stack.identifier == name)


class _Bot(PickupMixin):
    def __init__(self, stacks: list[_Stack], free: int | None) -> None:
        self.player_menu = _Menu(stacks, free)


class CannotTakeTests(unittest.TestCase):
    def test_an_empty_slot_always_accepts(self) -> None:
        bot = _Bot([_Stack(identifier="minecraft:stone", count=64)], free=12)
        self.assertFalse(bot._cannot_take("minecraft:oak_log"))

    def test_a_stack_below_the_limit_still_accepts(self) -> None:
        bot = _Bot([_Stack(identifier="minecraft:oak_log", count=63)], free=None)
        self.assertFalse(bot._cannot_take("minecraft:oak_log"))

    def test_a_full_inventory_and_a_full_stack_refuses(self) -> None:
        bot = _Bot(
            [
                _Stack(identifier="minecraft:stone", count=64),
                _Stack(identifier="minecraft:oak_log", count=64),
            ],
            free=None,
        )
        self.assertTrue(bot._cannot_take("minecraft:oak_log"))

    def test_a_full_inventory_without_the_item_refuses(self) -> None:
        bot = _Bot([_Stack(identifier="minecraft:stone", count=64)], free=None)
        self.assertTrue(bot._cannot_take("minecraft:oak_log"))

    def test_one_partial_stack_is_enough_to_keep_going(self) -> None:
        bot = _Bot(
            [
                _Stack(identifier="minecraft:oak_log", count=64),
                _Stack(identifier="minecraft:oak_log", count=7),
            ],
            free=None,
        )
        self.assertFalse(bot._cannot_take("minecraft:oak_log"))


class _WaitingBot(_Bot):
    """A bot with one drop on the floor and nowhere to put it."""

    def __init__(self, stacks: list[_Stack]) -> None:
        super().__init__(stacks, free=None)

    def dropped_items(self, **_):
        # ``dropped_items`` reads the tracked entities, so it is a plain method.
        return [(None, 7, "minecraft:oak_log", 1, 0.5, -60.0, 0.5)]

    async def navigate_to(self, *_args, **_kwargs):
        raise AssertionError("the bot must not walk to a drop it cannot take")


class PickUpRaisesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_full_inventory_with_a_full_stack_raises(self) -> None:
        bot = _WaitingBot([_Stack(identifier="minecraft:oak_log", count=64)])
        with self.assertRaises(InventoryFull) as raised:
            await bot.pick_up(items=["minecraft:oak_log"], count=3, timeout=1.0)
        self.assertIn("at its limit", raised.exception.report.reason)
        self.assertEqual(raised.exception.report.collected, {})

    async def test_a_partial_stack_is_picked_up_instead(self) -> None:
        bot = _WaitingBot([_Stack(identifier="minecraft:oak_log", count=63)])
        # A stack below the limit is not refused: the walk goes ahead, and it
        # is the timeout (not an early "cannot take" error) that ends the run.
        report = await bot.pick_up(items=["minecraft:oak_log"], count=3, timeout=0.05)
        self.assertFalse(report.ok)
        self.assertNotIn("at its limit", report.reason)


if __name__ == "__main__":
    unittest.main()
