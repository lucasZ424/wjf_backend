"""R02 / Q04, Q06: the text of a library error is cleaned and bounded where it is made, and what is left out says so.

Why it matters: this text reaches a page that every signed-in user reads (终版 §15.1 原因证据: credentials removed,
cut marked, queue and record length bounded).  Q04: a long message cut BEFORE it was cleaned could keep the start of
a credential.  Q06: a Modbus round carried the library's raw text through the ordered entry, so the bound on the number
of queued batches was no bound on memory.  The rule (整改方案 R02, 细则 4.1): above 4096 characters of raw text nothing
is kept; up to 4096 it is cleaned whole first and then cut to 512; in a URL everything from `scheme://` to the nearest
`@` is masked, spaces included, and so is whatever follows up to the last `@` that is joined to it without a blank or a
slash (a password with an unescaped `@`, the user's choice of 2026-10-19) - masking a little too much is the accepted
price of a rule that can be decided.
"""

import dataclasses

import pytest
from owner_support import Recorder, small_config
from protocol_support import FieldModbus, bridge_points
from pymodbus.exceptions import ModbusIOException
from test_health_communication import Rig as HealthRig
from test_health_communication import at

from htsg import records as r
from htsg.commlog import clean_detail
from htsg.config import Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner

SECRET = "SYNTHSECRET"


def url_with_secret(times):
    return "opc.tcp://user:" + SECRET * times + "@localhost/"


# the seven synthetic inputs of the Q04 probe (问题清单复核意见书 Q04), built from their description
SYNTHETIC = {
    "A: one URL, its @ beyond 4096": url_with_secret(400),
    "B: a sentence, then that URL as the last word": "the connection could not be opened: " + url_with_secret(400),
    "C: that URL, then many words": url_with_secret(400) + " word" * 2000,
    "D: the user information written as many words": "opc.tcp://user:" + " ".join([SECRET] * 600) + "@localhost/",
    "E: a long prefix, then password=": "x" * 5000 + " password=" + SECRET,
    "G: password= across the 4096 border": "y" * 4090 + " password=" + SECRET * 3,
}


# ------------------------------------------------------------------------------------- the cleaning rule


@pytest.mark.parametrize("text", [*SYNTHETIC.values(), url_with_secret(1)], ids=[*SYNTHETIC, "F: a short URL (control)"])
def test_no_part_of_a_credential_survives_whatever_the_length_or_shape(text):
    """Q04: of the seven probes A, C and D kept the start of the secret before the change."""
    detail, _ = clean_detail(text)
    assert "SYNTH" not in (detail or "")


@pytest.mark.parametrize("text", list(SYNTHETIC.values()), ids=list(SYNTHETIC))
def test_raw_text_above_4096_characters_is_left_out_whole_and_marked(text):
    """细则 4.1: no partial keeping - a text that long cannot be cleaned on the strength of a part of it."""
    assert len(text) > 4096
    assert clean_detail(text) == (None, True)


def test_the_limit_is_4096_characters_of_raw_text_to_the_character():
    assert clean_detail("y" * 4096) == ("y" * 512, True), "4096 is cleaned whole, then cut to 512 and marked"
    assert clean_detail("y" * 4097) == (None, True), "4097 is left out"


def test_a_short_url_is_masked_up_to_its_at_sign_whatever_stands_in_between():
    """Q04 probe D at short length: user information written as several words (the old rule stopped at the first blank)."""
    assert clean_detail("opc.tcp://user:SYNTH SECRET@localhost/") == ("opc.tcp://***@localhost/", False)
    assert clean_detail(url_with_secret(1)) == ("opc.tcp://***@localhost/", False)


def test_masking_up_to_the_nearest_at_sign_may_take_more_than_the_user_information_and_that_is_the_accepted_trade():
    """Masking too much is the accepted trade-off (the user's M6 decision, 整改方案 第2.3节): a rule of this kind
    needs no URL parser, and the loss is a little diagnostic text where the other mistake would be a leak."""
    raw = "connect opc.tcp://host:4840 failed; mail admin@example.com"
    assert clean_detail(raw) == ("connect opc.tcp://***@example.com", False)


