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


STATION = {1: "stuffers", 2: "testers", 3: "tuners"}
CONTRACT = {
    "1": "$750 per job delivered within 3 days, falling to $0 if it takes 6+ days",
    "2": "$1,000 per job delivered within 1 day, falling to $0 if it takes 3+ days",
    "3": "$1,500 per job delivered within 6 hours, falling to $0 if it takes 12+ hours",
}
RULE = {"fifo": "first come, first served", "pri2": "first test of each job goes first",
        "pri4": "final test of each job goes first (finishes jobs sooner)"}


def stats_of(run) -> dict:
    for l in run["lines"]:
        if l.startswith("stats:"):
            return {k: v for k, v in re.findall(r"(\w+)=([-\d.]+)", l)}
    return {}


def plain(act: str) -> str:
    """'buy S1 -> 4  (reason)' -> plain English."""
    m = re.match(r"(\w+)(?: S(\d))? -> (\S+)", act)
    if not m:
        return act
    kind, st, v = m.group(1), m.group(2), m.group(3)
    name = STATION.get(int(st), "station " + st) if st else ""
    def num(x):
        try:
            return f"{int(float(x)):,}"
        except ValueError:
            return x
    return {
        "buy": f"Bought machines: {name} now {v} (new ones start working in 3 days)",
        "sell": f"Sold machines: {name} now {v}",
        "wip": f"WIP cap set to {v} jobs",
        "contract": f"Switched to contract {v}: {CONTRACT.get(v, '')}",
        "lots": f"Each job now split into {v} lots",
        "rule": f"Tester priority set to {v} ({RULE.get(v, '')})",
        "rop": f"Kit reorder point set to {num(v)} kits",
        "roq": f"Kit order size set to {num(v)} kits",
        "loan": f"Borrowed ${num(v)}",
        "repay": f"Repaid ${num(v)} of the loan",
    }.get(kind, act)


def build(runs: list[dict], hours: float, now: datetime) -> str:
    start = now - timedelta(hours=hours)
    win = [r for r in runs if start <= r["ts"] <= now]
    before = [r for r in runs if r["ts"] < start and r["head"]]
    heads = [r for r in win if r["head"]]
    t0, t1 = start.astimezone(IST).strftime("%a %H:%M"), now.astimezone(IST).strftime("%H:%M IST")
    out = [f"*Littlefield factory update: last {hours:g}h* ({t0} to {t1})"]
    if not win:
        last = runs[-1]["ts"].astimezone(IST).strftime("%a %H:%M") if runs else "never"
        out.append(f":rotating_light: *The bot did not run in this window.* Last run: {last}. "
                   "Check the Actions tab on GitHub (schedule delayed, or PAUSE variable set).")
        return "\n".join(out)
    if heads:
        h = heads[-1]["head"]
        ref = before[-1]["head"] if before else heads[0]["head"]
        day, day0 = float(h["day"]), float(ref["day"])
        cash, debt = money(h["cash"]), money(h["debt"])
        dcash = cash - money(ref["cash"])
        out += ["", "*Where we stand*",
                f"- Game day *{day:.1f}* of 300 (1 game day = about 29 real minutes). "
                f"Moved {day - day0:+.1f} days in this window.",
                f"- Net cash *${cash - debt:,.0f}* (cash on hand ${cash:,.0f} minus debt ${debt:,.0f}). "
                "This is what the leaderboard ranks, and our final score on day 300.",
                f"- Cash on hand changed {dcash:+,.0f} in this window."]
        if debt:
            out.append("- Loans cost 20% a year plus a 5% fee; the bot repays when cash allows.")
        # bot status on its own line
        if h["edits"] == "on":
            out.append(":white_check_mark: *Bot status:* connected and able to change settings.")
        else:
            out.append(":warning: *Bot status: edits OFF.* The bot can read the factory but cannot change the "
                       "contract or WIP cap. It needs a fix.")
        age = (now - heads[-1]["ts"]).total_seconds() / 60
        if age > 40:
            out.append(f":warning: Last successful bot run was {age:.0f} minutes ago.")
        m = dict(re.findall(r"(\d+):\s*(\d+)", h["m"]))
        oo = dict(re.findall(r"(\d+):\s*(\d+)", h["oo"]))
        mach = ", ".join(f"{m.get(k, '?')} {STATION[int(k)]}" + (f" (+{oo[k]} arriving)" if oo.get(k, "0") != "0" else "")
                         for k in "123")
        stt = stats_of(heads[-1])
        lots = stt.get("lots", "1").split(".")[0]
        inv, rop, roq = int(h["inv"]), int(h["rop"]), int(h["roq"])
        out += ["", "*Factory setup*",
                f"- *Machines:* {mach}. Every job goes stuffer, then tester, then tuner, then tester again.",
                f"- *Contract {h['contract']}:* customers pay {CONTRACT.get(h['contract'], '?')}.",
                f"- *WIP cap {h['wip']}:* at most {h['wip']} jobs are allowed inside the factory at once; "
                "orders beyond that are turned away. A lower cap keeps delivery fast.",
                f"- *Lot size:* each 60-kit job moves through the factory in {lots} lot(s).",
                f"- *Tester priority:* {h['rule']} ({RULE.get(h['rule'], '')}).",
                f"- *Kits (raw material, 60 per job):* {inv:,} in stock (about {inv // 60} jobs). When stock falls to "
                f"{rop:,} the factory orders {roq:,} more; they cost about $10 each plus a fixed fee per order and "
                "arrive 4 days later."]
        if stt:
            f = lambda k, fmt="{:.1f}": fmt.format(float(stt[k])) if k in stt else "?"
            out += ["", "*How the factory ran on the latest game day*",
                    f"- Jobs arrived: {f('jobs_in')} | jobs completed: {f('jobs_out')} | "
                    f"average delivery time: {f('lead', '{:.2f}')} days | revenue per job: ${f('rev', '{:,.0f}')}",
                    f"- Kits waiting at stuffers / testers / tuners: {f('q1', '{:,.0f}')} / {f('q2', '{:,.0f}')} / "
                    f"{f('q3', '{:,.0f}')}",
                    "- Machine busy time (1.00 = 100%): stuffers " + f('u1', '{:.2f}') + ", testers "
                    + f('u2', '{:.2f}') + ", tuners " + f('u3', '{:.2f}')]
    acts, fails, dry = [], [], 0
    for r in win:
        day = float(r["head"]["day"]) if r["head"] else 0.0
        for l in r["lines"]:
            if l.startswith("OK"):
                acts.append(f"day {day:.1f}: {plain(l[5:].split(' | ')[0])}")
            elif l.startswith("FAIL"):
                fails.append(f"day {day:.1f}: {plain(l[5:].split(' | ')[0])}: {l.split(' | ')[-1][:120]}")
            elif l.startswith("DRY"):
                dry += 1
    errs = [r for r in win if r["error"]]
    out.append("")
    if acts:
        out.append(f"*What the bot changed ({len(acts)})*")
        if len(acts) > 25:
            out.append(f"- ... {len(acts) - 25} earlier changes are in logs/actions.log")
        out += [f"- {a}" for a in acts[-25:]]
    else:
        out.append("*What the bot changed:* nothing in this window" + (f" ({dry} test decisions logged)" if dry else ""))
    if fails:
        out.append(f"*:warning: Changes that failed ({len(fails)})*")
        out += [f"- {x}" for x in fails[:10]]
    if errs:
        out.append(f"*:rotating_light: Bot runs that errored ({len(errs)}):* " + errs[-1]["error"][:200])
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
