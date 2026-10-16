---
description: Ativa a status line do agentmeter no ~/.claude/settings.json
argument-hint: "[--force] [--refresh 2]"
allowed-tools: Bash(python3:*)
---

Rode este comando e mostre a saída ao usuário:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentmeter.py" setup $ARGUMENTS
```

Se ele sair com código 2, o usuário já tem outra status line. Mostre qual é, pergunte se pode substituir e, só com um sim, rode de novo com `--force`. Um backup do settings.json é salvo antes de qualquer mudança.

Depois de ativar, diga que a barra aparece no rodapé em poucos segundos e que o painel de subagents abaixo do prompt passa a mostrar só os ativos, com barra de contexto.
