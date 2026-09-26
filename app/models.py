"""Data models: the LLM extraction schema, its JSON Schema twin, and the stored lead."""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ServiceCategory(str, Enum):
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    HVAC = "hvac"
    HANDYMAN = "handyman"
    DRYWALL = "drywall"
    PAINTING = "painting"
    APPLIANCE_REPAIR = "appliance_repair"
    ROOFING = "roofing"
    FLOORING = "flooring"
    OTHER = "other"


class Urgency(str, Enum):
    EMERGENCY = "emergency"
    WITHIN_48H = "within_48h"
    THIS_WEEK = "this_week"
    FLEXIBLE = "flexible"


# Loose model outputs -> canonical enum values
_CATEGORY_ALIASES = {
    "appliance": "appliance_repair",
    "appliances": "appliance_repair",
    "heating": "hvac",
    "cooling": "hvac",
    "ac": "hvac",
    "air_conditioning": "hvac",
    "electric": "electrical",
    "electrician": "electrical",
    "plumber": "plumbing",
    "paint": "painting",
    "roof": "roofing",
    "floor": "flooring",
    "floors": "flooring",
}
_URGENCY_ALIASES = {
    "48h": "within_48h",
    "within_48_hours": "within_48h",
    "urgent": "within_48h",
    "week": "this_week",
    "emergent": "emergency",
}


def _norm_key(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")
    return value


class LeadExtraction(BaseModel):
    customer_name: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    service_category: ServiceCategory = ServiceCategory.OTHER
    problem_summary: str
    urgency: Urgency = Urgency.FLEXIBLE
    urgency_reason: str = ""
    preferred_time: str | None = None
    has_photos: bool = False
    language_detected: str = "en"
    missing_info: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator(
        "customer_name", "phone", "email", "address", "city", "state", "preferred_time",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a", "unknown"}:
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("service_category", mode="before")
    @classmethod
    def _norm_category(cls, v: Any) -> Any:
        v = _norm_key(v)
        v = _CATEGORY_ALIASES.get(v, v)
        return v if v in ServiceCategory._value2member_map_ else ServiceCategory.OTHER

    @field_validator("urgency", mode="before")
    @classmethod
    def _norm_urgency(cls, v: Any) -> Any:
        v = _norm_key(v)
        return _URGENCY_ALIASES.get(v, v)

    @field_validator("city", mode="after")
    @classmethod
    def _title_city(cls, v: str | None) -> str | None:
        return v.title() if v and (v.islower() or v.isupper()) else v

    @field_validator("state", mode="after")
    @classmethod
    def _upper_state(cls, v: str | None) -> str | None:
        return v.upper() if v and len(v) == 2 else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> Any:
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.5


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


# Plain JSON Schema passed to providers that support structured output.
EXTRACTION_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "customer_name": _nullable("string"),
        "phone": _nullable("string"),
        "email": _nullable("string"),
        "address": _nullable("string"),
        "city": _nullable("string"),
        "state": _nullable("string"),
        "service_category": {"type": "string", "enum": [c.value for c in ServiceCategory]},
        "problem_summary": {"type": "string"},
        "urgency": {"type": "string", "enum": [u.value for u in Urgency]},
        "urgency_reason": {"type": "string"},
        "preferred_time": _nullable("string"),
        "has_photos": {"type": "boolean"},
        "language_detected": {"type": "string"},
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": list(LeadExtraction.model_fields),
}


class LeadStatus(str, Enum):
    MATCHED = "matched"                              # contractor suggested, waiting for manager
    NEEDS_MANUAL_ASSIGNMENT = "needs_manual_assignment"
    CONFIRMED = "confirmed"                          # manager confirmed the suggested contractor
    CALL_FIRST = "call_first"                        # manager will call the customer first


class LeadRecord(BaseModel):
    """A lead as stored: raw input + extraction + routing state."""

    id: str
    created_at: str
    updated_at: str
    source: str
    raw_text: str
    extraction: LeadExtraction
    status: LeadStatus
    contractor_id: str | None = None
    contractor_name: str | None = None
    match_reason: str = ""
    tried_contractors: list[str] = Field(default_factory=list)   # offered so far, for Reassign
    draft_reply: str | None = None
    telegram_message_id: int | None = None
