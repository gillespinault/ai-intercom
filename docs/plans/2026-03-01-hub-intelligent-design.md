# Design: Hub Intelligent - Push Model

**Date**: 2026-03-01
**Status**: Approved
**Scope**: Refonte du data flow pour simplifier l'architecture

## Problem Statement

Le data flow actuel repose sur un modele **pull** : le Hub poll les daemons pour obtenir le statut des missions. Cela cree :

- **12 hops** pour un simple `intercom_ask` (Agent → MCP → HubClient → Hub → Daemon → Launcher, puis polling en boucle)
- **~14 serialisations** par requete (dict → JSON → bytes → HMAC → etc.)
- **10-20s de latence** due au polling toutes les 10s
- **~200 LOC de plomberie** (polling loop, proxy status, double mission_store)
- **Double source de verite** : le Hub et le Daemon stockent chacun l'etat des missions

## Solution: Push Model

Inverser le flux : les daemons **PUSH** les resultats au Hub au lieu d'etre polles.

### Flow actuel (pull)

```
Agent A → Hub → Daemon B (lance agent)
Agent A → Hub → Daemon B (poll status ×10, toutes les 10s)
Daemon B → Hub (proxy result)
Hub → Agent A (result)
```

**Hops**: 12 | **Latence**: 10-20s | **LOC polling**: ~200

### Flow propose (push)

```
Agent A → Hub → Daemon B (lance agent)
         ...agent travaille...
Daemon B → Hub (push feedback batch toutes les 30s)
Daemon B → Hub (push result final quand termine)
Agent A → Hub (get result, deja la)
```

**Hops**: 6 | **Latence**: <1s | **LOC polling**: 0

## Architecture Changes

### Nouveaux endpoints sur le Hub

#### `POST /api/missions/{mission_id}/feedback`

Recoit les updates de progression periodiques depuis les daemons.

```json
{
    "machine_id": "machine_b",
    "feedback": [
        {"timestamp": "2026-03-01T10:00:30Z", "kind": "tool", "summary": "Reading src/config.py"},
        {"timestamp": "2026-03-01T10:00:35Z", "kind": "text", "summary": "Analyzing configuration"}
    ],
    "turn_count": 3,
    "status": "running"
}
```

Actions du Hub :
- Stocker les feedback items dans `mission_store[mission_id]`
- Poster les updates dans le topic Telegram de la mission
- Retourner `{"status": "ok"}`

#### `POST /api/missions/{mission_id}/result`

Recoit le resultat final depuis le daemon quand l'agent termine.

```json
{
    "machine_id": "machine_b",
    "status": "completed",
    "output": "Resultat final de l'agent...",
    "feedback": [],
    "started_at": "2026-03-01T10:00:00Z",
    "finished_at": "2026-03-01T10:05:30Z",
    "turn_count": 5
}
```

Actions du Hub :
- Stocker le resultat dans `mission_store[mission_id]`
- Poster le resultat dans le topic Telegram (avec status emoji)
- Retourner `{"status": "ok"}`

### Endpoint simplifie sur le Hub

#### `GET /api/missions/{mission_id}/status` (modifie)

Avant : proxy vers le daemon cible (lookup machine → HTTP GET → forward response).
Apres : lecture directe depuis `mission_store` (local, pas de reseau).

```json
{
    "mission_id": "m-20260301-abc123",
    "status": "completed",
    "output": "Resultat final...",
    "feedback": [
        {"timestamp": "...", "kind": "tool", "summary": "..."}
    ],
    "started_at": "2026-03-01T10:00:00Z",
    "finished_at": "2026-03-01T10:05:30Z",
    "turn_count": 5
}
```

### Changements cote Daemon

Le daemon devient **actif** : il push ses resultats au Hub.

#### Feedback pusher (background task)

```python
async def _feedback_pusher(self, mission_id):
    """Push feedback items au Hub toutes les 30s."""
    cursor = 0
    while self._results[mission_id].status == "running":
        await asyncio.sleep(30)
        result = self._results[mission_id]
        new_feedback = result.feedback[cursor:]
        if new_feedback:
            await self.hub_client.push_feedback(
                mission_id=mission_id,
                feedback=[f.to_dict() for f in new_feedback],
                turn_count=result.turn_count,
                status="running"
            )
            cursor += len(new_feedback)
```

