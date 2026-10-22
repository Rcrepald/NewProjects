from datetime import date

import pytest

from pilates import relatorios as r
from pilates import servico as s

HOJE = date(2026, 10, 22)


@pytest.fixture
def conn():
    c = s.conectar(":memory:")
    s.sincronizar_professoras(c, "Ellen", {"Amanda": 40})
    return c


def cadastrar(conn, nome="Maria Souza", valor=280, dia=10, hoje=HOJE, **kw):
    novo = s.preparar_cadastro(conn, hoje, nome, valor, dia, **kw)
    return s.cadastrar_aluno(conn, hoje, novo)


def test_brl_e_centavos():
    assert s.brl(123456) == "R$ 1.234,56"
    assert s.brl(5) == "R$ 0,05"
    assert s.centavos(279.9) == 27990
    assert s.centavos("R$ 1.234,50") == 123450


def test_cadastro_define_primeira_competencia(conn):
    # dia 10 já passou em 22/10 -> começa em novembro
    novo = s.preparar_cadastro(conn, HOJE, "maria  souza", 280, 10)
    assert novo.nome == "Maria Souza"
    assert novo.inicio_cobranca == "2026-11"
    # dia 25 ainda não passou -> começa em outubro
    assert s.preparar_cadastro(conn, HOJE, "Ana", 250, 25).inicio_cobranca == "2026-10"


def test_cadastro_gera_mensalidades(conn):
    aid = cadastrar(conn, dia=25, primeira_competencia="2026-10")
    comps = [m["competencia"] for m in conn.execute("SELECT * FROM mensalidades WHERE aluno_id=?", (aid,))]
    assert comps == ["2026-10", "2026-11"]


def test_cadastro_duplicado(conn):
    cadastrar(conn)
    with pytest.raises(s.ErroNegocio, match="Já existe"):
        s.preparar_cadastro(conn, HOJE, "Maria Souza", 280, 10)


def test_vencimento_fim_de_mes():
    assert s.vencimento_para("2027-02", 31) == date(2027, 2, 28)


def test_encontrar_aluno(conn):
    cadastrar(conn, "Maria Souza")
    cadastrar(conn, "Maria Lima")
    cadastrar(conn, "João Pedro")
    assert s.encontrar_aluno(conn, "joao")["nome"] == "João Pedro"
    assert s.encontrar_aluno(conn, "maria lim")["nome"] == "Maria Lima"
    with pytest.raises(s.ErroNegocio, match="Mais de um"):
        s.encontrar_aluno(conn, "maria")
    with pytest.raises(s.ErroNegocio, match="Nenhum aluno"):
        s.encontrar_aluno(conn, "Carla")
    assert s.encontrar_aluno(conn, "#3")["nome"] == "João Pedro"


def test_pagamento_simples_quita_mais_antiga(conn):
    cadastrar(conn, dia=25, primeira_competencia="2026-10")
    plano = s.preparar_pagamento(conn, HOJE, "maria", forma="Pix")
    assert [p.competencia for p in plano.parcelas] == ["2026-10"]
    assert plano.valor == 28000 and plano.forma == "pix"
    s.registrar_pagamento(conn, plano)
    ms = s.mensalidades_com_saldo(conn, "m.competencia = '2026-10'")
    assert ms[0]["saldo"] == 0


def test_pagamento_adiantado_varios_meses(conn):
    cadastrar(conn, dia=25, primeira_competencia="2026-10")
    plano = s.preparar_pagamento(conn, HOJE, "maria", valor=840)
    assert [p.competencia for p in plano.parcelas] == ["2026-10", "2026-11", "2026-12"]
    assert plano.parcelas[2].mensalidade_id is None  # dezembro ainda não existia
    s.registrar_pagamento(conn, plano)
    assert conn.execute("SELECT COUNT(*) FROM mensalidades").fetchone()[0] == 3
    # próximo pagamento vai para janeiro
    assert s.preparar_pagamento(conn, HOJE, "maria").parcelas[0].competencia == "2027-01"


def test_pagamento_parcial(conn):
    cadastrar(conn, dia=25, primeira_competencia="2026-10")
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "maria", valor=100))
    plano = s.preparar_pagamento(conn, HOJE, "maria")
    assert plano.parcelas[0].competencia == "2026-10"
    assert plano.valor == 18000


