"""Configurable service coordinator for the API or Foundry hosted runtime."""
from __future__ import annotations

import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from dotenv import load_dotenv

from email_tool import send_customer_email
from case_tools import (
    add_report_followup, create_case, get_case, get_customer_context,
    list_customer_cases,
    resolve_service_location, search_knowledge, talk_to_human,
)
from prompts import ORGANIZATION_RULES, build_rules
from runtime_config import DEPLOYMENT, Deployment

load_dotenv(override=False)
COORDINATOR_INSTRUCTIONS = ORGANIZATION_RULES


def _coordinator_credential():
    client_id = os.environ.get("AZURE_CLIENT_ID")
    return ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()


def build_coordinator(client: FoundryChatClient | None = None, deployment: Deployment | None = None) -> Agent:
    deployment = deployment or DEPLOYMENT
    client = client or FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.environ.get("FOUNDRY_MODEL", "gpt-5.4-mini"),
        credential=_coordinator_credential(),
    )
    options = {"store": False, "reasoning": {"effort": "low"}}
    rules = build_rules(deployment)
    tools = []
    if deployment.features.knowledge:
        guidance = Agent(
            client=client, name="service_guidance", instructions=rules + "\nYou provide complex service guidance.",
            tools=[search_knowledge], default_options=options,
        )
        tools.extend([
            search_knowledge,
            guidance.as_tool(name="service_guidance", description="Complex research beyond a routine answer already covered by approved knowledge.", arg_description="The general service question, without private identifiers, and chosen response language."),
        ])
    if deployment.features.crm:
        tools.extend([get_customer_context, list_customer_cases, resolve_service_location,
                      create_case, get_case, add_report_followup])
    if deployment.features.email:
        email_tools = [send_customer_email]
        if deployment.features.crm:
            email_tools.append(get_customer_context)
        if deployment.features.knowledge:
            email_tools.append(search_knowledge)
        email = Agent(
            client=client, name="correspondence", instructions=rules + (
                "\nYou are the correspondence specialist. Send only the confirmed requested email, "
                "using current evidence without redundant lookups. Never retry an unconfirmed send automatically."
            ), tools=email_tools, default_options=options,
        )
        tools.append(email.as_tool(name="email_specialist", description="Send requested correspondence to a deployment-approved recipient.", arg_description="Confirmed recipient, information to email, and chosen response language."))
    if deployment.features.agentConsole:
        tools.append(talk_to_human)
    return Agent(
        client=client, name="service_assistant", instructions=rules,
        tools=tools, default_options=options,
    )


def main():
    ResponsesHostServer(build_coordinator()).run()


if __name__ == "__main__":
    main()
