#!/usr/bin/env python3
"""IdeaHarness: a Hunter agent pitches project ideas for you; Destroyer agents kill them
until one survives. Works with Claude Code, Codex, Gemini CLI, OpenCode, DeepSeek, Ollama,
OpenRouter or any other harness. Standard library only.

    python3 ideaharness.py doctor     # which harnesses and memory files are available
    python3 ideaharness.py profile    # build your profile from agent memory + a few questions
    python3 ideaharness.py            # hunt
"""

import sys

from harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
