import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import executor  # noqa: E402

UNPATCHED_HUMAN_APPROVAL_CHANNEL = executor.local_fixture.human_approval_channel

HUMAN_CHANNEL = {"baseUrl": "https://localhost:8139", "password": "private-admin-password", "cafile": "/run/tls.crt"}
APPROVED = {"result": "approved", "proposalId": "proposal-1", "httpStatus": 200, "status": "Succeeded",
            "approvalChannel": "console-interactive-password"}


@pytest.fixture(autouse=True)
def human_approver():
    """Unit tests never open a real HTTPS session: the human approval helper returns an approval."""
    with mock.patch.object(executor.local_fixture, "human_approval_channel", return_value=dict(HUMAN_CHANNEL)), \
            mock.patch.object(executor.human_approval, "approve_in_child", return_value=dict(APPROVED)) as approve:
        yield approve


@pytest.fixture
def unpatched_human_approval_channel():
    return UNPATCHED_HUMAN_APPROVAL_CHANNEL
