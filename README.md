# Littlefield bot (team 8a, IIMB PGP 2026)

Runs the Littlefield factory on its own from GitHub's servers, every ~10 minutes, from now until day 300. Your laptops can stay off.

Each run: log in, read every menu and plot, decide, post changes with the team password, log out, and commit a one-line log to `logs/actions.log`.

## Where the factory stands (day 50, read from the live game)

- Lead time 21.9 days. Every order since day 25 has earned $0.
- About 300 jobs are stuck. Most sit in the tester queue (15,322 kits). The tester is the bottleneck at ~6.6 jobs/day.
- Orders are being turned away at the WIP cap of 300. Real demand was ~20/day on day 39 and rising about 0.5/day.
- Cash $714,511, no debt. 1,140 kits in stock, 7,200 arriving in 2.9 days.
- The cash sheet charges about **$0.10 per kit per day** for holding raw material. The handout doesn't mention it, but it changes the kit policy.

## What the bot does

| Lever | Rule |
|---|---|
| Machines | Plans for forecast demand 3 to 10 days out (machines take 3 days to arrive) at 90% utilisation, plus enough extra to clear the backlog in about 6 days. Buys the most loaded station first, and only if the machine pays back 1.3x before day 300. |
| WIP cap | Throughput x 0.6 day. Lead time = WIP / throughput (Little's law), so this keeps a new job under the 1-day quote. Demand above capacity gets rejected instead of making every job late. |
| Contract | Moves to contract 2 ($1,000, 1 day) as soon as the backlog is gone, and goes back to contract 1 if lead time passes 1.8 days. Contract 3 is off: no-wait flow time is about 10.7h, against a 6h quote. |
| Lot size | 1 lot per job. Splitting adds setups the stuffer can't afford. |
| Tester | Priority to final test (pri4). This beat FIFO and pri2 in the twin. |
| Kits | EOQ with the real holding cost, reorder point = 4-day lead time + 1 day of safety stock. Stops ordering once stock covers forecast demand to day 300. |
| Loans | Borrows up to $600k early to fund capacity that pays back fast, and repays once cash allows. |
| Endgame | From day 200, sells machines the remaining demand doesn't need, keeping one spare. |
| Fallback | If contract and WIP never become editable, it stops buying kits after day 55 and holds the cash. In the twin that ends at ~$700k, against ~$0 if it keeps investing. |

## How we know it works

A discrete-event twin of the factory (`lf/sim.py`) reproduces the live game's day 50 to within 15%: lead time 19 to 22 vs 21.9 days, tester queue, throughput, tuner utilisation. `tests/test_sim_calibration.py` checks this.

Results over 8 random seeds, days 50 to 300:

| | Net cash at end |
|---|---|
| Do nothing | ~$44k |
| Bot | $2.27M average, $2.11M worst |
| Bot, demand 30% above or below the curve | $1.8M to $2.4M |

`tests/test_e2e.py` runs the real `bot.py` and HTTP client against a local copy of the game for a full season.

## Setup (10 minutes)

1. Create a **private** GitHub repo and upload everything in this folder, including `.github/`.
2. Settings > Secrets and variables > Actions > **Secrets**: add `LF_ID` = `8a` and `LF_PASSWORD` = your team password.
3. Actions tab > enable workflows.
4. Actions > littlefield-bot > **Run workflow** with "dry run" ticked. Open the run and check the first log line shows day, cash and machines. That proves login and reading work on the real site.
5. Run it once more with "dry run" unticked and watch the first changes land in the game. From then on the schedule takes over.

## Controls

- **Pause everything:** Settings > Variables > add `PAUSE` = `1`. Delete it to resume.
- **Dry run on the schedule:** variable `DRY_RUN` = `1`.
- **Change behaviour:** edit `config.yaml` on GitHub and commit. The next run uses it.
- **Test a change first:** `python scripts/evaluate.py util_target=0.85` scores it on the twin.
- **What it did:** `logs/actions.log`, or the summary of any run in the Actions tab. GitHub emails you if a run fails.

## Things to know

- **One session per team.** Each bot run logs in, which logs out anyone watching in a browser. The run takes about 20 seconds, so just log back in. Use the Standing page to watch.
- **Opening a URL the game doesn't know drops the session** (seen live). The client only requests pages the game itself links to.
- **Not yet seen live:** the order-menu and loan controls. Before the start they were hidden, so the bot finds them on the page at runtime instead of hard-coding them. If they never appear, the log shows `edits=OFF` and the fallback above takes over.
- GitHub cron can run a few minutes late. At ~29 minutes per simulated day that doesn't matter.

## Layout

```
bot.py              one run: login, read, decide, guard, act, log
config.yaml         knobs and hard limits
lf/client.py        HTTP client and page parsers for op2.responsive.net
lf/policy.py        decisions (pure function, same code for twin and live)
lf/sim.py           discrete-event digital twin, calibrated to days 1-50
lf/twin.py          runs the policy on the twin
data/               the 50-day history captured from the live game
tests/              parsers on live HTML, calibration, policy, full-season e2e on a mock server
scripts/evaluate.py score a config on the twin
```
