---
description: Relatório de tokens do projeto por agente, subagent, tipo e sessão
argument-hint: "[--sessao] [--desde 7d] [--csv] [--todos]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agentmeter.py" report $ARGUMENTS`

Mostre a saída acima exatamente como está, dentro de um bloco de código, sem comentários antes ou depois. Se a saída for CSV, pergunte em uma linha se o usuário quer salvar em arquivo.
