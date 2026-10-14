"""Correctness checks for lossless injection-runtime feature compaction."""

import importlib.util
from pathlib import Path

import torch


_RUNTIME = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "models"
    / "injection_engine"
    / "runtime"
    / "local_injection_hybrid_inference.py"
)


def _detector_cls():
    spec = importlib.util.spec_from_file_location("_inj_compact_runtime", _RUNTIME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.InjectionDetector


InjectionDetector = _detector_cls()


def test_compact_attention_hook_matches_per_sample_indexing():
    detector = object.__new__(InjectionDetector)
    query_indices = torch.tensor([[1, 2, 4, 4], [0, 3, 2, 1]])
    key_indices = torch.tensor([[0, 3, 1, 5], [4, 2, 2, 0]])
    detector._attention_pair_indices = (query_indices, key_indices)
    weights = torch.arange(2 * 3 * 6 * 6, dtype=torch.float32).reshape(2, 3, 6, 6)
    output = (torch.zeros((2, 6, 4)), weights, "unchanged")

    returned = detector._compact_attention_hook(None, (), output)

    expected = torch.stack(
        [
            weights[batch_index, :, query_indices[batch_index], key_indices[batch_index]]
            for batch_index in range(weights.shape[0])
        ]
    )
    assert returned[0] is output[0]
    assert returned[2] == "unchanged"
    torch.testing.assert_close(returned[1], expected, rtol=0, atol=0)


def test_compact_attention_hook_skips_when_inactive():
    detector = object.__new__(InjectionDetector)
    detector._attention_pair_indices = None
    output = (torch.zeros((1, 2, 3)), torch.zeros((1, 2, 3, 3)))

    assert detector._compact_attention_hook(None, (), output) is output


def test_compact_hidden_hook_matches_original_pooling_for_a_batch():
    detector = object.__new__(InjectionDetector)
    detector._hidden_captures = {}
    detector._hidden_capture_specs = [
        (torch.tensor([1, 2, 3, 4]), ((0, 1), (1, 3), (3, 4)), 5),
        (torch.tensor([0, 2, 4]), ((0, 1), (1, 2), (2, 3)), 4),
    ]
    hidden = torch.arange(2 * 6 * 4, dtype=torch.float32).reshape(2, 6, 4)

    hook = detector._make_compact_hidden_hook(8)
    returned = hook(None, (), hidden)

    assert returned is hidden
    expected_rows = []
    for batch_index, (tool_indices, bounds, last_index) in enumerate(
        detector._hidden_capture_specs
    ):
        tool_hidden = hidden[batch_index].index_select(0, tool_indices)
        pools = [tool_hidden[left:right].mean(0).float() for left, right in bounds]
        pools.append(hidden[batch_index, last_index].float())
        expected_rows.append(torch.stack(pools))
    expected = torch.stack(expected_rows)

    torch.testing.assert_close(detector._hidden_captures[8], expected, rtol=0, atol=0)


def test_compact_hidden_hook_preserves_tuple_output_and_skips_when_inactive():
    detector = object.__new__(InjectionDetector)
    detector._hidden_captures = {}
    detector._hidden_capture_specs = None
    hidden = torch.zeros((1, 3, 2))
    output = (hidden, "unchanged")

    returned = detector._make_compact_hidden_hook(15)(None, (), output)

    assert returned is output
    assert detector._hidden_captures == {}
