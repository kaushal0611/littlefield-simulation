"""Adapter so the policy can drive the digital twin exactly like the live game."""
from __future__ import annotations

from .policy import Observation, Action, decide
from .sim import Factory


def observe(f: Factory, can_edit_orders: bool = True) -> Observation:
    L = f.log
    series = {k: dict(L[k]) for k in ("JOBIN", "JOBOUT", "S1Q", "S2Q", "S3Q")}
    series["JOBQ"] = {d: 0.0 for d in L["JOBIN"]}
    return Observation(
        day=f.day, cash=f.cash, machines=dict(f.machines),
        on_order={k: f.machines_on_order(k) for k in (1, 2, 3)},
        inventory=f.inventory, kits_on_order=f.kit_order_qty if f.kit_order_due else 0,
        rop=f.rop, roq=f.roq, kit_unit=f.kit_unit, kit_order_cost=f.kit_order_cost,
        contract=f.contract, lots_per_job=f.lots_per_job, wip_limit=f.wip_limit,
        rule=f.rule, series=series, can_edit_orders=can_edit_orders, debt=f.debt)


def apply(f: Factory, acts: list[Action]):
    for a in acts:
        if a.kind == "buy":
            f.buy(a.station, a.value - f.machines[a.station])
        elif a.kind == "sell":
            f.buy(a.station, a.value - f.machines[a.station])
        elif a.kind == "rop":
            f.rop = a.value
        elif a.kind == "roq":
            f.roq = a.value
        elif a.kind == "contract":
            f.contract = a.value
        elif a.kind == "wip":
            f.wip_limit = a.value
        elif a.kind == "rule":
            f.rule = a.value
        elif a.kind == "loan":
            f.borrow(a.value)
        elif a.kind == "repay":
            f.repay(a.value)
        elif a.kind == "lots":
            f.lots_per_job = a.value
    f._check_reorder()


def run_season(seed=0, cfg=None, policy=True, cadence=0.5, can_edit_orders=True, start=50.0, end=300.0,
               demand_fn=None, verbose=False):
    kw = dict(seed=seed)
    if demand_fn:
        kw["demand_fn"] = demand_fn
    f = Factory(**kw)
    f.step_until(start)
    t = start
    log = []
    while t < end - 1e-9:
        if policy:
            acts = decide(observe(f, can_edit_orders), cfg)
            apply(f, acts)
            if verbose and acts:
                for a in acts:
                    log.append(f"d{t:6.1f} {a}")
        t = min(end, t + cadence)
        f.step_until(t)
    f.step_until(end + 5)  # game runs a few extra days after the freeze
    return f, log
