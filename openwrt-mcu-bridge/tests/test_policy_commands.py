"""Property-based tests for shell command validation policy."""

from __future__ import annotations

import string

import pytest

from mcubridge.policy import CommandValidationError, tokenize_shell_command

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
def test_tokenizer_accepts_shell_metacharacters_as_literals(command: str) -> None:
    """
    Verify that shell metacharacters are accepted as literal arguments.

    Since we use execve (no shell), these characters have no special meaning
    and should be passed through to the command.
    """
    tokens = tokenize_shell_command(command)
    assert len(tokens) > 0


def test_tokenizer_rejects_empty_command() -> None:
    with pytest.raises(CommandValidationError) as err:
        tokenize_shell_command("   ")
    assert "Empty" in err.value.message