#### Result push (on completion)

```python
async def _run_agent(self, mission_id, ...):
    # ... execute agent comme avant ...

    # Push result au Hub
    await self.hub_client.push_result(
        mission_id=mission_id,
        status=result.status,
        output=result.output,
        feedback=[f.to_dict() for f in result.feedback[cursor:]],
        started_at=result.started_at,
        finished_at=result.finished_at,
        turn_count=result.turn_count
    )
```

#### Nouvelles methodes HubClient

```python
async def push_feedback(self, mission_id, feedback, turn_count, status):
    await self._post(f"/api/missions/{mission_id}/feedback", {
        "machine_id": self.machine_id,
        "feedback": feedback,
        "turn_count": turn_count,
        "status": status
    })

async def push_result(self, mission_id, status, output, feedback,
                       started_at, finished_at, turn_count):
    await self._post(f"/api/missions/{mission_id}/result", {
        "machine_id": self.machine_id,
        "status": status,
        "output": output,
        "feedback": feedback,
        "started_at": started_at,
        "finished_at": finished_at,
        "turn_count": turn_count
    })
```

## Code a supprimer

| Fichier | Fonction | LOC | Raison |
|---------|----------|-----|--------|
| `hub_api.py` | `_track_mission_for_telegram()` | ~140 | Polling loop remplace par push |
| `hub_api.py` | `get_daemon_mission_status()` | ~60 | Proxy remplace par lecture locale |
| `daemon/api.py` | Simplification de `mission_status()` | ~20 | Le daemon n'a plus besoin de servir le status au Hub |

## Code a ajouter

| Fichier | Fonction | LOC | Raison |
|---------|----------|-----|--------|
| `hub_api.py` | `receive_feedback()` | ~25 | Recevoir les feedback batch |
| `hub_api.py` | `receive_result()` | ~25 | Recevoir le resultat final |
| `hub_client.py` | `push_feedback()` + `push_result()` | ~30 | Methodes d'envoi |
| `agent_launcher.py` | `_feedback_pusher()` | ~20 | Background task 30s |

**Bilan net : ~-120 LOC**

## Erreurs structurees (bonus)

Format standard pour toutes les reponses :

```json
{
    "status": "ok",
    "data": { "..." }
}
```

ou en cas d'erreur :

```json
{
    "status": "error",
    "error": {
        "code": "DAEMON_UNREACHABLE",
        "message": "Cannot reach daemon at http://...:7700",
        "hint": "Check if daemon is running: systemctl status ai-intercom-daemon"
    }
}
```

Codes d'erreur :
- `TARGET_NOT_FOUND` - machine/projet inexistant
- `DAEMON_UNREACHABLE` - daemon hors ligne
- `SESSION_NOT_FOUND` - pas de session active pour chat
- `MISSION_NOT_FOUND` - mission_id inconnu
- `AUTH_FAILED` - signature HMAC invalide
- `LAUNCH_FAILED` - agent n'a pas pu demarrer

## Ce qui ne change PAS

- Architecture Hub-Daemon-MCP
- HMAC auth
- Telegram forum topics
- Inbox JSONL pour chat
- MCP tool definitions
- Agent launcher subprocess
- `intercom_chat` flow (deja optimal)

## Risques et mitigations

| Risque | Mitigation |
|--------|-----------|
| Daemon ne peut pas joindre le Hub pour push | Retry avec backoff exponentiel (3 tentatives). Le daemon garde les resultats en memoire localement comme fallback. |
| Hub redemarrre pendant une mission | Le daemon re-push le result au prochain heartbeat si mission_id non trouve sur le Hub. |
| Feedback batch perdu (network glitch) | Non-critique : le result final contient tous les feedback items restants. |
| Backward compatibility | Les daemons anciens (pre-push) continuent de fonctionner : le Hub peut garder le proxy comme fallback temporaire. |
