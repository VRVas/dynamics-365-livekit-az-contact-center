"""Browse, reserve and buy Azure Communication Services phone numbers, checking every step's readback.

Read-only unless you pass `reserve --apply` or the `purchase` approval flags. Uses the pinned
azure-communication-phonenumbers SDK from infra/scripts/requirements.txt.
"""

import argparse
import json
import re
import shlex
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import native_voice_setup as native
from contact_center_setup import emit, private_directory, private_json, utc
from native_voice_setup import guid, private_path, write_private


LEARN = {
    "service_limits": "https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits#acquire-phone-numbers",
    "acs_retirement": "https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide",
    "number_eligibility": "https://learn.microsoft.com/en-us/azure/communication-services/concepts/numbers/sub-eligibility-number-capability",
    "uk_numbers": "https://learn.microsoft.com/en-us/azure/communication-services/concepts/numbers/phone-number-management-for-united-kingdom",
    "browse_api": "https://learn.microsoft.com/en-us/rest/api/communication/phonenumbers/phone-numbers/browse-available-numbers?view=rest-communication-phonenumbers-2025-06-01",
    "reservation_api": "https://learn.microsoft.com/en-us/rest/api/communication/phonenumbers/phone-numbers/create-or-update-reservation?view=rest-communication-phonenumbers-2025-06-01",
    "purchase_api": "https://learn.microsoft.com/en-us/rest/api/communication/phonenumbers/phone-numbers/purchase-reservation?view=rest-communication-phonenumbers-2025-06-01",
}
QUOTA_ERROR = "MaxAllowedNumbersPerReservationExceeded"
ACS_SCOPE = "https://communication.azure.com//.default"
CAPABILITIES = {"none": "NONE", "inbound": "INBOUND", "outbound": "OUTBOUND", "inbound_outbound": "INBOUND_OUTBOUND"}
NUMBER_TYPES = {"geographic": "GEOGRAPHIC", "toll_free": "TOLL_FREE"}
ASSIGNMENTS = {"application": "APPLICATION", "person": "PERSON"}
E164 = re.compile(r"\+[1-9]\d{6,14}")
MAX_NUMBERS = 10
PURCHASE_WAIT_SECONDS = 600
EXPIRY_MARGIN = timedelta(minutes=2)
RECOMMENDATION = (
    "UK local (geographic) numbers that can make and receive calls are recommended from past deployments for "
    "flexibility and reliability. Local numbers are desirable but often can't be obtained easily."
)
QUOTA_HELP = (
    "ACS refused the numbers with MaxAllowedNumbersPerReservationExceeded. Number purchase limits apply per Azure "
    "tenant. First release numbers you no longer use: list them with the purchased command, then release them in the "
    "Azure portal (Communication Services resource > Phone numbers). Then ask Azure Support for a higher purchase "
    "limit: Help + support > Create a support request > Service and subscription limits (quotas). Limits: "
    + LEARN["service_limits"] + " Since the ACS retirement announcement, customers without an ACS resource created "
    "before 23 October 2026 can't acquire numbers at all: " + LEARN["acs_retirement"]
)
FAILURE_HELP = (
    "Nothing was resent. Show the owner the error and the readback before anyone tries again. If this tenant had no "
    "ACS resource created before 23 October 2026, number acquisition is blocked: " + LEARN["acs_retirement"]
)


def sdk():
    """Import the phone numbers SDK only when a command needs it."""
    from azure.communication.phonenumbers import (
        AvailablePhoneNumber, PhoneNumberAssignmentType, PhoneNumberCapabilities, PhoneNumberCapabilityType,
        PhoneNumbersClient, PhoneNumberType,
    )
    return SimpleNamespace(
        AvailablePhoneNumber=AvailablePhoneNumber, PhoneNumberAssignmentType=PhoneNumberAssignmentType,
        PhoneNumberCapabilities=PhoneNumberCapabilities, PhoneNumberCapabilityType=PhoneNumberCapabilityType,
        PhoneNumbersClient=PhoneNumbersClient, PhoneNumberType=PhoneNumberType,
    )


