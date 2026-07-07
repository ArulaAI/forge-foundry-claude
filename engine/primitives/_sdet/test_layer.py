"""Canonical test-layer model — the testing cone.

Used by S1 sdet-architect and S2 test-maintainer to reason about which
layer a test belongs to and how layers should be proportioned in a
healthy test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

TARGET_CONE_SUM = 100


class TestLayer(Enum):
    """The 4 canonical layers of the testing cone (broad base, narrow tip)."""

    __test__ = False  # prevent pytest collection (name starts with "Test")

    UNIT = "unit"
    CONTRACT = "contract"
    INTEGRATION = "integration"
    E2E = "e2e"


@dataclass(frozen=True)
class ConeRatios:
    """Target percentage of tests at each layer; must sum to 100."""

    unit: int
    contract: int
    integration: int
    e2e: int

    DEFAULT: ClassVar[ConeRatios]

    def __post_init__(self) -> None:
        total = self.unit + self.contract + self.integration + self.e2e
        if total != TARGET_CONE_SUM:
            raise ValueError(
                f"ConeRatios values must sum to {TARGET_CONE_SUM}, got {total} "
                f"(unit={self.unit} + contract={self.contract} + "
                f"integration={self.integration} + e2e={self.e2e})"
            )


ConeRatios.DEFAULT = ConeRatios(unit=70, contract=15, integration=12, e2e=3)
