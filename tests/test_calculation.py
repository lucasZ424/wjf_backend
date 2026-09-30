from htsg.calculation import calculate
from htsg.config import Derived


def test_chained_calculation_cannot_hide_held_input_age(settings):
    rules = (
        Derived(id="a", inputs=("x",), operation="sum", unit="test", max_age=10),
        Derived(id="b", inputs=("a",), operation="sum", unit="test", max_age=2),
    )
    config = settings.model_copy(update={"derived": rules})
    frame = {
        "run_id": "r",
        "window_start": 100,
        "window_end": 101,
        "values": {"x": {"value": 1, "quality": "GOOD", "basis": "HELD_VALUE", "basis_time": 96}},
    }
    result = calculate(config, frame, None)["derived_results"]
    assert result["a"]["status"] == "OK" and result["a"]["max_input_age"] == 5
    assert result["b"]["value"] is None


def test_derivative_rejects_previous_frame_with_stale_hold(settings):
    rule = Derived(id="d", inputs=("x",), operation="derivative", unit="test/s", max_age=2)
    config = settings.model_copy(update={"derived": (rule,)})
    previous = {
        "run_id": "r",
        "window_start": 99,
        "window_end": 100,
        "derived_results": {},
        "values": {"x": {"value": 1, "quality": "GOOD", "basis": "HELD_VALUE", "basis_time": 90}},
    }
    current = {
        "run_id": "r",
        "window_start": 100,
        "window_end": 101,
        "values": {"x": {"value": 2, "quality": "GOOD", "basis": "WINDOW_VALUE"}},
    }
    assert calculate(config, current, previous)["derived_results"]["d"]["reason"] == "PREVIOUS_UNAVAILABLE"
