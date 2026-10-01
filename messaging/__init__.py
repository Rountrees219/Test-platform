"""
Messaging layer — ports & adapters.

Internal code should depend only on MessagingInterface (base.py).
Use get_messaging_interface() from factory.py to get the active channel.
"""

from messaging.base import MessagingInterface
from messaging.factory import get_messaging_interface

__all__ = ["MessagingInterface", "get_messaging_interface"]
