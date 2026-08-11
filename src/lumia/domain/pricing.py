"""The estimating engine.

This is deliberately plain Python, not an LLM prompt. Pricing has to be
reproducible and auditable — the agent decides *what* to measure and calls
into here to get the number. Tune the constants for your market and the
whole platform re-prices.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import JobType, LineItem, Quote, Surface, SurfaceCondition

# --- Tunable rate card -------------------------------------------------

#: Loaded labor cost per painter-hour (wage + burden), in dollars.
LABOR_RATE_PER_HOUR = 55.0

#: Retail cost of one gallon of paint by job type.
PAINT_COST_PER_GALLON = {
    JobType.INTERIOR: 48.0,
    JobType.EXTERIOR: 62.0,
    JobType.CABINET: 85.0,
    JobType.DECK_FENCE: 55.0,
    JobType.COMMERCIAL: 42.0,
}

#: Square feet one gallon covers in a single coat.
COVERAGE_SQFT_PER_GALLON = {
    JobType.INTERIOR: 375.0,
    JobType.EXTERIOR: 300.0,   # rougher substrate, more absorption
    JobType.CABINET: 400.0,
    JobType.DECK_FENCE: 250.0,  # bare/weathered wood drinks product
    JobType.COMMERCIAL: 400.0,
}

#: Square feet one painter covers per hour, per coat, at "good" condition.
PRODUCTION_SQFT_PER_HOUR = {
    JobType.INTERIOR: 180.0,
    JobType.EXTERIOR: 150.0,
    JobType.CABINET: 45.0,      # doors/boxes, lots of detail and dry time
    JobType.DECK_FENCE: 200.0,
    JobType.COMMERCIAL: 250.0,  # open spans, spray-friendly
}

#: Multiplier applied to base labor to cover prep for a given condition.
PREP_MULTIPLIER = {
    SurfaceCondition.NEW: 1.10,
    SurfaceCondition.GOOD: 1.15,
    SurfaceCondition.FAIR: 1.40,
    SurfaceCondition.POOR: 1.85,
}

#: Extra labor for work above standard ceiling height (ladders/staging).
HEIGHT_SURCHARGE_THRESHOLD_FT = 10.0
HEIGHT_SURCHARGE_PER_FT = 0.04  # +4% labor per foot over the threshold

#: Primer is priced as a partial extra coat.
PRIMER_COAT_EQUIVALENT = 0.85

#: Sundries (tape, plastic, caulk, sandpaper, blades) as a share of paint cost.
SUNDRIES_RATE = 0.18

#: Business overhead applied to direct cost, then target net margin.
OVERHEAD_RATE = 0.22
TARGET_MARGIN = 0.28

#: Never quote below this — small jobs still burn a mobilization day.
MINIMUM_JOB_TOTAL = 450.0

#: A crew of this size is assumed when converting hours into calendar days.
DEFAULT_CREW_SIZE = 2
WORKING_HOURS_PER_DAY = 8.0


@dataclass
class SurfaceEstimate:
    surface: Surface
    labor_hours: float
    gallons: float


def estimate_surface(surface: Surface, job_type: JobType) -> SurfaceEstimate:
    """Labor hours and paint volume for a single surface."""
    if surface.square_feet <= 0:
        return SurfaceEstimate(surface=surface, labor_hours=0.0, gallons=0.0)

    coats = float(surface.coats)
    if surface.needs_primer:
        coats += PRIMER_COAT_EQUIVALENT

    production = PRODUCTION_SQFT_PER_HOUR[job_type]
    base_hours = (surface.square_feet * coats) / production

    hours = base_hours * PREP_MULTIPLIER[surface.condition]

    if surface.height_feet > HEIGHT_SURCHARGE_THRESHOLD_FT:
        over = surface.height_feet - HEIGHT_SURCHARGE_THRESHOLD_FT
        hours *= 1.0 + over * HEIGHT_SURCHARGE_PER_FT

    coverage = COVERAGE_SQFT_PER_GALLON[job_type]
    gallons = (surface.square_feet * coats) / coverage

    return SurfaceEstimate(
        surface=surface,
        labor_hours=round(hours, 2),
        gallons=round(gallons, 2),
    )


def build_quote(
    lead_id: str,
    job_type: JobType,
    surfaces: list[Surface],
    *,
    extras: list[LineItem] | None = None,
    discount_rate: float = 0.0,
) -> Quote:
    """Price a complete job.

    `extras` covers anything not measured in square feet — drywall repair,
    a dumpster, colour consult. `discount_rate` is a fraction (0.05 = 5%).
    """
    if not surfaces:
        raise ValueError("a quote needs at least one surface")
    if not 0.0 <= discount_rate < 1.0:
        raise ValueError("discount_rate must be between 0 and 1")

    estimates = [estimate_surface(s, job_type) for s in surfaces]
    total_hours = sum(e.labor_hours for e in estimates)
    total_gallons = sum(e.gallons for e in estimates)

    # Paint is bought in whole gallons, plus 10% so the crew never runs short.
    gallons_to_buy = _round_up_gallons(total_gallons * 1.10)

    labor_cost = total_hours * LABOR_RATE_PER_HOUR
    paint_cost = gallons_to_buy * PAINT_COST_PER_GALLON[job_type]
    sundries_cost = paint_cost * SUNDRIES_RATE

    line_items: list[LineItem] = [
        LineItem(
            description=f"Labor — {len(surfaces)} surface(s), prep and paint",
            quantity=round(total_hours, 2),
            unit="hour",
            unit_price=LABOR_RATE_PER_HOUR,
        ),
        LineItem(
            description=f"Paint — {job_type.value}",
            quantity=gallons_to_buy,
            unit="gallon",
            unit_price=PAINT_COST_PER_GALLON[job_type],
        ),
        LineItem(
            description="Sundries — masking, caulk, abrasives",
            quantity=1,
            unit="lot",
            unit_price=round(sundries_cost, 2),
        ),
    ]
    line_items.extend(extras or [])

    direct_cost = labor_cost + paint_cost + sundries_cost
    direct_cost += sum(item.total for item in (extras or []))

    overhead = direct_cost * OVERHEAD_RATE
    cost_with_overhead = direct_cost + overhead

    # Margin is on the sell price, not marked up on cost.
    total = cost_with_overhead / (1.0 - TARGET_MARGIN)
    margin = total - cost_with_overhead

    if discount_rate:
        total *= 1.0 - discount_rate
        margin = total - cost_with_overhead

    floored = max(total, MINIMUM_JOB_TOTAL)
    if floored > total:
        margin += floored - total
        total = floored

    assumptions = _assumptions(job_type, surfaces, gallons_to_buy, discount_rate)

    return Quote(
        lead_id=lead_id,
        job_type=job_type,
        line_items=line_items,
        labor_hours=round(total_hours, 2),
        gallons_needed=gallons_to_buy,
        subtotal=round(direct_cost, 2),
        overhead=round(overhead, 2),
        margin=round(margin, 2),
        total=round(total, 2),
        assumptions=assumptions,
    )


def estimate_duration_days(labor_hours: float, crew_size: int = DEFAULT_CREW_SIZE) -> int:
    """Calendar days a crew needs for the given labor hours."""
    if crew_size < 1:
        raise ValueError("crew_size must be at least 1")
    if labor_hours <= 0:
        return 0
    days = labor_hours / (crew_size * WORKING_HOURS_PER_DAY)
    return max(1, int(days + 0.999))


def _round_up_gallons(gallons: float) -> float:
    """Paint is sold in quarts; round up to the nearest quarter gallon."""
    if gallons <= 0:
        return 0.0
    return round((int(gallons * 4 - 1e-9) + 1) / 4, 2)


def _assumptions(
    job_type: JobType,
    surfaces: list[Surface],
    gallons: float,
    discount_rate: float,
) -> list[str]:
    notes = [
        f"Priced at ${LABOR_RATE_PER_HOUR:.0f}/labor-hour with "
        f"{OVERHEAD_RATE:.0%} overhead and a {TARGET_MARGIN:.0%} target margin.",
        f"{gallons:g} gallons allocated, including a 10% waste allowance.",
        "Customer clears the work area; furniture moving is not included.",
    ]
    if job_type is JobType.EXTERIOR:
        notes.append("Exterior work assumes dry weather and surface temps above 50°F.")
    if any(s.condition is SurfaceCondition.POOR for s in surfaces):
        notes.append(
            "Poor-condition surfaces carry heavy prep; hidden rot or failed "
            "substrate is billed separately once exposed."
        )
    if any(s.height_feet > HEIGHT_SURCHARGE_THRESHOLD_FT for s in surfaces):
        notes.append("Includes ladder/staging time for work above 10 feet.")
    if discount_rate:
        notes.append(f"Reflects a {discount_rate:.0%} discount.")
    return notes
