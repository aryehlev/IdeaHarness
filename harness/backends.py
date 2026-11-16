"""Agent backends: one adapter per harness.

A backend runs one agent session (system prompt + user prompt, optionally with the
research tools) and returns an Outcome with the reply text and/or structured output.

    claude[:model]            Claude Code            (claude -p)
    codex[:model]             OpenAI Codex CLI       (codex exec)
    gemini[:model]            Gemini CLI             (gemini -p)
    opencode[:provider/model] OpenCode               (opencode run), e.g. opencode:deepseek/deepseek-chat
    deepseek[:model]          DeepSeek API           (built-in agent loop)
    openrouter:model          OpenRouter API         (built-in agent loop)
    openai:model              OpenAI API             (built-in agent loop)
    ollama:model              local Ollama           (built-in agent loop)
    lmstudio:model            local LM Studio        (built-in agent loop)
    api:model                 any OpenAI-compatible endpoint, with --api-base
    cmd:<command>             any other CLI; prompt on stdin or in {prompt_file}
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import tools as research
from .schemas import portable_schema
from .ui import clip

MCP_SERVER = pathlib.Path(__file__).resolve().parent / "mcp_server.py"
SERVER = "research"
Show = Callable[[str, str], None]


class BackendError(RuntimeError):
    pass


@dataclass
class Options:
    effort: str | None = None
    budget: float | None = None
    timeout_min: float = 20
    api_base: str | None = None
    api_key_env: str | None = None
    api_max_turns: int = 40
    num_ctx: int = 32768


@dataclass
class Outcome:
    text: str = ""
    structured: dict | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    turns: int | None = None
    error: str | None = None
    seconds: float = 0.0

    def add_tokens(self, n) -> None:
        if n:
            self.tokens = (self.tokens or 0) + int(n)

    def add_cost(self, c) -> None:
        if c is not None:
            self.cost_usd = (self.cost_usd or 0.0) + float(c)


def gh_config_dir() -> str:
    """Where gh keeps its login, so it still works when a harness runs with a different XDG_CONFIG_HOME."""
    if os.environ.get("GH_CONFIG_DIR"):
        return os.environ["GH_CONFIG_DIR"]
    base = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
    return str(pathlib.Path(base) / "gh")


def mcp_entry() -> dict:
    return {"command": sys.executable, "args": [str(MCP_SERVER)]}


def show_tool(name: str, args, show: Show) -> None:
    """Print one tool call in a harness-independent way."""
    name = str(name or "")
    for prefix in (f"mcp__{SERVER}__", f"{SERVER}__", f"{SERVER}_", f"{SERVER}."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    args = args if isinstance(args, dict) else {}
    low = name.lower()
    if low in ("structuredoutput", "todowrite", "write_todos"):
        return
    if low in ("github_search_repos", "github_repo"):
        show("github", args.get("query") or args.get("repo") or "")
    elif low == "github_search_issues":
        show("issues", args.get("query", ""))
    elif low == "hn_search":
        show("hn", args.get("query", ""))
    elif low == "package_search":
        show(str(args.get("registry", "pkg")), args.get("query", ""))
    elif "search" in low and ("web" in low or low in ("websearch", "google_web_search")):
        show("search", args.get("query") or args.get("q") or json.dumps(args))
    elif "fetch" in low:
        show("fetch", args.get("url") or args.get("prompt") or json.dumps(args))
    elif low in ("bash", "shell", "run_shell_command", "exec_command", "command_execution"):
        show("$", args.get("command") or json.dumps(args))
    else:
        show("tool", f"{name} {clip(json.dumps(args), 100)}")


class Backend:
    kind = ""
    native_schema = False

    def __init__(self, model: str | None, opts: Options):
        self.model = model or None
        self.opts = opts

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.model}" if self.model else self.kind

    def check(self) -> str | None:
        """None when the backend looks usable, otherwise the reason it isn't."""
        return None

    def run(self, *, system: str, prompt: str, schema: dict | None, research_tools: bool,
            transcript: pathlib.Path, show: Show) -> Outcome:
        raise NotImplementedError


# ---------------------------------------------------------------- CLI harnesses

