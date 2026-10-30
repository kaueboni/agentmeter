# agentmeter

Plug-in do Claude Code que mostra, no rodapé do terminal, o contexto do agente principal e de cada subagent **ativo**, com barrinhas, e conta os tokens do projeto inteiro separados por subagent.

```
▸ principal  ████░░░░░░  41%  Opus 5.5
─ ativos 3 ─────────────────────────────
▸ Explore#1  ███████░░░  73%   146k  0:48
▸ Explore#2  ███░░░░░░░  28%    56k  0:31
▸ Plan       █░░░░░░░░░   9%    90k  0:09
sessão 126k │ projeto 2.45M │ sub 38% │ US$ 19,60
```

- **Barra de status**: contexto do agente principal, subagents rodando naquele momento (cada um com barra, tokens e tempo), total da sessão, total do projeto, fatia dos subagents e custo estimado.
- **Painel de subagents** (abaixo do prompt): mostra só os ativos, com a mesma barra. Os que terminam somem.
- **`/agentmeter:tokens`**: relatório do projeto do começo ao fim, por agente, tipo de token e sessão.

As cores das barras: verde até 60%, amarelo até 85%, vermelho acima disso.

## Instalação

No Claude Code:

```
/plugin marketplace add kaueboni/agentmeter
/plugin install agentmeter@agentmeter
```

Abra uma sessão nova e rode uma vez:

```
/agentmeter:setup
```

O setup coloca a status line no seu `~/.claude/settings.json` (com backup antes). Se você já tiver outra status line, ele mostra qual é e pergunta antes de trocar. O painel de subagents já vem ativado pelo próprio plug-in.

Requisitos: Claude Code 2.1.293 ou mais novo e `python3`, `python` ou `py` no PATH (o lançador `scripts/run.sh` escolhe o primeiro que funcionar; no Windows use o Git Bash que o Claude Code já exige). Não precisa instalar nenhum pacote.

## Relatório

```
/agentmeter:tokens                 projeto atual, do começo ao fim
/agentmeter:tokens --sessao        só a sessão atual
/agentmeter:tokens --desde 7d      últimos 7 dias (também 24h, 2w ou 2026-10-01)
/agentmeter:tokens --csv           uma linha por requisição, para planilha ou banco
/agentmeter:tokens --json          relatório em JSON, para outras ferramentas
/agentmeter:tokens --todos         todos os projetos
```

Fora do Claude Code, o mesmo relatório sai com:

```
sh ~/.claude/agentmeter/run.sh report
```

### Saída em JSON

Para outras ferramentas (um mod, um painel, um script) lerem os números sem depender do texto:

```
sh ~/.claude/agentmeter/run.sh report --json              # indentado
sh ~/.claude/agentmeter/run.sh report --json --compacto   # uma linha
```

Aceita os mesmos filtros (`--sessao`, `--desde`, `--todos`, `--projeto`). Chaves principais:

| Chave | Conteúdo |
|-------|----------|
| `total` | tokens, `custo_usd`, `por_tipo` e período, com os filtros aplicados |
| `projeto_total`, `sessao_atual_total` | os mesmos campos, sempre sem filtro de período |
| `por_agente` | tokens, custo e número de execuções por tipo de agente |
| `execucoes` | uma linha por subagent: `agent_id`, `agent_type`, `descricao`, modelo, tokens, custo, início e fim |
| `contexto` | contexto do agente principal na sessão atual (tokens, janela, `pct`) |
| `ativos` | subagents rodando agora, com contexto e tempo |

`descricao` é a descrição que o Claude deu ao subagent. Se ela citar a tarefa (ex.: "Developer T16 front"),
dá para somar custo por tarefa sem guardar nada à parte.

## Como funciona

O Claude Code grava cada sessão em `~/.claude/projects/<projeto>/<sessão>.jsonl` e cada subagent em `<sessão>/subagents/agent-<id>.jsonl`. Cada resposta do modelo traz o uso de tokens (input, cache write, cache read, output).

O agentmeter lê só o trecho novo desses arquivos e grava cada requisição uma vez em um SQLite (`~/.claude/agentmeter/ledger.db`). Isso acontece quando a barra atualiza e nos hooks `Stop`, `SubagentStop`, `PreCompact` e `SessionEnd`. Como o ledger é dele, o histórico continua lá mesmo depois que o Claude Code apaga transcripts antigos. Na primeira vez que você roda o relatório num projeto, ele importa as sessões que já existiam.

Os subagents ativos vêm do painel de agentes do Claude Code (`subagentStatusLine`), com contagem de tokens e janela de contexto de cada um. Os hooks `SubagentStart` e `SubagentStop` servem de reserva e tiram da barra quem já terminou.

O custo é estimado pela tabela de preços por modelo dentro do script. Para ajustar preços, crie `~/.claude/agentmeter/pricing.json`:

```json
{ "opus-5-5": [4, 5, 8, 0.2, 20] }
```

Os valores são USD por milhão de tokens: input, cache write 5m, cache write 1h, cache read, output.

## Opções

- Sem os subagents na barra principal (só no painel): troque o comando da status line por `AGENTMETER_INLINE=0 sh "$HOME/.claude/agentmeter/run.sh" statusline`.
- Atualização da barra: o setup usa `refreshInterval: 2`, para as barras andarem enquanto o principal espera os subagents. Mude com `/agentmeter:setup --refresh 5`.
- Remover a status line: `sh ~/.claude/agentmeter/run.sh setup --remover`.

## Desenvolvimento

```
claude --plugin-dir .                  # carrega o plug-in local
python3 -m unittest discover -s tests  # testes
claude plugin validate .
```

## Licença

MIT
