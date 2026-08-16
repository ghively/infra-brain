"""Infra Brain conversational agent (provider-agnostic, UI-independent).

This package hosts the LangGraph chat agent and its read-only SQL tools. It was
moved out of ``infra_brain.ui.chat`` so the FastAPI dashboard can stream the same
agent without depending on the (legacy) Streamlit UI package or pandas.
"""
