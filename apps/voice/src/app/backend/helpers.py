import json
import logging
from openai.types.beta.realtime import (InputAudioBufferAppendEvent, SessionUpdateEvent)
from openai.types.beta.realtime.session_update_event import Session, SessionTurnDetection
from typing import Any, Literal, Optional
from backend.tools.tools import Tool

logger = logging.getLogger("helpers")

def transform_acs_to_openai_format(
    msg_data: Any, 
    model: Optional[str], 
    tools: dict[str, Tool], 
    system_message: Optional[str], 
    temperature: Optional[float], 
    max_tokens: Optional[int], 
    disable_audio: Optional[bool], 
    voice: str,
    transcribe_deployment: Optional[str] = None  # NEW: deployment name for gpt-4o-transcribe-diarize
) -> InputAudioBufferAppendEvent | SessionUpdateEvent | Any | None:
    """
    Transforms websocket message data from Azure Communication Services (ACS) to the OpenAI Realtime API format.
    Args:
        msg_data: The dict containing the ACS message data.
        model: The model name (optional).
        tools: Dictionary of tools to include in the session.
        system_message: The system prompt/instructions.
        temperature: Model temperature setting.
        max_tokens: Maximum response tokens.
        disable_audio: Whether to disable audio output.
        voice: The voice to use for TTS.
        transcribe_deployment: Deployment name for diarized transcription (e.g., gpt-4o-transcribe-diarize).
    Returns:
        The transformed message in the OpenAI Realtime API format, or None if not applicable.
    """
    oai_message: Any = None

    # Initial message from Azure Communication Services.
    # Set the initial configuration for the OpenAI Realtime API by sending a session.update message.
    if msg_data["kind"] == "AudioMetadata":
        # gpt-realtime-2 (GA realtime API) nested session schema. ACS media streaming is configured
        # for PCM 24 kHz mono (AudioFormat.PCM24_K_MONO), so input AND output use audio/pcm@24000
        # — same as the browser. The flat preview fields (voice/turn_detection/input_audio_format/
        # modalities/temperature) are rejected by gpt-realtime-2.
        session_obj = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.7,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,
                },
            },
            "tool_choice": "auto" if len(tools) > 0 else "none",
            "tools": [tool.schema for tool in tools.values()],
        }

        # Enable realtime input transcription only if a transcribe deployment is configured
        # (ACS Call Transcription handles phone transcripts otherwise).
        if transcribe_deployment:
            session_obj["audio"]["input"]["transcription"] = {"model": "gpt-4o-transcribe", "language": "en"}
            logger.info("ACS session: realtime input transcription enabled")

        if system_message is not None:
            session_obj["instructions"] = system_message
        if max_tokens is not None:
            session_obj["max_output_tokens"] = max_tokens

        oai_message = {"type": "session.update", "session": session_obj}

    # Message from Azure Communication Services with audio data.
    # Transform the message to the OpenAI Realtime API format.
    # UNMIXED audio streaming includes participantRawID to identify the speaker
    elif msg_data["kind"] == "AudioData":
        audio_data = msg_data.get("audioData", {})
        participant_id = audio_data.get("participantRawID", None)
        is_silent = audio_data.get("silent", False)
        
        oai_message = {
            "type": "input_audio_buffer.append",
            "audio": audio_data.get("data", ""),
            # Pass through participant info for speaker diarization
            "_participant_id": participant_id,
            "_is_silent": is_silent
        }
        
        # Log participant info for debugging (only if we have a participant ID)
        if participant_id and not is_silent:
            logger.debug(f"ACS audio from participant: {participant_id[:30]}...")

    return oai_message

def transform_openai_to_acs_format(msg_data: Any) -> Optional[Any]:
    """
    Transforms websocket message data from the OpenAI Realtime API format into the Azure Communication Services (ACS) format.
    Args:
        msg_data_json (str): The JSON string containing the message data from the OpenAI Realtime API.
    Returns:
        Optional[str]: A JSON string containing the transformed message in ACS format, or None if the message type is not handled.
    This is needed to plug the OpenAI Realtime API audio stream into Azure Communication Services.
    Both APIs have different message formats, so this function acts as a bridge between them.
    This method decides, if the given message is relevant for the ACS, and if so, it is transformed to the ACS format.
    """
    acs_message = None

    # Message from the OpenAI Realtime API with audio data.
    # Transform the message to the Azure Communication Services format.
    if msg_data["type"] == "response.output_audio.delta":
        acs_message = {
            "kind": "AudioData",
            "audioData": {
                "data": msg_data["delta"]
            }
        }

    # Message from the OpenAI Realtime API detecting, that the user starts speaking and interrupted the AI.
    # In this case, we don't want to send the unplayed audio buffer to the client anymore and clear the buffer audio.
    # Buffered audio is audio data that has been sent to Azure Communication Services, but not yet played by the client.
    if msg_data["type"] == "input_audio_buffer.speech_started":
        acs_message = {
            "kind": "StopAudio",
            "audioData": None,
            "stopAudio": {}
        }

    return acs_message

async def load_prompt_from_markdown(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        prompt = file.read()
    return prompt
