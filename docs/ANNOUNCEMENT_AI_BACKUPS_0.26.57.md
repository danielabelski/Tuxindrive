# TuxInDrive 0.26.57 AI Backup media kit

TuxInDrive 0.26.57 adds practical, storage-efficient automatic cloud backups
for locally installed AI tools. The feature supports Codex, Claude Code,
Gemini CLI, Cursor, and Continue.

## Verified facts

- TuxInDrive detects supported local AI-tool data folders and creates an
  upload-only backup job in an existing connected cloud account.
- Later runs are incremental: unchanged files are not uploaded again.
- A running backup displays its completion percentage.
- Replaced and deleted remote versions are retained for seven days, then
  pruned after a successful backup to prevent unbounded storage growth.
- Known authentication files, private keys, environment files, caches, logs,
  sockets, locks, and temporary runtime data are excluded.
- Interrupted backups are recorded clearly and can resume incrementally with
  **Sync now**, skipping content already present in the cloud.
- The connectors use local files only. They do not sign in to AI services and
  cannot export browser-only conversation history.

## Release-page summary

TuxInDrive 0.26.57 can automatically back up local data from Codex, Claude
Code, Gemini CLI, Cursor, and Continue to an existing cloud account. Backups
are upload-only and incremental, display live completion percentage, exclude
known credentials and transient runtime data, and keep only seven days of
replaced or deleted remote versions. If an upload is interrupted, **Sync now**
continues safely without uploading unchanged cloud content again.

Download: https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

## LinkedIn / project update

AI work increasingly lives outside individual documents: local conversations,
prompts, memories, skills, commands, and project context can become essential
working material.

TuxInDrive 0.26.57 now provides automatic cloud backups for locally installed
Codex, Claude Code, Gemini CLI, Cursor, and Continue. It detects supported data
folders and creates upload-only jobs in a cloud account you already control.

The backups are incremental, show live completion percentage, skip unchanged
files, and retain replaced or deleted versions for seven days before cleanup.
Known authentication files, private keys, environment files, caches, logs,
locks, sockets, and temporary runtime data are excluded. Interrupted uploads
can continue safely with **Sync now**.

This is a local-file backup connector, not an AI-account integration: it does
not sign in to AI services and cannot export browser-only chat history.

I maintain TuxInDrive as an open-source, multi-cloud synchronization client.
The new AI Backup function is available in 0.26.57:
https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

#OpenSource #Backup #AI #Codex #ClaudeCode #GeminiCLI

## Short social post

TuxInDrive 0.26.57 adds automatic incremental cloud backups for local Codex,
Claude Code, Gemini CLI, Cursor, and Continue data. Live progress, seven-day
version retention, credential exclusions, and safe resume after interruption.
Open source: https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

## Mastodon / X-sized post

TuxInDrive 0.26.57 backs up local Codex, Claude Code, Gemini CLI, Cursor &
Continue data incrementally to your cloud. Live %, secret exclusions, 7-day
version retention, safe resume. No AI-service login or browser-chat scraping.
https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

## Forum / Reddit post

### Automatic incremental backups for local AI-tool data

I maintain TuxInDrive, an open-source multi-cloud synchronization client. Its
new AI Backup function detects local Codex, Claude Code, Gemini CLI, Cursor,
and Continue installations and creates scheduled upload-only jobs in an
existing connected cloud account.

After the initial upload, unchanged files are skipped. The application shows
the current completion percentage, keeps replaced or deleted versions for
seven days, and prunes older history after a successful run. If the app closes
during a backup, the job is marked as interrupted and **Sync now** safely
continues the incremental upload.

The connectors exclude known authentication files, private keys, environment
files, caches, logs, locks, sockets, and temporary runtime data. They only
back up locally available content; they do not sign in to an AI provider or
extract browser-only history.

Release and downloads:
https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

Feedback on additional local AI tools and secret-file exclusions is welcome.

## Czech post

TuxInDrive 0.26.57 nově automaticky zálohuje lokální data z Codexu, Claude
Code, Gemini CLI, Cursoru a Continue do vašeho vlastního cloudu. Zálohy jsou
inkrementální, zobrazují průběh v procentech, nepřenášejí znovu nezměněné
soubory a po sedmi dnech bezpečně čistí starší nahrazené verze.

Známé přihlašovací soubory, privátní klíče, proměnné prostředí, cache, logy,
zámky a dočasná data jsou vyloučené. Přerušenou zálohu lze bezpečně obnovit
tlačítkem **Sync now**. Konektor se nepřihlašuje do AI služby a nestahuje
historii dostupnou pouze v prohlížeči.

Stažení: https://github.com/tpluharik/Tuxindrive/releases/tag/v0.26.57

