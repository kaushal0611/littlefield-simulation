"""Post a summary of the last N hours of bot activity to Slack.

Reads logs/actions.log (committed by every bot run) and sends one message to the
incoming-webhook URL in SLACK_WEBHOOK_URL. Run by .github/workflows/slack.yml.

  python scripts/slack_summary.py            # window = summary_hours in config.yaml (default 3)
  python scripts/slack_summary.py --hours 6
  python scripts/slack_summary.py --print    # print instead of posting
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "actions.log"
IST = ZoneInfo("Asia/Kolkata")
HEAD = re.compile(
    r"^(?P<ts>\S+) day (?P<day>[\d.]+) cash \$(?P<cash>-?[\d,]+) debt \$(?P<debt>-?[\d,]+) "
    r"machines (?P<m>\{[^}]*\}) on-order (?P<oo>\{[^}]*\}) inv (?P<inv>\d+) rop/roq (?P<rop>\d+)/(?P<roq>\d+) "
    r"contract (?P<contract>\d+) wip (?P<wip>\d+) rule (?P<rule>\S+) edits=(?P<edits>\S+)")
ERR = re.compile(r"^(?P<ts>20\S+) ERROR (?P<msg>.*)")


def parse(text: str):
    """Yield runs: dict(ts, head-fields or None, lines=[indented lines], error=str|None)."""
    run = None
    for line in text.splitlines():
        m, e = HEAD.match(line), ERR.match(line)
        if m or e:
            if run:
                yield run
            ts = datetime.fromisoformat((m or e).group("ts"))
            run = dict(ts=ts, head=m.groupdict() if m else None, lines=[], error=e.group("msg") if e else None)
        elif run is not None and line.startswith("  ") and line.strip():
            run["lines"].append(line.strip())
    if run:
        yield run


def money(s) -> float:
    return float(str(s).replace(",", ""))


def machines(s: str) -> str:
    d = dict(re.findall(r"(\d+):\s*(\d+)", s))
    return "/".join(d.get(k, "?") for k in "123")


def build(runs: list[dict], hours: float, now: datetime) -> str:
    start = now - timedelta(hours=hours)
    win = [r for r in runs if start <= r["ts"] <= now]
    before = [r for r in runs if r["ts"] < start and r["head"]]
    heads = [r for r in win if r["head"]]
    t0, t1 = start.astimezone(IST).strftime("%a %H:%M"), now.astimezone(IST).strftime("%H:%M IST")
    out = [f"*Littlefield bot: last {hours:g}h* ({t0} to {t1})"]
    if not win:
        last = runs[-1]["ts"].astimezone(IST).strftime("%a %H:%M") if runs else "never"
        out.append(f":rotating_light: No bot runs in this window. Last run: {last}. "
                   "Check the Actions tab (schedule paused, PAUSE variable set, or GitHub delays).")
        return "\n".join(out)
    if heads:
        h = heads[-1]["head"]
        ref = (before[-1]["head"] if before else heads[0]["head"])
        dcash = money(h["cash"]) - money(ref["cash"])
        out.append(f"*Now:* day {float(h['day']):.1f} | cash ${money(h['cash']):,.0f} ({dcash:+,.0f}) | "
                   f"debt ${money(h['debt']):,.0f}")
        oo = machines(h["oo"])
        out.append(f"*Factory:* machines S1/S2/S3 {machines(h['m'])}" + (f" (+{oo} on order)" if oo != "0/0/0" else "")
                   + f" | contract {h['contract']} | WIP cap {h['wip']} | tester {h['rule']}"
                   + f" | kits {int(h['inv']):,} (ROP {h['rop']}, ROQ {h['roq']}) | edits {h['edits']}")
        out.append(f"*Progress:* day {float(ref['day']):.1f} to {float(h['day']):.1f} over {len(win)} runs")
        if h["edits"] == "OFF" and float(h["day"]) >= 50.5:
            out.append(":warning: edits=OFF: the bot cannot change contract/WIP. Needs a parser fix.")
        last_age = (now - heads[-1]["ts"]).total_seconds() / 60
        if last_age > 40:
            out.append(f":warning: last successful run was {last_age:.0f} min ago.")
    acts, fails, dry = [], [], 0
    for r in win:
        day = float(r["head"]["day"]) if r["head"] else None
        for l in r["lines"]:
            if l.startswith("OK"):
                acts.append(f"day {day:.1f}: {l[5:].split(' | ')[0]}")
            elif l.startswith("FAIL"):
                fails.append(f"day {day:.1f}: {l[5:][:160]}")
            elif l.startswith("DRY"):
                dry += 1
    errs = [r for r in win if r["error"]]
    if acts:
        out.append(f"*Changes made ({len(acts)}):*")
        if len(acts) > 25:
            out.append(f"- ... {len(acts) - 25} earlier changes in logs/actions.log")
        out += [f"- {a}" for a in acts[-25:]]
    else:
        out.append("*Changes made:* none" + (f" ({dry} dry-run decisions logged)" if dry else ""))
    if fails:
        out.append(f"*:warning: Failed actions ({len(fails)}):*")
        out += [f"- {f}" for f in fails[:10]]
    if errs:
        out.append(f"*:rotating_light: Errored runs ({len(errs)}):* " + errs[-1]["error"][:200])
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float)
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()
    hours = a.hours
    if hours is None:
        try:
            import yaml
            hours = float((yaml.safe_load((ROOT / "config.yaml").read_text()) or {}).get("summary_hours", 3))
        except Exception:  # noqa: BLE001
            hours = 3.0
    runs = list(parse(LOG.read_text())) if LOG.exists() else []
    msg = build(runs, hours, datetime.now(timezone.utc))
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if a.print or not url:
        print(msg)
        if not url:
            print("\n(SLACK_WEBHOOK_URL not set: printed only)")
        return
    import requests
    r = requests.post(url, json={"text": msg}, timeout=15)
    r.raise_for_status()
    print("posted:\n" + msg)


if __name__ == "__main__":
    main()
