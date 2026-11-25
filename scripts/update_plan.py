"""
Recomputes the adaptive training plan (core/periodization.py) from the training
actually done and rewrites training_blocks, then measures the thresholds
(core/thresholds.py: LT1 from DFA-alpha1, critical speed) and runs the progress check
(core/progress.py: markers, verdicts, a new stimulus where progress stalled). Then: the
Withings weigh-ins, the low-energy check (core/energy.py), the goals (core/goals.py), the
markers snapshot and the adjustment log (core/adjustments.py), and on the first night of a
quarter the quarterly review.
Runs nightly after the Intervals.icu sync; safe to run any time.

Run: python scripts/update_plan.py [--dry-run] [--notify]
  --dry-run  print the replay and the projection, write nothing
  --notify   send a Telegram message when this week's phase changed, or a stimulus
             started or ended
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.db import get_connection, load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from core.periodization import compute_plan, update_plan  # noqa: E402
from core import adjustments, energy, goals, progress, thresholds  # noqa: E402


def label(r: dict | None) -> str:
    return (r["phase"] + (" (deload)" if r["is_deload"] and r["phase"] != "Recovery" else "")) if r else "no plan"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.dry_run:
            rows = compute_plan(conn)
        else:
            result = update_plan(conn)
            rows = result["rows"]
    finally:
        conn.close()

    for r in rows:
        actual = "" if r["actual_load"] is None else f"{r['actual_load']:>4}"
        print(f"{r['start_date']}  {r['phase']:8} {r['target_weekly_load_min']:>4}-{r['target_weekly_load_max']:<4} "
              f"{actual:>4}  {r['status'] or '':10} {r['notes']}")
    if args.dry_run:
        return

    before, now = result["before"], result["now"]
    print(f"\nThis week: {label(before)} -> {label(now)} ({now['notes']})")
    if before and (before["phase"], before["is_deload"]) != (now["phase"], now["is_deload"]):
        adjustments.log("plan", f"this week {label(before)} -> {label(now)}", now["notes"])
    if args.notify and before and (before["phase"], before["is_deload"]) != (now["phase"], now["is_deload"]):
        notify_telegram(load_env(), (
            f"📅 Training plan adapted: this week is now {label(now)} (was {label(before)}).\n"
            f"Why: {now['notes']}.\n"
            f"Target {now['target_weekly_load_min']:.0f}-{now['target_weekly_load_max']:.0f} TSS. {now['primary_focus']}"))

    try:
        before_th = thresholds._read(thresholds.STATE)
        th = thresholds.update()
        print("\n" + thresholds.summary(th))
        if msg := thresholds.changes(before_th, th):
            adjustments.log("thresholds", msg.split("\n")[0][:200])
            if args.notify:
                notify_telegram(load_env(), msg)
    except Exception as e:
        print(f"\nThreshold update failed: {e}")

    nightly_extras(args.notify)

    try:
        result = progress.update()
    except Exception as e:
        print(f"\nProgress check failed: {e}")
        return
    for e in result["ended"]:
        adjustments.log("stimulus", f"{e['sport']} stimulus ended: {e['stimulus']}", f"result {e['result']}")
    for e in result["started"]:
        adjustments.log("stimulus", f"{e['sport']} stimulus started until {e['until']}: {e['stimulus']}", e["why"])
    print("\n" + progress.markdown(result["report"]))
    messages = []
    for e in result["ended"]:
        messages.append(f"🔁 Stimulus ended ({e['sport']}): {progress.STIMULUS_TEXT[e['stimulus']]} - result: "
                        f"{e['result'].replace('_', ' ')}.")
    for e in result["started"]:
        messages.append(f"📈 Progress stalled ({e['sport']}), changing the stimulus until {e['until']}: "
                        f"{progress.STIMULUS_TEXT[e['stimulus']]}.\nWhy: {e['why']}")
    if args.notify and messages:
        notify_telegram(load_env(), "\n\n".join(messages))


ENERGY_STATE = os.path.join(ROOT, "state", "energy.json")


def nightly_extras(notify: bool):
    """Weigh-ins, the low-energy check, the goals, the markers snapshot - each on its own, so one
    failing doesn't stop the others."""
    send = (lambda text: notify_telegram(load_env(), text)) if notify else (lambda text: None)
    try:
        sys.path.insert(0, os.path.join(ROOT, "ingest"))
        from import_withings import sync_weight
        print(f"\nWithings: {sync_weight(30)}")
    except Exception as e:
        print(f"\nWithings sync failed: {e}")
    try:
        en = energy.current()
        print(en["text"])
        try:
            with open(ENERGY_STATE) as f:
                was = json.load(f).get("level")
        except (OSError, ValueError):
            was = None
        if en["level"] == "watch" and was != "watch":
            adjustments.log("energy", en["text"])
            send(f"🍝 {en['text']}.\nThe daily carbs in the 7-day plan moved to the top of their range.")
        elif was == "watch" and en["level"] == "ok":
            adjustments.log("energy", "low-energy flag cleared", en["text"])
            send(f"✅ {en['text']} - the low-energy flag is cleared.")
        with open(ENERGY_STATE, "w") as f:
            json.dump({"level": en["level"]}, f)
    except Exception as e:
        print(f"\nLow-energy check failed: {e}")
    try:
        st, msg = goals.update()
        print(goals.summary(st))
        if msg:
            adjustments.log("goal", msg.split("\n")[0][:200])
            send(msg)
    except Exception as e:
        print(f"\nGoals failed: {e}")
    try:
        adjustments.snapshot()
        if review := adjustments.quarterly():
            send(review)
    except Exception as e:
        print(f"\nMarkers / review failed: {e}")


if __name__ == "__main__":
    main()
