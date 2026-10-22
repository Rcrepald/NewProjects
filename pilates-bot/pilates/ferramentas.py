"""Ferramentas que a IA pode chamar.

Ferramentas de leitura rodam na hora. Ferramentas de escrita só *preparam* a
ação e devolvem um resumo; quem grava é o bot, depois que a Ellen confirma.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from . import relatorios as r
from . import servico as s
from .config import Config


@dataclass
class AcaoPreparada:
    resumo: str
    executar: Callable[[], str]


@dataclass
class Ferramenta:
    nome: str
    descricao: str
    parametros: dict
    obrigatorios: list[str]
    escrita: bool
    funcao: Callable  # (conn, hoje, cfg, args) -> str | AcaoPreparada

    def definicao(self) -> dict:
        return {
            "name": self.nome,
            "description": self.descricao,
            "input_schema": {
                "type": "object",
                "properties": self.parametros,
                "required": self.obrigatorios,
            },
        }


ALUNO = {"type": "string", "description": "Nome do aluno (pode ser parcial) ou id no formato #12."}
COMPETENCIA = {"type": "string", "description": "Mês de referência no formato AAAA-MM."}
DATA = {"type": "string", "description": "Data no formato AAAA-MM-DD."}


# ---------------------------------------------------------------- escrita

def _cadastrar(conn, hoje, cfg, a):
    novo = s.preparar_cadastro(conn, hoje, a.get("nome"), a.get("valor", 0), a.get("dia_vencimento", 0),
                               a.get("telefone"), a.get("plano"), a.get("primeira_competencia"),
                               a.get("observacoes"))
    partes = [f"🆕 Cadastrar *{novo.nome}*",
              f"   {s.brl(novo.valor)} por mês, vence todo dia {novo.dia_vencimento}"]
    if novo.plano:
        partes.append(f"   Plano: {novo.plano}")
    if novo.telefone:
        partes.append(f"   Telefone: {novo.telefone}")
    if novo.observacoes:
        partes.append(f"   Obs.: {novo.observacoes}")
    primeiro_venc = s.vencimento_para(novo.inicio_cobranca, novo.dia_vencimento)
    partes.append(f"   Primeira mensalidade: {s.nome_mes(novo.inicio_cobranca)} (vence {s.data_br(primeiro_venc)})")

    def executar():
        aid = s.cadastrar_aluno(conn, hoje, novo)
        return f"{novo.nome} cadastrado(a) com id #{aid}."
    return AcaoPreparada("\n".join(partes), executar)


def _pagamento(conn, hoje, cfg, a):
    plano = s.preparar_pagamento(conn, hoje, a.get("aluno", ""), a.get("valor"), a.get("data"),
                                 a.get("forma"), a.get("competencia"), a.get("observacao"))
    quando = "hoje" if plano.data == hoje else s.data_br(plano.data)
    forma = f", {plano.forma}" if plano.forma else ""
    partes = [f"💰 Pagamento de *{plano.aluno['nome']}*: {s.brl(plano.valor)}{forma}, {quando}"]
    for p in plano.parcelas:
        if p.aplicar + p.ja_pago >= p.valor_mensalidade:
            situacao = "quita" if p.ja_pago == 0 else f"completa (já tinha {s.brl(p.ja_pago)})"
        else:
            falta = p.valor_mensalidade - p.ja_pago - p.aplicar
            situacao = f"parcial, ainda faltarão {s.brl(falta)}"
        partes.append(f"   → {s.nome_mes(p.competencia)} (venc. {s.data_br(p.vencimento)}): "
                      f"{s.brl(p.aplicar)} — {situacao}")
    if plano.sobra:
        partes.append(f"   ⚠️ Sobram {s.brl(plano.sobra)} sem mensalidade para lançar.")
    if not plano.aluno["ativo"]:
        partes.append("   ⚠️ Esse aluno está inativo.")

    def executar():
        s.registrar_pagamento(conn, plano)
        return f"Pagamento de {plano.aluno['nome']} registrado."
    return AcaoPreparada("\n".join(partes), executar)


def _editar(conn, hoje, cfg, a):
    aluno, mud = s.preparar_edicao(conn, a.get("aluno", ""), {c: a.get(c) for c in s.CAMPOS_EDITAVEIS})
    rotulos = {"nome": "Nome", "telefone": "Telefone", "plano": "Plano", "valor": "Valor",
               "dia_vencimento": "Dia de vencimento", "observacoes": "Observações"}

    def fmt(campo, v):
        return s.brl(v) if campo == "valor" and v is not None else (v if v not in (None, "") else "—")
    partes = [f"✏️ Alterar *{aluno['nome']}*"]
    partes += [f"   {rotulos[c]}: {fmt(c, aluno[c])} → {fmt(c, v)}" for c, v in mud.items()]
    if "valor" in mud or "dia_vencimento" in mud:
        partes.append("   (vale para as mensalidades deste mês em diante que ainda não foram pagas)")

    def executar():
        s.editar_aluno(conn, hoje, aluno["id"], mud)
        return f"Dados de {aluno['nome']} atualizados."
    return AcaoPreparada("\n".join(partes), executar)


def _inativar(conn, hoje, cfg, a):
    aluno = s.encontrar_aluno(conn, a.get("aluno", ""))
    if not aluno["ativo"]:
        raise s.ErroNegocio(f"{aluno['nome']} já está inativo(a).")
    futuras = s.mensalidades_futuras_abertas(conn, aluno["id"], hoje)
    atrasadas = [m for m in s.mensalidades_com_saldo(conn, "m.aluno_id = ? AND m.vencimento < ?",
                                                     (aluno["id"], hoje.isoformat())) if m["saldo"]]
    partes = [f"🚪 Inativar *{aluno['nome']}* (para de gerar cobranças)"]
    if futuras:
        partes.append(f"   Cancela {len(futuras)} mensalidade(s) que ainda não venceram: "
                      + ", ".join(s.nome_mes(m["competencia"]) for m in futuras))
    if atrasadas:
        partes.append(f"   ⚠️ Continua devendo {s.brl(sum(m['saldo'] for m in atrasadas))} em atraso.")

    def executar():
        s.inativar_aluno(conn, hoje, aluno["id"])
        return f"{aluno['nome']} inativado(a)."
    return AcaoPreparada("\n".join(partes), executar)


def _reativar(conn, hoje, cfg, a):
    aluno = s.encontrar_aluno(conn, a.get("aluno", ""))
    if aluno["ativo"]:
        raise s.ErroNegocio(f"{aluno['nome']} já está ativo(a).")
    comp = s.validar_competencia(a["primeira_competencia"]) if a.get("primeira_competencia") \
        else s.primeira_competencia_padrao(aluno["dia_vencimento"], hoje)
    resumo = (f"🔄 Reativar *{aluno['nome']}* — {s.brl(aluno['valor'])}, dia {aluno['dia_vencimento']}\n"
              f"   Volta a cobrar a partir de {s.nome_mes(comp)}")

    def executar():
        s.reativar_aluno(conn, hoje, aluno["id"], comp)
        return f"{aluno['nome']} reativado(a)."
    return AcaoPreparada(resumo, executar)


def _ajustar(conn, hoje, cfg, a):
    aj = s.preparar_ajuste(conn, hoje, a.get("aluno", ""), a.get("competencia", ""), a.get("novo_valor"),
                           bool(a.get("isentar")), a.get("motivo"))
    antes = s.brl(aj.valor_antigo if aj.valor_antigo is not None else aj.aluno["valor"])
    depois = "isenta (não cobra)" if aj.valor_novo is None else s.brl(aj.valor_novo)
    motivo = f" — motivo: {aj.motivo}" if aj.motivo else ""
    resumo = f"🏷️ Mensalidade de {s.nome_mes(aj.competencia)} de *{aj.aluno['nome']}*: {antes} → {depois}{motivo}"

    def executar():
        s.ajustar_mensalidade(conn, aj)
        return "Mensalidade ajustada."
    return AcaoPreparada(resumo, executar)


def _desfazer(conn, hoje, cfg, a):
    aluno, linhas = s.ultimo_lote(conn, a.get("aluno", ""))
    total = sum(p["valor"] for p in linhas)
    refs = ", ".join(s.nome_mes(p["competencia"]) for p in linhas if p["competencia"])
    resumo = (f"↩️ Apagar o último pagamento de *{aluno['nome']}*: {s.brl(total)} "
              f"de {s.data_br(linhas[0]['data'])} (ref. {refs})")

    def executar():
        s.desfazer_lote(conn, linhas[0]["lote"])
        return "Pagamento apagado."
    return AcaoPreparada(resumo, executar)


# ---------------------------------------------------------------- leitura

FERRAMENTAS: list[Ferramenta] = [
    Ferramenta(
        "resumo_mes",
        "Resumo financeiro de um mês: previsto, recebido, quanto falta, quem está pendente, "
        "atrasados de meses anteriores e quanto entrou no caixa. Sem competência = mês atual.",
        {"competencia": COMPETENCIA}, [], False,
        lambda c, h, cfg, a: r.resumo_mes(c, h, a.get("competencia")),
    ),
    Ferramenta(
        "vencimentos",
        "Lista quem vence (tem mensalidade a pagar) num período. Sem datas = de hoje até domingo "
        "(a semana). Use para 'quem tem que pagar essa semana/amanhã/até dia X'.",
        {"inicio": DATA, "fim": DATA}, [], False,
        lambda c, h, cfg, a: r.vencimentos(c, h, a.get("inicio"), a.get("fim")),
    ),
    Ferramenta(
        "atrasados",
        "Lista todas as mensalidades vencidas e não pagas (inadimplentes), de qualquer mês.",
        {}, [], False,
        lambda c, h, cfg, a: r.atrasados(c, h),
    ),
    Ferramenta(
        "listar_alunos",
        "Lista alunos com valor, dia de vencimento, plano e situação do mês atual.",
        {"quais": {"type": "string", "enum": ["ativos", "inativos", "todos"]}}, [], False,
        lambda c, h, cfg, a: r.lista_alunos(c, h, a.get("quais") or "ativos"),
    ),
    Ferramenta(
        "ficha_aluno",
        "Situação completa de um aluno: dados, mensalidades em aberto e últimos pagamentos.",
        {"aluno": ALUNO}, ["aluno"], False,
        lambda c, h, cfg, a: r.ficha_aluno(c, h, a.get("aluno", "")),
    ),
    Ferramenta(
        "pagamentos_periodo",
        "Lista os pagamentos recebidos entre duas datas (extrato de recebimentos).",
        {"inicio": DATA, "fim": DATA}, ["inicio", "fim"], False,
        lambda c, h, cfg, a: r.pagamentos_periodo(c, a.get("inicio"), a.get("fim")),
    ),
    Ferramenta(
        "mensagem_cobranca",
        "Gera uma mensagem educada de cobrança/lembrete para o aluno, com chave Pix e link "
        "que abre o WhatsApp já com o texto. Repasse o resultado à Ellen sem reescrever o link.",
        {"aluno": ALUNO}, ["aluno"], False,
        lambda c, h, cfg, a: r.mensagem_cobranca(c, h, a.get("aluno", ""), cfg.nome_professora, cfg.chave_pix),
    ),
    # ------------------------------------------------------------ escrita
    Ferramenta(
        "cadastrar_aluno",
        "Cadastra um aluno novo. Precisa de nome, valor mensal e dia de vencimento. "
        "primeira_competencia é o primeiro mês a ser cobrado; se omitido, será o mês atual se o "
        "dia de vencimento ainda não passou, senão o próximo.",
        {
            "nome": {"type": "string", "description": "Nome completo."},
            "valor": {"type": "number", "description": "Mensalidade em reais, ex.: 280 ou 279.90."},
            "dia_vencimento": {"type": "integer", "description": "Dia do mês (1 a 31)."},
            "telefone": {"type": "string", "description": "Telefone com DDD."},
            "plano": {"type": "string", "description": "Ex.: 2x por semana."},
            "primeira_competencia": COMPETENCIA,
            "observacoes": {"type": "string"},
        },
        ["nome", "valor", "dia_vencimento"], True, _cadastrar,
    ),
    Ferramenta(
        "registrar_pagamento",
        "Registra um pagamento recebido. Sem valor = paga o saldo da mensalidade em aberto mais "
        "antiga. Com valor maior que uma mensalidade, adianta os meses seguintes. Use competencia "
        "só se a Ellen disser a qual mês se refere. Sem data = hoje.",
        {
            "aluno": ALUNO,
            "valor": {"type": "number", "description": "Valor recebido em reais."},
            "data": DATA,
            "forma": {"type": "string", "enum": list(s.FORMAS)},
            "competencia": COMPETENCIA,
            "observacao": {"type": "string"},
        },
        ["aluno"], True, _pagamento,
    ),
    Ferramenta(
        "editar_aluno",
        "Altera dados de um aluno (nome, telefone, plano, valor, dia de vencimento, observações). "
        "Envie só os campos que mudam.",
        {
            "aluno": ALUNO,
            "nome": {"type": "string"},
            "telefone": {"type": "string"},
            "plano": {"type": "string"},
            "valor": {"type": "number"},
            "dia_vencimento": {"type": "integer"},
            "observacoes": {"type": "string"},
        },
        ["aluno"], True, _editar,
    ),
    Ferramenta(
        "inativar_aluno",
        "Marca aluno como inativo (saiu/trancou): para de gerar mensalidades. Dívidas antigas continuam.",
        {"aluno": ALUNO}, ["aluno"], True, _inativar,
    ),
    Ferramenta(
        "reativar_aluno",
        "Reativa um aluno inativo e volta a cobrar a partir da competência informada (ou a próxima).",
        {"aluno": ALUNO, "primeira_competencia": COMPETENCIA}, ["aluno"], True, _reativar,
    ),
    Ferramenta(
        "ajustar_mensalidade",
        "Muda o valor de UMA mensalidade específica (desconto, acréscimo) ou isenta/cancela ela "
        "(férias, cortesia). Para mudar o valor de todos os meses, use editar_aluno.",
        {
            "aluno": ALUNO,
            "competencia": COMPETENCIA,
            "novo_valor": {"type": "number", "description": "Novo valor em reais."},
            "isentar": {"type": "boolean", "description": "true para não cobrar esse mês."},
            "motivo": {"type": "string"},
        },
        ["aluno", "competencia"], True, _ajustar,
    ),
    Ferramenta(
        "desfazer_ultimo_pagamento",
        "Apaga o último pagamento registrado de um aluno (para corrigir lançamento errado).",
        {"aluno": ALUNO}, ["aluno"], True, _desfazer,
    ),
]

POR_NOME = {f.nome: f for f in FERRAMENTAS}
DEFINICOES = [f.definicao() for f in FERRAMENTAS]


def chamar(nome: str, conn, hoje: date, cfg: Config, argumentos: dict) -> str | AcaoPreparada:
    """Roda a ferramenta. Erros de negócio sobem como ErroNegocio."""
    ferramenta = POR_NOME.get(nome)
    if ferramenta is None:
        raise s.ErroNegocio(f"Ferramenta desconhecida: {nome}")
    try:
        return ferramenta.funcao(conn, hoje, cfg, argumentos or {})
    except (TypeError, ValueError, KeyError) as e:
        raise s.ErroNegocio(f"Parâmetros inválidos: {e}") from e
