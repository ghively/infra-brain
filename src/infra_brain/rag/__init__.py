"""Shared RAG (retrieval-augmented-generation) building blocks.

Kept separate from any single agent so the ingestion/chunking logic has ONE
home, reused by both the Confluence path (``agents.knowledge.KnowledgeAgent``)
and the local repo-docs path (``agents.local_docs.LocalDocsAgent``). TRK-122.
"""
