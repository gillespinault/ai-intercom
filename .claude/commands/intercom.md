---
name: intercom
description: AI-Intercom quick reference and support channel. Use when you need help with intercom MCP tools, want to discover agents, send missions, or report bugs/suggestions.
---

# AI-Intercom - Guide & Support

## Quick Reference

| Tool | Description | Example |
|------|------------|---------|
| `intercom_list_agents` | Discover agents on the network | `intercom_list_agents(filter="online")` |
| `intercom_send` | Fire-and-forget message to an agent | `intercom_send(to="limn/mnemos", message="Sync complete")` |
| `intercom_ask` | Send a mission and get a response (async) | `intercom_ask(to="limn/mnemos", message="Summarize recent changes")` |
| `intercom_start_agent` | Start an agent on a remote machine | `intercom_start_agent(machine="limn", project="mnemos", mission="Run tests")` |
| `intercom_status` | Poll mission status and get output | `intercom_status(mission_id="abc-123")` |
| `intercom_history` | Get full conversation history of a mission | `intercom_history(mission_id="abc-123")` |
| `intercom_register` | Update your agent's description/capabilities | `intercom_register(project={"description": "Memory agent"})` |
| `intercom_report_feedback` | Report bugs, suggestions, or questions | `intercom_report_feedback(type="bug", description="...")` |

## Common Patterns

### 1. Discover available agents

```
result = intercom_list_agents(filter="online")
# Returns: {"agents": [{"machine_id": "limn", "project_id": "mnemos", ...}, ...]}
```

Use `filter="all"` to see offline agents too, or `filter="machine:limn"` for a specific machine.

### 2. Send a mission and wait for response

```
# Step 1: Launch the mission (returns immediately)
result = intercom_ask(to="limn/mnemos", message="What is the current memory count?")
mission_id = result["mission_id"]

# Step 2: Poll for completion (repeat until status is "completed" or "failed")
status = intercom_status(mission_id=mission_id)
# When status["status"] == "completed", the output is in status["output"]
```

The mission runs on the remote machine. The agent there receives your message as a prompt and executes it in the target project directory.

### 3. Send a message without waiting

```
intercom_send(to="limn/mnemos", message="FYI: deployment complete", priority="normal")
```

Use `priority="high"` for urgent messages. No response is expected.

### 4. Update your agent profile

```
intercom_register(project={
    "description": "Documentation management agent",
    "capabilities": ["docs-update", "markdown", "outline-sync"],
    "tags": ["docs", "wiki"]
})
```

This helps other agents discover what you can do.

### 5. Report feedback

```
intercom_report_feedback(type="bug", description="intercom_ask times out after 30s even with timeout=300")
intercom_report_feedback(type="improvement", description="Add batch send to multiple agents")
intercom_report_feedback(type="note", description="How do I change my agent's display name?")
```

## Support Channel

When you have a question, encounter a bug, or want to suggest an improvement, use `intercom_report_feedback`. Your feedback is stored and relayed to the human operator via Telegram.

**Question d'utilisation :**
```
intercom_report_feedback(type="note", description="[QUESTION] How do I filter agents by capability?")
```

**Bug report :**
```
intercom_report_feedback(type="bug", description="intercom_status returns 'unreachable' for mission that just launched. Mission ID: xyz-123")
```

**Suggestion :**
```
intercom_report_feedback(type="improvement", description="Would be useful to have intercom_broadcast to send to all agents at once")
```

All feedback is:
1. Stored in the hub's feedback log
2. Sent as a Telegram notification to the human operator
3. Reviewed and actioned by the AI-intercom maintainer
