# Changelog

## [0.1.2] - 2026-10-16

### Fixed
- Windows: `/agentmeter:setup` falhava com `settings.json` em UTF-8 com BOM (Bloco de Notas, PowerShell 5).
- Windows: o `settings.json` era regravado com CRLF; agora sempre LF.
- As conexões SQLite de `report` e `ingest` agora são fechadas (no Windows o arquivo ficava preso).

### Added
- `.gitattributes` e `.editorconfig` (LF, UTF-8), para `run.sh` não virar CRLF no checkout com `autocrlf=true`.
- Requisito documentado: no Windows é preciso o Git for Windows (Git Bash), que fornece o `sh`.
