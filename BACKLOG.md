# AI-Intercom Backlog

Feature ideas et ameliorations a potentiellement implementer.
Triees par priorite estimee (haute en premier).

---

## Haute priorite

### 1. SAV automatique par projet (maintainer routing)
Chaque projet a un "maintainer" (par defaut = lui-meme). Quand un agent a un probleme avec un service, il contacte automatiquement l'agent responsable.
- Ajouter champ `maintainer` au registre projets
- Tool MCP `intercom_support(service, issue)` qui route vers le bon maintainer
- Ex: probleme avec intercom → route vers `serverlab/ai-intercom`
- Ex: probleme avec TTS → route vers `limn/jetson-thor`
- ~~Le tool `intercom_report_feedback` existant ecrit dans un JSONL~~ → FAIT (v0.2.1) : feedback + notification Telegram
- Reste : routing actif vers le maintainer agent au lieu de juste notifier l'humain

### ~~2. Conversation vocale via Telegram (STT + TTS via limn)~~ → FAIT (v0.5.0)
~~Permettre une conversation vocale avec le dispatcher Telegram en utilisant les services STT et TTS heberges sur limn (Jetson Thor).~~
Implemente : Telegram voice messages → STT (Whisper sur limn) → texte → dispatcher → reponse texte → TTS (CosyVoice sur limn) → voice message Telegram. Detection de langue automatique incluse.

### ~~3. Continuite conversationnelle du dispatcher~~ → FAIT (v0.6.0)
~~Actuellement chaque message Telegram est independant (pas de memoire de conversation).~~
Implemente : SQLite-backed conversation memory. Derniers 10 messages (500 chars max) injectes dans le prompt dispatcher. Cleanup automatique >48h. API `GET /api/dispatcher/history` pour recherche dans l'historique.

### ~~TTS Narrator (voice progress announcements)~~ → FAIT (v0.7.0)
~~Permettre aux agents de narrer vocalement leur progression dans la PWA Attention Hub.~~
Implémenté : `intercom_announce` MCP tool → Hub broadcast → PWA `tts.js` → XTTS sur Jetson Thor (24kHz PCM). Catégories : milestone, difficulty, didactic. Préférences par catégorie, volume, verbosité (minimal/informatif), cooldown. Hub proxy TTS avec rate limiting 2s.

### ~~Terminal-only prompt detection + pyte~~ → FAIT (v0.7.0)
~~`notification_data` du hook était unreliable pour détecter le type de prompt (auto-allowed permissions, race conditions).~~
Refactorisé : terminal = seule source de vérité. `claude-pty` utilise pyte (émulateur VT100) au lieu de regex ANSI. Hooks ne trackent que le timing d'activité.

### Known Bugs (discovered v0.5.0 testing)

#### ~~B1. Hub/daemon sync perdue au restart du hub~~ → FIXÉ (aafa345)
~~Quand le hub redémarre après le daemon, toutes les sessions sont perdues.~~
Fix : hub_epoch tracking — le hub assigne un epoch au démarrage, retourné dans les heartbeat responses. Les daemons détectent le changement d'epoch et re-poussent toutes leurs sessions. Recovery en <30s.

#### ~~B2. notification_data vidée sur hook-working~~ → FIXÉ
~~Quand `UserPromptSubmit` fire, `cc-heartbeat.sh working` remet `notification_data` à vide.~~
Fix dual : (1) `cc-heartbeat.sh` préserve `notification_data` du fichier existant lors des transitions `start|stop|working`, (2) `AttentionMonitor` cache le dernier prompt détecté par session et le réutilise lors des transitions hors WAITING.

#### ~~B3. Tmux requis pour interaction PWA complète~~ → FERMÉ (limitation acceptée)
~~Sans tmux, les sessions sont en mode "monitor only".~~
Résolution : `claude-tmux.sh` wrapper disponible (v0.5.0) + `install.sh` configure l'alias automatiquement. Les sessions non-tmux restent en lecture seule dans la PWA mais affichent l'état et le prompt via `notification_data`. Comportement documenté, pas un bug.

---

## Moyenne priorite

### 4. ~~Streaming des reponses dispatcher~~ → FAIT (v0.2.0)
~~Actuellement la reponse arrive en bloc apres execution complete.~~
Implemente via `stream-json` : le daemon collecte les FeedbackItems en temps reel et le hub edite le message Telegram progressivement avec les activites de l'agent (outils utilises, fichiers lus, commandes executees).
Reste a faire : streamer le texte de reponse finale mot-a-mot (actuellement la reponse finale arrive en bloc).

### 5. Routing intelligent multi-target
Le dispatcher envoie tout a un seul target (`serverlab/serverlab`). Il devrait pouvoir router directement vers la bonne machine selon la demande.
- Analyser le message pour determiner la/les machine(s) cible(s)
- "Verifie l'espace disque sur toutes les machines" → fan-out vers serverlab + limn + vps
- Consolidation des resultats avant reponse

### 6. Notifications proactives
Les agents peuvent notifier l'humain via Telegram sans qu'il ait demande.
- Alertes de sante (disque plein, service down)
- Notifications de fin de mission longue
- Rapports periodiques automatiques

