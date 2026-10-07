"""Ecosystem bridges. Outside the core per Law 6: own versioning, own breakage.

Each adapter consumes ONE outside ecosystem and translates it into native
IKAREM execution. Adapters are never shipped as core, never required, and
each one states what it does strictly better than the default — or it gets
deleted. See AGENTS.md Law 6 and each module's kill-switch note.
"""
