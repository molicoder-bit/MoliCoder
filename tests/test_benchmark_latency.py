import pytest

from local_model_chat.benchmarks.latency import LatencySample, compare_latency


def _sample(path, repeat, first, total, *, model="coder", context=4096):
    return LatencySample("patch", repeat, path, "host", model, context, first, total)


def test_paired_latency_reports_first_token_total_retries_and_delta_range():
    report = compare_latency((
        _sample("direct", 0, 1, 5),
        _sample("gateway", 0, 1.5, 7),
        _sample("direct", 1, 2, 6),
        _sample("gateway", 1, 2.5, 9),
    ))
    assert report.direct.median_first_token_seconds == 1.5
    assert report.gateway.median_total_seconds == 8
    assert report.median_paired_total_delta_seconds == 2.5
    assert report.paired_total_delta_range_seconds == (2, 3)


def test_latency_comparison_rejects_unmatched_or_incomparable_runs():
    with pytest.raises(ValueError, match="both routes"):
        compare_latency((_sample("direct", 0, 1, 5),))
    with pytest.raises(ValueError, match="context must match"):
        compare_latency((_sample("direct", 0, 1, 5),
                         _sample("gateway", 0, 1, 5, context=8192)))
    with pytest.raises(ValueError, match="duplicate"):
        compare_latency((_sample("direct", 0, 1, 5), _sample("direct", 0, 1, 5)))
