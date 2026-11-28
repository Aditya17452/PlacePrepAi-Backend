"""Seed values for the `plans` table (PLAN.md §9).

These are the launch prices. The table is the source of truth at runtime, so changing a
price is a row update rather than a deploy — this file only seeds a fresh database.

Student prices include 18% GST. College plans are listed for reference and are granted by
an admin after an invoice is paid, not bought self-serve.
"""

from __future__ import annotations

STUDENT_PLANS = [
    {
        "code": "free",
        "audience": "student",
        "name": "Free",
        "price_paise": 0,
        "tokens": 2,
        "validity_days": None,  # lifetime
        "requires_phone_verification": True,
        "once_per_phone": True,
        "sort_order": 0,
        "notes": "Lifetime free tier. Full interviews; PDF report download is paid-only.",
    },
    {
        "code": "starter",
        "audience": "student",
        "name": "Starter",
        "price_paise": 14_900,
        "tokens": 5,
        "validity_days": 60,
        "sort_order": 1,
    },
    {
        "code": "pro",
        "audience": "student",
        "name": "Pro",
        "price_paise": 44_900,
        "tokens": 20,
        "validity_days": 180,
        "is_recommended": True,
        "sort_order": 2,
    },
    {
        "code": "placement_season",
        "audience": "student",
        "name": "Placement Season",
        "price_paise": 99_900,
        "tokens": 50,
        "validity_days": 365,
        "sort_order": 3,
    },
]

#: Plus GST; pool valid 12 months. Granted by an admin against an invoice.
#: The Rs 10 and Rs 9 tiers are seeded inactive — PLAN.md §9 holds them until the
#: measured cost per interview is below Rs 5.
COLLEGE_PLANS = [
    {
        "code": "college_pilot_free",
        "audience": "college",
        "name": "Free pilot",
        "price_paise": 0,
        "tokens": 100,
        "validity_days": 30,
        "sort_order": 10,
        "notes": "One per college.",
    },
    {
        "code": "college_pilot",
        "audience": "college",
        "name": "Pilot",
        "price_paise": 750_000,
        "tokens": 500,
        "validity_days": 365,
        "sort_order": 11,
        "notes": "Rs 15 per token.",
    },
    {
        "code": "college_campus",
        "audience": "college",
        "name": "Campus",
        "price_paise": 3_000_000,
        "tokens": 2_500,
        "validity_days": 365,
        "sort_order": 12,
        "notes": "Rs 12 per token.",
    },
    {
        "code": "college_campus_pro",
        "audience": "college",
        "name": "Campus Pro",
        "price_paise": 6_000_000,
        "tokens": 6_000,
        "validity_days": 365,
        "is_active": False,
        "sort_order": 13,
        "notes": "Rs 10 per token. Held until cost per interview is below Rs 5.",
    },
    {
        "code": "college_university",
        "audience": "college",
        "name": "University",
        "price_paise": 13_500_000,
        "tokens": 15_000,
        "validity_days": 365,
        "is_active": False,
        "sort_order": 14,
        "notes": "Custom quote, floor Rs 9 per token. Held until cost is below Rs 5.",
    },
]

ALL_PLANS = STUDENT_PLANS + COLLEGE_PLANS
