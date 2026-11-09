"""Score a policy config on the digital twin.

    python scripts/evaluate.py                 # current config.yaml
    python scripts/evaluate.py util_target=0.85 wip_days=0.8
"""
import statistics as st
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from lf.twin import run_season  # noqa: E402

cfg = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text())
pol = dict(cfg.get("policy", {}), allow_loans=cfg.get("allow_loans", False))
for kv in sys.argv[1:]:
    k, v = kv.split("=")
    pol[k] = yaml.safe_load(v)
r = [run_season(seed=s, cfg=pol)[0].net_cash for s in range(8)]
n = [run_season(seed=s, policy=False)[0].net_cash for s in range(8)]
print(f"policy  mean ${st.mean(r):,.0f}  worst ${min(r):,.0f}  best ${max(r):,.0f}")
print(f"nothing mean ${st.mean(n):,.0f}")
