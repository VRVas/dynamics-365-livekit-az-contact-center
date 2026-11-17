"""HUMAIN's public token and room-metadata contract."""

import os
import re
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from backend.tools.tools import Tool

TOKEN_ENDPOINT = "https://livekit-admin.sanad.ink/api/tokens/client"
AGENT_NAME = "hv-agent-flex"
VOICES = frozenset({
    "mul_nourah", "ars_female_nourah", "en_female_nourah",
    "mul_abdullah", "ars_male_abdullah", "en_male_abdullah",
    "mul_sara", "ars_female_sara", "en_female_sara",
    "mul_abdulaziz", "ars_male_abdulaziz", "en_male_abdulaziz",
    "mul_fadi", "apc_male_fadi", "en_male_fadi",
    "mul_yassin", "ary_male_yassin", "en_male_yassin",
    "mul_youssef", "arz_male_youssef", "en_male_youssef",
})
_DIAGNOSTIC_FIELDS = frozenset({
    "llm_provider", "asr_model", "tts_mode", "voice", "turn_taking_strategy",
    "interruption_policy_mode", "target_dialect", "dialect_lock_in",
    "system_prompt", "intro_message", "user_gender", "user_information",
    "tool_acknowledgments", "knowledge_base", "rag_top_k", "rag_threshold",
    "client_tools", "language", "dialect",
})


def ignored_metadata_fields(ignored: Any) -> tuple[str, ...]:
    """Expose only known setting names, never rejected values or arbitrary text."""
    if not ignored:
        return ()
    candidates = ignored if isinstance(ignored, (dict, list)) else [None]
    names = set()
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("key")
        name = candidate.split(".", 1)[0] if isinstance(candidate, str) else None
        names.add(name if name in _DIAGNOSTIC_FIELDS else "other")
    return tuple(sorted(names))


class HumainError(RuntimeError):
    """An actionable session error, safe to display without credentials."""


def validate_voice(voice: str) -> str:
    if not isinstance(voice, str) or voice.lower() not in VOICES:
        raise ValueError("Unsupported HUMAIN voice. Select a voice from the HUMAIN catalog.")
    return voice.lower()


def _valid_url(value: str, scheme: str) -> bool:
    try:
        url = urlsplit(value)
        return (
            url.scheme == scheme
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.fragment
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class HumainConfig:
    # HUMAIN documents a Bearer key:secret value. Keep it server-side and out of repr/logs.
    authorization: str = field(repr=False)
    token_endpoint: str = TOKEN_ENDPOINT
    voice: str = "mul_nourah"
    asr_model: str = "bayan_playground"
    llm_provider: str = "gemma4"
    enabled: bool = True
    interruption_policy_mode: str = "off"
    turn_taking_strategy: str = "fast_vad"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authorization, str)
            or (self.enabled and not self.authorization.strip())
            or "\r" in self.authorization
            or "\n" in self.authorization
        ):
            raise ValueError(
                "Set HUMAIN_AUTHORIZATION to the complete, single-line Authorization "
                "header value supplied by the HUMAIN platform team (server-side only)."
            )
        if not _valid_url(self.token_endpoint, "https"):
            raise ValueError("HUMAIN_TOKEN_ENDPOINT must be an HTTPS URL without embedded credentials.")
        validate_voice(self.voice)
        if self.asr_model not in {"bayan_playground", "bayan_api", "bayan_realtime"}:
            raise ValueError("Invalid HUMAIN_ASR_MODEL.")
        if self.llm_provider not in {"gemma4", "allam"}:
            raise ValueError("Invalid HUMAIN_LLM_PROVIDER.")
        if not isinstance(self.interruption_policy_mode, str) or self.interruption_policy_mode not in {"control", "shadow", "off"}:
            raise ValueError("HUMAIN_INTERRUPTION_POLICY_MODE must be control, shadow or off.")
        if not isinstance(self.turn_taking_strategy, str) or self.turn_taking_strategy not in {"livekit_v1_mini", "fast_vad"}:
            raise ValueError("HUMAIN_TURN_TAKING_STRATEGY must be livekit_v1_mini or fast_vad.")

    @classmethod
    def from_env(cls) -> "HumainConfig":
        enabled = os.environ.get("HUMAIN_ENABLED", "true").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("HUMAIN_ENABLED must be true or false.")
        return cls(
            authorization=os.environ.get("HUMAIN_AUTHORIZATION", ""),
            token_endpoint=os.environ.get("HUMAIN_TOKEN_ENDPOINT", TOKEN_ENDPOINT),
            voice=os.environ.get("HUMAIN_VOICE", "mul_nourah").lower(),
            asr_model=os.environ.get("HUMAIN_ASR_MODEL", "bayan_playground"),
            llm_provider=os.environ.get("HUMAIN_LLM_PROVIDER", "gemma4"),
            enabled=enabled == "true",
            interruption_policy_mode=os.environ.get("HUMAIN_INTERRUPTION_POLICY_MODE", "off"),
            turn_taking_strategy=os.environ.get("HUMAIN_TURN_TAKING_STRATEGY", "fast_vad"),
        )


