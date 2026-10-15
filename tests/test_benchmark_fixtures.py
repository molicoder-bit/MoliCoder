from local_model_chat.benchmarks.fixtures import load_benchmark_cases


def test_fixed_cases_cover_workloads_with_privacy_and_acceptance():
    cases = load_benchmark_cases()

    assert {case.category for case in cases} == {
        "chat", "retrieval", "code_review", "patch", "recovery", "architecture",
    }
    assert {case.privacy for case in cases} == {"public", "private", "restricted"}
    assert all(case.prompt and case.expected for case in cases)
    by_category = {case.category: case for case in cases}
    assert by_category["retrieval"].accepts("The path is src/parser.py.")
    assert not by_category["retrieval"].accepts("The path is src/other.py.")
    assert by_category["recovery"].accepts("Handle empty input, then add a test.")
    assert not by_category["recovery"].accepts("Handle empty input.")
