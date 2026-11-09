"""One bot run: log in, read the factory, decide, act, log, log out.

Run by GitHub Actions on a schedule. Safe to run as often as you like:
every action is computed from the live state, so repeated runs converge.

Env:
  LF_ID, LF_PASSWORD     team login (repo secrets)
  LF_BASE                default https://op2.responsive.net
  LF_INSTITUTION         default iimb
  DRY_RUN=1              decide and log, change nothing
  PAUSE=1                skip the run entirely (kill switch, set as a repo variable)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lf.client import LittlefieldClient, GameError
from lf.policy import Observation, decide, PRICES

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "state.json"
LOG = ROOT / "logs" / "actions.log"


def load_cfg():
    p = ROOT / "config.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"runs": 0, "actions": [], "last_day": None}


def save_state(st):
    STATE.parent.mkdir(exist_ok=True)
    st["actions"] = st["actions"][-500:]
    STATE.write_text(json.dumps(st, indent=1))


def to_obs(snap) -> Observation:
    series = {}
    for k in ("JOBIN", "JOBQ", "S1Q", "S2Q", "S3Q"):
        series[k] = {int(round(x)): y for x, y in snap.series.get(k, {}).items()}
    out = {}
    for k, v in snap.series.items():
        if k.startswith("JOBOUT"):
            for x, y in v.items():
                out[int(round(x))] = out.get(int(round(x)), 0) + y
    series["JOBOUT"] = out
    can_edit = all(k in snap.controls for k in ("contract", "wip"))
    return Observation(
        day=snap.day, cash=snap.cash, machines=snap.machines, on_order=snap.on_order,
        inventory=snap.inventory, kits_on_order=snap.kits_on_order, rop=snap.rop, roq=snap.roq,
        kit_unit=snap.kit_unit, kit_order_cost=snap.kit_order_cost, contract=snap.contract,
        lots_per_job=snap.lots_per_job, wip_limit=snap.wip_limit, rule=snap.rule, series=series,
        can_edit_orders=can_edit, debt=snap.debt)


def stats(snap) -> str:
    """Latest daily values from the game's plots, as key=value tokens for the Slack summary."""
    def last(prefix, agg=None):
        keys = [k for k in snap.series if k == prefix or k.startswith(prefix + "_")]
        pts = [(x, y) for k in keys for x, y in snap.series[k].items()]
        if not pts:
            return None
        xm = max(x for x, _ in pts)
        ys = [y for x, y in pts if x == xm]
        return sum(ys) if agg == "sum" else ys[-1]
    vals = dict(jobs_in=last("JOBIN"), jobs_out=last("JOBOUT", "sum"), lead=last("JOBT"), rev=last("JOBREV"),
                jobq=last("JOBQ"), q1=last("S1Q"), q2=last("S2Q"), q3=last("S3Q"),
                u1=last("S1UTIL"), u2=last("S2UTIL"), u3=last("S3UTIL"))
    out = [f"lots={snap.lots_per_job}"]
    out += [f"{k}={v:.2f}" for k, v in vals.items() if v is not None]
    return " ".join(out)