def test_pagamento_data_futura(conn):
    cadastrar(conn)
    with pytest.raises(s.ErroNegocio, match="futuro"):
        s.preparar_pagamento(conn, HOJE, "maria", data_pagamento="2026-12-01")


def test_desfazer(conn):
    cadastrar(conn, dia=25, primeira_competencia="2026-10")
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "maria", valor=560))
    _, linhas = s.ultimo_lote(conn, "maria")
    assert len(linhas) == 2
    s.desfazer_lote(conn, linhas[0]["lote"])
    assert conn.execute("SELECT COUNT(*) FROM pagamentos").fetchone()[0] == 0


def test_editar_valor_atualiza_mensalidades_abertas(conn):
    aid = cadastrar(conn, dia=25, primeira_competencia="2026-10")
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "maria"))  # paga outubro
    aluno, mud = s.preparar_edicao(conn, "maria", {"valor": 300, "dia_vencimento": 5})
    s.editar_aluno(conn, HOJE, aid, mud)
    ms = {m["competencia"]: m for m in conn.execute("SELECT * FROM mensalidades")}
    assert ms["2026-10"]["valor"] == 28000  # já paga, não muda
    assert ms["2026-11"]["valor"] == 30000
    assert ms["2026-11"]["vencimento"] == "2026-11-05"


def test_inativar_e_reativar(conn):
    aid = cadastrar(conn, dia=10)  # começa 2026-11
    assert s.inativar_aluno(conn, HOJE, aid) == 1
    assert s.gerar_mensalidades(conn, HOJE) == 0
    assert not s.mensalidades_com_saldo(conn)
    s.reativar_aluno(conn, HOJE, aid, "2026-11")
    assert len(s.mensalidades_com_saldo(conn)) == 1


def test_ajuste_e_isencao(conn):
    cadastrar(conn, dia=25, primeira_competencia="2026-10")
    s.ajustar_mensalidade(conn, s.preparar_ajuste(conn, HOJE, "maria", "2026-11", 200, False, "desconto"))
    s.ajustar_mensalidade(conn, s.preparar_ajuste(conn, HOJE, "maria", "2027-01", None, True, "férias"))
    s.gerar_mensalidades(conn, date(2026, 12, 1))
    s.gerar_mensalidades(conn, date(2027, 1, 1))
    ms = {m["competencia"]: m for m in s.mensalidades_com_saldo(conn)}
    assert ms["2026-11"]["valor"] == 20000
    assert "2027-01" not in ms
    # pagamento de 3 meses pula janeiro isento
    plano = s.preparar_pagamento(conn, HOJE, "maria", valor=280 + 200 + 280 + 280)
    assert [p.competencia for p in plano.parcelas] == ["2026-10", "2026-11", "2026-12", "2027-02"]


def test_relatorios(conn):
    cadastrar(conn, "Ana", 250, 5, primeira_competencia="2026-10")   # atrasada
    cadastrar(conn, "Bia", 300, 24)                                   # vence sábado
    cadastrar(conn, "Carla", 200, 20, primeira_competencia="2026-10")
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "carla", forma="pix"))

    resumo = r.resumo_mes(conn, HOJE)
    assert "Previsto: R$ 750,00" in resumo
    assert "Recebido: R$ 200,00 (26%)" in resumo
    assert "Falta receber: R$ 550,00" in resumo
    assert "pix R$ 200,00" in resumo

    semana = r.vencimentos(conn, HOJE)
    assert "Bia" in semana and "Ana" not in semana

    atras = r.atrasados(conn, HOJE)
    assert "Ana" in atras and "17 dias" in atras

    assert "Bia" in r.lista_alunos(conn, HOJE)
    assert "Últimos pagamentos" in r.ficha_aluno(conn, HOJE, "carla")
    assert r.aviso_diario(conn, HOJE) is None
    assert "Bia" in r.aviso_diario(conn, date(2026, 10, 24))


def test_mensagem_cobranca(conn):
    cadastrar(conn, "Ana Paula", 250, 5, telefone="(11) 98888-7777", primeira_competencia="2026-10")
    msg = r.mensagem_cobranca(conn, HOJE, "ana", "Ellen", "ellen@pix.com")
    assert "https://wa.me/5511988887777?text=" in msg
    assert "ficou em aberto" in msg and "ellen@pix.com" in msg


# ---------------------------------------------------------------- equipe / comissão