def test_each_url_is_masked_up_to_its_own_nearest_at_sign():
    raw = "first opc.tcp://a:b@h1 and then opc.tcp://c:d@h2"
    assert clean_detail(raw) == ("first opc.tcp://***@h1 and then opc.tcp://***@h2", False)


def test_a_password_with_an_unescaped_at_sign_is_masked_up_to_the_last_one_and_the_host_stays_readable():
    """R02, the user's choice (b): `u:p@ss@host` used to show `ss@host` - the part of the password after its first `@`,
    which is a part of a secret.  The rule goes on past every `@` that follows without a blank or a slash."""
    assert clean_detail("cannot open opc.tcp://u:p@ss@host/ca") == ("cannot open opc.tcp://***@host/ca", False)
    assert clean_detail("opc.tcp://user:pw1@pw2@pw3@host:4840/ca") == ("opc.tcp://***@host:4840/ca", False)
    assert clean_detail("opc.tcp://u:p%40ss@host/ca") == ("opc.tcp://***@host/ca", False), "an encoded one has a single @"
    assert clean_detail("opc.tcp://user:SYNTH SECRET@x@y@localhost/") == ("opc.tcp://***@localhost/", False)


def test_two_urls_with_an_unescaped_at_sign_each_are_masked_each_up_to_its_own_last_one():
    raw = "first opc.tcp://a:p@q@h1 and then opc.tcp://c:r@s@h2"
    assert clean_detail(raw) == ("first opc.tcp://***@h1 and then opc.tcp://***@h2", False)


def test_the_run_of_at_signs_ends_at_a_blank_or_a_slash_so_the_rest_of_a_sentence_and_a_path_are_kept():
    """The extension must not swallow what follows the address: a blank ends the user information, and so does a slash."""
    assert clean_detail("opc.tcp://u:p@a@b c@d") == ("opc.tcp://***@b c@d", False)
    assert clean_detail("opc.tcp://u:p@host/ca@x") == ("opc.tcp://***@host/ca@x", False)
    assert clean_detail("opc.tcp://u:p@host and admin@example.com") == ("opc.tcp://***@host and admin@example.com", False)


def test_cleaning_a_text_twice_changes_nothing_more():
    """The owner cleans again what the protocol side cleaned (细则 4.1): the second pass must find nothing left to do."""
    once, _ = clean_detail("cannot open opc.tcp://u:pw1@pw2@host/ca and opc.tcp://v:q@h2")
    assert clean_detail(once) == (once, False)


@pytest.mark.parametrize(
    "plain",
    [
        "opc.tcp://127.0.0.1:4840/",
        "127.0.0.1:502",
        "Modbus Error: [Input/Output] No response received after 0 retries, continue with next request",
        repr(ConnectionRefusedError(10061, "No connection could be made because the target machine actively refused it")),
        "connect call did not return within 0.8 s",
    ],
)
def test_ordinary_diagnostic_text_is_kept_exactly(plain):
    """细则 4.3: no user information in the URL means nothing is masked; common library messages stay as they are."""
    assert clean_detail(plain) == (plain, False)


# ---------------------------------------------------------------------------- where the text is made


class RaisingClient:
    """A client whose every request fails with `error` - the library's exception, whatever text it carries."""

    def __init__(self, error):
        self.error = error

    async def read_holding_registers(self, *args, **kw):
        raise self.error

    read_input_registers = read_holding_registers


def acquirer_of(owner, ingress):
    mb_points = [p for p in owner.config.points if p.source == "mb"]
    return ModbusAcquirer(owner.config.sources[0], mb_points, Timing(), ingress)


