"""
Configuration package for the call center application.
"""

from .models import CallCenterConfig, ConversationConfig, get_config, set_config
from .loader import auto_load_config, load_config_from_file, load_config_from_env

__all__ = [
    "CallCenterConfig",
    "ConversationConfig", 
    "get_config",
    "set_config",
    "auto_load_config",
    "load_config_from_file",
    "load_config_from_env",
]
