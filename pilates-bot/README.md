# Bot de Mensalidades — Pilates da Ellen

Assistente no Telegram para controlar alunos, mensalidades e pagamentos. A Ellen
conversa por **texto ou áudio**, e o bot cadastra, registra, cobra e manda relatórios.

## O que ele faz

| Pedido da Ellen (exemplos) | O que acontece |
|---|---|
| "Cadastra a Maria Souza, 2x por semana, 280, vence dia 10, tel 11 99999-8888" | Mostra o resumo e cadastra ao confirmar |
| "A Maria pagou no Pix" / "O João pagou 840 em dinheiro" | Lança na mensalidade em aberto mais antiga; se o valor cobrir vários meses, adianta os seguintes |
| "Como tá o mês?" | Previsto, recebido, quanto falta, pendentes, atrasados e entradas no caixa |
| "Quem vence essa semana?" / "Quem tá atrasado?" | Lista com valores e datas |
| "Como tá a Ana?" | Ficha do aluno com o que está em aberto e os últimos pagamentos |
| "Manda a cobrança da Ana" | Mensagem educada com Pix e **link que abre o WhatsApp já com o texto** |
| "Dezembro a Carla paga 200" / "Janeiro a Bia tá de férias" | Ajusta ou isenta só aquele mês |
| "A Júlia saiu" / "A Júlia voltou" | Inativa (para de cobrar) / reativa |
| "Apaga o último pagamento do João" | Desfaz um lançamento errado |
| "Cadastra a Paula, aluna da Amanda, 250, dia 15" | Aluno entra na carteira da Amanda |
| "Quanto pago pra Amanda esse mês?" | Extrato da comissão: cada pagamento, total a pagar e o que fica para o estúdio |
| "A Paula agora vai fazer aula com a Ellen" | Passa o aluno de uma carteira para outra |

## Carteiras e comissão

Cada aluno pertence a uma professora: a Ellen (dona) ou alguém da equipe, como a Amanda.
Quem não for indicado no cadastro fica com a Ellen. A equipe e o percentual de comissão
ficam no `.env` (`EQUIPE=Amanda:40`).

- A comissão é calculada **sobre o que os alunos dela pagaram no mês**, pela data do
  pagamento. Mensalidade atrasada que só é paga no mês seguinte entra na comissão do mês
  em que o dinheiro entrou.
- O valor da comissão fica **gravado em cada pagamento**. Se o percentual mudar, os meses
  anteriores continuam iguais.
- O resumo do mês mostra previsto e recebido separados por carteira, a comissão e quanto
  fica para o estúdio. No dia 1º chega também o extrato da comissão do mês que fechou.

**Nada é gravado sem confirmação.** Toda alteração aparece num resumo com os botões
✅ Confirmar / ❌ Cancelar. Também dá para responder "sim" ou "não".

**Avisos automáticos** (8h da manhã):
- **Segunda-feira:** quem vence na semana e quem está atrasado.
- **Outros dias:** só manda mensagem se alguém vence hoje ou venceu ontem sem pagar.
- **Dia 1º:** fechamento do mês anterior, comissão da equipe e previsão do novo mês.
- **Domingo, 20h:** backup com uma planilha Excel e o arquivo do sistema.

Atalhos: `/resumo` `/semana` `/atrasados` `/alunos` `/comissao` `/backup` `/novo` `/ajuda`

## Como funciona

```
Telegram (texto/áudio) → bot.py → transcrição local (faster-whisper)
                                  → agente.py (Claude escolhe a ação)
                                  → ferramentas.py → servico.py → SQLite (dados/pilates.db)
```

- `pilates/servico.py`: regras de negócio (alunos, mensalidades, pagamentos).
- `pilates/relatorios.py`: textos dos relatórios e avisos.
- `pilates/ferramentas.py`: ações que a IA pode usar. As de escrita só preparam a ação.
- `pilates/agente.py`: conversa com o Claude e fluxo de confirmação.
- `pilates/bot.py`: Telegram, avisos agendados e backup.

Como as mensalidades funcionam: cada aluno tem valor e dia de vencimento. O sistema
gera sozinho as mensalidades do mês atual e do próximo. Um pagamento quita a mais antiga
em aberto. Valores são guardados em centavos.

## Instalação no Pop!_OS

### 1. Criar o bot no Telegram
1. No Telegram, abra o **@BotFather** e mande `/newbot`.
2. Escolha um nome (ex.: "Pilates da Ellen") e um usuário terminado em `bot`.
3. Copie o **token** que ele mandar.

### 2. Criar a chave da IA
Em <https://console.anthropic.com> → *API Keys* → *Create Key*. Coloque alguns
dólares de crédito (o uso esperado é de poucos dólares por mês).

### 3. Instalar
```bash
git clone https://github.com/rcrepald/newprojects.git
cd newprojects/pilates-bot
bash deploy/instalar.sh      # 1ª vez: cria o .env e para
nano .env                    # preencha TELEGRAM_TOKEN, ANTHROPIC_API_KEY, CHAVE_PIX e EQUIPE
bash deploy/instalar.sh      # 2ª vez: instala e liga o serviço
```

### 4. Liberar a Ellen
1. A Ellen (e você, se quiser testar) manda `/start` para o bot. Ele responde com o **código do chat**.
2. Coloque os códigos no `.env`: `CHATS_AUTORIZADOS=123456789,987654321`
3. Reinicie: `sudo systemctl restart pilates-bot`

No primeiro start o bot baixa o modelo de transcrição (cerca de 500 MB). Pode demorar alguns minutos.

## Operação

```bash
journalctl -u pilates-bot -f          # ver o que está acontecendo
sudo systemctl restart pilates-bot    # reiniciar
git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart pilates-bot   # atualizar
```

**Importante para servidor caseiro:** desative a suspensão automática do Pop!_OS
(*Configurações → Energia → Suspensão automática: Desligada*). Se o PC dormir, o bot para.

**Backup e restauração:** os dados ficam em `dados/pilates.db`. As cópias de domingo ficam em
`dados/backups/` e também chegam no Telegram. Para restaurar, pare o serviço, copie o `.db`
do backup para `dados/pilates.db` e ligue de novo.

## Testes

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
