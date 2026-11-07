"""Domain registration; the agent/analyzer/runtime have no commerce-specific branches."""

from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI

from apps.commerce_v1 import create_app as commerce_v1
from apps.commerce_v2 import create_app as commerce_v2
from apps.moderated import new_app, old_app, public_check
from apps.transfer import create_v1, create_v2


@dataclass(frozen=True)
class Fixture:
    name: str
    old: Callable[[str], FastAPI]
    new: Callable[[str], FastAPI]
    public_check: Callable | None = None


FIXTURES = {
    "commerce": Fixture("commerce", commerce_v1, commerce_v2),
    "transfer": Fixture("transfer", create_v1, create_v2),
    "moderated": Fixture("moderated", old_app, new_app, public_check),
}
