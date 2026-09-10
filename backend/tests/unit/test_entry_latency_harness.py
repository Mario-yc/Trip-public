"""Evidence attribution support tests; no model, network or database writes."""

import importlib.util
from pathlib import Path
import threading

import pytest

SPEC = importlib.util.spec_from_file_location(
    "entry_latency_harness", Path(__file__).resolve().parents[2] / "evals/run_entry_latency.py"
)
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def test_late_transport_keeps_its_original_case_after_next_case_begins():
    bindings = harness.ProviderCaseBindings()
    earlier, later = object(), object()
    released = threading.Event()
    result = []
    bindings.bind(earlier, "case-before-timeout")

    def late_worker():
        assert released.wait(2)
        result.append(bindings.case_for(earlier))

    worker = threading.Thread(target=late_worker)
    worker.start()
    try:
        bindings.bind(later, "case-after-timeout")
        released.set()
        worker.join(2)
        assert not worker.is_alive()
        assert result == ["case-before-timeout"]
        assert bindings.case_for(later) == "case-after-timeout"
    finally:
        released.set()
        worker.join(2)


def test_one_provider_cannot_silently_change_its_evidence_identity():
    bindings = harness.ProviderCaseBindings()
    provider = object()
    bindings.bind(provider, "first")
    bindings.bind(provider, "first")
    with pytest.raises(ValueError, match="reused_across_cases"):
        bindings.bind(provider, "second")
    assert bindings.case_for(provider) == "first"


def test_unregistered_transport_cannot_receive_a_guessed_case():
    with pytest.raises(KeyError):
        harness.ProviderCaseBindings().case_for(object())
