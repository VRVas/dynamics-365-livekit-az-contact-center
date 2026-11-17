"""Strict, service-local deployment configuration; no environment or customer defaults."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE = re.compile(r"\+[1-9][0-9]{6,14}")


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Organization:
    name: str
    assistantName: str
    defaultLanguage: str
    timeZone: str
    greeting: dict[str, str]


@dataclass(frozen=True)
class Features:
    crm: bool
    knowledge: bool
    email: bool
    agentAssist: bool
    applicationTelephony: bool
    browserVoice: bool
    nativeVoice: bool
    agentConsole: bool


@dataclass(frozen=True)
class Business:
    caseCategories: tuple[str, ...]
    locationRequired: bool
    city: str


@dataclass(frozen=True)
class Knowledge:
    allowedDomains: tuple[str, ...]


@dataclass(frozen=True)
class EmailPolicy:
    allowedRecipients: tuple[str, ...]
    allowedDomains: tuple[str, ...]


@dataclass(frozen=True)
class PhonePolicy:
    allowedOutboundNumbers: tuple[str, ...]


@dataclass(frozen=True)
class Deployment:
    schemaVersion: int
    organization: Organization
    features: Features
    business: Business
    knowledge: Knowledge
    emailPolicy: EmailPolicy
    phonePolicy: PhonePolicy

    def phone_allowed(self, number: str) -> bool:
        return bool(self.features.applicationTelephony and isinstance(number, str)
                    and PHONE.fullmatch(number) and number in self.phonePolicy.allowedOutboundNumbers)

    def email_allowed(self, address: str, *, synthetic: bool = False) -> bool:
        normalized = address.strip().lower()
        return bool(self.features.email and EMAIL.fullmatch(normalized) and (
            normalized in self.emailPolicy.allowedRecipients
            or (not synthetic and normalized.rsplit("@", 1)[1] in self.emailPolicy.allowedDomains)
        ))


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str
    display_name_ar: str
    phone: str
    email: str
    context: str


def _object(value, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ConfigurationError(f"{label} must contain exactly: {', '.join(sorted(expected))}.")
    return value


def _text(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ConfigurationError(f"{label} must be a {'possibly empty ' if empty else 'nonempty '}string.")
    return value.strip()


def _strings(value, label: str, pattern=None) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{label} must be an array.")
    result = tuple(_text(item, label) for item in value)
    if len(set(result)) != len(result) or (pattern and any(not pattern.fullmatch(item) for item in result)):
        raise ConfigurationError(f"{label} contains duplicate or invalid entries.")
    return result


def parse_deployment(value) -> Deployment:
    root = _object(value, {field.name for field in fields(Deployment)}, "deployment.json")
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != 1:
        raise ConfigurationError("deployment.json schemaVersion must be 1.")
    org = _object(root["organization"], {field.name for field in fields(Organization)}, "organization")
    for key in ("name", "assistantName", "defaultLanguage", "timeZone"):
        _text(org[key], f"organization.{key}")
    if org["defaultLanguage"] not in {"en", "ar"}:
        raise ConfigurationError("organization.defaultLanguage must be en or ar.")
    try:
        ZoneInfo(org["timeZone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigurationError("organization.timeZone must be a valid IANA time zone.") from error
    greeting = _object(org["greeting"], {"en", "ar"}, "organization.greeting")
    for language, text in greeting.items():
        _text(text, f"organization.greeting.{language}")
    features = _object(root["features"], {field.name for field in fields(Features)}, "features")
    if any(type(value) is not bool for value in features.values()):
        raise ConfigurationError("Every feature must be a boolean.")
    business = _object(root["business"], {field.name for field in fields(Business)}, "business")
    categories = _strings(business["caseCategories"], "business.caseCategories")
    if not categories or len(categories) > 30 or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", category) for category in categories):
        raise ConfigurationError("business.caseCategories requires 1-30 distinct lowercase category identifiers of at most 32 characters.")
    if type(business["locationRequired"]) is not bool:
        raise ConfigurationError("business.locationRequired must be a boolean.")
    knowledge = _object(root["knowledge"], {"allowedDomains"}, "knowledge")
    email = _object(root["emailPolicy"], {"allowedRecipients", "allowedDomains"}, "emailPolicy")
    phone = _object(root["phonePolicy"], {"allowedOutboundNumbers"}, "phonePolicy")
    numbers = _strings(phone["allowedOutboundNumbers"], "phonePolicy.allowedOutboundNumbers", PHONE)
    if len(numbers) > 20:
        raise ConfigurationError("Approve at most twenty outbound destinations.")
    return Deployment(
        1, Organization(**org), Features(**features),
        Business(categories, business["locationRequired"], _text(business["city"], "business.city", empty=True)),
        Knowledge(_strings(knowledge["allowedDomains"], "knowledge.allowedDomains", DOMAIN)),
        EmailPolicy(
            tuple(address.lower() for address in _strings(email["allowedRecipients"], "emailPolicy.allowedRecipients", EMAIL)),
            _strings(email["allowedDomains"], "emailPolicy.allowedDomains", DOMAIN),
        ),
        PhonePolicy(numbers),
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Cannot read valid JSON from {path.name}.") from error


def load_deployment(path: Path = ROOT / "deployment.json") -> Deployment:
    return parse_deployment(_read_json(path))


def load_profiles(path: Path = ROOT / "profiles.json") -> tuple[Profile, ...]:
    if not path.exists():
        return ()
    raw = _read_json(path)
    if not isinstance(raw, list):
        raise ConfigurationError("profiles.json must be an array.")
    profiles = []
    ids, phones = {"no_profile"}, set()
    for item in raw:
        item = _object(item, {field.name for field in fields(Profile)}, "profiles.json entry")
        for key, value in item.items():
            _text(value, f"profile.{key}", empty=key not in {"id", "display_name"})
        if item["id"] in ids or (item["phone"] and item["phone"] in phones):
            raise ConfigurationError("Profile ids and nonempty phone mappings must be unique.")
        if item["phone"] and not re.fullmatch(r"\+[1-9]\d{6,14}", item["phone"]):
            raise ConfigurationError("Profile phones must be E.164 numbers.")
        if item["email"] and not EMAIL.fullmatch(item["email"]):
            raise ConfigurationError("Profile email is invalid.")
        profiles.append(Profile(**item))
        ids.add(item["id"])
        phones.add(item["phone"])
    return tuple(profiles)


def load_content(root: Path = ROOT) -> tuple[str, str]:
    try:
        persona = (root / "system_prompt.md").read_text(encoding="utf-8").strip()
        knowledge = (root / "knowledge.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError("The packaged system_prompt.md and knowledge.md files must be readable UTF-8.") from error
    if len(knowledge.encode("utf-8")) > 64 * 1024:
        raise ConfigurationError("knowledge.md exceeds its 64 KiB budget.")
    knowledge = knowledge if knowledge.strip() else ""
    if knowledge and (
        not knowledge.startswith("# Organization service knowledge\n")
        or not re.search(r"(?im)^revision:\s*\S+", knowledge)
    ):
        raise ConfigurationError("Nonempty knowledge.md requires the Organization service knowledge heading and Revision: value.")
    if re.search(r"(?im)^status:\s*unconfigured\s*$", knowledge):
        if re.search(r"(?im)^#{2,3}\s+\d+\.", knowledge) or "https://" in knowledge:
            raise ConfigurationError("Unconfigured knowledge must not contain topics or source URLs.")
        knowledge = ""
    return persona, knowledge


DEPLOYMENT = load_deployment()