def test_cadastro_por_professora(conn):
    cadastrar(conn, "Ana")
    cadastrar(conn, "Paula", professora="amanda")
    profs = dict(conn.execute("SELECT nome, professora FROM alunos").fetchall())
    assert profs == {"Ana": "Ellen", "Paula": "Amanda"}
    with pytest.raises(s.ErroNegocio, match="não encontrada"):
        s.preparar_cadastro(conn, HOJE, "Zé", 100, 10, professora="Carol")


def test_comissao_no_pagamento(conn):
    cadastrar(conn, "Paula", 250, 25, professora="Amanda", primeira_competencia="2026-10")
    cadastrar(conn, "Ana", 300, 25, primeira_competencia="2026-10")
    plano = s.preparar_pagamento(conn, HOJE, "paula", valor=500)  # outubro + novembro
    assert plano.comissao == 20000 and plano.professora == "Amanda"
    s.registrar_pagamento(conn, plano)
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "ana"))
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "paula", valor=250,
                                                     data_pagamento="2026-09-30"))  # outro mês
    assert [p["comissao"] for p in conn.execute("SELECT comissao FROM pagamentos ORDER BY id")] == \
        [10000, 10000, 0, 10000]

    resumo = r.resumo_mes(conn, HOJE)
    assert "Comissão da Amanda (40% do que os alunos dela pagaram): R$ 200,00" in resumo
    assert "Fica para o estúdio: R$ 600,00" in resumo  # 500 + 300 - 200
    assert "Alunos da Amanda: previsto R$ 250,00" in resumo

    extrato = r.comissoes(conn, HOJE)
    assert "Comissão a pagar: R$ 200,00" in extrato and "Paula" in extrato
    assert "Ana" not in extrato
    assert "Comissão a pagar: R$ 100,00" in r.comissoes(conn, HOJE, "2026-09")


def test_comissao_fica_gravada_mesmo_se_percentual_mudar(conn):
    cadastrar(conn, "Paula", 250, 25, professora="Amanda", primeira_competencia="2026-10")
    s.registrar_pagamento(conn, s.preparar_pagamento(conn, HOJE, "paula"))
    s.sincronizar_professoras(conn, "Ellen", {"Amanda": 50})
    assert "R$ 100,00" in r.comissoes(conn, HOJE)
    assert s.preparar_pagamento(conn, HOJE, "paula").comissao == 12500


def test_transferir_aluno_de_professora(conn):
    aid = cadastrar(conn, "Paula", 250, 25, primeira_competencia="2026-10")
    aluno, mud = s.preparar_edicao(conn, "paula", {"professora": "amanda"})
    assert mud == {"professora": "Amanda"}
    s.editar_aluno(conn, HOJE, aid, mud)
    assert s.preparar_pagamento(conn, HOJE, "paula").comissao == 10000
    assert "[Amanda]" in r.lista_alunos(conn, HOJE)
    assert "Paula" in r.lista_alunos(conn, HOJE, professora="Amanda")
    assert "Professora: Amanda (comissão de 40%)" in r.ficha_aluno(conn, HOJE, "paula")


def test_migracao_de_banco_antigo(tmp_path):
    import sqlite3
    caminho = tmp_path / "antigo.db"
    velho = sqlite3.connect(caminho)
    velho.executescript("""
        CREATE TABLE alunos (id INTEGER PRIMARY KEY, nome TEXT NOT NULL, telefone TEXT, plano TEXT,
            valor INTEGER NOT NULL, dia_vencimento INTEGER NOT NULL, inicio_cobranca TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1, observacoes TEXT, criado_em TEXT, inativado_em TEXT);
        CREATE TABLE pagamentos (id INTEGER PRIMARY KEY, aluno_id INTEGER NOT NULL, mensalidade_id INTEGER,
            valor INTEGER NOT NULL, data TEXT NOT NULL, forma TEXT, observacao TEXT, lote TEXT NOT NULL,
            criado_em TEXT);
        INSERT INTO alunos (nome, valor, dia_vencimento, inicio_cobranca) VALUES ('Ana', 100, 5, '2026-10');
    """)
    velho.commit()
    velho.close()
    conn = s.conectar(caminho)
    s.sincronizar_professoras(conn, "Ellen", {"Amanda": 40})
    assert conn.execute("SELECT professora FROM alunos").fetchone()[0] == "Ellen"
