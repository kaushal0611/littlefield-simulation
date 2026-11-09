"""Discrete-event digital twin of the Littlefield factory.

Used three ways:
  1. calibration check against the 50-day history (tests/test_sim_calibration.py)
  2. offline policy tuning (scripts/tune.py)
  3. the engine behind the mock server used for end-to-end tests

Time unit is days. Processing times are in hours in the params and converted.
"""
from __future__ import annotations

import heapq
from collections import deque
import math
import random
from dataclasses import dataclass, field

# Handout values (hours). CALIB scales them to match the live server's
# behaviour on days 1-50 (grid search on lead time, queues, throughput).
SETUP = {1: 1.4, 2: 0.0, 3: 1.5}
PER_UNIT = {1: 0.0625, 2: 0.025, 3: 0.002}
CALIB = {1: 1.0, 2: 1.03, 3: 1.0}  # fitted to days 1-50, see tests/test_sim_calibration.py

CONTRACTS = {
    1: dict(quoted=3.0, maxlt=6.0, rev=750.0),
    2: dict(quoted=1.0, maxlt=3.0, rev=1000.0),
    3: dict(quoted=0.25, maxlt=0.5, rev=1500.0),
}
PRICES = {1: (90_000, 9_000), 2: (80_000, 8_000), 3: (100_000, 10_000)}
LOTS_ALLOWED = (1, 2, 3, 5, 10)


def revenue(contract: int, lead_days: float) -> float:
    c = CONTRACTS[contract]
    if lead_days <= c["quoted"]:
        return c["rev"]
    if lead_days >= c["maxlt"]:
        return 0.0
    frac = (lead_days - c["quoted"]) / (c["maxlt"] - c["quoted"])
    return c["rev"] * (1 - frac)


def demand_curve(t: float, a: float = 3.69, b: float = 0.521, low_frac: float = 0.6) -> float:
    """Expected orders/day. Linear growth to day 90, flat to 180,
    step down to a lower plateau until 240, then linear fall to ~0 at 300."""
    peak = a + b * 90
    if t <= 90:
        return max(0.0, a + b * t)
    if t <= 180:
        return peak
    low = peak * low_frac
    if t <= 240:
        return low
    return max(0.0, low * (300 - t) / 60)


@dataclass
class Lot:
    job: "Job"
    size: int
    step: int = 1  # 1,2,3,4
    t_queue: float = 0.0


@dataclass
class Job:
    jid: int
    t_arr: float
    contract: int
    lots_left: int


