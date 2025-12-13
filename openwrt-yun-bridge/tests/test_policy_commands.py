"""Property-based tests for shell command validation policy."""

from __future__ import annotations

import string

import pytest

from yunbridge.policy import CommandValidationError, tokenize_shell_command

SAFE_TOKEN_CHARS = string.ascii_letters + string.digits + "._-/:"
FORBIDDEN_CHARS = ";&|><`"
FORBIDDEN_SUBSTRINGS = ("$(", "${", "&&", "||")


@pytest.mark.parametrize(
    "tokens,prefix,suffix",
    [
        (["ls", "-la"], "", ""),
        (["echo", "hello"], "  ", "  "),
        (["cat", "/tmp/file.txt"], "\t", ""),
        (["grep", "foo", "bar"], "", "\n"),
    ],
)
def test_tokenizer_accepts_safe_tokens(
    tokens: list[str], prefix: str, suffix: str
) -> None:
    command = prefix + " ".join(tokens) + suffix
    assert tokenize_shell_command(command) == tuple(tokens)


@pytest.mark.parametrize(
    "command",
    [
        "ls; rm -rf /",
        "echo $(whoami)",
        "cat file | grep secret",
        "command && other",
        "command || other",
        "echo `date`",
        "echo ${VAR}",
        "ls > file",
        "ls < file",
    ],
)
def test_tokenizer_rejects_forbidden_sequences(command: str) -> None:
    assert any(
        bad in command for bad in (*FORBIDDEN_SUBSTRINGS, *FORBIDDEN_CHARS)
    ), "Strategy must include forbidden sequence"
    with pytest.raises(CommandValidationError):
        tokenize_shell_command(command)
