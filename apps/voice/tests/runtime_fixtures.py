"""Explicit test-only deployment factory; never a packaged customer configuration."""
from dataclasses import asdict

from runtime_config import DEPLOYMENT, parse_deployment


def enabled_deployment(*, language="ar", allowed_numbers=(), **features):
    value = asdict(DEPLOYMENT)
    value["organization"]["defaultLanguage"] = language
    value["features"] = {name: True for name in value["features"]}
    value["features"].update(features)
    value["business"] = {"caseCategories": ["general"], "locationRequired": True, "city": "Test city"}
    value["knowledge"]["allowedDomains"] = ["docs.example.test"]
    value["emailPolicy"] = {"allowedRecipients": [], "allowedDomains": ["example.com"]}
    value["phonePolicy"]["allowedOutboundNumbers"] = list(allowed_numbers)
    return parse_deployment(value)