class CLIBackend(Backend):
    """Runs a harness CLI as a subprocess in an empty temporary directory."""

    binary = ""

    def check(self) -> str | None:
        return None if shutil.which(self.binary) else f"`{self.binary}` is not on PATH"

    def invocation(self, *, system, prompt, schema, research_tools, workdir: pathlib.Path):
        """Return (argv, extra_env, stdin_text)."""
        raise NotImplementedError

    def on_line(self, line: str, event: dict | None, out: Outcome, show: Show) -> None:
        pass

    def on_stderr(self, line: str, out: Outcome) -> bool:
        """Inspect one stderr line; return True to stop the session (e.g. a fatal error the CLI keeps retrying)."""
        return False

    def finish(self, out: Outcome, workdir: pathlib.Path) -> None:
        pass

    def run(self, *, system, prompt, schema, research_tools, transcript, show) -> Outcome:
        out = Outcome()
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="ideaharness-"))
        started = time.time()
        try:
            argv, env, stdin = self.invocation(system=system, prompt=prompt, schema=schema,
                                               research_tools=research_tools, workdir=workdir)
            # Own process group, so a timeout also kills what the harness spawned (wrapper scripts
            # launching native binaries, MCP servers); otherwise orphans keep the pipe open forever.
            proc = subprocess.Popen(
                argv, cwd=workdir, env={**os.environ, **(env or {})},
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                start_new_session=True,
            )
            timed_out = threading.Event()

            def kill_group():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()

            def kill():
                timed_out.set()
                kill_group()

            timer = threading.Timer(self.opts.timeout_min * 60, kill)
            timer.start()
            err_chunks: list[str] = []

            def read_stderr():
                for err_line in proc.stderr:
                    err_chunks.append(err_line)
                    if self.on_stderr(err_line, out):
                        kill_group()

            err_thread = threading.Thread(target=read_stderr, daemon=True)
            err_thread.start()
            try:
                if stdin is not None:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                with transcript.open("w") as raw:
                    for line in proc.stdout:
                        raw.write(line)
                        stripped = line.strip()
                        event = None
                        if stripped.startswith("{"):
                            try:
                                event = json.loads(stripped)
                            except json.JSONDecodeError:
                                event = None
                        self.on_line(line, event if isinstance(event, dict) else None, out, show)
                proc.wait()
            except KeyboardInterrupt:
                kill_group()
                raise
            finally:
                timer.cancel()
                if proc.poll() is None:
                    kill_group()
                else:  # the harness exited; reap anything it left behind in its group
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            err_thread.join(timeout=2)
            stderr = "".join(err_chunks)
            if stderr:
                transcript.with_name(transcript.name + ".stderr").write_text(stderr)
            self.finish(out, workdir)
            if timed_out.is_set():
                out.error = f"timed out after {self.opts.timeout_min:g} min"
            elif proc.returncode not in (0, None) and out.structured is None and not out.text.strip() and not out.error:
                out.error = f"exit code {proc.returncode}: {clip(stderr or 'no output', 300)}"
        except FileNotFoundError as exc:
            out.error = f"could not start {self.binary}: {exc}"
        finally:
            out.seconds = time.time() - started
            shutil.rmtree(workdir, ignore_errors=True)
        return out


