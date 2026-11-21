"""Validated simulator controls. These fields are never written to Eye's event stream."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LAB_SEED = 42
RECOVERY_HOURS = 4


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: Literal["DE", "FR"] | None = None
    platform: Literal["mobile", "web"] | None = None
    provider: Literal["Stripe", "Adyen"] | None = None

class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fault_stage: Literal["payment_timeout", "product_page_load"]
    scope: Scope
    severity: float = Field(ge=0.1, le=0.95)
    duration_hours: int = Field(ge=1, le=24)
    premise: str | None = Field(default=None, min_length=1, max_length=1500)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.fault_stage == "payment_timeout" and not self.scope.model_dump(exclude_none=True):
            raise ValueError("Choose at least one country, platform or provider for payment timeouts.")
        if self.fault_stage == "product_page_load" and self.scope.provider:
            raise ValueError("A payment provider cannot scope a product-page load.")
        return self


class ScenarioReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=500)
    scenario: Scenario | None


SCENARIO_PROMPT = """Plan experiments for a synthetic marketplace simulator.
Use the latest request and prior chat. Keep the hypothetical premise in the plan.
Translate premises into either payment_timeout (absolute timeout probability)
or product_page_load (fraction of otherwise successful product views prevented).
A cause need not have a graph node. For 'product pages hosted in Frankfurt fail
to load', propose product_page_load, scope {} and explain that a hosting location
does not describe customer traffic. There is no simulated hosting geography.
Only use explicit traffic restrictions: country DE or FR, platform mobile or web,
and, for payment only, provider Stripe or Adyen. Payment requires a restriction.
Use severity 0.65 and duration 1 virtual hour if unspecified. Duration is whole
hours from 1 to 24. For requests with no supported observable effect, return null
scenario and a short explanation. Return a proposal for review, and describe
what the simulator could change without claiming the premise happened."""
