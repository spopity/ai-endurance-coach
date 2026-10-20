"""
One-time backfill: activities that exist only on Strava (uploaded straight from
Zwift, TrainingPeaks Virtual, old devices...) are pushed to Intervals.icu and
then imported into the DB through the normal Intervals.icu pipeline, so they
look exactly like every other activity (Intervals.icu computes load, zones,
power curve, fitness).

For each Strava activity the name sync can't match:
- it overlaps an existing activity in time -> it's the same session recorded
  twice (e.g. Garmin + Zwift): the DB row is linked to it (strava_id) and gets
  the Strava name, nothing is uploaded. If that row is already linked to another
  Strava activity, it's a duplicate on Strava itself and is skipped.
- otherwise its streams are downloaded (1 Strava read each), rebuilt into a TCX
  file and uploaded to Intervals.icu with external_id "strava_<id>"; activities
  without streams (manual entries) are created as manual activities.

Re-runnable: anything already on Intervals.icu with external_id "strava_<id>"
is not uploaded again, so after a crash or the Strava daily limit just run it
again. Strava reads are throttled to stay under the 15-minute limit.

Run:
    python scripts/backfill_strava_history.py --dry-run     # classify only (Strava list + Intervals list)
    python scripts/backfill_strava_history.py --limit 1     # pilot one upload
    python scripts/backfill_strava_history.py --notify      # everything, Telegram report at the end

The report is printed and saved to state/strava_backfill_report.txt.
"""
import argparse
import gzip
import logging
import os
import sys
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import psycopg2
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "ingest"))

from core.api_log import trigger  # noqa: E402
from core.db import load_env  # noqa: E402
from core.notify import notify_telegram  # noqa: E402
from core.strava import _request, access_token, fetch_activities, utc_ts  # noqa: E402
from import_intervals import fetch_activities as fetch_intervals, upsert_activities  # noqa: E402
from import_wellness import fetch_wellness, upsert_wellness  # noqa: E402
from sync_strava_names import load_local, match, rename_on_intervals  # noqa: E402

logger = logging.getLogger("AI_COACH_V1.strava_backfill")

INTERVALS_API = "https://intervals.icu/api/v1"
REPORT_PATH = os.path.join(ROOT, "state", "strava_backfill_report.txt")
STREAM_KEYS = "time,latlng,distance,altitude,heartrate,cadence,watts"
# Strava's read limit is 100 per 15 min; keep headroom for the other jobs.
READS_PER_WINDOW = 85
BIKE_TYPES = {"Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide", "EMountainBikeRide",
              "Velomobile", "Handcycle"}
RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}


def intervals(env: dict, method: str, path: str, **kwargs) -> requests.Response:
    for attempt in range(5):
        resp = requests.request(method, f"{INTERVALS_API}{path}", auth=("API_KEY", env["INTERVALS_API_KEY"]),
                                timeout=120, **kwargs)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        time.sleep(10 * (attempt + 1))
    resp.raise_for_status()


class StravaThrottle:
    """Sleeps until the next 15-minute window (Strava's windows start at :00/:15/:30/:45) when it's used up."""

    def __init__(self):
        self.used = 0
        self.window = self._window()

    @staticmethod
    def _window() -> int:
        return int(time.time()) // 900

    def wait(self):
        if self._window() != self.window:
            self.window, self.used = self._window(), 0
        if self.used >= READS_PER_WINDOW:
            pause = (self.window + 1) * 900 - time.time() + 5
            logger.info(f"Strava read budget for this window used - sleeping {pause / 60:.1f} min")
            time.sleep(max(pause, 0))
            self.window, self.used = self._window(), 0
        self.used += 1

    def note_headers(self, headers):
        usage = headers.get("x-readratelimit-usage")
        if usage:
            self.used = max(self.used, int(usage.split(",")[0]))


# --- classification ---

