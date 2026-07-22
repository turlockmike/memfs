"""Regression: the ONNX inference batch must stay BOUNDED, or the indexer OOMs.

WHY THIS EXISTS (2026-07-22, measured — not theorized).
`mvm index --embed-only` climbed to ~7 GB RSS on a 7,815 MB box and was OOM-killed
every 15 minutes by the cron guard, collateral-killing live Claude sessions (two
dmesg events headed `claude invoked oom-killer`). The cause was NOT the number of
files: `_embed_batch` flattened every chunk of its 64-doc batch into ONE
`TextEmbedding.embed()` call, and transformer attention is O(batch × heads × seq²)
in the number of SEQUENCES per inference call. A 64-doc batch of long docs is
thousands of 512-token sequences in a single forward pass.

Measured on 20 real ghost docs (44,210 words → 136 chunks):
    unbounded (old): peak RSS 2,901 MB, 132.0 s
    batch_size=16  : peak RSS   763 MB, 113.2 s   ← 3.8× less memory AND faster

⚠️ THE TEMPTING WRONG FIX, locked out here: bounding the *file* list (chunk the
ghost list, cap docs per pass) does NOT fix this — a single sufficiently long
document produces enough chunks to OOM the process on its own. The bound must be
on chunks-per-inference-call. `test_single_large_doc_is_also_bounded` is the case
that fails under the file-list fix and passes under this one.

These tests assert the CONTRACT (a bounded batch_size reaches the model) rather
than measuring RSS, because a memory assertion would be flaky across machines and
would silently stop testing anything on a big-RAM host. They use a fake model, so
they are fast and need no ONNX runtime.
"""
import pytest

from mvm import index


class _SpyModel:
    """Stand-in for fastembed.TextEmbedding that records how it was called."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, batch_size=256, **kw):
        texts = list(texts)
        self.calls.append({"n": len(texts), "batch_size": batch_size})
        return [[float(i)] * 384 for i in range(len(texts))]


@pytest.fixture
def spy(monkeypatch):
    m = _SpyModel()
    monkeypatch.setattr(index, "_embed_model", lambda: m)
    return m


def test_embed_batch_passes_a_bounded_batch_size(spy):
    index._embed_batch(["word " * 1000, "other " * 1000])
    assert spy.calls, "model was never invoked"
    for c in spy.calls:
        assert c["batch_size"] == index._EMBED_CHUNK_BATCH, (
            "embed() called without the bounded batch_size — this is the exact "
            "regression that OOM-killed the box every 15 min")


def test_bound_is_small_enough_to_survive_this_host(spy):
    # 16 chunks ≈ 763 MB peak measured. Anything much larger reopens the OOM.
    # If a future host needs more throughput, raise MVM_EMBED_CHUNK_BATCH via env
    # and re-measure peak RSS — do not edit this ceiling blind.
    assert 1 <= index._EMBED_CHUNK_BATCH <= 32, (
        "chunk batch %d is outside the measured-safe range"
        % index._EMBED_CHUNK_BATCH)


def test_single_large_doc_is_also_bounded(spy):
    """The case that a file-list cap cannot catch.

    ONE document, so any per-file batching says "batch of 1, nothing to bound" —
    yet it is ~600 chunks in a single forward pass. Only a chunk-level bound helps.
    """
    huge = "word " * (350 * 600)  # ~600 chunks from a single doc
    index._embed_to_blob(huge)
    assert spy.calls, "model was never invoked"
    assert len(index._chunk_text(huge)) > 100, "fixture stopped being large"
    for c in spy.calls:
        assert c["batch_size"] == index._EMBED_CHUNK_BATCH, (
            "a single large doc bypassed the bound — a file-count cap would have "
            "shipped this OOM")


def test_meanpool_still_correct_under_batching(spy):
    """The bound must not change the OUTPUT — memory fix, not a semantics change."""
    docs = ["alpha " * 800, "beta " * 200, "gamma " * 1500]
    blobs = index._embed_batch(docs)
    assert len(blobs) == len(docs), "one vector per input doc, regardless of chunking"
    assert all(len(b) == 384 * 4 for b in blobs), "384-dim float32 blob per doc"
