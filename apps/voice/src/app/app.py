import logging
import hashlib
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiohttp import ClientError, web
from azure.eventgrid import EventGridEvent, SystemEventNames
from azure.core.messaging import CloudEvent
from dotenv import load_dotenv

from backend.rtmt import RTMiddleTier
from backend.humain import HumainConfig
from backend.acs import AcsCaller
from backend.agent_assist import AgentAssistClient, AgentAssistConfig
from backend.acs_transcription import transcription_websocket_handler, get_transcription_handler
from backend.session_registry import get_session_registry
from backend.dynamics_outbound import DynamicsOutboundClient, DynamicsOutboundConfig, register_dynamics_outbound
from backend.native_call_probe import NativeCallProbe
from backend.native_media import NativeMediaAccessLogger, NativeMediaConfig, NativeMediaPilot
from backend.dataverse import DataverseClient
from backend.tools.human_handover import human_handover_tool
from backend.tools.send_customer_email import send_customer_email_tool
from backend.tools.case_tools import CaseServiceClient
from backend.tools.tools import Tool
from backend.crm_context import apply_customer_context
from runtime_config import DEPLOYMENT, Deployment, ConfigurationError, load_content, load_profiles
from runtime_rules import build_system_prompt


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("voicerag")

def get_local_datetime(deployment: Deployment = DEPLOYMENT):
    zone = deployment.organization.timeZone
    now = datetime.now(ZoneInfo(zone))
    return f"**Current Date & Time ({zone}):** {now.strftime('%A, %B %d, %Y at %I:%M %p')}"


# ---------------------------------------------------------------------
# CORS — the unified web UI (ca-ui-v2) calls this backend cross-origin for the
# in-browser realtime voice (/realtime WS) and the outbound phone call (/call POST).
# WebSocket upgrades don't trigger a CORS preflight, but the JSON POST does, so we
# answer preflight (OPTIONS) and echo an allowed Origin on every plain HTTP response.
# Restrict origins via CORS_ALLOWED_ORIGINS (comma-separated); defaults to reflecting
# the caller's Origin.
# ---------------------------------------------------------------------
def _cors_allow_origin(request: web.Request) -> str:
    origin = request.headers.get("Origin", "")
    allowed = os.environ.get("CORS_ALLOWED_ORIGINS", "*").strip()
    if allowed == "*" or not origin:
        return origin or "*"
    allow_list = [o.strip() for o in allowed.split(",") if o.strip()]
    return origin if origin in allow_list else (allow_list[0] if allow_list else "*")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": _cors_allow_origin(request),
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": request.headers.get(
                    "Access-Control-Request-Headers", "Content-Type"
                ),
                "Access-Control-Max-Age": "3600",
                "Vary": "Origin",
            },
        )
    response = await handler(request)
    # A prepared WebSocket response can't accept new headers; only decorate plain HTTP.
    if not isinstance(response, web.WebSocketResponse):
        response.headers["Access-Control-Allow-Origin"] = _cors_allow_origin(request)
        response.headers["Vary"] = "Origin"
    return response

# ---------------------------------------------------------------------
PROFILES = {profile.phone: asdict(profile) for profile in load_profiles() if profile.phone}


def apply_phone_profile(session: RTMiddleTier, number: str) -> str:
    # Approved mappings select a CRM lookup, never static identity or contact details.
    session.customer_name = None
    session.customer_email = None
    session.customer_phone = number if number in PROFILES else None
    return ""


