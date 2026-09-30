# Analyst brief (Phase 8 evaluation, #70)

This is the task each evaluated agent gets. `{IGNITION_MCP}` is the ignition-mcp checkout and
`{ROOT}` this repository. The agent's persona and method are ignition-mcp's BMS/EMS analyst
assistant; this brief adds only the incident and the rules of the evaluation.

---

You are the analyst described in `{IGNITION_MCP}/assistants/bms-ems-analyst/system-prompt.md`.
Read it first, and load its skills from `{IGNITION_MCP}/assistants/bms-ems-analyst/skills/`
(read the `SKILL.md` files and their references) as it tells you to.

The operator writes:

> Something is wrong in the data hall's plant. Over roughly the last {WINDOW_MIN} minutes
> things have not looked normal. Please find out what happened, what caused it and what it
> affects.

Rules of the evaluation:

- Your only access to the plant is the Runtime MCP server, through this command:
  `cd {ROOT} && python3 tests/agents/mcp.py call <tool> '<json arguments>'`
  (`python3 tests/agents/mcp.py tools` lists the tools). The REST server is not connected, and
  no Tool lists active alarms; alarm points are Boolean tags you can read and trend.
- Do not read any other file in `{ROOT}` or elsewhere except the assistant's prompt and
  skills, and do not use the network any other way. The operator is not available for
  questions or field checks; say which field checks you would ask for.
- The gateway's clock is UTC. The incident happened within the last {WINDOW_MIN} minutes.

Finish with your incident answer, then a final fenced `json` block, exactly this shape:

```json
{
  "root_cause": [{"asset": "<tag folder of the equipment, as Graphene shows it>", "cause": "<what failed, in a few words>"}],
  "impact": "<what the cause affected, one or two sentences>",
  "confidence": "High | Medium | Low",
  "evidence": ["<tag path>", "..."]
}
```

List more than one root cause only if you find independent failures.
