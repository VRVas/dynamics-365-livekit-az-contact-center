"""
Configuration loader for the call center application.
Loads configuration from YAML files and environment variables.
"""

import os
import yaml
from typing import Optional

from .models import CallCenterConfig, init_config_from_dict
from runtime_config import DEPLOYMENT


def load_config_from_file(config_path: str) -> CallCenterConfig:
    """
    Load configuration from a YAML file.
    
    Args:
        config_path: Path to the YAML configuration file
        
    Returns:
        CallCenterConfig: Loaded configuration
        
    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config is invalid
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        config_data = yaml.safe_load(f)
    
    return init_config_from_dict(config_data)


def load_config_from_env() -> CallCenterConfig:
    """
    Load configuration from environment variables.
    
    Returns:
        CallCenterConfig: Configuration loaded from environment
        
    Raises:
        ValueError: If required environment variables are missing
    """
    # Build configuration dictionary from environment variables
    config_data = {
        "conversation": {
            "agent_phone_number": os.getenv("AGENT_PHONE_NUMBER"),
            "bot_company": DEPLOYMENT.organization.name,
            "bot_name": DEPLOYMENT.organization.assistantName,
            "bot_phone_number": os.getenv("BOT_PHONE_NUMBER"),
        },
        "acs_connection_string": os.getenv("ACS_CONNECTION_STRING"),
        "acs_callback_url": os.getenv("ACS_CALLBACK_URL"),
        "acs_media_streaming_url": os.getenv("ACS_MEDIA_STREAMING_WEBSOCKET_PATH"),
        "enable_call_recording": os.getenv("ENABLE_CALL_RECORDING", "false").lower() == "true",
        "max_transfer_retries": int(os.getenv("MAX_TRANSFER_RETRIES", "3")),
    }
    
    # Validate required fields
    required_fields = [
        "conversation.agent_phone_number",
        "conversation.bot_phone_number", 
        "acs_connection_string",
        "acs_callback_url",
        "acs_media_streaming_url",
    ]
    
    missing_fields = []
    for field in required_fields:
        if "." in field:
            # Handle nested fields
            parts = field.split(".")
            value = config_data
            for part in parts:
                value = value.get(part)
                if value is None:
                    missing_fields.append(field)
                    break
        else:
            if not config_data.get(field):
                missing_fields.append(field)
    
    if missing_fields:
        raise ValueError(f"Missing required environment variables: {missing_fields}")
    
    return init_config_from_dict(config_data)


def auto_load_config() -> CallCenterConfig:
    """
    Automatically load configuration from file or environment.
    
    Tries to load from:
    1. config.yaml in current directory
    2. config.yaml in parent directory
    3. Environment variables
    
    Returns:
        CallCenterConfig: Loaded configuration
        
    Raises:
        ValueError: If no valid configuration found
    """
    # Try config.yaml in current directory
    config_paths = [
        "config.yaml",
        "../config.yaml",
        "../../config.yaml",
    ]
    
    for path in config_paths:
        if os.path.exists(path):
            print(f"Loading configuration from: {path}")
            return load_config_from_file(path)
    
    # Try environment variables
    try:
        print("Loading configuration from environment variables")
        return load_config_from_env()
    except ValueError:
        pass
    
    # No configuration found
    raise ValueError(
        "No configuration found. Please create a config.yaml file or set environment variables. "
        "See config.example.yaml for an example."
    )