@dataclass
class Factory:
    seed: int = 1
    day: float = 0.0
    cash: float = 1_000_000.0
    machines: dict = field(default_factory=lambda: {1: 3, 2: 1, 3: 1})
    rule: str = "fifo"  # fifo | pri2 | pri4
    contract: int = 1
    lots_per_job: int = 1
    wip_limit: int = 300
    inventory: int = 9600
    rop: int = 1500
    roq: int = 7200
    kit_unit: float = 10.0
    kit_order_cost: float = 1000.0
    kit_lead: float = 4.0
    holding_per_kit_day: float = 0.098
    interest_rate: float = 0.10
    debt: float = 0.0
    debt_rate: float = 0.20
    loan_fee: float = 0.05
    demand_fn: object = None
    calib: dict = field(default_factory=lambda: dict(CALIB))

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.events: list = []
        self.seq = 0
        # station 2 keeps step-2 and step-4 lots apart so priority rules are O(1)
        self.queues = {1: deque(), 2: deque(), 3: deque(), 4: deque()}
        self.qk = {1: 0, 2: 0, 3: 0}
        self.busy = {1: 0, 2: 0, 3: 0}
        self.busy_time = {1: 0.0, 2: 0.0, 3: 0.0}
        self.kit_order_due: float | None = None
        self.kit_order_qty = 0
        self.waiting_jobs: list[Job] = []  # accepted, waiting for kits
        self.jobs_in_system = 0
        self.next_jid = 0
        self.pending_machines: list = []  # (due_day, station, n)
        # daily logs, index = day number (1-based like the game)
        self.log = {k: {} for k in ("JOBIN", "JOBOUT", "JOBT", "JOBREV", "S1Q", "S2Q", "S3Q",
                                     "S1UTIL", "S2UTIL", "S3UTIL", "INV", "CASH", "REJECT", "ARRIVALS")}
        self._day_acc = self._new_acc()
        self.holding_paid = 0.0
        self.revenue_total = 0.0
        self.last_bookkeep = 0.0
        if self.demand_fn is None:
            self.demand_fn = demand_curve
        self._schedule(self._next_arrival(0.0), "arrival", None)
        self._schedule(1.0, "eod", None)

    # ---------- infrastructure ----------
    def _new_acc(self):
        return dict(jobin=0, jobout=0, lt=[], rev=[], qarea={1: 0.0, 2: 0.0, 3: 0.0}, reject=0, arrivals=0)

    def _schedule(self, t, kind, payload):
        self.seq += 1
        heapq.heappush(self.events, (t, self.seq, kind, payload))

    def _next_arrival(self, t):
        # thinning on the demand curve
        lam_max = 80.0
        while True:
            t += self.rng.expovariate(lam_max)
            if t > 320:
                return float("inf")
            if self.rng.random() < self.demand_fn(t) / lam_max:
                return t

    def _qkits(self, s):
        return self.qk[s]

    def _enqueue(self, s, lot):
        self.qk[s] += lot.size
        lot.t_queue = self.day
        if s == 2 and lot.step == 4:
            self.queues[4].append(lot)
        else:
            self.queues[s].append(lot)

    def _qlen(self, s):
        return len(self.queues[s]) + (len(self.queues[4]) if s == 2 else 0)

    def _advance_areas(self, t_new):
        dt = t_new - self.day
        if dt <= 0:
            return
        for s in (1, 2, 3):
            self._day_acc["qarea"][s] += self._qkits(s) * dt
            self.busy_time[s] += self.busy[s] * dt
        # holding cost + interest accrue continuously, booked here
        self.cash -= self.inventory * self.holding_per_kit_day * dt
        self.holding_paid += self.inventory * self.holding_per_kit_day * dt
        if self.cash > 0:
            self.cash *= (1 + self.interest_rate) ** (dt / 365)
        if self.debt > 0:
            self.debt *= (1 + self.debt_rate) ** (dt / 365)
        self.day = t_new

    def proc_time(self, station, size, step):
        h = (SETUP[station] + PER_UNIT[station] * size)
        if station == 3:
            h = SETUP[3] + self.rng.expovariate(1.0 / (PER_UNIT[3] * size))
        return h * self.calib[station] / 24.0

    # ---------- controls (what the bot can change) ----------
    def buy(self, station, n):
        price, salvage = PRICES[station]
        if n > 0:
            cost = price * n
            if self.cash - cost < self.roq * self.kit_unit + self.kit_order_cost:
                return False
            self.cash -= cost
            self.pending_machines.append((self.day + 3.0, station, n))
            self._schedule(self.day + 3.0, "machine", (station, n))
            return True
        if n < 0:
            n = -n
            if self.machines[station] - n < 1:
                return False
            self.machines[station] -= n
            self.cash += salvage * n
            return True
        return True

    def borrow(self, amount):
        self.cash += amount
        self.debt += amount * (1 + self.loan_fee)

    def repay(self, amount):
        amount = min(amount, self.debt, max(0.0, self.cash))
        self.cash -= amount
        self.debt -= amount

    @property
    def net_cash(self):
        return self.cash - self.debt

    # ---------- dispatch ----------
    def _pick(self, s):
        if s != 2:
            lot = self.queues[s].popleft()
        else:
            a, b = self.queues[2], self.queues[4]
            if not a:
                src = b
            elif not b:
                src = a
            elif self.rule == "pri2":
                src = a
            elif self.rule == "pri4":
                src = b
            else:
                src = a if a[0].t_queue <= b[0].t_queue else b
            lot = src.popleft()
        self.qk[s] -= lot.size
        return lot

    def _try_start(self, s):
        while self.busy[s] < self.machines[s] and self._qlen(s):
            lot = self._pick(s)
            self.busy[s] += 1
            self._schedule(self.day + self.proc_time(s, lot.size, lot.step), "done", (s, lot))

    def _release(self):
        while self.waiting_jobs and self.inventory >= 60:
            job = self.waiting_jobs.pop(0)
            self.inventory -= 60
            size = 60 // self.lots_per_job
            for _ in range(self.lots_per_job):
                self._enqueue(1, Lot(job, size, 1, self.day))
            self._try_start(1)
        self._check_reorder()

    def _check_reorder(self):
        if (self.inventory < self.rop and self.kit_order_due is None
                and self.cash >= self.roq * self.kit_unit + self.kit_order_cost):
            self.cash -= self.roq * self.kit_unit + self.kit_order_cost
            self.kit_order_due = self.day + self.kit_lead
            self.kit_order_qty = self.roq
            self._schedule(self.kit_order_due, "kits", None)

    def step_until(self, t_end):
        while self.events and self.events[0][0] <= t_end:
            t, _, kind, p = heapq.heappop(self.events)
            self._advance_areas(t)
            getattr(self, "_on_" + kind)(p)
        self._advance_areas(t_end)

    def _on_arrival(self, _):
        self._day_acc["arrivals"] += 1
        if self.jobs_in_system >= self.wip_limit:
            self._day_acc["reject"] += 1
        else:
            job = Job(self.next_jid, self.day, self.contract, self.lots_per_job)
            self.next_jid += 1
            self.jobs_in_system += 1
            self._day_acc["jobin"] += 1
            self.waiting_jobs.append(job)
            self._release()
        self._schedule(self._next_arrival(self.day), "arrival", None)

    def _on_kits(self, _):
        self.inventory += self.kit_order_qty
        self.kit_order_due = None
        self._release()

    def _on_machine(self, p):
        s, n = p
        self.machines[s] += n
        for i, (due, st, k) in enumerate(self.pending_machines):
            if st == s and k == n and due <= self.day + 1e-9:
                self.pending_machines.pop(i)
                break
        self._try_start(s)

    def _on_done(self, p):
        s, lot = p
        self.busy[s] -= 1
        if lot.step == 1:
            lot.step = 2
            self._enqueue(2, lot)
            self._try_start(2)
        elif lot.step == 2:
            lot.step = 3
            self._enqueue(3, lot)
            self._try_start(3)
        elif lot.step == 3:
            lot.step = 4
            self._enqueue(2, lot)
            self._try_start(2)
        else:
            job = lot.job
            job.lots_left -= 1
            if job.lots_left == 0:
                lt = self.day - job.t_arr
                r = revenue(job.contract, lt)
                self.cash += r
                self.revenue_total += r
                self.jobs_in_system -= 1
                a = self._day_acc
                a["jobout"] += 1
                a["lt"].append(lt)
                a["rev"].append(r)
        self._try_start(s)

    def _on_eod(self, _):
        d = int(round(self.day))
        a = self._day_acc
        L = self.log
        L["JOBIN"][d] = a["jobin"]
        L["ARRIVALS"][d] = a["arrivals"]
        L["REJECT"][d] = a["reject"]
        L["JOBOUT"][d] = a["jobout"]
        L["JOBT"][d] = sum(a["lt"]) / len(a["lt"]) if a["lt"] else 0.0
        L["JOBREV"][d] = sum(a["rev"]) / len(a["rev"]) if a["rev"] else 0.0
        for s in (1, 2, 3):
            L[f"S{s}Q"][d] = a["qarea"][s]
            m = max(1, self.machines[s])
            L[f"S{s}UTIL"][d] = min(1.0, self.busy_time[s] / m)
            self.busy_time[s] = 0.0
        L["INV"][d] = self.inventory
        L["CASH"][d] = self.cash
        self._day_acc = self._new_acc()
        self._schedule(self.day + 1.0, "eod", None)

    # ---------- observation helpers (what the bot can see) ----------
    def wip_jobs(self):
        return self.jobs_in_system

    def machines_on_order(self, s):
        return sum(n for due, st, n in self.pending_machines if st == s)
