"""Crafting recipes as data a bot can act on.

A recipe is written the way a player sees it -- a small grid of characters plus
a legend -- so a goal can place the exact items the server expects without a
client-side recipe index.  Only what the caller declares is ever crafted; there
is no guess-work about "what could this become", because a bot that guesses is
a bot that wastes a player's materials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

__all__ = ["Recipe", "RECIPES", "recipe_for"]


@dataclass(frozen=True, slots=True)
class Recipe:
    """One crafting recipe.

    ``pattern`` rows are read top to bottom, each character is one cell, and a
    space (or ``.``) is an empty cell.  ``key`` maps those characters to item
    identifiers, so::

        Recipe(
            output="minecraft:stick",
            count=4,
            pattern=("o", "o"),
            key={"o": "minecraft:oak_planks"},
        )

    A shapeless recipe leaves ``pattern`` empty and lists one item per slot in
    ``ingredients`` order (row-major), which is what the server accepts anyway:
    the arrangement does not matter, only the multiset.
    """

    output: str
    count: int = 1
    pattern: tuple[str, ...] = ()
    key: Mapping[str, str] = field(default_factory=dict)
    shapeless: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.output:
            raise ValueError("a recipe needs an output item")
        if self.count <= 0:
            raise ValueError("a recipe needs a positive output count")
        if self.pattern and self.shapeless:
            raise ValueError("a recipe is either shaped or shapeless, not both")
        if not self.pattern and not self.shapeless:
            raise ValueError("a recipe needs a pattern or shapeless ingredients")
        for row in self.pattern:
            if len(row) != len(self.pattern[0]):
                raise ValueError("every pattern row must have the same width")
        if len(self.pattern) > 3 or (self.pattern and len(self.pattern[0]) > 3):
            raise ValueError("a crafting grid is at most 3x3")
        for row in self.pattern:
            for char in row:
                if char in (" ", "."):
                    continue
                if char not in self.key:
                    raise ValueError(f"pattern character {char!r} is not in the key")

    # -- shape -----------------------------------------------------------

    @property
    def shaped(self) -> bool:
        return bool(self.pattern)

    @property
    def width(self) -> int:
        if self.pattern:
            return len(self.pattern[0])
        return min(len(self.shapeless), 3) or 1

    @property
    def height(self) -> int:
        if self.pattern:
            return len(self.pattern)
        return max(1, (len(self.shapeless) + 2) // 3)

    @property
    def size(self) -> int:
        """Smallest vanilla grid this recipe fits in: 2 or 3."""

        return 2 if max(self.width, self.height) <= 2 else 3

    @property
    def cells(self) -> tuple[tuple[int, int, str], ...]:
        """``(row, column, item)`` for every non-empty cell, row-major."""

        if self.pattern:
            cells = []
            for row_index, row in enumerate(self.pattern):
                for column, char in enumerate(row):
                    if char in (" ", "."):
                        continue
                    cells.append((row_index, column, self.key[char]))
            return tuple(cells)
        cells = []
        for index, item in enumerate(self.shapeless):
            cells.append((index // 3, index % 3, item))
        return tuple(cells)

    @property
    def ingredients(self) -> dict[str, int]:
        """Item identifier -> how many this recipe consumes in total."""

        wants: dict[str, int] = {}
        for _, _, item in self.cells:
            wants[item] = wants.get(item, 0) + 1
        return wants

    @property
    def total_ingredients(self) -> int:
        return sum(self.ingredients.values())

    # -- helpers ---------------------------------------------------------

    def describe(self) -> str:
        if self.shaped:
            rows = " / ".join(repr(row) for row in self.pattern)
            return f"{self.count}x {self.output} from {rows}"
        items = ", ".join(f"{count}x {item}" for item, count in self.ingredients.items())
        return f"{self.count}x {self.output} from {items}"

    def __str__(self) -> str:
        return self.describe()


def _planks(wood: str) -> str:
    return f"minecraft:{wood}_planks"


def _log(wood: str) -> str:
    return f"minecraft:{wood}_log"


def _build() -> tuple[dict[str, Recipe], dict[str, dict[str, Recipe]]]:
    """A small starter set; callers are expected to add their own.

    Returns the default table plus a per-wood table, because planks, sticks, a
    crafting table and a chest all exist once per wood type and "the stick
    recipe" has to mean the one for the planks the bot is holding.
    """

    by_wood: dict[str, dict[str, Recipe]] = {}
    for wood in ("oak", "spruce", "birch"):
        by_wood[wood] = {
            "minecraft:stick": Recipe(
                output="minecraft:stick",
                count=4,
                pattern=("p", "p"),
                key={"p": _planks(wood)},
            ),
            "minecraft:crafting_table": Recipe(
                output="minecraft:crafting_table",
                pattern=("pp", "pp"),
                key={"p": _planks(wood)},
            ),
            "minecraft:chest": Recipe(
                output="minecraft:chest",
                pattern=("ppp", "p p", "ppp"),
                key={"p": _planks(wood)},
            ),
        }
    defaults: dict[str, Recipe] = {}
    for wood in ("oak", "spruce", "birch"):
        defaults[_planks(wood)] = Recipe(
            output=_planks(wood),
            count=4,
            shapeless=(_log(wood),),
        )
    defaults.update(by_wood["oak"])
    defaults["minecraft:torch"] = Recipe(
        output="minecraft:torch",
        count=4,
        pattern=("c", "s"),
        key={"c": "minecraft:coal", "s": "minecraft:stick"},
    )
    defaults["minecraft:furnace"] = Recipe(
        output="minecraft:furnace",
        pattern=("ccc", "c c", "ccc"),
        key={"c": "minecraft:cobblestone"},
    )
    defaults["minecraft:stone_pickaxe"] = Recipe(
        output="minecraft:stone_pickaxe",
        pattern=("ccc", " s ", " s "),
        key={"c": "minecraft:cobblestone", "s": "minecraft:stick"},
    )
    return defaults, by_wood


#: Built-in recipes keyed by output identifier.  Add whichever ones you need.
_DEFAULTS, _BY_WOOD = _build()
RECIPES: Mapping[str, Recipe] = _DEFAULTS

#: Wood -> output identifier -> recipe, for the items that exist per wood type.
WOOD_RECIPES: Mapping[str, Mapping[str, Recipe]] = _BY_WOOD


def recipe_for(output: str, *, planks: str = "minecraft:oak_planks") -> Recipe:
    """Look a recipe up by output, picking the wood variant from ``planks``."""

    identifier = output if ":" in output else f"minecraft:{output}"
    wood = planks.removeprefix("minecraft:").removesuffix("_planks")
    variant = WOOD_RECIPES.get(wood, {}).get(identifier)
    if variant is not None:
        return variant
    recipe = RECIPES.get(identifier)
    if recipe is not None:
        return recipe
    raise KeyError(f"no built-in recipe for {identifier}")
