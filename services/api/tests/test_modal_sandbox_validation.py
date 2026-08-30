from __future__ import annotations

import os

import pytest

# Deliberately NOT gated on credential presence alone (e.g. `~/.modal.toml` existing) -- on the
# project owner's own machine that file exists once `modal setup` has ever been run, which would
# make a plain `pytest` invocation silently make real, billable calls to a live Modal deployment
# by default. Final whole-branch review flagged this as a real risk (accidental repeated billing),
# not a hypothetical -- these two tests require an explicit, deliberate opt-in instead.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SONGBOX_MODAL_LIVE_TESTS"),
    reason=(
        "requires real Modal credentials AND explicit opt-in -- see M7c Task 4. "
        "Run as: SONGBOX_MODAL_LIVE_TESTS=1 pytest tests/test_modal_sandbox_validation.py"
    ),
)


def test_block_network_true_actually_blocks_a_real_outbound_call() -> None:
    """The one test in this milestone that can only be run against the real deployed sandbox --
    proves block_network=True genuinely blocks traffic, not merely that it was left unconfigured
    (which would look identical from the outside if egress happened to succeed by accident).

    This calls `blocked_egress_probe`, not one of the four real pipeline functions
    (run_separate/etc.) -- feeding one of them garbage bytes would prove nothing about network
    blocking, since a WAV-parsing failure happens the same way regardless of block_network's
    value (see app/modal_app.py's module docstring for exactly which of the four keep
    block_network=True today -- it's three of four, not all four, a real finding from this same
    validation pass, not the original design).
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