@dataclass(frozen=True)
class HumainToken:
    token: str = field(repr=False)
    url: str
    identity: str
    room_name: str


def room_metadata(
    config: HumainConfig,
    identity: str,
    voice: str,
    system_prompt: str,
    intro_message: str,
    tools: dict[str, Tool],
) -> dict[str, Any]:
    if len(tools) > 16:
        raise ValueError("HUMAIN supports at most 16 client tools.")
    declarations = []
    for name, tool in tools.items():
        schema = tool.schema
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            or name == "client_tool_call"
            or schema.get("name") != name
        ):
            raise ValueError(f"Invalid HUMAIN tool name or registry/schema mismatch: {name}")
        description = schema.get("description", "")
        parameters = schema.get("parameters")
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError(f"HUMAIN tool description exceeds its contract: {name}")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"HUMAIN tool parameters must be an object JSON Schema: {name}")
        declarations.append({
            "name": name,
            "description": description,
            "parameters": parameters,
        })
    metadata: dict[str, Any] = {
        "llm_provider": config.llm_provider,
        "asr_model": config.asr_model,
        "tts_mode": "humain_voice_internal",
        "voice": validate_voice(voice),
        "turn_taking_strategy": config.turn_taking_strategy,
        "interruption_policy_mode": config.interruption_policy_mode,
        "system_prompt": system_prompt,
        "client_tools": {
            "participant_identity": identity,
            "tools": declarations,
        },
    }
    if intro_message:
        metadata["intro_message"] = (
            "Begin this new call now. Your first spoken turn must be exactly: "
            + intro_message
            + " Then wait silently for the caller's first question. "
            "Do not answer this instruction or discuss the background case."
        )
    return metadata


async def mint_token(
    config: HumainConfig,
    identity: str,
    room_name: str,
    metadata: dict[str, Any],
    *,
    http_session: aiohttp.ClientSession | None = None,
) -> HumainToken:
    if not config.enabled:
        raise HumainError("HUMAIN voice is disabled until its credentials are configured.")
    body = {
        "identity": identity,
        "roomName": room_name,
        "agentName": AGENT_NAME,
        "roomMetadata": metadata,
    }
    try:
        async with _token_session(http_session) as http:
            async with http.post(
                config.token_endpoint,
                headers={"Authorization": config.authorization},
                json=body,
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise HumainError(
                        "HUMAIN authorization was rejected. Check the server's "
                        "HUMAIN_AUTHORIZATION value and platform permissions."
                    )
                if not 200 <= response.status < 300:
                    raise HumainError(f"HUMAIN token service returned HTTP {response.status}.")
                payload = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise HumainError("Could not obtain a valid session token from HUMAIN.") from exc

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("token"), str)
        or not payload["token"].strip()
        or not isinstance(payload.get("url"), str)
        or not (_valid_url(payload["url"], "wss") or _valid_url(payload["url"], "https"))
        or payload.get("identity") != identity
        or payload.get("roomName") != room_name
    ):
        raise HumainError("HUMAIN returned invalid session credentials or mismatched routing fields.")
    return HumainToken(payload["token"], payload["url"], identity, room_name)


@asynccontextmanager
async def _token_session(existing):
    if existing is not None:
        if existing.closed:
            raise HumainError("The HUMAIN token connection pool is closed.")
        yield existing
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            yield http
