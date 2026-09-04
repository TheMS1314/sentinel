"""
Case Retriever (light RAG)
============================
Retrieves similar PAST decisions from Sentinel's own audit log, to ground
the LLM reasoner's edge-case judgments in precedent rather than reasoning
cold on every ambiguous request.

Design choice -- local TF-IDF, not LLM embeddings:
  Retrieval here only needs to match similar merchant/category/intent text
  patterns across a modest, well-structured corpus (your own logged
  decisions) -- not open-ended semantic search over messy unstructured
  documents. TF-IDF handles that well, and it means:
    - zero API cost or added latency for every single transaction check
    - zero network dependency for a payments trust system's core path
      (a system that verifies payments should not have an external
      network call as a single point of failure for its own reasoning)
    - fully deterministic and debuggable retrieval, which matters for
      an auditable system

If the corpus ever grew large and heterogeneous enough that TF-IDF
stopped being good enough, swapping in embeddings later is a contained
change -- this class is the only place that would need to change.
"""

from __future__ import annotations
from dataclasses import dataclass
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np


@dataclass
class PastCase:
    request_id: str
    text: str              # the searchable text representation of the case
    verdict: str
    reasons: list[str]
    merchant_category: str
    merchant_name: str
    amount: float


class CaseRetriever:
    """
    Holds an in-memory index of past cases (loaded from the audit log) and
    retrieves the top-k most textually similar cases to a new request.
    Rebuild the index periodically (e.g. once per batch run) rather than
    on every single call, for efficiency -- see `refresh()`.
    """

    def __init__(self):
        self._cases: list[PastCase] = []
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None

    @staticmethod
    def _case_text(merchant_category: str, merchant_name: str, stated_intent: str, amount: float) -> str:
        # Deliberately include category + intent as the dominant signal --
        # amount is bucketed coarsely so retrieval matches on "kind of spend"
        # rather than exact figures, which would fragment the corpus.
        bucket = "low" if amount < 500 else "mid" if amount < 1500 else "high"
        return f"{merchant_category} {merchant_name} {stated_intent} amount_{bucket}"

    def refresh(self, past_cases: list[PastCase]):
        self._cases = past_cases
        if not past_cases:
            self._vectorizer = None
            self._matrix = None
            return
        texts = [c.text for c in past_cases]
        self._vectorizer = TfidfVectorizer(stop_words="english")
        self._matrix = self._vectorizer.fit_transform(texts)

    def retrieve_similar(
        self,
        merchant_category: str,
        merchant_name: str,
        stated_intent: str,
        amount: float,
        top_k: int = 3,
        min_similarity: float = 0.15,
    ) -> list[tuple[PastCase, float]]:
        """Returns up to top_k (PastCase, similarity_score) pairs, best first.
        Empty list if the corpus is empty or nothing clears min_similarity."""
        if not self._cases or self._vectorizer is None:
            return []

        query_text = self._case_text(merchant_category, merchant_name, stated_intent, amount)
        query_vec = self._vectorizer.transform([query_text])
        sims = cosine_similarity(query_vec, self._matrix)[0]

        ranked_idx = np.argsort(sims)[::-1][:top_k]
        results = [
            (self._cases[i], float(sims[i]))
            for i in ranked_idx
            if sims[i] >= min_similarity
        ]
        return results

    @staticmethod
    def format_for_prompt(results: list[tuple[PastCase, float]]) -> str:
        """Turns retrieved cases into a short, readable block for the LLM prompt."""
        if not results:
            return "No similar past cases found."
        lines = []
        for case, score in results:
            lines.append(
                f"- [{case.verdict}] {case.merchant_category}/{case.merchant_name}, "
                f"₹{case.amount:.2f} (similarity {score:.2f}): {case.reasons[0] if case.reasons else ''}"
            )
        return "\n".join(lines)
