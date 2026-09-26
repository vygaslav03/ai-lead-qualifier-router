"""Contractor matching: same metro -> has the specialization -> available -> highest rating."""
import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .models import LeadExtraction, ServiceCategory

log = logging.getLogger(__name__)

# Suburbs and neighbouring cities served by a metro's contractors: (city, state) -> metro city.
METRO_ALIASES: dict[tuple[str, str], str] = {
    **{(c, "TX"): "houston" for c in ("katy", "sugar land", "pasadena", "pearland", "the woodlands", "bellaire", "spring", "humble", "cypress")},
    **{(c, "TX"): "dallas" for c in ("plano", "irving", "garland", "richardson", "mesquite", "carrollton", "frisco", "addison")},
    **{(c, "GA"): "atlanta" for c in ("decatur", "marietta", "sandy springs", "smyrna", "east point", "brookhaven", "dunwoody")},
    **{(c, "IL"): "chicago" for c in ("evanston", "oak park", "cicero", "skokie", "berwyn", "oak lawn")},
    **{(c, "AZ"): "phoenix" for c in ("scottsdale", "tempe", "mesa", "chandler", "glendale", "gilbert", "peoria")},
    **{(c, "CO"): "denver" for c in ("aurora", "lakewood", "englewood", "arvada", "westminster", "littleton", "thornton")},
}


def _norm(value: str | None) -> str:
    return " ".join((value or "").lower().replace(".", "").split())


@dataclass(frozen=True)
class Contractor:
    id: str
    name: str
    city: str
    state: str
    specializations: tuple[str, ...]
    is_available: bool
    rating: float
    phone: str


@dataclass
class MatchResult:
    contractor: Contractor | None
    reason: str
    metro: str | None = None
    alternatives: list[Contractor] = field(default_factory=list)   # other available, qualified, ranked
    unavailable: list[Contractor] = field(default_factory=list)    # qualified but busy

    @property
    def matched(self) -> bool:
        return self.contractor is not None


class ContractorDirectory:
    """Contractors from CSV. Reloads automatically when the file changes (e.g. availability edits)."""

    def __init__(self, csv_path: str | Path):
        self.path = Path(csv_path)
        self._mtime = 0.0
        self._contractors: list[Contractor] = []

    @property
    def contractors(self) -> list[Contractor]:
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:
            self._contractors = self._load()
            self._mtime = mtime
        return self._contractors

    def _load(self) -> list[Contractor]:
        with self.path.open(newline="", encoding="utf-8") as f:
            rows = [
                Contractor(
                    id=r["id"].strip(),
                    name=r["name"].strip(),
                    city=r["city"].strip(),
                    state=r["state"].strip().upper(),
                    specializations=tuple(s.strip().lower() for s in r["specializations"].split(";") if s.strip()),
                    is_available=r["is_available"].strip().lower() in {"true", "1", "yes", "y"},
                    rating=float(r["rating"] or 0),
                    phone=r["phone"].strip(),
                )
                for r in csv.DictReader(f)
            ]
        log.info("Loaded %d contractors from %s", len(rows), self.path)
        return rows

    def get(self, contractor_id: str) -> Contractor | None:
        return next((c for c in self.contractors if c.id == contractor_id), None)

    def resolve_metro(self, city: str | None, state: str | None) -> tuple[str, str] | None:
        """Map a lead's city (incl. suburbs) to a covered metro as (city, state)."""
        city_n, state_n = _norm(city), (state or "").upper()
        if not city_n:
            return None
        covered = {(_norm(c.city), c.state): (c.city, c.state) for c in self.contractors}

        candidates = [key for key in covered if key[0] == city_n and (not state_n or key[1] == state_n)]
        if not candidates:
            aliases = [(metro, st) for (c, st), metro in METRO_ALIASES.items()
                       if c == city_n and (not state_n or st == state_n)]
            candidates = [key for key in aliases if key in covered]
        # Ambiguous without a state (e.g. Aurora CO vs Aurora IL) -> don't guess
        return covered[candidates[0]] if len(candidates) == 1 else None

    def match(self, lead: LeadExtraction, exclude_ids: set[str] | frozenset[str] = frozenset()) -> MatchResult:
        category = lead.service_category.value
        if lead.service_category == ServiceCategory.OTHER:
            return MatchResult(None, "Service category is unclear")
        if not lead.city:
            return MatchResult(None, "City is unknown")

        metro = self.resolve_metro(lead.city, lead.state)
        if not metro:
            where = f"{lead.city}, {lead.state}" if lead.state else lead.city
            return MatchResult(None, f"No coverage in {where}")
        metro_label = f"{metro[0]}, {metro[1]}"

        qualified = [
            c for c in self.contractors
            if (c.city, c.state) == metro and category in c.specializations and c.id not in exclude_ids
        ]
        qualified.sort(key=lambda c: (-c.rating, c.name))
        available = [c for c in qualified if c.is_available]
        unavailable = [c for c in qualified if not c.is_available]

        if not available:
            if unavailable:
                reason = f"All {category} contractors in {metro_label} are unavailable"
            elif exclude_ids:
                reason = f"No other {category} contractors in {metro_label}"
            else:
                reason = f"No {category} contractor in {metro_label}"
            return MatchResult(None, reason, metro_label, unavailable=unavailable)

        best = available[0]
        reason = f"Top-rated available {category} contractor in {metro_label} ({best.rating}★)"
        if _norm(lead.city) != _norm(metro[0]):
            reason += f"; {lead.city} is served by the {metro[0]} team"
        return MatchResult(best, reason, metro_label, alternatives=available[1:], unavailable=unavailable)
