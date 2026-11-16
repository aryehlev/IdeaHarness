"""Command line and main loop."""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import __version__
from . import profile as profiles
from .backends import ALL_KINDS, Backend, BackendError, Options, Outcome, make_backend
from .schemas import IDEA_SCHEMA, SCORE_KEYS, VERDICT_SCHEMA, extract_json, schema_instructions, validate
from .ui import bold, clip, confirm, cyan, dim, green, interactive, magenta, red, yellow

PKG = pathlib.Path(__file__).resolve().parent
PROMPTS = PKG / "prompts"
BRIEFS = PKG / "briefs"
# Your profile and run folders live here (not inside the installed package).
HOME_DIR = pathlib.Path(os.environ.get("IDEAHARNESS_HOME") or "~/.ideaharness").expanduser()
COMMANDS = ("run", "profile", "doctor", "mcp")


class AgentError(RuntimeError):
    def __init__(self, message: str, usage: "Usage"):
        super().__init__(message)
        self.usage = usage


@dataclass
class Usage:
    cost: float = 0.0
    cost_known: bool = False
    tokens: int = 0

    def add(self, out: Outcome | "Usage") -> None:
        if isinstance(out, Usage):
            self.cost += out.cost
            self.cost_known |= out.cost_known
            self.tokens += out.tokens
            return
        if out.cost_usd is not None:
            self.cost += out.cost_usd
            self.cost_known = True
        self.tokens += out.tokens or 0

    def text(self) -> str:
        parts = []
        if self.cost_known:
            parts.append(f"${self.cost:.2f}")
        if self.tokens:
            parts.append(f"{self.tokens / 1000:.0f}k tokens")
        return ", ".join(parts) or "usage not reported"


# ---------------------------------------------------------------- running one agent

REPAIR_SYSTEM = "You repair malformed JSON answers. Keep the content, fix the structure, and return only the JSON object."


def parse_outcome(out: Outcome, schema: dict) -> tuple[dict | None, list[str]]:
    candidate = out.structured if isinstance(out.structured, dict) else extract_json(out.text)
    if candidate is None:
        return None, [out.error or "the reply contained no JSON object"]
    return candidate, validate(candidate, schema)


def run_agent(*, role: str, backend: Backend, system: str, prompt: str, schema: dict,
              transcript_base: pathlib.Path, cfg, research_tools: bool = True, color=cyan) -> tuple[dict, Usage]:
    tag = color(f"    {role:<18} ")

    def show(kind: str, detail: str) -> None:
        print(f"{tag}{kind:<7} {clip(detail, 130)}", flush=True)

    transcript_base.parent.mkdir(parents=True, exist_ok=True)
    full_prompt = prompt if backend.native_schema else f"{prompt}\n\n{schema_instructions(schema)}"
    usage = Usage()
    last = "unknown error"
    for attempt in range(1, cfg.retries + 2):
        if attempt > 1:
            print(dim(f"    retry {attempt - 1}/{cfg.retries}: {clip(last, 200)}"))
        out = backend.run(system=system, prompt=full_prompt, schema=schema, research_tools=research_tools,
                          transcript=transcript_base.with_name(f"{transcript_base.name}.try{attempt}.jsonl"), show=show)
        usage.add(out)
        data, errors = parse_outcome(out, schema)
        if errors and out.text.strip() and not out.error:
            print(tag + dim(f"fixing malformed JSON ({clip(errors[0], 90)})"))
            fix_prompt = (
                "This reply was supposed to end with a JSON object matching the schema, but it failed validation:\n"
                + "\n".join(f"- {e}" for e in errors[:10])
                + f"\n\nThe reply:\n<<<\n{out.text[-30000:]}\n>>>\n\n"
                "Return only the corrected JSON object. Do not research anything."
            )
            if not backend.native_schema:
                fix_prompt += "\n\n" + schema_instructions(schema)
            fix = backend.run(system=REPAIR_SYSTEM, prompt=fix_prompt, schema=schema, research_tools=False,
                              transcript=transcript_base.with_name(f"{transcript_base.name}.repair{attempt}.jsonl"), show=show)
            usage.add(fix)
            data, errors = parse_outcome(fix, schema)
        if not errors:
            turns = f", {out.turns} turns" if out.turns else ""
            print(tag + dim(f"done in {out.seconds:.0f}s{turns}, {usage.text()}"))
            return data, usage
        last = out.error or errors[0]
    raise AgentError(f"{role} on {backend.label} failed: {last}", usage)


