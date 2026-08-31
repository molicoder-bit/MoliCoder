from local_model_chat.molicoder.fingerprint import classify_progress, failure_fingerprint


def test_fingerprint_normalizes_line_numbers_paths_and_timings() -> None:
    first = failure_fingerprint("C:/repo/app.py:41: AssertionError after 0.12s")
    second = failure_fingerprint("D:/work/app.py:87: AssertionError after 2.94s")
    assert first == second


def test_progress_classifies_stuck_oscillation_and_movement() -> None:
    assert classify_progress(["A", "A", "A"]) == "stuck"
    assert classify_progress(["A", "B", "A", "B"]) == "oscillating"
    assert classify_progress(["A", "B", "C"]) == "progress"
    assert classify_progress(["A"]) == "insufficient"

