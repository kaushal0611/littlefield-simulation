"""The twin must reproduce the live game's first 50 days before we trust it."""
import json
import statistics as st
from pathlib import Path

from lf.sim import Factory

H = json.loads((Path(__file__).parents[1] / "data" / "history_day50.json").read_text())["series"]


def test_twin_matches_day50_state():
    lt, s2q, out, util3 = [], [], [], []
    for seed in range(6):
        f = Factory(seed=seed)
        f.step_until(50.0)
        L = f.log
        lt.append(L["JOBT"][50])
        s2q.append(L["S2Q"][50])
        out.append(sum(L["JOBOUT"][d] for d in range(41, 51)) / 10)
        util3.append(sum(L["S3UTIL"][d] for d in range(41, 51)) / 10)
    obs_out = sum(H["JOBOUT_c1"][40:50]) / 10
    obs_u3 = sum(H["S3UTIL"][40:50]) / 10
    assert abs(st.mean(lt) - H["JOBT_c1"][49]) / H["JOBT_c1"][49] < 0.15
    assert abs(st.mean(s2q) - H["S2Q"][49]) / H["S2Q"][49] < 0.15
    assert abs(st.mean(out) - obs_out) / obs_out < 0.15
    assert abs(st.mean(util3) - obs_u3) < 0.08