# ---------------------------------------------------------------- judging and formatting

def judge(verdict: dict, cfg) -> str | None:
    """Return a cause of death, or None if the idea survives this attack.

    The Destroyer's own verdict counts, and three hard rules are enforced on top so a soft
    Destroyer can't wave an idea through.
    """
    if verdict["verdict"] == "kill":
        return verdict["cause_of_death"] or "killed by the Destroyer"
    for attack in verdict["attacks"]:
        if attack["severity"] == "fatal":
            return attack["finding"]
    for art in verdict["prior_art"]:
        if art["overlap"] == "same" and (art["stars"] or 0) >= cfg.prior_art_stars:
            return f"already exists: {art['name']} ({art['stars']}★) {art['url']}"
    low = [f"{k} {verdict['scores'][k]}/10" for k in SCORE_KEYS if verdict["scores"][k] < cfg.bar]
    if low:
        return f"below the bar of {cfg.bar}: " + ", ".join(low)
    return None


def graveyard_text(graveyard: list[dict]) -> str:
    if not graveyard:
        return "(empty: this is the first round)"
    lines = []
    for g in graveyard:
        arts = ", ".join(
            f"{a['name']} ({a['stars']}★)" if a.get("stars") is not None else a["name"]
            for a in g.get("prior_art", [])[:3]
        )
        line = f"- #{g['round']} {g['name']}: {g['one_liner']}\n  killed: {g['cause_of_death']}"
        if arts:
            line += f"\n  prior art: {arts}"
        if g.get("what_would_save_it"):
            line += f"\n  critic's pivot hint: {g['what_would_save_it']}"
        lines.append(line)
    return "\n".join(lines)


def idea_text(idea: dict) -> str:
    comps = "\n".join(
        f"  - {c['name']} ({c['stars'] if c['stars'] is not None else '?'}★) {c['url']}: {c['difference']}"
        for c in idea["known_competitors"]
    ) or "  (none named)"
    return (
        f"Name: {idea['name']}\n"
        f"One-liner: {idea['one_liner']}\n"
        f"Problem: {idea['problem']}\n"
        f"Solution: {idea['solution']}\n"
        f"Target users: {idea['target_users']}\n"
        f"Why now: {idea['why_now']}\n"
        f"Why this person: {idea['why_this_person']}\n"
        f"Hook: {idea['hook']}\n"
        f"First version: {idea['mvp']}\n"
        f"Competitors the Hunter already knows about:\n{comps}"
        + (f"\nPivot of: {idea['pivot_of']}" if idea.get("pivot_of") else "")
    )


def verdict_text(v: dict) -> str:
    attacks = "\n".join(f"  - [{a['severity']}] {a['angle']}: {a['finding']}" for a in v["attacks"]) or "  (none)"
    arts = "\n".join(
        f"  - {a['name']} ({a['stars'] if a['stars'] is not None else '?'}★, {a['overlap']}, "
        f"last active {a['last_active']}) {a['url']}"
        for a in v["prior_art"]
    ) or "  (none found)"
    scores = ", ".join(f"{k} {v['scores'][k]}" for k in SCORE_KEYS)
    return f"Summary: {v['summary']}\nScores: {scores}\nAttacks:\n{attacks}\nPrior art found:\n{arts}"


def render(name: str, **values) -> str:
    text = (PROMPTS / name).read_text()
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