def guard(acts, obs, cfg, state=None):
    """Hard limits that hold whatever the policy says."""
    ok, spend = [], 0
    cap = cfg.get("max_spend_per_run", 600_000)
    for a in acts:
        if a.kind == "buy":
            n = a.value - obs.machines[a.station]
            if n <= 0 or obs.on_order.get(a.station, 0) > 0:
                continue
            n = min(n, cfg.get("max_buy_per_station_per_run", 8), int((cap - spend) // PRICES[a.station][0]))
            if n <= 0:
                continue
            a.value = obs.machines[a.station] + n
            spend += n * PRICES[a.station][0]
        if a.kind == "sell":
            if a.value < 1 or a.value >= obs.machines[a.station]:
                continue
            if obs.machines[a.station] - a.value > cfg.get("max_sell_per_run", 3):
                a.value = obs.machines[a.station] - cfg.get("max_sell_per_run", 3)
        if a.kind == "loan" and not cfg.get("allow_loans", False):
            continue
        if a.kind in ("rop", "roq") and a.value < 1:
            continue
        ok.append(a)
    return ok


def execute(client, snap, a):
    c = snap.controls
    if a.kind in ("buy", "sell"):
        return client.submit(c[f"count{a.station}"], a.value)
    if a.kind in ("rop", "roq", "contract", "wip", "rule", "lots"):
        if a.kind not in c:
            raise GameError(f"no '{a.kind}' control on the page")
        return client.submit(c[a.kind], a.value)
    if a.kind in ("loan", "repay"):
        if not snap.loan_url:
            raise GameError("loan form not found on CashStatus")
        return client.loan(snap.loan_url, a.value, increase=(a.kind == "loan"))
    raise GameError("unknown action " + a.kind)


def summary(lines):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a") as f:
            f.write("\n".join(lines) + "\n")


def alert(lines, st, cfg):
    """Post to Slack right away for problems and big moves. Problems repeat at most once an hour."""
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return
    import requests
    head = lines[0] if lines else ""
    day = st.get("last_day") or 0
    problems = []
    err = next((l for l in lines if l.startswith("20") and " ERROR " in l), None)
    if err:
        problems.append(("error", ":rotating_light: bot run ERROR: " + err[:300]))
    fails = [l.strip() for l in lines if l.strip().startswith("FAIL")]
    if fails:
        problems.append(("fail", ":warning: action failed:\n" + "\n".join(fails[:5])))
    if "edits=OFF" in head and day >= cfg.get("alert_edits_off_after_day", 50.5):
        problems.append(("editsoff", f":warning: day {day:.1f}: bot can't edit contract/WIP (edits=OFF). Kit orders are "
                         f"cut from day {cfg.get('policy', {}).get('hold_after_day', 55)} if this persists."))
    sent = st.setdefault("alerts", {})
    now = time.time()
    for kind, text in problems:
        if now - sent.get(kind, 0) >= 3600:
            requests.post(url, json={"text": text}, timeout=10)
            sent[kind] = now
    big = [l.strip()[5:] for l in lines if l.strip().startswith("OK") and l.strip()[5:].startswith(("sell", "loan", "repay", "contract"))]
    if big:
        requests.post(url, json={"text": f":information_source: day {day:.1f}: " + "; ".join(big)}, timeout=10)


def main(client=None, now=None) -> int:
    if os.environ.get("PAUSE", "").strip() in ("1", "true", "yes"):
        print("PAUSE set, skipping run")
        return 0
    cfg = load_cfg()
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes") or cfg.get("dry_run", False)
    st = load_state()
    st["runs"] += 1
    ts = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    client = client or LittlefieldClient(os.environ["LF_ID"], os.environ["LF_PASSWORD"],
                                         base=os.environ.get("LF_BASE", "https://op2.responsive.net"),
                                         institution=os.environ.get("LF_INSTITUTION", "iimb"))
    lines = []
    rc = 0
    try:
        client.login()
        snap = client.snapshot()
        obs = to_obs(snap)
        head = (f"{ts} day {snap.day:.1f} cash ${snap.cash:,.0f} debt ${snap.debt:,.0f} "
                f"machines {snap.machines} on-order {snap.on_order} inv {snap.inventory} "
                f"rop/roq {snap.rop}/{snap.roq} contract {snap.contract} wip {snap.wip_limit} "
                f"rule {snap.rule} edits={'on' if obs.can_edit_orders else 'OFF'}")
        lines.append(head)
        try:
            lines.append("  stats: " + stats(snap))
        except Exception as e:  # noqa: BLE001
            lines.append(f"  stats: unavailable ({e})")
        if not obs.can_edit_orders or not snap.loan_url:
            # keep the live pages so a parser mismatch can be fixed without logging in
            LOG.parent.mkdir(exist_ok=True)
            for k, h in (snap.raw or {}).items():
                (LOG.parent / f"debug_{k}.html").write_text(h)
            lines.append(f"  debug: order controls {sorted(k for k in snap.controls if k in ('contract','wip','lots'))}, "
                         f"loan form {'found' if snap.loan_url else 'missing'}; pages saved to logs/debug_*.html")
        if snap.day >= cfg.get("stop_after_day", 299.5):
            lines.append("  game over or about to end: no actions")
        else:
            acts = guard(decide(obs, cfg.get("policy", {}) | {"allow_loans": cfg.get("allow_loans", False)}), obs, cfg, st)
            for a in acts:
                if dry:
                    lines.append(f"  DRY  {a}")
                    continue
                try:
                    res = execute(client, snap, a)
                    lines.append(f"  OK   {a} | {res}")
                    st["actions"].append({"ts": ts, "day": snap.day, "act": str(a)})
                except GameError as e:
                    lines.append(f"  FAIL {a} | {e}")
                    rc = 2
            if not acts:
                lines.append("  no changes needed")
        st["last_day"] = snap.day
        st["last"] = head
    except Exception as e:  # noqa: BLE001
        lines.append(f"{ts} ERROR {e}")
        lines.append(traceback.format_exc())
        rc = 1
    finally:
        client.quit()
    print("\n".join(lines))
    try:
        alert(lines, st, cfg)
    except Exception as e:  # noqa: BLE001  (alerts must never break a run)
        print("slack alert failed:", e)
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write("\n".join(lines) + "\n")
    summary(lines)
    save_state(st)
    return rc


if __name__ == "__main__":
    sys.exit(main())
