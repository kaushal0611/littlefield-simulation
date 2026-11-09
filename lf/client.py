"""HTTP client for the Littlefield web game (op2.responsive.net).

Endpoints and form formats were mapped from the live IIMB game on 2026-11-09:
  login     POST /Littlefield/CheckAccess  institution, ismobile, id, password
  status    GET  /Littlefield/LTStatus                -> day, cash
  cash      GET  /Littlefield/CashStatus?team=<id>    -> debt, loan form
  menus     GET  /Littlefield/{OrdersMenu,MaterialMenu,StationMenu?id=N}
  plots     GET  /Littlefield/Plot?data=<NAME>&x=all  -> "label: '..', points: 'x y x y'"
  changes   POST /Littlefield/<url from page>         trans=<TYPE>,<VALUE>&pwd=<pw>&submit=confirm
            url and TYPE are read from the update...Value(...) calls on each menu,
            so nothing about the form names is hard-coded.
  loans     POST <url> submit=increase&increasestring=increase&amount=<n>   (decrease to repay)

The game keeps one session per team. Requesting an unknown path kills the
session, so this client only ever requests paths the pages themselves link to.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

import requests

PLOTS = ["JOBIN", "JOBQ", "INV", "S1Q", "S2Q", "S3Q", "S1UTIL", "S2UTIL", "S3UTIL",
         "JOBOUT", "JOBT", "JOBREV", "CASH"]

UPDATE_RE = re.compile(
    r"(update\w*Value)\(\s*[\"']([^\"']*)[\"']\s*,\s*[\"']([^\"']*)[\"']\s*,\s*[\"']([^\"']*)[\"']")
POINTS_RE = re.compile(r"label:\s*'([^']*)',(?:\s*name:\s*'[^']*',)?\s*points:\s*'([^']*)'")


class SessionLost(Exception):
    pass


class GameError(Exception):
    pass


@dataclass
class Control:
    fn: str          # e.g. updateStationValue
    url: str         # e.g. StationForm?id=2
    current: str     # value shown on the page
    trans: str       # e.g. S2COUNT


@dataclass
class Snapshot:
    day: float
    cash: float
    debt: float
    team: str
    machines: dict
    on_order: dict
    rule: str
    contract: int
    lots_per_job: int
    wip_limit: int
    inventory: int
    rop: int
    roq: int
    kit_unit: float
    kit_order_cost: float
    kits_on_order: int
    controls: dict = field(default_factory=dict)   # key -> Control
    loan_url: str | None = None
    series: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)        # page html kept for debugging


def _num(s: str) -> float:
    return float(s.replace(",", "").strip())


def _text(h: str) -> str:
    t = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", h, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def parse_status(h: str):
    t = _text(h)
    day = re.search(r"Day:\s*([\d.,]+)", t)
    cash = re.search(r"Cash Balance:\s*(-?[\d,]+(?:\.\d+)?)", t)
    team = re.search(r"Team:\s*(\S+)", t)
    if not day or not cash:
        raise SessionLost("status page has no Day/Cash: " + t[:120])
    return _num(day.group(1)), _num(cash.group(1)), team.group(1) if team else ""


def parse_controls(h: str) -> list[Control]:
    out = []
    for m in UPDATE_RE.finditer(html.unescape(h)):
        out.append(Control(fn=m.group(1), url=m.group(2), current=m.group(3), trans=m.group(4)))
    return out


def parse_orders(h: str):
    t = _text(h)
    wip = re.search(r"Maximum WIP Limit:\s*([\d,]+)", t)
    lot = re.search(r"Lot size:\s*([\d,]+)\s*kits,\s*or\s*(\d+)\s*lots? per job", t)
    con = re.search(r"Current contract:\s*(\d+)", t)
    controls = parse_controls(h)
    return dict(
        wip_limit=int(_num(wip.group(1))) if wip else 300,
        lots_per_job=int(lot.group(2)) if lot else 1,
        contract=int(con.group(1)) if con else 1,
        controls=controls,
    )


def parse_material(h: str):
    t = _text(h)
    g = lambda pat, d=None: (re.search(pat, t).group(1) if re.search(pat, t) else d)
    inv = g(r"Inventory on day\s*[\d.]+:\s*([\d,]+)\s*kits", "0")
    rop = g(r"Reorder Point:\s*([\d,]+)\s*kits", "0")
    roq = g(r"Order Quantity:\s*([\d,]+)\s*kits", "0")
    unit = g(r"Unit Cost:\s*\$\s*([\d,.]+)", "10")
    oc = g(r"Order Cost:\s*\$\s*([\d,.]+)", "1000")
    m = re.search(r"Material order of\s*([\d,]+)\s*kits due to arrive in\s*([\d.]+)", t)
    return dict(inventory=int(_num(inv)), rop=int(_num(rop)), roq=int(_num(roq)),
                kit_unit=_num(unit), kit_order_cost=_num(oc),
                kits_on_order=int(_num(m.group(1))) if m else 0,
                kits_due_in=float(m.group(2)) if m else None,
                controls=parse_controls(h))


def parse_station(h: str):
    t = _text(h)
    n = re.search(r"Number of Installed Machines:\s*(\d+)", t)
    on = 0
    if re.search(r"on order", t, re.I) and not re.search(r"no additional machines on order", t, re.I):
        m = re.search(r"(\d+)\s+(?:additional\s+)?machines?\b[^.]{0,60}?on order", t, re.I) or \
            re.search(r"on order[^.]{0,40}?(\d+)", t, re.I)
        on = int(m.group(1)) if m else 1   # unknown wording: assume something is pending
    rule = None
    m = re.search(r"<input[^>]*type=.radio.[^>]*name=.rule.[^>]*value=.(\w+).[^>]*checked", h, re.I) or \
        re.search(r"<input[^>]*type=.radio.[^>]*value=.(\w+).[^>]*name=.rule.[^>]*checked", h, re.I)
    if m:
        rule = m.group(1)
    return dict(machines=int(n.group(1)) if n else None, on_order=on, rule=rule,
                controls=parse_controls(h))


def parse_plot(h: str) -> dict:
    """-> {label: {x: y}}; x may be fractional (inventory plot)."""
    out = {}
    for lab, pts in POINTS_RE.findall(h):
        a = pts.split()
        out[lab] = {float(a[i]): float(a[i + 1]) for i in range(0, len(a) - 1, 2)}
    return out


def parse_cash(h: str):
    t = _text(h)
    debt = re.search(r"Debt\s*(-?[\d,]+(?:\.\d+)?)", t)
    loan_url = None
    m = re.search(r"\(\s*[\"']([^\"']+)[\"'][^)]*[\"']MOREDEBT[\"']", h)
    if m:
        loan_url = m.group(1)
    return dict(debt=_num(debt.group(1)) if debt else 0.0, loan_url=loan_url)


class LittlefieldClient:
    def __init__(self, team: str, password: str, base: str = "https://op2.responsive.net",
                 institution: str = "iimb", timeout: float = 30.0, pause: float = 0.3):
        self.team, self.password = team, password
        self.base = base.rstrip("/")
        self.institution = institution
        self.timeout = timeout
        self.pause = pause
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "littlefield-ops-bot/1.0"

    # ---------- session ----------
    def login(self):
        r = self.s.post(f"{self.base}/Littlefield/CheckAccess", timeout=self.timeout, data=dict(
            institution=self.institution, ismobile="false", id=self.team, password=self.password))
        r.raise_for_status()
        if "teamId" not in r.text and "LTStatus" not in r.text:
            raise GameError("login failed: " + _text(r.text)[:200])

    def get(self, path: str, retry=True) -> str:
        time.sleep(self.pause)
        r = self.s.get(f"{self.base}/Littlefield/{path}", timeout=self.timeout)
        lost = r.status_code in (401, 403) or "log in again" in r.text or "return to your log in page" in r.text
        if lost:
            if not retry:
                raise SessionLost(path)
            self.login()
            return self.get(path, retry=False)
        r.raise_for_status()
        return r.text

    def quit(self):
        try:
            self.s.get(f"{self.base}/Littlefield/Quit", timeout=self.timeout)
        except requests.RequestException:
            pass

    # ---------- read ----------
    def snapshot(self, with_plots=True) -> Snapshot:
        day, cash, team = parse_status(self.get("LTStatus"))
        cash_html = self.get(f"CashStatus?team={self.team}")
        cs = parse_cash(cash_html)
        orders_html = self.get("OrdersMenu")
        o = parse_orders(orders_html)
        m = parse_material(self.get("MaterialMenu"))
        st = {k: parse_station(self.get(f"StationMenu?id={k}")) for k in (1, 2, 3)}
        controls = {}
        for c in o["controls"]:
            key = {"updateContractValue": "contract", "updateLotSizeValue": "lots",
                   "updateMaximumWipLimitValue": "wip"}.get(c.fn)
            if key is None:  # unknown function name: fall back to the transaction code / name
                tag = (c.trans + " " + c.fn).upper()
                key = ("contract" if "CONTRACT" in tag else "lots" if "LOT" in tag
                       else "wip" if ("WIP" in tag or "MAXJOB" in tag) else c.fn)
            controls.setdefault(key, c)
        for c in m["controls"]:
            controls["rop" if "POINT" in c.trans.upper() else "roq"] = c
        for k, v in st.items():
            for c in v["controls"]:
                controls[f"rule" if "RULE" in c.trans.upper() else f"count{k}"] = c
        rule = st[2]["rule"] or (controls["rule"].current if "rule" in controls else "fifo")
        snap = Snapshot(
            day=day, cash=cash, debt=cs["debt"], team=team,
            machines={k: st[k]["machines"] for k in (1, 2, 3)},
            on_order={k: st[k]["on_order"] for k in (1, 2, 3)},
            rule=rule, contract=o["contract"], lots_per_job=o["lots_per_job"], wip_limit=o["wip_limit"],
            inventory=m["inventory"], rop=m["rop"], roq=m["roq"], kit_unit=m["kit_unit"],
            kit_order_cost=m["kit_order_cost"], kits_on_order=m["kits_on_order"],
            controls=controls, loan_url=cs["loan_url"], raw={"orders": orders_html, "cash": cash_html})
        if with_plots:
            for name in PLOTS:
                for lab, pts in parse_plot(self.get(f"Plot?data={name}&x=all")).items():
                    key = name if lab == "data" else f"{name}_{lab.replace('contract ', 'c')}"
                    snap.series[key] = pts
        return snap

    # ---------- write ----------
    def submit(self, control: Control, value) -> str:
        """POST a change exactly as the page's confirm dialog does."""
        time.sleep(self.pause)
        body = f"trans={control.trans},{value}&pwd={requests.utils.quote(self.password, safe='')}&submit=confirm"
        r = self.s.post(f"{self.base}/Littlefield/{control.url}", data=body, timeout=self.timeout,
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                 "X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        txt = r.text
        if "Error:" in txt:
            raise GameError(txt.split("Error:")[-1].strip()[:200])
        return txt.split("Success:")[-1].strip()[:200] if "Success:" in txt else _text(txt)[:200]

    def loan(self, url: str, amount: int, increase=True) -> str:
        time.sleep(self.pause)
        kind = "increase" if increase else "decrease"
        r = self.s.post(f"{self.base}/Littlefield/{url.lstrip('/').removeprefix('Littlefield/')}",
                        data=f"submit={kind}&{kind}string={kind}&amount={int(amount)}", timeout=self.timeout,
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        r.raise_for_status()
        if "Error" in r.text:
            raise GameError(_text(r.text)[:200])
        return _text(r.text)[:200]