def save_json(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ---------------------------------------------------------------- commands

def options(cfg) -> Options:
    return Options(effort=cfg.effort, budget=cfg.agent_budget, timeout_min=cfg.timeout,
                   api_base=cfg.api_base, api_key_env=cfg.api_key_env, api_max_turns=cfg.api_max_turns,
                   num_ctx=cfg.num_ctx)


def ready(backend: Backend) -> Backend:
    reason = backend.check()
    if reason:
        raise SystemExit(red(f"{backend.label}: {reason}") + dim("\n(run `ideaharness doctor` to see every harness)"))
    return backend


def build_profile(cfg, opts: Options) -> str:
    backend = ready(make_backend(cfg.profiler or getattr(cfg, "hunter", None) or cfg.agent, opts))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return profiles.build(
        run_agent=functools.partial(run_agent, cfg=cfg),
        backend=backend,
        out_path=pathlib.Path(cfg.profile),
        transcript_dir=pathlib.Path(cfg.out) / f"profile-{stamp}",
        extra_memory=cfg.memory or [],
        use_github=not cfg.no_github,
        questions=not cfg.no_questions,
        assume_yes=cfg.yes,
        interactive=interactive(),
    )


def cmd_profile(cfg) -> int:
    path = pathlib.Path(cfg.profile)
    if cfg.show:
        print(path.read_text() if path.exists() else yellow(f"No profile at {path} yet."))
        return 0
    if path.exists() and interactive() and not cfg.yes and not confirm(f"Replace the existing {path}?", default=True):
        return 0
    build_profile(cfg, options(cfg))
    return 0


def cmd_doctor(cfg) -> int:
    opts = options(cfg)
    print(bold("Agent harnesses") + dim("  (use with --agent, --hunter, --destroyer)"))
    for kind in [k for k in ALL_KINDS if k != "cmd"]:
        backend = make_backend(kind, opts)
        reason = backend.check()
        note = ""
        if reason and reason.startswith("no model"):
            reason, note = None, f"needs a model, e.g. {kind}:<model>"
            models = backend.local_models() if hasattr(backend, "local_models") else []
            if models:
                note += " · installed: " + ", ".join(models[:6])
        mark = green("✓") if not reason else red("✗")
        print(f"  {mark} {kind:<11}" + (dim(note) if not reason else dim(reason)))
    print(f"  {dim('·')} {'cmd':<11}" + dim("any other CLI: --agent \"cmd:<command>\", prompt on stdin or {prompt_file}"))

    print(bold("\nResearch tools"))
    if shutil.which("gh"):
        ok = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).returncode == 0
        print(f"  {green('✓') if ok else yellow('!')} gh " + dim("logged in: GitHub searches use your rate limit"
                                                              if ok else "not logged in: GitHub searches are rate limited"))
    else:
        print(f"  {yellow('!')} gh " + dim("not installed: GitHub searches fall back to anonymous API calls"))
    print(f"  {green('✓')} MCP server " + dim("`ideaharness mcp` (stdio), for harnesses you wire up yourself"))

    print(bold("\nMemory a profile can be built from"))
    profiles.print_sources(profiles.discover_memory(cfg.memory or []), None)
    path = pathlib.Path(cfg.profile)
    print(bold("\nProfile ") + (f"{path}" if path.exists() else dim(f"{path} not built yet (ideaharness profile)")))
    return 0


def resolve_profile(cfg, opts: Options) -> str | None:
    if cfg.no_profile:
        return None
    path = pathlib.Path(cfg.profile)
    if path.exists():
        print(dim(f"profile: {path}"))
        return path.read_text()
    print(yellow("No profile yet, so the ideas would be generic. Building one first.\n"))
    text = build_profile(cfg, opts)
    print()
    return text


