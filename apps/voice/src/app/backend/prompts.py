"""
TTS prompt system for generating dynamic, randomized text-to-speech messages
for call center operations including transfer scenarios.
"""

import random
from typing import List
from pydantic import BaseModel

from runtime_config import DEPLOYMENT


class TransferPrompts(BaseModel):
    """
    Prompts related to call transfers and human handover.
    """
    
    # Prompts when ending assistant session to connect to human
    end_call_to_connect_agent_templates: List[str] = [
        "As requested, I'll connect you to an agent. Please stay on the line.",
        "Ending my session here. An agent will join you shortly.", 
        "I'll disconnect now, and an agent will assist you shortly.",
        "I'll end my call now. An agent will take over from here.",
        "Understood. Transferring you now, please hold.",
        "Let me connect you with a human agent who can better assist you.",
        "I'm transferring you to one of our specialists right away.",
    ]
    
    # Prompts while connecting/bridging to agent
    connect_agent_templates: List[str] = [
        "Connecting you to a specialist now. One moment, please.",
        "Hold on; I'm putting you through to an agent.",
        "I'm routing your call to the next available agent; thank you for holding.",
        "Please stay on the line while I transfer you to an agent.",
        "Transferring now. An agent will assist you shortly.",
        "Please hold while I connect you with someone who can help.",
        "One moment while I find the right person to assist you.",
    ]
    
    # Prompts when transfer fails
    transfer_failure_templates: List[str] = [
        "All lines are busy. We'll call you back in a moment.",
        "All our agents are busy. Expect a callback soon.", 
        "I can't reach an agent right now. We'll ring you back shortly.",
        "I'm unable to connect you. We'll return your call as soon as possible.",
        "No agents available at the moment. You'll hear from us shortly.",
        "Our agents are currently unavailable. We'll contact you as soon as possible.",
        "I'm having trouble connecting you. Please expect a call back within the next few minutes.",
    ]
    
    def get_end_call_to_connect_agent(self, company_name: str = None, bot_name: str = None) -> str:
        """
        Get a randomized prompt for ending assistant session before transfer.
        
        Args:
            company_name: Name of the company (optional)
            bot_name: Name of the bot (optional)
            
        Returns:
            Formatted prompt string
        """
        template = random.choice(self.end_call_to_connect_agent_templates)
        
        # Add context formatting if needed
        if company_name and "company" in template.lower():
            template = template.replace("{company}", company_name)
        if bot_name and "bot" in template.lower():
            template = template.replace("{bot}", bot_name)
            
        return template
    
    def get_connect_agent(self, company_name: str = None) -> str:
        """
        Get a randomized prompt for connecting to agent.
        
        Args:
            company_name: Name of the company (optional)
            
        Returns:
            Formatted prompt string
        """
        template = random.choice(self.connect_agent_templates)
        
        if company_name and "company" in template.lower():
            template = template.replace("{company}", company_name)
            
        return template
    
    def get_transfer_failure(self, company_name: str = None) -> str:
        """
        Get a randomized prompt for transfer failure.
        
        Args:
            company_name: Name of the company (optional)
            
        Returns:
            Formatted prompt string  
        """
        template = random.choice(self.transfer_failure_templates)
        
        if company_name and "company" in template.lower():
            template = template.replace("{company}", company_name)
            
        return template


class GeneralPrompts(BaseModel):
    """
    General prompts for call center operations.
    """
    
    greeting_templates: List[str] = [
        "Hello! This is {bot_name} from {company}. How can I help you today?",
        "Good day! {bot_name} here from {company}. What can I do for you?", 
        "Hi there! I'm {bot_name}, your virtual assistant from {company}. How may I assist you?",
        "Welcome to {company}! I'm {bot_name}. How can I help you today?",
        "Hello, this is {bot_name} from {company}. What can I assist you with?",
    ]
    
    goodbye_templates: List[str] = [
        "Thank you for calling {company}. Have a great day!",
        "It was a pleasure helping you today. Goodbye from {company}!",
        "Thank you for choosing {company}. Take care!",
        "Thanks for calling. Have a wonderful day!",
        "Goodbye, and thank you for contacting {company}!",
    ]
    
    error_templates: List[str] = [
        "I'm sorry, I didn't quite catch that. Could you please repeat?",
        "Could you say that again more slowly?",
        "I didn't understand. Can you clarify what you need?",
        "Sorry, I missed that. Please try again.",
        "I'm having trouble understanding. Could you rephrase that?",
    ]
    
    def get_greeting(self, company_name: str, bot_name: str) -> str:
        """Get a randomized greeting prompt."""
        template = random.choice(self.greeting_templates)
        return template.format(company=company_name, bot_name=bot_name)
    
    def get_goodbye(self, company_name: str) -> str:
        """Get a randomized goodbye prompt."""
        template = random.choice(self.goodbye_templates)
        return template.format(company=company_name)
    
    def get_error(self) -> str:
        """Get a randomized error prompt."""
        return random.choice(self.error_templates)


class PromptManager:
    """
    Central manager for all TTS prompts.
    """
    
    def __init__(self):
        self.transfer = TransferPrompts()
        self.general = GeneralPrompts()
    
    def get_context_from_config(self) -> tuple[str, str]:
        """
        Get company and bot name from global config.
        
        Returns:
            Tuple of (company_name, bot_name)
        """
        return DEPLOYMENT.organization.name, DEPLOYMENT.organization.assistantName
    
    def end_call_to_connect_agent(self) -> str:
        """Get prompt for ending assistant session before transfer."""
        company_name, bot_name = self.get_context_from_config()
        return self.transfer.get_end_call_to_connect_agent(company_name, bot_name)
    
    def connect_agent(self) -> str:
        """Get prompt for connecting to agent."""
        company_name, _ = self.get_context_from_config()
        return self.transfer.get_connect_agent(company_name)
    
    def transfer_failure(self) -> str:
        """Get prompt for transfer failure."""
        company_name, _ = self.get_context_from_config()
        return self.transfer.get_transfer_failure(company_name)
    
    def greeting(self) -> str:
        """Get greeting prompt."""
        company_name, bot_name = self.get_context_from_config()
        return self.general.get_greeting(company_name, bot_name)
    
    def goodbye(self) -> str:
        """Get goodbye prompt."""
        company_name, _ = self.get_context_from_config()
        return self.general.get_goodbye(company_name)
    
    def error(self) -> str:
        """Get error prompt."""
        return self.general.get_error()


# Global prompt manager instance
prompt_manager = PromptManager()
