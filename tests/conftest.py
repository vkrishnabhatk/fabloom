from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT, ModelsConfig, RunConfig, models_config
from fabloom.contracts import CheckSet, FabricPlan, Intent, Platform, RunBudget, TopologySpec
from fabloom.llm import ModelRouter

CONF_DIR = REPO_ROOT / "conf"


@pytest.fixture
def frr_intent() -> Intent:
    return Intent(platform=Platform.FRR, topology=TopologySpec(spines=2, leaves=2))


@pytest.fixture
def frr_fabric(frr_intent: Intent) -> FabricPlan:
    return allocate(frr_intent)


@pytest.fixture
def frr_checkset(frr_intent: Intent, frr_fabric: FabricPlan) -> CheckSet:
    return compile_checks(frr_intent, frr_fabric)


@pytest.fixture
def models() -> ModelsConfig:
    return models_config(CONF_DIR)


@pytest.fixture
def router(models: ModelsConfig) -> ModelRouter:
    return ModelRouter(models)


@pytest.fixture
def run_config() -> RunConfig:
    return RunConfig(budget=RunBudget(max_repair_rounds=3, max_usd=1.0), repair_candidates=1)


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()
