"""Conversa com o Claude: entende a Ellen e decide qual ferramenta usar.

Fluxo de confirmação: quando a IA chama ferramentas de escrita, o bot NÃO
grava nada. Ele guarda as ações preparadas, mostra um resumo com botões e só
executa quando a Ellen confirma. O resultado (confirmado ou cancelado) vai
para a IA como `tool_result` junto com a próxima mensagem da Ellen, mantendo o
histórico sempre só-acrescentado.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import anthropic

from . import ferramentas as f
from . import servico as s
from .config import Config

log = logging.getLogger(__name__)

DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")

SISTEMA = """\
Você é a assistente financeira do estúdio de Pilates da {professora}. Você conversa só com a \
{professora}, pelo Telegram, e cuida do cadastro de alunos, das mensalidades e dos pagamentos.

Jeito de falar:
- Português do Brasil, tom carinhoso, simples e direto, como uma secretária de confiança.
- Respostas curtas. Nada de termos técnicos (não fale em "competência", "banco de dados", \
"ferramenta", "id"). Diga "mensalidade de outubro", "vence dia 10".
- Para formatar no Telegram use *negrito* com UM asterisco só. Não use títulos com #, \
tabelas ou **dois asteriscos**.
- As mensagens podem vir de áudio transcrito: releve erros de transcrição e use o bom senso \
com nomes parecidos.