class ClaudeBackend(CLIBackend):
    kind = binary = "claude"
    native_schema = True

    def invocation(self, *, system, prompt, schema, research_tools, workdir):
        argv = [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence",
            "--setting-sources=",             # no user/project settings: no plugins, hooks or user MCP servers
            "--system-prompt", system,
            "--permission-mode", "dontAsk",   # anything not allowed below is denied, never prompted
        ]
        if schema:
            argv += ["--json-schema", json.dumps(schema)]
        if research_tools:
            cfg = workdir / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {SERVER: mcp_entry()}}))
            argv += ["--tools", "WebSearch,WebFetch", "--mcp-config", str(cfg), "--strict-mcp-config",
                     "--allowedTools", "WebSearch", "WebFetch", f"mcp__{SERVER}"]
        else:
            argv += ["--tools", ""]
        if self.model:
            argv += ["--model", self.model]
        if self.opts.effort:
            argv += ["--effort", self.opts.effort]
        if self.opts.budget:
            argv += ["--max-budget-usd", str(self.opts.budget)]
        return argv, None, prompt

    def on_line(self, line, event, out, show):
        if not event:
            return
        etype = event.get("type")
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    show_tool(block.get("name"), block.get("input"), show)
        elif etype == "system" and event.get("subtype") == "permission_denied":
            show("denied", "a tool outside the allowed research tools")
        elif etype == "result":
            out.structured = event.get("structured_output") if isinstance(event.get("structured_output"), dict) else None
            out.text = event.get("result") or ""
            out.add_cost(event.get("total_cost_usd"))
            usage = event.get("usage") or {}
            out.add_tokens((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                           + (usage.get("cache_creation_input_tokens") or 0))
            out.turns = event.get("num_turns")
            if event.get("is_error"):
                out.error = f"{event.get('subtype')}: {clip(event.get('result') or '', 200)}"


class CodexBackend(CLIBackend):
    kind = binary = "codex"
    native_schema = True

    def check(self):
        if not shutil.which("codex"):
            return "`codex` is not on PATH"
        try:
            proc = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"codex does not run: {exc}"
        if proc.returncode != 0:
            return ("codex is installed but does not run (reinstall with `npm install -g @openai/codex` "
                    f"or `brew install codex`): {clip(proc.stderr or proc.stdout, 120)}")
        return None

    def invocation(self, *, system, prompt, schema, research_tools, workdir):
        last = workdir / "last_message.txt"
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only",
                "-C", str(workdir), "-o", str(last), "-c", 'approval_policy="never"']
        if schema:
            schema_file = workdir / "schema.json"
            schema_file.write_text(json.dumps(portable_schema(schema)))
            argv += ["--output-schema", str(schema_file)]
        if research_tools:
            entry = mcp_entry()
            argv += ["-c", 'web_search="live"',
                     "-c", f"mcp_servers.{SERVER}.command={json.dumps(entry['command'])}",
                     "-c", f"mcp_servers.{SERVER}.args={json.dumps(entry['args'])}"]
        else:
            argv += ["-c", 'web_search="disabled"']
        if self.model:
            argv += ["-m", self.model]
        if self.opts.effort:
            effort = "xhigh" if self.opts.effort == "max" else self.opts.effort
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        argv.append("-")  # read the prompt from stdin
        return argv, None, f"{system}\n\n---\n\n{prompt}"

    def on_line(self, line, event, out, show):
        if not event:
            return
        etype = event.get("type", "")
        item = event.get("item") or {}
        itype = item.get("type")
        if etype in ("item.started", "item.completed"):
            key = item.get("id")
            shown = getattr(self, "_shown", set())
            self._shown = shown
            if itype == "agent_message" and etype == "item.completed":
                out.text = item.get("text") or out.text
            elif key not in shown:
                detail = None
                if itype == "command_execution" and item.get("command"):
                    detail = ("bash", {"command": item["command"]})
                elif itype == "web_search" and item.get("query"):
                    detail = ("web_search", {"query": item["query"]})
                elif itype == "mcp_tool_call" and item.get("tool"):
                    detail = (item["tool"], item.get("arguments") or {})
                if detail:
                    shown.add(key)
                    show_tool(detail[0], detail[1], show)
        elif etype == "turn.completed":
            usage = event.get("usage") or {}
            out.add_tokens((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))
            out.turns = (out.turns or 0) + 1
        elif etype in ("turn.failed", "error"):
            err = event.get("error") or {}
            out.error = clip(err.get("message") if isinstance(err, dict) else err or event.get("message") or line, 300)

    def finish(self, out, workdir):
        self._shown = set()
        last = workdir / "last_message.txt"
        if last.exists():
            out.text = last.read_text() or out.text
        try:
            value = json.loads(out.text)
            out.structured = value if isinstance(value, dict) else None
        except (json.JSONDecodeError, TypeError):
            pass
        if out.structured is not None or out.text.strip():
            out.error = None


