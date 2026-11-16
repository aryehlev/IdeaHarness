# IdeaHarness

Two kinds of agents fight over what you should build next. The **Hunter** pitches one idea per round, written for *you*. It uses a profile built from what your AI coding agents already remember about you. The **Destroyers** search GitHub, Hacker News, package registries and the web to kill it. Dead ideas go to a graveyard with their cause of death and the evidence, and the Hunter reads the graveyard before its next pitch. The loop ends when an idea survives the whole gauntlet: by default, two Destroyers in a row.

It runs on whichever agent harness you have: Claude Code, Codex, Gemini CLI, OpenCode, DeepSeek, Ollama, OpenRouter, any OpenAI-compatible API, or any other CLI. You can mix them, so a Codex Destroyer can attack a Claude Hunter's idea.

## Install

```
curl -fsSL https://raw.githubusercontent.com/aryehlev/IdeaHarness/main/install.sh | sh
```

That puts an `ideaharness` command in `~/.local/bin`. It needs only Python 3.9 or later, which macOS already ships, plus `curl` and `tar`. IdeaHarness has no dependencies, so nothing is pip-installed. Run the same line again to upgrade. To uninstall, run `rm -rf ~/.ideaharness/app ~/.local/bin/ideaharness`; your profile and runs stay in `~/.ideaharness`.

You can also use `uv tool install git+https://github.com/aryehlev/IdeaHarness` or `pipx install git+https://github.com/aryehlev/IdeaHarness`.

```
ideaharness doctor     # which harnesses, tools and memory files are available
ideaharness            # build your profile on the first run, then hunt
ideaharness profile    # rebuild your profile any time
```

```
── Round 2 (graveyard: 1)
    hunter·claude    github  claude code hooks
    pitch            UndoBench — Reproducible benchmark: how often do AI-agent undo tools actually restore your files?
    destroyer1·codex github  RonitSachdev/ccundo
    destroyer1·codex hn      undo AI agent
    ✗ killed         Nobody has shown they want a recovery-rate scorecard for four small undo tools (3–46★) ...  [nove:4 dema:3 goal:4 feas:7]
```

You need at least one harness from the table below, and ideally `gh` logged in so star counts come from your GitHub rate limit.

## Your profile

Ideas are only useful if they fit the person building them. The first run (or `ideaharness profile`) builds `~/.ideaharness/profile.md` from:

- **What your agents remember about you.** It reads Claude Code memory and `CLAUDE.md`, Codex `AGENTS.md` and memories, Gemini CLI `GEMINI.md`, and the OpenCode, Cursor, Windsurf, Cline, Amp, Goose, Qwen Code and Kimi rule files. It finds them automatically; add more with `--memory PATH`.
- **Your public GitHub repos** with their star counts, so the agents know what you've built and what took off. Skip this with `--no-github`.
- **A few questions**, chosen to cover what the memory doesn't say. Without a terminal, the questions are written into the profile for you to answer there.

It shows you every file it will read and asks before sending them to an agent. Emails and anything that looks like a key or token are redacted first. The result is plain markdown, so read it, fix it, and add to it; every agent reads it on every run. Use `--profile FILE` for a different profile, or `--no-profile` to hunt without one.

## Harnesses

Pick with `--agent` for every role, or per role with `--hunter`, `--destroyer` and `--profiler`. Add `:model` to choose a model.

| Spec | Runs | Needs |
|---|---|---|
| `claude`, `claude:opus` | Claude Code (`claude -p`) | Claude Code logged in |
| `codex`, `codex:gpt-5.5` | OpenAI Codex CLI (`codex exec`) | Codex logged in |
| `gemini`, `gemini:gemini-3-pro` | Gemini CLI (`gemini -p`) | Gemini CLI logged in, or `GEMINI_API_KEY` |
| `opencode:deepseek/deepseek-chat` | OpenCode, with any of its providers | `opencode auth login` for that provider |
| `deepseek`, `deepseek:deepseek-reasoner` | DeepSeek API, built-in agent loop | `DEEPSEEK_API_KEY` |
| `openrouter:<model>` | OpenRouter, built-in agent loop | `OPENROUTER_API_KEY` |
| `openai:<model>` | OpenAI API, built-in agent loop | `OPENAI_API_KEY` |
| `ollama:<model>`, `lmstudio:<model>` | local models, built-in agent loop | the local server running |
| `api:<model>` | any OpenAI-compatible endpoint | `--api-base URL`, key in `IDEAHARNESS_API_KEY` or `--api-key-env` |
| `cmd:"<command>"` | any other CLI | prompt on stdin, or `{prompt_file}` in the command |

