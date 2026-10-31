import pytest

from htsg.auth import Accounts


def test_failed_account_persistence_does_not_activate_new_credential(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")

    def fail(entry):
        raise OSError("disk failure")

    accounts.record = fail
    with pytest.raises(OSError):
        accounts.create("new", "viewer", {"action": "CREATE_ACCOUNT"})
    assert "new" not in accounts.users
