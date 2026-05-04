#!/usr/bin/env python3
"""claude-diag — generate a redacted diagnostic report for Claude Code.

Single-file, stdlib-only. Safe to pipe over curl:

    curl -fsSL https://raw.githubusercontent.com/engels74/arrsenal-of-scripts/refs/heads/main/miscellaneous/claude/claude-diag.py | python3 -

See --help for flags.
"""

import argparse
import io
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast
from zoneinfo import ZoneInfo

type JsonObject = dict[str, JsonValue]
type JsonValue = None | bool | int | float | str | list[JsonValue] | JsonObject
type PathInput = str | os.PathLike[str]
type Redact = Callable[[object | None], str]
type HttpPost = Callable[[str, bytes, dict[str, str]], tuple[int, str]]

__version__ = "0.1.0"
SCRIPT_URL = (
    "https://raw.githubusercontent.com/engels74/arrsenal-of-scripts/refs/heads/"
    "main/miscellaneous/claude/claude-diag.py"
)

HOME = Path.home()
USERNAME = HOME.name
CLAUDE_DIR = HOME / ".claude"
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------- redactor --

class Redactor:
    PATTERNS: ClassVar[tuple[tuple[re.Pattern[str], str], ...]] = (
        (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[REDACTED:ANTHROPIC_KEY]"),
        (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:OPENAI_KEY]"),
        (re.compile(r"gh[posru]_[A-Za-z0-9]{30,}"), "[REDACTED:GITHUB_TOKEN]"),
        (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY]"),
        (re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
         "[REDACTED:JWT]"),
        (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
         "[REDACTED:EMAIL]"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
         "Bearer [REDACTED]"),
    )
    IPV4: ClassVar[re.Pattern[str]] = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    URL_QS: ClassVar[re.Pattern[str]] = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
    AUTH_HEADER: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)\b(Authorization|X-API-Key|X-Auth-Token|Cookie)(\s*[:=]\s*)\S+"
    )
    USERS_PATH: ClassVar[re.Pattern[str]] = re.compile(r"/Users/[^/\s:\"',]+")
    HOME_PATH: ClassVar[re.Pattern[str]] = re.compile(r"/home/[^/\s:\"',]+")
    PROJECT_PATH: ClassVar[re.Pattern[str]] = re.compile(r"~/\.claude/projects/([^/\s\"',]+)")

    hostname: str
    short_hostname: str
    _project_ids: dict[str, int]
    _next_id: int

    def __init__(self) -> None:
        self.hostname = socket.gethostname() or ""
        self.short_hostname = self.hostname.split(".")[0] if self.hostname else ""
        self._project_ids = {}
        self._next_id = 1

    def _ip(self, m: re.Match[str]) -> str:
        ip = m.group(0)
        try:
            o = [int(x) for x in ip.split(".")]
        except ValueError:
            return ip
        if len(o) != 4 or any(x > 255 for x in o):
            return ip
        if o[0] == 10: return ip
        if o[0] == 172 and 16 <= o[1] <= 31: return ip
        if o[0] == 192 and o[1] == 168: return ip
        if o[0] == 127: return ip
        if o[0] == 0: return ip
        return "[REDACTED:IP]"

    def _project(self, m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in self._project_ids:
            self._project_ids[name] = self._next_id
            self._next_id += 1
        return f"~/.claude/projects/[PROJECT-{self._project_ids[name]}]"

    def __call__(self, s: object | None) -> str:
        if s is None:
            return ""
        if not isinstance(s, str):
            s = str(s)
        for pat, repl in self.PATTERNS:
            s = pat.sub(repl, s)
        s = self.IPV4.sub(self._ip, s)
        s = self.URL_QS.sub(r"\1?[REDACTED:QUERYSTRING]", s)
        s = self.AUTH_HEADER.sub(r"\1\2[REDACTED]", s)
        if len(self.hostname) > 2:
            s = s.replace(self.hostname, "[REDACTED:HOSTNAME]")
        if len(self.short_hostname) > 2 and self.short_hostname != self.hostname:
            s = re.sub(rf"\b{re.escape(self.short_hostname)}\b",
                       "[REDACTED:HOSTNAME]", s)
        s = self.USERS_PATH.sub("~", s)
        s = self.HOME_PATH.sub("~", s)
        s = self.PROJECT_PATH.sub(self._project, s)
        return s

    @property
    def project_count(self) -> int:
        return len(self._project_ids)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None

    @property
    def total(self) -> int | None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
        )
        if not any(v is not None for v in values):
            return None
        return sum(v or 0 for v in values)


@dataclass(frozen=True)
class ModelUsageSummary:
    model: str
    total_cost_usd: float | None
    tokens: TokenUsage


@dataclass(frozen=True)
class UsageSummary:
    total_cost_usd: float | None
    tokens: TokenUsage
    model_usage: tuple[ModelUsageSummary, ...] = ()


# ----------------------------------------------------------------- helpers --

def run(cmd: Sequence[str], timeout: int) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return out.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "[command timed out]", 124
    except FileNotFoundError:
        return "[not installed]", 127
    except Exception as e:
        return f"[error: {e}]", 1


def safe_read(path: PathInput, max_bytes: int = 512 * 1024) -> str | None:
    try:
        p = Path(path)
        if not p.is_file():
            return None
        data = p.read_text(errors="replace")
        if len(data) > max_bytes:
            return data[:max_bytes] + f"\n\n[truncated at {max_bytes} bytes]"
        return data
    except Exception as e:
        return f"[read error: {e}]"


def folder_size(path: PathInput) -> tuple[int | None, int]:
    p = Path(path)
    if not p.exists():
        return None, 0
    total, count = 0, 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def humansize(n: int | float | None) -> str:
    if n is None:
        return "n/a"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def line_count(path: PathInput) -> int:
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def details(title: str, body: str, open_: bool = False) -> str:
    o = " open" if open_ else ""
    return f"<details{o}>\n<summary>{title}</summary>\n\n{body}\n\n</details>\n"


def code_block(s: str, lang: str = "") -> str:
    return f"```{lang}\n{s.rstrip()}\n```"


def load_json_text(text: str) -> JsonValue:
    return cast(JsonValue, json.loads(text))


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _int_field(data: JsonObject, keys: Sequence[str]) -> int | None:
    for key in keys:
        value = _number(data.get(key))
        if value is not None:
            return int(value)
    return None


