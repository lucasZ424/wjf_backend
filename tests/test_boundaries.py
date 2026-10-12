import pytest

from htsg.auth import Accounts


def test_failed_account_persistence_does_not_activate_new_credential(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")

    def fail(users):
        raise OSError("disk failure")

    accounts.persist = fail
    with pytest.raises(OSError):
        accounts.create("new", "viewer")
    assert "new" not in accounts.users
