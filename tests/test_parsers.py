"""Parsers against markup captured from the live game (tests/fixtures/*_live.html)."""
from pathlib import Path

from lf import client as c

FX = Path(__file__).parent / "fixtures"
read = lambda n: (FX / n).read_text()


def test_status():
    assert c.parse_status(read("status_live.html")) == (50.0, 714511.0, "8a")


def test_orders_locked_before_start():
    o = c.parse_orders(read("orders_live.html"))
    assert (o["wip_limit"], o["lots_per_job"], o["contract"]) == (300, 1, 1)
    assert o["controls"] == []          # no edit controls visible pre-start


def test_station2():
    s = c.parse_station(read("station2_live.html"))
    assert s["machines"] == 1 and s["on_order"] == 0 and s["rule"] == "fifo"
    by = {x.trans: x for x in s["controls"]}
    assert by["S2COUNT"].url == "StationForm?id=2" and by["S2COUNT"].current == "1"
    assert by["S2RULE"].current == "fifo"


def test_material():
    m = c.parse_material(read("material_live.html"))
    assert (m["inventory"], m["rop"], m["roq"]) == (1140, 1500, 7200)
    assert m["kit_order_cost"] == 1000.0 and m["kit_unit"] == 10.0
    assert m["kits_on_order"] == 7200 and abs(m["kits_due_in"] - 2.9) < 1e-9
    assert {x.trans for x in m["controls"]} == {"ORDERPOINT", "ORDERQUANT"}


def test_plot_multi_contract():
    p = c.parse_plot(read("plot_jobt_live.html"))
    assert p["contract 1"][50.0] == 21.873 and p["contract 2"][50.0] == 0


def test_cash():
    cs = c.parse_cash(read("cash_live.html"))
    assert cs["debt"] == 0 and cs["loan_url"] is None


def test_station_on_order_wording_is_conservative():
    s = c.parse_station("<B> Number of Installed Machines:</B> 3 2 machine(s) on order arriving soon")
    assert s["on_order"] == 2
    s = c.parse_station("<B> Number of Installed Machines:</B> 3 something unexpected about an order")
    assert s["on_order"] == 0
    s = c.parse_station("<B> Number of Installed Machines:</B> 3 machines on order: delivery day 55")
    assert s["on_order"] >= 1