def _float_field(data: JsonObject, keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _number(data.get(key))
        if value is not None:
            return float(value)
    return None


def _parse_token_usage(value: JsonValue) -> TokenUsage:
    if not isinstance(value, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_int_field(value, ("input_tokens", "inputTokens")),
        output_tokens=_int_field(value, ("output_tokens", "outputTokens")),
        cache_read_tokens=_int_field(
            value,
            (
                "cache_read_input_tokens",
                "cache_read_tokens",
                "cacheReadInputTokens",
                "cacheReadTokens",
            ),
        ),
        cache_creation_tokens=_int_field(
            value,
            (
                "cache_creation_input_tokens",
                "cache_creation_tokens",
                "cacheCreationInputTokens",
                "cacheCreationTokens",
            ),
        ),
    )


def _usage_source(data: JsonObject) -> JsonValue:
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else data


def _parse_model_usage(value: JsonValue) -> tuple[ModelUsageSummary, ...]:
    rows: list[ModelUsageSummary] = []
    if isinstance(value, dict):
        items: list[tuple[str, JsonValue]] = [
            (str(model), model_data) for model, model_data in value.items()
        ]
    elif isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, dict):
                continue
            model = item.get("model") or item.get("name")
            if isinstance(model, str):
                items.append((model, item))
    else:
        return ()

    for model, model_data in items:
        if not isinstance(model_data, dict):
            continue
        cost = _float_field(
            model_data,
            ("total_cost_usd", "cost_usd", "totalCostUsd", "costUsd", "cost"),
        )
        tokens = _parse_token_usage(_usage_source(model_data))
        if cost is None and tokens.total is None:
            continue
        rows.append(ModelUsageSummary(model, cost, tokens))
    return tuple(rows)


def parse_context_json(raw: str) -> tuple[str, UsageSummary | None]:
    try:
        data = load_json_text(raw)
    except Exception:
        return raw, None
    if not isinstance(data, dict):
        return raw, None
    result = data.get("result")
    if not isinstance(result, str):
        return raw, None

    total_cost_usd = _float_field(data, ("total_cost_usd", "totalCostUsd"))
    tokens = _parse_token_usage(data.get("usage"))
    model_usage = _parse_model_usage(data.get("modelUsage") or data.get("model_usage"))
    if total_cost_usd is None and tokens.total is None and not model_usage:
        return result, None
    return result, UsageSummary(total_cost_usd, tokens, model_usage)


def _format_cost(cost: float | None) -> str:
    if cost is None:
        return "cost unavailable"
    if cost < 0.01:
        amount = f"{cost:.4f}"
    elif cost < 1:
        amount = f"{cost:.3f}"
    else:
        amount = f"{cost:.2f}"
    return f"~${amount}"


def _format_cost_phrase(cost: float | None) -> str:
    cost_text = _format_cost(cost)
    return f"approx {cost_text}" if cost is not None else cost_text


def _format_token_count(tokens: int) -> str:
    if tokens < 1_000:
        return f"{tokens:,}"
    if tokens < 1_000_000:
        return f"{tokens / 1_000:.1f}K"
    return f"{tokens / 1_000_000:.1f}M"


def _format_token_parts(tokens: TokenUsage) -> str:
    parts: list[str] = []
    if tokens.input_tokens is not None:
        parts.append(f"input {_format_token_count(tokens.input_tokens)}")
    if tokens.output_tokens is not None:
        parts.append(f"output {_format_token_count(tokens.output_tokens)}")
    if tokens.cache_read_tokens is not None:
        parts.append(f"cache read {_format_token_count(tokens.cache_read_tokens)}")
    if tokens.cache_creation_tokens is not None:
        parts.append(f"cache write {_format_token_count(tokens.cache_creation_tokens)}")
    return ", ".join(parts)


def format_usage_summary(summary: UsageSummary | None) -> str:
    if summary is None:
        return "Run usage: not available."

    total = summary.tokens.total
    if total is None:
        line = (
            f"Run usage: {_format_cost_phrase(summary.total_cost_usd)}, "
            + "tokens unavailable."
        )
    else:
        details_text = _format_token_parts(summary.tokens)
        detail_suffix = f" ({details_text})" if details_text else ""
        line = (
            f"Run usage: {_format_cost_phrase(summary.total_cost_usd)}, "
            + f"{_format_token_count(total)} tokens{detail_suffix}."
        )

    if summary.model_usage:
        model_parts: list[str] = []
        for row in summary.model_usage[:3]:
            row_total = row.tokens.total
            if row_total is None:
                model_parts.append(f"{row.model}: {_format_cost(row.total_cost_usd)}")
            else:
                model_parts.append(
                    f"{row.model}: {_format_cost(row.total_cost_usd)}, "
                    + f"{_format_token_count(row_total)} tokens"
                )
        more = "" if len(summary.model_usage) <= 3 else ", ..."
        line += " Models: " + "; ".join(model_parts) + more + "."
    return line


# ---------------------------------------------------------------- sections --

def section_header() -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"# Claude Code diagnostic report\n\n"
        f"- Generator: `claude-diag` v{__version__}\n"
        f"- Generated: {now}\n"
        f"- Redaction: secrets, emails, hostnames, public IPs, "
        f"and user paths are scrubbed before output.\n"
    )


def section_environment(redact: Redact, timeout: int) -> str:
    uname = platform.uname()
    py = sys.version.split()[0]
    node_v, _ = run(["node", "--version"], timeout)
    npm_v, _ = run(["npm", "--version"], timeout)
    shell = os.environ.get("SHELL", "")
    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    lines = [
        f"- OS: {uname.system} {uname.release} ({uname.machine})",
        f"- Platform: {platform.platform()}",
        f"- Python: {py}",
        f"- Node: {node_v}",
        f"- npm: {npm_v}",
        f"- Shell: {redact(shell)}",
        f"- TERM: {term}",
        f"- TERM_PROGRAM: {term_program}",
    ]
    return "## Environment\n\n" + "\n".join(lines) + "\n"


def section_claude(redact: Redact, timeout: int) -> str:
    version, _ = run(["claude", "--version"], timeout)
    path = shutil.which("claude") or "[not on PATH]"
    auth_env_keys = [
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    ]
    auth_present = {k: ("set" if os.environ.get(k) else "unset") for k in auth_env_keys}
    creds_path = CLAUDE_DIR / ".credentials.json"
    has_credfile = creds_path.exists()
    lines = [
        f"- `claude --version`: `{redact(version)}`",
        f"- install path: `{redact(path)}`",
        f"- credentials file present (`~/.claude/.credentials.json`): {has_credfile}",
        f"- auth env vars (presence only):",
    ]
    for k, v in auth_present.items():
        lines.append(f"  - `{k}`: {v}")
    return "## Claude Code\n\n" + "\n".join(lines) + "\n"