def cmd_run(cfg) -> int:
    opts = options(cfg)
    try:
        hunter = ready(make_backend(cfg.hunter or cfg.agent, opts))
        destroyers = [ready(make_backend(spec, opts)) for spec in (cfg.destroyer or [cfg.agent])]
    except BackendError as exc:
        raise SystemExit(red(str(exc)))

    if cfg.resume:
        run_dir = pathlib.Path(cfg.resume).resolve()
        state_path = run_dir / "state.json"
        if not state_path.exists():
            raise SystemExit(red(f"{run_dir} has no state.json"))
        state = json.loads(state_path.read_text())
        state.setdefault("tokens", 0)
        if state.get("winner"):
            print(green(f"This run already has a winner: {run_dir / 'winner.md'}"))
            return 0
        print(dim(f"resuming {run_dir} at round {state['round'] + 1}, {len(state['graveyard'])} in the graveyard"))
    else:
        profile_text = resolve_profile(cfg, opts)
        brief = pathlib.Path(cfg.brief).read_text().strip()
        if cfg.focus:
            brief += f"\n\n## Focus for this run\n\n{cfg.focus}"
        if profile_text:
            profile_text = re.sub(r"<!--.*?-->", "", profile_text, flags=re.S).strip()
            brief = f"# Who this is for\n\n{profile_text}\n\n# What to look for\n\n{brief}"
        run_dir = pathlib.Path(cfg.out).resolve() / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        state_path = run_dir / "state.json"
        state = {"brief": brief, "round": 0, "graveyard": [], "cost": 0.0, "tokens": 0, "winner": None}
        save_json(state_path, state)
        (run_dir / "log.md").write_text(f"# IdeaHarness run {run_dir.name}\n\n{brief}\n\n")
    for sub in ("transcripts", "rounds"):
        (run_dir / sub).mkdir(exist_ok=True)

    def log(text: str) -> None:
        with (run_dir / "log.md").open("a") as f:
            f.write(text + "\n")

    def spend(usage: Usage) -> None:
        state["cost"] += usage.cost
        state["tokens"] = state.get("tokens", 0) + usage.tokens

    today = dt.date.today().isoformat()
    hunter_system = render("hunter.md", DATE=today, STARS=cfg.prior_art_stars, BAR=cfg.bar)
    destroyer_system = render("destroyer.md", DATE=today, STARS=cfg.prior_art_stars, BAR=cfg.bar)
    names = [d.label for d in destroyers]

    print(bold(f"IdeaHarness → {run_dir}"))
    print(dim(f"hunter: {hunter.label} · destroyer{'s' if len(names) > 1 else ''}: {', '.join(names)}"))
    print(dim(f"up to {cfg.max_rounds} rounds, gauntlet of {cfg.gauntlet}, bar {cfg.bar}/10, "
              f"prior art kills at {cfg.prior_art_stars}★\n"))

    try:
        while state["round"] < cfg.max_rounds:
            if cfg.max_cost and state["cost"] >= cfg.max_cost:
                print(yellow(f"Stopping: spent ${state['cost']:.2f}, limit ${cfg.max_cost:.2f}."))
                break
            rnd = state["round"] + 1
            tag = f"{rnd:02d}"
            print(bold(f"── Round {rnd} ") + dim(f"(graveyard: {len(state['graveyard'])})"))

            idea, usage = run_agent(
                role=f"hunter·{hunter.kind}", backend=hunter, system=hunter_system, schema=IDEA_SCHEMA, cfg=cfg,
                transcript_base=run_dir / "transcripts" / f"{tag}-hunter", color=cyan,
                prompt=(f"{state['brief']}\n\n# Graveyard (dead ideas, do not resubmit)\n\n"
                        f"{graveyard_text(state['graveyard'])}\n\nPitch your single strongest idea for this person."),
            )
            spend(usage)
            print(cyan(f"    {'pitch':<19}") + bold(idea["name"]) + " — " + idea["one_liner"])

            verdicts: list[dict] = []
            judges: list[str] = []
            cause = None
            for g in range(1, cfg.gauntlet + 1):
                destroyer = destroyers[(g - 1) % len(destroyers)]
                prior = ""
                if verdicts:
                    prior = ("\n\n# Earlier attacks\n\nAnother Destroyer already attacked this idea and let it "
                             "live. Assume they were too soft. Re-run prior art with search terms they did not "
                             "use and find what they missed.\n\n" + "\n\n".join(verdict_text(v) for v in verdicts))
                verdict, usage = run_agent(
                    role=f"destroyer{g}·{destroyer.kind}", backend=destroyer, system=destroyer_system,
                    schema=VERDICT_SCHEMA, cfg=cfg, color=red,
                    transcript_base=run_dir / "transcripts" / f"{tag}-destroyer{g}",
                    prompt=f"{state['brief']}\n\n# The idea to destroy\n\n{idea_text(idea)}{prior}",
                )
                spend(usage)
                verdicts.append(verdict)
                judges.append(destroyer.label)
                cause = judge(verdict, cfg)
                scores = " ".join(f"{k[:4]}:{verdict['scores'][k]}" for k in SCORE_KEYS)
                if cause:
                    print(red(f"    {'✗ killed':<19}") + clip(cause, 160) + dim(f"  [{scores}]"))
                    break
                print(green(f"    {'✓ survives':<19}") + clip(verdict["summary"], 160) + dim(f"  [{scores}]"))

            save_json(run_dir / "rounds" / f"{tag}.json",
                      {"hunter": hunter.label, "destroyers": judges, "idea": idea,
                       "verdicts": verdicts, "cause_of_death": cause})
            state["round"] = rnd
            log(f"## Round {rnd}: {idea['name']}\n\n> {idea['one_liner']}\n\n*Hunter: {hunter.label}*\n\n"
                + "\n\n".join(f"**Destroyer {i} ({judges[i - 1]})**\n\n{verdict_text(v)}" for i, v in enumerate(verdicts, 1))
                + f"\n\n**Outcome:** {'KILLED: ' + cause if cause else 'SURVIVED'}\n")

            if cause:
                last = verdicts[-1]
                state["graveyard"].append({
                    "round": rnd, "name": idea["name"], "one_liner": idea["one_liner"],
                    "cause_of_death": cause, "prior_art": last["prior_art"][:5],
                    "what_would_save_it": last["what_would_save_it"], "scores": last["scores"],
                })
                save_json(state_path, state)
                print()
                continue

            state["winner"] = idea["name"]
            save_json(state_path, state)
            path = write_winner(run_dir, state, idea, verdicts, judges, hunter.label)
            print("\n" + green(bold(f"Winner after {rnd} round(s): {idea['name']}")))
            print(f"  {idea['one_liner']}")
            print(f"  why you: {clip(idea['why_this_person'], 200)}")
            print(f"  hook: {clip(idea['hook'], 200)}")
            print(dim(f"  full report: {path}"))
            break
        else:
            print(yellow(f"\nNo idea survived {cfg.max_rounds} rounds."))
    except KeyboardInterrupt:
        save_json(state_path, state)
        print(yellow(f"\nInterrupted. Resume with: ideaharness --resume {run_dir}"))
        return 130
    except AgentError as exc:
        spend(exc.usage)
        save_json(state_path, state)
        print(red(f"\n{exc}") + f"\nResume with: ideaharness --resume {run_dir}")
        return 1

    if not state.get("winner") and state["graveyard"]:
        print(bold("\nNear misses (highest average score):"))
        for g in sorted(state["graveyard"], key=lambda g: -sum(g["scores"].values()))[:3]:
            avg = sum(g["scores"].values()) / len(SCORE_KEYS)
            print(f"  {avg:.1f}  {g['name']}: {clip(g['cause_of_death'], 120)}")
        print(dim(f"  continue with: ideaharness --resume {run_dir} --max-rounds {cfg.max_rounds + 10}"))

    spent = Usage(cost=state["cost"], cost_known=state["cost"] > 0, tokens=state.get("tokens", 0))
    print(dim(f"\nused so far: {spent.text()} (costs at list price, where the harness reports them) · log: {run_dir / 'log.md'}"))
    return 0


