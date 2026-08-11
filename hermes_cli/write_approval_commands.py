#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.
"""

from __future__ import annotations

import json
from typing import List, Optional

from agent.i18n import translate_or
from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    state = translate_or(
        "gateway.command_locale.write_approval.on" if on
        else "gateway.command_locale.write_approval.off",
        "on" if on else "off",
    )
    return f"{subsystem}.write_approval = {state}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return translate_or(
            "gateway.command_locale.write_approval.none",
            "No pending {subsystem} writes.", subsystem=subsystem,
        )
    lines = [translate_or(
        "gateway.command_locale.write_approval.header",
        "Pending {subsystem} writes ({count}):",
        subsystem=subsystem, count=len(records),
    )]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(translate_or(
        "gateway.command_locale.write_approval.actions",
        "Apply: {apply}   Reject: /{subsystem} reject <id>",
        apply=where, subsystem=subsystem,
    ))
    if subsystem == wa.SKILLS:
        lines.append(translate_or(
            "gateway.command_locale.write_approval.review_diff",
            "Review full diff: /skills diff <id>",
        ))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, translate_or(
            "gateway.command_locale.write_approval.resolve_usage",
            "Usage: /{subsystem} approve|reject <id>  (or 'all')",
            subsystem=subsystem,
        )
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} approve <id>"

    records = wa.list_pending(subsystem)
    if not records:
        return translate_or(
            "gateway.command_locale.write_approval.none",
            "No pending {subsystem} writes.", subsystem=subsystem,
        )

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return translate_or(
                "gateway.command_locale.write_approval.not_found",
                "No pending {subsystem} write with id '{target}'.",
                subsystem=subsystem, target=target,
            )
        targets = [rec]

    applied, failed = 0, []
    for rec in targets:
        ok, msg = _apply_one(subsystem, rec, memory_store)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [translate_or(
        "gateway.command_locale.write_approval.approved",
        "Approved {count} {subsystem} write(s).",
        count=applied, subsystem=subsystem,
    )]
    if failed:
        out.append(translate_or(
            "gateway.command_locale.write_approval.failed_header", "Failed:"
        ))
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", "")
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
            return bool(result.get("success")), result.get("error", "")
    except Exception as e:
        return False, str(e)


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all":
        n = 0
        for rec in wa.list_pending(subsystem):
            if wa.discard_pending(subsystem, rec["id"]):
                n += 1
        return translate_or(
            "gateway.command_locale.write_approval.rejected_many",
            "Rejected {count} pending {subsystem} write(s).",
            count=n, subsystem=subsystem,
        )
    if wa.discard_pending(subsystem, target):
        return translate_or(
            "gateway.command_locale.write_approval.rejected_one",
            "Rejected pending {subsystem} write '{target}'.",
            subsystem=subsystem, target=target,
        )
    return translate_or(
        "gateway.command_locale.write_approval.not_found",
        "No pending {subsystem} write with id '{target}'.",
        subsystem=subsystem, target=target,
    )


def _diff(rest: List[str]) -> str:
    if not rest:
        return translate_or(
            "gateway.command_locale.write_approval.diff_usage",
            "Usage: /skills diff <id>",
        )
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return translate_or(
            "gateway.command_locale.write_approval.skill_not_found",
            "No pending skill write with id '{target}'.", target=rest[0],
        )
    diff = wa.skill_pending_diff(rec)
    header = translate_or(
        "gateway.command_locale.write_approval.diff_header",
        "# Pending skill write {target}: {summary}\n",
        target=rec["id"], summary=rec.get("summary", ""),
    )
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return translate_or(
            "gateway.command_locale.write_approval.set_usage",
            "{state}\nSet with: /{subsystem} approval <on|off>",
            state=_fmt_state(subsystem), subsystem=subsystem,
        )
    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return translate_or(
            "gateway.command_locale.write_approval.invalid",
            "Invalid value '{value}'. Use: on or off.", value=arg,
        )
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return translate_or(
            "gateway.command_locale.write_approval.cli_set",
            "To change the {subsystem} approval gate, run:\n"
            "  hermes config set {subsystem}.write_approval {value}",
            subsystem=subsystem, value=val,
        )
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return translate_or(
            "gateway.command_locale.write_approval.set_failed",
            "Failed to set {subsystem}.write_approval: {error}",
            subsystem=subsystem, error=str(e),
        )
    state = translate_or(
        "gateway.command_locale.write_approval.on" if enabled
        else "gateway.command_locale.write_approval.off",
        "on" if enabled else "off",
    )
    return translate_or(
        "gateway.command_locale.write_approval.set",
        "{subsystem}.write_approval set to '{state}'.",
        subsystem=subsystem, state=state,
    )