### 7. ~~Dashboard web de l'annuaire~~ → PARTIELLEMENT FAIT (v0.5.0)
~~Interface web pour visualiser les machines, projets, statuts, missions en cours.~~
PWA Attention Hub redesigned (v0.5.0) : Industrial Ops Console, machine grouping, sound alerts, event timeline, toast notifications, tmux-centric UX.
~~Terminal viewer sans tmux~~ → FAIT (v0.5.0) : sessions non-tmux enrichies via `notification_data` du hook, affichent prompt info sans terminal view
~~Hooks multi-machine~~ → FAIT (v0.5.0) : `install.sh` telecharge `cc-heartbeat.sh` depuis hub et configure les hooks automatiquement
Reste a faire :
- Historique des missions
- Logs de communication
- Vue annuaire machines/projets complete

---

## Basse priorite / Exploration

### 8. Haute disponibilite du dispatcher
Actuellement le dispatcher est sur serverlab uniquement.
- Failover vers un autre agent si serverlab est down
- Health check du dispatcher target

### 9. File d'attente de missions
Quand un agent est occupe, les missions s'empilent.
- Queue persistante (Redis ou SQLite)
- Priorite des missions
- Retry automatique en cas d'echec

### 10. Authentification inter-machine renforcee
HMAC existe mais les tokens sont souvent vides.
- Rotation automatique des tokens
- mTLS entre machines via Tailscale certs

### 11. Metriques et observabilite
- Temps de reponse par machine/mission
- Cout (tokens Claude) par mission
- Prometheus/Grafana integration

---

## Fait (reference)

- [x] Dispatcher Telegram intelligent via `claude -p` (v1)
- [x] Fire-and-check pattern (intercom_ask non-bloquant)
- [x] Auto-sync IP via heartbeat
- [x] Support multi-tailnet (machine.tailscale_ip override)
- [x] Annuaire avec descriptions et capabilities enrichies
- [x] Auto-approve pour communication cross-machine serverlab
- [x] Auto-decouverte de projets (CLAUDE.md, .claude/)
- [x] Join flow avec approbation Telegram
- [x] Forum topics par mission
- [x] Feedback granulaire des missions (stream-json, FeedbackItem, Telegram live updates)
- [x] Skill `/intercom` (guide MCP + canal SAV) deploye sur serverlab, limn, vps
- [x] Notification Telegram sur feedback agents (bug/improvement/note)
- [x] Chat interactif agent-a-agent via `intercom_chat()` / `intercom_reply()` (v0.3.0)
- [x] Hooks PostToolUse/UserPromptSubmit pour delivery automatique des messages (v0.3.0)
- [x] Inbox fichier + CLI `check-inbox` pour integration hooks (v0.3.0)
- [x] Sessions actives visibles dans heartbeat et `intercom_list_agents()` (v0.3.0)
- [x] Push model : daemons poussent feedback/resultats vers Hub (v0.4.0)
- [x] Attention Hub PWA : dashboard temps-reel des sessions agents (v0.4.0)
- [x] Version tracking dans heartbeat + `machine_version` dans annuaire (v0.4.0)
- [x] Self-upgrade CLI : `ai-intercom self-upgrade` (git pull + pip install + restart) (v0.4.0)
- [x] Hub upgrade API : `POST /api/upgrade` + MCP tool `intercom_upgrade` (v0.4.0)
- [x] GitHub Actions sync monorepo → repo dedie (v0.4.0)
- [x] Bypass CLAUDECODE env var pour lancement agents (v0.4.0)
- [x] Heartbeat hooks Claude Code : `cc-heartbeat.sh` via SessionStart/Stop/Notification/UserPromptSubmit ecrit dans `/tmp/cc-sessions/` (v0.4.0)
- [x] Fix push_attention_event : format event wrapping pour match hub API (v0.4.0)
- [x] PWA redesign Industrial Ops Console : machine grouping, sound alerts, timeline, toasts, tmux-centric UX (v0.5.0)
- [x] Non-tmux session enrichment via `notification_data` hook payload + `parse_notification_data()` (v0.5.0)
- [x] HTTPS via Traefik subdomain (`attention.robotsinlove.be`) au lieu de `tailscale serve` (v0.5.0)
- [x] Hub script serving : `GET /api/scripts/{name}` pour distribution `cc-heartbeat.sh` (v0.5.0)
- [x] Stale session cleanup : sessions >5min sans update supprimees automatiquement (v0.5.0)
- [x] `install.sh` heartbeat hooks setup : telecharge script + configure hooks automatiquement (v0.5.0)
- [x] `last_update` tracking sur `AttentionSession` pour detection staleness (v0.5.0)
- [x] Voice via Telegram : STT (Whisper) + TTS (CosyVoice) sur limn, detection langue auto (v0.5.0)
- [x] Telegram notification filtering : toggles per-prompt-type (permission/question/text_input) dans PWA, filtrage hub-side (v0.6.0)
- [x] Dispatcher conversation memory : SQLite, 10 msg window, 500 chars/msg, TTL 48h, API history (v0.6.0)
- [x] Terminal-only prompt detection : notification_data supprimé, terminal = source unique via pyte VT100 emulator (v0.7.0)
- [x] TTS Narrator : `intercom_announce` MCP tool, Hub proxy XTTS, PWA tts.js playback avec préférences par catégorie (v0.7.0)
