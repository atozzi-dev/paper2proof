"""Step implementations, one module per FSM step (A-15 onwards)."""

from collections.abc import Mapping

from app.domain.models import StepName
from app.orchestrator.fsm import Step

StepRegistry = Mapping[StepName, Step]