def section_context(
    redact: Redact,
    model: str | None,
    timeout: int,
    skip: bool,
) -> tuple[str, UsageSummary | None]:
    if skip:
        return "## `/context` output\n\n_skipped via `--no-context`_\n", None
    cmd = [
        "claude", "-p", "/context", "--output-format", "json",
    ]
    if model:
        cmd.extend(("--model", model))
    out, code = run(cmd, timeout)
    if code != 0 and out in ("[not installed]", "[command timed out]"):
        return f"## `/context` output\n\n_{out}_\n", None
    body, usage_summary = parse_context_json(out) if out else ("_no output_", None)
    body = redact(body) if body else "_no output_"
    captured_cmd = redact(shlex.join(cmd))
    return (
        "## `/context` output\n\n"
        + f"_Captured via `{captured_cmd}` "
        + f"(exit code {code}). Paths and secrets redacted._\n\n"
        + code_block(body, "")
        + "\n"
    ), usage_summary


def _settings_summary(data: JsonObject, redact: Redact) -> str:
    lines: list[str] = []
    lines.append(f"- Top-level keys: `{', '.join(sorted(data.keys())) or '(none)'}`")
    env = data.get("env")
    if isinstance(env, dict) and env:
        lines.append(f"- env vars (keys only, {len(env)}):")
        for k in sorted(env.keys()):
            lines.append(f"  - `{redact(k)}`")
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and hooks:
        lines.append(f"- hooks ({len(hooks)} event types):")
        for event in sorted(hooks.keys()):
            entries = hooks[event]
            n_match = len(entries) if isinstance(entries, list) else 0
            n_cmd = 0
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict):
                        h = e.get("hooks", [])
                        if isinstance(h, list):
                            n_cmd += len(h)
            lines.append(f"  - `{event}`: {n_match} matchers, {n_cmd} commands")
    perms = data.get("permissions")
    if isinstance(perms, dict) and perms:
        for k in ("allow", "ask", "deny", "additionalDirectories"):
            v = perms.get(k)
            if isinstance(v, list):
                lines.append(f"- permissions.{k}: {len(v)}")
        if "defaultMode" in perms:
            lines.append(f"- permissions.defaultMode: `{redact(perms['defaultMode'])}`")
    if "statusLine" in data:
        sl = data["statusLine"]
        if isinstance(sl, dict):
            t = sl.get("type", "?")
            lines.append(f"- statusLine: configured (type=`{t}`)")
        else:
            lines.append(f"- statusLine: configured")
    else:
        lines.append("- statusLine: not configured")
    plugins = data.get("enabledPlugins")
    if isinstance(plugins, dict) and plugins:
        lines.append(f"- enabledPlugins ({len(plugins)}):")
        for k in sorted(plugins.keys()):
            lines.append(f"  - `{redact(k)}`: {plugins[k]}")
    flags = sorted(k for k in data.keys()
                   if k.startswith(("CLAUDE_CODE_", "ENABLE_CLAUDEAI_", "DISABLE_")))
    if flags:
        lines.append(f"- feature flags: {', '.join(flags)}")
    for k in ("model", "outputStyle", "theme", "includeCoAuthoredBy",
              "verbose", "autoUpdates"):
        if k in data:
            lines.append(f"- `{k}`: `{redact(data[k])}`")
    return "\n".join(lines)


def section_global_settings(redact: Redact) -> str:
    out = ["## Global settings (`~/.claude/settings.json`)\n"]
    for fname in ("settings.json", "settings.local.json"):
        path = CLAUDE_DIR / fname
        if not path.exists():
            out.append(f"### `~/.claude/{fname}`\n\n_not present_\n")
            continue
        try:
            data = load_json_text(path.read_text())
        except Exception as e:
            out.append(f"### `~/.claude/{fname}`\n\n_parse error: {redact(str(e))}_\n")
            continue
        if isinstance(data, dict):
            out.append(f"### `~/.claude/{fname}`\n\n" + _settings_summary(data, redact) + "\n")
        else:
            out.append(f"### `~/.claude/{fname}`\n\n_malformed: expected JSON object_\n")
    return "\n".join(out)


def section_project_settings(redact: Redact) -> str:
    cwd = Path.cwd()
    candidates: list[tuple[Path, str]] = [
        (cwd / ".claude" / "settings.json", "$PWD/.claude/settings.json"),
        (cwd / ".claude" / "settings.local.json", "$PWD/.claude/settings.local.json"),
        (cwd / ".mcp.json", "$PWD/.mcp.json"),
    ]
    out = ["## Project settings\n"]
    found_any = False
    for path, label in candidates:
        if not path.exists():
            continue
        found_any = True
        try:
            data = load_json_text(path.read_text())
        except Exception as e:
            out.append(f"### `{label}`\n\n_parse error: {redact(str(e))}_\n")
            continue
        if label.endswith(".mcp.json"):
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            server_names = sorted(servers.keys()) if isinstance(servers, dict) else []
            out.append(
                f"### `{label}`\n\n- mcpServers: {len(server_names)} "
                + f"({', '.join(server_names)})\n"
            )
        elif isinstance(data, dict):
            out.append(f"### `{label}`\n\n" + _settings_summary(data, redact) + "\n")
        else:
            out.append(f"### `{label}`\n\n_malformed: expected JSON object_\n")
    if not found_any:
        out.append("_no project-level Claude Code config in `$PWD`_\n")
    return "\n".join(out)


def section_mcp(redact: Redact, timeout: int) -> str:
    out, code = run(["claude", "mcp", "list"], timeout)
    body = redact(out) if out else "_no output_"
    return (
        "## MCP servers\n\n"
        + f"_via `claude mcp list` (exit code {code})._\n\n"
        + code_block(body, "")
        + "\n"
    )


