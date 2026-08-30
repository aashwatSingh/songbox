from __future__ import annotations

import os

import pytest

_has_modal_credentials = os.environ.get("MODAL_TOKEN_ID") or os.path.exists(
    os.path.expanduser("~/.modal.toml")
)
pytestmark = pytest.mark.skipif(
    not _has_modal_credentials, reason="requires real Modal credentials -- see M7c Task 4"
)


def test_block_network_true_actually_blocks_a_real_outbound_call() -> None:
    """The one test in this milestone that can only be run against the real deployed sandbox --
    proves block_network=True genuinely blocks traffic, not merely that it was left unconfigured
    (which would look identical from the outside if egress happened to succeed by accident).

    This calls `blocked_egress_probe`, not one of the four real pipeline functions
    (run_separate/etc.) -- those never attempt a network call at all, even on bad input (that's
    the whole point of Decision 2's zero-egress design), so feeding them garbage bytes would prove
    nothing about network blocking; the failure would just be a WAV-parsing error either way.
    `blocked_egress_probe` runs the EXACT SAME urllib call as the sibling `egress_probe` function
    below, differing only in `block_network`, so its failure (or success) is real, direct evidence.
    """
    import modal

    blocked_fn = modal.Function.from_name("songbox-gpu", "blocked_egress_probe")
    with pytest.raises(Exception) as exc_info:  # noqa: B017 -- exact exception type is Modal's own
        blocked_fn.remote()
    # Confirm the failure is genuinely network-related, not some unrelated crash that happened to
    # also raise -- a real block manifests as a connection/network error surfaced through Modal's
    # remote-call exception wrapping.
    failure_text = str(exc_info.value).lower()
    assert any(
        keyword in failure_text
        for keyword in (
            "network",
            "connect",
            "resolution",  # e.g. glibc's "Temporary failure in name resolution" -- DNS blocked
            "resolve",
            "unreachable",
            "refused",
            "timed out",
            "gaierror",
        )
    ), f"expected a network-related failure, got: {exc_info.value!r}"


def test_egress_probe_confirms_networking_works_when_not_blocked() -> None:
    """Confirms the OTHER test's negative result means something: this sibling function has
    block_network=False and must successfully reach a real public endpoint. If this one also
    failed, that would mean Modal itself has no outbound networking available at all (a Modal
    platform issue, not evidence block_network=True is doing anything), making the blocked test's
    result meaningless.
    """
    import modal

    probe_fn = modal.Function.from_name("songbox-gpu", "egress_probe")
    result = probe_fn.remote()
    assert "reached example.com" in result
