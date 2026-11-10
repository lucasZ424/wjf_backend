import json
import struct
from pathlib import Path

import pytest
from owner_support import small_config
from pydantic import ValidationError

from htsg.config import Settings
from htsg.modbus import decode_registers


def test_mapping_required_before_enabling_real_adapter():
    data = small_config().model_dump()
    data["sources"] = [{"id": "mb", "kind": "modbus", "enabled": True}]
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_production_cannot_enable_development_login_and_the_simulator_kind_is_gone():
    data = small_config().model_dump()
    data["mode"], data["dev_admin_login"] = "production", True
    with pytest.raises(ValidationError, match="only allowed in development"):
        Settings.model_validate(data)
    data["dev_admin_login"], data["sources"][0]["kind"] = False, "simulator"
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_a_development_database_on_c_is_accepted():
    """修复轮06: lifted - the rehearsal's emergency database is on C:."""
    data = small_config().model_dump()
    data["storage"]["primary_path"] = "C:/HTSG/primary"
    assert Settings.model_validate(data).storage.primary_path == "C:/HTSG/primary"


def test_modbus_decode_handles_explicit_word_order_and_conversion():
    point = small_config().points[0].model_copy(update={"word_order": "little", "scale": 2, "offset": 1})
    words = list(struct.unpack(">HH", struct.pack(">f", 12.5)))
    assert decode_registers(list(reversed(words)), point) == (26, "GOOD")


def ua_points(count):
    data = small_config().model_dump()
    data["sources"] = [{"id": "ua", "kind": "opcua", "enabled": False}]
    data["points"] = [
        {"id": f"ua_{i:03d}", "source": "ua", "unit": "test", "node_id": f"ns=2;s=ua_{i:03d}"} for i in range(count)
    ]
    data["derived"] = []
    return data


def test_opcua_deployment_configuration_takes_222_channels():
    """A2, Q1: 215 walls and 7 process quantities fill the capacity exactly."""
    assert len(Settings.model_validate(ua_points(222)).points) == 222


def test_opcua_deployment_configuration_rejects_a_223rd_channel():
    """A3, Q1: the limit was measured at this size; a bigger configuration does not start on the hope that it holds."""
    with pytest.raises(ValidationError, match="OPC UA channel capacity is 222"):
        Settings.model_validate(ua_points(223))


def test_a_ninth_modbus_bridge_is_rejected():
    """A4, 终版 §3.1: 8 bridges, one 16-register request a round."""
    data = small_config().model_dump()
    data["points"] = [
        {"id": f"b{i}", "source": "mb", "unit": "mV/V", "address": 2 * i, "data_type": "float32"} for i in range(9)
    ]
    data["derived"] = []
    with pytest.raises(ValidationError, match="Modbus Bridge channel capacity is 8"):
        Settings.model_validate(data)
    data["points"].pop()
    assert len(Settings.model_validate(data).points) == 8


@pytest.mark.parametrize("config", ["config/research-sim.json", "config/production.base.json"])
def test_no_channel_of_the_old_table_is_left_in_a_shipped_configuration(config):
    """A6 (用户 11-05): the ids the customer's table no longer has; a leftover would be subscribed and never answered."""
    data = json.loads(Path(config).read_text(encoding="utf-8"))
    text = json.dumps(data, ensure_ascii=False)
    for old in ("temp_inner", "temp_outer", "delta_t", "waterwall_flow", "main_steam_pressure", "thermal_stress\"", "restraint", "expansion_per_k"):
        assert old not in text, old
    assert not [d for d in data["derived"] if d["operation"] == "difference"]
