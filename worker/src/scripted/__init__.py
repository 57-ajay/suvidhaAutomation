# worker/src/scripted/__init__.py
"""SCRIPTED (web-source, human-handover) border-tax module.

Self-contained on purpose: the wizard/handover/helpers in here are deliberate
copies of their tasks/border_tax counterparts (module isolation over DRY, by
decision), so a scripted issue is always debugged inside this one directory.
Shared with the rest of the worker: engine, lifecycle, api_client,
redis_client, config — the platform, never the flow.
"""
