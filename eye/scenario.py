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

    effect: Literal["reduce_completions", "payment_timeout", "payment_success_delay", "search_rollout"]
    transition: Literal["product_view", "cart_add", "payment", "search"]
    scope: Scope = Field(default_factory=Scope)
    severity: float = Field(ge=0.1, le=0.95)
    duration_hours: int = Field(ge=1, le=24)
    premise: str | None = Field(default=None, min_length=1, max_length=1500)

    @model_validator(mode="before")
    @classmethod
    def upgrade_saved_plan(cls, value):
        if isinstance(value, dict) and "fault_stage" in value and "effect" not in value and "transition" not in value:
            legacy = {"payment_timeout": ("payment_timeout", "payment"),
                      "product_page_load": ("reduce_completions", "product_view")}
            stage = value["fault_stage"]
            if isinstance(stage, str) and stage in legacy:
                value = value.copy()
                value.pop("fault_stage")
                value["effect"], value["transition"] = legacy[stage]
        return value

    @model_validator(mode="after")
    def validate_transition(self):
        supported = {
            "product_view": {"reduce_completions"},
            "cart_add": {"reduce_completions"},
            "payment": {"payment_timeout", "payment_success_delay"},
            "search": {"search_rollout"},
        }
        if self.effect not in supported[self.transition]:
            raise ValueError(f"{self.effect} is unavailable at the {self.transition} transition.")
        if self.transition != "payment" and self.scope.provider:
            raise ValueError("A payment provider can only scope payment traffic.")
        return self


class ScenarioReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=500)
    scenario: Scenario | None


SCENARIO_PROMPT = """Plan experiments for a synthetic marketplace simulator.
Use the latest request and prior chat. Keep the hypothetical premise in the plan.
Use effect reduce_completions for fewer successful outcomes at a transition,
either product_view (a page request reaches a successful product view) or
cart_add (an add-to-cart request reaches a successful cart addition). Severity
is the fraction of otherwise successful completions prevented at that transition.
Use effect payment_timeout with transition payment and an absolute timeout probability.
Use effect payment_success_delay with transition payment when successful payment
receipts arrive late. Severity is the fraction delayed by 90 virtual minutes.
Use effect search_rollout with transition search to try a faster search release.
Severity is the share of eligible shoppers assigned the faster release, with the
rest staying on the existing release. For a request about a faster release and
recommendation-assisted sales, simulate the release and let Eye observe the effect.
A cause need not have a graph node. 'Frankfurt product pages fail to load' maps to
product_view and 'people struggle to add products to their cart' maps to cart_add.
Both use scope {} unless a customer traffic restriction is explicit. The simulator
has no hosting geography, so Frankfurt as a hosting premise cannot scope traffic.
Use marketplace-wide scope {} for any unspecified traffic. Explicit restrictions
are country DE or FR, platform mobile or web, and for payment only provider Stripe or Adyen.
Use severity 0.65 and duration 1 virtual hour if unspecified. Duration is whole
hours from 1 to 24. For requests with no supported observable effect, return null
scenario and a short explanation. Return a proposal for review, and describe
what the simulator could change without claiming the premise happened."""
