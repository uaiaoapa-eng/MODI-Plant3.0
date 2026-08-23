# Local RAG data

The files in this directory are generated runtime assets and may contain
session identifiers, user identifiers, conversation-derived text, and
embeddings. They are intentionally excluded from source packages.

Build a clean local corpus from the reviewed templates in `reference/` before
enabling the local RAG backend. See `scripts/build_ontology.py` and
`scripts/build_embeddings.py` for the regeneration entry points.
