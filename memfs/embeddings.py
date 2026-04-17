"""Vector embeddings — stub for Neo4j port.

Vector search is deferred for M1. This module is retained only to avoid
import errors from other modules that may conditionally reference it. The
original sentence-transformers + cosine-search implementation is dropped
until a native Neo4j vector index adapter is added.
"""

# Intentionally minimal. Any caller using embed_file / embed_all / cosine_search
# will get AttributeError — by design. Reintroduce with a Neo4j-native vector
# index (Neo4j 5 supports HNSW) when vector search is back on the roadmap.
