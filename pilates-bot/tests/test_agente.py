"""Fluxo de conversa com o Claude simulado (sem chamar a API)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pilates import agente as ag
from pilates import config, servico as s


def texto(t):
    return NS(type="text", text=t)


def ferramenta(id_, nome, entrada):
    return NS(type="tool_use", id=id_, name=nome, input=entrada)


class ClienteFalso:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.chamadas.append({**kw, "messages": list(kw["messages"])})
        conteudo, motivo = self.respostas.pop(0)
        return NS(content=conteudo, stop_reason=motivo)


@pytest.fixture
def agente(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("BANCO", str(tmp_path / "t.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "teste")
    cfg = config.carregar(tmp_path)
    conn = s.conectar(":memory:")
    return ag.Agente(cfg, conn)


def rodar(coro):
    return asyncio.run(coro)


CADASTRO = {"nome": "Maria Souza", "valor": 280, "dia_vencimento": 10}


def test_escrita_pede_confirmacao_e_grava_no_botao(agente):
    agente.cliente = ClienteFalso([
        ([texto("Confere pra mim 👇"), ferramenta("t1", "cadastrar_aluno", CADASTRO)], "tool_use"),
        ([texto("Feito!")], "end_turn"),
    ])
    resp = rodar(agente.responder(1, "cadastra a maria, 280, dia 10"))
    assert resp.pendente is not None
    assert "Maria Souza" in resp.pendente.resumo()
    assert agente.conn.execute("SELECT COUNT(*) FROM alunos").fetchone()[0] == 0

    resultado = agente.resolver_pendente(1, resp.pendente.numero, True)
    assert "cadastrado" in resultado
    assert agente.conn.execute("SELECT COUNT(*) FROM alunos").fetchone()[0] == 1

    # botão repetido não grava de novo
    assert agente.resolver_pendente(1, resp.pendente.numero, True) is None

    # a próxima mensagem leva o resultado da ação para a IA
    rodar(agente.responder(1, "valeu"))
    ultima = agente.cliente.chamadas[-1]["messages"][-1]
    assert ultima["role"] == "user"
    assert ultima["content"][0]["type"] == "tool_result"
    assert "gravado" in ultima["content"][0]["content"]
    assert "valeu" in ultima["content"][1]["text"]


def test_confirmacao_por_texto(agente):
    agente.cliente = ClienteFalso([
        ([ferramenta("t1", "cadastrar_aluno", CADASTRO)], "tool_use"),
    ])
    rodar(agente.responder(1, "cadastra a maria"))
    resp = rodar(agente.responder(1, "Sim!"))
    assert "cadastrado" in resp.texto
    assert len(agente.cliente.chamadas) == 1  # não precisou chamar a IA


def test_nova_mensagem_cancela_pendente(agente):
    agente.cliente = ClienteFalso([
        ([ferramenta("t1", "cadastrar_aluno", CADASTRO)], "tool_use"),
        ([texto("Ok, qual o valor certo?")], "end_turn"),
    ])
    rodar(agente.responder(1, "cadastra a maria"))
    resp = rodar(agente.responder(1, "espera, o valor tá errado"))
    assert resp.texto == "Ok, qual o valor certo?"
    assert agente.conn.execute("SELECT COUNT(*) FROM alunos").fetchone()[0] == 0
    enviado = agente.cliente.chamadas[-1]["messages"][-1]["content"][0]["content"]
    assert "não confirmou" in enviado


def test_erro_de_negocio_volta_para_ia(agente):
    agente.cliente = ClienteFalso([
        ([ferramenta("t1", "registrar_pagamento", {"aluno": "Fulano"})], "tool_use"),
        ([texto("Não achei o Fulano. Quem é?")], "end_turn"),
    ])
    resp = rodar(agente.responder(1, "fulano pagou"))
    assert resp.pendente is None
    assert "Fulano" in resp.texto
    resultado = agente.cliente.chamadas[-1]["messages"][-1]["content"][0]
    assert resultado["is_error"] and "Nenhum aluno" in resultado["content"]


def test_leitura_roda_direto(agente):
    agente.cliente = ClienteFalso([
        ([ferramenta("t1", "atrasados", {})], "tool_use"),
        ([texto("Ninguém atrasado!")], "end_turn"),
    ])
    resp = rodar(agente.responder(1, "quem tá devendo?"))
    assert resp.texto == "Ninguém atrasado!"
    resultado = agente.cliente.chamadas[-1]["messages"][-1]["content"][0]
    assert "Ninguém em atraso" in resultado["content"]


def test_parametros_da_api(agente):
    agente.cliente = ClienteFalso([([texto("Oi!")], "end_turn")])
    rodar(agente.responder(1, "oi"))
    kw = agente.cliente.chamadas[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in kw["betas"]
    assert "[Hoje:" in kw["messages"][0]["content"][0]["text"]
