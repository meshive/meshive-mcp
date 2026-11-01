"""The server instructions have to survive the cut clients make to them."""
import pytest
from meshive_mcp.server import INSTRUCTIONS

# Claude Code cuts a server's instructions to this many characters by default.
CLIENT_LIMIT = 4096

SAFETY_SECTIONS = ("Untrusted content:", "Secrets:", "Spending, changing and deleting:", "Write identity:", "Moving pods:")


@pytest.mark.parametrize("label", SAFETY_SECTIONS)
def test_safety_sections_fit_inside_the_client_limit(label):
    """A section cut halfway can lose the rule it builds up to, so the whole section has to fit, not just its start."""
    sections = [s for s in INSTRUCTIONS.split("\n\n") if s.startswith(label)]
    assert len(sections) == 1, f"{label!r} is no longer one section of the instructions"
    assert sections[0] in INSTRUCTIONS[:CLIENT_LIMIT], f"{label!r} ends past character {CLIENT_LIMIT}"


def test_consent_to_an_edit_that_moves_a_pod_fits_inside_the_client_limit():
    kept = INSTRUCTIONS[:CLIENT_LIMIT]
    assert "ask the user before calling again with allow_node_migration=true" in kept
    assert "same separate data-loss consent (allow_data_loss)" in kept