def classify(conn, unmatched: list[dict], pushed: dict[str, str]) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, external_id, name, start_time, elapsed_time_sec, strava_id "
                    "FROM activities WHERE source = 'intervals_icu';")
        rows = [{"id": r[0], "external_id": r[1], "name": r[2], "start": r[3].timestamp(),
                 "end": r[3].timestamp() + (r[4] or 0), "strava_id": r[5]} for r in cur.fetchall()]

    groups = {"link": [], "duplicate": [], "already_pushed": [], "push": []}
    linked_rows = set()
    for s in unmatched:
        start = utc_ts(s["start_date"])
        end = start + (s.get("elapsed_time") or 0)
        overlap = max((r for r in rows if r["start"] < end and start < r["end"]),
                      key=lambda r: min(end, r["end"]) - max(start, r["start"]), default=None)
        if str(s["id"]) in pushed:
            groups["already_pushed"].append(s)
        elif overlap and not overlap["strava_id"] and overlap["id"] not in linked_rows:
            groups["link"].append((s, overlap))
            linked_rows.add(overlap["id"])
        elif overlap:
            groups["duplicate"].append((s, overlap))
        else:
            groups["push"].append(s)
    return groups


# --- Strava streams -> TCX ---

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_tcx(s: dict, streams: dict) -> bytes:
    start = utc_ts(s["start_date"])
    sport_type = s.get("sport_type") or s.get("type")
    sport = "Biking" if sport_type in BIKE_TYPES else "Running" if sport_type in RUN_TYPES else "Other"

    def col(key):
        return streams.get(key, {}).get("data") or []

    t, latlng, dist, alt, hr, cad, watts = (col(k) for k in
                                           ("time", "latlng", "distance", "altitude", "heartrate", "cadence", "watts"))

    def at(series, i):
        return series[i] if i < len(series) else None

    points = []
    for i, sec in enumerate(t):
        p = [f"<Trackpoint><Time>{_iso(start + sec)}</Time>"]
        if (ll := at(latlng, i)) and ll[0] is not None:
            p.append(f"<Position><LatitudeDegrees>{ll[0]}</LatitudeDegrees>"
                     f"<LongitudeDegrees>{ll[1]}</LongitudeDegrees></Position>")
        if (v := at(alt, i)) is not None:
            p.append(f"<AltitudeMeters>{v}</AltitudeMeters>")
        if (v := at(dist, i)) is not None:
            p.append(f"<DistanceMeters>{v}</DistanceMeters>")
        if (v := at(hr, i)):
            p.append(f"<HeartRateBpm><Value>{int(v)}</Value></HeartRateBpm>")
        if (v := at(cad, i)) is not None and sport != "Running":
            p.append(f"<Cadence>{min(int(v), 254)}</Cadence>")
        ext = []
        if (v := at(watts, i)) is not None:
            ext.append(f"<ns3:Watts>{int(v)}</ns3:Watts>")
        if (v := at(cad, i)) is not None and sport == "Running":
            ext.append(f"<ns3:RunCadence>{min(int(v), 254)}</ns3:RunCadence>")
        if ext:
            p.append(f"<Extensions><ns3:TPX>{''.join(ext)}</ns3:TPX></Extensions>")
        p.append("</Trackpoint>")
        points.append("".join(p))

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
        'xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">'
        f'<Activities><Activity Sport="{sport}"><Id>{_iso(start)}</Id>'
        f'<Lap StartTime="{_iso(start)}"><TotalTimeSeconds>{s.get("elapsed_time") or 0}</TotalTimeSeconds>'
        f'<DistanceMeters>{s.get("distance") or 0}</DistanceMeters><Calories>0</Calories>'
        '<Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod>'
        f'<Track>{"".join(points)}</Track></Lap>'
        f'<Notes>{escape(s.get("name") or "")}</Notes></Activity></Activities></TrainingCenterDatabase>'
    )
    return gzip.compress(xml.encode())


# --- pushing ---

