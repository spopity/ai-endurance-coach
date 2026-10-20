"""
What a gym session trained, from its title. The athlete names each session after the
muscles worked (e.g. "Chest + arms", "Core", "Legs"), so only a title that names the legs
counts as leg work - anything else is treated as not loading the legs at all.

Kept free of other core imports: core.metrics and core.plan both use it.
"""
import re

WEIGHTS_TYPES = {"WeightTraining", "Workout"}
LEG_WORDS = re.compile(r"\b(legs?|lower[\s-]?body|full[\s-]?body|squats?|deadlifts?|lunges?)\b", re.IGNORECASE)


def gym_focus(activity_type: str | None, name: str | None) -> str | None:
    """For gym sessions, what the title says was trained - None for other sports."""
    if activity_type not in WEIGHTS_TYPES:
        return None
    if LEG_WORDS.search(name or ""):
        return "legs worked (leg fatigue for the next 1-2 days)"
    return "upper body/core only - legs NOT loaded, no effect on leg freshness"
