"""Decision policy. Pure function of an Observation -> list of Actions.

Same code runs against the live game (via lf.client) and the digital twin
(via lf.sim), so what we test offline is exactly what ships.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .sim import CONTRACTS, PRICES, SETUP, PER_UNIT, CALIB, revenue

GAME_END = 300


@dataclass
class Observation:
    day: float
    cash: float
    machines: dict            # {1: n, 2: n, 3: n} installed
    on_order: dict            # {1: n, 2: n, 3: n} machines bought, not yet arrived
    inventory: int
    kits_on_order: int        # 0 if none outstanding
    rop: int
    roq: int
    kit_unit: float
    kit_order_cost: float
    contract: int
    lots_per_job: int
    wip_limit: int
    rule: str
    series: dict              # name -> {day: value}; JOBIN, JOBOUT, S1Q, S2Q, S3Q, JOBQ
    can_edit_orders: bool = True   # contract / lot size / WIP editable?
    debt: float = 0.0


@dataclass
class Action:
    kind: str     # buy | sell | rop | roq | contract | wip | rule | lots | loan
    value: object
    station: int | None = None
    reason: str = ""

    def __str__(self):
        st = f" S{self.station}" if self.station else ""
        return f"{self.kind}{st} -> {self.value}  ({self.reason})"


DEFAULTS = dict(
    util_target=0.90,          # planned utilisation at forecast demand
    flush_days=6.0,            # clear existing backlog within this many days
    plan_ahead=7.0,            # days of demand beyond the 3-day machine lead time
    min_payback_mult=1.3,      # machine must earn >= this x its net cost before day 300
    safety_days=1.0,           # kit safety stock, in days of demand
    holding_per_kit_day=0.098,
    cash_buffer=20_000,
    max_spend_per_run=600_000,
    max_machines=25,
    rule="pri4",
    enable_c3=False,
    c2_switch_lt=1.0,          # switch to c2 once a new job's predicted lead time fits the 1-day quote
    c2_revert_lt=1.8,          # back to c1 if lead time blows out (c2 earns < c1 past ~1.5d)
    wip_days=0.6,              # WIP cap = throughput x this many days
    min_wip=8,
    sell_from_day=200,
    allow_loans=False,
    max_debt=600_000,
    low_frac_prior=0.6,        # prior: post-day-180 demand as share of peak
    low_rate=None,             # optional override of the measured post-peak level (jobs/day)
    low_end=240,               # handout: lower plateau lasts ~1 month after day 180, then falls to ~0 by day 300
    hold_after_day=55,
    no_edit_mode="hold",       # if contract/WIP can't be edited: "hold" cash or "invest" anyway
    contract_mode="c2",        # "c2": c1 -> c2 as before | "best": pick the contract paying most at the predicted lead time
    lots_target=None,          # e.g. 3 = split each job into 3 lots of 20 (None: keep current)
    lead_wip_mult=1.3,         # "best" mode: WIP cap = throughput x no-wait flow time x this
    strategy="c2",             # "c2": adaptive contract-2 plan | "c3plan": split lots, size for peak, contract 3
    c3_lots=3,                 # c3plan: lots per job once the old backlog is gone
    c3_util=1.0,               # c3plan: size machines so peak demand runs at this utilisation
    c3_wip=12,                 # c3plan: WIP cap (jobs) while on contract 3
    c3_fallback_hours=8.0,     # c3plan: if real c3 lead times average above this, use contract 2 instead
    late_c3_day=None,          # from this day (demand falling, spare capacity): split lots + contract 3
    late_lots=2,
    late_wip_days=0.3,         # WIP cap = throughput x this while on late contract 3
    extra_machines=None,       # e.g. {1: 1}: keep this many machines above the plan at a station (until day 150)
    cash_floor=0,              # borrow (within max_debt) to keep at least this much cash for kit orders
    c3_wait_for_fleet=False,   # c3plan: only switch to contract 3 once no machines are still on order
    loan_from_day=0,           # no borrowing before this day
    repay_from_day=0,          # no repayment before this day (999 = hold debt to the end)
)


def hours_per_job(station: int, lots: int = 1) -> float:
    """Machine-hours one 60-kit job needs at a station (both test passes at S2)."""
    size = 60 // lots
    per_lot = SETUP[station] + PER_UNIT[station] * size
    h = per_lot * lots * CALIB[station]
    return h * 2 if station == 2 else h


def _ceil60(x):
    return int(max(60, math.ceil(x / 60.0) * 60))


# ---------------------------------------------------------------- demand
class Forecaster:
    def __init__(self, obs: Observation, cfg: dict):
        self.cfg = cfg
        s = obs.series
        jobin = s.get("JOBIN", {})
        wip_est = {}
        for d in jobin:
            q = sum(s.get(f"S{k}Q", {}).get(d, 0.0) for k in (1, 2, 3)) / 60.0
            wip_est[d] = q + s.get("JOBQ", {}).get(d, 0.0)
        cap = obs.wip_limit
        # a day is censored if the factory sat near its WIP cap (orders rejected)
        self.clean = {d: v for d, v in jobin.items() if wip_est.get(d, 0) < 0.85 * cap and d >= 1}
        pts = [(d, v) for d, v in self.clean.items() if d <= 95]
        if len(pts) >= 8:
            n = len(pts)
            mx = sum(d for d, _ in pts) / n
            my = sum(v for _, v in pts) / n
            sxx = sum((d - mx) ** 2 for d, _ in pts)
            b = sum((d - mx) * (v - my) for d, v in pts) / sxx if sxx else 0.0
            self.a, self.b = my - b * mx, max(0.0, b)
        else:
            self.a, self.b = 3.69, 0.521
        self.peak = self.a + self.b * 90
        self.day = obs.day
        recent = [v for d, v in self.clean.items() if obs.day - 10 <= d <= obs.day]
        self.recent_mean = sum(recent) / len(recent) if len(recent) >= 5 else None
        # plateau observed after day 100
        plateau = [v for d, v in self.clean.items() if 100 <= d <= 180]
        if len(plateau) >= 7:
            self.peak = sum(plateau) / len(plateau)
        low = [v for d, v in self.clean.items() if 185 <= d <= cfg["low_end"]]
        self.low = sum(low) / len(low) if len(low) >= 5 else self.peak * cfg["low_frac_prior"]
        if cfg.get("low_rate"):
            self.low = float(cfg["low_rate"])   # manual override of the post-peak demand level (jobs/day)

    def rate(self, t: float) -> float:
        if t <= 90:
            r = self.a + self.b * t
        elif t <= 180:
            r = self.peak
        elif t <= self.cfg["low_end"]:
            r = self.low
        else:
            r = self.low * max(0.0, (GAME_END - t) / (GAME_END - self.cfg["low_end"]))
        # if we have clean recent data near t, trust it over the curve
        if self.recent_mean is not None and abs(t - self.day) <= 3 and t > 90:
            r = self.recent_mean
        return max(0.0, r)

    def max_rate(self, t0, t1):
        if t1 <= t0:
            return 0.0 if t0 >= GAME_END else self.rate(t0)
        return max(self.rate(t0 + i * 0.5) for i in range(int((t1 - t0) / 0.5) + 1))


# ---------------------------------------------------------------- policy
def decide(obs: Observation, cfg: dict | None = None) -> list[Action]:
    cfg = {**DEFAULTS, **(cfg or {})}
    acts: list[Action] = []
    if not obs.can_edit_orders and cfg["no_edit_mode"] == "hold":
        # give the game a few days to unlock order edits before giving up on them
        return _hold_mode(obs) if obs.day >= cfg["hold_after_day"] else []
    f = Forecaster(obs, cfg)
    d = obs.day
    late = cfg["late_c3_day"] is not None and obs.day >= cfg["late_c3_day"] and obs.can_edit_orders
    if late:
        cfg = {**cfg, "strategy": "c3plan", "c3_lots": cfg["late_lots"], "c3_util": cfg["util_target"]}
    c3plan = cfg["strategy"] == "c3plan" and obs.can_edit_orders
    if c3plan:
        cfg = {**cfg, "util_target": cfg["c3_util"], "contract_mode": "c3plan"}
    lots = obs.lots_per_job
    if cfg["lots_target"] and obs.can_edit_orders and lots != cfg["lots_target"]:
        lots = int(cfg["lots_target"])
        acts.append(Action("lots", lots, reason="config lots_target"))
    h = {k: hours_per_job(k, lots) for k in (1, 2, 3)}
    if c3plan:      # size capacity for the split-lot factory from the start
        h_plan = {k: hours_per_job(k, cfg["c3_lots"]) for k in (1, 2, 3)}
    u = cfg["util_target"]
    s = obs.series

    def last(name):
        ser = s.get(name, {})
        return ser[max(ser)] if ser else 0.0

    q = {k: last(f"S{k}Q") for k in (1, 2, 3)}
    wip_jobs = (q[1] + q[2] + q[3]) / 60.0 + last("JOBQ")
    # backlog work by station (machine-hours)
    j1, j2, j3 = q[1] / 60, q[2] / 60, q[3] / 60
    backlog_h = {
        1: j1 * h[1],
        2: (j1 * 2 + j2 * 1.4 + j3) * h[2] / 2,
        3: (j1 + j2 * 0.75 + j3) * h[3],
    }

    # ---- contract + WIP cap (Little's law: lead time = WIP / throughput)
    contract = obs.contract
    eff = {k: obs.machines[k] + obs.on_order.get(k, 0) for k in (1, 2, 3)}
    mu = min(eff[k] * 24 / h[k] for k in (1, 2, 3))            # jobs/day once orders land
    mu_now = min(obs.machines[k] * 24 / h[k] for k in (1, 2, 3))
    flow = sum(h.values()) / 24                                  # no-wait flow time, days
    lt_new = max(flow, wip_jobs / max(mu_now, 0.1))
    if c3plan:
        lt3s = sorted((dd, v) for dd, v in s.get("LT3", {}).items() if v > 0)
        lt3 = [v for dd, v in lt3s if dd >= lt3s[0][0] + 4][-3:] if lt3s else []   # skip start-up days
        measured = sum(lt3) / len(lt3) if len(lt3) >= 3 else None
        c3_ok = measured is None or measured * 24 <= cfg["c3_fallback_hours"]
        flushed = lt_new <= 1.0
        lam_l = f.max_rate(d + 3.0, GAME_END) if d + 3.0 < GAME_END else 0.0
        need_c3 = {k: max(1, math.ceil(lam_l * h_plan[k] / (24 * cfg["util_target"]) - 1e-9) - 1) for k in (1, 2, 3)}
        fleet_ready = not cfg["c3_wait_for_fleet"] or (not any(obs.on_order.get(k, 0) for k in (1, 2, 3))
                                                       and all(obs.machines[k] >= need_c3[k] for k in (1, 2, 3)))
        if flushed and lots != cfg["c3_lots"] and (fleet_ready or contract == 3):
            acts.append(Action("lots", cfg["c3_lots"], reason="fleet ready: split jobs for contract 3"))
            lots = cfg["c3_lots"]
        want_c = (3 if c3_ok else 2) if flushed and (fleet_ready or contract == 3) else contract
        c3cap = int(max(cfg["min_wip"] // 2, round(mu_now * cfg["late_wip_days"]))) if late else cfg["c3_wip"]
        cap = (c3cap if want_c == 3 else int(max(cfg["min_wip"], round(mu_now * cfg["wip_days"])))) \
            if flushed else int(max(cfg["min_wip"], round(mu_now * cfg["wip_days"])))
        exact = flushed and want_c == 3 and not late          # fixed c3 cap: apply it exactly
        if (cap != obs.wip_limit) if exact else abs(cap - obs.wip_limit) >= max(2, 0.12 * obs.wip_limit):
            acts.append(Action("wip", cap, reason=f"c3plan, contract {want_c}"))
        if want_c != contract:
            why = "backlog gone, split lots fit the 6h quote" if want_c == 3 else \
                f"real c3 lead {measured * 24:.1f}h > {cfg['c3_fallback_hours']}h: c2 pays more"
            acts.append(Action("contract", want_c, reason=why))
            contract = want_c
    elif obs.can_edit_orders and cfg["contract_mode"] == "best":
        # no-wait flow time when the lots of a job run in parallel (one lot's path through all steps)
        size = 60 // lots
        lot_flow = sum((SETUP[k] + PER_UNIT[k] * size) * CALIB[k] * (2 if k == 2 else 1) for k in (1, 2, 3)) / 24
        cap = int(max(cfg["min_wip"], round(mu_now * lot_flow * cfg["lead_wip_mult"])))
        if lt_new > 1.0:                       # backlog not flushed yet: c2-style cap while it drains
            cap = int(max(cfg["min_wip"], round(mu_now * cfg["wip_days"])))
        if abs(cap - obs.wip_limit) >= max(2, 0.12 * obs.wip_limit):
            acts.append(Action("wip", cap, reason=f"μ {mu_now:.1f}/d, lot flow {lot_flow * 24:.1f}h"))
        pred = max(lot_flow * cfg["lead_wip_mult"], wip_jobs / max(mu_now, 0.1)) if lt_new > 1.0 else \
            max(lot_flow, cap / max(mu_now, 0.1))
        best = max((1, 2, 3), key=lambda c: revenue(c, pred))
        if best != contract:
            contract = best
            acts.append(Action("contract", best, reason=f"pred lead {pred * 24:.1f}h pays most on c{best}"))
    elif obs.can_edit_orders:
        # cap WIP so a new job's lead time stays inside the c2 quote. When demand
        # beats capacity this rejects the excess instead of letting every job go late.
        cap = int(max(cfg["min_wip"], round(mu_now * cfg["wip_days"])))
        if abs(cap - obs.wip_limit) >= max(2, 0.12 * obs.wip_limit):
            acts.append(Action("wip", cap, reason=f"μ {mu_now:.1f} jobs/d x {cfg['wip_days']}d"))
        if contract != 2 and lt_new <= cfg["c2_switch_lt"]:
            contract = 2
            acts.append(Action("contract", 2, reason=f"backlog flushed, pred lead {lt_new:.2f}d"))
        elif contract == 2 and lt_new > cfg["c2_revert_lt"]:
            contract = 1
            acts.append(Action("contract", 1, reason=f"pred lead {lt_new:.2f}d, c1 pays more"))
    target_contract = (3 if c3plan else contract if cfg["contract_mode"] == "best" else 2) \
        if obs.can_edit_orders else contract
    margin = CONTRACTS[target_contract]["rev"] - 60 * obs.kit_unit - 60 * 1.5 * cfg["holding_per_kit_day"]

    # ---- capacity
    t0 = d + 3.0
    lam_plan = f.max_rate(t0, min(GAME_END, t0 + cfg["plan_ahead"]))
    lam_life = f.max_rate(t0, GAME_END) if t0 < GAME_END else 0.0
    rem = max(0.0, GAME_END - t0)
    spend_left = min(cfg["max_spend_per_run"],
                     obs.cash - (obs.roq * obs.kit_unit + obs.kit_order_cost) - cfg["cash_buffer"])
    want = {}
    for k in (1, 2, 3):
        if c3plan and not late:
            want[k] = max(1, min(cfg["max_machines"], math.ceil(lam_life * h_plan[k] / (24 * u) - 1e-9)))
            continue
        steady = lam_plan * h[k] / (24 * u)
        flush = backlog_h[k] / (24 * cfg["flush_days"]) / u
        need = math.ceil(steady + flush - 1e-9)
        life_need = math.ceil(lam_life * h[k] / (24 * u) + 1e-9)
        need = min(need, life_need + 1, cfg["max_machines"])
        want[k] = max(1, need)

    # payback gate: a machine only pays if it serves enough jobs before day 300
    def pays(k):
        if not obs.can_edit_orders:
            return True   # no WIP cap to shed excess demand: any shortage makes every job late
        price, salvage = PRICES[k]
        per_day = (24 * u / (h_plan[k] if c3plan else h[k])) * margin
        return per_day * rem >= cfg["min_payback_mult"] * (price - salvage)

    if cfg["extra_machines"] and d < 150:
        for k, n in cfg["extra_machines"].items():
            want[int(k)] = min(cfg["max_machines"], want[int(k)] + int(n))
    buys = []
    shortfall = {k: want[k] - eff[k] for k in (1, 2, 3)}
    if c3plan and not late and cfg["allow_loans"] and cfg["loan_from_day"] <= d < 150:
        # borrow first, so the whole fleet can be bought in one go (machines take 3 days either way)
        cost = sum(PRICES[k][0] * max(0, shortfall[k]) for k in (1, 2, 3)
                   if pays(k) and obs.on_order.get(k, 0) == 0)          # only what can be bought this run
        reserve = 60 * lam_plan * 6 * obs.kit_unit + obs.kit_order_cost + 5 * cfg["cash_buffer"]   # ~6 days of kits
        loan = min(cost + reserve - obs.cash, cfg["max_debt"] - obs.debt)
        if loan >= 50_000 and cost > 0:      # kit top-ups are left to cash_floor (each loan costs a 5% fee)
            acts.append(Action("loan", int(loan), reason=f"fund c3 fleet ${cost:,.0f}"))
            spend_left = min(cfg["max_spend_per_run"], obs.cash + loan - reserve)
    # buy one at a time on the most loaded station, so a cash limit hits the
    # least critical station last
    alloc = {k: 0 for k in (1, 2, 3)}
    while True:
        cands = [k for k in (1, 2, 3) if shortfall[k] - alloc[k] > 0 and pays(k) and obs.on_order.get(k, 0) == 0]
        if not cands:
            break
        k = max(cands, key=lambda k: (lam_plan * h[k] + backlog_h[k] / cfg["flush_days"]) /
                (24 * (eff[k] + alloc[k])))
        price = PRICES[k][0]
        if spend_left < price:
            break
        alloc[k] += 1
        spend_left -= price
    for k in (1, 2, 3):
        if alloc[k] and obs.on_order.get(k, 0) == 0:
            buys.append(Action("buy", obs.machines[k] + alloc[k], station=k,
                               reason=f"want {want[k]} (λplan {lam_plan:.1f}/d, backlog {backlog_h[k]:.0f}h)"))
    acts += buys
    unfunded = sum(PRICES[k][0] * (shortfall[k] - alloc[k]) for k in (1, 2, 3)
                   if shortfall[k] - alloc[k] > 0 and pays(k))
    if c3plan and any(a.kind == "loan" for a in acts):
        pass
    elif cfg["allow_loans"] and obs.can_edit_orders and unfunded > 0 and cfg["loan_from_day"] <= d < 150 \
            and obs.debt < cfg["max_debt"] and not c3plan:
        amt = min(unfunded * 1.05, cfg["max_debt"] - obs.debt)
        if amt >= 50_000:
            acts.append(Action("loan", int(amt), reason=f"unfunded high-payback capacity ${unfunded:,.0f}"))
    elif obs.debt > 0 and unfunded == 0 and d >= cfg["repay_from_day"] and \
            not (c3plan and any(shortfall[k] > 0 and pays(k) for k in (1, 2, 3))):   # fleet still being built
        free = obs.cash - (obs.roq * obs.kit_unit + obs.kit_order_cost) * 2 - cfg["cash_buffer"] * 3
        if c3plan:   # keep a kit war-chest; repay only real surplus, in big chunks
            free = obs.cash - (60 * lam_plan * 6 * obs.kit_unit + obs.kit_order_cost) - 100_000
            free = free if free > 100_000 else 0
        if cfg["cash_floor"]:   # never repay below floor + 2 kit orders, so we don't borrow it back (5% fee)
            free = min(free, obs.cash - cfg["cash_floor"] - 2 * (obs.roq * obs.kit_unit + obs.kit_order_cost))
            free = free if free > 50_000 else 0
        if free > 20_000:
            acts.append(Action("repay", int(min(free, obs.debt)), reason="debt costs 20%, cash earns 10%"))

    if cfg["cash_floor"] and cfg["allow_loans"] and obs.can_edit_orders and cfg["loan_from_day"] <= d < 200 \
            and not any(a.kind in ("loan", "repay") for a in acts):
        top_up = min(cfg["cash_floor"] - obs.cash, cfg["max_debt"] - obs.debt)
        if top_up >= 50_000:
            acts.append(Action("loan", int(top_up), reason=f"keep ${cfg['cash_floor']:,.0f} cash for kit orders"))
    # ---- sell surplus late in the game
    if d >= cfg["sell_from_day"]:
        for k in (1, 2, 3):
            keep = 1 if d >= 296 else max(1, math.ceil(lam_life * h[k] / (24 * u) + 1e-9) + 1)
            if obs.machines[k] > keep and obs.on_order.get(k, 0) == 0 and backlog_h[k] < 24:
                acts.append(Action("sell", keep, station=k,
                                   reason=f"life demand needs {keep - 1}+1 spare"))

    # ---- tester rule
    if obs.rule != cfg["rule"]:
        acts.append(Action("rule", cfg["rule"], station=2, reason="config"))

    # ---- kits
    D = 60 * min(f.max_rate(d, d + 5), mu)              # kits/day we will actually consume
    if obs.can_edit_orders and wip_jobs > 0:
        # while the WIP cap is binding we accept roughly what we ship
        pass
    S = obs.kit_order_cost
    hold = cfg["holding_per_kit_day"]
    eoq = math.sqrt(2 * D * S / hold) if D > 0 else 60
    lead = 4.0
    rop = _ceil60((lead + cfg["safety_days"]) * D)
    roq = _ceil60(max(eoq, (lead + 0.5) * D))
    # endgame: never buy kits we can't turn into revenue
    need_rest = sum(60 * min(f.rate(t + 0.5), mu) for t in range(int(d), GAME_END - 1))
    have = obs.inventory + obs.kits_on_order
    endgame = obs.rop <= 60 and d > 200
    if have >= need_rest or (endgame and have >= 0.8 * need_rest):
        rop = 60
        roq = 60
    else:
        roq = min(roq, _ceil60(need_rest - have))
    borrowed = sum(a.value for a in acts if a.kind == "loan") - sum(a.value for a in acts if a.kind == "repay")
    afford = int((obs.cash + borrowed - cfg["cash_buffer"] - sum(PRICES[a.station][0] * (a.value - obs.machines[a.station])
                                                                 for a in buys)) / obs.kit_unit)
    roq = max(60, min(roq, (afford // 60) * 60))
    if _changed(obs.rop, rop):
        acts.append(Action("rop", rop, reason=f"D {D:.0f} kits/d, lead 4d + {cfg['safety_days']}d safety"))
    if _changed(obs.roq, roq, 0.3 if roq > obs.roq else 0.15):
        acts.append(Action("roq", roq, reason=f"EOQ {eoq:.0f}, cover {lead + 0.5}d"))
    return acts


def _changed(old, new, tol=0.15):
    if old == new:
        return False
    if new <= 60 or old <= 60:
        return True
    return abs(new - old) > max(tol * old, 600)


def _hold_mode(obs: Observation) -> list[Action]:
    """Contract and WIP locked: every job finishes late and earns ~$0 while each
    still costs $600 of kits. Stop feeding the factory and keep the cash."""
    acts = []
    if obs.rop != 1:
        acts.append(Action("rop", 1, reason="orders not editable: starve kits, hold cash"))
    if obs.roq != 60:
        acts.append(Action("roq", 60, reason="orders not editable: minimum kit order"))
    return acts
