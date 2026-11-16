"""Build a profile of the person the ideas are for.

Sources, all optional:
  - memory and global instruction files kept by AI coding agents (Claude Code, Codex,
    Gemini CLI, OpenCode, Cursor, Windsurf, Cline, Amp, Goose, Qwen Code, Kimi)
  - their public, non-fork GitHub repos with star counts (via gh)
  - a few questions asked in the terminal, chosen to fill what the memory doesn't say

An agent condenses the raw material into profile.md, which is plain markdown you can edit.
Emails and credential-looking strings are redacted before anything is sent to an agent.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass

from .ui import ask, bold, clip, confirm, cyan, dim, green, yellow

HOME = pathlib.Path.home()
PER_FILE_CHARS = 8000
TOTAL_CHARS = 120000

# (harness, glob pattern relative to $HOME)
MEMORY_GLOBS = [
    ("Claude Code", ".claude/CLAUDE.md"),
    ("Claude Code memory", ".claude/projects/*/memory/*.md"),
    ("Claude Code memory", ".claude/memory/*.md"),
    ("Codex", ".codex/AGENTS.md"),
    ("Codex memory", ".codex/memories/**/*.md"),
    ("Gemini CLI", ".gemini/GEMINI.md"),
    ("OpenCode", ".config/opencode/AGENTS.md"),
    ("Cursor", ".cursor/rules/*.md*"),
    ("Windsurf", ".codeium/windsurf/memories/*.md"),
    ("Cline", "Documents/Cline/Rules/*.md"),
    ("Amp", ".config/amp/AGENTS.md"),
    ("Goose", ".config/goose/.goosehints"),
    ("Qwen Code", ".qwen/QWEN.md"),
    ("Kimi", ".kimi/AGENTS.md"),
]
SKIP_NAMES = {"MEMORY.md"}  # Claude Code's index file only points at the real memory files

DEFAULT_QUESTIONS = [
    "What should this project get you first: GitHub stars, real users, income, or something else?",
    "What are you strongest at (languages, domains, kinds of systems)?",
    "How much time can you put in per week, and is there any budget for infrastructure?",
    "Which problems or topics do you actually care about?",
    "What have you built or launched before, and how did it go?",
]
CLOSING_QUESTION = "Anything else the idea must fit, or that you never want to build?"

_REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,})"), "[secret]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[secret]"),
    (re.compile(r"\b(xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), "[secret]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[secret]"),
]


@dataclass
class Source:
    harness: str
    path: str
    text: str
    truncated: bool = False


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def discover_memory(extra: list[str] | None = None) -> list[Source]:
    found: list[Source] = []
    seen: set[pathlib.Path] = set()

    def add(harness: str, path: pathlib.Path) -> None:
        path = path.resolve()
        if path in seen or not path.is_file() or path.name in SKIP_NAMES:
            return
        seen.add(path)
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            return
        if text:
            found.append(Source(harness, _tilde(path), text[:PER_FILE_CHARS], len(text) > PER_FILE_CHARS))

    for harness, pattern in MEMORY_GLOBS:
        for name in sorted(glob.glob(str(HOME / pattern), recursive=True)):
            add(harness, pathlib.Path(name))
    found += _codex_db_memories()
    for item in extra or []:
        p = pathlib.Path(item).expanduser()
        for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
            add("extra", f)
    return found


def _codex_db_memories() -> list[Source]:
    db = HOME / ".codex" / "memories_1.sqlite"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("SELECT raw_memory FROM stage1_outputs ORDER BY source_updated_at DESC LIMIT 50").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    text = "\n\n".join(r[0] for r in rows if r and r[0]).strip()
    return [Source("Codex memory", _tilde(db), text[:PER_FILE_CHARS], len(text) > PER_FILE_CHARS)] if text else []


def github_repos() -> str | None:
    if not shutil.which("gh"):
        return None
    try:
        proc = subprocess.run(
            ["gh", "repo", "list", "--limit", "100", "--source", "--visibility", "public",
             "--json", "name,description,stargazerCount,primaryLanguage,pushedAt,createdAt"],
            capture_output=True, text=True, timeout=60,
        )
        repos = json.loads(proc.stdout) if proc.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not repos:
        return None
    repos.sort(key=lambda r: -(r.get("stargazerCount") or 0))
    lines = []
    for r in repos:
        lang = (r.get("primaryLanguage") or {}).get("name") or "?"
        lines.append(f"- {r['name']} ({r.get('stargazerCount', 0)}★, {lang}, created {r.get('createdAt', '')[:10]}, "
                     f"last push {r.get('pushedAt', '')[:10]}): {clip(r.get('description') or '', 160)}")
    return "\n".join(lines)


def _tilde(path: pathlib.Path) -> str:
    try:
        return "~/" + str(path.relative_to(HOME))
    except ValueError:
        return str(path)


def _size(n: int) -> str:
    return f"{n / 1000:.1f} KB" if n >= 1000 else f"{n} B"


def material_text(sources: list[Source], repos: str | None) -> str:
    parts, total = [], 0
    for s in sources:
        flag = ' truncated="yes"' if s.truncated else ""
        block = f'<source harness="{s.harness}" path="{s.path}"{flag}>\n{redact(s.text)}\n</source>'
        if total + len(block) > TOTAL_CHARS:
            parts.append("<!-- more sources omitted to stay within size limits -->")
            break
        parts.append(block)
        total += len(block)
    if repos:
        parts.append(f'<source harness="GitHub" path="public repositories, most starred first">\n{repos}\n</source>')
    return "\n\n".join(parts)


def print_sources(sources: list[Source], repos: str | None) -> None:
    if not sources and not repos:
        print(dim("  no agent memory files found"))
    for s in sources:
        note = ", truncated" if s.truncated else ""
        print(f"  {cyan(s.harness.ljust(19))} {s.path} " + dim(f"({_size(len(s.text))}{note})"))
    if repos:
        print(f"  {cyan('GitHub'.ljust(19))} {len(repos.splitlines())} public repos with star counts")


def build(*, run_agent, backend, out_path: pathlib.Path, transcript_dir: pathlib.Path, extra_memory: list[str],
          use_github: bool, questions: bool, assume_yes: bool, interactive: bool) -> str:
    """Build profile.md and return its text. run_agent is cli.run_agent (passed in to avoid a cycle)."""
    from .schemas import PROFILE_SCHEMA

    print(bold("Building your profile"))
    print("Reading what your AI agents already know about you:")
    sources = discover_memory(extra_memory)
    repos = github_repos() if use_github else None
    print_sources(sources, repos)

    draft, asked = "", []
    if sources or repos:
        print()
        msg = (f"Send these to {backend.label} to draft your profile? Emails and anything that looks like a "
               "key or token are redacted first.")
        if not assume_yes:
            if not interactive:
                raise SystemExit("Refusing to send memory files without confirmation: rerun with --yes, "
                                 "or run `python3 ideaharness.py profile` in a terminal.")
            if not confirm(msg):
                sources, repos = [], None
        if sources or repos:
            system = (pathlib.Path(__file__).resolve().parent.parent / "prompts" / "profiler.md").read_text()
            system = system.replace("{{DATE}}", dt.date.today().isoformat())
            data, _usage = run_agent(
                role="profiler", backend=backend, system=system, schema=PROFILE_SCHEMA,
                prompt="# Material\n\n" + material_text(sources, repos),
                transcript_base=transcript_dir / "profiler", research_tools=False,
            )
            draft = data["profile_markdown"].strip()
            asked = [q for q in data["questions"] if q.strip()][:5]
    if not draft:
        asked = list(DEFAULT_QUESTIONS)

    answers: list[tuple[str, str]] = []
    if questions and interactive:
        if draft:
            print("\n" + dim(draft) + "\n")
        print(bold("A few questions to fill the gaps") + dim(" (press Enter to skip any)"))
        for q in [*asked, CLOSING_QUESTION]:
            a = ask(f"{yellow('?')} {q}\n  > ")
            if a:
                answers.append((q, a))
    elif not draft:
        print(yellow("No agent memory and no answers: the profile will be empty. "
                     "Run `python3 ideaharness.py profile` in a terminal to answer a few questions."))
    unanswered = [q for q in [*asked, CLOSING_QUESTION] if q not in {q for q, _ in answers}] if not answers else []

    used = sorted({s.harness for s in sources} | ({"GitHub"} if repos else set()))
    header = (f"<!-- Built by IdeaHarness on {dt.date.today().isoformat()} from: "
              f"{', '.join(used) if used else 'interview only'}. Every agent reads this file on every run; "
              f"edit it freely. Rebuild with: python3 ideaharness.py profile -->\n\n")
    body = draft or "# Profile\n"
    if answers:
        body += "\n\n## In their own words\n\n" + "\n\n".join(f"**{q}**\n{a}" for q, a in answers)
    elif unanswered:
        body += ("\n\n## Open questions\n\n<!-- Answer under each question; the agents read your answers. -->\n\n"
                 + "\n\n".join(f"**{q}**\n" for q in unanswered))
    text = header + body.strip() + "\n"
    out_path.write_text(text)
    print(green(f"\nSaved {out_path}") + dim(" — edit it any time; it is sent to the agents on every run."))
    return text
