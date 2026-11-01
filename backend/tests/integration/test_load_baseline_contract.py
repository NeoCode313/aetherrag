"""The relative gate uses matched concurrency and still rejects exceeded budgets."""
from scripts.load_smoke import run_load


def test_matched_baseline_is_measured_and_an_exceeded_budget_still_fails():
    report = run_load(count=4, chat_p95_budget_ms=100000,
        admission_p95_budget_ms=100000, max_chat_degradation=1e-12)
    assert report["integrity_passed"]
    assert report["chat_post_load"]["count"] == 8
    assert report["chat_post_load_matched_concurrency"]["count"] == 64
    assert report["chat_concurrency"] == 4
    expected = round(report["chat_under_load"]["p95_ms"] /
                     report["chat_post_load_matched_concurrency"]["p95_ms"], 3)
    assert report["chat_p95_ratio_vs_matched_concurrency"] == expected
    assert not report["performance_checks"]["chat_degradation_within_budget"]
    assert not report["performance_passed"]
    assert not report["passed"]