def push_one(env: dict, token: str, throttle: StravaThrottle, s: dict) -> tuple[str, str]:
    """Uploads one Strava activity to Intervals.icu. Returns (intervals_id, how)."""
    throttle.wait()
    try:
        resp = _request("GET", f"/activities/{s['id']}/streams", token,
                        params={"keys": STREAM_KEYS, "key_by_type": "true"})
        throttle.note_headers(resp.headers)
        streams = resp.json() if resp.content else {}
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        streams = {}  # Strava has no streams for it - create it as a manual activity
    if not isinstance(streams, dict):  # key_by_type should give a dict; be safe
        streams = {x["type"]: x for x in streams}
    sport_type = s.get("sport_type") or s.get("type")
    external_id = f"strava_{s['id']}"

    if streams.get("time", {}).get("data"):
        resp = intervals(env, "POST", f"/athlete/{env['INTERVALS_ATHLETE_ID']}/activities",
                         params={"name": s.get("name") or "", "external_id": external_id},
                         files={"file": (f"strava_{s['id']}.tcx.gz", build_tcx(s, streams))})
        icu_id = str(resp.json()["id"])
        how = "file"
    else:
        resp = intervals(env, "POST", f"/athlete/{env['INTERVALS_ATHLETE_ID']}/activities/manual", json={
            "start_date_local": s["start_date_local"].rstrip("Z"),
            "type": sport_type, "name": s.get("name"), "external_id": external_id,
            "moving_time": s.get("moving_time"), "elapsed_time": s.get("elapsed_time"),
            "distance": s.get("distance"), "total_elevation_gain": s.get("total_elevation_gain"),
        })
        icu_id = str(resp.json()["id"])
        how = "manual"

    # The file only says Biking/Running/Other - set the exact Strava type and flags.
    intervals(env, "PUT", f"/activity/{icu_id}", json={
        "type": sport_type, "trainer": bool(s.get("trainer")), "commute": bool(s.get("commute")),
    })
    return icu_id, how


