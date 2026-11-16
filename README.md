"""CLI-backed turns — a vendor agent binary drives the session.

Every coding CLI the Agentic IDE registers (``jarvis.workspace.agents``)
has a planner here, so the chat's picker offers the same CLIs a pane does
(maintainer, 2026-08-27: the CLIs connected in the Agentic IDE, and each
one drawn as a chat, never as a terminal). Nine entries, seven wire shapes:

* **Claude-shaped NDJSON** — ``claude`` (Claude Code), ``grok`` (Grok
  Build, whose ``--output-format streaming-messages-json`` is the Anthropic
  Messages wire shape on purpose) and GLM Coding Plan, which IS Claude Code
  pointed at Z.ai's endpoint through the registry's environment factory.
  One translator turns their ``system`` / ``stream_event`` / ``assistant`` /
  ``user`` / ``result`` lines into agent-chat events.
* **Codex NDJSON** — ``codex exec --json`` with ``thread.*`` / ``turn.*`` /
  ``item.*`` lines.
* **Antigravity NDJSON** — ``agy --output-format stream-json`` with
  ``event: init | step_update | result`` lines (``step_update`` carries text
  deltas and tool steps keyed by ``step_index``; verified against agy 1.1.19).
* **OpenCode NDJSON** — ``opencode run --format json`` with ``step_start`` /
  ``text`` / ``reasoning`` / ``tool_use`` / ``step_finish`` / ``error``
  lines, each wrapping one ``part`` of the session record and naming the
  session on every line (verified against opencode 1.18.23).
* **Kimi NDJSON** — ``kimi -p --output-format stream-json`` writes chat
  messages by ``role``: an ``assistant`` line with its text and
  ``tool_calls`` (OpenAI's function-call shape, arguments as a JSON string)
  and a ``tool`` line per result. Nothing streams and nothing names the
  session, so the id is found afterwards in Kimi's own session store
  (``agentic_ide.agent_sessions``), the way a pane finds its own
  (verified against kimi-code 0.29.2).
* **Plain text** — ``dsh --profile headless`` (DeepSeek Harness) prints the
  final message and exits: no tool stream, no session, one answer.
* **Cursor stream-json** — ``agent -p --output-format stream-json`` with
  ``system`` / ``assistant`` / ``tool_call`` / ``result`` lines (Cursor's
  documented print-mode wire; session id on every line).

Every CLI that keeps a conversation resumes it natively (``claude --resume``,
``codex exec resume``, ``grok -r``, ``agy --conversation``, ``opencode run
--session``, ``kimi --session``, ``agent --resume``), so the session row keeps the vendor's id
in ``vendor_session`` and a later turn continues the same conversation — the
tools, skills, MCP servers and permissions are the CLI's own, exactly as in
a terminal. The person's permission mode maps onto the closest stance each
CLI offers. A print-mode CLI cannot ask back: Claude answers permission
prompts over stdin; Codex has ``--approve-for-me``. agy 1.1.26 instead
soft-denies the tool and **exits**, so the chat shows ``run_command - failed``
and goes idle with no answer — ``accept-edits`` therefore also passes
``--dangerously-skip-permissions`` (the same flag the mission worker has
used since 2026-06). Jarvis tools reached over MCP still go through
:class:`~jarvis.core.protocols.SupervisorToolGateway`.

Spawning follows the mission workers: shell-free argv, the prompt on stdin
where the binary accepts it, ``NO_WINDOW_CREATIONFLAGS``, UTF-8 decoding,
and the agent-account environment (``jarvis.agent_accounts.spawn_env``) so
the active subscription seat is the one that answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from jarvis.agent_chat import jarvis_harness
from jarvis.agent_chat.approval_bridge import approval_ref
from jarvis.agent_chat.effort import normalize_effort, snap_to_ladder
from jarvis.agent_chat.events import make_event
from jarvis.agent_chat.permissions import normalize_permission
from jarvis.agent_chat.runner_api import TurnHandle
from jarvis.agent_chat.tool_context import register_turn, unregister_turn
from jarvis.core.process_utils import NO_WINDOW_CREATIONFLAGS

log = logging.getLogger(__name__)

_READLINE_LIMIT: Final[int] = 16 * 1024 * 1024
_TURN_TIMEOUT_S: Final[float] = 3600.0


class CliUnavailable(RuntimeError):
    """The binary is not installed (or not on PATH) — the UI says so."""


# ------------------------------------------------------------ binaries


#: The binaries behind each CLI runner, in the order tried. One table for the
#: planners below, the catalog route's "installed" flag and the workspace
#: panes' launch picks — three readers of one fact, so a CLI added here is
#: offerable everywhere at once.
CLI_BINARIES: Final[dict[str, tuple[str, ...]]] = {
    "claude-cli": ("claude", "claude.cmd", "claude.exe"),
    "codex-cli": ("codex", "codex.cmd", "codex.exe"),
    "agy-cli": ("agy", "agy.exe"),
    "grok-cli": ("grok", "grok.exe", "grok.cmd"),
    "opencode-cli": ("opencode", "opencode.cmd", "opencode.exe"),
    "kimi-cli": ("kimi", "kimi.cmd", "kimi.exe"),
    # GLM Coding Plan IS Claude Code — Z.ai ships no binary of its own
    # (``jarvis.workspace.agents``), so its seat stands on Claude Code's.
    "glm-cli": ("claude", "claude.cmd", "claude.exe"),
    "dsh-cli": ("dsh", "dsh.cmd", "dsh.exe"),
    # Documented command is ``agent``; the Windows installer also drops
    # ``cursor-agent`` so the generic name does not collide with another
    # product. Prefer the unambiguous name when both are present.
    "cursor-cli": (
        "cursor-agent",
        "cursor-agent.exe",
        "agent",
        "agent.exe",
        "agent.cmd",
    ),
}


def _which(*names: str) -> str | None:
    # The well-known install dirs a GUI-launched process does not inherit
    # (``~/.local/bin``, ``~/.grok/bin``, npm's prefix) — best-effort, and the
    # same augmentation a pane runs before it resolves its CLI.
    try:
        from jarvis.core.path_augment import ensure_cli_paths

        ensure_cli_paths()
    except Exception:  # noqa: BLE001, S110 — PATH augmentation is best-effort
        pass
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def cli_installed(runner: str) -> bool:
    """Whether ``runner``'s binary resolves from here (False for an unknown runner)."""
    if runner == "cursor-cli":
        from jarvis.workspace.cursor_cli import resolve_cursor_binary

        return resolve_cursor_binary() is not None
    return _which(*CLI_BINARIES.get(runner, ())) is not None


