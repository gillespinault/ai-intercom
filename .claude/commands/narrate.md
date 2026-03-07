---
name: narrate
description: Activate TTS narrator mode — check if the PWA Attention Hub has listeners and which categories are enabled. Use at the start of multi-step plans or when working on significant tasks.
---

# TTS Narrator Mode

## Step 1: Check presence and settings

Call the Attention Hub presence endpoint:

```bash
curl -s http://localhost:7700/api/attention/presence
```

Response example:
```json
{
  "connected_clients": 2,
  "active_sessions": 1,
  "tts": {
    "enabled": true,
    "categories": {
      "milestone": true,
      "difficulty": true,
      "didactic": false,
      "attention": true,
      "permission": true,
      "lifecycle": true
    }
  }
}
```

## Step 2: Decide narration mode

- `connected_clients == 0` → **Silent mode**. No narration.
- `connected_clients > 0` but `tts.enabled == false` → **Silent mode**. User disabled TTS.
- `connected_clients > 0` and `tts.enabled == true` → **Narrator mode**. Only use categories where `tts.categories.<name>` is `true`.

## Step 3: If narrator mode is active

Use `intercom_announce()` only for **enabled categories**. For example if `didactic` is `false`, skip didactic announcements entirely.

### When to announce

| Moment | Category | Example |
|--------|----------|---------|
| Phase/step of a plan completed | `milestone` | "Phase 2 terminee, tous les tests passent" |
| All tests green after changes | `milestone` | "129 tests OK, je passe au deploiement" |
| Deployment done | `milestone` | "Hub redeploye, le proxy TTS repond en 200" |
| 3rd retry on a failing test/API | `difficulty` | "Bloque sur un test flaky, je retente" |
| Unexpected error blocking progress | `difficulty` | "Erreur 502 du proxy TTS, j'investigue" |
| Starting a new major task section | `didactic` | "Je refactorise le parser pour gerer les edge cases" |
| Explaining a non-obvious choice | `didactic` | "J'utilise pyte au lieu de regex pour le stripping ANSI" |

### Rules

- **French**, conversational tone
- **Max 200 characters** (XTTS limit is 250, keep margin)
- **Don't over-narrate** — only significant moments, not every file edit
- **1-2 announcements per plan phase** is a good cadence
- Priority `"normal"` for most, `"high"` only for blockers or final completion
- **Skip disabled categories** — respect user's preferences

### Example calls

```python
intercom_announce(message="Je commence par ajouter l'endpoint de presence au hub", category="didactic")
intercom_announce(message="Tests OK, 2 sur 2. Je passe au deploiement", category="milestone")
intercom_announce(message="Erreur 502, le hostname ne resout pas dans Docker", category="difficulty")
```

## Step 4: If silent mode

Work normally without calling `intercom_announce()`. No need to mention this to the user.

## Integration with plan execution

Check presence **once at the start** of a multi-step task. If active, narrate throughout. Don't re-check between steps.
