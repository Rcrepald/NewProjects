"""Regras de negócio: alunos, mensalidades e pagamentos.

Valores em dinheiro são guardados em centavos (int). Datas em texto ISO
(AAAA-MM-DD) e competências (o mês a que a mensalidade se refere) em AAAA-MM.
Todas as funções recebem `hoje` para que o comportamento seja testável.
"""

import calendar
import difflib
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS alunos (
    id              INTEGER PRIMARY KEY,
    nome            TEXT NOT NULL,
    telefone        TEXT,
    plano           TEXT,
    valor           INTEGER NOT NULL,
    dia_vencimento  INTEGER NOT NULL CHECK (dia_vencimento BETWEEN 1 AND 31),
    inicio_cobranca TEXT NOT NULL,
    professora      TEXT NOT NULL DEFAULT '',
    ativo           INTEGER NOT NULL DEFAULT 1,
    observacoes     TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    inativado_em    TEXT
);
CREATE TABLE IF NOT EXISTS mensalidades (
    id          INTEGER PRIMARY KEY,
    aluno_id    INTEGER NOT NULL REFERENCES alunos(id),
    competencia TEXT NOT NULL,
    valor       INTEGER NOT NULL,
    vencimento  TEXT NOT NULL,
    cancelada   INTEGER NOT NULL DEFAULT 0,
    observacao  TEXT,
    UNIQUE (aluno_id, competencia)
);
CREATE TABLE IF NOT EXISTS pagamentos (
    id             INTEGER PRIMARY KEY,
    aluno_id       INTEGER NOT NULL REFERENCES alunos(id),
    mensalidade_id INTEGER REFERENCES mensalidades(id),
    valor          INTEGER NOT NULL,
    data           TEXT NOT NULL,
    forma          TEXT,
    observacao     TEXT,
    professora     TEXT NOT NULL DEFAULT '',
    comissao       INTEGER NOT NULL DEFAULT 0,
    lote           TEXT NOT NULL,
    criado_em      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS professoras (
    nome     TEXT PRIMARY KEY,
    comissao REAL NOT NULL DEFAULT 0,
    dona     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_mens_venc ON mensalidades (vencimento);
CREATE INDEX IF NOT EXISTS ix_pag_data ON pagamentos (data);
"""

FORMAS = ("pix", "dinheiro", "cartao", "transferencia", "outro")

MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


class ErroNegocio(Exception):
    """Erro de validação com mensagem pensada para ser lida pela IA/usuária."""


# ---------------------------------------------------------------- utilidades

def conectar(caminho: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ESQUEMA)
    _migrar(conn)
    return conn


def _migrar(conn: sqlite3.Connection) -> None:
    """Acrescenta colunas novas em bancos criados por versões anteriores."""
    novas = {
        "alunos": [("professora", "TEXT NOT NULL DEFAULT ''")],
        "pagamentos": [("professora", "TEXT NOT NULL DEFAULT ''"), ("comissao", "INTEGER NOT NULL DEFAULT 0")],
    }
    for tabela, colunas in novas.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo in colunas:
            if nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
    conn.commit()


def brl(centavos: int) -> str:
    sinal = "-" if centavos < 0 else ""
    reais, cent = divmod(abs(centavos), 100)
    return f"{sinal}R$ {reais:,}".replace(",", ".") + f",{cent:02d}"


def centavos(valor_reais: float | int | str) -> int:
    if isinstance(valor_reais, str):
        valor_reais = float(valor_reais.replace("R$", "").replace(".", "").replace(",", ".").strip())
    c = round(float(valor_reais) * 100)
    if c < 0:
        raise ErroNegocio("O valor não pode ser negativo.")
    return c


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def competencia_de(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def somar_meses(comp: str, n: int) -> str:
    ano, mes = map(int, comp.split("-"))
    total = ano * 12 + (mes - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def validar_competencia(comp: str) -> str:
    try:
        ano, mes = map(int, comp.split("-"))
        if not 1 <= mes <= 12 or ano < 2000:
            raise ValueError
    except ValueError:
        raise ErroNegocio(f"Competência inválida '{comp}'. Use o formato AAAA-MM.") from None
    return f"{ano:04d}-{mes:02d}"


def validar_data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except (TypeError, ValueError):
        raise ErroNegocio(f"Data inválida '{texto}'. Use o formato AAAA-MM-DD.") from None


def nome_mes(comp: str) -> str:
    ano, mes = map(int, comp.split("-"))
    return f"{MESES[mes - 1]}/{ano}"


def data_br(iso: str | date) -> str:
    d = iso if isinstance(iso, date) else date.fromisoformat(iso)
    return d.strftime("%d/%m")


def vencimento_para(comp: str, dia: int) -> date:
    ano, mes = map(int, comp.split("-"))
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def normalizar_forma(forma: str | None) -> str | None:
    if not forma:
        return None
    f = normalizar(forma).replace("ã", "a")
    apelidos = {"cartao de credito": "cartao", "credito": "cartao", "debito": "cartao",
                "cartao de debito": "cartao", "especie": "dinheiro", "ted": "transferencia",
                "doc": "transferencia"}
    f = apelidos.get(f, f)
    if f not in FORMAS:
        raise ErroNegocio(f"Forma de pagamento '{forma}' desconhecida. Use: {', '.join(FORMAS)}.")
    return f


# ---------------------------------------------------------------- professoras

def sincronizar_professoras(conn, dona: str, equipe: dict[str, float]) -> None:
    """Grava a dona (sem comissão) e a equipe com seus percentuais de comissão."""
    conn.execute("UPDATE professoras SET dona = 0")
    conn.execute("INSERT INTO professoras (nome, comissao, dona) VALUES (?, 0, 1)"
                 " ON CONFLICT(nome) DO UPDATE SET comissao = 0, dona = 1", (dona,))
    for nome, pct in equipe.items():
        if normalizar(nome) == normalizar(dona):
            continue
        conn.execute("INSERT INTO professoras (nome, comissao) VALUES (?, ?)"
                     " ON CONFLICT(nome) DO UPDATE SET comissao = excluded.comissao", (nome, pct))
    # Alunos antigos, sem professora definida, ficam com a dona.
    conn.execute("UPDATE alunos SET professora = ? WHERE professora = ''", (dona,))
    conn.execute("UPDATE pagamentos SET professora = ? WHERE professora = ''", (dona,))
    conn.commit()


def professoras(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM professoras ORDER BY dona DESC, nome").fetchall()


def dona(conn) -> str:
    row = conn.execute("SELECT nome FROM professoras WHERE dona = 1").fetchone()
    return row["nome"] if row else ""


def encontrar_professora(conn, nome: str | None) -> sqlite3.Row:
    """Sem nome = a dona do estúdio."""
    lista = professoras(conn)
    if not nome:
        for p in lista:
            if p["dona"]:
                return p
        raise ErroNegocio("Nenhuma professora configurada.")
    alvo = normalizar(nome)
    achadas = [p for p in lista if normalizar(p["nome"]) == alvo] or \
              [p for p in lista if normalizar(p["nome"]).startswith(alvo)]
    if len(achadas) != 1:
        nomes = ", ".join(p["nome"] for p in lista)
        raise ErroNegocio(f"Professora '{nome}' não encontrada. Professoras do estúdio: {nomes}.")
    return achadas[0]


def calcular_comissao(valor: int, pct: float) -> int:
    return round(valor * pct / 100)


# ---------------------------------------------------------------- alunos

def _todos_alunos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alunos ORDER BY nome").fetchall()


def encontrar_aluno(conn: sqlite3.Connection, referencia: str) -> sqlite3.Row:
    """Acha um aluno por id ('#3' ou '3') ou por nome (parcial, sem acento)."""
    ref = str(referencia).strip()
    if ref.lstrip("#").isdigit():
        row = conn.execute("SELECT * FROM alunos WHERE id = ?", (int(ref.lstrip("#")),)).fetchone()
        if not row:
            raise ErroNegocio(f"Não existe aluno com id #{ref.lstrip('#')}.")
        return row

    alvo = normalizar(ref)
    if not alvo:
        raise ErroNegocio("Informe o nome do aluno.")
    alunos = _todos_alunos(conn)
    exatos = [a for a in alunos if normalizar(a["nome"]) == alvo]
    if len(exatos) == 1:
        return exatos[0]

    tokens = alvo.split()

    def combina(nome: str) -> bool:
        partes = normalizar(nome).split()
        return all(any(p.startswith(t) for p in partes) for t in tokens)

    candidatos = exatos or [a for a in alunos if combina(a["nome"])]
    ativos = [a for a in candidatos if a["ativo"]]
    if len(ativos) == 1:
        return ativos[0]
    if not ativos and len(candidatos) == 1:
        return candidatos[0]
    if candidatos:
        lista = "; ".join(
            f"#{a['id']} {a['nome']}{'' if a['ativo'] else ' (inativo)'}" for a in candidatos
        )
        raise ErroNegocio(f"Mais de um aluno combina com '{ref}': {lista}. Pergunte qual é.")

    parecidos = difflib.get_close_matches(alvo, [normalizar(a["nome"]) for a in alunos], n=3, cutoff=0.5)
    nomes = [a["nome"] for a in alunos if normalizar(a["nome"]) in parecidos]
    dica = f" Nomes parecidos: {', '.join(nomes)}." if nomes else ""
    raise ErroNegocio(f"Nenhum aluno encontrado com o nome '{ref}'.{dica}")


def primeira_competencia_padrao(dia_vencimento: int, hoje: date) -> str:
    """Se o dia de vencimento deste mês ainda não passou, cobra já este mês."""
    comp = competencia_de(hoje)
    return comp if vencimento_para(comp, dia_vencimento) >= hoje else somar_meses(comp, 1)


@dataclass
class NovoAluno:
    nome: str
    valor: int
    dia_vencimento: int
    inicio_cobranca: str
    telefone: str | None = None
    plano: str | None = None
    observacoes: str | None = None
    professora: str = ""


def preparar_cadastro(conn, hoje: date, nome: str, valor: float, dia_vencimento: int,
                      telefone: str | None = None, plano: str | None = None,
                      primeira_competencia: str | None = None,
                      observacoes: str | None = None, professora: str | None = None) -> NovoAluno:
    nome = " ".join((nome or "").split()).title()
    if not nome:
        raise ErroNegocio("Falta o nome do aluno.")
    dia = int(dia_vencimento)
    if not 1 <= dia <= 31:
        raise ErroNegocio("O dia de vencimento precisa estar entre 1 e 31.")
    valor_c = centavos(valor)
    if valor_c == 0:
        raise ErroNegocio("Falta o valor da mensalidade.")
    repetido = [a for a in _todos_alunos(conn) if normalizar(a["nome"]) == normalizar(nome)]
    if repetido:
        raise ErroNegocio(f"Já existe aluno com o nome {nome} (#{repetido[0]['id']}). "
                          "Confirme se é outra pessoa e diferencie o nome (ex.: sobrenome).")
    inicio = validar_competencia(primeira_competencia) if primeira_competencia \
        else primeira_competencia_padrao(dia, hoje)
    prof = encontrar_professora(conn, professora)["nome"]
    return NovoAluno(nome, valor_c, dia, inicio, telefone or None, plano or None, observacoes or None, prof)


def cadastrar_aluno(conn, hoje: date, novo: NovoAluno) -> int:
    cur = conn.execute(
        "INSERT INTO alunos (nome, telefone, plano, valor, dia_vencimento, inicio_cobranca, observacoes,"
        " professora) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (novo.nome, novo.telefone, novo.plano, novo.valor, novo.dia_vencimento,
         novo.inicio_cobranca, novo.observacoes, novo.professora),
    )
    conn.commit()
    gerar_mensalidades(conn, hoje)
    return cur.lastrowid


CAMPOS_EDITAVEIS = ("nome", "telefone", "plano", "valor", "dia_vencimento", "observacoes", "professora")


def preparar_edicao(conn, aluno_ref: str, alteracoes: dict) -> tuple[sqlite3.Row, dict]:
    aluno = encontrar_aluno(conn, aluno_ref)
    mudancas = {}
    for campo, valor in alteracoes.items():
        if campo not in CAMPOS_EDITAVEIS or valor is None:
            continue
        if campo == "valor":
            valor = centavos(valor)
            if valor == 0:
                raise ErroNegocio("O valor da mensalidade não pode ser zero.")
        elif campo == "dia_vencimento":
            valor = int(valor)
            if not 1 <= valor <= 31:
                raise ErroNegocio("O dia de vencimento precisa estar entre 1 e 31.")
        elif campo == "nome":
            valor = " ".join(str(valor).split()).title()
        elif campo == "professora":
            valor = encontrar_professora(conn, valor)["nome"]
        if aluno[campo] != valor:
            mudancas[campo] = valor
    if not mudancas:
        raise ErroNegocio("Nenhuma alteração informada (ou os dados já estão assim).")
    return aluno, mudancas


def editar_aluno(conn, hoje: date, aluno_id: int, mudancas: dict) -> None:
    sets = ", ".join(f"{c} = ?" for c in mudancas)
    conn.execute(f"UPDATE alunos SET {sets} WHERE id = ?", (*mudancas.values(), aluno_id))
    # Valor e vencimento novos valem para as mensalidades a partir deste mês
    # que ainda não receberam nenhum pagamento.
    if "valor" in mudancas or "dia_vencimento" in mudancas:
        aluno = conn.execute("SELECT * FROM alunos WHERE id = ?", (aluno_id,)).fetchone()
        abertas = conn.execute(
            "SELECT m.* FROM mensalidades m WHERE m.aluno_id = ? AND m.competencia >= ?"
            " AND m.cancelada = 0 AND NOT EXISTS (SELECT 1 FROM pagamentos p WHERE p.mensalidade_id = m.id)",
            (aluno_id, competencia_de(hoje)),
        ).fetchall()
        for m in abertas:
            conn.execute(
                "UPDATE mensalidades SET valor = ?, vencimento = ? WHERE id = ?",
                (aluno["valor"], vencimento_para(m["competencia"], aluno["dia_vencimento"]).isoformat(), m["id"]),
            )
    conn.commit()


def mensalidades_futuras_abertas(conn, aluno_id: int, hoje: date) -> list[sqlite3.Row]:
    """Mensalidades que ainda não venceram e não têm pagamento."""
    return conn.execute(
        "SELECT m.* FROM mensalidades m WHERE m.aluno_id = ? AND m.cancelada = 0 AND m.vencimento >= ?"
        " AND NOT EXISTS (SELECT 1 FROM pagamentos p WHERE p.mensalidade_id = m.id) ORDER BY competencia",
        (aluno_id, hoje.isoformat()),
    ).fetchall()


def inativar_aluno(conn, hoje: date, aluno_id: int) -> int:
    futuras = mensalidades_futuras_abertas(conn, aluno_id, hoje)
    conn.execute("UPDATE alunos SET ativo = 0, inativado_em = ? WHERE id = ?", (hoje.isoformat(), aluno_id))
    conn.executemany("UPDATE mensalidades SET cancelada = 1, observacao = 'aluno inativado' WHERE id = ?",
                     [(m["id"],) for m in futuras])
    conn.commit()
    return len(futuras)


def reativar_aluno(conn, hoje: date, aluno_id: int, primeira_competencia: str) -> None:
    conn.execute("UPDATE alunos SET ativo = 1, inativado_em = NULL, inicio_cobranca = ? WHERE id = ?",
                 (primeira_competencia, aluno_id))
    conn.execute(
        "UPDATE mensalidades SET cancelada = 0, observacao = NULL WHERE aluno_id = ? AND competencia >= ?"
        " AND cancelada = 1 AND observacao = 'aluno inativado'",
        (aluno_id, primeira_competencia),
    )
    conn.commit()
    gerar_mensalidades(conn, hoje)


# ---------------------------------------------------------------- mensalidades

def gerar_mensalidades(conn, hoje: date) -> int:
    """Garante as mensalidades do mês atual e do próximo para alunos ativos."""
    criadas = 0
    atual = competencia_de(hoje)
    for aluno in conn.execute("SELECT * FROM alunos WHERE ativo = 1").fetchall():
        for comp in (atual, somar_meses(atual, 1)):
            if comp < aluno["inicio_cobranca"]:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO mensalidades (aluno_id, competencia, valor, vencimento)"
                " VALUES (?, ?, ?, ?)",
                (aluno["id"], comp, aluno["valor"], vencimento_para(comp, aluno["dia_vencimento"]).isoformat()),
            )
            criadas += cur.rowcount
    conn.commit()
    return criadas


def pago_da_mensalidade(conn, mensalidade_id: int) -> int:
    return conn.execute("SELECT COALESCE(SUM(valor), 0) FROM pagamentos WHERE mensalidade_id = ?",
                        (mensalidade_id,)).fetchone()[0]


def situacao(m: sqlite3.Row | dict, pago: int, hoje: date) -> str:
    if m["cancelada"]:
        return "cancelada"
    if pago >= m["valor"]:
        return "paga"
    return "atrasada" if date.fromisoformat(m["vencimento"]) < hoje else "em aberto"


def mensalidades_com_saldo(conn, where: str = "1=1", params: tuple = ()) -> list[dict]:
    """Mensalidades (não canceladas) com nome do aluno, total pago e saldo."""
    rows = conn.execute(
        "SELECT m.*, a.nome, a.telefone, a.ativo, a.professora,"
        " COALESCE((SELECT SUM(p.valor) FROM pagamentos p WHERE p.mensalidade_id = m.id), 0) AS pago"
        " FROM mensalidades m JOIN alunos a ON a.id = m.aluno_id"
        f" WHERE m.cancelada = 0 AND ({where}) ORDER BY m.vencimento, a.nome",
        params,
    ).fetchall()
    resultado = []
    for r in rows:
        d = dict(r)
        d["saldo"] = max(d["valor"] - d["pago"], 0)
        resultado.append(d)
    return resultado


@dataclass
class AjusteMensalidade:
    aluno: sqlite3.Row
    competencia: str
    mensalidade_id: int | None
    valor_antigo: int | None
    valor_novo: int | None  # None = isentar/cancelar
    motivo: str | None


def preparar_ajuste(conn, hoje: date, aluno_ref: str, competencia: str,
                    novo_valor: float | None, isentar: bool, motivo: str | None) -> AjusteMensalidade:
    aluno = encontrar_aluno(conn, aluno_ref)
    comp = validar_competencia(competencia)
    if not isentar and novo_valor is None:
        raise ErroNegocio("Informe o novo valor ou peça para isentar/cancelar a mensalidade.")
    m = conn.execute("SELECT * FROM mensalidades WHERE aluno_id = ? AND competencia = ?",
                     (aluno["id"], comp)).fetchone()
    if m is None and comp > somar_meses(competencia_de(hoje), 12):
        raise ErroNegocio("Só dá para ajustar mensalidades de até 12 meses à frente.")
    if m is not None and pago_da_mensalidade(conn, m["id"]) and isentar:
        raise ErroNegocio("Essa mensalidade já tem pagamento registrado; desfaça o pagamento antes de isentar.")
    return AjusteMensalidade(aluno, comp, m["id"] if m else None, m["valor"] if m else None,
                             None if isentar else centavos(novo_valor), motivo)


def ajustar_mensalidade(conn, aj: AjusteMensalidade) -> None:
    if aj.mensalidade_id is None:
        conn.execute(
            "INSERT INTO mensalidades (aluno_id, competencia, valor, vencimento) VALUES (?, ?, ?, ?)",
            (aj.aluno["id"], aj.competencia, aj.aluno["valor"],
             vencimento_para(aj.competencia, aj.aluno["dia_vencimento"]).isoformat()),
        )
        aj.mensalidade_id = conn.execute(
            "SELECT id FROM mensalidades WHERE aluno_id = ? AND competencia = ?",
            (aj.aluno["id"], aj.competencia)).fetchone()[0]
    if aj.valor_novo is None:
        conn.execute("UPDATE mensalidades SET cancelada = 1, observacao = ? WHERE id = ?",
                     (aj.motivo or "isenta", aj.mensalidade_id))
    else:
        conn.execute("UPDATE mensalidades SET valor = ?, cancelada = 0, observacao = ? WHERE id = ?",
                     (aj.valor_novo, aj.motivo, aj.mensalidade_id))
    conn.commit()


# ---------------------------------------------------------------- pagamentos

@dataclass
class ParcelaPagamento:
    competencia: str
    mensalidade_id: int | None  # None = mensalidade futura ainda não gerada
    vencimento: str
    valor_mensalidade: int
    ja_pago: int
    aplicar: int


@dataclass
class PlanoPagamento:
    aluno: sqlite3.Row
    data: date
    forma: str | None
    valor: int
    parcelas: list[ParcelaPagamento]
    observacao: str | None
    sobra: int = 0
    professora: str = ""
    comissao_pct: float = 0

    @property
    def comissao(self) -> int:
        return sum(calcular_comissao(p.aplicar, self.comissao_pct) for p in self.parcelas)


def preparar_pagamento(conn, hoje: date, aluno_ref: str, valor: float | None = None,
                       data_pagamento: str | None = None, forma: str | None = None,
                       competencia: str | None = None, observacao: str | None = None) -> PlanoPagamento:
    """Calcula em quais mensalidades o pagamento será lançado (sem gravar).

    O dinheiro vai para a mensalidade em aberto mais antiga (ou a partir da
    competência informada). Se sobrar, adianta os meses seguintes.
    """
    aluno = encontrar_aluno(conn, aluno_ref)
    data_pag = validar_data(data_pagamento) if data_pagamento else hoje
    if data_pag > hoje:
        raise ErroNegocio("A data do pagamento está no futuro.")
    forma_n = normalizar_forma(forma)
    comp_ini = validar_competencia(competencia) if competencia else None

    existentes = {
        m["competencia"]: m for m in conn.execute(
            "SELECT * FROM mensalidades WHERE aluno_id = ? ORDER BY competencia", (aluno["id"],)
        ).fetchall()
    }

    def candidatas():
        """Gera (comp, mensalidade|None) em ordem, incluindo meses futuros."""
        if comp_ini:
            comp = comp_ini
        else:
            abertas = [c for c, m in existentes.items()
                       if not m["cancelada"] and pago_da_mensalidade(conn, m["id"]) < m["valor"]]
            if abertas:
                comp = abertas[0]
            elif existentes:
                comp = somar_meses(max(existentes), 1)
            else:
                comp = aluno["inicio_cobranca"]
        for _ in range(24):
            yield comp, existentes.get(comp)
            comp = somar_meses(comp, 1)

    parcelas: list[ParcelaPagamento] = []
    restante = centavos(valor) if valor is not None else None
    if restante == 0:
        raise ErroNegocio("O valor do pagamento não pode ser zero.")

    for comp, m in candidatas():
        if m is not None and m["cancelada"]:
            continue
        valor_m = m["valor"] if m else aluno["valor"]
        pago = pago_da_mensalidade(conn, m["id"]) if m else 0
        saldo = valor_m - pago
        if saldo <= 0:
            if comp_ini and comp == comp_ini and restante is None:
                raise ErroNegocio(f"A mensalidade de {nome_mes(comp)} de {aluno['nome']} já está paga.")
            continue
        venc = m["vencimento"] if m else vencimento_para(comp, aluno["dia_vencimento"]).isoformat()
        aplicar = saldo if restante is None else min(saldo, restante)
        parcelas.append(ParcelaPagamento(comp, m["id"] if m else None, venc, valor_m, pago, aplicar))
        if restante is None:
            break
        restante -= aplicar
        if restante == 0:
            break

    if not parcelas:
        raise ErroNegocio("Não encontrei mensalidade para lançar esse pagamento.")
    total = sum(p.aplicar for p in parcelas)
    prof = conn.execute("SELECT * FROM professoras WHERE nome = ?", (aluno["professora"],)).fetchone()
    return PlanoPagamento(aluno, data_pag, forma_n, total, parcelas, observacao or None,
                          sobra=(restante or 0), professora=aluno["professora"],
                          comissao_pct=prof["comissao"] if prof else 0)


def registrar_pagamento(conn, plano: PlanoPagamento) -> str:
    lote = uuid.uuid4().hex[:8]
    for p in plano.parcelas:
        mid = p.mensalidade_id
        if mid is None:
            conn.execute(
                "INSERT OR IGNORE INTO mensalidades (aluno_id, competencia, valor, vencimento) VALUES (?, ?, ?, ?)",
                (plano.aluno["id"], p.competencia, p.valor_mensalidade, p.vencimento),
            )
            mid = conn.execute("SELECT id FROM mensalidades WHERE aluno_id = ? AND competencia = ?",
                               (plano.aluno["id"], p.competencia)).fetchone()[0]
        conn.execute(
            "INSERT INTO pagamentos (aluno_id, mensalidade_id, valor, data, forma, observacao, lote,"
            " professora, comissao) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plano.aluno["id"], mid, p.aplicar, plano.data.isoformat(), plano.forma, plano.observacao, lote,
             plano.professora, calcular_comissao(p.aplicar, plano.comissao_pct)),
        )
    conn.commit()
    return lote


def ultimo_lote(conn, aluno_ref: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    aluno = encontrar_aluno(conn, aluno_ref)
    ultimo = conn.execute("SELECT lote FROM pagamentos WHERE aluno_id = ? ORDER BY id DESC LIMIT 1",
                          (aluno["id"],)).fetchone()
    if not ultimo:
        raise ErroNegocio(f"{aluno['nome']} não tem pagamentos registrados.")
    linhas = conn.execute(
        "SELECT p.*, m.competencia FROM pagamentos p LEFT JOIN mensalidades m ON m.id = p.mensalidade_id"
        " WHERE p.lote = ? ORDER BY m.competencia", (ultimo["lote"],)).fetchall()
    return aluno, linhas


def desfazer_lote(conn, lote: str) -> None:
    conn.execute("DELETE FROM pagamentos WHERE lote = ?", (lote,))
    conn.commit()