Como trabalhar:
- Use as ferramentas para tudo que envolve dados. Nunca invente números, nomes ou datas.
- Cada mensagem da {professora} começa com a data e hora de hoje entre colchetes. Use para \
resolver "ontem", "sexta passada", "mês que vem" etc. Datas no formato AAAA-MM-DD e meses \
como AAAA-MM nas ferramentas.
- Quando um relatório vem de uma ferramenta, mande o texto dele praticamente como está; \
pode acrescentar um comentário curto se ajudar.
- Ferramentas de escrita (cadastrar, registrar pagamento, editar, inativar, reativar, \
ajustar, desfazer) não gravam na hora: a {professora} recebe um resumo com botões para \
confirmar. Por isso, não peça confirmação você mesma; chame a ferramenta e, no máximo, diga \
uma frase curta como "Confere pra mim 👇". Se ela pedir várias coisas de uma vez, chame \
todas as ferramentas juntas.
- Se faltar algo essencial (por exemplo valor ou dia de vencimento num cadastro), pergunte \
antes de chamar a ferramenta. Não pergunte o que tem padrão razoável (data do pagamento = \
hoje; forma de pagamento pode ficar em branco).
- Se a ferramenta disser que há mais de um aluno com aquele nome, pergunte qual.
- Quando o resultado de uma ação chegar dizendo que foi cancelada, não refaça sozinha.
- Assuntos fora do estúdio: responda com gentileza e brevidade, sem usar ferramentas.
"""

CONFIRMA = re.compile(r"^\s*(sim|s|pode|pode sim|confirma|confirmo|confirmado|ok|okay|isso|isso mesmo|"
                      r"certo|correto|pode confirmar|pode salvar|salva|manda|beleza|blz|👍|✅)[\s.!]*$",
                      re.IGNORECASE)
CANCELA = re.compile(r"^\s*(n[aã]o|nao|cancela|cancelar|cancelado|errado|esquece|deixa|❌)[\s.!]*$",
                     re.IGNORECASE)

MAX_MENSAGENS = 60
OCIOSO_SEGUNDOS = 2 * 60 * 60


@dataclass
class Pendente:
    numero: int
    acoes: dict[str, f.AcaoPreparada]  # tool_use_id -> ação

    def resumo(self) -> str:
        return "\n\n".join(a.resumo for a in self.acoes.values())


@dataclass
class Conversa:
    mensagens: list = field(default_factory=list)
    resultados_abertos: list[dict] = field(default_factory=list)  # tool_results ainda não enviados
    pendente: Pendente | None = None
    ultima_atividade: float = field(default_factory=time.time)


@dataclass
class Resposta:
    texto: str
    pendente: Pendente | None = None


class Agente:
    def __init__(self, cfg: Config, conn):
        self.cfg = cfg
        self.conn = conn
        self.cliente = anthropic.AsyncAnthropic()
        self.sistema = SISTEMA.format(professora=cfg.nome_professora)
        self.conversas: dict[int, Conversa] = {}
        self._contador = 0

    # ------------------------------------------------------------ estado

    def hoje(self):
        return datetime.now(self.cfg.fuso).date()

    def _conversa(self, chat_id: int) -> Conversa:
        c = self.conversas.get(chat_id)
        agora = time.time()
        if c is None or (agora - c.ultima_atividade > OCIOSO_SEGUNDOS) or (
            c.pendente is None and len(c.mensagens) > MAX_MENSAGENS
        ):
            c = Conversa()
            self.conversas[chat_id] = c
        c.ultima_atividade = agora
        return c

    def esquecer(self, chat_id: int) -> None:
        self.conversas.pop(chat_id, None)

    # ------------------------------------------------------------ confirmação

    def resolver_pendente(self, chat_id: int, numero: int | None, confirmar: bool) -> str | None:
        """Executa ou cancela as ações pendentes. Devolve o texto para a Ellen."""
        c = self.conversas.get(chat_id)
        if c is None or c.pendente is None or (numero is not None and c.pendente.numero != numero):
            return None
        pend, c.pendente = c.pendente, None
        linhas = []
        for tool_id, acao in pend.acoes.items():
            if confirmar:
                try:
                    resultado = acao.executar()
                    linhas.append(f"✅ {resultado}")
                    conteudo = f"Confirmado pela {self.cfg.nome_professora} e gravado: {resultado}"
                except s.ErroNegocio as e:
                    linhas.append(f"⚠️ {e}")
                    conteudo = f"Falhou ao gravar: {e}"
                except Exception:
                    log.exception("erro ao executar ação")
                    linhas.append("⚠️ Deu um erro ao gravar essa ação. Nada foi alterado nela.")
                    conteudo = "Erro interno ao gravar; nada foi alterado."
            else:
                conteudo = f"Cancelado pela {self.cfg.nome_professora}; nada foi gravado."
            self._preencher_resultado(c, tool_id, conteudo)
        if not confirmar:
            return "❌ Cancelado, não gravei nada."
        return "\n".join(linhas)

    @staticmethod
    def _preencher_resultado(c: Conversa, tool_id: str, conteudo: str) -> None:
        for r in c.resultados_abertos:
            if r["tool_use_id"] == tool_id:
                r["content"] = conteudo

    # ------------------------------------------------------------ conversa

    async def responder(self, chat_id: int, texto: str) -> Resposta:
        c = self._conversa(chat_id)

        if c.pendente is not None:
            if CONFIRMA.match(texto):
                return Resposta(self.resolver_pendente(chat_id, None, True) or "")
            if CANCELA.match(texto):
                return Resposta(self.resolver_pendente(chat_id, None, False) or "")
            pend, c.pendente = c.pendente, None
            for tool_id in pend.acoes:
                self._preencher_resultado(
                    c, tool_id,
                    f"A {self.cfg.nome_professora} não confirmou (mandou outra mensagem); nada foi gravado.")

        agora = datetime.now(self.cfg.fuso)
        cabecalho = f"[Hoje: {DIAS[agora.weekday()]}, {agora:%Y-%m-%d %H:%M}]"
        conteudo = c.resultados_abertos + [{"type": "text", "text": f"{cabecalho}\n{texto}"}]
        c.resultados_abertos = []
        c.mensagens.append({"role": "user", "content": conteudo})

        try:
            return await self._ciclo(c)
        except anthropic.APIError:
            log.exception("erro na API do Claude")
            self.esquecer(chat_id)
            return Resposta("Tive um probleminha para pensar agora 😕. Pode repetir daqui a pouco?")

    async def _ciclo(self, c: Conversa) -> Resposta:
        for _ in range(8):
            resp = await self.cliente.beta.messages.create(
                model=self.cfg.modelo,
                max_tokens=16000,
                system=self.sistema,
                tools=f.DEFINICOES,
                messages=c.mensagens,
                output_config={"effort": self.cfg.esforco},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            c.mensagens.append({"role": "assistant", "content": resp.content})
            texto = "\n".join(b.text for b in resp.content if b.type == "text").strip()

            if resp.stop_reason == "refusal":
                c.mensagens.clear()
                return Resposta("Desculpa, não consegui responder isso. Pode falar de outro jeito?")
            if resp.stop_reason != "tool_use":
                if any(b.type == "tool_use" for b in resp.content):
                    c.mensagens.clear()  # resposta cortada no meio de uma chamada
                return Resposta(texto or "👍")

            resultados, acoes, houve_erro = [], {}, False
            for bloco in resp.content:
                if bloco.type != "tool_use":
                    continue
                try:
                    saida = f.chamar(bloco.name, self.conn, self.hoje(), self.cfg, bloco.input)
                except s.ErroNegocio as e:
                    houve_erro = True
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id,
                                       "content": str(e), "is_error": True})
                    continue
                except Exception as e:
                    log.exception("erro na ferramenta %s", bloco.name)
                    houve_erro = True
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id,
                                       "content": f"Erro interno: {e}", "is_error": True})
                    continue
                if isinstance(saida, f.AcaoPreparada):
                    acoes[bloco.id] = saida
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id,
                                       "content": "Aguardando confirmação."})
                else:
                    resultados.append({"type": "tool_result", "tool_use_id": bloco.id, "content": saida})

            if acoes and houve_erro:
                # Não pede confirmação de um pacote incompleto: devolve tudo à IA.
                for r in resultados:
                    if r["tool_use_id"] in acoes:
                        r["content"] = ("Não executado porque outra ação desta mesma resposta deu erro. "
                                        "Corrija e chame de novo todas as ações juntas.")
                acoes = {}
            if acoes:
                self._contador += 1
                c.pendente = Pendente(self._contador, acoes)
                c.resultados_abertos = resultados
                return Resposta(texto, c.pendente)
            c.mensagens.append({"role": "user", "content": resultados})

        c.mensagens.clear()
        return Resposta("Me enrolei aqui 😅. Pode repetir de um jeito mais simples?")
