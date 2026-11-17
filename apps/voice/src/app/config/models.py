"""
Configuration models for call center operations including conversation settings
and agent handover configuration.
"""

from pydantic import BaseModel, Field
from typing import Optional


class ConversationConfig(BaseModel):
    """
    Configuration for conversation and bot behavior.
    """
    agent_phone_number: str = Field(
        ..., 
        description="Phone number to transfer calls to when customer requests human agent",
        example="+12025550101"
    )
    bot_company: str = Field(
        default="Your Company",
        description="Company name for the bot to identify itself"
    )
    bot_name: str = Field(
        default="Assistant",
        description="Name of the bot assistant"
    )
    bot_phone_number: str = Field(
        ...,
        description="Phone number of the bot/call center",
        example="+12025550102"
    )


class CallCenterConfig(BaseModel):
    """
    Main configuration for the call center application.
    """
    conversation: ConversationConfig
    
    # Azure Communication Services settings
    acs_connection_string: str = Field(
        ...,
        description="Azure Communication Services connection string"
    )
    acs_callback_url: str = Field(
        ..., 
        description="Callback URL for ACS events"
    )
    acs_media_streaming_url: str = Field(
        ...,
        description="WebSocket URL for media streaming"
    )
    
    # Optional settings
    enable_call_recording: bool = Field(
        default=False,
        description="Whether to enable call recording"
    )
    max_transfer_retries: int = Field(
        default=3,
        description="Maximum number of transfer retry attempts"
    )


# Global config instance (will be initialized from environment/config file)
_global_config: Optional[CallCenterConfig] = None


def get_config() -> CallCenterConfig:
    """
    Get the global configuration instance.
    
    Returns:
        CallCenterConfig: The global configuration
        
    Raises:
        RuntimeError: If configuration has not been initialized
    """
    if _global_config is None:
        raise RuntimeError("Configuration not initialized. Call set_config() first.")
    return _global_config


def set_config(config: CallCenterConfig) -> None:
    """
    Set the global configuration instance.
    
    Args:
        config: The configuration to set as global
    """
    global _global_config
    _global_config = config


def init_config_from_dict(config_dict: dict) -> CallCenterConfig:
    """
    Initialize configuration from a dictionary (e.g., from YAML/JSON).
    
    Args:
        config_dict: Dictionary containing configuration values
        
    Returns:
        CallCenterConfig: The initialized configuration
    """
    config = CallCenterConfig(**config_dict)
    set_config(config)
    return config