def claude_argv_prefix() -> list[str]:
    binary = _which(*CLI_BINARIES["claude-cli"])
    if not binary:
        raise CliUnavailable("Claude Code (claude) is not installed or not on PATH.")
    from jarvis.claude_auth import claude_cli_argv_prefix

    return claude_cli_argv_prefix(binary)


def codex_argv_prefix() -> list[str]:
    try:
        from jarvis.missions.workers.codex_direct_worker import _resolve_codex_argv_prefix

        prefix = _resolve_codex_argv_prefix()
    except Exception:  # noqa: BLE001 — the worker helper is a convenience, not a contract
        prefix = []
    if prefix:
        return list(prefix)
    binary = _which(*CLI_BINARIES["codex-cli"])
    if not binary:
        raise CliUnavailable("Codex CLI (codex) is not installed or not on PATH.")
    return [binary]


def agy_argv_prefix() -> list[str]:
    try:
        from jarvis.google_cli.resolver import resolve_google_cli

        cli = resolve_google_cli()
    except Exception:  # noqa: BLE001 — fall back to PATH below
        cli = None
    if cli is not None and cli.kind == "agy":
        return list(cli.argv_prefix)
    binary = _which(*CLI_BINARIES["agy-cli"])
    if not binary:
        raise CliUnavailable("Antigravity CLI (agy) is not installed or not on PATH.")
    return [binary]


def grok_argv_prefix() -> list[str]:
    binary = _which(*CLI_BINARIES["grok-cli"])
    if not binary:
        raise CliUnavailable("Grok Build (grok) is not installed or not on PATH.")
    return [binary]


def opencode_argv_prefix() -> list[str]:
    binary = _which(*CLI_BINARIES["opencode-cli"])
    if not binary:
        raise CliUnavailable("OpenCode (opencode) is not installed or not on PATH.")
    return [binary]


def kimi_argv_prefix() -> list[str]:
    binary = _which(*CLI_BINARIES["kimi-cli"])
    if not binary:
        raise CliUnavailable("Kimi Code (kimi) is not installed or not on PATH.")
    return [binary]


def dsh_argv_prefix() -> list[str]:
    binary = _which(*CLI_BINARIES["dsh-cli"])
    if not binary:
        raise CliUnavailable("DeepSeek Harness (dsh) is not installed or not on PATH.")
    return [binary]


def cursor_argv_prefix() -> list[str]:
    from jarvis.workspace.cursor_cli import resolve_cursor_binary

    binary = resolve_cursor_binary()
    if not binary:
        raise CliUnavailable("Cursor CLI (agent) is not installed or not on PATH.")
    return [binary]


#: The subscription seat the CURRENT turn runs on, when its session names one
#: (an agent-society member pinned to a specific account). Empty = the
#: platform's active account, exactly as before the society existed.
ACCOUNT_OVERRIDE: ContextVar[str] = ContextVar("agent_chat.account_override", default="")


def _account_env(platform: str) -> dict[str, str]:
    """The child environment for the subscription seat of ``platform`` — the
    turn's pinned account when its session names one, else the active one."""
    try:
        from jarvis import agent_accounts

        pinned = agent_accounts.resolve(ACCOUNT_OVERRIDE.get() or None)
        if pinned is not None and pinned.platform == platform:
            account = pinned
        else:
            account = agent_accounts.active_account(platform)  # type: ignore[arg-type]
        env = agent_accounts.spawn_env(platform, account.id, base=os.environ)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — no account layer for this platform → plain env
        env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _registry_env(agent: str, base: dict[str, str]) -> dict[str, str]:
    """``base`` with the Agentic IDE registry's per-CLI environment on top.

    The same overlay a pane of ``agent`` gets (``jarvis.agentic_ide.session.
    agent_spawn_overlay``): the entry's fixed variables — an auto-updater
    switched off so the binary is not swapped under a running turn — and, for
    an entry whose environment is user configuration, its factory's answer.
    A factory that answers ``None`` says the CLI is not configured (GLM
    without a Z.ai key), and that stops the turn here rather than letting
    Claude Code answer from — and bill — the wrong vendor. An empty value
    removes the variable from the child, as it does for a pane.
    """
    env = dict(base)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        from jarvis.workspace import agents as workspace_agents

        spec = workspace_agents.get_agent(agent)
    except Exception:  # noqa: BLE001 — no registry, no overlay
        spec = None
    if spec is None:
        return env
    overlay = dict(spec.spawn_env)
    if spec.spawn_env_factory is not None:
        resolved = spec.spawn_env_factory()
        if resolved is None:
            raise CliUnavailable(
                f"{spec.display_name} is not configured yet — add its API key on "
                "the API Keys page, then send again."
            )
        overlay.update(resolved)
    for key, value in overlay.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env


# ------------------------------------------------------------ argv per CLI

#: Prepended to the prompt for a runner without a plan mode of its own: the
#: sandbox already forbids changes; this tells the model what to do instead.
_PLAN_PREAMBLE: Final[str] = (
    "PLAN MODE: do not change any files or run commands that write. Investigate "
    "(read, search, list), then answer with a concrete, numbered plan of the "
    "changes you would make — files, functions, the order — and the open "
    "questions. The person will switch to Build mode to have it carried out.\n\n"
)


@dataclass(slots=True)
class CliPlan:
    argv: list[str]
    env: dict[str, str]
    stdin_text: str | None
    shape: str  # a key of ``_SHAPES``: claude | codex | agy | opencode | kimi | text | cursor
    #: Vendor session id we chose up front (claude/grok --session-id), or None
    #: when the CLI assigns one (agy, codex) and we read it from the stream.
    vendor_session: str | None
    #: Keep stdin open after the prompt: the CLI talks back on it (Claude
    #: Code's control protocol — permission prompts answered from the chat).
    #: The pump closes stdin once the turn's ``result`` line arrived.
    keep_stdin: bool = False
    #: The control-protocol handshake, written before the prompt. Claude Code
    #: only asks about tool calls once a client has announced itself this way.
    control_init: str | None = None
    #: For a CLI whose stream never names its session: called once the turn
    #: is over with the working folder and the wall-clock start, and answers
    #: the id the CLI's own store gave the conversation (or None).
    discover: Callable[[Path, float], str | None] | None = None


