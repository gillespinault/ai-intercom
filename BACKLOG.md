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

### 2. Conversation vocale via Telegram (STT + TTS via limn)
Permettre une conversation vocale avec le dispatcher Telegram en utilisant les services STT et TTS heberges sur limn (Jetson Thor).
- Telegram voice messages → STT (Whisper sur limn) → texte → dispatcher → reponse texte → TTS (CosyVoice sur limn) → voice message Telegram
- Necessite : endpoint STT sur limn, endpoint TTS streaming sur limn (CosyVoice3)
- Le dispatcher recoit du texte transcrit au lieu du texte tape, le reste du pipeline est identique
- Bonus: detection de langue automatique

### 3. Continuite conversationnelle du dispatcher
Actuellement chaque message Telegram est independant (pas de memoire de conversation).
- Maintenir un historique par utilisateur/session
- Passer le contexte des echanges precedents au dispatcher
- Permettre des conversations multi-tours ("fais X" → "maintenant fais Y sur le meme serveur")

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

### 7. ~~Dashboard web de l'annuaire~~ → PARTIELLEMENT FAIT (v0.4.0)
~~Interface web pour visualiser les machines, projets, statuts, missions en cours.~~
PWA Attention Hub implementee (`/attention`) : vue temps-reel des sessions, terminal viewer, reponse aux prompts.
Reste a faire :
- Historique des missions
- Logs de communication
- Vue annuaire machines/projets complete
- Terminal viewer sans tmux : actuellement capture/injection requiert tmux (`capture-pane`, `send-keys`), les sessions non-tmux n'ont pas de terminal view ni de reponse aux prompts
- Hooks multi-machine : `cc-heartbeat.sh` deploye uniquement sur serverlab, a installer sur limn/vps via `install.sh`

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
