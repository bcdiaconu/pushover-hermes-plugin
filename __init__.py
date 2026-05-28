"""Pushover platform plugin for Hermes Agent.

Entry point for both user plugin loader (hermes plugins install)
and pip-installed entry-point discovery.
"""

from .adapter import register

__all__ = ["register"]