async def create_app(deployment: Deployment | None = None):
    load_dotenv()
    BASE_DIR = Path(__file__).parent
    deployment = deployment or DEPLOYMENT
    features = deployment.features
    organization = deployment.organization

    # -------------  HUMAIN / LiveKit  -------------------------------
    humain_config = HumainConfig.from_env()
    if not humain_config.enabled:
        logger.warning("HUMAIN voice is disabled; web, health and tool configuration remain available.")
    case_client = CaseServiceClient(deployment)
    dynamics_outbound = DynamicsOutboundClient(
        DynamicsOutboundConfig.from_env() if features.nativeVoice else DynamicsOutboundConfig()
    )
    native_call_probe = NativeCallProbe.from_env() if features.nativeVoice else NativeCallProbe()

    # -------------  ACS / PSTN  --------------------------------------
    acs_source_number     = os.environ.get("ACS_SOURCE_NUMBER")
    acs_connection_string = os.environ.get("ACS_CONNECTION_STRING")
    acs_callback_url      = os.environ.get("ACS_CALLBACK_PATH")              # e.g. https://…/acs
    acs_media_ws          = os.environ.get("ACS_MEDIA_STREAMING_WEBSOCKET_PATH")
    acs_conference_setting = os.environ.get("ACS_AUTO_CONFERENCE_ENABLED", "false").strip().lower()
    if features.applicationTelephony and acs_conference_setting not in {"true", "false"}:
        logger.error("Invalid ACS_AUTO_CONFERENCE_ENABLED; choose true or false.")
        raise ValueError("ACS_AUTO_CONFERENCE_ENABLED must be true or false.")
    acs_auto_conference_enabled = acs_conference_setting == "true"
    
    # ACS Transcription config (Azure AI Services for real-time call transcription)
    acs_transcription_ws  = os.environ.get("ACS_TRANSCRIPTION_WEBSOCKET_PATH")  # e.g. wss://…/transcription-acs
    cognitive_services_ep = os.environ.get("AZURE_COGNITIVE_SERVICES_ENDPOINT")  # Azure AI Services endpoint

    caller: Optional[AcsCaller] = None
    if features.applicationTelephony:
        if not all([acs_source_number, acs_connection_string, acs_callback_url, acs_media_ws]):
            raise ConfigurationError("applicationTelephony requires ACS_SOURCE_NUMBER, ACS_CONNECTION_STRING, ACS_CALLBACK_PATH and ACS_MEDIA_STREAMING_WEBSOCKET_PATH.")
        caller = AcsCaller(
            source_number=acs_source_number,
            acs_connection_string=acs_connection_string,
            acs_callback_path=acs_callback_url,
            acs_media_streaming_websocket_path=acs_media_ws,
            acs_transcription_websocket_path=acs_transcription_ws if features.agentAssist else None,
            cognitive_services_endpoint=cognitive_services_ep if features.agentAssist else None,
            auto_conference_enabled=acs_auto_conference_enabled,
            auto_conference_number=os.environ.get("ACS_AUTO_CONFERENCE_NUMBER", "").strip(),
            allowed_outbound_numbers=deployment.phonePolicy.allowedOutboundNumbers,
        )
        if cognitive_services_ep and acs_transcription_ws:
            logger.info(f"ACS Call Transcription enabled via Azure AI Services")
        else:
            logger.warning("ACS Call Transcription disabled - missing AZURE_COGNITIVE_SERVICES_ENDPOINT or ACS_TRANSCRIPTION_WEBSOCKET_PATH")

    # -------------  Prompts  -----------------------------------------
    persona, packaged_knowledge = load_content()
    embedded_knowledge = packaged_knowledge
    embedded_bytes = len(embedded_knowledge.encode("utf-8"))
    embedded_hash = hashlib.sha256(embedded_knowledge.encode("utf-8")).hexdigest()
    inbound_prompt = build_system_prompt(deployment, persona, embedded_knowledge)
    outbound_prompt = inbound_prompt + "\nThis is a requested callback. Briefly check whether the person can speak before continuing."


    # -------------  RTMiddleTier  ------------------------------------
    rtmt = RTMiddleTier(humain_config, deployment)
    rtmt.system_message = get_local_datetime(deployment) + "\n\n" + inbound_prompt
    rtmt.intro_message = organization.greeting[organization.defaultLanguage]

    # Configure CCAgent Assist Console integration
    agent_assist_url = os.environ.get("AGENT_ASSIST_BASE_URL") or os.environ.get("CCAGENT_ASSIST_URL")
    agent_assist_key = os.environ.get("AGENT_ASSIST_API_KEY") or os.environ.get("CCAGENT_ASSIST_API_KEY")
    agent_assist_enabled = os.environ.get("AGENT_ASSIST_ENABLED", "true").lower() == "true"
    if features.agentAssist and agent_assist_url and agent_assist_key and agent_assist_enabled:
        agent_assist_config = AgentAssistConfig(
            base_url=agent_assist_url,
            api_key=agent_assist_key,
            channel="voice",  # Default to voice, will be updated per session
            enabled=True
        )
        rtmt.agent_assist_client = AgentAssistClient(agent_assist_config)
        logger.info(f"CCAgent Assist integration enabled: {agent_assist_url}")
    else:
        logger.warning("CCAgent Assist integration disabled (missing AGENT_ASSIST_BASE_URL, AGENT_ASSIST_API_KEY, or AGENT_ASSIST_ENABLED=false)")

    # Store caller instance in rtmt for tools to access
    rtmt.acs_caller = caller
    
    # Store rtmt reference in caller for reverse access
    if caller:
        caller._rtmt_instance = rtmt
    
    # Configure ACS transcription handler to use RTMT for Agent Assist
    transcription_handler = get_transcription_handler()
    transcription_handler._rtmt_instance = rtmt if features.agentAssist else None
    logger.info("ACS Transcription handler linked to RTMT for Agent Assist forwarding")

    if features.email:
        rtmt.tools["send_customer_email"] = send_customer_email_tool(deployment)
    rtmt.tools.update(case_client.tools())

    if caller:
        rtmt.tools["talk_to_human"] = human_handover_tool(caller)
        logger.info("Handover tool registered; the operator is selected per eligible outbound call.")
    else:
        logger.warning("Phone handover is unavailable until ACS calling is configured.")
    logger.info("Voice tools registered: %s", sorted(rtmt.tools))

    async def unbound_native_handover(arguments):
        raise ValueError("Native handover is unavailable until this session is bound to its validated Dynamics conversation.")

    def native_voice_session(phone, language):
        session = rtmt.clone_for_session()
        session.language = language
        session.acs_caller = None
        session.agent_assist_client = None
        session.customer_phone = phone
        session.system_message = get_local_datetime(deployment) + "\n\n" + inbound_prompt
        session.intro_message = organization.greeting[language]
        # NativeMediaPilot binds the target after validating the existing conversation.
        session.tools = {**session.tools, "talk_to_human": Tool(
            schema={
                "type": "function", "name": "talk_to_human",
                "description": "Request human assistance within the existing Dynamics conversation. A request is not a connected representative.",
                "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
            target=unbound_native_handover,
        )}
        return session

    native_media = NativeMediaPilot(
        NativeMediaConfig.from_env() if features.nativeVoice else NativeMediaConfig(),
        DataverseClient(dynamics_outbound.config.org_url, dynamics_outbound.config.tenant_id, timeout=6),
        native_call_probe, native_voice_session,
        customer_context_provider=case_client.fetch_customer_context if features.crm else None,
    )

    # =================================================================
    #  H A N D L E R S
    # =================================================================
    async def index(request):
        ui_url = os.environ.get("CUSTOMER_UI_URL", "").strip()
        if ui_url and urlsplit(ui_url).scheme not in {"https", "http"}:
            logger.error("Invalid CUSTOMER_UI_URL.")
            return web.Response(text="Customer interface configuration is invalid.", status=503)
        notice = "" if ui_url else "<p>The customer chat address is not configured on this voice page.</p>"
        content = (BASE_DIR / "static" / "index.html").read_text()
        return web.Response(
            text=content.replace("__CUSTOMER_UI_URL__", escape(ui_url or "about:blank", quote=True))
            .replace("__CHAT_NOTICE__", notice)
            .replace("__ORGANIZATION_NAME__", escape(organization.name))
            .replace("__ASSISTANT_NAME__", escape(organization.assistantName)),
            content_type="text/html",
        )

    async def public_config(request):
        return web.json_response({"organization": asdict(organization), "features": asdict(features)})

    async def health(request):
        return web.json_response({
            "service": "voice",
            "status": "configured" if humain_config.enabled else "awaiting_humain_credentials",
            "provider": "humain",
            "voice_enabled": humain_config.enabled,
            "interruption_policy_mode": humain_config.interruption_policy_mode,
            "turn_taking_strategy": humain_config.turn_taking_strategy,
            "acs_configured": caller is not None,
            "automatic_phone_conference_enabled": caller is not None and acs_auto_conference_enabled,
            "assist_configured": rtmt.agent_assist_client is not None,
            "case_service_configured": case_client.configured,
            "features": asdict(features),
            "knowledge_mode": "embedded_first" if features.knowledge else "disabled",
            "default_language": organization.defaultLanguage,
            "embedded_knowledge_sha256": embedded_hash,
            "embedded_knowledge_bytes": embedded_bytes,
            "knowledge_fallback_configured": features.knowledge and case_client.configured,
            "email_configured": features.email and bool(os.environ.get("ACS_EMAIL_CONNECTION_STRING") and os.environ.get("ACS_EMAIL_SENDER")),
            "dynamics_outbound": dynamics_outbound.health(),
            "native_call_probe": native_call_probe.health(),
            "native_media_pilot": native_media.health(),
            "tools": sorted(rtmt.tools),
        })

    # ----------  OUTBOUND CALL ( /call )  ----------------------------
    async def call(request: web.Request):
        if not features.applicationTelephony:
            return web.json_response({"error": {"code": "MODULE_DISABLED", "message": "Application telephony is disabled."}}, status=503)
        if not humain_config.enabled:
            logger.warning("Outbound call refused: HUMAIN is not configured.")
            return web.Response(text="HUMAIN voice is not enabled yet. Configure its credentials before placing calls.", status=503)
        body   = await request.json()
        if not isinstance(body, dict) or body.get("language", organization.defaultLanguage) not in ("en", "ar"):
            logger.warning("Outbound call refused: invalid language or request shape.")
            return web.Response(text="Choose English or Arabic for the call.", status=400)
        target = body.get("number")
        operator = body.get("operator_number")
        if not isinstance(target, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", target) or (
            operator is not None and (not isinstance(operator, str) or operator and not re.fullmatch(r"\+[1-9][0-9]{6,14}", operator))
        ):
            logger.warning("Outbound call refused: invalid E.164 number.")
            return web.Response(text="Enter a valid international phone number.", status=400)
        if not (caller and target):
            return web.Response(text="Outbound calling is not configured", status=400)
        if not deployment.phone_allowed(target) or operator and not deployment.phone_allowed(operator):
            logger.warning("Outbound call refused: a destination is outside the approved dialing policy.")
            return web.Response(text="This recipient is not an approved dialing destination. No call was placed.", status=403)

        # One session per outbound call. The web app supplies the operator number, so this
        # web-initiated outbound test call transfers to whoever the tester entered (no env mutation),
        # and live handover is enabled only for these calls.
        session = rtmt.clone_for_session()
        session.language = body.get("language", organization.defaultLanguage)
        session.operator_transfer_number = (body.get("operator_number") or "").strip() or None
        session.transfer_enabled = True
        logger.warning(f"[CALL] operator_transfer_number={session.operator_transfer_number}")

        # Build outbound prompt
        session.system_message = apply_phone_profile(session, target) + "\n" + get_local_datetime(deployment) + "\n\n" + outbound_prompt
        session.system_message += (
            f"\nInitial explicitly selected response language: {session.language}. "
            "Keep this language until an explicit language-change request; do not infer a switch from the input language."
        )
        session.intro_message = organization.greeting[session.language]
        if features.crm:
            try:
                apply_customer_context(session, await case_client.fetch_customer_context(session))
            except (ClientError, TimeoutError, ValueError):
                return web.Response(text="Dynamics customer context could not be verified. No call was placed.", status=503)
        session.intro_message += (
            " هل يناسبك التحدث الآن؟" if session.language == "ar" else " Is now a good time to speak?"
        )
        logger.warning(f"[CALL] from={caller.source_number} to={target}")

        # <<< robust call >>>
        call_id = None
        try:
            # 1️⃣ attempt "new" signature (returns the callConnectionId)
            call_id = await caller.initiate_call(
                target_number=target,
                callback_url=acs_callback_url,
                media_ws_path=acs_media_ws
            )
        except TypeError as te:
            # fallback to legacy signature
            logger.warning(f"Init-call TypeError → retry legacy signature: {te}")
            try:
                call_id = await caller.initiate_call(target)
            except Exception as exc:          # still failed
                logger.error(f"CreateCallFailed (legacy): {exc}")
                return web.Response(text="Call failed – see logs", status=500)
        except Exception as exc:
            logger.error(f"CreateCallFailed (Python): {exc}")
            return web.Response(text="Call failed – see logs", status=500)

        # Bind the session to the new call so the media WS / events / transcription resolve to it.
        if call_id:
            get_session_registry().register(call_id, session)
            logger.warning(f"[CALL] registered outbound session for call {call_id}")

        return web.Response(text="Created outbound call")

    # ----------  WEBSOCKET (UI)  -------------------------------------
    async def websocket_handler(request):
        if not features.browserVoice:
            return web.json_response({"error": {"code": "MODULE_DISABLED", "message": "Browser voice is disabled."}}, status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # Each in-browser mic session gets its OWN RTMiddleTier so concurrent web users never share
        # persona / handover / transcript state. No PSTN leg => handover stays disabled.
        session = rtmt.clone_for_session()
        session.transfer_enabled = False
        session.operator_transfer_number = None
        session.system_message = get_local_datetime(deployment) + "\n\n" + inbound_prompt
        await session.forward_messages(ws, False)
        return ws

    # ----------  WEBSOCKET (ACS audio)  ------------------------------
    async def websocket_handler_acs(request):
        if not features.applicationTelephony:
            return web.json_response({"error": {"code": "MODULE_DISABLED", "message": "Application telephony is disabled."}}, status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # ACS stamps the media-streaming WS with the call's id (audio-streaming quickstart:
        # x-ms-call-connection-id). Use it to bind this media stream to the session that the
        # incoming/outbound call handler already created for this call.
        call_id = request.headers.get("x-ms-call-connection-id")
        registry = get_session_registry()
        session = registry.get(call_id)
        if session is None:
            # Never reuse another call's LiveKit room when a new call id arrives.
            # An unkeyed stream can use the fallback only when there is exactly one call.
            session = registry.most_recent() if not call_id and registry.active_count == 1 else None
            if session is None:
                session = rtmt.clone_for_session()
                session.system_message = get_local_datetime(deployment) + "\n\n" + inbound_prompt
            if call_id:
                registry.register(call_id, session)
        elif call_id and session.current_call_connection_id != call_id:
            registry.register(call_id, session)
        await session.forward_messages(ws, True)
        return ws

    # ----------  INBOUND CALL (Event Grid)  --------------------------
    async def incoming_call_handler(request):
        try:
            request_body = await request.json()
            print(f"CALL_TRACKING: Incoming call handler received {len(request_body)} events")
            logger.info(f"CALL_TRACKING: Incoming call handler received {len(request_body)} events")
            
            for raw in request_body:
                ev = EventGridEvent.from_dict(raw)
                print(f"CALL_TRACKING: Processing event type: {ev.event_type}")
                logger.info(f"CALL_TRACKING: Processing event type: {ev.event_type}")
                
                if ev.event_type == SystemEventNames.EventGridSubscriptionValidationEventName:
                    return web.json_response({"validationResponse": ev.data["validationCode"]})

                if ev.event_type == "Microsoft.Communication.IncomingCall":
                    if not humain_config.enabled or caller is None:
                        logger.warning("Incoming call refused: voice/ACS is not configured.")
                        return web.Response(text="Voice calling is not configured.", status=503)
                    print(f"CALL_TRACKING: Processing incoming call (active calls={get_session_registry().active_count})")
                    logger.info(f"CALL_TRACKING: Processing incoming call (active calls={get_session_registry().active_count})")
                    
                    from_obj = ev.data.get("from", {}) or {}
                    caller_id = ((from_obj.get("phoneNumber") or {}).get("value") or "").strip()
                    if not caller_id:
                        m = re.search(r"\+?\d{6,15}", str(from_obj.get("rawId", "")))
                        caller_id = ("+" + re.sub(r"\D", "", m.group(0))) if m else ""
                    print(f"CALL_TRACKING: Caller ID: {caller_id}")
                    logger.info(f"CALL_TRACKING: Caller ID: {caller_id}")
                    
                    # One RTMiddleTier session per inbound call, keyed by callConnectionId so several
                    # concurrent PSTN calls stay isolated. Inbound PSTN: handover disabled (privacy);
                    # the inbound prompt also instructs the model never to call talk_to_human.
                    session = rtmt.clone_for_session()
                    session.transfer_enabled = False
                    session.operator_transfer_number = None

                    session.system_message = apply_phone_profile(session, caller_id) + "\n" + get_local_datetime(deployment) + "\n\n" + inbound_prompt
                    if features.crm:
                        apply_customer_context(session, await case_client.fetch_customer_context(session))
                    call_connection_id = await caller.handle_incoming_call(ev.data, acs_callback_url)
                    
                    # Register the session under the real call id so the media WS, transcription WS
                    # and call-event webhooks all resolve to it.
                    get_session_registry().register(call_connection_id, session)
                    print(f"CALL_TRACKING: NEW CALL - registered session for call connection ID: {call_connection_id}")
                    logger.info(f"CALL_TRACKING: NEW CALL - registered session for call connection ID: {call_connection_id}")
            return web.Response(status=200)
        except Exception as exc:
            logger.error(f"incoming_call_handler: {exc}")
            return web.Response(text=str(exc), status=500)

    # ----------  CALL EVENTS (PSTN life-cycle)  ----------------------
    async def call_event_handler(request):
        try:
            request_body = await request.json()
            print(f"CALL_TRACKING: Call event handler received {len(request_body)} events")
            logger.info(f"CALL_TRACKING: Call event handler received {len(request_body)} events")
            
            for raw in request_body:
                ce = CloudEvent.from_dict(raw)
                print(f"CALL_TRACKING: Processing call event type: {ce.type}")
                logger.info(f"CALL_TRACKING: Processing call event type: {ce.type}")

                if caller is not None and await caller.handle_conference_event(ce):
                    continue

                # Resolve the session driving THIS specific call so concurrent calls stay isolated.
                registry = get_session_registry()
                _evt_call_id = (ce.data or {}).get("callConnectionId")
                session = registry.get(_evt_call_id)

                if ce.type == "Microsoft.Communication.CreateCallFailed":
                    logger.error(f"ACS-CreateCallFailed: {ce.data.get('resultInformation')}")
                elif ce.type == "Microsoft.Communication.CallConnected":
                    call_id = ce.data["callConnectionId"]
                    print(f"CALL_TRACKING: Call connected - ID: {call_id}")
                    logger.info(f"CALL_TRACKING: Call connected - ID: {call_id}")
                    if session is not None:
                        session.current_call_connection_id = call_id
                        print(f"CALL_TRACKING: bound call ID {call_id} to its session")
                        logger.info(f"CALL_TRACKING: bound call ID {call_id} to its session")
                    # TODO: TTS functionality temporarily disabled to simplify transfer testing
                    # await caller.play_message(call_id, "Hello! How can I assist you today?", "ChatContext")
                elif ce.type == "Microsoft.Communication.CallDisconnected":
                    call_id = ce.data.get("callConnectionId", "unknown")
                    print(f"CALL_TRACKING: Call disconnected - ID: {call_id}")
                    logger.info(f"CALL_TRACKING: Call disconnected - ID: {call_id}")
                    if session is not None:
                        # Reset this call's per-call state (defensive; the session is about to be dropped).
                        session.current_call_connection_id = None
                        session.operator_transfer_number = None
                        session.transfer_enabled = False
                        session.transfer_active = False
                        transferred_calls = getattr(session, "_transferred_calls", None)
                        if isinstance(transferred_calls, set) and call_id in transferred_calls:
                            print(f"CALL_TRACKING: Transfer call completed - human party hung up (call {call_id})")
                            logger.info(f"CALL_TRACKING: Transfer call completed - human party hung up (call {call_id})")
                            transferred_calls.remove(call_id)
                            # De-escalate the session - human attention no longer needed
                            if session.agent_assist_client and session.agent_assist_session:
                                await session.agent_assist_client.deescalate_session(
                                    session.agent_assist_session,
                                    reason="Human agent call ended"
                                )
                                print(f"CALL_TRACKING: De-escalated CCAgent session - humanAttentionNeeded set to false")
                                logger.info(f"CALL_TRACKING: De-escalated CCAgent session")
                    # Drop this call's session + transcription tracker from the registries.
                    registry.unregister(_evt_call_id)
                    get_transcription_handler().cleanup_call(_evt_call_id or call_id)
                    print(f"CALL_TRACKING: Cleaned up call {call_id} (active calls={registry.active_count})")
                    logger.info(f"CALL_TRACKING: Cleaned up call {call_id} (active calls={registry.active_count})")
                elif ce.type == "Microsoft.Communication.CallTransferFailed":
                    call_id = ce.data.get("callConnectionId", "unknown")
                    transfer_error = ce.data.get("resultInformation", {})
                    print(f"CALL_TRACKING: Call transfer failed - ID: {call_id}, Error: {transfer_error}")
                    logger.error(f"CALL_TRACKING: Call transfer failed - ID: {call_id}, Error: {transfer_error}")
                elif ce.type == "Microsoft.Communication.CallTransferAccepted":
                    call_id = ce.data.get("callConnectionId", "unknown")
                    print(f"CALL_TRACKING: Call transfer accepted - ID: {call_id}")
                    logger.info(f"CALL_TRACKING: Call transfer accepted - ID: {call_id}")
                    # After successful transfer, the call might be disconnected
                    print(f"CALL_TRACKING: Transfer successful - call may be handed off")
                    logger.info(f"CALL_TRACKING: Transfer successful - call may be handed off")
            return web.Response(status=200)
        except Exception as exc:
            logger.error(f"call_event_handler: {exc}")
            return web.Response(text=str(exc), status=500)

    # =================================================================
    #  A P P  /  R O U T E S
    # =================================================================
    app = web.Application(
        middlewares=[cors_middleware],
        handler_args={"access_log_class": NativeMediaAccessLogger},
    )
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/config", public_config)
    register_dynamics_outbound(app, dynamics_outbound)
    native_call_probe.register(app)
    native_media.register(app)
    app.router.add_static("/static/", path=str(BASE_DIR / "static"), name="static")
    app.router.add_post("/call", call)
    app.router.add_get("/realtime", websocket_handler)
    app.router.add_get("/realtime-acs", websocket_handler_acs)
    async def transcription(request):
        if not (features.agentAssist and features.applicationTelephony):
            return web.json_response({"error": {"code": "MODULE_DISABLED", "message": "Application transcription ingestion is disabled."}}, status=503)
        return await transcription_websocket_handler(request)

    app.router.add_get("/transcription-acs", transcription)
    app.router.add_post("/acs/incoming-call", incoming_call_handler)
    app.router.add_post("/acs/call-events",  call_event_handler)
    if caller:
        app.router.add_post("/acs", caller.outbound_call_handler)

    async def warm_native_transport(app):
        await native_media.start_supervision()
        await native_media.warmup()

    app.on_startup.append(warm_native_transport)

    async def close_clients(app):
        await native_media.close()
        native_call_probe.clear()
        await dynamics_outbound.close()
        await case_client.close()
        if rtmt.agent_assist_client:
            await rtmt.agent_assist_client.close()
        if caller:
            await caller.close()

    app.on_cleanup.append(close_clients)
    return app


# ---------------------------------------------------------------------
if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
