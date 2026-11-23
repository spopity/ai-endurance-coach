"""LT1 from DFA-alpha1, Zone 2 from LT1 and critical speed (core/thresholds.py)."""
import random

from core import thresholds as T


def test_dfa_a1_separates_noise_from_correlated_rhythm():
    rnd = random.Random(1)
    white = [800 + rnd.gauss(0, 20) for _ in range(300)]
    walk, x = [], 800.0
    for _ in range(300):
        x += rnd.gauss(0, 5)
        walk.append(x)
    assert 0.3 < T.dfa_a1(white) < 0.7       # uncorrelated: ~0.5 (hard exercise)
    assert T.dfa_a1(walk) > 1.2              # strongly correlated: ~1.5 (rest, easy)


def curve(points: dict[float, float], n: int = 6) -> list[tuple[float, float]]:
    return [(x, a) for x, a in points.items() for _ in range(n)]


def test_lt1_is_where_the_binned_alpha1_crosses_075():
    hr, why = T.crossing(curve({126: 1.3, 134: 1.1, 142: 0.95, 150: 0.85, 158: 0.65, 166: 0.5}), T.HR_BIN)
    assert why == "ok" and 152 < hr < 158


def test_rebound_at_high_heart_rate_is_unreliable():
    # Wrist RR / cadence coupling: alpha1 climbs back over 0.9 at high heart rates.
    hr, why = T.crossing(curve({126: 1.1, 134: 0.9, 138: 0.72, 146: 0.8, 150: 1.0, 158: 1.05}), T.HR_BIN)
    assert hr is None and "climbs back" in why


def test_no_lt1_without_easy_or_hard_stretches():
    assert T.crossing(curve({140: 0.9, 146: 0.8, 150: 0.7, 158: 0.6}), T.HR_BIN)[0] is None
    assert T.crossing(curve({120: 1.4, 126: 1.3, 132: 1.2, 138: 1.1}), T.HR_BIN)[1] == "no stretches hard enough to cross LT1"


def test_zone_2_top_follows_lt1_within_bounds():
    assert T.z2_top(300, 252) == 0.798        # 95% of LT1 power
    assert T.z2_top(300, None) == 0.75        # classic Zone 2 without LT1
    assert T.z2_top(300, 290) == 0.82 and T.z2_top(300, 150) == 0.70


def test_critical_speed_from_the_3_and_9_minute_efforts():
    assert T.critical_speed(1000, 2700) == {"cs": 4.722, "d_prime": 150}
    assert T.critical_speed(900, 2700) is None          # D' implausible: not two real all-out efforts
    assert T.pace(4.722) == "3:32"


def test_distance_at_interpolates_the_pace_curve():
    assert T._distance_at([800, 1000, 1200], [160, 200, 250], 180) == 900


def test_changes_announce_a_new_zone_2_and_a_cs_test():
    before = {"ftp": 300, "ride": {"watts": None}, "cs": {"source": "training"}}
    after = {"ftp": 300, "ride": {"watts": 252}, "cs": {"source": "test", "day": "2026-12-08", "pace": "4:10",
                                                       "d_prime": 180}}
    msg = T.changes(before, after)
    assert "Zone 2 now goes up to 80% FTP (was 75%)" in msg and "4:10/km" in msg
    assert T.changes(after, after) is None
    bad = {"ftp": 300, "ride": {"watts": 252}, "cs": {"source": "training", "test_unusable": "2026-12-08"}}
    assert "no usable critical speed" in T.changes(after, bad)


def test_run_windows_locked_to_the_step_rate_are_dropped():
    assert T.coupled([155, 1.05, None, 81])          # 162 steps/min, heart rate 155
    assert not T.coupled([135, 0.8, None, 81])
    assert not T.coupled([155, 1.05, 250, None])     # rides: no cadence check


def test_run_lt1_far_below_the_bike_is_not_trusted():
    def acts(points):
        return [{"day": "2026-11-0%d" % i, "windows": [[x, a, None, 90] for x, a in points.items()] * 2}
                for i in range(1, 5)]
    pts = {126: 1.3, 130: 1.2, 134: 0.95, 138: 0.7, 142: 0.6}
    assert T.lt1(acts(pts), "run", bike_hr=158)["status"] == "unreliable"
    assert T.lt1(acts(pts), "run", bike_hr=138)["hr"] == 137


def test_first_indoor_ride_with_rr_is_announced():
    before = {"indoor_rr": {"with": 0, "without": 12, "last_with": None}}
    after = {"indoor_rr": {"with": 1, "without": 12, "last_with": "2027-02-02"}}
    assert "Indoor rides now carry beat-to-beat" in T.changes(before, after)
    assert T.changes(after, after) is None
