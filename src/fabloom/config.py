"""Config: commented YAML in ``conf/`` plus environment settings. Deliberately not Hydra.

- ``load_config(name)`` parses ``conf/<name>.yaml`` once per process (``lru_cache``), so a web
  request never re-parses YAML (moodrift fix #6).
- ``Settings`` holds what varies per deployment and every secret; secrets never live in YAML.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fabloom.contracts import ModelRole, RunBudget

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FABLOOM_", env_file=".env", extra="ignore")

    token_factory_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FABLOOM_TOKEN_FACTORY_API_KEY", "NEBIUS_API_KEY"),
    )
    token_factory_base_url: str = "https://api.tokenfactory.nebius.com/v1/"  # noqa: S105
    conf_dir: Path = REPO_ROOT / "conf"
    runs_dir: Path = Path("runs")
    verifier: Literal["local"] = "local"
    batfish_host: str = "localhost"
    log_level: str = "INFO"
    log_json: bool = True
    # SQLite store plus ./mlruns artifacts; both are backed up off the VM (architecture §11).
    mlflow_tracking_uri: str = "sqlite:///mlflow.db"
    mlflow_artifact_root: Path = Path("mlruns")


def resolve(path: str | Path, base: Path = REPO_ROOT) -> Path:
    """Resolve a config-relative path against the repo root, leaving absolute paths alone."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=32)
def load_config(name: str, conf_dir: Path | None = None) -> dict[str, Any]:
    path = (conf_dir or Settings().conf_dir) / f"{name}.yaml"
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return data


# --- typed views over conf/*.yaml ------------------------------------------------------


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    usd_per_m_input: float = Field(ge=0)
    usd_per_m_output: float = Field(ge=0)
    max_output_tokens: int = Field(default=8192, ge=1)
    temperature: float = Field(default=0.2, ge=0, le=2)
    price_verified: bool = False  # False until spikes/nemotron_probe.py confirms it
    extra_body: dict[str, Any] = Field(default_factory=dict)

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.usd_per_m_input + completion_tokens * self.usd_per_m_output
        ) / 1_000_000


class EscalationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_round: int = Field(default=3, ge=1)
    critical_from_round: int = Field(default=2, ge=1)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, ModelSpec]
    roles: dict[ModelRole, str]
    escalation: EscalationRule = EscalationRule()

    def for_role(self, role: ModelRole) -> ModelSpec:
        return self.models[self.roles[role]]


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: RunBudget = RunBudget()
    repair_candidates: int = Field(default=1, ge=1, le=8)
    # The benchmark turns this off: nothing grades the explanation, so it would only add cost.
    explain_with_model: bool = True


class ArmSpec(BaseModel):
    """One benchmark arm: a routing override plus run-limit overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    roles: dict[ModelRole, str] = Field(default_factory=dict)
    max_repair_rounds: int | None = Field(default=None, ge=0)  # None: conf/fabloom.yaml's

    def models(self, base: ModelsConfig) -> ModelsConfig:
        unknown = sorted(set(self.roles.values()) - set(base.models))
        if unknown:
            raise ValueError(f"arm routes to models not in conf/models.yaml: {unknown}")
        return base.model_copy(update={"roles": {**base.roles, **self.roles}})

    def run_config(self, base: RunConfig) -> RunConfig:
        budget = base.budget
        if self.max_repair_rounds is not None:
            budget = budget.model_copy(update={"max_repair_rounds": self.max_repair_rounds})
        return base.model_copy(update={"budget": budget, "explain_with_model": False})


class RegressionGateSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: list[str] = Field(default_factory=lambda: ["final_pass_rate"])
    max_drop: float = Field(default=0.10, ge=0, le=1)


class GroundingGateSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: str = "super-repair-grounding"
    control: str = "super-repair"
    min_gain: float = Field(default=0.05, ge=0, le=1)
    max_cost_ratio: float = Field(default=1.5, gt=0)


class GatesSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regression: RegressionGateSpec = RegressionGateSpec()
    grounding: GroundingGateSpec = GroundingGateSpec()


class NightlySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arms: list[str] = Field(default_factory=lambda: ["routed"])
    scenarios: int = Field(default=10, ge=1)


class BenchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arms: dict[str, ArmSpec]
    gates: GatesSpec = GatesSpec()
    nightly: NightlySpec = NightlySpec()


def models_config(conf_dir: Path | None = None) -> ModelsConfig:
    return ModelsConfig.model_validate(load_config("models", conf_dir))


def run_config(conf_dir: Path | None = None) -> RunConfig:
    return RunConfig.model_validate(load_config("fabloom", conf_dir).get("run", {}))


def bench_config(conf_dir: Path | None = None) -> BenchConfig:
    return BenchConfig.model_validate(load_config("bench", conf_dir))
