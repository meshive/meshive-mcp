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


def _section(label):
    return next(s for s in INSTRUCTIONS.split("\n\n") if s.startswith(label))


def test_untrusted_content_names_host_messages_inside_the_client_limit():
    section = _section("Untrusted content:")
    assert "`host_messages` returns what renters wrote" in section
    assert "`workspaces` host messages and maintenance notes come from the host" in section
    assert section in INSTRUCTIONS[:CLIENT_LIMIT]


def test_secrets_name_the_host_secrets_inside_the_client_limit():
    section = _section("Secrets:")
    assert "`machine_credentials`" in section and "`host_install_command`" in section
    assert "only on the machine the user named" in section and "emails the account owner" in section
    assert section in INSTRUCTIONS[:CLIENT_LIMIT]


def test_spending_covers_every_tool_with_confirm_inside_the_client_limit():
    """The section no longer lists the tools one by one, so a new tool with `confirm` is covered without editing it."""
    section = _section("Spending, changing and deleting:")
    assert "every tool with a `confirm`" in section
    assert "Never set confirm=true without the user's go-ahead" in section
    assert "set_watched_folders when it only removes or turns off folders" in section
    assert section in INSTRUCTIONS[:CLIENT_LIMIT]


def test_untrusted_content_names_the_activity_fields_a_client_sent():
    section = _section("Untrusted content:")
    assert "`activity` sources and ids are what a client sent" in section
    assert section in INSTRUCTIONS[:CLIENT_LIMIT]


@pytest.mark.parametrize("label", ("Prices are USD per hour", "Sizes:"))
def test_prices_and_sizes_end_inside_the_client_limit(label):
    section = _section(label)
    assert INSTRUCTIONS.index(section) + len(section) <= CLIENT_LIMIT


def test_gpu_memory_in_gb_is_inside_the_client_limit():
    """The one exception to "say GiB" sits near the end of the Sizes section, so it is among the first to fall off."""
    assert "Only GPU memory (`vram_gb`) is said in GB" in INSTRUCTIONS[:CLIENT_LIMIT]
