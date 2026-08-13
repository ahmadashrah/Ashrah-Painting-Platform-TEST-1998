"""Lumia — the operating system for Ashrah Painting Ltd.

Growth wins the work; the Communication Agent delivers it. `Lumia` is the
front door to both:

    from lumia import Lumia
    lumia = Lumia()
    lumia.agents()
"""

from .agent import Agent, AgentRun
from .comms.desk import CommunicationDesk
from .facade import AgentInfo, Lumia
from .orchestrator import Orchestrator
from .workspace import Workspace

__version__ = "0.2.0"
__all__ = [
    "Agent",
    "AgentInfo",
    "AgentRun",
    "CommunicationDesk",
    "Lumia",
    "Orchestrator",
    "Workspace",
    "__version__",
]
