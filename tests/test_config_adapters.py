import struct

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


def test_opcua_deployment_configuration_rejects_more_than_two_hundred_channels():
    data = small_config().model_dump()
    data["sources"] = [{"id": "ua", "kind": "opcua", "enabled": False}]
    data["points"] = [
        {"id": f"ua_{i:03d}", "source": "ua", "unit": "test", "node_id": f"ns=2;s=ua_{i:03d}"} for i in range(201)
    ]
    data["derived"] = []
    with pytest.raises(ValidationError, match="OPC UA channel capacity is 200"):
        Settings.model_validate(data)