class GeminiBackend(CLIBackend):
    kind = binary = "gemini"
    EXCLUDE = ["run_shell_command", "write_file", "replace", "edit", "smart_edit", "save_memory", "write_todos"]

    def check(self):
        if not shutil.which("gemini"):
            return "`gemini` is not on PATH"
        home = pathlib.Path.home() / ".gemini"
        keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT")
        if any(os.environ.get(k) for k in keys) or (home / "oauth_creds.json").exists():
            return None
        return "not logged in: run `gemini` once to sign in, or set GEMINI_API_KEY"

    def invocation(self, *, system, prompt, schema, research_tools, workdir):
        settings: dict = {"tools": {"exclude": list(self.EXCLUDE)}}
        if research_tools:
            settings["mcpServers"] = {SERVER: {**mcp_entry(), "trust": True}}
        else:
            settings["tools"]["exclude"] += ["google_web_search", "web_fetch"]
        (workdir / ".gemini").mkdir()
        (workdir / ".gemini" / "settings.json").write_text(json.dumps(settings))
        argv = ["gemini", "-o", "stream-json", "--approval-mode", "yolo", "--skip-trust",
                "-e", "none", "--allowed-mcp-server-names", SERVER,
                "-p", "Follow the instructions above."]
        if self.model:
            argv += ["-m", self.model]
        return argv, None, f"{system}\n\n---\n\n{prompt}"

    def on_line(self, line, event, out, show):
        if not event:
            return
        etype = event.get("type")
        if etype == "message" and event.get("role") == "assistant":
            out.text += event.get("content") or ""
        elif etype == "tool_use":
            show_tool(event.get("tool_name"), event.get("parameters"), show)
        elif etype == "result":
            stats = event.get("stats") or {}
            out.add_tokens(stats.get("total_tokens"))
            out.turns = stats.get("tool_calls")
            if event.get("status") == "error":
                err = event.get("error") or {}
                out.error = clip(err.get("message") if isinstance(err, dict) else err, 300)
        elif etype == "error" and event.get("severity") == "error":
            out.error = clip(event.get("message"), 300)


class OpenCodeBackend(CLIBackend):
    kind = binary = "opencode"

    def invocation(self, *, system, prompt, schema, research_tools, workdir):
        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"edit": "deny", "bash": "deny", "webfetch": "allow" if research_tools else "deny"},
            "tools": {"bash": False, "edit": False, "write": False, "patch": False,
                      "todowrite": False, "todoread": False, "task": False, "skill": False},
        }
        if research_tools:
            config["mcp"] = {SERVER: {"type": "local", "command": [sys.executable, str(MCP_SERVER)], "enabled": True}}
        else:
            config["tools"]["webfetch"] = False
        config["autoupdate"] = False
        # Run with an empty config directory: the user's plugins (and any auto-updaters in them)
        # never load and their global config is never touched. Logins live in
        # ~/.local/share/opencode/auth.json, which stays visible, so `opencode auth login` keys work.
        xdg = workdir / "xdg"
        (xdg / "opencode").mkdir(parents=True)
        cfg = xdg / "opencode" / "opencode.json"
        cfg.write_text(json.dumps(config))
        argv = ["opencode", "run", "--format", "json", "--print-logs", "--log-level", "ERROR"]
        if self.model:
            argv += ["-m", self.model]
        if self.opts.effort:
            argv += ["--variant", self.opts.effort]
        argv += ["--", f"{system}\n\n---\n\n{prompt}"]
        env = {"XDG_CONFIG_HOME": str(xdg), "OPENCODE_CONFIG": str(cfg), "OPENCODE_DISABLE_AUTOUPDATE": "true",
               "GH_CONFIG_DIR": gh_config_dir()}
        return argv, env, None

    def on_stderr(self, line, out):
        # OpenCode retries model errors (usage limits, auth failures) silently; stop on the first one.
        if "service=session.processor error=" in line:
            out.error = clip(line.split("error=", 1)[1], 300)
            return True
        return False

    def on_line(self, line, event, out, show):
        if not event:
            return
        etype = event.get("type")
        part = event.get("part") or {}
        if etype == "text":
            out.text += (part.get("text") or "") + "\n"
        elif etype == "tool_use":
            show_tool(part.get("tool"), (part.get("state") or {}).get("input"), show)
        elif etype == "step_finish":
            out.add_cost(part.get("cost"))
            tokens = part.get("tokens") or {}
            out.add_tokens(sum(v for v in tokens.values() if isinstance(v, (int, float))))
            out.turns = (out.turns or 0) + 1
        elif etype == "error":
            err = event.get("error") or {}
            data = err.get("data") if isinstance(err, dict) else None
            out.error = clip((data or {}).get("message") if isinstance(data, dict) else json.dumps(err), 300)


