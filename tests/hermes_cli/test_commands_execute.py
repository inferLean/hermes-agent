"""Invariant tests for registry-owned slash execution (CommandDef.execute).

Every ``CommandDef`` with ``execute`` set must:
  * name a key that exists in :data:`hermes_cli.slash_exec.EXECUTORS`, and
  * produce IDENTICAL core text across surfaces for a fixed context — the
    executor may only vary on ``args``/``options``, never on ``surface``.
"""

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from hermes_cli.slash_exec import (
    EXECUTORS,
    CommandContext,
    CommandReply,
    execute_command,
    resolve_executor,
    run_execute,
)

MIGRATED = [cmd for cmd in COMMAND_REGISTRY if cmd.execute]

SURFACES = ("cli", "gateway", "tui")


def test_some_commands_are_migrated():
    names = {cmd.name for cmd in MIGRATED}
    # The thin-slice set — extend as more commands migrate.
    assert {"version", "egress", "profile", "bundles", "help", "commands"} <= names


def test_gateway_help_translates_every_builtin_description_to_persian(monkeypatch):
    """Persian /help must not expose the registry's English descriptions."""
    from agent import i18n

    monkeypatch.setenv("HERMES_LANGUAGE", "fa")
    i18n.reset_language_cache()
    try:
        reply = execute_command("help", CommandContext(surface="gateway"))
    finally:
        i18n.reset_language_cache()

    assert "`/new [name]` -- آغاز یک نشست تازه" in reply.text
    assert "`/sessions` -- مرور و ادامه نشست‌های پیشین" in reply.text
    assert "Start a new session" not in reply.text
    assert "Browse and resume previous sessions" not in reply.text




def test_unmigrated_commands_have_no_executor():
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None









