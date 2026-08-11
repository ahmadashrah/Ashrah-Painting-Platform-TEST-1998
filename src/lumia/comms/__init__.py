"""The Communication Agent: field intake, project reporting and outbound messaging.

Deliberately import-light. The orchestrator lives in `desk.py` rather than
here, because `tools.py` in this package is mixed into the platform's
toolbox — importing the agent stack from this module's `__init__` would
close an import cycle through it.
"""
