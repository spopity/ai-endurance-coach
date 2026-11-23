from core.nutrition import carbs_per_hour, fuel


def test_short_easy_needs_water_only():
    f = fuel("ride", 45, False, 70)
    assert f["carbs_per_hour"] == 0 and f["before"] == "normal meal before"
    assert f["text"] == "Fuel: normal meal before · during water only · after 20 g protein"


def test_hard_session_fuelled_before_and_after():
    f = fuel("ride", 75, True, 70)
    assert f["carbs_per_hour"] == 30
    assert f["before"] == "~105 g carbs 2-3 h before"
    assert f["after"] == "20 g protein + 70 g carbs within 1 h"


def test_long_ride_high_carb_with_fluids():
    f = fuel("ride", 180, False, 70)
    assert f["carbs_per_hour"] == 90
    assert "90 g carbs/h (~270 g) + 750-1000 ml/h with electrolytes" in f["text"]


def test_running_tolerates_less():
    assert carbs_per_hour("run", 180, False) == 75
    assert carbs_per_hour("run", 120, True) == 75
    assert carbs_per_hour("run", 70, False) == 0


def test_gym_protein_only():
    f = fuel("gym", 45, True, 70)
    assert f["text"] == "Fuel: normal meal before · after 30 g protein"


def test_unknown_weight_uses_default():
    assert fuel("ride", 45, False, None)["after"] == "20 g protein"


def test_strava_comparison_gets_the_steps_without_fuel():
    from core.nutrition import without_fuel
    desc = fuel("ride", 120, True, 70)["text"] + "\n\nWarmup\n- 15m 50-60%\n\nMain set 3x\n- 10m 95-100%"
    out = without_fuel(desc)
    assert "Fuel" not in out and "carbs" not in out
    assert out.startswith("Warmup") and "- 10m 95-100%" in out
    assert without_fuel("Amber note\nFuel: x\n\n- 5m 50%") == "Amber note\n\n- 5m 50%"
