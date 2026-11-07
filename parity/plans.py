"""Domain-neutral, versioned migration plan schema."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Strict):
    document: Literal["old", "new", "notes", "probe"]
    pointer: str
    confidence: Literal["static", "observed", "assumption"]


class Mapping(Strict):
    legacy_operation: str
    target_operations: list[str] = Field(min_length=1)
    field_mappings: dict[str, str]
    evidence: list[Evidence] = Field(min_length=1)


class Step(Strict):
    name: str
    operation: str
    depends_on: list[str]
    retry: str
    compensation: str


class ErrorMapping(Strict):
    upstream: str
    legacy_status: int = Field(ge=400, le=599)
    legacy_code: str


class MigrationPlan(Strict):
    schema_version: Literal[1]
    revision: int = Field(ge=1)
    domain: str
    assumptions: list[str]
    mappings: list[Mapping] = Field(min_length=1)
    workflow: list[Step] = Field(min_length=1)
    errors: list[ErrorMapping]
    idempotency_strategy: str = Field(min_length=20)
    identifier_strategy: str = Field(min_length=10)
    pagination_strategy: str
    limitations: list[str]

    @model_validator(mode="after")
    def valid_steps(self):
        seen: set[str] = set()
        for step in self.workflow:
            if step.name in seen or not set(step.depends_on) <= seen:
                raise ValueError("workflow must have unique steps in dependency order")
            seen.add(step.name)
        return self


def validate_plan(plan: MigrationPlan, old: dict, new: dict):
    from parity.contracts import operations, resolve_pointer

    left, right = operations(old), operations(new)
    for mapping in plan.mappings:
        if mapping.legacy_operation not in left or any(
            x not in right for x in mapping.target_operations
        ):
            raise ValueError("plan names an operation absent from contracts")
        for source in mapping.evidence:
            if source.document in {"old", "new"}:
                resolve_pointer(old if source.document == "old" else new, source.pointer)
    for step in plan.workflow:
        if step.operation not in right:
            raise ValueError("workflow names an unknown target operation")
    return plan
