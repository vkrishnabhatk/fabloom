"""Web plane: the demo front end and JSON API over the run event log.

Boundary rule: handed a ready ``RunMachine`` by the composition root; never imports
``verify``, ``llm`` clients or ``eval``.
"""

from fabloom.web.app import create_app

__all__ = ["create_app"]
