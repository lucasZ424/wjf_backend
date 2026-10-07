import struct

import pytest
from pydantic import ValidationError

from htsg.adapters import decode_registers
from htsg.config import Settings


def test_mapping_required_before_enabling_real_adapter(settings):
    data = settings.model_dump()
    data["sources"] = [{"id": "s", "kind": "modbus", "enabled": True}]
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_production_cannot_use_simulator_or_test_capture_durations(settings):
    data = settings.model_dump()
    data["mode"] = "production"
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_development_database_on_c_is_rejected(settings):
    data = settings.model_dump()
    data["storage"]["primary_path"] = "C:/bad"
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_modbus_decode_handles_explicit_word_order_and_conversion(settings):
    point = settings.points[0].model_copy(update={"word_order": "little", "scale": 2, "offset": 1})
    words = list(struct.unpack(">HH", struct.pack(">f", 12.5)))
    assert decode_registers(list(reversed(words)), point) == (26, "GOOD")


def test_profile_cannot_omit_derived_input(settings):
    data = settings.model_dump()
    data["profiles"][0]["derived_points"] = ["delta"]
    data["profiles"][0]["source_points"] = ["x"]
    data["profiles"][0]["trigger_points"] = ["x"]
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_opcua_deployment_configuration_rejects_more_than_two_hundred_channels(settings):
    data = settings.model_dump()
    data["sources"] = [{"id": "ua", "kind": "opcua", "enabled": False}]
    data["points"] = [
        {
            "id": f"ua_{i:03d}",
            "source": "ua",
            "unit": "test",
            "aggregation": "Last",
            "node_id": f"ns=2;s=ua_{i:03d}",
        }
        for i in range(201)
    ]
    data["derived"] = []
    data["profiles"] = []
    with pytest.raises(ValidationError, match="OPC UA channel capacity is 200"):
        Settings.model_validate(data)
