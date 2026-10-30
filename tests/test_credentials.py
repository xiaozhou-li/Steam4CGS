import pickle
import re
from pathlib import Path

import pytest
from fakes import FAKE_KEY

from crawler.credentials import ENV_VAR, ApiKey, InvalidCredentialError, MissingCredentialError, load_api_key

ROOT = Path(__file__).resolve().parent.parent


def test_load_from_environment_strips_whitespace():
    key = load_api_key({ENV_VAR: f"  {FAKE_KEY}\n"})
    assert key.reveal() == FAKE_KEY


def test_missing_key():
    with pytest.raises(MissingCredentialError):
        load_api_key({})
    assert load_api_key({ENV_VAR: "  "}, required=False) is None


@pytest.mark.parametrize("value", ["abc", FAKE_KEY[:-1], FAKE_KEY + "0", "G" * 32, FAKE_KEY[:16] + " " + FAKE_KEY[17:]])
def test_malformed_key_message_does_not_echo_value(value):
    with pytest.raises(InvalidCredentialError) as info:
        load_api_key({ENV_VAR: value})
    assert value.strip() not in str(info.value)


def test_key_is_masked_everywhere():
    key = ApiKey(FAKE_KEY)
    for text in (repr(key), str(key), f"{key}", f"{key!r}", repr([key]), repr({"k": key})):
        assert FAKE_KEY not in text
    with pytest.raises(TypeError):
        pickle.dumps(key)


def test_key_equality():
    assert ApiKey(FAKE_KEY) == ApiKey(FAKE_KEY) and ApiKey(FAKE_KEY) != ApiKey("CD" * 16)


def test_no_hardcoded_api_keys_in_project_sources():
    """Generated code, tests and docs must not contain a 32-hex key literal."""
    pattern = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
    paths = [*ROOT.glob("crawler/**/*.py"), *ROOT.glob("tests/**/*.py"), *ROOT.glob("docs/**/*.md"),
             ROOT / "pyproject.toml", ROOT / "README.md"]
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in paths if p.exists()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"possible API key literal at: {offenders}"


def test_legacy_script_is_never_imported():
    import sys

    import crawler.cli  # noqa: F401  (pull in the whole package)
    import crawler.preflight  # noqa: F401
    assert "steamreviewcrawler" not in sys.modules
