import statistics as st

from bot import guard
from lf.policy import Observation, Action, decide
from lf.sim import demand_curve
from lf.twin import run_season, observe
from lf.sim import Factory


def day50_obs(**kw):
    f = Factory(seed=0)
    f.step_until(50.0)
    o = observe(f)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_day50_first_moves():
    acts = decide(day50_obs(), {"allow_loans": True})
    kinds = {a.kind for a in acts}
    assert "buy" in kinds and "wip" in kinds and "rule" in kinds
    wip = [a for a in acts if a.kind == "wip"][0].value
    assert wip < 60                      # stop accepting jobs that can only be late
    buys = {a.station: a.value for a in acts if a.kind == "buy"}
    assert buys.get(2, 1) > 1           # tester is the bottleneck


def test_hold_mode_only_after_grace_period():
    assert decide(day50_obs(can_edit_orders=False)) == []
    acts = decide(day50_obs(can_edit_orders=False, day=56.0))
    assert {a.kind for a in acts} <= {"rop", "roq"} and any(a.value == 1 for a in acts)


def test_guard_blocks_unsafe_actions():
    o = day50_obs()
    o.on_order = {1: 0, 2: 1, 3: 0}
    acts = [Action("buy", 9, station=2), Action("sell", 0, station=1), Action("sell", 1, station=1),
            Action("buy", 30, station=1), Action("loan", 100000), Action("rop", 0)]
    out = guard(acts, o, {"max_spend_per_run": 600000, "allow_loans": False})
    assert all(a.kind != "loan" for a in out)
    assert not any(a.kind == "buy" and a.station == 2 for a in out)     # already on order
    assert not any(a.kind == "sell" and a.value < 1 for a in out)
    b1 = [a for a in out if a.kind == "buy" and a.station == 1]
    assert b1 and b1[0].value - 3 <= 8                                  # per-run cap
    assert not any(a.kind == "rop" for a in out)


def test_policy_beats_doing_nothing_by_a_mile():
    pol = [run_season(seed=s, cfg={"allow_loans": True})[0].net_cash for s in range(3)]
    nil = [run_season(seed=s, policy=False)[0].net_cash for s in range(3)]
    assert min(pol) > 1_500_000 and max(nil) < 200_000


def test_policy_robust_to_demand_shape():
    for k in (0.7, 1.3):
        fn = lambda t, k=k: demand_curve(t) * k
        r = st.mean(run_season(seed=s, cfg={"allow_loans": True}, demand_fn=fn)[0].net_cash for s in range(2))
        assert r > 1_200_000