def link_one(conn, env: dict, s: dict, row: dict):
    name = (s.get("name") or "").strip()
    if name and name != row["name"]:
        rename_on_intervals(env, row["external_id"], name)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE activities
            SET strava_id = %s,
                name_before_strava = CASE WHEN %s <> '' AND %s IS DISTINCT FROM name
                                          THEN COALESCE(name_before_strava, name) ELSE name_before_strava END,
                name = COALESCE(NULLIF(%s, ''), name), updated_at = now()
            WHERE id = %s;
        """, (str(s["id"]), name, name, name, row["id"]))
    conn.commit()


def refresh_db(conn, env: dict, strava_ids: dict[str, str]) -> dict:
    """Re-imports the full Intervals.icu history: adds the new activities and refreshes every
    activity's fitness snapshot and the wellness CTL/ATL, which change once older load is added."""
    days = (datetime.now().date() - datetime(2000, 1, 1).date()).days
    remote = fetch_intervals(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"], days)
    inserted, updated, failed = upsert_activities(conn, remote)
    with conn.cursor() as cur:
        for icu_id, strava_id in strava_ids.items():
            cur.execute("UPDATE activities SET strava_id = %s WHERE source = 'intervals_icu' AND external_id = %s;",
                        (strava_id, icu_id))
    conn.commit()
    wellness = fetch_wellness(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"], days)
    w_inserted, w_updated, w_failed = upsert_wellness(conn, wellness)
    return {"remote": len(remote), "inserted": inserted, "updated": updated, "failed": failed,
            "wellness": len(wellness), "wellness_failed": w_failed}


# --- report ---

def describe(s: dict) -> str:
    return f"{s['start_date'][:10]} {s.get('sport_type') or s.get('type')} - {s.get('name')} (strava {s['id']})"


def build_report(total_strava: int, groups: dict, pushed_now: list, failed: list, stopped: str | None,
                 refresh: dict | None, after: dict | None, dry_run: bool) -> str:
    lines = [f"Strava backfill {datetime.now():%Y-%m-%d %H:%M}{' (DRY RUN)' if dry_run else ''}", ""]
    lines.append(f"Strava activities: {total_strava}; not in the DB: "
                 f"{sum(len(v) for v in groups.values())}")
    lines.append(f"  to upload to Intervals.icu: {len(groups['push'])}")
    lines.append(f"  already uploaded by an earlier run: {len(groups['already_pushed'])}")
    lines.append(f"  same session as an existing activity -> linked: {len(groups['link'])}")
    lines.append(f"  duplicates on Strava itself -> skipped: {len(groups['duplicate'])}")
    if not dry_run:
        files = sum(1 for _, _, how in pushed_now if how == "file")
        lines += ["", f"Uploaded now: {len(pushed_now)} ({files} with full data, {len(pushed_now) - files} manual)",
                  f"Failed: {len(failed)}"]
        for s, err in failed:
            lines.append(f"  FAILED {describe(s)}: {err[:200]}")
        if stopped:
            lines.append(f"Stopped early: {stopped} - run the script again to continue.")
    if groups["link"]:
        lines += ["", "Linked (same session, not uploaded):"]
        lines += [f"  {describe(s)} = DB {row['external_id']} {row['name']!r}" for s, row in groups["link"]]
    if groups["duplicate"]:
        lines += ["", "Skipped duplicates (the DB row is already linked to another Strava activity):"]
        lines += [f"  {describe(s)} = DB {row['external_id']} {row['name']!r}" for s, row in groups["duplicate"]]
    if refresh:
        lines += ["", f"DB refresh from Intervals.icu: {refresh['remote']} activities "
                      f"({refresh['inserted']} new, {refresh['updated']} refreshed, {len(refresh['failed'])} failed), "
                      f"{refresh['wellness']} wellness days refreshed"]
        for rid, err in refresh["failed"][:10]:
            lines.append(f"  failed to map {rid}: {err.splitlines()[0][:200]}")
    if after:
        lines += ["", f"Check afterwards: Strava {after['strava']}, DB {after['local']}, matched {after['matched']}, "
                      f"still unmatched {len(after['unmatched'])}"]
        lines += [f"  {describe(s)}" for s in after["unmatched"][:30]]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push Strava-only activities to Intervals.icu and the DB (one-time).")
    parser.add_argument("--dry-run", action="store_true", help="classify and report only")
    parser.add_argument("--limit", type=int, help="upload at most this many (pilot)")
    parser.add_argument("--notify", action="store_true", help="send the report to Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    env = load_env()
    conn = psycopg2.connect(env["DATABASE_URL"])
    token = access_token(env)
    throttle = StravaThrottle()
    try:
        strava = fetch_activities(token)
        throttle.used += len(strava) // 200 + 1
        _, unmatched = match(strava, load_local(conn))

        days = (datetime.now().date() - datetime(2000, 1, 1).date()).days + 1
        on_intervals = fetch_intervals(env["INTERVALS_ATHLETE_ID"], env["INTERVALS_API_KEY"], days)
        pushed = {str(a["external_id"]).removeprefix("strava_"): str(a["id"]) for a in on_intervals
                  if str(a.get("external_id") or "").startswith("strava_")}
        groups = classify(conn, unmatched, pushed)
        logger.info({k: len(v) for k, v in groups.items()})

        pushed_now, failed, stopped, refresh, after = [], [], None, None, None
        if not args.dry_run:
            for s, row in groups["link"]:
                link_one(conn, env, s, row)

            todo = groups["push"][:args.limit] if args.limit else groups["push"]
            for n, s in enumerate(todo, 1):
                try:
                    icu_id, how = push_one(env, token, throttle, s)
                except RuntimeError as e:  # Strava rate limit (core.strava raises on 429)
                    stopped = str(e)
                    break
                except Exception as e:
                    logger.warning(f"Failed {describe(s)}: {e}")
                    failed.append((s, str(e)))
                    continue
                pushed[str(s["id"])] = icu_id
                pushed_now.append((s, icu_id, how))
                logger.info(f"[{n}/{len(todo)}] {describe(s)} -> {icu_id} ({how})")
                if n % 50 == 0:
                    token = access_token(env)  # refreshes if the 6h token is about to expire

            time.sleep(30)  # let Intervals.icu finish recomputing fitness before re-importing
            refresh = refresh_db(conn, env, {icu: sid for sid, icu in pushed.items()})
            _, still = match(strava, load_local(conn))
            after = {"strava": len(strava), "local": len(load_local(conn)), "matched": len(strava) - len(still),
                     "unmatched": still}
    except Exception as e:
        logger.error(f"Strava backfill failed: {e}", exc_info=True)
        if args.notify:
            notify_telegram(env, f"⚠️ Strava backfill failed: {e}")
        return 2
    finally:
        conn.close()

    report = build_report(len(strava), groups, pushed_now, failed, stopped, refresh, after, args.dry_run)
    print(report)
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report + "\n")
    if args.notify:
        notify_telegram(env, f"📥 {report}")
    return 1 if failed or stopped else 0


if __name__ == "__main__":
    with trigger("strava_backfill"):  # tags this run's calls on the System page
        sys.exit(main())
