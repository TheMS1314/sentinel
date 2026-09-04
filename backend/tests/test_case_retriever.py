"""
Tests for the case-based retriever (RAG).
Run with: pytest backend/tests/test_case_retriever.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.rag.case_retriever import CaseRetriever, PastCase


def make_case(**overrides) -> PastCase:
    defaults = dict(
        request_id="req_1",
        text="grocery FreshMart buy groceries amount_low",
        verdict="APPROVE",
        reasons=["All deterministic policy checks passed."],
        merchant_category="grocery",
        merchant_name="FreshMart",
        amount=300.0,
    )
    defaults.update(overrides)
    return PastCase(**defaults)


class TestFormatForPrompt:
    def test_empty_results_returns_placeholder(self):
        assert CaseRetriever.format_for_prompt([]) == "No similar past cases found."

    def test_nonempty_results_include_merchant_name(self):
        """Regression test for a real bug found via load testing: this
        formatting function referenced case.merchant_name, but PastCase
        didn't store that field, causing an AttributeError the moment
        retrieval actually returned a non-empty result (which single-case
        tests never exercised)."""
        case = make_case()
        result = CaseRetriever.format_for_prompt([(case, 0.42)])
        assert "FreshMart" in result
        assert "grocery" in result
        assert "APPROVE" in result

    def test_multiple_results_all_render(self):
        cases = [
            (make_case(request_id="r1", merchant_name="FreshMart"), 0.9),
            (make_case(request_id="r2", merchant_name="DailyBasket", verdict="BLOCK"), 0.5),
        ]
        result = CaseRetriever.format_for_prompt(cases)
        assert "FreshMart" in result
        assert "DailyBasket" in result


class TestRetrieveSimilar:
    def test_retrieval_after_refresh_returns_cases_with_all_fields_populated(self):
        """End-to-end sanity check: refresh the index, retrieve, and
        confirm every returned PastCase has a usable merchant_name --
        not just that retrieval runs without error, but that the objects
        it returns are actually complete."""
        retriever = CaseRetriever()
        cases = [
            make_case(request_id="r1", merchant_name="FreshMart", text="grocery FreshMart restock kitchen"),
            make_case(request_id="r2", merchant_name="DailyBasket", text="grocery DailyBasket restock kitchen"),
        ]
        retriever.refresh(cases)
        results = retriever.retrieve_similar(
            merchant_category="grocery", merchant_name="QuickKirana",
            stated_intent="restock kitchen", amount=300.0, min_similarity=0.01,
        )
        assert len(results) > 0
        for case, score in results:
            assert case.merchant_name  # must not be empty/missing
            assert isinstance(score, float)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
