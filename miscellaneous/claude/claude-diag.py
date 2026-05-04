#!/usr/bin/env python3
"""claude-diag — generate a redacted diagnostic report for Claude Code.

Single-file, stdlib-only. Safe to pipe over curl:

    curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/main/claude-diag.py | python3 -

See --help for flags.
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

HOME = Path.home()
USERNAME = HOME.name
CLAUDE_DIR = HOME / ".claude"


# ---------------------------------------------------------------- redactor --

class Redactor:
    PATTERNS = [
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
    ]
    IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    URL_QS = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
    AUTH_HEADER = re.compile(
        r"(?i)\b(Authorization|X-API-Key|X-Auth-Token|Cookie)(\s*[:=]\s*)\S+"
    )
    USERS_PATH = re.compile(r"/Users/[^/\s:\"',]+")
    HOME_PATH = re.compile(r"/home/[^/\s:\"',]+")
    PROJECT_PATH = re.compile(r"~/\.claude/projects/([^/\s\"',]+)")

    def __init__(self):
        self.hostname = socket.gethostname() or ""
        self.short_hostname = self.hostname.split(".")[0] if self.hostname else ""
        self._project_ids = {}
        self._next_id = 1

    def _ip(self, m):
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

    def _project(self, m):
        name = m.group(1)
        if name not in self._project_ids:
            self._project_ids[name] = self._next_id
            self._next_id += 1
        return f"~/.claude/projects/[PROJECT-{self._project_ids[name]}]"

    def __call__(self, s):
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
    def project_count(self):
        return len(self._project_ids)


# ----------------------------------------------------------------- helpers --

def run(cmd, timeout):
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


def safe_read(path, max_bytes=512 * 1024):
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


def folder_size(path):
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


def humansize(n):
    if n is None:
        return "n/a"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def line_count(path):
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def details(title, body, open_=False):
    o = " open" if open_ else ""
    return f"<details{o}>\n<summary>{title}</summary>\n\n{body}\n\n</details>\n"


def code_block(s, lang=""):
    return f"```{lang}\n{s.rstrip()}\n```"


# ---------------------------------------------------------------- sections --

def section_header():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"# Claude Code diagnostic report\n\n"
        f"- Generator: `claude-diag` v{__version__}\n"
        f"- Generated: {now}\n"
        f"- Redaction: secrets, emails, hostnames, public IPs, "
        f"and user paths are scrubbed before output. "
        f"See the footer for what *isn't* redacted.\n"
    )


def section_environment(redact, timeout):
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


def section_claude(redact, timeout):
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


def section_context(redact, model, timeout, skip):
    if skip:
        return "## `/context` output\n\n_skipped via `--no-context`_\n"
    cmd = [
        "claude", "-p", "/context",
        "--model", model,
    ]
    out, code = run(cmd, timeout)
    if code != 0 and out in ("[not installed]", "[command timed out]"):
        return f"## `/context` output\n\n_{out}_\n"
    body = redact(out) if out else "_no output_"
    return (
        f"## `/context` output\n\n"
        f"_Captured via `claude -p /context --model {model}` "
        f"(exit code {code}). Paths and secrets redacted._\n\n"
        + code_block(body, "")
        + "\n"
    )


def _settings_summary(data, redact):
    lines = []
    lines.append(f"- Top-level keys: `{', '.join(sorted(data.keys())) or '(none)'}`")
    env = data.get("env", {})
    if isinstance(env, dict) and env:
        lines.append(f"- env vars (keys only, {len(env)}):")
        for k in sorted(env.keys()):
            lines.append(f"  - `{redact(k)}`")
    hooks = data.get("hooks", {})
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
    perms = data.get("permissions", {})
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
    plugins = data.get("enabledPlugins", {})
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


def section_global_settings(redact):
    out = ["## Global settings (`~/.claude/settings.json`)\n"]
    for fname in ("settings.json", "settings.local.json"):
        path = CLAUDE_DIR / fname
        if not path.exists():
            out.append(f"### `~/.claude/{fname}`\n\n_not present_\n")
            continue
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            out.append(f"### `~/.claude/{fname}`\n\n_parse error: {redact(str(e))}_\n")
            continue
        out.append(f"### `~/.claude/{fname}`\n\n" + _settings_summary(data, redact) + "\n")
    return "\n".join(out)


def section_project_settings(redact):
    cwd = Path.cwd()
    candidates = [
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
            data = json.loads(path.read_text())
        except Exception as e:
            out.append(f"### `{label}`\n\n_parse error: {redact(str(e))}_\n")
            continue
        if label.endswith(".mcp.json"):
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            out.append(f"### `{label}`\n\n- mcpServers: {len(servers)} "
                       f"({', '.join(sorted(servers.keys()))})\n")
        else:
            out.append(f"### `{label}`\n\n" + _settings_summary(data, redact) + "\n")
    if not found_any:
        out.append("_no project-level Claude Code config in `$PWD`_\n")
    return "\n".join(out)


def section_mcp(redact, timeout):
    out, code = run(["claude", "mcp", "list"], timeout)
    body = redact(out) if out else "_no output_"
    return (
        f"## MCP servers\n\n"
        f"_via `claude mcp list` (exit code {code})._\n\n"
        + code_block(body, "")
        + "\n"
    )


def section_plugins(redact, timeout):
    out, code = run(["claude", "plugin", "list"], timeout)
    body = redact(out) if out else "_no output_"
    parts = [
        f"## Plugins\n\n_via `claude plugin list` (exit code {code})._\n",
        code_block(body, ""),
    ]
    plugin_json = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    if plugin_json.exists():
        try:
            data = json.loads(plugin_json.read_text())
            count = sum(len(v) if isinstance(v, dict) else 0 for v in data.values()) \
                if isinstance(data, dict) else 0
            parts.append(f"\n- `installed_plugins.json` size: "
                         f"{humansize(plugin_json.stat().st_size)}, "
                         f"~{count} entries\n")
        except Exception as e:
            parts.append(f"\n- `installed_plugins.json` parse error: {redact(str(e))}\n")
    return "\n".join(parts)


def _list_dir_entries(path):
    try:
        return sorted(p for p in Path(path).iterdir() if not p.name.startswith("."))
    except Exception:
        return []


def section_skills(redact):
    skills_dir = CLAUDE_DIR / "skills"
    if not skills_dir.exists():
        return "## Skills\n\n_`~/.claude/skills/` not present_\n"
    entries = _list_dir_entries(skills_dir)
    lines = [f"## Skills\n\n_{len(entries)} entries in `~/.claude/skills/`_\n"]
    rows = []
    for e in entries:
        if e.is_dir():
            total, n = folder_size(e)
            rows.append(f"- `{redact(e.name)}/` — {humansize(total)}, {n} files")
        else:
            rows.append(f"- `{redact(e.name)}` — {humansize(e.stat().st_size)}")
    if rows:
        lines.append("\n".join(rows))
    return "\n".join(lines) + "\n"


def section_agents(redact, include_memories):
    agents_dir = CLAUDE_DIR / "agents"
    if not agents_dir.exists():
        return "## Agents\n\n_`~/.claude/agents/` not present_\n"
    entries = sorted(p for p in agents_dir.glob("*.md"))
    lines = [f"## Agents\n\n_{len(entries)} agent files in `~/.claude/agents/`_\n"]
    rows = []
    bodies = []
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


def section_commands(redact):
    cmd_dir = CLAUDE_DIR / "commands"
    if not cmd_dir.exists():
        return "## Slash commands\n\n_`~/.claude/commands/` not present_\n"
    entries = sorted(p for p in cmd_dir.iterdir() if not p.name.startswith("."))
    lines = [f"## Slash commands\n\n_{len(entries)} entries in `~/.claude/commands/`_\n"]
    rows = []
    for e in entries:
        suffix = "/" if e.is_dir() else ""
        rows.append(f"- `{redact(e.name)}{suffix}`")
    if rows:
        lines.append("\n".join(rows))
    return "\n".join(lines) + "\n"


def section_memories(redact, include_memories):
    targets = [
        ("~/.claude/CLAUDE.md", CLAUDE_DIR / "CLAUDE.md"),
        ("~/.claude/AGENTS.md", CLAUDE_DIR / "AGENTS.md"),
        ("~/.claude/MEMORIES.md", CLAUDE_DIR / "MEMORIES.md"),
        ("~/.claude/RTK.md", CLAUDE_DIR / "RTK.md"),
        ("$PWD/CLAUDE.md", Path.cwd() / "CLAUDE.md"),
        ("$PWD/AGENTS.md", Path.cwd() / "AGENTS.md"),
    ]
    lines = ["## Memory files\n"]
    bodies = []
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
        lines.append(f"- `{label}`: {humansize(size)}, {n_lines} lines, "
                     f"{n_imports} `@imports`")
        if include_memories:
            bodies.append(f"### `{label}`\n\n"
                          + code_block(redact(text), "markdown"))
    if bodies:
        lines.append("\n" + details("Memory file bodies (`--include-memories`)",
                                    "\n\n".join(bodies)))
    return "\n".join(lines) + "\n"


def section_hooks(redact):
    settings_path = CLAUDE_DIR / "settings.json"
    if not settings_path.exists():
        return "## Hooks\n\n_no `~/.claude/settings.json`_\n"
    try:
        data = json.loads(settings_path.read_text())
    except Exception as e:
        return f"## Hooks\n\n_parse error: {redact(str(e))}_\n"
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict) or not hooks:
        return "## Hooks\n\n_no hooks configured_\n"
    lines = ["## Hooks\n",
             "_Event names and counts only — command bodies are never printed._\n"]
    for event in sorted(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            lines.append(f"- `{event}`: malformed")
            continue
        matchers = []
        cmd_count = 0
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            matcher = ent.get("matcher", "*")
            hs = ent.get("hooks", [])
            n = len(hs) if isinstance(hs, list) else 0
            cmd_count += n
            matchers.append(f"`{redact(str(matcher))}`×{n}")
        lines.append(f"- `{event}`: {len(entries)} matchers, "
                     f"{cmd_count} commands — {', '.join(matchers)}")
    return "\n".join(lines) + "\n"


def section_state_footprint(redact):
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


def section_activity(redact):
    lines = ["## Activity\n"]
    stats_path = CLAUDE_DIR / ".session-stats.json"
    if stats_path.exists():
        try:
            data = json.loads(stats_path.read_text())
            sessions = data.get("sessions", {})
            n_sessions = len(sessions) if isinstance(sessions, dict) else 0
            now = datetime.now(timezone.utc).timestamp()
            recent = 0
            tool_totals = {}
            for s in (sessions.values() if isinstance(sessions, dict) else []):
                if not isinstance(s, dict):
                    continue
                started = s.get("started_at") or s.get("updated_at") or 0
                if isinstance(started, (int, float)) and now - started < 7 * 86400:
                    recent += 1
                tc = s.get("tool_counts", {})
                if isinstance(tc, dict):
                    for k, v in tc.items():
                        try:
                            tool_totals[k] = tool_totals.get(k, 0) + int(v)
                        except (TypeError, ValueError):
                            pass
            lines.append(f"- total sessions tracked: {n_sessions}")
            lines.append(f"- sessions in last 7 days: {recent}")
            if tool_totals:
                top = sorted(tool_totals.items(), key=lambda x: -x[1])[:8]
                lines.append("- top tools (by count): "
                             + ", ".join(f"`{k}`:{v}" for k, v in top))
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


def section_recent_errors(redact, limit=20):
    tdir = CLAUDE_DIR / "telemetry"
    if not tdir.exists():
        return "## Recent errors\n\n_`~/.claude/telemetry/` not present_\n"
    files = sorted(tdir.glob("1p_failed_events.*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "## Recent errors\n\n_no `1p_failed_events.*.json` files_\n"
    counts = {}
    sample_files = files[:8]
    total_lines = 0
    for f in sample_files:
        try:
            with f.open("r", errors="replace") as fh:
                for line in fh:
                    total_lines += 1
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    name = None
                    if isinstance(ev, dict):
                        name = (ev.get("event_data", {}) or {}).get("event_name") \
                            if isinstance(ev.get("event_data"), dict) else None
                        name = name or ev.get("event_name") or ev.get("event_type")
                    if name:
                        counts[name] = counts.get(name, 0) + 1
        except Exception:
            continue
    lines = [
        "## Recent errors\n",
        f"_event-name counts only, no payloads. Sampled {len(sample_files)} of "
        f"{len(files)} `1p_failed_events.*.json` files ({total_lines} events)._\n",
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


def section_footer(redact):
    return (
        "## What this report does and doesn't redact\n\n"
        "**Redacted:** Anthropic / OpenAI / GitHub / AWS / JWT tokens, "
        "email addresses, public IPv4 addresses, your hostname, your "
        "username in paths (`/Users/<u>/` → `~/`), project-directory names "
        "(replaced with `[PROJECT-N]`), URL query strings, and "
        "`Authorization`/`X-API-Key`/`Cookie` header values. "
        "Hook command bodies and memory file contents are never printed by "
        "default; settings.json `env` values are stripped (keys preserved).\n\n"
        "**Not redacted:** plugin names, MCP server names (the names "
        "themselves — not their URL secrets), skill names, agent file names, "
        "settings.json keys, feature-flag environment-variable names, "
        "Node/npm/Python/OS versions.\n\n"
        f"**Tip:** if `claude doctor` is relevant to your issue, paste its "
        f"output separately — this script intentionally does not run it "
        f"(it can hang on a network probe).\n\n"
        f"_Generated by `claude-diag` v{__version__}. "
        f"Source: https://github.com/<user>/<repo>_\n"
    )


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

def self_test():
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
    print("=== checks ===")
    if failures:
        print(f"FAIL: leaked substrings: {failures}")
    if missing:
        print(f"FAIL: missing redactions: {missing}")
    if private_dropped:
        print(f"FAIL: dropped private IPs (should be kept): {private_dropped}")
    ok = not failures and not missing and not private_dropped
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------- cli --

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="claude-diag",
        description="Generate a redacted Claude Code diagnostic report.",
    )
    p.add_argument("--output", help="Path to save the report. "
                   "Default: /tmp/claude-diag-<UTC-timestamp>.md")
    p.add_argument("--include-memories", action="store_true",
                   help="Dump memory & agent file bodies (still redacted).")
    p.add_argument("--no-context", action="store_true",
                   help="Skip the `claude -p /context` subprocess call.")
    p.add_argument("--no-save", action="store_true",
                   help="Print to stdout only; do not write a file.")
    p.add_argument("--model", default="haiku",
                   help="Model used for the /context call (default: haiku).")
    p.add_argument("--timeout", type=int, default=45,
                   help="Per-subprocess timeout in seconds (default: 45).")
    p.add_argument("--self-test", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--debug", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--version", action="version",
                   version=f"claude-diag {__version__}")
    return p.parse_args(argv)


def build_report(args, redact):
    sections = [
        section_header(),
        section_environment(redact, args.timeout),
        section_claude(redact, args.timeout),
        section_context(redact, args.model, args.timeout, args.no_context),
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
        section_footer(redact),
    ]
    return "\n".join(sections)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.self_test:
        return self_test()

    redact = Redactor()
    if args.debug:
        print("[debug] running self-test fixture first", file=sys.stderr)
        self_test()

    report = build_report(args, redact)

    if not args.no_save:
        out_path = args.output
        if not out_path:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_path = f"/tmp/claude-diag-{ts}.md"
        try:
            Path(out_path).write_text(report)
            print(f"[claude-diag] wrote {out_path} "
                  f"({len(report):,} bytes)", file=sys.stderr)
        except Exception as e:
            print(f"[claude-diag] failed to write {out_path}: {e}",
                  file=sys.stderr)

    sys.stdout.write(report)
    if not report.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