def section_plugins(redact: Redact, timeout: int) -> str:
    out, code = run(["claude", "plugin", "list"], timeout)
    body = redact(out) if out else "_no output_"
    parts = [
        f"## Plugins\n\n_via `claude plugin list` (exit code {code})._\n",
        code_block(body, ""),
    ]
    plugin_json = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if plugin_json.exists():
        try:
            data = load_json_text(plugin_json.read_text())
            count = sum(len(v) if isinstance(v, dict) else 0 for v in data.values()) \
                if isinstance(data, dict) else 0
            parts.append(
                "\n- `installed_plugins.json` size: "
                + f"{humansize(plugin_json.stat().st_size)}, "
                + f"~{count} entries\n"
            )
        except Exception as e:
            parts.append(f"\n- `installed_plugins.json` parse error: {redact(str(e))}\n")
    return "\n".join(parts)


def _list_dir_entries(path: PathInput) -> list[Path]:
    try:
        return sorted(p for p in Path(path).iterdir() if not p.name.startswith("."))
    except Exception:
        return []


def section_skills(redact: Redact) -> str:
    skills_dir = CLAUDE_DIR / "skills"
    if not skills_dir.exists():
        return "## Skills\n\n_`~/.claude/skills/` not present_\n"
    entries = _list_dir_entries(skills_dir)
    lines = [f"## Skills\n\n_{len(entries)} entries in `~/.claude/skills/`_\n"]
    rows: list[str] = []
    for e in entries:
        if e.is_dir():
            total, n = folder_size(e)
            rows.append(f"- `{redact(e.name)}/` — {humansize(total)}, {n} files")
        else:
            rows.append(f"- `{redact(e.name)}` — {humansize(e.stat().st_size)}")
    if rows:
        lines.append("\n".join(rows))
    return "\n".join(lines) + "\n"


def section_agents(redact: Redact, include_memories: bool) -> str:
    agents_dir = CLAUDE_DIR / "agents"
    if not agents_dir.exists():
        return "## Agents\n\n_`~/.claude/agents/` not present_\n"
    entries = sorted(p for p in agents_dir.glob("*.md"))
    lines = [f"## Agents\n\n_{len(entries)} agent files in `~/.claude/agents/`_\n"]
    rows: list[str] = []
    bodies: list[str] = []
    for e in entries:
        lc = line_count(e)
        rows.append(f"- `{redact(e.name)}` — {lc} lines, {humansize(e.stat().st_size)}")
        if include_memories:
            content = safe_read(e)
            if content is not None:
                bodies.append(f"### `{redact(e.name)}`\n\n"
                              + code_block(redact(content), "markdown"))
    if rows:
        lines.append("\n".join(rows))
    if bodies:
        lines.append("\n" + details("Agent bodies (`--include-memories`)",
                                    "\n\n".join(bodies)))
    return "\n".join(lines) + "\n"


def section_commands(redact: Redact) -> str:
    cmd_dir = CLAUDE_DIR / "commands"
    if not cmd_dir.exists():
        return "## Slash commands\n\n_`~/.claude/commands/` not present_\n"
    entries = sorted(p for p in cmd_dir.iterdir() if not p.name.startswith("."))
    lines = [f"## Slash commands\n\n_{len(entries)} entries in `~/.claude/commands/`_\n"]
    rows: list[str] = []
    for e in entries:
        suffix = "/" if e.is_dir() else ""
        rows.append(f"- `{redact(e.name)}{suffix}`")
    if rows:
        lines.append("\n".join(rows))
    return "\n".join(lines) + "\n"


def section_memories(redact: Redact, include_memories: bool) -> str:
    targets: list[tuple[str, Path]] = [
        ("~/.claude/CLAUDE.md", CLAUDE_DIR / "CLAUDE.md"),
        ("~/.claude/AGENTS.md", CLAUDE_DIR / "AGENTS.md"),
        ("~/.claude/MEMORIES.md", CLAUDE_DIR / "MEMORIES.md"),
        ("~/.claude/RTK.md", CLAUDE_DIR / "RTK.md"),
        ("$PWD/CLAUDE.md", Path.cwd() / "CLAUDE.md"),
        ("$PWD/AGENTS.md", Path.cwd() / "AGENTS.md"),
    ]
    lines = ["## Memory files\n"]
    bodies: list[str] = []
    for label, path in targets:
        if not path.exists():
            lines.append(f"- `{label}`: _not present_")
            continue
        try:
            text = path.read_text(errors="replace")
        except Exception as e:
            lines.append(f"- `{label}`: read error ({redact(str(e))})")
            continue
        n_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        n_imports = len(re.findall(r"(?m)^@[\w./-]+", text))
        size = path.stat().st_size
        lines.append(
            f"- `{label}`: {humansize(size)}, {n_lines} lines, "
            + f"{n_imports} `@imports`"
        )
        if include_memories:
            bodies.append(f"### `{label}`\n\n"
                          + code_block(redact(text), "markdown"))
    if bodies:
        lines.append("\n" + details("Memory file bodies (`--include-memories`)",
                                    "\n\n".join(bodies)))
    return "\n".join(lines) + "\n"


