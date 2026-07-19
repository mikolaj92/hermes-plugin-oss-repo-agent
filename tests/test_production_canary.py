from repo_agent.production_canary import CANARY_MARKER


def test_canary_marker():
    assert CANARY_MARKER == "hermes-production-canary"
