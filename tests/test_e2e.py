"""Full season, end to end: real bot.py + real HTTP client against the mock game.

Each bot run logs in (the mock advances the twin by `advance_per_login` days),
reads every page, decides, POSTs changes with the password, and quits.
"""
import importlib

import pytest

import bot
from lf.client import LittlefieldClient
from tests.mock_server import MockGame, serve


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(bot, "LOG", tmp_path / "actions.log")
    monkeypatch.delenv("PAUSE", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    return tmp_path


def run_game(game, runs, password="pw"):
    srv, base = serve(game)
    try:
        rcs = []
        for _ in range(runs):
            cl = LittlefieldClient(game.team, password, base=base, pause=0)
            rcs.append(bot.main(client=cl))
            if game.f.day >= 300:
                break
        game.f.step_until(305)
        return rcs
    finally:
        srv.shutdown()


def test_full_season(isolated):
    game = MockGame(seed=3, advance_per_login=0.5)
    rcs = run_game(game, runs=520)
    log = (isolated / "actions.log").read_text()
    assert set(rcs) == {0}, log[-3000:]
    assert "FAIL" not in log and "ERROR" not in log
    assert game.f.net_cash > 1_500_000, f"net cash {game.f.net_cash:,.0f}"
    assert game.f.contract == 2
    # password travelled with every change
    assert game.posts and all("pwd=pw" in b or "amount=" in b for _, b in game.posts)


def test_dry_run_changes_nothing(isolated, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    game = MockGame(seed=1)
    run_game(game, runs=3)
    assert game.posts == []
    assert "DRY" in (isolated / "actions.log").read_text()


def test_pause_switch(isolated, monkeypatch):
    monkeypatch.setenv("PAUSE", "1")
    assert bot.main(client=object()) == 0


def test_wrong_password_is_reported(isolated):
    game = MockGame(seed=1)
    srv, base = serve(game)
    try:
        rc = bot.main(client=LittlefieldClient("8a", "nope", base=base, pause=0))
    finally:
        srv.shutdown()
    assert rc == 1 and "login failed" in (isolated / "actions.log").read_text()


def test_locked_order_menu_falls_back_to_hold(isolated):
    game = MockGame(seed=2, editable_orders=False, advance_per_login=1.0)
    run_game(game, runs=12)
    log = (isolated / "actions.log").read_text()
    assert "edits=OFF" in log and game.f.rop == 1
