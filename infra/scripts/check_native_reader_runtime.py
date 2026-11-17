"""Prove the voice container's own Dataverse identity with read-only calls made inside the deployed container."""

import argparse
import errno
import json
import os
import pty
import re
import select
import subprocess
import time

from native_voice_setup import guid, private_path, write_private


MARKER = "NATIVE_READER_RUNTIME_RESULT="
# Native phone tables covered by the reader role (prvReadmsdyn_ocphonenumber, prvReadmsdyn_ocphonecallengagementctx).
NATIVE_TABLES = ("msdyn_ocphonenumbers", "msdyn_ocphonecallengagementctxes")

# Runs inside the container. It uses the same identity as the voice app's DataverseClient:
# ManagedIdentityCredential with the container's AZURE_CLIENT_ID (apps/voice/src/app/backend/azure.py).
# It only reads, never prints the token and reports observations; the host decides pass or fail.
PROGRAM = r'''
import base64
import json
import os
import re
import urllib.error
import urllib.request
from azure.identity import ManagedIdentityCredential


def native_reader_probe():
    marker = "NATIVE_READER_RUNTIME_RESULT="
    tables = (("msdyn_ocphonenumbers", "msdyn_ocphonenumberid"),
              ("msdyn_ocphonecallengagementctxes", "msdyn_ocphonecallengagementctxid"))
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    org = os.environ.get("DYNAMICS_ORG_URL", "").strip().rstrip("/")
    client = os.environ.get("AZURE_CLIENT_ID", "").strip().lower()
    result = {"client_id": client, "tenant_id": os.environ.get("AZURE_TENANT_ID", "").strip().lower(),
              "remote_writes": 0, "calls_placed": 0}
    if not re.fullmatch(r"https://[a-z0-9-]+\.crm\d*\.dynamics\.com", org) or not re.fullmatch(uuid, client):
        result["error"] = "DYNAMICS_ORG_URL or AZURE_CLIENT_ID is not configured in this container."
        print(marker + json.dumps(result), flush=True)
        return
    credential = ManagedIdentityCredential(client_id=client)
    try:
        token = credential.get_token(org + "/.default").token
    finally:
        credential.close()
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        claims = {}
    result["token"] = {key: str(claims.get(key) or "").lower() for key in ("appid", "azp", "oid", "tid")}

    def read(path):
        request = urllib.request.Request(org + "/api/data/v9.2/" + path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "OData-Version": "4.0", "OData-MaxVersion": "4.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read(1048576) or b"{}"), {}
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(16384)
            finally:
                error.close()
            try:
                detail = json.loads(raw).get("error") or {}
            except (ValueError, AttributeError):
                detail = {}
            return error.code, {}, {"code": str(detail.get("code") or "")[:32], "message": str(detail.get("message") or "")[:2000]}

    status, body, _ = read("WhoAmI")
    user = str(body.get("UserId") or "").lower()
    result["who_am_i"] = {"status": status, "user_id": user, "business_unit_id": str(body.get("BusinessUnitId") or "").lower()}
    if status == 200 and re.fullmatch(uuid, user):
        status, body, _ = read("systemusers(" + user + ")?$select=applicationid,azureactivedirectoryobjectid,isdisabled")
        result["application_user"] = {
            "status": status, "application_id": str(body.get("applicationid") or "").lower(),
            "object_id": str(body.get("azureactivedirectoryobjectid") or "").lower(), "disabled": body.get("isdisabled"),
        }
    result["native_reads"] = {}
    for table, key in tables:
        status, body, error = read(table + "?$select=" + key + "&$top=1")
        rows = body.get("value")
        result["native_reads"][table] = {"status": status, "rows": len(rows) if isinstance(rows, list) else None,
                                         "error_code": error.get("code")}
    status, body, error = read("roles?$select=roleid&$top=1")
    message = error.get("message", "")
    principal = re.search(r"Principal user \(Id=(" + uuid + r")", message, re.IGNORECASE)
    caller = re.search(r"context\.Caller=(" + uuid + r")", message, re.IGNORECASE)
    result["role_read"] = {
        "status": status, "error_code": error.get("code"), "missing_prvReadRole": "prvReadRole" in message,
        "principal_user_id": principal.group(1).lower() if principal else None,
        "caller_user_id": caller.group(1).lower() if caller else None,
    }
    print(marker + json.dumps(result), flush=True)


native_reader_probe()
'''