def section_hooks(redact: Redact) -> str:
    settings_path = CLAUDE_DIR / "settings.json"
    if not settings_path.exists():
        return "## Hooks\n\n_no `~/.claude/settings.json`_\n"
    try:
        data = load_json_text(settings_path.read_text())
    except Exception as e:
        return f"## Hooks\n\n_parse error: {redact(str(e))}_\n"
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return "## Hooks\n\n_no hooks configured_\n"
    lines = ["## Hooks\n",
             "_Event names and counts only — command bodies are never printed._\n"]
    for event in sorted(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            lines.append(f"- `{event}`: malformed")
            continue
        matchers: list[str] = []
        cmd_count = 0
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            matcher = ent.get("matcher", "*")
            hs = ent.get("hooks", [])
            n = len(hs) if isinstance(hs, list) else 0
            cmd_count += n
            matchers.append(f"`{redact(str(matcher))}`×{n}")
        lines.append(
            f"- `{event}`: {len(entries)} matchers, "
            + f"{cmd_count} commands — {', '.join(matchers)}"
        )
    return "\n".join(lines) + "\n"


def section_state_footprint(_redact: Redact) -> str:
    dirs = ["projects", "debug", "telemetry", "plans", "todos",
            "paste-cache", "file-history", "shell-snapshots",
            "sessions", "session-env", "tasks", "teams",
            "plugins", "skills", "agents", "commands", "hud", "cache",
            "backups"]
    lines = ["## State footprint\n",
             "_sizes under `~/.claude/`_\n",
             "| dir | size | files |",
             "| --- | --- | --- |"]
    grand = 0
    for d in dirs:
        p = CLAUDE_DIR / d
        size, n = folder_size(p)
        if size is None:
            lines.append(f"| `{d}/` | _absent_ | — |")
            continue
        grand += size
        lines.append(f"| `{d}/` | {humansize(size)} | {n} |")
    lines.append(f"| **total tracked** | **{humansize(grand)}** | |")
    return "\n".join(lines) + "\n"


def section_activity(redact: Redact) -> str:
    lines = ["## Activity\n"]
    stats_path = CLAUDE_DIR / ".session-stats.json"
    if stats_path.exists():
        try:
            data = load_json_text(stats_path.read_text())
            sessions = data.get("sessions") if isinstance(data, dict) else None
            n_sessions = len(sessions) if isinstance(sessions, dict) else 0
            now = datetime.now(UTC).timestamp()
            recent = 0
            tool_totals: dict[str, int] = {}
            if not isinstance(sessions, dict):
                session_values: tuple[JsonValue, ...] = ()
            else:
                session_values = tuple(sessions.values())
            for s in session_values:
                if not isinstance(s, dict):
                    continue
                started = s.get("started_at") or s.get("updated_at") or 0
                if isinstance(started, (int, float)) and now - started < 7 * 86400:
                    recent += 1
                tc = s.get("tool_counts")
                if isinstance(tc, dict):
                    for k, v in tc.items():
                        try:
                            count = int(cast(str | bytes | int | float, v))
                            tool_totals[k] = tool_totals.get(k, 0) + count
                        except TypeError, ValueError:
                            pass
            lines.append(f"- total sessions tracked: {n_sessions}")
            lines.append(f"- sessions in last 7 days: {recent}")
            if tool_totals:
                top = sorted(tool_totals.items(), key=lambda x: -x[1])[:8]
                lines.append(
                    "- top tools (by count): "
                    + ", ".join(f"`{k}`:{v}" for k, v in top)
                )
        except Exception as e:
            lines.append(f"- session stats parse error: {redact(str(e))}")
    else:
        lines.append("- `~/.claude/.session-stats.json`: not present")

    history = CLAUDE_DIR / "history.jsonl"
    if history.exists():
        lc = line_count(history)
        size = history.stat().st_size
        lines.append(f"- `history.jsonl`: {humansize(size)}, {lc} entries")
    else:
        lines.append("- `history.jsonl`: not present")

    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.exists():
        try:
            n = sum(1 for p in projects_dir.iterdir()
                    if p.is_dir() and not p.name.startswith("."))
            lines.append(f"- distinct projects under `~/.claude/projects/`: {n}")
        except Exception:
            pass
    return "\n".join(lines) + "\n"


def section_recent_errors(redact: Redact, limit: int = 20) -> str:
    tdir = CLAUDE_DIR / "telemetry"
    if not tdir.exists():
        return "## Recent errors\n\n_`~/.claude/telemetry/` not present_\n"
    files = sorted(tdir.glob("1p_failed_events.*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "## Recent errors\n\n_no `1p_failed_events.*.json` files_\n"
    counts: dict[str, int] = {}
    sample_files = files[:8]
    total_lines = 0
    for f in sample_files:
        try:
            with f.open("r", errors="replace") as fh:
                for line in fh:
                    total_lines += 1
                    try:
                        ev = load_json_text(line)
                    except Exception:
                        continue
                    name: JsonValue = None
                    if isinstance(ev, dict):
                        event_data = ev.get("event_data")
                        name = event_data.get("event_name") \
                            if isinstance(event_data, dict) else None
                        name = name or ev.get("event_name") or ev.get("event_type")
                    if name:
                        name_text = str(name)
                        counts[name_text] = counts.get(name_text, 0) + 1
        except Exception:
            continue
    lines = [
        "## Recent errors\n",
        f"_event-name counts only, no payloads. Sampled {len(sample_files)} of "
        + f"{len(files)} `1p_failed_events.*.json` files ({total_lines} events)._\n",
    ]
    if not counts:
        lines.append("_no recognized event names_")
    else:
        ranked = sorted(counts.items(), key=lambda x: -x[1])[:limit]
        lines.append("| event_name | count |")
        lines.append("| --- | --- |")
        for name, n in ranked:
            lines.append(f"| `{redact(str(name))}` | {n} |")
    return "\n".join(lines) + "\n"


def section_footer(
    _redact: Redact,
    usage_summary: UsageSummary | None,
    context_skipped: bool,
) -> str:
    usage_line = (
        "Run usage: not available (/context skipped)."
        if context_skipped else format_usage_summary(usage_summary)
    )
    return (
        "## About\n\n"
        f"_Generated by `claude-diag` v{__version__}._\n"
        f"{usage_line}\n"
        f"Source: {SCRIPT_URL}\n\n"
        "© engels74\n"
    )


# --------------------------------------------------------------- pastefy --

PASTEFY_DEFAULT_URL = "https://pastefy.app"
PASTEFY_VISIBILITY = "UNLISTED"


class PastefyPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PastefyPublishResult:
    rendered_url: str
    raw_url: str


def pastefy_base_url(url: str) -> str:
    return url.rstrip("/")


def pastefy_host(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or parsed.path or url


def pastefy_api_url(base_url: str) -> str:
    return f"{pastefy_base_url(base_url)}/api/v2/paste"


def _http_post_json(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(getattr(response, "status", response.getcode()))
            response_body = response.read().decode("utf-8", errors="replace")
            return status, response_body
    except urllib.error.HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        raise PastefyPublishError(
            f"HTTP {e.code}: {response_body[:500] or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise PastefyPublishError(f"network error: {e.reason}") from e
    except OSError as e:
        raise PastefyPublishError(f"network error: {e}") from e


def publish_pastefy_report(
    *,
    base_url: str,
    token: str,
    title: str,
    content: str,
    expire_at: str | None,
    http_post: HttpPost = _http_post_json,
) -> PastefyPublishResult:
    payload: JsonObject = {
        "title": title,
        "content": content,
        "visibility": PASTEFY_VISIBILITY,
        "type": "PASTE",
    }
    if expire_at:
        payload["expireAt"] = expire_at

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": f"claude-diag/{__version__}",
    }
    status, response_body = http_post(pastefy_api_url(base_url), body, headers)
    if status < 200 or status >= 300:
        raise PastefyPublishError(
            f"HTTP {status}: {response_body[:500] or 'empty response'}"
        )

    try:
        data = load_json_text(response_body)
    except Exception as e:
        raise PastefyPublishError("malformed JSON response from Pastefy") from e
    if not isinstance(data, dict):
        raise PastefyPublishError("unexpected Pastefy response shape")
    paste = data.get("paste")
    if not isinstance(paste, dict):
        raise PastefyPublishError("Pastefy response did not include paste details")

    raw_url = paste.get("raw_url") or paste.get("rawUrl")
    paste_url = paste.get("url") or paste.get("paste_url") or paste.get("pasteUrl")
    paste_id = paste.get("id")
    if not isinstance(paste_url, str) or not paste_url:
        if isinstance(paste_id, str) and paste_id:
            paste_url = f"{pastefy_base_url(base_url)}/{paste_id}"
    if not isinstance(raw_url, str) or not raw_url:
        raise PastefyPublishError("Pastefy response did not include raw_url")
    if not isinstance(paste_url, str) or not paste_url:
        raise PastefyPublishError("Pastefy response did not include a paste URL")
    return PastefyPublishResult(rendered_url=paste_url, raw_url=raw_url)


def confirm_pastefy_publish(
    *,
    base_url: str,
    title: str,
    report: str,
    saved_path: str | None,
    input_func: Callable[[], str] = input,
    stream: object = sys.stderr,
) -> bool:
    print("[claude-diag] Pastefy publishing requested:", file=stream)
    print(f"  target host: {pastefy_host(base_url)}", file=stream)
    print(f"  visibility: {PASTEFY_VISIBILITY}", file=stream)
    print(f"  title: {title}", file=stream)
    print(f"  report bytes: {len(report.encode('utf-8')):,}", file=stream)
    if saved_path:
        print(f"  local report: {saved_path}", file=stream)
    print("Publish redacted report to pastefy.app? [y/N]:", end="", file=stream, flush=True)
    try:
        answer = input_func()
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def run_pastefy_publish(
    args: "Args",
    report: str,
    saved_path: str | None,
    timestamp: str,
    *,
    input_func: Callable[[], str] = input,
    http_post: HttpPost = _http_post_json,
    stream: object = sys.stderr,
) -> int:
    token = args.pastefy_token
    if not token:
        print(
            "[claude-diag] --publish requires --pastefy-token or "
            "PASTEFY_API_TOKEN.",
            file=stream,
        )
        return 1

    title = f"claude-diag-{timestamp}.md"
    if not args.yes and not confirm_pastefy_publish(
        base_url=args.pastefy_url,
        title=title,
        report=report,
        saved_path=saved_path,
        input_func=input_func,
        stream=stream,
    ):
        print("[claude-diag] Pastefy publish skipped.", file=stream)
        return 0

    try:
        result = publish_pastefy_report(
            base_url=args.pastefy_url,
            token=token,
            title=title,
            content=report,
            expire_at=args.pastefy_expire_at,
            http_post=http_post,
        )
    except PastefyPublishError as e:
        print(f"[claude-diag] Pastefy upload failed: {e}", file=stream)
        return 1

    print(f"[claude-diag] Pastefy URL: {result.rendered_url}", file=stream)
    print(f"[claude-diag] Pastefy raw URL: {result.raw_url}", file=stream)
    return 0


# ----------------------------------------------------------------- self-test --

SELF_TEST_FIXTURE = """
ant-key: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-_xyz
openai: sk-proj-1234567890abcdefghijklmnop
github: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789AbCdEf
aws: AKIAIOSFODNN7EXAMPLE
jwt: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U
email: alice@example.com
ip-public: 8.8.8.8
ip-private: 10.0.0.5 192.168.1.1 127.0.0.1
host: __HOST__
home: /Users/jdoe/code/foo /home/jdoe/bar
project: ~/.claude/projects/-Users-jdoe--secret-thing/abc.jsonl
url: https://api.example.com/v1?api_key=zzz
header: Authorization: Bearer abc.def.ghi
"""

def self_test() -> int:
    r = Redactor()
    fixture = SELF_TEST_FIXTURE.replace("__HOST__", r.hostname or "fakehost.local")
    out = r(fixture)
    forbidden = [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-proj-1234567890abcdefghijklmnop",
        "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789AbCdEf",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIi",
        "alice@example.com",
        "8.8.8.8",
        "/Users/jdoe",
        "/home/jdoe",
        "-Users-jdoe--secret-thing",
        "api_key=zzz",
        "Bearer abc.def.ghi",
    ]
    if r.hostname:
        forbidden.append(r.hostname)
    failures = [s for s in forbidden if s in out]
    expected = [
        "[REDACTED:ANTHROPIC_KEY]",
        "[REDACTED:OPENAI_KEY]",
        "[REDACTED:GITHUB_TOKEN]",
        "[REDACTED:AWS_KEY]",
        "[REDACTED:JWT]",
        "[REDACTED:EMAIL]",
        "[REDACTED:IP]",
        "[REDACTED:HOSTNAME]",
        "[REDACTED:QUERYSTRING]",
        "[REDACTED]",
        "[PROJECT-",
    ]
    missing = [s for s in expected if s not in out]
    keep = ["10.0.0.5", "192.168.1.1", "127.0.0.1"]
    private_dropped = [s for s in keep if s not in out]
    print("=== self-test fixture (input) ===")
    print(fixture)
    print("=== self-test fixture (redacted) ===")
    print(out)
    context_failures = _self_test_context()
    pastefy_failures = _self_test_pastefy()
    print("=== checks ===")
    if failures:
        print(f"FAIL: leaked substrings: {failures}")
    if missing:
        print(f"FAIL: missing redactions: {missing}")
    if private_dropped:
        print(f"FAIL: dropped private IPs (should be kept): {private_dropped}")
    for failure in context_failures:
        print(f"FAIL: {failure}")
    for failure in pastefy_failures:
        print(f"FAIL: {failure}")
    ok = (
        not failures
        and not missing
        and not private_dropped
        and not context_failures
        and not pastefy_failures
    )
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def _fake_claude_script() -> str:
    return """#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ["CLAUDE_DIAG_FAKE_LOG"]
with open(log_path, "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if args == ["--version"]:
    print("fake-claude 1.0")
    raise SystemExit(0)
if args == ["mcp", "list"] or args == ["plugin", "list"]:
    print("none")
    raise SystemExit(0)

mode = os.environ.get("CLAUDE_DIAG_FAKE_MODE", "valid")
if mode == "malformed":
    print("{bad json")
elif mode == "missing":
    print(json.dumps({"result": "missing metadata body"}))
else:
    print(json.dumps({
        "result": "context body",
        "total_cost_usd": 0.0032,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 38000,
            "cache_creation_input_tokens": 2600
        },
        "modelUsage": {
            "claude-sonnet": {
                "total_cost_usd": 0.0032,
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_input_tokens": 38000,
                "cache_creation_input_tokens": 2600
            }
        }
    }))
"""


def _read_fake_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text().splitlines():
        data = load_json_text(line)
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            rows.append([str(item) for item in data])
    return rows


def _self_test_context() -> list[str]:
    failures: list[str] = []
    original_path = os.environ.get("PATH", "")
    original_log = os.environ.get("CLAUDE_DIAG_FAKE_LOG")
    original_mode = os.environ.get("CLAUDE_DIAG_FAKE_MODE")
    r = Redactor()

    with tempfile.TemporaryDirectory(prefix="claude-diag-test-") as tmp:
        tmp_path = Path(tmp)
        fake = tmp_path / "claude"
        log = tmp_path / "claude.log"
        _ = fake.write_text(_fake_claude_script())
        fake.chmod(0o755)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{original_path}"
        os.environ["CLAUDE_DIAG_FAKE_LOG"] = str(log)
        try:
            os.environ["CLAUDE_DIAG_FAKE_MODE"] = "valid"
            context, usage = section_context(r, None, 5, False)
            rows = _read_fake_log(log)
            if rows[-1:] != [["-p", "/context", "--output-format", "json"]]:
                failures.append(f"default /context command mismatch: {rows[-1:]}")
            if "context body" not in context:
                failures.append("valid JSON result was not used as /context body")
            footer = section_footer(r, usage, False)
            if "Run usage: approx ~$0.0032, 42.1K tokens" not in footer:
                failures.append("valid JSON usage was not shown in footer")
            if "claude-sonnet" not in footer:
                failures.append("modelUsage breakdown was not shown in footer")

            _ = section_context(r, "sonnet", 5, False)
            rows = _read_fake_log(log)
            expected_model_cmd = [
                "-p", "/context", "--output-format", "json", "--model", "sonnet",
            ]
            if rows[-1:] != [expected_model_cmd]:
                failures.append(f"model /context command mismatch: {rows[-1:]}")

            os.environ["CLAUDE_DIAG_FAKE_MODE"] = "malformed"
            context, usage = section_context(r, None, 5, False)
            if "{bad json" not in context or usage is not None:
                failures.append("malformed JSON did not fall back to raw output")

            os.environ["CLAUDE_DIAG_FAKE_MODE"] = "missing"
            context, usage = section_context(r, None, 5, False)
            if "missing metadata body" not in context or usage is not None:
                failures.append("missing cost/usage metadata was not handled cleanly")

            footer = section_footer(r, None, True)
            if "Run usage: not available (/context skipped)." not in footer:
                failures.append("--no-context footer did not explain skipped usage")
        finally:
            os.environ["PATH"] = original_path
            if original_log is None:
                _ = os.environ.pop("CLAUDE_DIAG_FAKE_LOG", None)
            else:
                os.environ["CLAUDE_DIAG_FAKE_LOG"] = original_log
            if original_mode is None:
                _ = os.environ.pop("CLAUDE_DIAG_FAKE_MODE", None)
            else:
                os.environ["CLAUDE_DIAG_FAKE_MODE"] = original_mode

    return failures


def _test_args() -> "Args":
    args = Args()
    args.publish = True
    args.pastefy_url = PASTEFY_DEFAULT_URL
    args.pastefy_token = "test-token"
    args.pastefy_expire_at = None
    args.yes = False
    return args


def _self_test_pastefy() -> list[str]:
    failures: list[str] = []
    report = "# redacted\n"
    timestamp = "20260101T000000Z"
    captured: list[tuple[str, JsonObject, dict[str, str]]] = []

    def fake_success(
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, str]:
        data = load_json_text(body.decode("utf-8"))
        if not isinstance(data, dict):
            failures.append("Pastefy request body was not a JSON object")
            data = {}
        captured.append((url, data, headers))
        return 200, json.dumps({
            "paste": {
                "id": "abc123",
                "raw_url": "https://pastefy.app/api/v2/paste/abc123/raw",
            },
        })

    for answer in ("", "n", "maybe"):
        sink = io.StringIO()
        if confirm_pastefy_publish(
            base_url=PASTEFY_DEFAULT_URL,
            title=f"claude-diag-{timestamp}.md",
            report=report,
            saved_path="/tmp/claude-diag-test.md",
            input_func=lambda answer=answer: answer,
            stream=sink,
        ):
            failures.append(f"confirmation accepted {answer!r}")
        if "Publish redacted report to pastefy.app? [y/N]:" not in sink.getvalue():
            failures.append("confirmation prompt text changed")

    for answer in ("y", "yes", " YES "):
        if not confirm_pastefy_publish(
            base_url=PASTEFY_DEFAULT_URL,
            title=f"claude-diag-{timestamp}.md",
            report=report,
            saved_path=None,
            input_func=lambda answer=answer: answer,
            stream=io.StringIO(),
        ):
            failures.append(f"confirmation rejected {answer!r}")

    args = _test_args()
    args.yes = True
    rc = run_pastefy_publish(
        args,
        report,
        "/tmp/claude-diag-test.md",
        timestamp,
        input_func=lambda: (_ for _ in ()).throw(
            AssertionError("--yes should not prompt")
        ),
        http_post=fake_success,
        stream=io.StringIO(),
    )
    if rc != 0:
        failures.append("--yes Pastefy publish did not succeed")
    if not captured:
        failures.append("--yes Pastefy publish did not upload")
    else:
        url, body, headers = captured[-1]
        if url != "https://pastefy.app/api/v2/paste":
            failures.append(f"Pastefy upload URL mismatch: {url}")
        if body.get("title") != f"claude-diag-{timestamp}.md":
            failures.append("Pastefy title did not keep .md extension")
        if body.get("content") != report:
            failures.append("Pastefy content did not match report")
        if body.get("visibility") != PASTEFY_VISIBILITY:
            failures.append("Pastefy visibility was not UNLISTED")
        if body.get("type") != "PASTE":
            failures.append("Pastefy type was not PASTE")
        if headers.get("Authorization") != "Bearer test-token":
            failures.append("Pastefy Authorization header missing")

    args = _test_args()
    args.pastefy_expire_at = "2026-12-31T00:00:00Z"
    captured.clear()
    _ = publish_pastefy_report(
        base_url=args.pastefy_url,
        token=args.pastefy_token or "",
        title=f"claude-diag-{timestamp}.md",
        content=report,
        expire_at=args.pastefy_expire_at,
        http_post=fake_success,
    )
    if not captured or captured[-1][1].get("expireAt") != args.pastefy_expire_at:
        failures.append("Pastefy expireAt was not passed through")

    args = _test_args()
    args.pastefy_token = None
    rc = run_pastefy_publish(
        args,
        report,
        None,
        timestamp,
        input_func=lambda: "y",
        http_post=fake_success,
        stream=io.StringIO(),
    )
    if rc == 0:
        failures.append("Pastefy publish without token succeeded")

    def fake_http_error(
        _url: str,
        _body: bytes,
        _headers: dict[str, str],
    ) -> tuple[int, str]:
        return 500, "server error"

    try:
        _ = publish_pastefy_report(
            base_url=PASTEFY_DEFAULT_URL,
            token="test-token",
            title=f"claude-diag-{timestamp}.md",
            content=report,
            expire_at=None,
            http_post=fake_http_error,
        )
        failures.append("Pastefy HTTP error did not fail")
    except PastefyPublishError:
        pass

    def fake_malformed(
        _url: str,
        _body: bytes,
        _headers: dict[str, str],
    ) -> tuple[int, str]:
        return 200, "{bad json"

    try:
        _ = publish_pastefy_report(
            base_url=PASTEFY_DEFAULT_URL,
            token="test-token",
            title=f"claude-diag-{timestamp}.md",
            content=report,
            expire_at=None,
            http_post=fake_malformed,
        )
        failures.append("Pastefy malformed JSON response did not fail")
    except PastefyPublishError:
        pass

    sink = io.StringIO()
    args = _test_args()
    args.yes = True
    rc = run_pastefy_publish(
        args,
        report,
        None,
        timestamp,
        http_post=fake_success,
        stream=sink,
    )
    output = sink.getvalue()
    if rc != 0 or "Pastefy URL: https://pastefy.app/abc123" not in output:
        failures.append("Pastefy rendered URL was not printed")
    if "Pastefy raw URL: https://pastefy.app/api/v2/paste/abc123/raw" not in output:
        failures.append("Pastefy raw URL was not printed")

    return failures


# ---------------------------------------------------------------------- cli --

class Args(argparse.Namespace):
    output: str | None = None
    include_memories: bool = False
    no_context: bool = False
    no_save: bool = False
    model: str | None = None
    timeout: int = 45
    publish: bool = False
    pastefy_url: str = PASTEFY_DEFAULT_URL
    pastefy_token: str | None = None
    pastefy_expire_at: str | None = None
    yes: bool = False
    self_test: bool = False
    debug: bool = False


def parse_args(argv: Sequence[str]) -> Args:
    p = argparse.ArgumentParser(
        prog="claude-diag",
        description="Generate a redacted Claude Code diagnostic report.",
    )
    _ = p.add_argument(
        "--output",
        help="Path to save the report. "
        + "Default: /tmp/claude-diag-<UTC-timestamp>.md",
    )
    _ = p.add_argument(
        "--include-memories",
        action="store_true",
        help="Dump memory & agent file bodies (still redacted).",
    )
    _ = p.add_argument(
        "--no-context",
        action="store_true",
        help="Skip the `claude -p /context` subprocess call.",
    )
    _ = p.add_argument(
        "--no-save",
        action="store_true",
        help="Print to stdout only; do not write a file.",
    )
    _ = p.add_argument(
        "--model",
        help=(
            "Optional model override for the /context call. "
            "Default: use Claude CLI settings."
        ),
    )
    _ = p.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Per-subprocess timeout in seconds (default: 45).",
    )
    _ = p.add_argument(
        "--publish",
        action="store_true",
        help="Offer to publish the redacted report as an unlisted Pastefy paste.",
    )
    _ = p.add_argument(
        "--pastefy-url",
        default=PASTEFY_DEFAULT_URL,
        help=f"Pastefy base URL (default: {PASTEFY_DEFAULT_URL}).",
    )
    _ = p.add_argument(
        "--pastefy-token",
        default=os.environ.get("PASTEFY_API_TOKEN"),
        help="Pastefy API token. Default: PASTEFY_API_TOKEN.",
    )
    _ = p.add_argument(
        "--pastefy-expire-at",
        help="Optional Pastefy expiration timestamp passed as expireAt.",
    )
    _ = p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the Pastefy publish confirmation prompt.",
    )
    _ = p.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    _ = p.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    _ = p.add_argument(
        "--version",
        action="version",
        version=f"claude-diag {__version__}",
    )
    return p.parse_args(argv, namespace=Args())


def build_report(args: Args, redact: Redact) -> str:
    sections = [
        section_header(),
        section_environment(redact, args.timeout),
        section_claude(redact, args.timeout),
    ]
    context_section, usage_summary = section_context(
        redact, args.model, args.timeout, args.no_context
    )
    sections.extend([
        context_section,
        section_global_settings(redact),
        section_project_settings(redact),
        section_mcp(redact, args.timeout),
        section_plugins(redact, args.timeout),
        section_skills(redact),
        section_agents(redact, args.include_memories),
        section_commands(redact),
        section_memories(redact, args.include_memories),
        section_hooks(redact),
        section_state_footprint(redact),
        section_activity(redact),
        section_recent_errors(redact),
        section_footer(redact, usage_summary, args.no_context),
    ])
    return "\n".join(sections)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.self_test:
        return self_test()

    redact = Redactor()
    if args.debug:
        print("[debug] running self-test fixture first", file=sys.stderr)
        _ = self_test()

    report = build_report(args, redact)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    saved_path: str | None = None

    if not args.no_save:
        out_path = args.output
        if not out_path:
            out_path = f"/tmp/claude-diag-{ts}.md"
        try:
            _ = Path(out_path).write_text(report)
            saved_path = out_path
            print(
                f"[claude-diag] wrote {out_path} "
                + f"({len(report):,} bytes)",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[claude-diag] failed to write {out_path}: {e}",
                  file=sys.stderr)

    _ = sys.stdout.write(report)
    if not report.endswith("\n"):
        _ = sys.stdout.write("\n")
    if args.publish:
        return run_pastefy_publish(args, report, saved_path, ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
