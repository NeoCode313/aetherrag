"""Multimodal RAG system — hexagonal architecture.

Layers (dependencies point inwards only):
    presentation -> application -> domain
    infrastructure -> (implements) domain.ports
"""

__version__ = "1.0.0"
