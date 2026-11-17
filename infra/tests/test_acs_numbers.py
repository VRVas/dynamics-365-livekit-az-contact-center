import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import acs_numbers as acs
import stage_native_media_topic as topics


ENDPOINT = "https://example-acs.communication.azure.com"
TENANT = "11111111-1111-4111-8111-111111111111"
# Ofcom drama numbers (0161 496 0xxx): fictional by design.
PLAIN, ROUND, RUN, PAIRS, SMS = "+441614960958", "+441614960000", "+441614960123", "+441614960707", "+441614960555"


class Capability(str, Enum):
    NONE = "none"
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INBOUND_OUTBOUND = "inbound+outbound"


class NumberType(str, Enum):
    GEOGRAPHIC = "geographic"
    TOLL_FREE = "tollFree"


class Assignment(str, Enum):
    PERSON = "person"
    APPLICATION = "application"


def available(**fields):
    return SimpleNamespace(**{"id": None, "phone_number": None, "cost": None, "status": None, "error": None,
                              "is_agreement_to_not_resell_required": None, **fields})


FAKE_SDK = SimpleNamespace(
    AvailablePhoneNumber=available, PhoneNumberCapabilities=lambda **fields: SimpleNamespace(**fields),
    PhoneNumberCapabilityType=Capability, PhoneNumberType=NumberType, PhoneNumberAssignmentType=Assignment,
    PhoneNumbersClient=MagicMock(),
)


def offer(number, *, calling="inbound+outbound", sms="none", assignment="person", amount=1.0, currency="GBP",
          frequency="monthly", resell=False, country="GB", kind="geographic", status="available", error=None):
    return SimpleNamespace(
        id=number.lstrip("+"), phone_number=number, country_code=country, phone_number_type=kind,
        assignment_type=assignment, capabilities=SimpleNamespace(calling=calling, sms=sms),
        cost=SimpleNamespace(amount=amount, currency_code=currency, billing_frequency=frequency),
        status=status, is_agreement_to_not_resell_required=resell, error=error,
    )


class ServiceError(Exception):
    def __init__(self, status, code):
        super().__init__(f"({code}) The service refused the request.")
        self.status_code, self.error = status, SimpleNamespace(code=code)


class FakePhoneNumbers:
    def __init__(self, offers):
        self.offers, self.journal = offers, None
        self.browse_calls, self.reserve_calls, self.purchase_calls = [], [], []
        self.reservations, self.purchased, self.overrides = {}, {}, {}
        self.reserve_error = self.purchase_error = None
        self.expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    def browse_available_phone_numbers(self, **request):
        self.browse_calls.append(request)
        return SimpleNamespace(phone_numbers=list(self.offers))

    def create_or_update_reservation(self, *, reservation_id, numbers_to_add):
        assert (self.journal / "reservation-intent.json").is_file(), "Journal the reservation before sending it."
        self.reserve_calls.append((reservation_id, numbers_to_add))
        if self.reserve_error:
            raise self.reserve_error
        source = {item.phone_number: item for item in self.offers}
        entries = {}
        for item in numbers_to_add:
            number = "+" + item.id
            readback = offer(
                number, calling=item.capabilities.calling.value, sms=item.capabilities.sms.value,
                assignment=item.assignment_type.value, kind=item.phone_number_type.value, country=item.country_code,
                amount=source[number].cost.amount, resell=source[number].is_agreement_to_not_resell_required,
                status="reserved",
            )
            for key, value in self.overrides.get(number, {}).items():
                setattr(readback, key, value)
            entries[item.id] = readback
        self.reservations[reservation_id] = SimpleNamespace(
            id=reservation_id, status="active", expires_at=self.expires_at, phone_numbers=entries,
        )
        return self.reservations[reservation_id]

    def get_reservation(self, reservation_id):
        if reservation_id not in self.reservations:
            raise ServiceError(404, "ReservationNotFound")
        return self.reservations[reservation_id]

    def begin_purchase_reservation(self, reservation_id, *, agree_to_not_resell=False):
        assert (self.journal / "purchase-intent.json").is_file(), "Journal the purchase before sending it."
        self.purchase_calls.append((reservation_id, agree_to_not_resell))
        if self.purchase_error:
            raise self.purchase_error
        for entry in self.reservations[reservation_id].phone_numbers.values():
            self.purchased[entry.phone_number] = SimpleNamespace(
                id=entry.id, phone_number=entry.phone_number, country_code=entry.country_code,
                phone_number_type=entry.phone_number_type, assignment_type=entry.assignment_type,
                capabilities=entry.capabilities, cost=entry.cost, purchase_date=datetime(2026, 11, 17, tzinfo=timezone.utc),
            )
        self.reservations[reservation_id].status = "completed"
        return SimpleNamespace(wait=lambda timeout=None: None, done=lambda: True)

    def get_purchased_phone_number(self, number):
        if number not in self.purchased:
            raise ServiceError(404, "PhoneNumberNotFound")
        return self.purchased[number]

    def list_purchased_phone_numbers(self):
        return list(self.purchased.values())