Repeat `--destroyer` to rotate harnesses through the gauntlet. With the command below, a winning idea has to survive both Claude and Codex:

```
ideaharness --hunter claude --destroyer claude --destroyer codex
```

Ollama models are loaded with a 32k context window (`--num-ctx`), because the prompts alone are several thousand tokens and Ollama's 4k default silently cuts them off. Small local models research much less than hosted ones, so they work better as a cheap extra Destroyer than as the only one.

## Research tools

Every harness gets the same read-only research tools, so kills are backed by real numbers whichever model runs them:

- **GitHub**: `github_search_repos` and `github_repo` for real star counts and last push dates, and `github_search_issues` for demand evidence
- **Hacker News**: `hn_search` for stories and Show HN launches, with points
- **Package registries**: `package_search` for npm and crates.io downloads and PyPI lookups
- **The web**: `web_search` (keyless) and `fetch_page`

CLI harnesses reach these through a small MCP server, and API backends call them directly. They also keep their own web search where they have one. For a `cmd:` harness, or to use the tools in your own agent sessions, register `ideaharness mcp` as a stdio MCP server.

## How an idea dies

Each Destroyer returns a verdict, attacks with evidence, the prior art it found (with star counts and last activity) and four scores from 1 to 10: novelty, demand, goal fit and feasibility. Goal fit and feasibility are judged against your profile. The harness kills the idea if any of these is true, even when the Destroyer votes to let it live:

- the Destroyer votes `kill`, or marks any attack `fatal`
- an active project doing the same thing has `--prior-art-stars` stars or more (default 200)
- any score is below `--bar` (default 7)

Low-star or abandoned predecessors count as evidence of weak demand, not an open gap. A second or later Destroyer sees the earlier reports and is told the earlier one was too soft.

## Options

```
--focus TEXT          extra direction for this run, e.g. "tools for Rust developers"
--brief FILE          what kind of idea to look for (default: briefs/default.md; the profile wins on conflicts)
--max-rounds N        give up after N pitches (default 12)
--gauntlet N          Destroyer attacks an idea must survive in a row (default 2)
--bar N               minimum for every score (default 7)
--prior-art-stars N   star count at which existing prior art kills an idea (default 200)
--effort LEVEL        low | medium | high | xhigh | max, where the harness supports it
--max-cost USD        stop starting rounds once reported cost reaches this
--timeout MIN         kill an agent session after this long (default 20)
--resume DIR          continue a run folder with its brief and graveyard
```

Run `ideaharness run --help` for the rest.

## Output

Each run writes a folder under `~/.ideaharness/runs/` (change it with `--out`, or move everything with `IDEAHARNESS_HOME`):

- `log.md`: the whole fight in readable form
- `winner.md`: the surviving idea, every attack it survived, and the graveyard
- `state.json`: progress and the graveyard, used by `--resume`
- `rounds/NN.json`: each round's idea and verdicts
- `transcripts/`: the raw output of every agent session, including every search

Press Ctrl-C at any time and continue later with `ideaharness --resume <folder>`.

## Sandboxing

Agents run in an empty temporary directory and get only read-only research tools:

- **Claude Code** runs without your settings, plugins, hooks or MCP servers, and anything outside the allowed tools is denied without prompting.
- **Codex** runs in its read-only sandbox.
- **Gemini CLI** runs with its shell, file-writing and memory tools excluded.
- **OpenCode** runs with an empty config directory, so your plugins never load and your global config is never touched; your `opencode auth` logins still work.

None of them can run shell commands. The research tools refuse to fetch private or local network addresses. A session that hits `--timeout` is killed along with every process it started.

## License

MIT
