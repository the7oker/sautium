"""The Worker's write path and its mail (worker/verify.js, driven in Node by
tests/p2p/worker_harness.mjs with its stores emulated): who may make it write
or send, how much, and what a failing store or a malformed request gets."""

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@functools.cache
def _run(scenario: str) -> dict:
    out = subprocess.run(
        ["node", str(HERE / "worker_harness.mjs"), os.urandom(32).hex(), "test-email-pepper",
         "--scenario", scenario],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    return json.loads(out)


# --- the mail routes ---------------------------------------------------------

def test_an_invite_needs_a_certificate_and_a_verified_email():
    r = _run("mail")
    assert r["invited"]["status"] == 200
    [sent] = r["invitedMail"]
    assert sent["to"] == ["friend@example.net"]
    assert sent["subject"] == "alice invites you to Sautium"
    assert "Verified sender: <strong>alice@example.org</strong>" in sent["html"]
    assert r["unverified"]["status"] == 403
    assert r["uncertified"]["status"] == 403
    assert r["mailsAfterRefusals"] == 0


def test_the_invite_text_is_short_plain_text_counted_in_characters():
    r = _run("mail")
    assert "hello there" in r["invitedMail"][0]["html"]
    assert r["messageAtCap"]["status"] == 200
    assert r["messageOverCap"]["status"] == 400
    assert r["messageNotText"]["status"] == 400
    assert r["messageOfWideCharacters"]["status"] == 200   # 300 characters outside the BMP


def test_a_key_has_one_name_and_one_spelling_of_its_code():
    r = _run("mail")
    assert r["lowercaseCode"]["status"] == 403
    assert r["otherName"]["status"] == 403
    assert r["otherNameCode"]["status"] == 403
    # the first writing request fixes the name; a read fixes nothing
    assert r["boundByFirstUse"]["status"] == 403
    assert r["firstNameStill"]["status"] == 200
    assert r["readUnderAName"]["status"] == 200 and r["namesAfterRead"] == 0
    assert r["writeUnderAnother"]["status"] == 200


def test_a_name_unused_for_a_year_is_forgotten():
    r = _run("names")
    assert r["otherNameAtOnce"]["status"] == 403
    assert r["otherNameAfterAYear"]["status"] == 200


def test_a_recipient_is_one_plain_mailbox_counted_however_spelled():
    r = _run("mail")
    assert r["listSyntax"]["status"] == 400
    assert r["displayName"]["status"] == 400
    assert r["sameMailbox"] == [200, 200, 200, 429]


def test_invites_are_capped_per_key_per_sender_mailbox_and_in_all():
    r = _run("mail")
    assert r["perKey"] == [200] * 20 + [429]
    assert r["perMailbox"] == [200] * 20 + [429]          # two keys, one verified mailbox
    assert r["ceiling"] == [200, 429]


def test_a_full_ceiling_sends_its_waiting_callers_back_at_spread_times():
    retries = _run("mail")["ceilingRetries"]
    assert all(86_000 <= t <= 86_400 + 3_600 for t in retries)
    assert len(set(retries)) > 1


def test_concurrent_requests_cannot_pass_a_counter_together():
    r = _run("mail")
    assert r["concurrentStatuses"] == [200] * 3 + [429] * 9
    assert r["concurrentMails"] == 3


def test_each_mail_route_has_counters_of_its_own():
    # ten verification codes asked for one mailbox spend nothing of its invites
    assert _run("mail")["inviteAfterCodes"]["status"] == 200


def test_a_refused_mail_request_writes_nothing():
    r = _run("mail")
    assert r["refusedCode"]["status"] == 429 and r["refusedInvite"]["status"] == 403
    assert r["writesByRefusals"] == 0


def test_the_limiter_holds_no_address_no_mailbox_and_no_username():
    assert _run("mail")["counterKeysHoldNoAddress"] is True


def test_an_unreachable_mail_provider_is_a_502_without_its_text():
    r = _run("mail")["providerDown"]
    assert r["status"] == 502 and r["body"] == {"error": "email send failed"}


# --- the email code ----------------------------------------------------------

def test_five_wrong_tries_spend_a_pending_code():
    r = _run("codes")
    assert r["wrong"] == [403] * 5
    assert r["rightAfterFive"]["status"] == 403
    assert r["newCode"]["status"] == 200                   # a new code opens a new round
    assert r["rightAfterFour"]["status"] == 200
    assert r["otherMailbox"]["status"] == 403


def test_an_expired_code_is_removed_by_its_alarm():
    r = _run("codes")
    assert r["alarmSet"] is True and r["expiredLeft"] == 0


def test_codes_to_one_mailbox_are_capped_however_many_keys_ask():
    assert _run("codes")["codesToOneMailbox"] == [200] * 5 + [429]


# --- acceptances -------------------------------------------------------------

def test_an_acceptance_is_kept_under_the_code_its_owner_builds():
    r = _run("accepts")
    assert r["invalidTheirs"]["status"] == 400
    assert r["ownCode"]["status"] == 400
    assert r["accepted"]["status"] == 200 and r["accepted"]["body"]["notified"] is True
    assert r["notifiedTo"] == [["masha@example.org"]]
    [picked] = r["pickup"]["body"]["accepts"]
    assert picked["invite_code"].startswith("sasha#")
    assert r["pickupAgain"]["body"] == {"accepts": []}
    assert r["pickupUnderAnotherName"]["status"] == 403


def test_acceptances_and_their_mail_and_pickups_are_capped_before_any_write():
    r = _run("accepts")
    assert r["acceptAtKeyCap"]["status"] == 429 and r["writesByAcceptRefusal"] == 0
    assert r["acceptAtNotifyCap"]["body"] == {"status": "accepted", "notified": False}
    assert r["mailsAtNotifyCap"] == 0
    assert r["pickupAtCap"]["status"] == 429 and r["writesByPickupRefusal"] == 0


# --- timestamps --------------------------------------------------------------

def test_only_a_key_with_a_certificate_writes_a_timestamp():
    r = _run("stamps")
    assert r["unsigned"]["status"] == 400
    assert r["uncertified"]["status"] == 403
    assert r["certified"]["status"] == 200
    assert r["certifiedAgain"]["body"]["date"] == r["certified"]["body"]["date"]


def test_new_roots_are_capped_per_key_per_address_and_in_all():
    r = _run("stamps")
    assert r["keyCapTwo"]["status"] == 429 and r["writesByRefusal"] == 0
    assert r["keyCapOne"]["status"] == 200
    assert r["keyCapFull"]["status"] == 429
    assert r["knownRootAtCap"]["status"] == 200            # a known root costs nothing
    assert r["addrKeyShape"] == "stamp:addr:4."
    assert r["addrCapFull"]["status"] == 429
    assert r["otherAddr"]["status"] == 200
    assert r["ceiling"] == [200, 429]
    assert r["knownAtCeiling"]["status"] == 200


def test_the_roots_a_timestamp_asks_about_are_counted_before_they_are_read():
    r = _run("stamps")
    assert r["lookupsOverKeyCap"]["status"] == 429
    assert r["readsByLookupRefusal"] == 1                  # the certificate, no root
    assert r["lookupsUnderKeyCap"]["status"] == 200
    # a request that brings no new root leaves no row in the ledger
    assert r["knownRootAgain"]["status"] == 200 and r["ledgerRowsByKnownRoot"] == 0


# --- the stores and the request ----------------------------------------------

def test_a_failing_store_answers_503_without_its_text():
    r = _run("store")
    for name in ("birthKvDown", "readKvDown", "limiterDown"):
        assert r[name]["status"] == 503, name
        assert r[name]["body"] == {"error": "temporarily unavailable"}, name
        assert r[name]["headers"]["retry-after"] == "60", name


def test_a_birth_is_in_kv_before_the_ledger_records_it():
    r = _run("store")
    assert r["ledgerRowsAfterFailedBirth"] == 0
    assert r["birthAfter"]["status"] == 200
    assert r["ledgerRowsAfterBirth"] == 1


def test_births_at_the_same_moment_are_all_issued_and_all_recorded():
    # KV takes one write a second to a key: no birth may share one
    r = _run("burst")
    assert r["statuses"] == [200] * 6
    assert r["ledgerRows"] == 6 and r["dailyBirths"] == 6


def test_the_daily_issuance_counts_are_adopted_once_from_kv():
    assert _run("history")["daily"] == [
        {"day": "2026-10-31", "field": "births", "n": 2},
        {"day": "2026-10-31", "field": "succession", "n": 1},
        {"day": "2026-11-01", "field": "births", "n": 3},
        {"day": "2026-11-01", "field": "email", "n": 1}]


def test_the_owners_brake_stops_every_write_and_no_read():
    r = _run("paused")
    for name in ("birth", "stamp", "code", "pickup", "legacyRead"):
        assert r[name]["status"] == 503, name
        assert r[name]["body"] == {"error": "temporarily unavailable"}, name
    assert r["read"]["status"] == 200 and r["stats"]["status"] == 200
    assert r["writesWhilePaused"] == 0 and r["limiterUntouched"] is True
    assert r["zeroIsOff"] == 200


def test_one_routes_limiter_failing_leaves_the_others_working():
    r = _run("shards")
    assert r["pickup"] == 503 and r["birth"] == 200


def test_a_full_master_mailbox_refuses_and_drops_nothing():
    r = _run("mailbox")
    assert r["answer"]["status"] == 429 and r["answer"]["body"] == {"error": "mailbox full"}
    assert r["oldestKept"] == 1 and r["rows"] == 20000


def test_any_other_failure_is_a_500_without_its_text():
    r = _run("store")["internal"]
    assert r["status"] == 500 and r["body"] == {"error": "internal error"}


def test_births_are_capped_per_address_and_in_all_before_any_write():
    r = _run("store")
    assert r["birthAtCeiling"]["status"] == 429
    assert r["writesByRefusal"] == 0 and r["ledgerRowsAfterRefusal"] == 0
    assert r["perAddress"] == [200, 429]


def test_a_body_is_one_json_object_under_the_size_cap():
    r = _run("store")
    assert r["textPlain"]["status"] == 415
    assert r["formPost"]["status"] == 415
    assert r["badJson"]["status"] == 400
    assert r["notObject"]["status"] == 400
    assert r["tooLarge"]["status"] == 413
    assert r["charsetJson"]["status"] == 200


def test_no_answer_grants_a_browser_origin():
    r = _run("store")
    assert not [h for h in r["preflight"]["headers"] if h.startswith("access-control")]
    assert r["anyAnswerCors"] == []


def test_a_signature_is_read_at_its_exact_length():
    r = _run("store")
    assert r["sigOddLength"]["status"] == 403
    assert r["sigUpper"]["status"] == 200


# --- the directory -----------------------------------------------------------

def test_one_network_holds_at_most_sixteen_directory_entries():
    r = _run("directory")
    assert r["v4"] == [200] * 16 + [429]
    assert r["v4Again"]["status"] == 200                   # a listed volunteer renews
    assert r["v4Elsewhere"]["status"] == 200
    assert r["v6"] == [200] * 16 + [429]                   # one /48 across its /64s
    assert r["crowdRefusal"] == 429 and r["rowsUnchangedByCrowdRefusal"] is True


def test_directory_rows_from_before_the_network_columns_get_them():
    assert [(row["net4"], row["net6"]) for row in _run("directory")["backfilled"]] == [
        ("192.0.2.0/24", None), (None, "2001:db8:5::/48")]


def test_the_directory_total_counts_networks_not_entries():
    assert _run("directory")["total"] == 2


def test_directory_registrations_are_limited_per_ipv6_slash64_before_any_write():
    r = _run("directory")
    assert r["sameSlash64"] == [200] * 12 + [429]
    assert r["writesByRefusal"] == 0 and r["rowsUnchangedByRefusal"] is True


# --- the birth ledger --------------------------------------------------------

def test_a_wave_of_births_does_not_raise_their_multiplier():
    last = _run("ledger")["last"]
    assert last["n_glob1"] == 60                           # counted, never priced
    assert last["m_shadow"] == 1
