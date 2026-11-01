"""The manual scenario trigger (scripts/trigger_scenario.py): one scenario named in a file, never a sequence.

Why it matters: the web pages have no way to start a fault, so this is the one hand-operated way to try a scenario on the
running lab.  It must refuse a bad file or name before it touches the simulator, say what went wrong when the simulator is not
there or the token is wrong, and never print the control token.
"""

import json
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from test_simulation import small_config

from htsg.simulation import create_simulation_app
from scripts import trigger_scenario as trig

TOKEN = "test-simulator-control-secret"


class Reply:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, *_):
        return self.body


def through(client):
    """An `urlopen` that talks to the real control app of the simulator in memory."""

    def opener(request, timeout=None):
        response = client.post("/scenario", content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "refused", {}, None)
        return Reply(response.content)

    return opener


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A lab folder with a token, a trigger file path, and the small research configuration."""
    local = tmp_path / "research"
    local.mkdir()
    (local / "simulation-secret.txt").write_text(TOKEN)
    monkeypatch.setattr(trig, "LOCAL", local)
    config = small_config()
    monkeypatch.setattr(trig, "load_settings", lambda path: config)
    monkeypatch.setenv(config.simulation.token_env, TOKEN)
    return local, config


def write(local, **choice):
    path = local / "trigger.json"
    path.write_text(json.dumps(choice))
    return str(path)


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"scenarios": ["step", "bad"]}', '{"scenario": "step", "then": "bad"}', '{"scenario": 3}'],
    ids=["not-json", "not-an-object", "a-sequence", "extra-key", "not-text"],
)
def test_the_trigger_file_names_one_scenario_and_nothing_else(tmp_path, content):
    path = tmp_path / "t.json"
    path.write_text(content)
    with pytest.raises(SystemExit):
        trig.read_choice(path)


def test_a_missing_trigger_file_says_how_to_make_one(tmp_path):
    with pytest.raises(SystemExit, match="Create it"):
        trig.read_choice(tmp_path / "absent.json")


def test_an_unknown_scenario_is_refused_before_any_request(lab, monkeypatch):
    local, _ = lab
    monkeypatch.setattr(trig, "trigger", lambda *a, **k: pytest.fail("the simulator must not be asked"))
    with pytest.raises(SystemExit, match=r"Unknown scenario 'missing'.*baseline, step"):
        trig.main(["--config", write(local, scenario="missing")])


def test_the_scenario_reaches_the_real_control_app_and_the_token_is_never_printed(lab, monkeypatch, capsys):
    local, config = lab
    app = create_simulation_app(config)
    monkeypatch.setattr(trig, "urlopen", through(TestClient(app)))
    trig.main(["--config", write(local, scenario="step")])
    assert app.state.field.scenario.name == "step", "the simulator is in the scenario that was named"
    out = capsys.readouterr()
    assert "Scenario step (thermal_step) is set, lasts 30 s, then the simulator returns to baseline" in out.out
    assert TOKEN not in out.out + out.err


def test_a_simulator_that_is_not_running_is_reported_not_a_traceback(lab, monkeypatch):
    local, _ = lab

    def down(request, timeout=None):
        raise URLError("refused")

    monkeypatch.setattr(trig, "urlopen", down)
    with pytest.raises(SystemExit, match=r"not reachable at 127\.0\.0\.1:\d+.*research_lab\.py run"):
        trig.main(["--config", write(local, scenario="step")])


def test_a_wrong_token_is_reported(lab, monkeypatch):
    local, config = lab
    (local / "simulation-secret.txt").write_text("another-secret-of-sixteen-or-more")
    monkeypatch.setattr(trig, "urlopen", through(TestClient(create_simulation_app(config))))
    with pytest.raises(SystemExit, match="HTTP 401.*token is wrong"):
        trig.main(["--config", write(local, scenario="step")])


def test_list_shows_every_scenario_of_the_research_configuration(capsys):
    trig.main(["--list"])
    out = capsys.readouterr().out
    for name in ("baseline", "load_ramp", "hotspot_01", "overload", "extreme_temperature", "bad_quality", "freeze", "disconnect_modbus", "disconnect_opcua"):
        assert name in out
    assert "20 s" in out and "1200 s" in out
