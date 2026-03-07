# Design: Telegram Voice Conversation Pipeline

**Date**: 2026-03-07
**Status**: Approved
**Scope**: STT/TTS fixes, Telegram response visibility, conversation supervisee, PWA preferences, voix agents

---

## Context

Analyse d'une session vocale Telegram du 7 mars 2026 qui a revele plusieurs problemes:

1. **STT tronque** — Un message vocal de ~1min a ete partiellement transcrit (phrase sur l'imprimante POS perdue). Le serveur Whisper STT sur le Jetson Thor est concu pour des segments courts (5-10s via VAD). Un audio complet de 1-2min depasse la fenetre 30s de Whisper.

2. **Reponse invisible** — Le poeme genere par l'agent n'a jamais ete vu sur Telegram. La reponse etait editee dans le message de progression (`edit_text`) au lieu d'etre envoyee comme nouveau message. Le Markdown V2 (`**bold**`) dans le poeme etait incompatible avec le `parse_mode="Markdown"` (V1).

3. **Pas de conversation active** — Le dispatcher est fire-and-forget. Pas de mecanisme pour injecter un message dans une mission en cours, pas de suivi conversationnel multi-etapes.

4. **Preferences eparpillees** — TTS prefs dans le PWA, config dispatcher dans config.yml, pas de controle utilisateur sur le comportement conversationnel.

## Infrastructure existante (Jetson Thor)

| Port | Service | Usage |
|------|---------|-------|
| 8430 | Speaker Embeddings (CAM++) | Mnemos |
| 8431 | CosyVoice3 TTS | Mnemos |
| 8432 | Whisper STT (large-v3-turbo) | AI-intercom + Mnemos |
| 8433 | XTTS v2 TTS | AI-intercom |

NeMo ASR n'est PAS deploye (code existe mais pas dans docker-compose).

## Lecons de Mnemos

- Whisper est utilise UNIQUEMENT pour des segments courts (VAD-segmentes, 5-10s)
- TTS: chunking par phrases, max 250 chars par requete
- Timeouts: connect=5s, read=30s
- Filtre hallucinations: probabilite mots + phrases connues + repetitions
- Le client Whisper mnemos utilise `word_timestamps: true` et `initial_prompt` (keyterms)

---

## Section 1: STT Pipeline

### 1.1 Chunking aligne fenetre Whisper

Fichier: `src/hub/voice_services.py`

Quand l'audio PCM depasse 25 secondes:
- Decouper en segments de 25s (fenetre native Whisper = 30s, marge de securite)
- Passer la transcription du segment N comme `initial_prompt` du segment N+1 (coherence contextuelle)
- Concatener les transcriptions

```
OGG Telegram -> PCM 16kHz -> [segments 25s] -> POST /v1/stt chacun -> concat texte
```

### 1.2 Word timestamps et detection de troncation

- Ajouter `word_timestamps: true` dans le payload STT
- Apres transcription, verifier que le dernier mot couvre approximativement la duree de l'audio
- Logger un warning si la couverture est < 80% de la duree

### 1.3 Filtre hallucinations

Copier le module `hallucination_filter.py` de mnemos (50 lignes):
- Phrases connues: "sous-titrage st", "merci d'avoir regarde", etc.
- Repetitions: meme mot (3+ chars) repete 3+ fois
- Probabilite mots faible (< 0.30) via `words` array

Fichier: `src/hub/hallucination_filter.py`

### 1.4 Timeouts

Aligner sur mnemos: `httpx.Timeout(30.0, connect=5.0)` par segment (au lieu de `timeout=60` global).

### 1.5 Logging diagnostic

Logger avant chaque POST STT:
- Duree audio estimee (len(pcm) / 16000 / 2)
- Taille payload base64
- Numero de segment / total segments

### 1.6 TTS par phrases (reponses longues)

Fichier: `src/hub/voice_services.py` (`synthesize`)

- Decouper le texte en phrases (split sur `.!?` suivi d'espace)
- Max 250 chars par phrase (aligne mnemos)
- Synthetiser chaque phrase avec XTTS
- Concatener les PCM, convertir en OGG final

---

## Section 2: Reponse Telegram visible

### 2.1 Nouveau message au lieu d'edit

Fichier: `src/hub/main.py` (`on_dispatch`)

La reponse finale est envoyee comme `reply_text()` separe. Le `thinking_msg` est mis a jour avec un statut final court:
```
Ancien: thinking_msg.edit_text(full_output)  # invisible, Markdown casse
Nouveau: thinking_msg.edit_text("Termine en 4m25s")
         update.message.reply_text(response)  # nouveau message clair
```

### 2.2 Sanitization Markdown V1

Nouvelle fonction `_sanitize_markdown_v1(text)`:
- `**bold**` -> `*bold*` (V2 -> V1)
- Echapper les `_`, `*`, backtick non-apparies
- Fallback plain text si l'envoi Markdown echoue

### 2.3 Split messages longs

Si la reponse depasse 4000 chars:
- Decouper sur les frontieres de paragraphe (`\n\n`)
- Envoyer chaque partie comme message separe
- Plus de troncation avec "... (tronque)"

---

## Section 3: Conversation supervisee

### 3.1 ActiveConversationManager

Nouveau fichier: `src/hub/active_conversations.py`

```python
class ActiveConversation:
    user_id: int
    mission_id: str
    daemon_url: str
    started_at: float
    last_activity: float
    status: Literal["active", "completed", "failed"]

class ActiveConversationManager:
    # Une seule conversation active par utilisateur
    _active: dict[int, ActiveConversation]

    def start(user_id, mission_id, daemon_url) -> None
    def get_active(user_id) -> ActiveConversation | None
    def touch(user_id) -> None          # reset last_activity
    def close(user_id) -> None
    def cleanup_stale(ttl=600) -> None   # 10 min TTL
```

### 3.2 Injection de messages

Modification de `on_dispatch`:

```python
active = conversation_manager.get_active(user_id)
if active and active.status == "active":
    # Injecter dans la session existante
    await inject_message(active.daemon_url, active.mission_id, text)
    conversation_manager.touch(user_id)
else:
    # Nouvelle mission (comportement actuel)
    ...
    conversation_manager.start(user_id, mission_id, daemon_url)
```

L'injection utilise `POST /api/session/deliver` (endpoint daemon existant).

### 3.3 Heuristique nouveau vs lie

Pas de LLM (trop lent/couteux). Regles simples:
- Mission active < 10 min -> toujours injecter
- Mission active > 10 min -> nouvelle conversation
- Commande `/new` -> force nouvelle conversation
- Reply sur un message agent -> injection explicite

### 3.4 Visibilite des echanges agents

Le hub intercepte les messages routes entre agents via le router et les forward dans le fil Telegram:
```
Agent -> POS-printer: "Imprime ce texte..."
POS-printer -> Agent: "Impression OK, 3 lignes"
```

Format condense, pas le contenu complet des payloads.

---

## Section 4: Preferences PWA

### 4.1 Backend

Nouveau endpoint dans `attention_api.py`:
- `GET /api/attention/dispatcher-prefs`
- `PATCH /api/attention/dispatcher-prefs`

Meme pattern que `tts-prefs`: JSON persiste + broadcast WebSocket.

### 4.2 Preferences

| Cle | Default | Description |
|-----|---------|-------------|
| `conversation_active` | true | Mode conversation supervisee |
| `show_agent_exchanges` | true | Forward messages inter-agents dans Telegram |
| `voice_response` | true | TTS sur reponses aux messages vocaux |
| `auto_print_pos` | false | Envoie resultats longs a l'imprimante POS |
| `hear_agents` | false | Synthetise echanges inter-agents avec voix distinctes |

### 4.3 PWA (index.html + app.js)

Nouvelle section "Dispatcher" dans le panel prefs:
- Separateur entre les sections TTS et Dispatcher
- Toggles pour chaque preference
- Sync WebSocket (meme pattern que TTS prefs)

---

## Section 5: Voix des agents (bonus)

### 5.1 Voice style par agent

Le registry est enrichi d'un champ `voice_style` par agent:

```python
{
    "agent_id": "serverlab/AI-intercom",
    "display_name": "Intercom",
    "voice_style": "Speak with a calm, professional male French voice"
}
```

Defaults automatiques si non configure:
- Dispatcher: voix feminine (actuelle, via config.yml `tts_instruct`)
- Agents projet: voix masculine calme
- Agents infra: voix masculine technique

### 5.2 Synthese inter-agent

Quand le toggle `hear_agents` est ON:
1. Message inter-agent capte (Section 3.4)
2. Synthetise via XTTS avec le `instruct` = `voice_style` de l'agent source
3. Envoye au PWA via `/api/attention/tts`

### 5.3 Configuration future

Les `voice_style` seront configurables dans le panel prefs PWA dans une version ulterieure. V1 = defaults dans config.yml.

---

## Fichiers impactes

| Fichier | Sections | Nature |
|---------|----------|--------|
| `src/hub/voice_services.py` | 1.1-1.6 | Refactor majeur (chunking STT, TTS phrases) |
| `src/hub/hallucination_filter.py` | 1.3 | Nouveau (copie mnemos) |
| `src/hub/main.py` | 2.1-2.3, 3.2-3.4 | Refactor on_dispatch |
| `src/hub/active_conversations.py` | 3.1 | Nouveau |
| `src/hub/attention_api.py` | 4.1 | Extension (dispatcher-prefs endpoint) |
| `src/hub/attention_store.py` | 4.1 | Extension (dispatcher prefs storage) |
| `src/hub/telegram_bot.py` | 2.2 | Sanitization Markdown |
| `src/hub/registry.py` | 5.1 | Extension (voice_style field) |
| `pwa/index.html` | 4.3 | Extension (prefs section) |
| `pwa/app.js` | 4.3 | Extension (prefs sync) |
| `config/config.yml` | 5.1, 5.3 | Extension (voice defaults) |
| Tests | Toutes | Nouveaux tests pour chaque section |