class CommandBackend(CLIBackend):
    """Any other harness. Placeholders: {prompt_file}, {schema_file}, {workdir}, {mcp_server}."""

    kind = "cmd"

    def __init__(self, template: str, opts: Options):
        super().__init__(None, opts)
        if not template:
            raise BackendError("cmd needs a command, e.g. cmd:'aider --yes --message-file {prompt_file}'")
        self.template = template
        self.binary = shlex.split(template)[0]

    @property
    def label(self) -> str:
        return f"cmd:{self.binary}"

    def invocation(self, *, system, prompt, schema, research_tools, workdir):
        full = f"{system}\n\n---\n\n{prompt}"
        (workdir / "prompt.md").write_text(full)
        (workdir / "schema.json").write_text(json.dumps(schema or {}))
        values = {
            "{prompt_file}": str(workdir / "prompt.md"),
            "{schema_file}": str(workdir / "schema.json"),
            "{workdir}": str(workdir),
            "{mcp_server}": f"{sys.executable} {MCP_SERVER}",
        }
        argv = []
        for arg in shlex.split(self.template):
            for key, value in values.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        return argv, None, (None if "{prompt_file}" in self.template else full)

    def on_line(self, line, event, out, show):
        out.text += line


# ---------------------------------------------------------------- model APIs

PRESETS = {
    # kind: (base_url, api key env var, default model)
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", None),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", None),
    "ollama": ("http://localhost:11434", None, None),   # native /api/chat, so the context size can be set
    "lmstudio": ("http://localhost:1234/v1", None, None),
    "api": (None, "IDEAHARNESS_API_KEY", None),
}
LOCAL = ("ollama", "lmstudio")


@dataclass
class Reply:
    message: dict              # the assistant message, appended to the history as-is
    content: str
    calls: list                # [(call_id, name, args dict or None)]
    tokens: int = 0
    cost: float | None = None