def write_winner(run_dir, state, idea, verdicts, judges, hunter_label) -> pathlib.Path:
    parts = [
        f"# {idea['name']}\n",
        f"> {idea['one_liner']}\n",
        f"Survived {len(verdicts)} Destroyer attack(s) in round {state['round']} after "
        f"{len(state['graveyard'])} idea(s) were killed. Pitched by {hunter_label}; attacked by {', '.join(judges)}.\n",
        "## The idea\n",
        idea_text(idea) + "\n",
    ]
    for i, v in enumerate(verdicts, 1):
        parts += [f"## Destroyer {i} ({judges[i - 1]})\n", verdict_text(v) + "\n",
                  f"What would make it stronger: {v['what_would_save_it']}\n"]
    parts += ["## Graveyard\n", graveyard_text(state["graveyard"]) + "\n"]
    path = run_dir / "winner.md"
    path.write_text("\n".join(parts))
    return path


# ---------------------------------------------------------------- argument parsing

def parse_args(argv: list[str]) -> argparse.Namespace:
    if not argv or argv[0] not in COMMANDS + ("-h", "--help", "--version"):
        argv = ["run", *argv]

    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("agents")
    g.add_argument("--agent", default="claude", metavar="SPEC",
                   help=f"harness for every role unless overridden: {', '.join(ALL_KINDS)}, optionally with :model "
                        "(e.g. claude:opus, codex:gpt-5.5, opencode:deepseek/deepseek-chat, deepseek, ollama:qwen3)")
    g.add_argument("--profiler", metavar="SPEC", help="harness that drafts the profile (default: the hunter's)")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="reasoning effort, where the harness supports it")
    g.add_argument("--agent-budget", type=float, metavar="USD", help="max USD per Claude Code session")
    g.add_argument("--timeout", type=float, default=20, metavar="MIN", help="minutes before an agent session is killed")
    g.add_argument("--retries", type=int, default=1, help="retries for an agent session that fails")
    g.add_argument("--api-base", metavar="URL", help="base URL for api:<model> (any OpenAI-compatible endpoint)")
    g.add_argument("--api-key-env", metavar="VAR", help="environment variable holding the API key")
    g.add_argument("--api-max-turns", type=int, default=40, help="tool-calling turns for API backends")
    g.add_argument("--num-ctx", type=int, default=32768, help="context window to load Ollama models with")
    p = common.add_argument_group("profile")
    p.add_argument("--profile", default=str(HOME_DIR / "profile.md"), metavar="FILE", help="profile of the person the ideas are for")
    p.add_argument("--memory", action="append", metavar="PATH", help="extra memory file or folder to read (repeatable)")
    p.add_argument("--no-github", action="store_true", help="don't read your public GitHub repos into the profile")
    p.add_argument("--no-questions", action="store_true", help="don't ask questions while building the profile")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask before sending memory files to an agent")
    common.add_argument("--out", default=str(HOME_DIR / "runs"), metavar="DIR", help="directory for run folders")

    parser = argparse.ArgumentParser(
        prog="ideaharness",
        description="A Hunter agent pitches ideas for you; Destroyer agents kill them until one survives.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ideaharness {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="hunt for an idea (the default command)",
                         formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    run.add_argument("--hunter", metavar="SPEC", help="harness for the Hunter (default: --agent)")
    run.add_argument("--destroyer", action="append", metavar="SPEC",
                     help="harness for the Destroyers; repeat to rotate harnesses through the gauntlet (default: --agent)")
    run.add_argument("--brief", default=str(BRIEFS / "default.md"), help="what kind of idea to look for")
    run.add_argument("--focus", help="extra direction for this run, e.g. 'tools for Rust developers'")
    run.add_argument("--no-profile", action="store_true", help="hunt without a profile")
    run.add_argument("--max-rounds", type=int, default=12, help="give up after this many pitches")
    run.add_argument("--gauntlet", type=int, default=2, help="Destroyer attacks an idea must survive in a row")
    run.add_argument("--bar", type=int, default=7, help="minimum for every score (novelty, demand, goal fit, feasibility)")
    run.add_argument("--prior-art-stars", type=int, default=200, help="star count at which an existing project doing the same thing kills an idea")
    run.add_argument("--max-cost", type=float, metavar="USD", help="stop starting rounds once reported cost reaches this")
    run.add_argument("--resume", metavar="DIR", help="continue a run folder with its brief and graveyard")

    prof = sub.add_parser("profile", parents=[common], help="build or rebuild your profile from agent memory and a few questions")
    prof.add_argument("--show", action="store_true", help="print the current profile and exit")

    sub.add_parser("doctor", parents=[common], help="show which harnesses, tools and memory files are available")
    sub.add_parser("mcp", help="run the read-only research tools as a stdio MCP server")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if cfg.command == "profile":
            return cmd_profile(cfg)
        if cfg.command == "doctor":
            return cmd_doctor(cfg)
        if cfg.command == "mcp":
            from .mcp_server import main as serve
            serve()
            return 0
        return cmd_run(cfg)
    except BackendError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 2
    except AgentError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 1
