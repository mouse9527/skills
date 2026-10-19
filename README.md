# skills

Personal agent skills, shared across Claude Code, Codex and Pi via symlinks.

| skill | what |
|---|---|
| [herdr-workers](herdr-workers/SKILL.md) | Dispatch, lay out, monitor and verify worker agents with [herdr](https://herdr.dev) (Chinese) |

## Install

```bash
git clone https://github.com/mouse9527/skills.git ~/workspaces/skills
for d in ~/.claude/skills ~/.codex/skills ~/.pi/agent/skills; do
  mkdir -p "$d"
  for s in ~/workspaces/skills/*/; do ln -sfn "${s%/}" "$d/$(basename "$s")"; done
done
```

Edit skills here and commit; every agent picks up the change through the symlink.