def acs_endpoint(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.communication\.azure\.com", value):
        raise ValueError("Use the ACS endpoint origin, for example https://<resource>.communication.azure.com, without a path.")
    return value


class CliCredential:
    """Entra token from the signed-in Azure CLI (az login), the same source azure-identity's AzureCliCredential uses."""

    def __init__(self, tenant_id=None):
        self.tenant_id = guid(tenant_id) if tenant_id else None

    def get_token(self, *scopes, **kwargs):
        from azure.core.credentials import AccessToken
        if len(scopes) != 1 or not scopes[0].endswith("/.default"):
            raise ValueError("Request exactly one '/.default' scope.")
        command = ["az", "account", "get-access-token", "--resource", scopes[0][: -len("/.default")], "-o", "json"]
        if self.tenant_id:
            command += ["--tenant", self.tenant_id]
        token = json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=45).stdout)
        if self.tenant_id and str(token.get("tenant", self.tenant_id)).lower() != self.tenant_id:
            raise ValueError("Azure CLI returned a token for another tenant.")
        expires = token.get("expires_on")
        if expires is None:
            expires = datetime.strptime(token["expiresOn"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
        return AccessToken(token["accessToken"], int(expires))


def connection_string(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(native.topics.ROOT):
        raise ValueError("Keep the connection-string file outside the checkout.")
    value = native.secret_value(path, "ACS_CONNECTION_STRING")
    parts = dict(part.partition("=")[::2] for part in value.strip(";").split(";"))
    parts = {key.strip().lower(): item for key, item in parts.items()}
    if set(parts) != {"endpoint", "accesskey"} or not parts["accesskey"]:
        raise ValueError("ACS_CONNECTION_STRING must look like endpoint=https://<resource>.communication.azure.com/;accesskey=<key>.")
    return value, acs_endpoint(parts["endpoint"].rstrip("/"))


def client(args):
    api = sdk()
    if args.connection_string_file:
        secret, endpoint = connection_string(args.connection_string_file)
        return api.PhoneNumbersClient.from_connection_string(secret), endpoint
    credential = CliCredential(args.tenant_id)
    try:
        # Sign in before any journaled write, so a CLI login problem never looks like an uncertain request.
        credential.get_token(ACS_SCOPE)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise RuntimeError("az login didn't return an ACS token. Run az login (add --tenant-id for another tenant), "
                           "or use --connection-string-file.") from None
    return api.PhoneNumbersClient(args.endpoint, credential), args.endpoint


def auth_args(args):
    if args.connection_string_file:
        return "--connection-string-file " + shlex.quote(str(args.connection_string_file))
    return f"--endpoint {args.endpoint}" + (f" --tenant-id {args.tenant_id}" if args.tenant_id else "")


def text(value):
    if isinstance(value, Enum):
        return value.value
    return value if value is None or isinstance(value, str) else str(value)


def number_record(number):
    capabilities, cost, error = (getattr(number, key, None) for key in ("capabilities", "cost", "error"))
    purchase_date = getattr(number, "purchase_date", None)
    return {
        "id": getattr(number, "id", None), "phone_number": getattr(number, "phone_number", None),
        "country_code": text(getattr(number, "country_code", None)),
        "phone_number_type": text(getattr(number, "phone_number_type", None)),
        "assignment_type": text(getattr(number, "assignment_type", None)),
        "capabilities": None if capabilities is None else {
            "calling": text(capabilities.calling), "sms": text(capabilities.sms),
        },
        "cost": None if cost is None else {
            "amount": cost.amount, "currency_code": text(cost.currency_code), "billing_frequency": text(cost.billing_frequency),
        },
        "status": text(getattr(number, "status", None)),
        "is_agreement_to_not_resell_required": getattr(number, "is_agreement_to_not_resell_required", None),
        "error": None if error is None else (getattr(error, "code", None) or "unknown"),
        **({"purchase_date": purchase_date.isoformat() if hasattr(purchase_date, "isoformat") else str(purchase_date)}
           if purchase_date is not None else {}),
    }


def amount(record):
    try:
        value = Decimal(str((record.get("cost") or {}).get("amount")))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def money(value, currency):
    if value.as_tuple().exponent > -2:
        value = value.quantize(Decimal("0.01"))
    return f"{value} {currency}"


def monthly_cost(record):
    cost, value = record.get("cost") or {}, amount(record)
    if value is None or not cost.get("currency_code"):
        return None
    label = money(value, cost["currency_code"])
    frequency = str(cost.get("billing_frequency") or "")
    return label if frequency.lower() == "monthly" else f"{label} per {frequency or 'unknown period'}"


def problems(record, request, *, status):
    wire, capabilities = request["wire"], record.get("capabilities") or {}
    checks = {
        "country": (record.get("country_code"), request["country"]),
        "number type": (record.get("phone_number_type"), wire["number_type"]),
        "assignment": (record.get("assignment_type"), wire["assignment"]),
        "calling": (capabilities.get("calling"), wire["calling"]),
        "SMS": (capabilities.get("sms"), wire["sms"]),
        "status": (record.get("status"), status),
    }
    found = [
        f"{label} is {actual!r}, expected {expected!r}" for label, (actual, expected) in checks.items()
        if expected is not None and str(actual or "").lower() != expected.lower()
    ]
    cost = record.get("cost") or {}
    if amount(record) is None or not cost.get("currency_code") or str(cost.get("billing_frequency") or "").lower() != "monthly":
        found.append("no monthly cost")
    if record.get("error"):
        found.append(f"service error {record['error']}")
    return found


def sequences(digits):
    found, index = [], 0
    while index < len(digits) - 2:
        step, end = int(digits[index + 1]) - int(digits[index]), index + 1
        if step in (1, -1):
            while end + 1 < len(digits) and int(digits[end + 1]) - int(digits[end]) == step:
                end += 1
        if end - index >= 2:
            found.append(digits[index:end + 1])
            index = end
        else:
            index += 1
    return found


def memorability(number):
    """Score the last seven digits: repeats, runs, repeated pairs/triples, few distinct digits, round endings."""
    digits, score, reasons = re.sub(r"\D", "", number or "")[-7:], 0, []
    for match in re.finditer(r"(\d)\1+", digits):
        score += 3 * (len(match[0]) - 1)
        reasons.append(f"{match[0]} repeats")
    for run in sequences(digits):
        score += 3 * (len(run) - 2)
        reasons.append(f"{run} runs")
    for size in (3, 2):
        blocks = [digits[start:start + size] for start in range(len(digits) - 2 * size + 1)
                  if len(set(digits[start:start + size])) > 1
                  and digits[start + size:start + 2 * size] == digits[start:start + size]]
        if blocks:
            score += 2 * size
            reasons.append(f"{blocks[0] * 2} pattern")
            break
    distinct = len(set(digits))
    if distinct <= 4:
        score += 5 - distinct
        reasons.append(f"only {distinct} different digits")
    if digits.endswith("00"):
        score += 2
        reasons.append("ends in 00")
    return score, reasons


def enums_for(api, request):
    try:
        return {
            "number_type": api.PhoneNumberType[NUMBER_TYPES[request["number_type"]]],
            "assignment": api.PhoneNumberAssignmentType[ASSIGNMENTS[request["assignment"]]],
            "calling": api.PhoneNumberCapabilityType[CAPABILITIES[request["calling"]]],
            "sms": api.PhoneNumberCapabilityType[CAPABILITIES[request["sms"]]],
        }
    except (KeyError, TypeError):
        raise ValueError("Unsupported number type, assignment or capability.") from None


def browse(args, phone, endpoint):
    api = sdk()
    country = args.country.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("--country must be a two-letter ISO code such as GB.")
    prefixes = [prefix.lstrip("+") for prefix in args.prefix]
    if any(not re.fullmatch(r"\d{1,12}", prefix) for prefix in prefixes):
        raise ValueError("--prefix takes digits including the country code, for example 44161.")
    if not 1 <= args.show <= 50:
        raise ValueError("--show must be between 1 and 50.")
    output = private_path(args.output)
    if output.exists():
        raise ValueError("Use a new --output file; browse results are never overwritten.")
    request = {"country": country, "number_type": args.number_type, "assignment": args.assignment,
               "calling": args.calling, "sms": args.sms, "prefixes": prefixes}
    enums = enums_for(api, request)
    request["wire"] = {key: text(value) for key, value in enums.items()}
    result = phone.browse_available_phone_numbers(
        country_code=country, phone_number_type=enums["number_type"], assignment_type=enums["assignment"],
        calling_capability=enums["calling"], sms_capability=enums["sms"], phone_number_prefixes=prefixes or None,
    )
    kept, dropped = [], 0
    for offer in map(number_record, getattr(result, "phone_numbers", None) or []):
        # Offers can carry a different assignment label than requested; the reservation readback decides.
        issues = [issue for issue in problems(offer, request, status=None) if not issue.startswith(("assignment", "no monthly"))]
        if issues or not E164.fullmatch(offer["phone_number"] or "") or not offer["id"] or (offer["status"] or "available").lower() != "available":
            dropped += 1
        else:
            kept.append(offer)
    kept.sort(key=lambda offer: (-memorability(offer["phone_number"])[0], offer["phone_number"]))
    write_private(output, json.dumps({
        "version": 1, "kind": "acs-number-browse", "created_at": utc(), "endpoint": endpoint,
        "request": request, "offers": kept,
    }, indent=2) + "\n")
    candidates = []
    for rank, offer in enumerate(kept[:args.show], 1):
        score, reasons = memorability(offer["phone_number"])
        candidates.append({
            "rank": rank, "phone_number": offer["phone_number"], "memorability": score, "why": reasons,
            "monthly_cost": monthly_cost(offer), "offer_assignment_label": offer["assignment_type"],
            "not_resell_agreement_required": offer["is_agreement_to_not_resell_required"],
        })
    notes = ["A number's country says nothing about where call data is stored."]
    if any((offer["assignment_type"] or "").lower() != request["wire"]["assignment"] for offer in kept):
        notes.append(f"Some offers are labelled with another assignment than {args.assignment!r}. Earlier UK offers came "
                     "back as 'person' but reserved as application: reserve, then trust the reservation readback.")
    document = {
        "status": "candidates" if kept else "none_available", "request": request, "offered": len(kept) + dropped,
        "dropped_not_matching": dropped, "candidates": candidates, "browse_file": str(output),
        "recommendation": RECOMMENDATION, "notes": notes,
    }
    if kept:
        document["next"] = (
            "Show the owner the candidates and their monthly cost. Preview the reservation: python "
            f"infra/scripts/acs_numbers.py reserve {auth_args(args)} --browse-file {shlex.quote(str(output))} "
            "--number <chosen number> --output-dir <new private dir>; then add --apply."
        )
    else:
        document["next"] = ("No matching number is available. Stop and agree an alternative with the owner (another "
                            "prefix, a toll-free number or another country), then browse again with the approved values.")
    emit(document)
    return 0 if kept else 1


def load_browse(path, api):
    document = private_json(Path(path).expanduser())
    request = document.get("request")
    if document.get("kind") != "acs-number-browse" or document.get("version") != 1 or not isinstance(request, dict):
        raise ValueError("Use a browse file written by acs_numbers.py browse.")
    if request.get("wire") != {key: text(value) for key, value in enums_for(api, request).items()}:
        raise ValueError("The browse file's request was changed; browse again.")
    offers = {offer.get("phone_number"): offer for offer in document.get("offers") or [] if isinstance(offer, dict)}
    return request, offers


def chosen(numbers, offers):
    if not numbers or len(numbers) > MAX_NUMBERS or len(set(numbers)) != len(numbers):
        raise ValueError(f"Choose 1-{MAX_NUMBERS} distinct numbers with --number.")
    for number in numbers:
        if not E164.fullmatch(number) or number not in offers:
            raise ValueError(f"{number} is not an offer in the browse file; pick a listed candidate.")
    return list(numbers)


def entry(api, enums, offer):
    number = api.AvailablePhoneNumber(
        country_code=offer["country_code"],
        capabilities=api.PhoneNumberCapabilities(calling=enums["calling"], sms=enums["sms"]),
        phone_number_type=enums["number_type"], assignment_type=enums["assignment"],
    )
    number.id = offer["id"]
    return number


def service_error(error):
    status, codes = getattr(error, "status_code", None), []
    for source in (getattr(error, "error", None), getattr(getattr(error, "model", None), "error", None)):
        code = getattr(source, "code", None)
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    if QUOTA_ERROR in str(error) and QUOTA_ERROR not in codes:
        codes.append(QUOTA_ERROR)
    return (status if type(status) is int else None), codes


def load_intent(path, endpoint):
    intent = private_json(path)
    try:
        UUID(str(intent.get("reservation_id")))
    except ValueError:
        intent = {}
    if intent.get("kind") != "acs-number-reservation" or intent.get("version") != 1:
        raise ValueError("Use a --reservation-dir written by acs_numbers.py reserve --apply.")
    if intent.get("endpoint") != endpoint:
        raise ValueError(f"Use the ACS resource that holds the reservation: {intent.get('endpoint')}.")
    return intent


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def summarize(reservation, intent):
    request, offers, wanted = intent["request"], intent["offers"], intent["numbers"]
    records = [number_record(number) for number in (getattr(reservation, "phone_numbers", None) or {}).values()]
    by_number = {record["phone_number"]: record for record in records}
    status, expires = text(getattr(reservation, "status", None)), getattr(reservation, "expires_at", None)
    found = [] if str(status).lower() == "active" else [f"reservation status is {status!r}, expected 'active'"]
    if set(by_number) != set(wanted):
        found.append("the reservation holds " + (", ".join(sorted(map(str, by_number))) or "no numbers")
                     + "; expected " + ", ".join(wanted))
    numbers, differences, total, currencies = [], [], Decimal(0), set()
    for number in wanted:
        record = by_number.get(number)
        if record is None:
            continue
        found.extend(f"{number}: {issue}" for issue in problems(record, request, status="reserved"))
        offer = offers.get(number) or {}
        for field, before, after in (
            ("assignment", offer.get("assignment_type"), record["assignment_type"]),
            ("monthly cost", monthly_cost(offer), monthly_cost(record)),
        ):
            if str(before or "").lower() != str(after or "").lower():
                differences.append({"phone_number": number, "field": field, "offer": before, "reservation": after,
                                    "trusted": "reservation readback"})
        if amount(record) is not None:
            total += amount(record)
            currencies.add((record.get("cost") or {}).get("currency_code"))
        numbers.append({
            "phone_number": number, "status": record["status"], "country": record["country_code"],
            "number_type": record["phone_number_type"], "assignment": record["assignment_type"],
            "capabilities": record["capabilities"], "monthly_cost": monthly_cost(record),
            "not_resell_agreement_required": record["is_agreement_to_not_resell_required"], "error": record["error"],
        })
    if len(currencies) > 1:
        found.append("the numbers are priced in different currencies")
    currency = next(iter(currencies)) if len(currencies) == 1 and len(numbers) == len(wanted) else None
    return {
        "reservation_id": intent["reservation_id"], "status": status, "expires_at": iso(expires), "numbers": numbers,
        "total_monthly_cost": money(total, currency) if currency else None, "total": [str(total), currency],
        "not_resell_agreement_required": any(row["not_resell_agreement_required"] for row in numbers),
        "differences_from_offer": differences, "problems": found,
        "service_codes": sorted({row["error"] for row in numbers if row["error"]}),
    }


def reservation_report(args, phone, endpoint, intent, output, *, failure=None):
    try:
        summary = summarize(phone.get_reservation(intent["reservation_id"]), intent)
    except Exception as error:  # The SDK's error types are imported lazily.
        status, codes = service_error(error)
        summary = {"reservation_id": intent["reservation_id"], "problems": ["the reservation readback failed"],
                   "readback_error": {"http_status": status, "codes": codes}, "service_codes": codes}
    codes = sorted(set((failure or {}).get("codes", [])) | set(summary.get("service_codes", [])))
    ok = failure is None and not summary["problems"]
    document = {
        "status": "reserved" if ok else ("failed" if failure else "mismatch"), "endpoint": endpoint, "reservation": summary,
        "request_failure": failure, "request_resent": False, "purchased": False,
    }
    if ok:
        document["next"] = (
            "Show the owner the numbers and the total monthly cost. Then preview the purchase: python "
            f"infra/scripts/acs_numbers.py purchase {auth_args(args)} --reservation-dir {shlex.quote(str(output))}. "
            f"The reservation expires at {summary['expires_at']}; each change extends it by 15 minutes, up to 2 hours."
        )
    else:
        document["help"] = QUOTA_HELP if QUOTA_ERROR in codes else FAILURE_HELP
        document["next"] = ("reserve never buys numbers; an unused reservation expires on its own. Rerunning reserve "
                            "with this --output-dir only reads back; a new --output-dir needs the owner's decision.")
    write_private(output / f"reservation-readback-{uuid4().hex}.json", json.dumps({**document, "at": utc()}, indent=2) + "\n")
    emit(document)
    return 0 if ok else 1


def reserve(args, phone, endpoint):
    api = sdk()
    request, offers = load_browse(args.browse_file, api)
    numbers = chosen(args.number, offers)
    output = private_path(args.output_dir)
    intent_path = output / "reservation-intent.json"
    if output.exists():
        private_directory(output)
        if intent_path.exists() or intent_path.is_symlink():
            intent = load_intent(intent_path, endpoint)
            if sorted(intent["numbers"]) != sorted(numbers):
                raise ValueError("This --output-dir already journals a reservation for other numbers; nothing was sent.")
            return reservation_report(args, phone, endpoint, intent, output)
    planned = [{key: offers[number][key] for key in ("phone_number", "country_code", "phone_number_type", "capabilities")}
               | {"assignment_type": request["wire"]["assignment"], "offer_monthly_cost": monthly_cost(offers[number])}
               for number in numbers]
    if not args.apply:
        emit({"status": "planned", "endpoint": endpoint, "reserve": planned, "remote_changes": False,
              "next": "Rerun with --apply to reserve. A reservation costs nothing and holds the numbers for 15 minutes "
                      "after the last change, up to 2 hours."})
        return 0
    if not output.exists():
        output.mkdir(mode=0o700)
    intent = {
        "version": 1, "kind": "acs-number-reservation", "endpoint": endpoint, "reservation_id": str(uuid4()),
        "numbers": numbers, "request": request, "offers": {number: offers[number] for number in numbers},
        "created_at": utc(),
    }
    write_private(intent_path, json.dumps(intent, indent=2) + "\n")
    enums = enums_for(api, request)
    try:
        phone.create_or_update_reservation(
            reservation_id=intent["reservation_id"], numbers_to_add=[entry(api, enums, offers[number]) for number in numbers],
        )
    except Exception as error:  # Every failure is journaled and read back; the request is never repeated.
        status, codes = service_error(error)
        failure = {"http_status": status, "codes": codes}
        write_private(output / "reservation-error.json", json.dumps({**failure, "at": utc()}) + "\n")
        return reservation_report(args, phone, endpoint, intent, output, failure=failure)
    return reservation_report(args, phone, endpoint, intent, output)


def parse_cost(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s+([A-Za-z]{3})\s*", value or "")
    if not match:
        raise ValueError("--confirm-monthly-cost takes an amount and currency, for example '2.00 GBP'.")
    return Decimal(match[1]), match[2].upper()


def purchase_report(phone, endpoint, intent, journal, output, *, failure=None, finished=True):
    purchased, issues, codes = [], [], list((failure or {}).get("codes", []))
    total, currencies = Decimal(0), set()
    for number in intent["numbers"]:
        try:
            record = number_record(phone.get_purchased_phone_number(number))
        except Exception as error:  # The SDK's error types are imported lazily.
            status, error_codes = service_error(error)
            issues.append(f"{number} is not readable as a purchased number (HTTP {status})")
            codes.extend(error_codes)
            continue
        issues.extend(f"{number}: {issue}" for issue in problems(record, intent["request"], status=None))
        if amount(record) is not None:
            total += amount(record)
            currencies.add((record.get("cost") or {}).get("currency_code"))
        purchased.append({
            "phone_number": number, "country": record["country_code"], "number_type": record["phone_number_type"],
            "assignment": record["assignment_type"], "capabilities": record["capabilities"],
            "monthly_cost": monthly_cost(record), "purchase_date": record.get("purchase_date"),
        })
    approved = parse_cost(journal["approved_monthly_cost"])
    if len(purchased) == len(intent["numbers"]) and (total, next(iter(currencies), None)) != approved:
        issues.append(f"purchased monthly cost {money(total, next(iter(currencies), '?'))} differs from the approved "
                      f"{journal['approved_monthly_cost']}")
    ok = failure is None and finished and not issues
    document = {
        "status": "purchased" if ok else "not_confirmed", "endpoint": endpoint, "purchased_numbers": purchased,
        "approved_monthly_cost": journal["approved_monthly_cost"], "problems": issues, "request_failure": failure,
        "purchase_finished": finished, "request_resent": False,
    }
    if ok:
        document["next"] = "Record the numbers in the private deployment notes and use them where the guide says."
    else:
        document["help"] = QUOTA_HELP if QUOTA_ERROR in codes else FAILURE_HELP
        document["next"] = ("Rerun purchase with the same --reservation-dir to read back again; it never buys twice. If "
                            "numbers stay missing, ask the owner to check the ACS resource's Phone numbers page.")
    write_private(output / f"purchase-readback-{uuid4().hex}.json", json.dumps({**document, "at": utc()}, indent=2) + "\n")
    emit(document)
    return 0 if ok else 1


def purchase(args, phone, endpoint):
    output = private_path(args.reservation_dir)
    if not (output / "reservation-intent.json").exists():
        raise ValueError("Pass the --output-dir you used with reserve --apply as --reservation-dir.")
    intent = load_intent(private_directory(output) / "reservation-intent.json", endpoint)
    journal_path = output / "purchase-intent.json"
    if journal_path.exists() or journal_path.is_symlink():
        return purchase_report(phone, endpoint, intent, private_json(journal_path), output)
    summary = summarize(phone.get_reservation(intent["reservation_id"]), intent)
    if summary["problems"]:
        emit({"status": "not_purchasable", "endpoint": endpoint, "reservation": summary, "purchased": False,
              "next": "Nothing was bought. Reserve again into a new --output-dir after the owner agrees."})
        return 1
    if not args.confirm_number and not args.confirm_monthly_cost:
        flags = " ".join(f"--confirm-number {number}" for number in intent["numbers"])
        flags += f" --confirm-monthly-cost {shlex.quote(summary['total_monthly_cost'])}"
        if summary["not_resell_agreement_required"]:
            flags += " --agree-to-not-resell"
        emit({"status": "ready_to_purchase", "endpoint": endpoint, "reservation": summary, "purchased": False,
              "approve_with": flags,
              "next": "Get the owner's approval for these numbers and this monthly cost, then rerun with the flags above."})
        return 0
    numbers = args.confirm_number or []
    if len(numbers) != len(intent["numbers"]) or set(numbers) != set(intent["numbers"]):
        raise ValueError("Repeat every reserved number exactly once with --confirm-number: "
                         + ", ".join(intent["numbers"]) + ". Nothing was bought.")
    total, currency = summary["total"]
    if parse_cost(args.confirm_monthly_cost) != (Decimal(total), currency):
        raise ValueError(f"--confirm-monthly-cost must match the readback: {summary['total_monthly_cost']}. Nothing was bought.")
    if summary["not_resell_agreement_required"] and not args.agree_to_not_resell:
        raise ValueError("These numbers need the agreement not to resell them. If the owner agrees, add "
                         "--agree-to-not-resell. Nothing was bought.")
    expires = datetime.fromisoformat(str(summary["expires_at"]).replace("Z", "+00:00")) if summary["expires_at"] else None
    if expires is None or expires.tzinfo is None or expires - datetime.now(timezone.utc) < EXPIRY_MARGIN:
        raise ValueError(f"The reservation expires at {summary['expires_at']}, too soon to buy safely. Reserve again "
                         "into a new --output-dir. Nothing was bought.")
    journal = {
        "version": 1, "kind": "acs-number-purchase", "reservation_id": intent["reservation_id"],
        "numbers": intent["numbers"], "approved_monthly_cost": summary["total_monthly_cost"],
        "agree_to_not_resell": bool(args.agree_to_not_resell), "created_at": utc(),
    }
    write_private(journal_path, json.dumps(journal, indent=2) + "\n")
    failure, finished = None, True
    try:
        poller = phone.begin_purchase_reservation(intent["reservation_id"], agree_to_not_resell=bool(args.agree_to_not_resell))
        poller.wait(timeout=PURCHASE_WAIT_SECONDS)
        finished = poller.done()
    except Exception as error:  # Every failure is journaled and read back; the purchase is never repeated.
        status, codes = service_error(error)
        failure = {"http_status": status, "codes": codes}
        write_private(output / "purchase-error.json", json.dumps({**failure, "at": utc()}) + "\n")
    return purchase_report(phone, endpoint, intent, journal, output, failure=failure, finished=finished)


def purchased(args, phone, endpoint):
    rows = []
    for number in phone.list_purchased_phone_numbers():
        if len(rows) >= 500:
            raise ValueError("More than 500 purchased numbers; review them in the Azure portal instead.")
        record = number_record(number)
        rows.append({key: record.get(key) for key in (
            "phone_number", "country_code", "phone_number_type", "assignment_type", "capabilities", "purchase_date",
        )} | {"monthly_cost": monthly_cost(record)})
    emit({"endpoint": endpoint, "count": len(rows), "purchased_numbers": rows,
          "note": "Purchase limits apply per Azure tenant. Release numbers you no longer use before you buy more."})
    return 0


HELP = {
    "browse": f"""List available numbers, most memorable first, with each offer's monthly cost. Read-only.

Defaults: country GB, geographic (local) numbers, application assignment, calling in and out
(PhoneNumberCapabilityType.INBOUND_OUTBOUND, sent as the SDK enum; the docstring literal
"inbound_outbound" returned HTTP 400), no SMS. Offers for another country or type, with other
capabilities, or not available are dropped.
{textwrap.fill(RECOMMENDATION, 104)}

Ranking looks at the last seven digits: repeated digits, runs such as 1234, repeated pairs or triples,
few distinct digits and round endings. All kept offers go to the private --output file that reserve reads.
UK offers requested for application assignment came back labelled 'person'; the reservation readback
decides. Number searches are rate-limited per Azure tenant, so reuse the saved file instead of browsing
in a loop. No candidates exits 1: stop and agree an alternative with the owner.

Learn: {LEARN['browse_api']}
       {LEARN['uk_numbers']}""",
    "reserve": f"""Reserve chosen numbers from a browse file and check the readback.

Read-only preview by default. --apply journals the reservation ID in --output-dir, sends one
create-or-update reservation and reads it back. The readback must show every number reserved with the
requested country, number type, assignment, calling and SMS capabilities, and a monthly cost.
Differences from the offer (for example a 'person' label that reserved as application) are reported;
the readback is trusted. A rerun with the same --output-dir only reads back. A reservation costs
nothing and expires 15 minutes after the last change, up to 2 hours.

MaxAllowedNumbersPerReservationExceeded: number purchase limits apply per Azure tenant. Release
unused numbers, then ask Azure Support for a higher limit (Service and subscription limits (quotas)).
Customers without an ACS resource created before 23 October 2026 can't acquire numbers at all.

Learn: {LEARN['reservation_api']}
       {LEARN['service_limits']}
       {LEARN['acs_retirement']}""",
    "purchase": f"""Buy a checked reservation; this costs money every month.

Without approval flags it reads the reservation back and prints the exact flags to approve. It buys only
when --confirm-number repeats every reserved number and --confirm-monthly-cost repeats the readback's
total, for example '2.00 GBP'. Numbers that need the do-not-resell agreement also need
--agree-to-not-resell. It journals the purchase first, buys once, then reads back each purchased number
and checks country, type, assignment, capabilities and cost. A rerun only reads back; it never buys twice.

Learn: {LEARN['purchase_api']}
       {LEARN['number_eligibility']}""",
    "purchased": """List the numbers this ACS resource already owns, with monthly cost. Read-only.

Purchase limits apply per Azure tenant: release numbers you no longer use before buying more.""",
}


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = result.add_subparsers(dest="command", required=True)

    def command(name, summary):
        item = commands.add_parser(name, help=summary, description=HELP[name],
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
        auth = item.add_mutually_exclusive_group(required=True)
        auth.add_argument("--endpoint", type=acs_endpoint,
                          help="ACS endpoint, for example https://<resource>.communication.azure.com. Signs in with your "
                               "az login (Entra); preferred.")
        auth.add_argument("--connection-string-file", type=Path,
                          help="Owner-only (chmod 600) file outside the checkout with ACS_CONNECTION_STRING=...; never printed.")
        item.add_argument("--tenant-id", type=guid, help="Tenant for the az login token (with --endpoint).")
        return item

    found = command("browse", "List available numbers, most memorable first, with monthly cost.")
    found.add_argument("--country", default="GB", help="ISO country code (default GB).")
    found.add_argument("--number-type", choices=sorted(NUMBER_TYPES), default="geographic", help="Default geographic (local).")
    found.add_argument("--assignment", choices=sorted(ASSIGNMENTS), default="application", help="Default application.")
    found.add_argument("--calling", choices=sorted(CAPABILITIES), default="inbound_outbound", help="Default inbound_outbound.")
    found.add_argument("--sms", choices=sorted(CAPABILITIES), default="none", help="Default none.")
    found.add_argument("--prefix", action="append", default=[],
                       help="Digits including the country code, for example 44161. Repeatable.")
    found.add_argument("--show", type=int, default=10, help="Candidates to print (default 10); all go to --output.")
    found.add_argument("--output", required=True, type=Path, help="New private file outside the checkout for reserve.")
    hold = command("reserve", "Preview, then reserve chosen numbers and check the readback.")
    hold.add_argument("--browse-file", required=True, type=Path, help="File written by browse.")
    hold.add_argument("--number", action="append", required=True, help="E.164 number from the browse file. Repeatable.")
    hold.add_argument("--output-dir", required=True, type=Path, help="Private journal directory; purchase reads it.")
    hold.add_argument("--apply", action="store_true", help="Send the reservation (free; expires on its own).")
    buy = command("purchase", "Buy a checked reservation after explicit approval.")
    buy.add_argument("--reservation-dir", required=True, type=Path, help="The --output-dir used with reserve.")
    buy.add_argument("--confirm-number", action="append", help="Repeat each reserved number to approve the purchase.")
    buy.add_argument("--confirm-monthly-cost", help="Repeat the readback's total monthly cost, for example '2.00 GBP'.")
    buy.add_argument("--agree-to-not-resell", action="store_true",
                     help="The owner agrees not to resell; required when the readback says so.")
    command("purchased", "List numbers this ACS resource already owns.")
    return result


COMMANDS = {"browse": browse, "reserve": reserve, "purchase": purchase, "purchased": purchased}


def main(argv=None):
    args = parser().parse_args(argv)
    phone, endpoint = client(args)
    return COMMANDS[args.command](args, phone, endpoint)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportError:
        print(json.dumps({"status": "failed", "error": "Install the pinned SDK: python -m pip install -r infra/scripts/requirements.txt"}))
        raise SystemExit(1)
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        raise SystemExit(1)
    except Exception as error:  # SDK and CLI errors: print only the type, HTTP status and service codes.
        status, codes = service_error(error)
        print(json.dumps({"status": "failed", "error": type(error).__name__, "http_status": status, "codes": codes}))
        raise SystemExit(1)
