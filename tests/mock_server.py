"""Local stand-in for op2.responsive.net, driven by the digital twin.

Page markup copies the structure captured from the live IIMB game on
2026-11-09 (same labels, same update...Value(...) calls, same plot format),
so the real parsers and the real submit path are exercised.

Order-menu and loan controls were NOT visible before the game started; their
markup here follows the station-menu pattern and is flagged as assumed.
"""
from __future__ import annotations

import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote_plus

from lf.sim import Factory, PRICES, CONTRACTS


def _fmt(n):
    return f"{n:,.0f}"


class MockGame:
    def __init__(self, team="8a", password="pw", seed=0, advance_per_login=0.5, editable_orders=True,
                 loans=True, demand_fn=None):
        self.team, self.password = team, password
        kw = dict(seed=seed)
        if demand_fn:
            kw["demand_fn"] = demand_fn
        self.f = Factory(**kw)
        self.f.step_until(50.0)
        self.advance = advance_per_login
        self.editable_orders = editable_orders
        self.loans = loans
        self.session = None
        self.lock = threading.Lock()
        self.posts = []          # (url, body) for assertions
        self.frozen = False

    # ---------- pages ----------
    def status(self):
        f = self.f
        return f"""<html><head><body bgcolor=white onload='connect();'><font face=arial size=-1>
<center><hr color=black noshade><div style="float: left;">
<img src='/lt/image/bullet.jpg'> <b>Day: </b> {int(f.day)}
<img src='/lt/image/bullet.jpg'> <b>Team: </b> {self.team}
<img src='/lt/image/bullet.jpg'> <b>Cash Balance: </b> {_fmt(f.cash)}
<input type=hidden name='isChatEnabled' id='isChatEnabled' value='false'>
</div><div style=clear:both;></div><hr color=black noshade></center></font></body></html>"""

    def main(self):
        return f"""<html><body><input type="hidden" id="teamId" name="teamId" value="{self.team}">
<div class="ltStatus" id="Ltstatus"></div>
<map><area title="Click here for Customer Order Queue ..." href="javascript:openChildWin ('/Littlefield/OrdersMenu', 'CustOrderQ')">
<area href="javascript:openChildWin ('/Littlefield/MaterialMenu', 'Material')">
<area href="javascript:openChildWin ('/Littlefield/StationMenu?id=1', 'S1')"></map></body></html>"""

    def orders(self):
        f = self.f
        c = CONTRACTS[f.contract]
        ctrl = ""
        if self.editable_orders:  # ASSUMED markup (not visible before game start)
            ctrl = f"""
<input type='button' class="buttonStyle ml10 green" onclick='updateMaximumWipLimitValue("OrdersForm","{f.wip_limit}","WIPLIMIT","maxWipLimit","maximumWipLimitForm","Change WIP limit to ","",{f.wip_limit},"text")' value='Confirm'/>
<input type='button' class="buttonStyle ml10 green" onclick='updateLotSizeValue("OrdersForm","{f.lots_per_job}","LOTSIZE","lotSize","lotSizeBlock","Change lot size","",{f.lots_per_job},"radio")' value='Confirm'/>
<input type='button' class="buttonStyle ml10 green" onclick='updateContractValue("OrdersForm","{f.contract}","CONTRACT","contract","contractFormBlock","Change contract to ","",{f.contract},"radio")' value='Confirm'/>"""
        return f"""<HTML> <head> <TITLE>ORDERS MENU</TITLE></head><BODY>
<h2 class='pageTitle' style='margin-top:15px;'>Littlefield Technologies - ORDERS MENU</h2>
<div style='margin:0;padding:0;' class='label'><B> Name: </B>{self.team}<BR>
<b class='label'>Maximum WIP Limit:</b>{f.wip_limit} jobs
<div style='clear:both;'></div><B class='label'>Number of kits in 1 job: </B> <span class='label'>60</span><BR>
<B class='label'>Lot size: </B><span class='label'>{60 // f.lots_per_job} kits, or {f.lots_per_job} lot{'s' if f.lots_per_job > 1 else ''} per job
</span></div>{ctrl}
<p style='margin:0;padding:0;float:left;' ><B>Current contract: </B> {f.contract}
</p></br></br><div id='contractValue'>
<DD>Quoted lead time: {c['quoted']} day(s)<BR><DD>Maximum lead time: {c['maxlt']} day(s)<BR>
<DD>Revenue per order: {c['rev']} dollars</div>
<FORM action='OrdersMenu' method=GET><INPUT type='submit' name='update' value='update'></FORM></BODY></HTML>"""

    def material(self):
        f = self.f
        due = ""
        if f.kit_order_due:
            due = (f"<P><B>Material order of {_fmt(f.kit_order_qty)} kits due to arrive in "
                   f"{f.kit_order_due - f.day:.2f} simulated days")
        return f"""<HTML> <head> <TITLE>Material Menu</TITLE> </head> <BODY>
<h2 class='pageTitle' style='margin-top:15px;'>Littlefield Technologies - MATERIALS MENU</h2>
<P style='line-height:25px;padding:0px;margin:0px;'><B> Name: </B>{self.team} <BR><B>Unit Cost: </B> $ {f.kit_unit} <BR><B>Order Cost: </B> $ {f.kit_order_cost} <BR><B>Lead Time:</B> 4 day(s)
<div class='newUI'><input type='number' id='reorderPoint' value='{f.rop}'/>
<input type='button' onclick='updateMaterialValue("MaterialForm","{f.rop}","ORDERPOINT","reorderPoint","reorderPointForm","Change order point to ","kits","0","text")' value='Confirm'/>
<input type='number' id='orderQuantity' value='{f.roq}'/>
<input type='button' onclick='updateMaterialValue("MaterialForm","{f.roq}","ORDERQUANT","orderQuantity","reorderPointForm","Change order quantity to ","kits","0","text")' value='Confirm'/>
</div>
<div class='newUIPlot' style='display:none;'><div class='newUILabel'>Inventory on day&nbsp;{f.day:.1f}:</div>
<div class='newUILabelValue floatleft ml5'>{f.inventory} kits </div></div>
<p class='oldUI'><B>Reorder Point:</B> {_fmt(f.rop)} kits ( {f.rop // 60} batches of 60 )
<BR><B>Order Quantity:</B> {_fmt(f.roq)} kits ( {f.roq // 60} batches of 60 )
{due}</p></BODY></HTML>"""

    def station(self, k):
        f = self.f
        n = f.machines[k]
        on = f.machines_on_order(k)
        on_txt = ("There are no additional machines on order" if not on
                  else f"{on} additional machine(s) on order")   # ASSUMED wording
        rule = ""
        if k == 2:
            rule = "".join(
                f"<input type='radio' name='rule' value='{r}' {'checked' if f.rule == r else ''}>"
                for r in ("fifo", "pri2", "pri4"))
            rule += (f"""<input type='button' onclick='updateShedulingRuleValue("StationForm?id=2","{f.rule}","S2RULE","rule","schedulingRuleBlock","Change rule to: ","","{f.rule}","radio")' value='Confirm'/>""")
        price, salvage = PRICES[k]
        return f"""<HTML><BODY><h2 class='pageTitle'>Littlefield Technologies - STATION {k} MENU</h2>
<P><B> Name: </B>{self.team}
<div class='oldUI'> <B> Number of Installed Machines:</B> {n}</div>
<input type='number' id='stationValue{k}' value='{n}'/>
<input type='button' class="buttonStyle ml10 green" onclick='updateStationValue("StationForm?id={k}","{n}","S{k}COUNT","stationValue{k}","numberOfMachineForm","Buy "," more machine(s)",{n},"text")' value='Confirm'/>
{rule}
<p><B>Purchase Price: </B>$ {price:,.2f}<BR><B>Retirement Price: </B>$ {salvage:,.2f}<BR>
<B>Purchase lead time: </B>3 day(s)
 {on_txt}<div class='clearboth'> </div></BODY></HTML>"""

    def cash(self):
        f = self.f
        loan = ""
        if self.loans:  # ASSUMED markup
            loan = ("""<input type='button' onclick='updateDebtValue("CashStatus","0","MOREDEBT","debtAmount","loanform","","","0","text")' value='increase'/>""")
        return f"""<html><body>Littlefield Technologies - Sources and Uses of Cash
<table><tr><td>Cash Balance</td><td>{_fmt(f.cash)}</td></tr>
<tr><td>Debt</td><td>{_fmt(f.debt)}</td></tr>
<tr><td>Cash on Hand</td><td>{_fmt(f.cash)}</td></tr></table>{loan}</body></html>"""

    def plot(self, name):
        L = self.f.log
        if name in ("JOBOUT", "JOBT", "JOBREV"):
            src = L[name]
            pts = " ".join(f"{d} {v:.3f}" for d, v in sorted(src.items()))
            lines = (f"{{label: 'contract 1', name: 'quoted lead time = 3.00 day(s)', points: '{pts}'}}")
        else:
            src = L.get(name, {})
            pts = " ".join(f"{d} {v:.3f}" for d, v in sorted(src.items()))
            lines = f"{{label: 'data', points: '{pts}'}}"
        return f"""<html><body><div id='content_1' class='content'> <script> var data = {{
region: {{x1: 1.0, x2: 50.0, y1: 0.0, y2: 33.0}}, lines:[ {lines} ], legend: "FALSE" }}</script></body></html>"""

    # ---------- changes ----------
    def apply(self, url, body):
        q = parse_qs(body)
        trans = q.get("trans", [""])[0]
        pwd = q.get("pwd", [""])[0]
        if "MOREDEBT" in url or "increase" in q.get("submit", [""]) or "decrease" in q.get("submit", [""]):
            amt = float(q.get("amount", ["0"])[0])
            if q.get("submit", [""])[0] == "increase":
                self.f.borrow(amt)
            else:
                self.f.repay(amt)
            return "Success: debt updated"
        if pwd != self.password:
            return "Error: Please enter correct password."
        typ, _, val = trans.partition(",")
        f = self.f
        if typ.endswith("COUNT"):
            k = int(typ[1])
            n = int(val) - f.machines[k]
            if f.machines_on_order(k) and n > 0:
                return "Error: machines already on order"
            if not f.buy(k, n):
                return "Error: insufficient cash"
            return f"Success: station {k} now {val}"
        if typ == "ORDERPOINT":
            f.rop = int(val)
        elif typ == "ORDERQUANT":
            f.roq = int(val)
        elif typ == "S2RULE":
            f.rule = val
        elif typ == "CONTRACT":
            f.contract = int(val)
        elif typ == "LOTSIZE":
            f.lots_per_job = int(val)
        elif typ == "WIPLIMIT":
            f.wip_limit = int(val)
        else:
            return "Error: unknown transaction"
        f._check_reorder()
        return f"Success: {typ} set to {val}"