@contextmanager
def private_test_directory():
    # Private outputs live outside the checkout; the "checkout" subfolder stands in for the repository.
    with tempfile.TemporaryDirectory(prefix="acs-numbers-test-") as name:
        directory = Path(name).resolve()
        if directory.is_relative_to(ROOT):
            raise RuntimeError("Point TMPDIR outside the checkout; tests must not write into the repository.")
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout):
            yield directory


class NumberToolTests(unittest.TestCase):
    def setUp(self):
        manager = private_test_directory()
        self.root = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        sdk = patch.object(acs, "sdk", return_value=FAKE_SDK)
        sdk.start()
        self.addCleanup(sdk.stop)
        self.phone = FakePhoneNumbers([offer(PLAIN), offer(ROUND), offer(RUN), offer(PAIRS), offer(SMS, sms="inbound+outbound")])
        self.browse_file = self.root / "browse.json"
        self.directory = self.root / "reservation"

    def call(self, function, **fields):
        args = SimpleNamespace(endpoint=ENDPOINT, connection_string_file=None, tenant_id=None, **fields)
        with redirect_stdout(io.StringIO()) as output:
            code = function(args, self.phone, ENDPOINT)
        return code, json.loads(output.getvalue())

    def browse(self):
        return self.call(acs.browse, country="GB", number_type="geographic", assignment="application",
                         calling="inbound_outbound", sms="none", prefix=[], show=10, output=self.browse_file)

    def reserve(self, numbers=(ROUND,), *, apply=True, directory=None):
        directory = directory or self.directory
        self.phone.journal = directory
        return self.call(acs.reserve, browse_file=self.browse_file, number=list(numbers), output_dir=directory, apply=apply)

    def purchase(self, numbers=None, cost=None, *, agree=False):
        self.phone.journal = self.directory
        return self.call(acs.purchase, reservation_dir=self.directory, confirm_number=numbers,
                         confirm_monthly_cost=cost, agree_to_not_resell=agree)

    def test_browse_sends_enums_ranks_memorable_numbers_and_saves_every_offer(self):
        code, result = self.browse()
        self.assertEqual(code, 0)
        [request] = self.phone.browse_calls
        self.assertIs(request["calling_capability"], Capability.INBOUND_OUTBOUND)
        self.assertIs(request["sms_capability"], Capability.NONE)
        self.assertIs(request["assignment_type"], Assignment.APPLICATION)
        self.assertIs(request["phone_number_type"], NumberType.GEOGRAPHIC)
        self.assertEqual((request["country_code"], request["phone_number_prefixes"]), ("GB", None))
        self.assertEqual([row["phone_number"] for row in result["candidates"]], [ROUND, RUN, PAIRS, PLAIN])
        self.assertEqual(result["candidates"][0]["monthly_cost"], "1.00 GBP")
        self.assertIn("0000 repeats", result["candidates"][0]["why"])
        self.assertEqual(result["dropped_not_matching"], 1)
        self.assertTrue(any("'person'" in note for note in result["notes"]))
        saved = json.loads(self.browse_file.read_text())
        self.assertEqual(len(saved["offers"]), 4)
        self.assertEqual(saved["request"]["wire"]["calling"], "inbound+outbound")
        if os.name != "nt":
            self.assertEqual(self.browse_file.stat().st_mode & 0o777, 0o600)

    def test_browse_without_matching_numbers_stops_for_an_alternative(self):
        self.phone.offers = [offer(SMS, sms="inbound+outbound"), offer(PLAIN, calling="inbound")]
        code, result = self.browse()
        self.assertEqual((code, result["status"], result["candidates"]), (1, "none_available", []))
        self.assertIn("Stop and agree an alternative with the owner", result["next"])

    def test_reserve_preview_sends_nothing(self):
        self.browse()
        code, result = self.reserve(apply=False)
        self.assertEqual((code, result["status"], self.phone.reserve_calls), (0, "planned", []))
        self.assertEqual(result["reserve"][0]["assignment_type"], "application")
        self.assertFalse(self.directory.exists())

    def test_reserve_journals_first_sends_enums_and_trusts_the_readback_label(self):
        self.browse()
        code, result = self.reserve()
        self.assertEqual((code, result["status"]), (0, "reserved"))
        [(reservation_id, [entry])] = self.phone.reserve_calls
        self.assertEqual(entry.id, ROUND.lstrip("+"))
        self.assertIs(entry.capabilities.calling, Capability.INBOUND_OUTBOUND)
        self.assertIs(entry.assignment_type, Assignment.APPLICATION)
        self.assertEqual(json.loads((self.directory / "reservation-intent.json").read_text())["reservation_id"], reservation_id)
        reservation = result["reservation"]
        self.assertEqual(reservation["total_monthly_cost"], "1.00 GBP")
        self.assertEqual(reservation["differences_from_offer"], [{
            "phone_number": ROUND, "field": "assignment", "offer": "person", "reservation": "application",
            "trusted": "reservation readback",
        }])
        self.assertIn("purchase", result["next"])
        code, rerun = self.reserve()
        self.assertEqual((code, rerun["status"], len(self.phone.reserve_calls)), (0, "reserved", 1))

    def test_reservation_readback_mismatches_fail(self):
        self.browse()
        for index, (override, message) in enumerate((
            ({"capabilities": SimpleNamespace(calling="inbound", sms="none")}, "calling is 'inbound'"),
            ({"capabilities": SimpleNamespace(calling="inbound+outbound", sms="inbound+outbound")}, "SMS is"),
            ({"country_code": "US"}, "country is 'US'"),
            ({"assignment_type": "person"}, "assignment is 'person'"),
            ({"phone_number_type": "tollFree"}, "number type is 'tollFree'"),
            ({"status": "error"}, "status is 'error'"),
            ({"cost": SimpleNamespace(amount=1.0, currency_code="GBP", billing_frequency="yearly")}, "no monthly cost"),
        )):
            with self.subTest(override=override):
                self.phone.overrides = {ROUND: override}
                code, result = self.reserve(directory=self.root / f"reservation-{index}")
                self.assertEqual((code, result["status"]), (1, "mismatch"))
                self.assertTrue(any(message in problem for problem in result["reservation"]["problems"]), result)

    def test_quota_error_explains_tenant_limits_and_is_never_retried(self):
        self.browse()
        self.phone.reserve_error = ServiceError(400, acs.QUOTA_ERROR)
        code, result = self.reserve()
        self.assertEqual((code, result["status"]), (1, "failed"))
        self.assertEqual(result["request_failure"], {"http_status": 400, "codes": [acs.QUOTA_ERROR]})
        for phrase in ("per Azure tenant", "release them", "Azure Support", "Service and subscription limits",
                       "service-limits", "acs-retirement-and-breaking-changes-guide"):
            self.assertIn(phrase, result["help"])
        self.assertTrue((self.directory / "reservation-error.json").is_file())
        self.reserve()
        self.assertEqual(len(self.phone.reserve_calls), 1)

    def test_quota_code_on_a_partially_reserved_number_gets_the_same_help(self):
        self.browse()
        self.phone.overrides = {ROUND: {"status": "error", "error": SimpleNamespace(code=acs.QUOTA_ERROR)}}
        code, result = self.reserve()
        self.assertEqual((code, result["help"]), (1, acs.QUOTA_HELP))

    def test_purchase_preview_prints_the_exact_approval(self):
        self.browse()
        self.reserve()
        code, result = self.purchase()
        self.assertEqual((code, result["status"], self.phone.purchase_calls), (0, "ready_to_purchase", []))
        self.assertEqual(result["approve_with"], f"--confirm-number {ROUND} --confirm-monthly-cost '1.00 GBP'")

    def test_purchase_refuses_anything_but_the_exact_numbers_and_cost(self):
        self.browse()
        self.reserve([ROUND, RUN])
        for numbers, cost, message in (
            ([ROUND], "2.00 GBP", "every reserved number"),
            ([ROUND, ROUND], "2.00 GBP", "every reserved number"),
            ([ROUND, PLAIN], "2.00 GBP", "every reserved number"),
            ([ROUND, RUN], "1.00 GBP", "must match the readback: 2.00 GBP"),
            ([ROUND, RUN], "2.00 USD", "must match the readback"),
            ([ROUND, RUN], None, "amount and currency"),
            (None, "2.00 GBP", "every reserved number"),
        ):
            with self.subTest(numbers=numbers, cost=cost), self.assertRaisesRegex(ValueError, message):
                self.purchase(numbers, cost)
        self.assertEqual(self.phone.purchase_calls, [])
        self.assertFalse((self.directory / "purchase-intent.json").exists())

    def test_do_not_resell_agreement_must_be_explicit_when_required(self):
        self.phone.offers = [offer(ROUND, resell=True)]
        self.browse()
        self.reserve()
        with self.assertRaisesRegex(ValueError, "--agree-to-not-resell"):
            self.purchase([ROUND], "1.00 GBP")
        self.assertEqual(self.phone.purchase_calls, [])
        code, result = self.purchase([ROUND], "1.00 GBP", agree=True)
        self.assertEqual((code, self.phone.purchase_calls[0][1]), (0, True))

    def test_purchase_buys_once_and_reads_back_every_number(self):
        self.browse()
        self.reserve()
        code, result = self.purchase([ROUND], "1.0 gbp")
        self.assertEqual((code, result["status"]), (0, "purchased"))
        self.assertEqual(len(self.phone.purchase_calls), 1)
        [bought] = result["purchased_numbers"]
        self.assertEqual((bought["phone_number"], bought["assignment"], bought["monthly_cost"]), (ROUND, "application", "1.00 GBP"))
        self.assertEqual(bought["capabilities"], {"calling": "inbound+outbound", "sms": "none"})
        code, rerun = self.purchase([ROUND], "1.00 GBP")
        self.assertEqual((code, rerun["status"], len(self.phone.purchase_calls)), (0, "purchased", 1))
        code, listing = self.call(acs.purchased)
        self.assertEqual([row["phone_number"] for row in listing["purchased_numbers"]], [ROUND])

    def test_failed_purchase_is_recorded_and_never_repeated(self):
        self.browse()
        self.reserve()
        self.phone.purchase_error = ServiceError(500, "PurchaseFailed")
        code, result = self.purchase([ROUND], "1.00 GBP")
        self.assertEqual((code, result["status"]), (1, "not_confirmed"))
        self.assertTrue((self.directory / "purchase-error.json").is_file())
        self.phone.purchase_error = None
        code, result = self.purchase([ROUND], "1.00 GBP")
        self.assertEqual((code, len(self.phone.purchase_calls)), (1, 1))
        self.assertIn("never buys twice", result["next"])

    def test_purchase_refuses_a_reservation_that_is_about_to_expire(self):
        self.phone.expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        self.browse()
        self.reserve()
        with self.assertRaisesRegex(ValueError, "too soon to buy safely"):
            self.purchase([ROUND], "1.00 GBP")
        self.assertEqual(self.phone.purchase_calls, [])


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        manager = private_test_directory()
        self.root = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)

    def secret_file(self, path, value, mode=0o600):
        path.write_text(f'ACS_CONNECTION_STRING="{value}"\n')
        path.chmod(mode)
        return path

    def test_connection_string_file_must_be_private_outside_the_checkout_and_well_formed(self):
        key = "synthetic-access-key-for-unit-tests-only"
        value = f"endpoint={ENDPOINT}/;accesskey={key}"
        self.assertEqual(acs.connection_string(self.secret_file(self.root / "acs.env", value)), (value, ENDPOINT))
        with self.assertRaisesRegex(ValueError, "outside the checkout"):
            acs.connection_string(self.secret_file(topics.ROOT / "acs.env", value))
        if os.name != "nt":
            with self.assertRaisesRegex(ValueError, "chmod 600"):
                acs.connection_string(self.secret_file(self.root / "shared.env", value, 0o644))
        with self.assertRaises(ValueError) as raised:
            acs.connection_string(self.secret_file(self.root / "bad.env", f"endpoint=https://example.com/;accesskey={key}"))
        self.assertNotIn(key, str(raised.exception))

    @unittest.skipUnless(importlib.util.find_spec("azure.core"), "azure-core is installed with the pinned SDK")
    def test_cli_credential_asks_az_login_for_an_acs_token_in_the_selected_tenant(self):
        reply = {"accessToken": "synthetic-token", "expires_on": 1700000000, "tenant": TENANT}
        with patch.object(acs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")) as run:
            token = acs.CliCredential(TENANT).get_token(acs.ACS_SCOPE)
        self.assertEqual((token.token, token.expires_on), ("synthetic-token", 1700000000))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--resource") + 1], "https://communication.azure.com/")
        self.assertEqual(command[command.index("--tenant") + 1], TENANT)
        reply["tenant"] = "33333333-3333-4333-8333-333333333333"
        with patch.object(acs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
            with self.assertRaisesRegex(ValueError, "another tenant"):
                acs.CliCredential(TENANT).get_token(acs.ACS_SCOPE)

    def test_failed_az_login_stops_before_any_client_call(self):
        args = SimpleNamespace(endpoint=ENDPOINT, connection_string_file=None, tenant_id=None)
        sdk = SimpleNamespace(PhoneNumbersClient=MagicMock())
        with patch.object(acs, "sdk", return_value=sdk), patch.object(
            acs.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["az"]),
        ), self.assertRaisesRegex(RuntimeError, "az login"):
            acs.client(args)
        sdk.PhoneNumbersClient.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("azure.communication.phonenumbers"), "pinned SDK not installed")
class InstalledSdkTests(unittest.TestCase):
    def test_reservation_body_from_the_public_exports_uses_enum_wire_values(self):
        from azure.communication.phonenumbers import PhoneNumbersReservation
        api = acs.sdk()
        enums = acs.enums_for(api, {"number_type": "geographic", "assignment": "application",
                                    "calling": "inbound_outbound", "sms": "none"})
        entry = acs.entry(api, enums, {"id": ROUND.lstrip("+"), "country_code": "GB"})
        self.assertEqual(PhoneNumbersReservation(phone_numbers={entry.id: entry}).serialize(), {"phoneNumbers": {
            ROUND.lstrip("+"): {
                "countryCode": "GB", "capabilities": {"calling": "inbound+outbound", "sms": "none"},
                "phoneNumberType": "geographic", "assignmentType": "application",
            },
        }})


if __name__ == "__main__":
    unittest.main()
