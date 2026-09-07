import pytest
from mcp.server.mcpserver.exceptions import ToolError

from meshive_mcp.paging import decode_cursor, encode_cursor, paginate


def test_cursor_roundtrip():
    assert decode_cursor(None) == 0
    assert decode_cursor(encode_cursor(0)) == 0
    assert decode_cursor(encode_cursor(123)) == 123


@pytest.mark.parametrize("bad", ["???", "eD01", "bz0tMQ"])  # eD01 = "x=5", bz0tMQ = "o=-1"
def test_bad_cursor_is_tool_error(bad):
    with pytest.raises(ToolError) as exc:
        decode_cursor(bad)
    assert "invalid_argument" in str(exc.value)


def test_paginate_caps_and_chains():
    items = list(range(45))
    first = paginate(items, None, None)
    assert len(first["items"]) == 20 and first["total"] == 45 and first["next_cursor"]
    second = paginate(items, 20, first["next_cursor"])
    assert second["items"][0] == 20
    third = paginate(items, 20, second["next_cursor"])
    assert third["items"] == list(range(40, 45)) and third["next_cursor"] is None
    assert len(paginate(items, 1000, None)["items"]) == 45  # max_limit=100 > 45


def test_paginate_limit_validation():
    with pytest.raises(ToolError):
        paginate([1], 0, None)
