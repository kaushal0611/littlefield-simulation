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
    pend = re.search(r"(\d+)\s+more\s+machines?\s+will\s+be\s+installed", t, re.I)
    if pend:
        on = int(pend.group(1))
    elif re.search(r"on order", t, re.I) and not re.search(r"no additional machines on order", t, re.I):
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


INPUT_RE = re.compile(r"<input\b([^>]*)>", re.I)
ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([^\s>]+))""")
FORMTAG_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)


def _attrs(tag: str) -> dict:
    out = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None
                                else m.group(4)) for m in ATTR_RE.finditer(tag)}
    if re.search(r"\bchecked\b", tag, re.I):
        out["checked"] = "1"
    return out


def parse_forms(h: str) -> list[dict]:
    """-> [{action, fields (what a browser submits, minus submit buttons), submits: [(name, value)], password: name|None}]"""
    forms = []
    for m in FORMTAG_RE.finditer(html.unescape(h)):
        fa = _attrs(m.group(1))
        fields, submits, pw = {}, [], None
        for im in INPUT_RE.finditer(m.group(2)):
            a = _attrs(im.group(1))
            typ, name, val = a.get("type", "text").lower(), a.get("name"), a.get("value", "")
            if not name:
                continue
            if typ == "submit":
                submits.append((name, val))
            elif typ == "password":
                pw = name
                fields[name] = ""
            elif typ in ("radio", "checkbox"):
                if "checked" in a:
                    fields[name] = val
            elif typ in ("hidden", "text"):
                fields[name] = val
        forms.append(dict(action=fa.get("action", ""), method=fa.get("method", "GET").upper(),
                          fields=fields, submits=submits, password=pw))
    return forms


FORM_LINK_RE = re.compile(r"window\.open\(\s*['\"]([A-Za-z]+Form(?:\?[^'\"]*)?)['\"]")


def form_links(h: str) -> list[str]:
    """Edit pages a menu links to (e.g. OrdersForm, LoanForm, StationForm?id=2). Only URLs the game itself links."""
    out = []
    for u in FORM_LINK_RE.findall(html.unescape(h)):
        if u not in out:
            out.append(u)
    return out


def parse_cash(h: str):
    t = _text(h)
    debt = re.search(r"Debt\s*(-?[\d,]+(?:\.\d+)?)", t)
    loan_url = None
    m = re.search(r"\(\s*[\"']([^\"']+)[\"'][^)]*[\"']MOREDEBT[\"']", h)
    if m:
        loan_url = m.group(1)
    onhand = re.search(r"Cash on Hand\s*(-?[\d,]+(?:\.\d+)?)", t)
    return dict(debt=_num(debt.group(1)) if debt else 0.0, loan_url=loan_url,
                on_hand=_num(onhand.group(1)) if onhand else None)


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
        self.forms = {}

    # ---------- session ----------
    def login(self):
        r = self.s.post(f"{self.base}/Littlefield/CheckAccess", timeout=self.timeout, data=dict(
            institution=self.institution, ismobile="false", id=self.team, password=self.password))
        r.raise_for_status()
        if "teamId" not in r.text and "LTStatus" not in r.text:
            raise GameError("login failed: " + _text(r.text)[:200])
        self.main_html = r.text

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
        raw = {}
        if getattr(self, "main_html", ""):
            raw["main"] = self.main_html
            # leaderboard: only a URL the game's own main page links to
            for u in re.findall(r"""(?:window\.open\(\s*|href\s*=\s*|action\s*=\s*)['"]([^'"]*[Ss]tand[^'"]*)['"]""", self.main_html):
                u = u.split("/Littlefield/")[-1].lstrip("/")
                if u and not u.startswith("http"):
                    try:
                        raw["standing"] = self.get(u)
                    except Exception:  # noqa: BLE001
                        pass
                    break
        raw["status"] = self.get("LTStatus")
        day, cash, team = parse_status(raw["status"])
        cash_html = raw["cash"] = self.get(f"CashStatus?team={self.team}")
        cs = parse_cash(cash_html)
        orders_html = raw["orders"] = self.get("OrdersMenu")
        o = parse_orders(orders_html)
        mat_html = raw["material"] = self.get("MaterialMenu")
        m = parse_material(mat_html)
        st = {}
        for k in (1, 2, 3):
            raw[f"station{k}"] = self.get(f"StationMenu?id={k}")
            st[k] = parse_station(raw[f"station{k}"])
        # once the game runs, edit controls live on separate pages the menus open ("edit data")
        forms = {}
        for menu in list(raw):
            for url in form_links(raw[menu]):
                if url not in forms:
                    try:
                        forms[url] = self.get(url)
                    except requests.RequestException:
                        continue
                    raw["form_" + re.sub(r"\W+", "_", url)] = forms[url]
        self.forms = {}
        for url, fh in forms.items():
            for f in parse_forms(fh):
                if f["method"] == "POST" and f["action"]:
                    self.forms[url] = f
        for url, fh in forms.items():
            cl = parse_controls(fh)
            f = self.forms.get(url)
            if f:
                fl = f["fields"]
                if url.startswith("OrdersForm"):
                    for key, fld in (("contract", "contractOpt"), ("wip", "maxwip"), ("lots", "lotCount")):
                        if fld in fl:
                            cl.append(Control(fn="form", url=url, current=fl[fld], trans=fld))
                elif url.startswith("MaterialForm"):
                    for fld in ("point", "quant"):
                        if fld in fl:
                            cl.append(Control(fn="form", url=url, current=fl[fld], trans=fld))
                elif url.startswith("StationForm"):
                    for fld in ("count", "rule"):
                        if fld in fl:
                            cl.append(Control(fn="form", url=url, current=fl[fld], trans=fld))
            if url.startswith("OrdersForm"):
                o["controls"] += cl
            elif url.startswith("MaterialForm"):
                m["controls"] += cl
            elif url.startswith("StationForm"):
                sid = re.search(r"id=(\d)", url)
                if sid and int(sid.group(1)) in st:
                    st[int(sid.group(1))]["controls"] += cl
            if not cs["loan_url"]:
                lu = parse_cash(fh)["loan_url"]
                if lu:
                    cs["loan_url"] = lu
                elif url.startswith("LoanForm") and url in self.forms:
                    cs["loan_url"] = "form:" + url
        controls = {}
        for c in o["controls"]:
            key = {"updateContractValue": "contract", "updateLotSizeValue": "lots",
                   "updateMaximumWipLimitValue": "wip"}.get(c.fn)
            if c.fn == "form":
                key = {"contractOpt": "contract", "maxwip": "wip", "lotCount": "lots"}[c.trans]
                controls[key] = c
                continue
            if key is None:  # unknown function name: fall back to the transaction code / name
                tag = (c.trans + " " + c.fn).upper()
                key = ("contract" if "CONTRACT" in tag else "lots" if "LOT" in tag
                       else "wip" if ("WIP" in tag or "MAXJOB" in tag) else c.fn)
            controls.setdefault(key, c)
        for c in m["controls"]:
            key = "rop" if "POINT" in c.trans.upper() else "roq"
            if c.fn == "form" or key not in controls:
                controls[key] = c
        for k, v in st.items():
            for c in v["controls"]:
                key = "rule" if "RULE" in c.trans.upper() else f"count{k}"
                if key == "rule" and k != 2:
                    continue
                if c.fn == "form" or key not in controls:
                    controls[key] = c
        rule = st[2]["rule"] or (controls["rule"].current if "rule" in controls else "fifo")
        if cs.get("on_hand") is not None:      # LTStatus "Cash Balance" is net of debt; spendable cash is on hand
            cash = cs["on_hand"]
        snap = Snapshot(
            day=day, cash=cash, debt=cs["debt"], team=team,
            machines={k: st[k]["machines"] for k in (1, 2, 3)},
            on_order={k: st[k]["on_order"] for k in (1, 2, 3)},
            rule=rule, contract=o["contract"], lots_per_job=o["lots_per_job"], wip_limit=o["wip_limit"],
            inventory=m["inventory"], rop=m["rop"], roq=m["roq"], kit_unit=m["kit_unit"],
            kit_order_cost=m["kit_order_cost"], kits_on_order=m["kits_on_order"],
            controls=controls, loan_url=cs["loan_url"], raw=raw)
        if with_plots:
            for name in PLOTS:
                for lab, pts in parse_plot(self.get(f"Plot?data={name}&x=all")).items():
                    key = name if lab == "data" else f"{name}_{lab.replace('contract ', 'c')}"
                    snap.series[key] = pts
        return snap

    # ---------- write ----------
    def _post_form(self, url: str, fields: dict, submit: tuple | None = None) -> str:
        """POST a form like a browser, then answer a password confirmation page if one comes back."""
        data = dict(fields)
        if submit:
            data[submit[0]] = submit[1]
        time.sleep(self.pause)
        path = url.split("?")[0] if "?" in url and "id=" in url and "id" in data else url
        r = self.s.post(f"{self.base}/Littlefield/{path}", data=data, timeout=self.timeout)
        r.raise_for_status()
        txt = r.text
        for _ in range(2):
            conf = next((f for f in parse_forms(txt) if f["password"]), None)
            if not conf:
                break
            d = dict(conf["fields"])
            d[conf["password"]] = self.password
            sub = next(((n, v) for n, v in conf["submits"] if v.lower() in ("confirm", "ok", "submit")),
                       conf["submits"][0] if conf["submits"] else None)
            if sub:
                d[sub[0]] = sub[1]
            elif "submit" not in d:
                d["submit"] = "confirm"
            time.sleep(self.pause)
            act = conf["action"].lstrip("/").removeprefix("Littlefield/") or path
            r = self.s.post(f"{self.base}/Littlefield/{act}", data=d, timeout=self.timeout)
            r.raise_for_status()
            txt = r.text
        t = _text(txt)
        if re.search(r"\b(error|invalid|incorrect|wrong password)\b", t, re.I):
            raise GameError(t[:200])
        return t[:200] or "ok"

    def submit(self, control: Control, value) -> str:
        """POST a change exactly as the page's confirm dialog does."""
        if control.fn == "form":
            f = self.forms[control.url]
            v = value
            if control.trans in ("point", "quant"):        # material form counts batches of 60 kits
                v = max(0, -(-int(value) // 60))
            f["fields"][control.trans] = str(v)
            return self._post_form(control.url, f["fields"])
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
        kind = "increase" if increase else "decrease"
        if url.startswith("form:"):
            f = self.forms[url[5:]]
            out = []
            left = int(amount)
            while left > 0:                                  # amount box takes at most 6 digits
                step = min(left, 999_999)
                fields = dict(f["fields"], amount=str(step))
                out.append(self._post_form(url[5:], fields, ("submit", kind)))
                left -= step
            return " | ".join(out)[:200]
        time.sleep(self.pause)
        r = self.s.post(f"{self.base}/Littlefield/{url.lstrip('/').removeprefix('Littlefield/')}",
                        data=f"submit={kind}&{kind}string={kind}&amount={int(amount)}", timeout=self.timeout,
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        r.raise_for_status()
        if "Error" in r.text:
            raise GameError(_text(r.text)[:200])
        return _text(r.text)[:200]
