"""Connect the deployed voice webhook to IncomingCall events; does not buy or dial a number."""

import argparse
import json
import subprocess
from urllib.parse import urlsplit
from urllib.request import urlopen

from setup_knowledge import environment_value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default="")
    args = parser.parse_args()
    subscription = environment_value("AZURE_SUBSCRIPTION_ID", args.environment)
    group = environment_value("AZURE_RESOURCE_GROUP", args.environment)
    acs = environment_value("MOACC_ACS_NAME", args.environment)
    voice = environment_value("SERVICE_VOICE_URI", args.environment).rstrip("/")
    if urlsplit(voice).scheme != "https":
        raise ValueError("The deployed voice service must have an HTTPS endpoint.")
    with urlopen(voice + "/health", timeout=30) as response:
        health = json.load(response)
    if health.get("service") != "voice":
        raise RuntimeError("Deploy the real voice application before registering its webhook.")
    resource_id = (
        f"/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Communication/communicationServices/{acs}"
    )
    subprocess.run([
        "az", "eventgrid", "event-subscription", "create",
        "--name", "moacc-incoming-calls",
        "--source-resource-id", resource_id,
        "--endpoint-type", "webhook",
        "--endpoint", voice + "/acs/incoming-call",
        "--event-delivery-schema", "EventGridSchema",
        "--included-event-types", "Microsoft.Communication.IncomingCall",
        "--advanced-filter", "data.to.kind", "StringIn", "phoneNumber",
        "--subscription", subscription,
        "--output", "none",
    ], check=True)
    state = subprocess.check_output([
        "az", "eventgrid", "event-subscription", "show",
        "--name", "moacc-incoming-calls", "--source-resource-id", resource_id,
        "--subscription", subscription, "--query", "provisioningState", "--output", "tsv",
    ], text=True).strip()
    if state != "Succeeded":
        raise RuntimeError(f"Incoming-call subscription is not ready: {state}")
    print("Incoming-call routing configured. No phone number was purchased and no call was placed.")


if __name__ == "__main__":
    main()
