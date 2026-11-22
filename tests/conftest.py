"""
conftest.py — pytest fixtures for AWS Cost Analyser tests.

Patches boto3 at session level so that importing any analyser module does not
make real AWS API calls (all analysers call STS get_caller_identity at module load).
"""
import sys
import os
from unittest.mock import MagicMock, patch

# ── Ensure ROOT is in sys.path ────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── Patch boto3.Session before any analyser is imported ──────────────────────
# All analyser modules call boto3.Session().client("sts").get_caller_identity()
# at the top level. We intercept this to avoid real AWS network calls.

_mock_session = MagicMock()
_mock_sts = MagicMock()
_mock_sts.get_caller_identity.return_value = {"Account": "000000000000"}
_mock_session.return_value.client.return_value = _mock_sts

_boto3_patcher = patch("boto3.Session", _mock_session)
_boto3_patcher.start()

# Also patch the top-level boto3.client for modules that use it directly
_boto3_client_mock = MagicMock()
_boto3_client_mock.return_value = _mock_sts
patch("boto3.client", _boto3_client_mock).start()

# ── Provide consistent env vars ───────────────────────────────────────────────
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_SESSION_TOKEN", "test")
