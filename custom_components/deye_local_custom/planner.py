"""Which registers to request now: no I/O, no Home Assistant state."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .description import AnyDescription, Model, RegisterDescription


@dataclass(frozen=True)
class Request:
    first: int
    count: int

    @property
    def last(self) -> int:
        return self.first + self.count - 1

    def holds(self, description: RegisterDescription) -> bool:
        return (
            self.first <= min(description.registers)
            and max(description.registers) <= self.last
        )


def is_due(
    description: AnyDescription,
    read_at: Mapping[str, float],
    now: float,
    slack: float,
) -> bool:
    """Never read, or read an interval ago (less `slack`: polls drift a little late).

    Counted from the last good read, so a failed request is retried on the next poll,
    not an interval later.
    """
    last = read_at.get(description.key)
    return last is None or now - last >= description.interval - slack


def plan(
    model: Model,
    active: Collection[str],
    read_at: Mapping[str, float],
    now: float,
) -> list[Request]:
    """One request per block that has due registers: from the block's lowest active
    register, due or not, to its highest due one.

    Starting low keeps what a block reads together in one request also when an
    interval of its own puts a register out of step: grid voltage (150, possibly every
    5 s) comes with every read of grid status (194), even on a refresh between polls.
    `active`: keys of the entities that exist and are enabled; others are not read.
    """
    slack = model.tick / 2
    enabled = [d for d in model.descriptions if d.key in active]
    due = [d for d in enabled if is_due(d, read_at, now, slack)]
    requests = []
    for block in model.blocks:
        last = max(
            (r for d in due if block.holds(d) for r in d.registers), default=None
        )
        if last is None:
            continue
        first = min(r for d in enabled if block.holds(d) for r in d.registers)
        requests.append(Request(first, last - first + 1))
    return requests


def requests_per_minute(model: Model, active: Collection[str]) -> float:
    """Requests a minute the plan makes over 10 minutes (the clock's 300 s twice):
    every poll on time, every request answered."""
    span = 600
    read_at: dict[str, float] = {}
    count = 0
    for now in range(0, span, model.tick):
        for request in plan(model, active, read_at, float(now)):
            count += 1
            for d in model.descriptions:
                if d.key in active and request.holds(d):
                    read_at[d.key] = float(now)
    return count * 60 / span
