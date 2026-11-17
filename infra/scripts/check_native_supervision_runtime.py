"""Read-only supervision preflight inside the deployed voice container."""

import argparse
import errno
import json
import os
import pty
import re
import select
import subprocess
import time

from native_voice_setup import guid, private_path, read_private, write_private


def validated_env_hashes(value):
    if not isinstance(value, dict) or not 1 <= len(value) <= 64 or not all(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) and
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in value.items()
    ):
        raise ValueError("Expected 1-64 explicit environment names with lowercase SHA256 hashes.")
    return value


def program(expected_user, expected_env_hashes=None):
    expected_user = guid(expected_user)
    expected_env_hashes = {} if expected_env_hashes is None else validated_env_hashes(expected_env_hashes)
    return f"""
import asyncio
import hashlib
import json
import logging
import os
from backend.dataverse import DataverseClient
from backend.native_supervision import NativeCommandConsumer, SupervisionConfig
logging.getLogger("azure").setLevel(logging.WARNING)

async def preflight():
    expected_env_hashes = {expected_env_hashes!r}
    for name, expected in expected_env_hashes.items():
        value = os.environ.get(name)
        if value is None or hashlib.sha256(value.encode()).hexdigest() != expected:
            raise ValueError("Environment secret hash mismatch: " + name)
    settings = SupervisionConfig.from_env()
    if not settings.actors or not settings.controllers:
        raise ValueError("Configure reviewed actors/controller pins while keeping supervision disabled.")
    client = DataverseClient(os.environ["DYNAMICS_ORG_URL"], os.environ.get("AZURE_TENANT_ID"))
    try:
        who = await client.get("WhoAmI")
        if who.get("UserId") != {expected_user!r}:
            raise ValueError("The actual runtime identity differs from the approved application user.")
        actor = await client.get("systemusers(" + who["UserId"] + ")", params={{"$select": "applicationid,isdisabled"}})
        if actor.get("applicationid") != os.environ.get("AZURE_CLIENT_ID") or actor.get("isdisabled") is not False:
            raise ValueError("The deployed managed identity or application user does not match.")
        consumer = NativeCommandConsumer(settings, client, None, lambda: {{}})
        await consumer.verify()
        result = {{
            "runtime_identity_verified": True,
            "schema_and_controller_ready": consumer.ready,
            "controller_count": len(settings.controllers),
            "supervision_enabled": settings.enabled,
            "command_polling_started": False,
            "remote_writes": 0,
            "calls_placed": 0
        }}
        if expected_env_hashes:
            result.update(environment_secret_hashes_verified=True, environment_secret_count=len(expected_env_hashes))
        print("SUPERVISION_PREFLIGHT_RESULT=" + json.dumps(result), flush=True)
    finally:
        await client.close()

asyncio.run(preflight())
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True, type=guid)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--expected-runtime-user", required=True, type=guid)
    parser.add_argument("--receipt-file", required=True, type=private_path)
    parser.add_argument("--expected-env-hashes", type=private_path,
                        help="Optional owner-only JSON mapping explicit environment names to SHA256 hashes; never secret values.")
    args = parser.parse_args(argv)
    for value in (args.resource_group, args.app_name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}", value):
            raise ValueError("Use an explicit Azure resource name.")
    if args.receipt_file.exists():
        raise ValueError("Use a new private preflight receipt filename.")
    expected_env_hashes = (
        validated_env_hashes(json.loads(read_private(args.expected_env_hashes))) if args.expected_env_hashes else None
    )
    master, slave = pty.openpty()
    process = subprocess.Popen([
        "az", "containerapp", "exec", "--subscription", args.subscription_id,
        "--resource-group", args.resource_group, "--name", args.app_name,
        "--command", "python -i", "--only-show-errors",
    ], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    sent = False
    deadline = time.monotonic() + 180
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
                    raise ValueError("Runtime preflight output exceeded its bounded receipt size.")
                if not sent and b">>>" in output:
                    os.write(master, ("exec(" + repr(program(args.expected_runtime_user, expected_env_hashes)) + ")\nexit()\n").encode())
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
    text = output.decode(errors="replace").replace("\r", "")
    matches = re.findall(r"^SUPERVISION_PREFLIGHT_RESULT=(\{[^\n]+\})$", text, re.MULTILINE)
    receipt = {"status": "unconfirmed", "terminal": text}
    if len(matches) == 1:
        result = json.loads(matches[0])
        if result.get("runtime_identity_verified") is True and result.get("schema_and_controller_ready") is True and (
            expected_env_hashes is None or (
                result.get("environment_secret_hashes_verified") is True and
                result.get("environment_secret_count") == len(expected_env_hashes)
            )
        ):
            receipt.update(status="verified", result=result)
    write_private(args.receipt_file, json.dumps(receipt, indent=2) + "\n")
    if receipt["status"] != "verified":
        raise RuntimeError("Runtime preflight did not pass. Inspect the private receipt; do not enable supervision.")
    print(json.dumps({"status": "verified", **receipt["result"], "private_receipt": str(args.receipt_file)}))


if __name__ == "__main__":
    main()
