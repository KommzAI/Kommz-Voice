# Kommz Voice

[Français](README.md) | [English](README.en.md)

Kommz Voice is the voice-engine layer of the Kommz ecosystem.
It powers synthesis, transcription, and multi-engine routing for connected clients.

## What this project does

- XTTS for multilingual synthesis
- GPT-SoVITS for voice timbre and style
- Whisper for transcription
- Server API for client integration
- Routing and fallback based on engine availability

## Ecosystem positioning

- Open-source client / community app: `Kommz Gamer Community`
- Engine layer: `Kommz Voice`
- Support and community: Discord + Patreon

## References

- Architecture: `docs/architecture-overview.md`
- Message guidelines: `docs/message-guidelines.md`
- Security and rotation: `docs/security-rotation-runbook.md`
- Release checklist: `docs/release-security-checklist.md`
- Roadmap: `ROADMAP.en.md`

## Quick start

1. Copy `env.template` to `.env`.
2. Fill only the required secrets.
3. Copy `settings.example.json` to `settings.json` for local configuration.
4. Never commit `.env` or `settings.json`.
5. Install dependencies:
   - `pip install -r requirements.txt`
6. Start the server:
   - `python vtp_web_server.py`

## Security

- Recommended hooks:
  - `pip install pre-commit detect-secrets`
  - `pre-commit install`
- Refresh the secrets baseline:
  - `detect-secrets scan > .secrets.baseline`
- For Git history purges after secret rotation:
  - `scripts/purge-git-history.ps1`

`vtp_web_server.py` applies fail-fast checks in production when critical secrets are missing or invalid.

## Public links

- Community GitHub: https://github.com/Kommz-Gamer/Kommz-Gamer
- Discord: https://discord.gg/uv25d6uGKZ
- Patreon: https://www.patreon.com/KommzInnovations

## Contributing / Support

- Contributing: `CONTRIBUTING.md`
- Support: `SUPPORT.md`

## Releases

Use these templates to standardize release notes:

- French: `release-template.fr.md`
- English: `release-template.en.md`
