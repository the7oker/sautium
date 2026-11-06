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
DAY_MS = 86_400_000
VECTORS = json.loads((HERE / "vectors" / "invite_wire_v1.json").read_text())

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@functools.cache
def _run(scenario: str, *flags: str) -> dict:
    out = subprocess.run(
        ["node", str(HERE / "worker_harness.mjs"), os.urandom(32).hex(), "test-email-pepper",
         "--scenario", scenario, *flags],
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


def test_a_key_under_another_name_spends_nobodys_counters():
    assert _run("names")["countersUntouchedByName"] is True


def test_a_recipient_is_one_plain_mailbox_counted_however_spelled():
    r = _run("mail")
    assert r["listSyntax"]["status"] == 400
    assert r["displayName"]["status"] == 400
    assert r["sameMailbox"] == [200, 200, 200, 429]


def test_invites_are_capped_per_key_per_sender_mailbox_and_in_all():
    r = _run("mail")
    assert r["perKey"] == [200] * 5 + [429]
    assert r["perMailbox"] == [200] * 5 + [429]           # two keys, one verified mailbox
    assert r["ceiling"] == [200, 429]


def test_the_days_mail_fits_the_mail_plan_codes_first():
    # The plan sends 100 mails a day and 3 000 a month: 60 codes, 24 invites
    # and 12 acceptance mails make 96 (96 x 31 = 2 976).
    assert _run("codes")["codeCeiling"] == [200, 429]
    assert _run("mail")["ceiling"] == [200, 429]
    assert _run("accepts")["notifyCeiling"] == [[200, True], [200, False]]


def test_a_mail_ceiling_ends_at_midnight_utc_and_a_persons_count_a_day_after_its_first_use():
    w = _run("codes")["window"]
    assert w["ceiling"] in {(w["t0"] // DAY_MS + 1) * DAY_MS, (w["t1"] // DAY_MS + 1) * DAY_MS}
    assert w["t0"] + DAY_MS <= w["person"] <= w["t1"] + DAY_MS


def test_a_full_ceiling_sends_its_waiting_callers_back_at_spread_times():
    # to the end of the ceiling's window (a mail route's: 00:00 UTC), plus up
    # to an hour each
    r = _run("mail")
    soonest = -(-(r["ceilingUntil"] - r["retriesTo"]) // 1000)
    latest = -(-(r["ceilingUntil"] - r["retriesFrom"]) // 1000) + 3_600
    assert all(soonest <= t <= latest for t in r["ceilingRetries"])
    assert len(set(r["ceilingRetries"])) > 1


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


# --- invites: the token, its one spending, the add by plain code, the pickup ---

def test_an_invite_mail_carries_a_token_the_worker_keeps_only_hashed():
    r = _run("accepts")
    assert r["sent"]["status"] == 200 and r["sent"]["body"]["verified_sender"] is True
    assert r["tokenIdMatches"] is True and r["stringInMail"] is True
    assert r["inviteMailTo"] == ["sasha@example.net"]
    [row] = r["rowsAfterSend"]
    assert row["inviter"] == r["keys"]["masha"] and row["inviter_code"] == r["keys"]["masha_code"]
    assert row["acceptor"] is None
    # no token, recipient or message in what the Worker keeps
    assert r["rowHoldsNothingOfTheMail"] is True


def test_an_invites_token_is_spent_once_and_its_acceptor_hears_the_same_answer_again():
    r = _run("accepts")
    accepted = r["accepted"]["body"]
    assert r["accepted"]["status"] == 200 and accepted["status"] == "accepted"
    assert accepted["invite_id"] == r["sent"]["body"]["invite_id"]
    assert accepted["inviter"] == {"invite_code": r["keys"]["masha_code"], "public_key_hex": r["keys"]["masha"]}
    assert accepted["notified"] is True
    again = r["acceptedAgain"]
    assert again["status"] == 200 and {k: v for k, v in again["body"].items() if k != "notified"} \
        == {k: v for k, v in accepted.items() if k != "notified"}
    assert r["mailsByAgain"] == 0
    assert r["usedByAnother"]["status"] == 409 and r["usedByAnother"]["body"]["reason"] == "used"
    assert r["raced"] == [200, 409] and r["racedRows"] == 1          # two keys at once: one of them


def test_an_acceptance_whose_mail_failed_is_told_at_the_retry_once():
    r = _run("told")
    assert r["toldFirst"] is False and r["toldRetry"] is True and r["toldAgain"] is True
    assert r["mailsByRetries"] == 1


def test_one_call_at_a_time_holds_the_right_to_mail_the_inviter():
    r = _run("told")
    assert r["atOnce"] == [200, 200] and r["mailsAtOnce"] == 1
    assert r["retriesAtOnce"] == [200, 200] and r["mailsByRetriesAtOnce"] == 1


def test_the_right_to_mail_is_the_inviters_own_address_and_a_mail_that_never_left_gives_it_back():
    r = _run("told")
    assert r["anothersRecord"] is False and r["mailsToAnothersRecord"] == 0
    assert r["notifyDown"] == 503 and r["retryAfterNotifyDown"] is True and r["mailsAfterNotifyDown"] == 1
    # a call's give-back clears its own lease only, never the next call's
    assert r["leases"] == {"firstHeld": True, "secondHeld": True, "secondKept": True}


def test_a_mail_that_went_never_fails_the_answer_and_a_right_not_given_back_runs_out():
    r = _run("told")
    assert r["recordFailed"]["status"] == 200 and r["recordFailed"]["body"]["notified"] is True
    assert r["retryAfterRecordFailed"] is False and r["mailsAfterRecordFailed"] == 1
    assert r["releaseFailed"] == 200
    assert r["retryWhileHeld"] is False and r["retryAfterLease"] is True and r["mailsAfterLease"] == 1


def test_a_refused_invite_names_its_reason():
    r = _run("accepts")
    for name, status, reason in (("badToken", 400, "bad_token"), ("upperToken", 400, "bad_token"),
                                 ("unknownToken", 404, "not_found"), ("expired", 404, "not_found"),
                                 ("notThisCode", 403, "not_this_code"), ("own", 403, "own")):
        assert r[name]["status"] == status and r[name]["body"]["reason"] == reason, name
    assert r["ownByCode"]["status"] == 400 and r["invalidTheirs"]["status"] == 400


def test_the_invite_routes_take_a_fresh_time_and_send_binds_its_message():
    r = _run("accepts")
    for name in ("staleSend", "staleAccept", "stalePickup"):
        assert r[name]["status"] == 403 and r[name]["body"] == {"error": "stale timestamp"}, name
    assert r["unsignedMessage"]["status"] == 403
    assert r["textTs"]["status"] == 400                         # a time is a number


def test_the_acceptance_mail_names_no_address_of_the_acceptors():
    r = _run("accepts")
    [mail] = r["acceptMail"]
    assert mail["to"] == ["masha@example.org"]
    assert mail["subject"] == "sasha accepted your Sautium invite"
    assert "Accepted through the invite you sent" in mail["html"]
    assert "sasha@example.net" not in mail["html"] and "Unverified" not in mail["html"]
    assert r["keys"]["sasha_code"] + "-" in mail["html"]        # the long code: the short one and more


def test_an_add_by_plain_code_keeps_nothing_and_mails_the_username_only():
    r = _run("accepts")
    assert r["added"]["body"] == {"status": "added", "notified": True}
    assert r["addKeptNothing"] is True
    [mail] = r["addMail"]
    assert mail["to"] == ["masha@example.org"]
    assert mail["subject"] == "sasha added your Sautium invite code"
    assert "sasha@example.net" not in mail["html"] and "@" not in mail["html"].split("<hr")[0]
    assert r["addToNoMailbox"]["body"] == {"status": "added", "notified": False}


def test_an_add_mails_only_the_address_the_codes_own_key_verified():
    r = _run("accepts")
    assert r["addToAnothersRecord"]["body"] == {"status": "added", "notified": False}
    assert r["mailsToAnothersRecord"] == 0
    # a record naming a key whose digest is not the code's, though it holds its own
    assert r["addToMismatchedRecord"]["body"] == {"status": "added", "notified": False}
    assert r["mailsToMismatchedRecord"] == 0


def test_the_pickup_is_a_signed_read_of_every_accepted_invite():
    r = _run("accepts")
    [picked] = r["pickup"]["body"]["accepts"]
    assert picked["invite_id"] == r["sent"]["body"]["invite_id"]
    assert picked["acceptor"]["invite_code"] == r["keys"]["sasha_code"]
    assert picked["acceptor"]["public_key_hex"] == r["keys"]["sasha"]
    assert isinstance(picked["acceptor"]["ts"], int) and len(picked["acceptor"]["signature"]) == 128
    assert r["pickupAgain"]["body"] == r["pickup"]["body"]       # nothing deleted
    assert r["pickupAfterOldKey"]["body"] == r["pickup"]["body"]   # stored acceptances are never read
    assert r["pickupUnderAnotherName"]["status"] == 403
    assert r["pickupByGet"]["status"] == 404


def test_a_mail_that_fails_leaves_no_invite_behind():
    r = _run("accepts")
    assert r["sendMailDown"]["status"] == 502 and r["invitesLeftByMailDown"] == 0


def test_acceptances_and_their_mail_and_pickups_are_capped_before_their_step():
    r = _run("accepts")
    assert r["acceptAtKeyCap"]["status"] == 429 and r["stepUntouchedAtCap"] is True
    assert r["acceptAtNotifyCap"]["status"] == 200 and r["acceptAtNotifyCap"]["body"]["notified"] is False
    assert r["mailsAtNotifyCap"] == 0
    assert r["pickupAtCap"]["status"] == 429


def test_the_acceptance_mails_one_address_sets_off_are_capped():
    r = _run("accepts")
    assert r["addAtNotifyAddrCap"]["body"] == {"status": "added", "notified": False}
    assert r["mailsAtNotifyAddrCap"] == 0
    assert r["addFromOtherAddr"]["body"] == {"status": "added", "notified": True}


def test_a_pickup_returns_a_page_the_newest_acceptance_first():
    # a page never hides a fresh acceptance behind old ones
    assert _run("accepts")["page"] == {"length": 300, "newestFirst": True, "oldestLeft": True,
                                       "newestInserted": True}


def test_an_expired_invite_is_removed_by_its_alarm():
    r = _run("accepts")
    assert r["alarmSet"] is True and r["expiredLeft"] == 0


# --- the invite wire's vectors, through the Worker ---------------------------

def test_the_worker_speaks_the_invite_wires_vectors():
    v, r = VECTORS, _run("vectors")
    assert r["keys"] == {"inviter": v["inviter"]["pubkey"], "acceptor": v["acceptor"]["pubkey"]}
    for name in ("send", "accept", "add", "pickup"):
        assert r[f"{name}Sig"] == v["signatures"][name], name
    assert r["sent"]["status"] == 200 and r["sent"]["body"]["invite_id"] == v["invite_id"]
    [mail] = r["inviteMail"]
    assert v["invite_string"] in mail["html"]
    assert "until 13 December 2025" in mail["html"]           # thirty days from the vectors' time
    assert r["accepted"]["status"] == 200
    assert r["accepted"]["body"]["inviter"] == {"invite_code": v["inviter"]["short_code"],
                                                "public_key_hex": v["inviter"]["pubkey"]}
    [accept_mail] = r["acceptMail"]
    assert v["acceptor"]["long_code"] in accept_mail["html"]
    [picked] = r["pickup"]["body"]["accepts"]
    assert picked == {"invite_id": v["invite_id"], "accepted_at": "2025-11-13T02:13:20.000Z",
                      "acceptor": {"invite_code": v["acceptor"]["short_code"], "public_key_hex": v["acceptor"]["pubkey"],
                                   "ts": v["ts"], "signature": v["signatures"]["accept"]}}
    assert r["added"]["body"] == {"status": "added", "notified": True}
    [add_mail] = r["addMail"]
    assert v["acceptor"]["long_code"] in add_mail["html"]


# --- an invite code's owner --------------------------------------------------

def test_an_invite_code_belongs_to_the_earliest_key_born_under_its_digest():
    r = _run("owners")
    assert r["lateHolds"] != r["lateCode"]                     # a later birth leaves the elder's claim
    # every invite route refuses the later key, for that reason
    assert r["lateRoutes"] == [[403, {"error": "invite code doesn't match public key"}]] * 6
    assert r["freshHolds"] is True                              # a birth claims its digest
    assert r["tieHolder"] == "aa" * 32                          # one second, two keys: the lesser


def test_an_elder_whose_claim_never_landed_holds_its_code_at_its_first_use():
    r = _run("owners")
    assert r["elderFirstUse"]["status"] == 200 and r["elderNowHolds"] is True
    assert r["undated"]["body"] == {"error": "no certificate for this key"}


def test_a_birth_never_waits_on_the_owners_instance_and_its_claim_lands_from_its_own():
    r = _run("shards")
    assert r["birthOwnersHang"] == 200 and r["claimedFromBirth"] is True
    # the owners instance failing: kept, retried a minute later, landed
    assert r["birthOwnersDown"] == 200 and r["owedWhileDown"] == 1
    assert 60_000 <= r["retryAfterMs"] <= 61_000
    assert r["claimedLater"] is True and r["owedAfter"] == 0 and r["alarmAfter"] is None


def test_a_claim_owed_during_a_handover_lands_with_the_next():
    r = _run("owed")
    assert r["afterFirstHandover"] == {"k1": True, "k2": False, "owed": 1, "again": True}
    assert r["afterSecond"] == {"k1": True, "k2": True, "owed": 0, "alarm": None}
    assert r["page"] == {"handovers": 2, "held": 600, "owed": 0}       # a page of 500 a handover


def test_a_claim_owed_while_a_handover_waits_waits_with_it_and_a_refusal_is_no_handover():
    r = _run("owed")
    assert r["secondOweLeavesTheWait"] is True
    assert r["refusedAnswer"] == {"owed": 2, "waits": True}


def test_a_handover_the_owners_instance_never_answers_ends_at_its_deadline():
    r = _run("owedhang", "--owed-timeout", "50")
    assert r["tookMs"] < 5000 and r["owed"] == 1 and r["waits"] is True
    assert r["landed"] is True


def test_a_birth_whose_own_instance_cannot_keep_its_claim_is_answered_and_claims_at_first_use():
    r = _run("shards")
    assert r["oweFails"] == 200 and r["claimedAfterOweFailed"] is False
    assert r["firstUse"] == 200 and r["claimedAtFirstUse"] is True


def test_the_births_stored_before_are_read_once_and_until_then_no_owner_is_answered():
    r = _run("owners")
    assert r["beforeBackfill"]["status"] == 503
    assert r["alarmAfterFirstStep"] is True and r["listsSoFar"] == 1
    # a step whose reads fail writes nothing; the alarm, retried, chains
    # itself to the end over pages KV may hand back short
    assert r["failedStep"].startswith("KV GET failed") and r["failedStepWroteNothing"] is True
    # a page's claims and the cursor past it are one transaction
    assert r["fullStep"].startswith("SQLITE_FULL") and r["fullStepWroteNothing"] is True
    assert r["steps"] >= 2 and r["backfillDone"] is True
    assert r["backfilled"] == 1201
    assert r["elderHolds"] is True                              # the earlier birth wins a later claim
    assert r["afterBackfill"]["status"] == 200


def test_a_stored_birth_the_backfill_missed_claims_at_its_first_use():
    r = _run("owners")
    assert r["missedFirstUse"]["status"] == 200 and r["missedHolds"] is True


def test_a_key_with_no_birth_holds_no_code():
    assert _run("owners")["unborn"]["body"] == {"error": "no certificate for this key"}


# --- the reserve share -------------------------------------------------------

def test_the_newcomers_share_of_the_stamp_ceilings_leaves_the_rest_to_nodes_already_known():
    r = _run("reserve")
    assert r["stampNewcomer"] == 429 and r["stampKnown"] == 200
    assert r["stampKnownAtCeiling"] == 429                     # the ceiling itself still holds
    assert r["lookupNewcomer"] == 429 and r["lookupKnown"] == 200
    assert r["newcomerAfter"] == 200


def test_the_newcomers_share_of_the_directorys_day_leaves_the_rest_to_nodes_already_known():
    r = _run("reserve")
    assert r["dirNewcomer"] == 429 and r["dirKnown"] == 200
    assert r["newCountedForKnown"] is True                     # a known node spends none of the share
    assert r["olderCallCountsAsNew"] is True                   # a call that says nothing of the age


# --- keys ----------------------------------------------------------------------

def test_a_small_order_key_gets_no_certificate():
    r = _run("smallorder")
    assert r["birth"]["status"] == 403 and r["signedHigh"]["status"] == 403
    assert r["certified"] is False
    # every encoding of the eight points, either sign bit, any case; nothing else
    assert r["everyEncoding"] == [True] * 21 and r["ordinaryKeys"] == [False] * 3


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
    assert r["ledgerUntouchedByRefusal"] is True           # neither recorded nor charged
    assert r["duplicateRoots"]["status"] == 400
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


def test_a_root_re_signed_from_an_older_format_counts_like_a_new_one():
    r = _run("stamps")
    assert r["resignOverKeyCap"]["status"] == 429
    assert r["resignAtKeyCap"]["status"] == 200
    assert r["resignedRecord"]["v"] == 2
    assert r["resignedRecord"]["date"] == "2026-01-01T00:00:00Z"     # the first date is kept
    assert r["resignOverAddrCap"]["status"] == 429                   # and on its address's count
    assert r["resignAtAddrCap"]["status"] == 200


def test_armed_the_budget_holds_for_requests_at_one_moment():
    r = _run("armed", "--armed")
    assert r["statuses"] == [200] * 6 + [429] * 2              # a new identity's burst is six
    assert r["tsKeys"] == 6


def test_armed_a_spent_budget_is_refused_before_the_caps_and_leaves_nothing():
    r = _run("armed", "--armed")
    assert r["spent"]["status"] == 429
    assert r["ledgerUntouched"] is True and r["countersUntouched"] is True
    assert r["spentWrote"] is False


def test_armed_no_verdict_is_no_stamp():
    r = _run("armed", "--armed")
    assert r["ledgerDown"]["status"] == 503 and r["ledgerDownWrote"] is False
    # the charging step's verdict decides: read but not charged, nothing is stamped
    assert r["chargeDown"]["status"] == 503 and r["chargeDownWrote"] is False


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
    # a read of the invite family under the brake claims nothing and steps no backfill
    assert r["checkPaused"]["status"] == 503 and r["ownersUntouched"] is True
    assert r["zeroIsOff"] == 200
    # the master still wakes and drains its mailbox; only its address hint waits
    assert r["masterWake"]["status"] == 426 and r["hintUnchanged"] is True
    assert r["masterDrain"]["status"] == 200


def test_under_the_brake_the_owner_claims_wait_and_go_on_once_it_is_lifted():
    r = _run("paused")
    assert r["firstStep"] == 503 and r["cursorAfterFirstStep"] is True
    # the backfill's alarm: no step, no list, a minute's wait
    a = r["alarmUnderBrake"]
    assert a["lists"] == 0 and a["cursorKept"] is True and 60_000 <= a["waitsMs"] <= 61_000
    assert r["backfillDoneAfterBrake"] is True
    # an elder's first use past the backfill claims nothing under the brake
    assert r["elderUnderBrake"]["status"] == 503 and r["elderClaimedNothing"] is True
    assert r["elderAfterBrake"] == 200 and r["elderHolds"] is True
    # a birth's claim kept in its instance is not handed over under the brake
    o = r["owedUnderBrake"]
    assert o["ownersUntouched"] is True and o["kept"] == 1 and 60_000 <= o["waitsMs"] <= 61_000
    assert r["owedAfterBrake"] == 0


def test_one_routes_limiter_failing_leaves_the_others_working():
    r = _run("shards")
    assert r["pickup"] == 503 and r["birth"] == 200


def test_a_full_master_mailbox_refuses_and_drops_nothing():
    r = _run("mailbox")
    assert r["answer"]["status"] == 429 and r["answer"]["body"] == {"error": "mailbox full"}
    assert r["oldestKept"] == 1 and r["rows"] == 20000


def test_an_older_mailbox_keeps_its_mail_counts_it_once_and_indexes_its_expiry():
    assert _run("mailbox")["upgraded"] == {
        "held": 5, "indexes": ["mail_expires"], "triggers": ["mail_held_in", "mail_held_out"], "rows": 5,
        "heldAfterRestart": 5}


def test_the_count_of_mail_held_follows_the_rows_whoever_writes_them():
    # this code's drops, ack and expiry, then an older Worker's insert and delete
    assert _run("drops")["heldExact"] == [True] * 4


def test_a_drop_whose_step_fails_after_its_writes_leaves_none_of_them():
    r = _run("drops")
    assert r["failedAfterWrite"] == [503, {"error": "temporarily unavailable"}]
    assert r["failedAfterWriteLeft"] is False


def test_the_master_mailbox_counts_the_mail_it_stores_per_sender_address_and_day():
    r = _run("drops")
    assert r["senderRefusals"] == [429] * 3 and r["dropsByRefusals"] == 0
    assert r["afterDrain"] == 429                          # drained mail still counts for the day
    assert r["addressCap"] == [[200, None], [429, "address cap reached"]]
    assert r["otherAddress"] == 200
    assert r["dayCap"]["status"] == 429 and r["dayCap"]["body"]["error"] == "mailbox busy"
    assert int(r["dayCap"]["headers"]["retry-after"]) > 0
    assert r["nextDay"] == 200 and r["dropsNextDay"] == 1


def test_a_refused_drop_reads_by_index_only():
    assert _run("drops")["scans"] == []


def test_a_drop_without_an_address_is_refused_before_any_write():
    # an older Worker's call during a deploy: the sender keeps it and retries
    r = _run("drops")
    assert r["olderWorker"] == [503, {"error": "temporarily unavailable"}]
    assert r["olderWorkerWrote"] is False


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


def test_directory_registrations_are_capped_per_address_and_in_all_a_day():
    r = _run("directory")
    assert r["firstOfDay"] == 200
    assert r["addrDayCap"] == 429 and r["rowsUnchangedByDayCap"] is True
    assert r["dayCeiling"] == 429 and int(r["dayCeilingRetryAfter"]) > 0


def test_a_registration_its_networks_cap_refuses_spends_none_of_its_day():
    # seventeen volunteers in one /48: sixteen listed, and only they counted
    assert _run("directory")["v6DayCount"] == [16]


def test_a_volunteer_past_its_own_day_spends_none_of_its_networks():
    # the per-address day holds sixteen keys' (16 x 30 = 480)
    r = _run("directory")
    assert r["keyDayCap"] == 429 and r["neighbourAtKeyCap"] == 200


def test_a_registration_without_an_address_is_refused_before_any_write():
    r = _run("directory")
    assert r["olderWorker"] == [503, {"error": "temporarily unavailable"}]
    assert r["olderWorkerWrote"] is False


# --- the birth ledger --------------------------------------------------------

def test_a_wave_of_births_does_not_raise_their_multiplier():
    last = _run("ledger")["last"]
    assert last["n_glob1"] == 60                           # counted, never priced
    assert last["m_shadow"] == 1