async def test_a_round_carries_the_text_cleaned_and_cut_not_the_raw_one():
    """Q06: the text is made safe before the batch exists, so nothing downstream ever holds the raw one."""
    owner = Owner(small_config(), 1000.0, 100.0, run_id="run")
    acquirer = acquirer_of(owner, Ingress(owner, 100))
    secret_text = "no answer from opc.tcp://admin:" + SECRET + "@10.0.0.9/ " + "z" * 3000
    batch, outcome = await acquirer.poll(RaisingClient(ModbusIOException(secret_text)), 1)
    (request,) = batch.requests
    assert outcome == "TIMEOUT" and request.truncated is True and len(request.detail) == 512
    assert "SYNTH" not in request.detail and "admin" not in request.detail and "opc.tcp://***@10.0.0.9/" in request.detail


async def test_the_librarys_own_timeout_text_passes_through_unchanged():
    """细则 4.3: a real pymodbus request that is not answered gives the library's common short text; it is kept as is."""
    peer = FieldModbus()
    peer.delay = 1.0
    await peer.start()
    try:
        owner = Owner(small_config(), 1000.0, 100.0, run_id="run")
        acquirer = ModbusAcquirer(
            peer.source(), bridge_points(), Timing(modbus_request_timeout=0.15, modbus_connect_timeout=0.3), Ingress(owner, 10)
        )
        client = acquirer.client_factory()
        assert await client.connect()
        try:
            batch, outcome = await acquirer.poll(client, 1)
        finally:
            client.close()
    finally:
        peer.delay = 0
        await peer.stop()
    (request,) = batch.requests
    assert outcome == "TIMEOUT" and request.truncated is False
    assert request.detail == "Modbus Error: [Input/Output] No response received after 0 retries, continue with next request"


async def test_a_five_megabyte_exception_text_is_left_out_where_it_is_made_and_stays_so_everywhere(tmp_path):
    """Q06: the raw text of 5,000,000 characters is gone from the poll result, from the ordered entry while the batch
    waits there, from the owner's log and from /api/health - and each of them says that it was left out."""
    rig = HealthRig(tmp_path)
    ingress = Ingress(rig.owner, 100)
    rig.owner.begin_generation("mb", 1, *at(101.0))
    acquirer = acquirer_of(rig.owner, ingress)
    batch, outcome = await acquirer.poll(RaisingClient(ModbusIOException("E" * 5_000_000)), 1)
    assert outcome == "TIMEOUT"
    assert [(q.detail, q.truncated) for q in batch.requests] == [(None, True)], "at the poll result"

    ingress.offer(dataclasses.replace(batch, conn_gen=1, wall=1002.0, mono=102.0))
    waiting = ingress.queue[-1]
    assert [(q.detail, q.truncated) for q in waiting.requests] == [(None, True)], "while it waits in the ordered entry"

    ingress.drain()
    (logged,) = [e for e in rig.owner.commlog.snapshot()["entries"] if e["event"] == "POLL_TIMEOUT"]
    assert (logged["detail"], logged["detail_truncated"]) == (None, True), "in the owner's log"

    health = await rig.health()
    (served,) = [e for e in health["communication"]["log"]["entries"] if e["event"] == "POLL_TIMEOUT"]
    assert (served["detail"], served["detail_truncated"]) == (None, True), "in the /api/health response"


def test_the_owner_never_turns_a_cut_that_was_made_at_the_protocol_side_back_into_a_whole_text():
    """细则 4.2: the owner cleans once more; the flag set earlier is kept, not overwritten with the result of the second
    cleaning of a text that is already short."""
    owner = Owner(small_config(), 1000.0, 100.0, commit=Recorder().commit, run_id="run")
    owner.begin_generation("mb", 1, 1001.0, 101.0)
    request = r.RequestResult("holding/1/0+4", "TIMEOUT", None, "no answer", True)
    owner.accept(
        r.InputBatch("mb", 1, r.KIND_MODBUS_POLL, 1002.0, 102.0, (), tick=1, poll_id="p1", outcome="TIMEOUT", requests=(request,))
    )
    (entry,) = [e for e in owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    assert (entry.detail, entry.detail_truncated) == ("no answer", True)
