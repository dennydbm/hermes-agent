"""Help-only gateway lifecycle invocations must not request consent or run."""

import pytest

from tools.approval_detection import detect_dangerous_command


@pytest.mark.parametrize("command", [
    "/opt/hermes/.venv/bin/hermes gateway restart --help; /opt/hermes/.venv/bin/hermes gateway status",
    "hermes gateway stop --help",
    "hermes gateway restart -h",
])
def test_gateway_lifecycle_help_only_is_not_dangerous(command):
    assert detect_dangerous_command(command)[0] is False


@pytest.mark.parametrize("command", [
    "hermes gateway restart",
    "hermes gateway stop",
    "hermes gateway restart --helpful",
    "hermes gateway restart --help && hermes gateway restart",
    "hermes -p work gateway restart",
])
def test_real_gateway_lifecycle_still_requires_approval(command):
    assert detect_dangerous_command(command)[0] is True
