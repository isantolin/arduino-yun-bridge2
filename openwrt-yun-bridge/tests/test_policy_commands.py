"""Property-based tests for shell command validation policy."""

from __future__ import annotations

import string

import pytest
from hypothesis import given, strategies as st
from hypothesis.strategies import SearchStrategy

from yunbridge.policy import CommandValidationError, tokenize_shell_command

SAFE_TOKEN_CHARS = string.ascii_letters + string.digits + "._-/:"
SAFE_TOKEN = st.text(SAFE_TOKEN_CHARS, min_size=1, max_size=16)
WHITESPACE = st.text(" \t", min_size=0, max_size=2)
FORBIDDEN_CHARS = ";&|><`"
FORBIDDEN_SUBSTRINGS = ("$(", "${", "&&", "||")


def forbidden_command_strategy() -> SearchStrategy[str]:
    base_text = st.text(SAFE_TOKEN_CHARS + " \t", min_size=0, max_size=24)
    char_injection = st.builds(
        lambda left, bad, right: f"{left}{bad}{right}",
        base_text,
        st.sampled_from(list(FORBIDDEN_CHARS)),
        base_text,
    )
    substring_injection = st.builds(
        lambda left, bad, right: f"{left}{bad}{right}",
        base_text,
        st.sampled_from(FORBIDDEN_SUBSTRINGS),
        base_text,
    )
    return st.one_of(char_injection, substring_injection)


@given(
    tokens=st.lists(SAFE_TOKEN, min_size=1, max_size=6),
    prefix=WHITESPACE,
    suffix=WHITESPACE,
)
def test_tokenizer_accepts_safe_tokens(
    tokens: list[str], prefix: str, suffix: str
) -> None:
    command = prefix + " ".join(tokens) + suffix
    assert tokenize_shell_command(command) == tuple(tokens)


@given(command=forbidden_command_strategy())
def test_tokenizer_rejects_forbidden_sequences(command: str) -> None:
    assert any(
        bad in command for bad in (*FORBIDDEN_SUBSTRINGS, *FORBIDDEN_CHARS)
    ), "Strategy must include forbidden sequence"
    with pytest.raises(CommandValidationError):
        tokenize_shell_command(command)
