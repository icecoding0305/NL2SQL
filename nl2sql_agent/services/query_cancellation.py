"""Cooperative cancellation primitives for running query graphs."""


class QueryExecutionCancelled(RuntimeError):
    """Raised between graph nodes after a user cancels a query."""