def claude_control_init() -> str:
    """The handshake that turns Claude Code's control protocol on.

    Without it, ``--permission-prompt-tool stdio`` is a flag the CLI accepts
    and never uses: it has no client on the other end that it knows can answer,
    so every tool call runs straight through and "Ask before acting" behaves
    exactly like "Auto-accept edits" (maintainer report 2026-08-24). The CLI
    answers this with a ``control_response`` listing its commands; from then on
    a permission prompt arrives as ``control_request {subtype: can_use_tool}``
    and the chat's approval card answers it on stdin.

    ``hooks: null`` says this client registers none of its own — the CLI's
    configured hooks still run.
    """
    return (
        json.dumps(
            {
                "type": "control_request",
                "request_id": "jarvis-init",
                "request": {"subtype": "initialize", "hooks": None},
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def claude_stream_input(prompt: str) -> str:
    """One NDJSON user message for ``--input-format stream-json``."""
    return (
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": prompt}},
            ensure_ascii=False,
        )
        + "\n"
    )


def plan_claude(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    return _plan_claude_code(
        prompt=prompt,
        cwd=cwd,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        resume=resume,
        identity=identity,
        env=jarvis_harness.apply_env(_account_env("claude")),
    )


def plan_glm(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    """GLM Coding Plan: Claude Code's own plan, pointed at Z.ai by the registry.

    Z.ai ships no CLI; its documentation says to run Anthropic's binary
    against its endpoint, so everything — the control protocol that answers
    permission prompts from the chat, ``--resume``, the MCP tools — is
    inherited rather than re-implemented. Two things differ: the environment
    is ``glm_spawn_env``'s (and its ``None`` refuses the turn instead of
    letting Claude Code answer from, and bill, the wrong vendor), and no
    ``--effort`` is sent, because that flag belongs to Anthropic's endpoint
    (``_GLM_PICKS`` in ``jarvis.workspace.agents``).
    """
    return _plan_claude_code(
        prompt=prompt,
        cwd=cwd,
        model=model,
        effort="",
        permission_mode=permission_mode,
        resume=resume,
        identity=identity,
        env=jarvis_harness.apply_env(_registry_env("glm", dict(os.environ))),
    )


def _plan_claude_code(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None,
    env: dict[str, str],
) -> CliPlan:
    mode = normalize_permission("claude-cli", permission_mode)
    argv = [
        *claude_argv_prefix(),
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        # The control protocol: the user message goes in as NDJSON and a
        # permission prompt comes back as a ``control_request`` line that the
        # chat answers on stdin — so "ask" really asks, here, in the chat.
        "--input-format",
        "stream-json",
        "--permission-prompt-tool",
        "stdio",
        "--permission-mode",
        mode,
    ]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    # Jarvis' own tools, and the identity that says when to use them. Both are
    # skipped when the app cannot offer them (no control key, server not bound
    # yet) — the session then behaves exactly as it did before. On the Jarvis
    # surface the identity is Jarvis' whole head, in a FILE: argv cannot carry
    # it on Windows (32 767 characters for the whole command line).
    mcp_config = jarvis_harness.mcp_config_json(identity.session_id if identity else None)
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
        if identity is not None and identity.path is not None:
            argv += ["--append-system-prompt-file", str(identity.path)]
        else:
            argv += ["--append-system-prompt", jarvis_harness.SYSTEM_PREAMBLE]
    if resume:
        argv += ["--resume", resume]
        sid = resume
    else:
        sid = str(uuid.uuid4())
        argv += ["--session-id", sid]
    return CliPlan(
        argv,
        env,
        claude_stream_input(prompt),
        "claude",
        sid,
        keep_stdin=True,
        control_init=claude_control_init(),
    )


def plan_grok(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    # Grok Build takes the prompt on argv, so the identity rides in front of
    # it — the compact cut, and only on a fresh conversation.
    prompt = _with_identity(prompt, identity, resume, compact=True)
    argv = [
        *grok_argv_prefix(),
        "--no-auto-update",
        "--no-alt-screen",
        "--output-format",
        "streaming-messages-json",
        "--include-partial-messages",
        "--permission-mode",
        normalize_permission("grok-cli", permission_mode),
        "--cwd",
        str(cwd),
    ]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["--reasoning-effort", effort]
    if resume:
        argv += ["-r", resume]
        sid = resume
    else:
        sid = str(uuid.uuid4())
        argv += ["--session-id", sid]
    argv += ["-p", prompt]
    return CliPlan(argv, _account_env("grok-build"), None, "claude", sid)


#: agy's own model ids as of 1.1.19, for a box where ``agy models`` cannot be
#: read (the live list is account-dependent and wins whenever it answers).
AGY_FALLBACK_MODELS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("gemini-3.7-flash", "Gemini 3.7 Flash", ("low", "medium", "high")),
    ("gemini-3.6-flash", "Gemini 3.6 Flash", ("low", "medium", "high")),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", ("low", "medium", "high")),
    ("gemini-3.1-pro", "Gemini 3.1 Pro", ("low", "high")),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", ()),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", ()),
    ("gpt-oss-120b", "GPT-OSS 120B", ("medium",)),
)

_AGY_EFFORT_SUFFIXES: Final[tuple[str, ...]] = ("low", "medium", "high")


def agy_model_catalog(raw_models: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Fold agy's suffixed ids into base models with an effort ladder each.

    ``agy models`` lists ``gemini-3.5-flash-low`` / ``-medium`` / ``-high``
    as three ids; the composer shows ONE model with a three-step effort
    pick, so this folds ``<base>-<effort>`` onto ``<base>`` and records the
    efforts seen. Ids without a suffix (the Claude models, ``gpt-oss-120b``)
    keep an empty ladder = no effort pick. ``None`` (no list readable) gives
    the fallback table.
    """
    if not raw_models:
        return [
            {"id": mid, "label": label, "efforts": list(efforts)}
            for mid, label, efforts in AGY_FALLBACK_MODELS
        ]
    order: list[str] = []
    efforts: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for m in raw_models:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        label = str(m.get("label") or mid)
        base, suffix = mid, ""
        for sfx in _AGY_EFFORT_SUFFIXES:
            if mid.endswith("-" + sfx):
                base, suffix = mid[: -(len(sfx) + 1)], sfx
                break
        if base not in efforts:
            efforts[base] = []
            order.append(base)
            labels[base] = _strip_effort_label(label) if suffix else label
        if suffix and suffix not in efforts[base]:
            efforts[base].append(suffix)
    return [
        {
            "id": base,
            "label": labels[base],
            "efforts": sorted(efforts[base], key=_AGY_EFFORT_SUFFIXES.index),
        }
        for base in order
    ]


def _strip_effort_label(label: str) -> str:
    # "Gemini 3.5 Flash (High)" -> "Gemini 3.5 Flash"
    for sfx in ("(Low)", "(Medium)", "(High)"):
        if label.endswith(sfx):
            return label[: -len(sfx)].rstrip()
    return label


def agy_model_args(
    model: str, effort: str, catalog: list[dict[str, Any]] | None = None
) -> list[str]:
    """The ``--model`` / ``--effort`` pair agy accepts for a base id + effort.

    agy is strict: a base Gemini id REQUIRES ``--effort`` (and only the levels
    that model has — Pro knows low/high), a suffixed id FORBIDS it, and the
    Claude / GPT-OSS ids take none at all. The catalog says which is which;
    without one the fallback table does.
    """
    rows = catalog or agy_model_catalog(None)
    by_id = {r["id"]: r for r in rows}
    if not model:
        # Default model: agy accepts ``--effort`` alone.
        return ["--effort", effort] if effort in _AGY_EFFORT_SUFFIXES else []
    row = by_id.get(model)
    if row is None:
        # A suffixed or unknown id: pass it through untouched.
        return ["--model", model]
    ladder = list(row.get("efforts") or [])
    if not ladder:
        return ["--model", model]
    if ladder == ["medium"] and model.startswith("gpt-oss"):
        # gpt-oss-120b: the bare id runs; ``-medium`` is the only suffix.
        return ["--model", model]
    level = effort if effort in ladder else _nearest_lower(effort, ladder)
    return ["--model", model, "--effort", level]


def _nearest_lower(effort: str, ladder: list[str]) -> str:
    order = list(_AGY_EFFORT_SUFFIXES)
    if effort not in order:
        return ladder[-1] if "high" in ladder else ladder[0]
    idx = order.index(effort)
    lower = [lvl for lvl in ladder if order.index(lvl) <= idx]
    return lower[-1] if lower else ladder[0]


def _resolved_cwd(raw: Path | str | None) -> Path:
    """Absolute working folder for a CLI spawn.

    agy refuses a relative ``--add-dir`` (``path is not absolute``) and a
    society session used to store ``data\\society\\<id>\\workspace`` relative
    to the app cwd. Resolve here so every planner sees the same folder.
    """
    path = Path(raw or Path.home()).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def plan_agy(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    mode = normalize_permission("agy-cli", permission_mode)
    # agy has no system-prompt flag: the identity rides in front of the prompt
    # on stdin, on a fresh conversation (a resumed one already knows).
    prompt = _with_identity(prompt, identity, resume)
    work = _resolved_cwd(cwd)
    if identity is not None:
        # agy has no --mcp-config; a workspace plugin is how print-mode
        # actually sees Jarvis' tools (gmail, linear, the rest of the gateway).
        jarvis_harness.install_agy_jarvis_plugin(work, identity.session_id)
    argv = [
        *agy_argv_prefix(),
        "--output-format",
        "stream-json",
        "--add-dir",
        str(work),
        # agy's own print timeout is 5 min; ours is the long one.
        "--print-timeout",
        f"{int(_TURN_TIMEOUT_S // 60)}m",
    ]
    if mode == "plan":
        argv += ["--mode", "plan"]
    else:
        # Print-mode agy 1.1.26: a tool that still wants a confirmation is
        # soft-denied and the process exits. ``accept-edits`` still asks for
        # RunCommand, so without this flag the turn dies after the first
        # shell call (live 2026-09-04, society:test / society:gamil-agent).
        argv += ["--dangerously-skip-permissions"]
        if mode == "accept-edits":
            argv += ["--mode", "accept-edits"]
    argv += agy_model_args(model, effort, _agy_catalog_cached())
    if resume:
        argv += ["--conversation", resume]
    env = jarvis_harness.apply_env(_account_env("antigravity"))
    env.setdefault("AGY_CLI_HIDE_LOGO", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Prompt on stdin: no argv length limit, and no ``-p`` that could swallow
    # the next flag as its value.
    return CliPlan(argv, env, prompt, "agy", resume)


def read_codex_models() -> list[dict[str, Any]] | None:
    """Codex's account catalog from ``$CODEX_HOME/models_cache.json``.

    The CLI refreshes that file itself (TTL 5 min, ETag) on every run; it is
    the same list the Codex TUI's model picker shows for this login. Rows:
    ``{id, label, efforts, note}`` for ``visibility: list`` models, ordered
    by the catalog's ``priority``. ``None`` when the file is missing or
    unreadable (the caller falls back to the bundled list).
    """
    env = _account_env("codex")
    home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    path = home / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("agent chat: codex models cache unreadable at %s: %s", path, exc)
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    rows: list[tuple[int, dict[str, Any]]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        if str(m.get("visibility") or "list") != "list":
            continue
        slug = str(m.get("slug") or m.get("id") or "").strip()
        if not slug:
            continue
        levels = m.get("supported_reasoning_levels") or []
        efforts = [
            str(lvl.get("effort") if isinstance(lvl, dict) else lvl)
            for lvl in levels
            if (lvl.get("effort") if isinstance(lvl, dict) else lvl)
        ]
        note = ""
        upgrade = m.get("upgrade")
        if isinstance(upgrade, dict) and upgrade.get("retirement_at"):
            note = f"retires {str(upgrade['retirement_at'])[:10]}"
        try:
            prio = int(m.get("priority") or 0)
        except (TypeError, ValueError):
            prio = 0
        rows.append(
            (
                prio,
                {
                    "id": slug,
                    "label": str(m.get("display_name") or slug),
                    "efforts": efforts,
                    "note": note,
                },
            )
        )
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows] or None


_AGY_CATALOG: dict[str, Any] = {"at": 0.0, "rows": None}
_AGY_CATALOG_TTL_S: Final[float] = 600.0


def _agy_catalog_cached() -> list[dict[str, Any]] | None:
    rows = _AGY_CATALOG.get("rows")
    return rows if isinstance(rows, list) else None


def read_agy_models(timeout_s: float = 8.0) -> list[dict[str, Any]]:
    """``agy --output-format json models`` -> the folded catalog (cached 10 min).

    Blocking (a ~2 s subprocess) — call it off the event loop. Any failure
    yields the fallback table; the cache keeps a failure out of the next
    request for the TTL too.
    """
    import subprocess

    now = time.monotonic()
    if _AGY_CATALOG["rows"] is not None and now - _AGY_CATALOG["at"] < _AGY_CATALOG_TTL_S:
        return list(_AGY_CATALOG["rows"])
    raw: list[dict[str, Any]] | None = None
    try:
        argv = [*agy_argv_prefix(), "--output-format", "json", "models"]
        env = dict(os.environ)
        env.setdefault("AGY_CLI_HIDE_LOGO", "1")
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            timeout=timeout_s,
            env=env,
            creationflags=NO_WINDOW_CREATIONFLAGS,
        )
        text = proc.stdout.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            obj = json.loads(line)
            data = ((obj.get("command") or {}).get("data") or {}) if isinstance(obj, dict) else {}
            models = data.get("models") if isinstance(data, dict) else None
            if isinstance(models, list):
                raw = [m for m in models if isinstance(m, dict)]
                break
    except (CliUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("agent chat: agy models unavailable: %s", exc)
    rows = agy_model_catalog(raw)
    _AGY_CATALOG["rows"] = rows
    _AGY_CATALOG["at"] = now
    return list(rows)


def plan_codex(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    mode = normalize_permission("codex-cli", permission_mode)
    # Codex reads the prompt from stdin, which has no length limit: the
    # identity rides in front of it on a fresh conversation. (Its
    # ``model_instructions_file`` REPLACES the CLI's own instructions rather
    # than adding to them, so it is not used here.)
    prompt = _with_identity(prompt, identity, resume)
    base = codex_argv_prefix()
    if resume:
        argv = [*base, "exec", "resume", resume, "--json"]
    else:
        argv = [*base, "exec", "--json", "--cd", str(cwd)]
    argv += ["--skip-git-repo-check"]
    # Coding-agent turns are task-contained. Do not inherit ambient plugins or
    # computer-use tools that could expand a failed repository task into
    # Terminal/Cursor/browser automation.
    argv += ["--ignore-user-config"]
    # Jarvis' own tools over streamable-HTTP MCP, mounted for this run only —
    # the person's ~/.codex/config.toml is never touched.
    argv += jarvis_harness.codex_config_args(identity.session_id if identity else None)
    # The TUI's presets, spelled out for ``exec`` (codex 0.149): Read only /
    # Auto (workspace-write) / Full access (``--yolo``), plus "approve for
    # me" — Codex's own reviewer model decides what would have asked you,
    # the one ask-stance a headless run has. ``exec`` never prompts on
    # stdin; the sandbox decides what may happen. Plan is the read-only
    # sandbox plus an instruction to plan instead of act. The sandbox goes
    # through ``-c sandbox_mode`` because ``exec resume`` has no ``-s``.
    if mode == "full-access":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    elif mode == "approve-for-me":
        argv += [
            "-c",
            'sandbox_mode="workspace-write"',
            "-c",
            'approvals_reviewer="auto_review"',
            "-c",
            'approval_policy="on-request"',
        ]
    else:
        sandbox = "workspace-write" if mode == "auto" else "read-only"
        argv += ["-c", f'sandbox_mode="{sandbox}"']
    if model:
        # The account's catalog says which levels THIS model takes (5.5 stops
        # at xhigh, terra goes to ultra); an unsupported level is snapped
        # rather than sent for the server to reject.
        for row in read_codex_models() or []:
            if row.get("id") == model and row.get("efforts"):
                effort = snap_to_ladder(effort, list(row["efforts"]))
                break
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    # Without a summary setting no ``reasoning`` items appear at all.
    argv += ["-c", "model_reasoning_summary=auto"]
    if model:
        argv += ["--model", model]
    argv += ["-"]  # prompt on stdin
    text = _PLAN_PREAMBLE + prompt if mode == "plan" else prompt
    env = jarvis_harness.apply_env(_account_env("codex"))
    from jarvis.missions.workers.codex_direct_worker import _codex_env_with_execution_host

    env = _codex_env_with_execution_host(env, base)
    return CliPlan(argv, env, text, "codex", resume)


_OPENCODE_CATALOG: dict[str, Any] = {"at": 0.0, "rows": None}
_OPENCODE_CATALOG_TTL_S: Final[float] = 600.0


def read_opencode_models(timeout_s: float = 20.0) -> list[dict[str, Any]]:
    """``opencode models`` -> the ids this install can run (cached 10 min).

    OpenCode's ids are ``provider/model`` and the list is whatever providers
    the person configured — nothing to curate, only to ask. The label is the
    model half and the note the provider, so ``anthropic/claude-opus-5`` and
    ``cloudflare-ai-gateway/anthropic/claude-opus-5`` read apart in the picker.
    Blocking (a ~2 s subprocess) — call it off the event loop. Any failure
    yields an empty list, and the cache keeps a failure out of the next
    request for the TTL too.
    """
    import subprocess

    now = time.monotonic()
    if (
        _OPENCODE_CATALOG["rows"] is not None
        and now - _OPENCODE_CATALOG["at"] < _OPENCODE_CATALOG_TTL_S
    ):
        return list(_OPENCODE_CATALOG["rows"])
    rows: list[dict[str, Any]] = []
    try:
        argv = [*opencode_argv_prefix(), "models"]
        env = _registry_env("opencode", dict(os.environ))
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            timeout=timeout_s,
            env=env,
            creationflags=NO_WINDOW_CREATIONFLAGS,
        )
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            mid = line.strip()
            if not mid or "/" not in mid or " " in mid:
                continue
            provider, _, model = mid.partition("/")
            rows.append({"id": mid, "label": model, "note": provider})
    except (CliUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("agent chat: opencode models unavailable: %s", exc)
    _OPENCODE_CATALOG["rows"] = rows
    _OPENCODE_CATALOG["at"] = now
    return list(rows)


def plan_opencode(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    mode = normalize_permission("opencode-cli", permission_mode)
    # The prompt is a positional of ``run`` (no stdin mode), behind ``--`` so
    # a sentence that starts with a dash is a sentence and not a flag; the
    # identity rides in front of it on a fresh conversation.
    prompt = _with_identity(prompt, identity, resume)
    argv = [*opencode_argv_prefix(), "run", "--format", "json", "--thinking"]
    if mode == "auto":
        argv += ["--auto"]
    elif mode == "plan":
        # OpenCode's own plan agent: read-only by its definition.
        argv += ["--agent", "plan"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--variant", effort]
    if resume:
        argv += ["--session", resume]
    argv += ["--", prompt]
    env = _registry_env("opencode", _account_env("opencode"))
    return CliPlan(argv, env, None, "opencode", resume)


def _kimi_session_after(cwd: Path, started_at: float) -> str | None:
    """The session Kimi's print run in ``cwd`` created after ``started_at``.

    Kimi's stream names no session; its store does. The same search a pane
    runs for its own conversation — the newest session under this folder's
    bucket, created after the start, with content — through
    ``agent_sessions.discover``, so the two never disagree about where Kimi
    keeps a conversation.
    """
    try:
        from jarvis.agentic_ide import agent_sessions

        handle = agent_sessions.discover("kimi", str(cwd), started_at)
    except Exception as exc:  # noqa: BLE001 — a lost id costs one resume, not the turn
        log.debug("agent chat: kimi session discovery failed: %s", exc)
        return None
    return handle.id if handle is not None else None


def plan_kimi(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    mode = normalize_permission("kimi-cli", permission_mode)
    prompt = _with_identity(prompt, identity, resume)
    argv = [*kimi_argv_prefix(), "--output-format", "stream-json"]
    if model:
        argv += ["--model", model]
    # Print mode is autonomous by Kimi's own design (a headless run has nobody
    # to ask); the flag only says so out loud. Plan is an instruction, not a
    # sandbox — the ladder's sentence says as much.
    if mode == "auto":
        argv += ["--auto"]
    if resume:
        # Long form on purpose: the two Kimi generations disagree on the short
        # flag (``agent_sessions``).
        argv += ["--session", resume]
    text = _PLAN_PREAMBLE + prompt if mode == "plan" else prompt
    argv += ["--prompt", text]
    env = _registry_env("kimi", _account_env("kimi"))
    return CliPlan(
        argv, env, None, "kimi", resume, discover=None if resume else _kimi_session_after
    )


def plan_dsh(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    """DeepSeek Harness's headless profile: one task in, the final message out.

    No model flag, no session, no tool stream — the harness keeps its key and
    its policy in its own settings (``jarvis.workspace.agents``). What comes
    back is text, and the chat shows it as the answer.
    """
    prompt = _with_identity(prompt, identity, None, compact=True)
    argv = [*dsh_argv_prefix(), "--profile", "headless", prompt]
    env = _registry_env("deepseek-harness", _account_env("deepseek-harness"))
    return CliPlan(argv, env, None, "text", None)


def plan_cursor(
    *,
    prompt: str,
    cwd: Path,
    model: str,
    effort: str,
    permission_mode: str,
    resume: str | None,
    identity: jarvis_harness.Identity | None = None,
) -> CliPlan:
    """Cursor CLI print mode: one prompt in, stream-json events out.

    ``-p`` / ``--print`` is the non-interactive path (scripts, CI, this chat).
    ``--force`` is what actually applies edits — without it print mode only
    proposes. Ask and Plan are ``--mode`` flags and do not take ``--force``,
    because those stances are supposed to leave the tree alone.
    """
    del cwd, effort  # cwd is the spawn cwd; Cursor has no effort flag.
    mode = normalize_permission("cursor-cli", permission_mode)
    prompt = _with_identity(prompt, identity, resume, compact=True)
    argv = [
        *cursor_argv_prefix(),
        "-p",
        "--output-format",
        "stream-json",
    ]
    if model:
        argv += ["--model", model]
    if mode == "plan":
        argv += ["--mode", "plan"]
    elif mode == "ask":
        argv += ["--mode", "ask"]
    else:
        argv += ["--force"]
    if resume:
        argv += ["--resume", resume]
    argv.append(prompt)
    env = _registry_env("cursor", _account_env("cursor"))
    return CliPlan(argv, env, None, "cursor", resume)


def _with_identity(
    prompt: str,
    identity: jarvis_harness.Identity | None,
    resume: str | None,
    *,
    compact: bool = False,
) -> str:
    """The prompt with the identity in front of it — on a fresh conversation.

    A resumed conversation carries the identity from its first turn; repeating
    fifty thousand characters of it every turn would cost the subscription's
    context for nothing. The block is fenced so the model can tell the head
    from the person's words.
    """
    if identity is None:
        return prompt
    if resume:
        if identity.session_id.startswith("society:"):
            from jarvis.core.response_style import CONVERSATIONAL_TURN_REMINDER

            # A resumed vendor conversation may remember an old, incomplete
            # tool catalog. Refresh the execution contract without repeating
            # the large identity/history block on every turn.
            return (
                "<jarvis_turn_context>\n"
                "You are a Society agent in the running Personal Jarvis app. "
                "Your current tools are available through the jarvis MCP server. "
                "For recurring-work requests, inspect society_routines, then call "
                "society_propose_change with kind=routine, mode=apply and an exact "
                "request_quote from the current user message. Brief confirmations "
                "refer to the agreed task. Read back the saved routine with "
                "society_routines before reporting it active. Do not substitute "
                "a plan, memory note, shell command or workspace file for scheduling. "
                "Use existing connected-account information; ask only for essential "
                "missing information. If a tool is unavailable or fails, report the "
                "actual blocker without claiming completion. Existing permission "
                "rules still apply.\n"
                + CONVERSATIONAL_TURN_REMINDER
                + "\n</jarvis_turn_context>\n\n" + prompt
            )
        return prompt
    text = identity.compact if compact else identity.text
    return f"<jarvis_identity>\n{text}\n</jarvis_identity>\n\n{prompt}"


_PLANNERS: Final[dict[str, Any]] = {
    "claude-cli": plan_claude,
    "grok-cli": plan_grok,
    "agy-cli": plan_agy,
    "codex-cli": plan_codex,
    "opencode-cli": plan_opencode,
    "kimi-cli": plan_kimi,
    "glm-cli": plan_glm,
    "dsh-cli": plan_dsh,
    "cursor-cli": plan_cursor,
}

#: Every runner this module drives — the catalog route's "is this a CLI
#: seat" question, answered from the planners rather than a second list.
CLI_RUNNERS: Final[frozenset[str]] = frozenset(_PLANNERS)

#: The runners that are Claude Code under the hood, and so take its identity
#: file and its control protocol.
_CLAUDE_CODE_RUNNERS: Final[frozenset[str]] = frozenset({"claude-cli", "glm-cli"})


def supports_cli_runner(runner: str) -> bool:
    return runner in _PLANNERS


# ------------------------------------------------------------ translation

#: The token fields a Claude CLI message reports. The two cache fields carry
#: the bulk of a real turn — a warm session reads tens of thousands of cached
#: tokens while ``input_tokens`` counts only the handful that were new — so a
#: counter that skips them under-reports by orders of magnitude (BUG-173).
_CLAUDE_USAGE_KEYS: Final[tuple[str, ...]] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass(slots=True)
class _ClaudeState:
    turn_id: str
    vendor_session: str | None = None
    current_message_id: str | None = None
    text_acc: dict[str, str] = field(default_factory=dict)
    thinking_acc: dict[str, str] = field(default_factory=dict)
    thinking_started: dict[str, float] = field(default_factory=dict)
    #: Message ids whose thinking block was announced live (one card each).
    thinking_announced: set[str] = field(default_factory=set)
    #: Per-message usage as the CLI reports it — summed into the live counter.
    usage_by_message: dict[str, dict[str, int]] = field(default_factory=dict)
    emitted_tool_ids: set[str] = field(default_factory=set)
    emitted_text: bool = False
    status: str = "done"
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    result_text: str = ""
    #: The turn's ``result`` line arrived — stdin may close, the CLI exits.
    saw_result: bool = False


def _claude_request_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:200]
    return tool_name


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(
            isinstance(block, dict)
            and block.get("type")
            in {"image", "image_url", "input_image", "video", "audio", "resource", "resource_link"}
            for block in content
        ):
            # Preserve structured media for the common chat delivery layer.
            return json.dumps(content, ensure_ascii=False)
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif "text" in block:
                    parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return "" if content is None else str(content)


def translate_claude_line(obj: dict[str, Any], st: _ClaudeState) -> list[dict[str, Any]]:
    """One Claude-shaped NDJSON object -> zero or more agent-chat events."""
    out: list[dict[str, Any]] = []
    kind = str(obj.get("type") or "")
    sid = obj.get("session_id") or obj.get("conversation_id")
    if sid and not st.vendor_session:
        st.vendor_session = str(sid)

    if kind == "system":
        return out  # init / hooks / status — nothing the timeline shows

    if kind == "stream_event":
        ev = obj.get("event") or {}
        et = ev.get("type")
        if et == "message_start":
            mid = ((ev.get("message") or {}).get("id")) or uuid.uuid4().hex
            st.current_message_id = str(mid)
        elif et == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "thinking":
                mid = st.current_message_id or uuid.uuid4().hex
                st.current_message_id = mid
                st.thinking_started.setdefault(mid, time.perf_counter())
                # Claude Code redacts thinking text in its stream, so without
                # this the UI learned of eight seconds of thought only once it
                # was over. Announce the block the moment it opens.
                if mid not in st.thinking_announced:
                    st.thinking_announced.add(mid)
                    out.append(
                        make_event(
                            "reasoning_started",
                            {"turn_id": st.turn_id, "message_id": mid},
                        )
                    )
        elif et == "content_block_delta":
            delta = ev.get("delta") or {}
            mid = st.current_message_id or uuid.uuid4().hex
            st.current_message_id = mid
            if delta.get("type") == "text_delta" and delta.get("text"):
                # Streamed text counts as text the timeline already has. Without
                # this, a turn whose closing ``assistant`` message never arrives
                # (a kill, a crash, a truncated stream) fell through to the
                # ``result`` fallback below and emitted the SAME answer a second
                # time under a new message id — the duplicated output the
                # maintainer saw on 2026-08-25. The Codex shape already does it.
                st.emitted_text = True
                out.append(
                    make_event(
                        "text_delta",
                        {"turn_id": st.turn_id, "message_id": mid, "text": delta["text"]},
                    )
                )
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                st.thinking_started.setdefault(mid, time.perf_counter())
                out.append(
                    make_event(
                        "reasoning_delta",
                        {"turn_id": st.turn_id, "message_id": mid, "text": delta["thinking"]},
                    )
                )
        return out

    if kind == "assistant":
        message = obj.get("message") or {}
        mid = str(message.get("id") or st.current_message_id or uuid.uuid4().hex)
        st.current_message_id = mid
        usage_now = message.get("usage")
        if isinstance(usage_now, dict):
            counted = {
                k: int(usage_now[k])
                for k in _CLAUDE_USAGE_KEYS
                if isinstance(usage_now.get(k), int | float)
            }
            # A message's first usage report is the ``message_start`` snapshot:
            # the input side is already final, ``output_tokens`` is a placeholder
            # that only becomes true later. Keep the largest value seen so a
            # placeholder can never walk a real count back down (BUG-173).
            previous = st.usage_by_message.get(mid)
            if previous:
                counted = {
                    k: max(counted.get(k, 0), previous.get(k, 0)) for k in (*counted, *previous)
                }
            if counted and counted != previous:
                st.usage_by_message[mid] = counted
                totals: dict[str, int] = {}
                for per_message in st.usage_by_message.values():
                    for k, v in per_message.items():
                        totals[k] = totals.get(k, 0) + v
                out.append(
                    make_event(
                        "usage_delta",
                        {"turn_id": st.turn_id, "usage": totals},
                    )
                )
        content = message.get("content") or []
        if not isinstance(content, list):
            content = [{"type": "text", "text": str(content)}]
        text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        snapshot = len(content) > 1
        visual_blocks = [
            block
            for block in content
            if isinstance(block, dict)
            and block.get("type")
            in {"image", "image_url", "input_image", "video", "audio", "resource", "resource_link"}
        ]
        if visual_blocks:
            st.emitted_text = True
            visual_content = [
                block
                for block in content
                if block in visual_blocks
                or (isinstance(block, dict) and block.get("type") == "text")
            ]
            out.append(
                make_event(
                    "assistant_text",
                    {
                        "turn_id": st.turn_id,
                        "message_id": mid,
                        "text": json.dumps({"content": visual_content}, ensure_ascii=False),
                    },
                )
            )
            text_blocks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                thought = str(block.get("thinking") or "")
                if snapshot:
                    st.thinking_acc[mid] = thought
                else:
                    st.thinking_acc[mid] = st.thinking_acc.get(mid, "") + thought
                started = st.thinking_started.get(mid)
                out.append(
                    make_event(
                        "reasoning",
                        {
                            "turn_id": st.turn_id,
                            "message_id": mid,
                            "text": st.thinking_acc[mid],
                            "duration_ms": int((time.perf_counter() - started) * 1000)
                            if started
                            else None,
                        },
                    )
                )
            elif btype == "tool_use":
                call_id = str(block.get("id") or uuid.uuid4().hex)
                if call_id in st.emitted_tool_ids:
                    continue
                st.emitted_tool_ids.add(call_id)
                name = str(block.get("name") or "tool")
                args = block.get("input") or {}
                out.append(
                    make_event(
                        "tool_call",
                        {
                            "turn_id": st.turn_id,
                            "call_id": call_id,
                            "name": name,
                            "input": args if isinstance(args, dict) else {"input": args},
                            "summary": _cli_tool_summary(name, args),
                        },
                    )
                )
        if text_blocks:
            joined = "".join(str(b.get("text") or "") for b in text_blocks)
            if snapshot:
                st.text_acc[mid] = joined
            else:
                st.text_acc[mid] = st.text_acc.get(mid, "") + joined
            if st.text_acc[mid].strip():
                st.emitted_text = True
                out.append(
                    make_event(
                        "assistant_text",
                        {"turn_id": st.turn_id, "message_id": mid, "text": st.text_acc[mid]},
                    )
                )
        return out

    if kind == "user":
        message = obj.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(
                        make_event(
                            "tool_result",
                            {
                                "turn_id": st.turn_id,
                                "call_id": str(block.get("tool_use_id") or ""),
                                "output": _content_text(block.get("content")),
                                "is_error": bool(block.get("is_error")),
                                "duration_ms": None,
                            },
                        )
                    )
        return out

    if kind == "result":
        st.saw_result = True
        st.result_text = str(obj.get("result") or "")
        if obj.get("is_error"):
            st.status = "error"
            errors = obj.get("errors")
            detail = "; ".join(str(e) for e in errors if e) if isinstance(errors, list) else ""
            st.error = st.result_text or detail or str(obj.get("subtype") or "error")
        usage = obj.get("usage") or {}
        if isinstance(usage, dict):
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                if isinstance(usage.get(k), int | float):
                    st.usage[k] = int(usage[k])
            details = usage.get("output_tokens_details")
            if isinstance(details, dict) and isinstance(
                details.get("thinking_tokens"), int | float
            ):
                st.usage["thinking_tokens"] = int(details["thinking_tokens"])
        cost = obj.get("total_cost_usd", obj.get("cost_usd"))
        if isinstance(cost, int | float):
            st.cost_usd = float(cost)
        return out

    return out


def _cli_tool_summary(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in (
        "command",
        "file_path",
        "filePath",
        "target_file",
        "path",
        "pattern",
        "query",
        "url",
        "description",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:200]
    return ""


@dataclass(slots=True)
class _CodexState:
    turn_id: str
    vendor_session: str | None = None
    status: str = "done"
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    emitted_text: bool = False
    #: item id -> (call_id, name) for items that became tool calls
    items: dict[str, tuple[str, str]] = field(default_factory=dict)
    started_at: dict[str, float] = field(default_factory=dict)
    #: The last top-level ``error`` notification (retryable until turn.failed).
    last_error: str | None = None


def translate_codex_line(obj: dict[str, Any], st: _CodexState) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kind = str(obj.get("type") or "")
    if kind == "thread.started":
        tid = obj.get("thread_id")
        if tid:
            st.vendor_session = str(tid)
        return out
    if kind == "turn.completed":
        usage = obj.get("usage") or {}
        if isinstance(usage, dict):
            for k in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "reasoning_output_tokens",
            ):
                if isinstance(usage.get(k), int | float):
                    st.usage[k] = int(usage[k])
        # A completed turn wins over a retryable error seen on the way.
        st.status = "done"
        st.error = None
        return out
    if kind == "turn.failed":
        st.status = "error"
        err = obj.get("error")
        st.error = (
            str(err.get("message") or err)
            if isinstance(err, dict)
            else str(err or st.last_error or "turn failed")
        )
        return out
    if kind == "error":
        # A server notification — may be retried by the CLI; remembered, and
        # only terminal when no turn.completed follows (exit code decides).
        st.last_error = str(obj.get("message") or "error")
        return out
    if not kind.startswith("item."):
        return out

    item = obj.get("item") or {}
    item_id = str(item.get("id") or uuid.uuid4().hex)
    itype = str(item.get("type") or "")
    phase = kind.split(".", 1)[1]  # started | updated | completed

    if itype == "agent_message":
        text = str(item.get("text") or "")
        if phase == "completed" and text.strip():
            st.emitted_text = True
            out.append(
                make_event(
                    "assistant_text",
                    {"turn_id": st.turn_id, "message_id": item_id, "text": text},
                )
            )
        return out
    if itype == "reasoning":
        text = str(item.get("text") or "")
        if phase == "started":
            out.append(
                make_event(
                    "reasoning_started",
                    {"turn_id": st.turn_id, "message_id": item_id},
                )
            )
            return out
        if phase == "completed" and text.strip():
            out.append(
                make_event(
                    "reasoning",
                    {
                        "turn_id": st.turn_id,
                        "message_id": item_id,
                        "text": text,
                        "duration_ms": None,
                    },
                )
            )
        return out
    if itype in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
        if itype == "command_execution":
            name, args = "RunCommand", {"command": str(item.get("command") or "")}
            summary = args["command"].splitlines()[0][:200] if args["command"] else ""
        elif itype == "file_change":
            changes = item.get("changes") or []
            paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
            name, args = "Edit", {"changes": changes}
            summary = ", ".join(p for p in paths if p)[:200]
        elif itype == "mcp_tool_call":
            name = str(item.get("tool") or item.get("server") or "mcp")
            args = item.get("arguments") or {}
            summary = str(item.get("server") or "")
        else:
            name, args = "WebSearch", {"query": str(item.get("query") or "")}
            summary = args["query"]
        if item_id not in st.items:
            st.items[item_id] = (item_id, name)
            st.started_at[item_id] = time.perf_counter()
            out.append(
                make_event(
                    "tool_call",
                    {
                        "turn_id": st.turn_id,
                        "call_id": item_id,
                        "name": name,
                        "input": args if isinstance(args, dict) else {"input": args},
                        "summary": summary,
                    },
                )
            )
        if phase == "completed":
            if itype == "command_execution":
                output = str(item.get("aggregated_output") or "")
                code = item.get("exit_code")
                if code is not None:
                    output = (output.rstrip() + f"\n[exit {code}]").strip()
                item_status = str(item.get("status") or "")
                is_error = bool(code) or item_status in {"failed", "declined"}
                if item_status == "declined":
                    output = (output.rstrip() + "\n[declined by the sandbox / policy]").strip()
            elif itype == "file_change":
                output = "\n".join(
                    f"{c.get('kind', 'edit')}: {c.get('path', '')}"
                    for c in (item.get("changes") or [])
                    if isinstance(c, dict)
                )
                is_error = str(item.get("status") or "") == "failed"
            elif itype == "mcp_tool_call":
                output = json.dumps(
                    item.get("result") or item.get("error") or {}, ensure_ascii=False
                )
                is_error = bool(item.get("error"))
            else:
                output = "done"
                is_error = False
            started = st.started_at.get(item_id)
            out.append(
                make_event(
                    "tool_result",
                    {
                        "turn_id": st.turn_id,
                        "call_id": item_id,
                        "output": output,
                        "is_error": is_error,
                        "duration_ms": int((time.perf_counter() - started) * 1000)
                        if started
                        else None,
                    },
                )
            )
        return out
    if itype == "error":
        # NOT a failed turn: Codex files warnings here (skills budget, hook
        # clamps, model reroutes) in every run. Remembered for the error text
        # should the turn fail without a message of its own.
        st.last_error = str(item.get("message") or "error")
    return out


# ------------------------------------------------------------ agy translation


@dataclass(slots=True)
class _AgyState:
    turn_id: str
    vendor_session: str | None = None
    status: str = "done"
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    emitted_text: bool = False
    result_text: str = ""
    #: step_index -> wall-clock start, for tool durations
    started_at: dict[int, float] = field(default_factory=dict)
    #: step_index of tool steps already announced
    tool_steps: set[int] = field(default_factory=set)
    #: text per agent_response step (a DONE step repeats its last delta)
    text_acc: dict[int, str] = field(default_factory=dict)


_AGY_TOOL_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "CommandLine",
    "AbsolutePath",
    "TargetFile",
    "Pattern",
    "SearchDirectory",
    "DirectoryPath",
    "Query",
    "Url",
)


def _agy_summary(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    for key in _AGY_TOOL_SUMMARY_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().splitlines()[0][:200]
    first = next((v for v in params.values() if isinstance(v, str) and v.strip()), "")
