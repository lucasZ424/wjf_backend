"""Trigger ONE scenario of the independent simulated field, by hand (the web pages have no such control).

    python scripts/trigger_scenario.py --list          the scenarios of the research configuration
    python scripts/trigger_scenario.py                 trigger the scenario named in .local/research/trigger.json
    python scripts/trigger_scenario.py --config FILE   ... or in another trigger file

The trigger file is JSON with the scenario to start, e.g.  {"scenario": "freeze"}  (and, only when the research
configuration is not config/research-sim.json, "research_config": "<path>").  One run starts one scenario: there is no sequence;
to try the next one edit the file and run again.  The scenario starts with the simulator's next generation step and lasts its
`duration`, then the simulator returns to `baseline`; starting one while another runs replaces it.

The simulator must be running (`scripts/research_lab.py run`).  Its control token is read from
.local/research/simulation-secret.txt and is never printed.  Nothing here writes to a real device or to the backend.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from htsg.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/research"
DEFAULT_TRIGGER = LOCAL / "trigger.json"
DEFAULT_CONFIG = "config/research-sim.json"
EXAMPLE = '{"scenario": "freeze"}'


def read_choice(path) -> dict:
    """The trigger file: one scenario name, nothing else but the research configuration to take the control address from."""
    try:
        choice = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"No trigger file at {path}.  Create it, e.g. {EXAMPLE}; --list shows the names.") from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from None
    if not isinstance(choice, dict) or not isinstance(choice.get("scenario"), str) or set(choice) - {"scenario", "research_config"}:
        raise SystemExit(f'{path} must be one object with a text "scenario" (and optionally "research_config"), e.g. {EXAMPLE}')
    return choice


def listing(config) -> str:
    rows = [f"{'name':<22}{'kind':<20}{'lasts':>8}  label"]
    for s in config.simulation.scenarios:
        lasts = "-" if s.kind == "baseline" else f"{s.duration:g} s"
        rows.append(f"{s.name:<22}{s.kind:<20}{lasts:>8}  {s.label}")
    return "\n".join(rows)


def trigger(config, token: str, scenario: str, opener=None) -> dict:
    """POST the scenario to the simulator's control interface; the simulator's status comes back."""
    opener = opener or urlopen
    sim = config.simulation
    request = Request(
        f"http://{sim.control_host}:{sim.control_port}/scenario",
        data=json.dumps({"scenario": scenario}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=5) as response:
            return json.load(response)
    except HTTPError as error:
        why = {401: "the control token is wrong", 422: "the simulator does not know that scenario"}.get(error.code, "see .local/research/simulation.log")
        raise SystemExit(f"The simulator refused it (HTTP {error.code}): {why}.") from None
    except OSError:
        raise SystemExit(
            f"The simulator is not reachable at {sim.control_host}:{sim.control_port} - is `scripts/research_lab.py run` running?"
        ) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_TRIGGER), help="the trigger file (JSON); default .local/research/trigger.json")
    parser.add_argument("--list", action="store_true", help="list the scenarios of the research configuration and exit")
    args = parser.parse_args(argv)
    if args.list:
        print(listing(load_settings(ROOT / DEFAULT_CONFIG)))
        return
    choice = read_choice(args.config)
    config = load_settings(ROOT / choice.get("research_config", DEFAULT_CONFIG))
    scenarios = {s.name: s for s in config.simulation.scenarios}
    if choice["scenario"] not in scenarios:
        raise SystemExit(f"Unknown scenario {choice['scenario']!r}.  The scenarios are: {', '.join(scenarios)}.")
    try:
        token = (LOCAL / "simulation-secret.txt").read_text().strip()
    except FileNotFoundError:
        raise SystemExit(f"No control token at {LOCAL / 'simulation-secret.txt'} (written by research_lab.py prepare).") from None
    status = trigger(config, token, choice["scenario"])
    chosen = scenarios[status["scenario"]]
    after = "" if chosen.kind == "baseline" else f", lasts {chosen.duration:g} s, then the simulator returns to baseline"
    print(f"Scenario {chosen.name} ({chosen.kind}) is set{after}.")


if __name__ == "__main__":
    main()
