#!/usr/bin/env python3
"""Generate outrigger/data/commands.json from Hermes's published slash-command reference.

Hermes has no public API for its command catalog yet (NousResearch/hermes-agent#149673),
so the Outrigger platform ships a build-time list taken from the "Messaging slash
commands" table of website/docs/reference/slash-commands.md. Rerun after a Hermes
upgrade:

    python tools/gen_command_catalog.py [path/to/hermes-agent]

The default path is ~/.hermes/hermes-agent. Commands that make no sense on an
Outrigger client (platform-protocol pings, Telegram- or Discord-only ones) are left out.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

EXCLUDE = {"start", "topic", "login", "<skill-name>"}
ROW = re.compile(r"^\|\s*`/(?P<cmd>[^`]+)`(?P<rest_cmd>[^|]*)\|\s*(?P<desc>.*?)\s*\|\s*$")


def first_sentence(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # links -> text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = text.replace("\\|", "|")
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()[:160]


def parse(md: str) -> list[dict]:
    section = md.split("## Messaging slash commands", 1)[1].split("\n## ", 1)[0]
    out, seen = [], set()
    for line in section.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        head = (m.group("cmd") + m.group("rest_cmd")).replace("\\|", "|")
        name, _, args = m.group("cmd").replace("\\|", "|").partition(" ")
        if name in EXCLUDE or name in seen:
            continue
        aliases = re.findall(r"`/([^`\s]+)`", m.group("rest_cmd"))
        seen.add(name)
        out.append({"name": name, "args_hint": args.strip(), "aliases": aliases,
                    "description": first_sentence(m.group("desc")), "source": "builtin"})
    return out


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes/hermes-agent")
    doc = root / "website/docs/reference/slash-commands.md"
    commands = parse(doc.read_text())
    try:
        version = subprocess.run(["git", "-C", str(root), "describe", "--tags", "--always"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        version = ""
    target = Path(__file__).resolve().parents[1] / "outrigger" / "data" / "commands.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({"source": "hermes docs: reference/slash-commands.md (Messaging slash commands)",
                                  "hermes_version": version, "commands": commands}, indent=1) + "\n")
    print(f"{len(commands)} commands -> {target}")


if __name__ == "__main__":
    main()