def make_handler(game: MockGame):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, cookie=None):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            if cookie:
                self.send_header("Set-Cookie", f"JSESSIONID={cookie}; Path=/Littlefield")
            self.end_headers()
            self.wfile.write(b)

        def _sid(self):
            c = self.headers.get("Cookie", "")
            for part in c.split(";"):
                if part.strip().startswith("JSESSIONID="):
                    return part.strip().split("=", 1)[1]
            return None

        def _auth(self):
            return game.session and self._sid() == game.session

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            p = u.path
            with game.lock:
                if p == "/Littlefield/Quit":
                    game.session = None
                    return self._send(200, "bye")
                if not self._auth():
                    return self._send(200, "<HTML><BODY>Please return to your log in page and log in</BODY></HTML>")
                routes = {
                    "/Littlefield/LTStatus": game.status,
                    "/Littlefield/OrdersMenu": game.orders,
                    "/Littlefield/MaterialMenu": game.material,
                    "/Littlefield/CashStatus": game.cash,
                }
                if p in routes:
                    return self._send(200, routes[p]())
                if p == "/Littlefield/StationMenu":
                    return self._send(200, game.station(int(qs["id"][0])))
                if p == "/Littlefield/Plot":
                    return self._send(200, game.plot(qs["data"][0]))
                game.session = None   # the live server drops the session on unknown paths
                return self._send(404, "HTTP Status 404 - Not Found")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode()
            with game.lock:
                if u.path == "/Littlefield/CheckAccess":
                    q = parse_qs(body)
                    if q.get("id", [""])[0] == game.team and q.get("password", [""])[0] == game.password \
                            and q.get("institution", [""])[0] == "iimb":
                        if not game.frozen:
                            game.f.step_until(game.f.day + game.advance)
                        game.session = secrets.token_hex(8)
                        return self._send(200, game.main(), cookie=game.session)
                    return self._send(200, "<html>Invalid team id or password</html>")
                if not self._auth():
                    return self._send(200, "Please return to your log in page and log in")
                game.posts.append((self.path, unquote_plus(body)))
                return self._send(200, game.apply(self.path, body))
    return H


def serve(game: MockGame, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(game))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