def evaluate(result, expected_user, expected_business_unit):
    if not isinstance(result, dict):
        return ["The probe returned no structured result."]
    if result.get("error"):
        return [str(result["error"])[:300]]
    failures = []
    client, token = result.get("client_id"), result.get("token") or {}
    if not client or (token.get("appid") or token.get("azp")) != client:
        failures.append("The Dataverse token wasn't issued to the container's managed identity (AZURE_CLIENT_ID).")
    if result.get("tenant_id") and token.get("tid") != result["tenant_id"]:
        failures.append("The token tenant differs from the container's AZURE_TENANT_ID.")
    who = result.get("who_am_i") or {}
    if who.get("status") != 200:
        failures.append(f"WhoAmI returned HTTP {who.get('status')}.")
    else:
        if who.get("user_id") != expected_user:
            failures.append("WhoAmI returned another user than the expected native reader application user.")
        if who.get("business_unit_id") != expected_business_unit:
            failures.append("WhoAmI returned another business unit than expected.")
    user = result.get("application_user") or {}
    if user.get("status") != 200 or user.get("application_id") != client or user.get("disabled") is not False:
        failures.append("The application user is missing, disabled or mapped to another application ID than AZURE_CLIENT_ID.")
    elif user.get("object_id") and token.get("oid") and user["object_id"] != token["oid"]:
        failures.append("The application user's Entra object ID differs from the managed identity in the token.")
    reads = result.get("native_reads") or {}
    for table in NATIVE_TABLES:
        read = reads.get(table) or {}
        if read.get("status") != 200 or not isinstance(read.get("rows"), int):
            failures.append(f"Reading {table} returned HTTP {read.get('status')}; check the reader role's native read privileges.")
    role = result.get("role_read") or {}
    if role.get("status") != 403 or role.get("error_code") != "0x80040220" or role.get("missing_prvReadRole") is not True:
        failures.append(
            f"The security-role read returned HTTP {role.get('status')} instead of a 403 prvReadRole denial; "
            "the identity can read more than the reader role allows, or the check didn't run as designed."
        )
    elif any(role.get(key) not in (None, expected_user) for key in ("principal_user_id", "caller_user_id")):
        failures.append("The role-read denial names another user than the expected application user.")
    return failures


def run_in_container(subscription, group, app, container, program, timeout=180):
    """Type the program into `python -i` inside the latest revision; az containerapp exec needs a terminal."""
    master, slave = pty.openpty()
    process = subprocess.Popen([
        "az", "containerapp", "exec", "--subscription", subscription, "--resource-group", group, "--name", app,
        "--container", container, "--command", "python -i", "--only-show-errors",
    ], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output, sent, deadline = bytearray(), False, time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 128 * 1024:
                    raise ValueError("The probe output exceeded its bounded receipt size.")
                if not sent and b">>>" in output:
                    os.write(master, ("exec(" + repr(program) + ")\nexit()\n").encode())
                    sent = True
            if process.poll() is not None:
                break
    finally:
        os.close(master)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return output.decode(errors="replace").replace("\r", "")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--subscription-id", required=True, type=guid)
    result.add_argument("--resource-group", required=True)
    result.add_argument("--app-name", default="ca-voice")
    result.add_argument("--container-name", default="voice")
    result.add_argument("--expected-runtime-user", required=True, type=guid, help="Dataverse systemuserid of the native reader application user.")
    result.add_argument("--expected-business-unit", required=True, type=guid)
    result.add_argument("--receipt-file", required=True, type=private_path, help="New owner-only receipt path outside the checkout.")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    for value, pattern in ((args.resource_group, r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}"),
                           (args.app_name, r"[a-z][a-z0-9-]{0,30}[a-z0-9]"),
                           (args.container_name, r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")):
        if not re.fullmatch(pattern, value):
            raise ValueError("Use explicit Azure resource, app and container names.")
    if args.receipt_file.exists() or args.receipt_file.is_symlink():
        raise ValueError("Use a new private receipt filename.")
    text = run_in_container(args.subscription_id, args.resource_group, args.app_name, args.container_name, PROGRAM)
    matches = re.findall(r"^" + re.escape(MARKER) + r"(\{[^\n]+\})$", text, re.MULTILINE)
    receipt = {
        "status": "unconfirmed", "expected_runtime_user": args.expected_runtime_user,
        "expected_business_unit": args.expected_business_unit, "terminal": text[-65536:],
        "failures": ["The probe printed no single result line; inspect the terminal transcript."],
    }
    if len(matches) == 1:
        try:
            result = json.loads(matches[0])
        except ValueError:
            result = None
        failures = evaluate(result, args.expected_runtime_user, args.expected_business_unit)
        receipt.update(status="failed" if failures else "verified", result=result, failures=failures)
    write_private(args.receipt_file, json.dumps(receipt, indent=2) + "\n")
    if receipt["status"] != "verified":
        raise RuntimeError("The native reader runtime check didn't pass: " + " ".join(receipt["failures"])
                           + f" Inspect {args.receipt_file}. Nothing was changed.")
    result = receipt["result"]
    print(json.dumps({
        "status": "verified", "runtime_user_id": result["who_am_i"]["user_id"],
        "business_unit_id": result["who_am_i"]["business_unit_id"], "managed_identity_client_id": result["client_id"],
        "native_reads": {table: result["native_reads"][table]["status"] for table in NATIVE_TABLES},
        "role_read": "denied (403 prvReadRole)", "remote_writes": 0, "calls_placed": 0,
        "private_receipt": str(args.receipt_file),
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__}))
        raise SystemExit(1)