class APIBackend(Backend):
    """A built-in tool-calling agent loop over a model API: any OpenAI-compatible Chat Completions
    endpoint, or Ollama's native chat API."""

    def __init__(self, kind: str, model: str | None, opts: Options):
        base, key_env, default_model = PRESETS[kind]
        super().__init__(model or default_model, opts)
        self.kind = kind
        self.base = (opts.api_base or base or os.environ.get("IDEAHARNESS_API_BASE") or "").rstrip("/")
        if kind == "ollama" and self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.key_env = opts.api_key_env or key_env

    def check(self):
        if not self.base:
            return "no endpoint: pass --api-base or set IDEAHARNESS_API_BASE"
        if self.key_env and not os.environ.get(self.key_env) and self.kind not in LOCAL:
            return f"{self.key_env} is not set"
        if self.kind in LOCAL:
            try:
                urllib.request.urlopen(self.base + ("/api/tags" if self.kind == "ollama" else "/models"), timeout=3).read()
            except OSError:
                return f"no server answering at {self.base}"
        if not self.model:
            return f"no model: use {self.kind}:<model>"
        return None

    def local_models(self) -> list[str]:
        try:
            if self.kind == "ollama":
                data = json.loads(urllib.request.urlopen(self.base + "/api/tags", timeout=3).read())
                return [m.get("name", "?") for m in data.get("models", [])]
            data = json.loads(urllib.request.urlopen(self.base + "/models", timeout=3).read())
            return [m.get("id", "?") for m in data.get("data", [])]
        except (OSError, ValueError):
            return []

    def _post(self, path: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.key_env) if self.key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        delay = 5
        for attempt in range(5):
            req = urllib.request.Request(self.base + path, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=min(900, self.opts.timeout_min * 60)) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in (408, 429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BackendError(f"HTTP {exc.code} from {self.base}: {clip(detail, 300)}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BackendError(f"could not reach {self.base}: {exc}") from None
        raise BackendError("unreachable")

    def _complete(self, messages: list[dict], tools: list | None) -> tuple[Reply, dict]:
        if self.kind == "ollama":
            body: dict = {"model": self.model, "messages": messages, "stream": False,
                          "options": {"num_ctx": self.opts.num_ctx}}
            if tools:
                body["tools"] = tools
            resp = self._post("/api/chat", body)
            msg = resp.get("message") or {}
            calls = [(None, (c.get("function") or {}).get("name", ""), _args((c.get("function") or {}).get("arguments")))
                     for c in msg.get("tool_calls") or []]
            tokens = (resp.get("prompt_eval_count") or 0) + (resp.get("eval_count") or 0)
            return Reply(msg, msg.get("content") or "", calls, tokens), resp
        body = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        resp = self._post("/chat/completions", body)
        choices = resp.get("choices") or []
        if not choices:
            raise BackendError(f"no choices in response: {clip(json.dumps(resp), 200)}")
        msg = choices[0].get("message") or {}
        calls = [(c.get("id"), (c.get("function") or {}).get("name", ""), _args((c.get("function") or {}).get("arguments")))
                 for c in msg.get("tool_calls") or []]
        usage = resp.get("usage") or {}
        # Keep the whole assistant message: DeepSeek needs reasoning_content echoed back during tool use.
        return Reply({k: v for k, v in msg.items() if v is not None}, msg.get("content") or "", calls,
                     usage.get("total_tokens") or 0, usage.get("cost")), resp

    def _tool_message(self, call_id, name: str, result: str) -> dict:
        if self.kind == "ollama":
            return {"role": "tool", "tool_name": name, "content": result}
        return {"role": "tool", "tool_call_id": call_id, "content": result}

    def run(self, *, system, prompt, schema, research_tools, transcript, show) -> Outcome:
        out = Outcome()
        started = time.time()
        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in research.TOOLS
        ] if research_tools else None
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

        def call(name, args) -> str:
            if not isinstance(args, dict):
                result, is_error = "arguments were not a valid JSON object", True
            else:
                show_tool(name, args, show)
                result, is_error = research.call_tool(name, args)
            raw.write(json.dumps({"tool": name, "args": args, "error": is_error, "result": result[:3000]}) + "\n")
            return result

        try:
            with transcript.open("w") as raw:
                for turn in range(1, self.opts.api_max_turns + 2):
                    last_turn = turn > self.opts.api_max_turns
                    if last_turn:
                        messages.append({"role": "user", "content": "Stop researching now and give your final answer."})
                    reply, resp = self._complete(messages, None if last_turn else tools)
                    raw.write(json.dumps({"turn": turn, "response": resp}) + "\n")
                    out.add_tokens(reply.tokens)
                    out.add_cost(reply.cost)
                    out.turns = turn
                    messages.append(reply.message)
                    if reply.calls:
                        for call_id, name, args in reply.calls:
                            messages.append(self._tool_message(call_id, name, call(name, args)))
                        continue
                    text_calls = _text_tool_calls(reply.content) if tools and not last_turn else []
                    if text_calls:  # weak local models often write tool calls as plain JSON text
                        results = [f"Result of {name}({json.dumps(args)}):\n{call(name, args)}" for name, args in text_calls]
                        messages.append({"role": "user", "content": "\n\n".join(results)
                                         + "\n\nContinue. When you are done, give your final answer."})
                        continue
                    out.text = reply.content
                    break
        except BackendError as exc:
            out.error = str(exc)
        out.seconds = time.time() - started
        return out


def _args(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _text_tool_calls(content: str) -> list[tuple[str, dict]]:
    """Find tool calls written as text, e.g. {"name": "github_repo", "arguments": {...}} or <tool_call>...</tool_call>."""
    decoder = json.JSONDecoder()
    calls = []
    i = content.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(content, i)
        except json.JSONDecodeError:
            i = content.find("{", i + 1)
            continue
        if isinstance(value, dict) and value.get("name") in research.BY_NAME:
            args = value.get("arguments", value.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            if isinstance(args, dict):
                calls.append((value["name"], args))
        i = content.find("{", end)
    return calls


# ---------------------------------------------------------------- factory

CLI_KINDS = {"claude": ClaudeBackend, "codex": CodexBackend, "gemini": GeminiBackend, "opencode": OpenCodeBackend}
ALL_KINDS = [*CLI_KINDS, *PRESETS, "cmd"]


def make_backend(spec: str, opts: Options) -> Backend:
    kind, _, rest = spec.strip().partition(":")
    kind = kind.lower()
    if kind == "cmd":
        return CommandBackend(rest, opts)
    if kind in CLI_KINDS:
        return CLI_KINDS[kind](rest, opts)
    if kind in PRESETS:
        return APIBackend(kind, rest, opts)
    raise BackendError(f"unknown agent '{kind}'. Choose from: {', '.join(ALL_KINDS)}")
