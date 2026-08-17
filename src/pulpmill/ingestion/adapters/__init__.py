"""Source adapters.

Importing this package registers every built-in adapter with the registry, so
`create_adapter` can resolve them by the `adapter:` name in configuration.
"""

from pulpmill.ingestion.adapters.fourchan import FourchanAdapter
from pulpmill.ingestion.adapters.reddit import RedditAdapter
from pulpmill.ingestion.adapters.x import XAdapter

__all__ = ["FourchanAdapter", "RedditAdapter", "XAdapter"]
