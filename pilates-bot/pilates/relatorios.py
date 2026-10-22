"""Relatórios em texto curto, prontos para o Telegram."""

from datetime import date, timedelta
from urllib.parse import quote

from . import servico as s

DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _tem_equipe(conn) -> bool:
    return len(s.professoras(conn)) > 1


def _etiqueta(conn, professora: str) -> str:
    """Marca alunos que não são da dona, ex.: ' [Amanda]'."""
    return f" [{professora}]" if professora and _tem_equipe(conn) and professora != s.dona(conn) else ""


def _linha_mensalidade(m: dict, hoje: date, mostrar_mes: bool = False, conn=None) -> str:
    venc = date.fromisoformat(m["vencimento"])
    status = s.situacao(m, m["pago"], hoje)
    marca = {"paga": "✅", "atrasada": "🔴", "em aberto": "⏳"}.get(status, "•")
    extra = f" (pagou {s.brl(m['pago'])})" if 0 < m["pago"] < m["valor"] else ""
    mes = f" · {s.nome_mes(m['competencia'])}" if mostrar_mes else ""
    if status == "atrasada":
        dias = (hoje - venc).days
        quando = f"venceu {s.data_br(venc)} ({dias} dia{'s' if dias > 1 else ''})"
    elif status == "paga":
        quando = f"venc. {s.data_br(venc)}"
    else:
        quando = f"vence {DIAS_SEMANA[venc.weekday()][:3]} {s.data_br(venc)}"
    valor = s.brl(m["saldo"]) if status != "paga" else s.brl(m["valor"])
    tag = _etiqueta(conn, m.get("professora", "")) if conn is not None else ""
    return f"{marca} {m['nome']}{tag} — {valor} — {quando}{mes}{extra}"


def resumo_mes(conn, hoje: date, competencia: str | None = None) -> str:
    comp = s.validar_competencia(competencia) if competencia else s.competencia_de(hoje)
    ms = s.mensalidades_com_saldo(conn, "m.competencia = ?", (comp,))
    previsto = sum(m["valor"] for m in ms)
    recebido = sum(min(m["pago"], m["valor"]) for m in ms)
    falta = sum(m["saldo"] for m in ms)
    pagas = [m for m in ms if m["saldo"] == 0]
    pendentes = [m for m in ms if m["saldo"] > 0]
    pct = f" ({recebido * 100 // previsto}%)" if previsto else ""

    linhas = [
        f"📊 *Resumo de {s.nome_mes(comp)}*",
        f"Previsto: {s.brl(previsto)} ({len(ms)} mensalidades)",
        f"Recebido: {s.brl(recebido)}{pct} — {len(pagas)} pagas",
        f"Falta receber: {s.brl(falta)} — {len(pendentes)} pendentes",
    ]
    if _tem_equipe(conn):
        linhas.append("")
        for prof in s.professoras(conn):
            dela = [m for m in ms if m["professora"] == prof["nome"]]
            if not dela:
                continue
            rec = sum(min(m["pago"], m["valor"]) for m in dela)
            linhas.append(f"• Alunos da {prof['nome']}: previsto {s.brl(sum(m['valor'] for m in dela))}"
                          f" · recebido {s.brl(rec)} · falta {s.brl(sum(m['saldo'] for m in dela))}")
    if pendentes:
        linhas.append("")
        linhas.append("*Pendentes:*")
        linhas += [_linha_mensalidade(m, hoje, conn=conn) for m in pendentes]

    atrasadas_antes = s.mensalidades_com_saldo(conn, "m.competencia < ? AND m.vencimento < ?",
                                               (comp, hoje.isoformat()))
    atrasadas_antes = [m for m in atrasadas_antes if m["saldo"] > 0]
    if atrasadas_antes:
        linhas.append("")
        linhas.append(f"⚠️ *Atrasados de meses anteriores:* {s.brl(sum(m['saldo'] for m in atrasadas_antes))}")
        linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True, conn=conn) for m in atrasadas_antes]

    caixa = conn.execute(
        "SELECT COALESCE(forma, 'não informada') AS forma, SUM(valor) AS total FROM pagamentos"
        " WHERE substr(data, 1, 7) = ? GROUP BY 1 ORDER BY 2 DESC", (comp,)).fetchall()
    if caixa:
        total = sum(r["total"] for r in caixa)
        formas = ", ".join(f"{r['forma']} {s.brl(r['total'])}" for r in caixa)
        linhas.append("")
        linhas.append(f"💰 Entrou no caixa em {s.nome_mes(comp)}: {s.brl(total)} ({formas})")
        comissoes = _comissoes_do_mes(conn, comp)
        if comissoes:
            total_com = sum(c["comissao"] for c in comissoes)
            for c in comissoes:
                linhas.append(f"💼 Comissão da {c['nome']} ({c['pct']:g}% do que os alunos dela pagaram): "
                              f"{s.brl(c['comissao'])}")
            linhas.append(f"🏠 Fica para o estúdio: {s.brl(total - total_com)}")
    return "\n".join(linhas)


def _comissoes_do_mes(conn, comp: str) -> list[dict]:
    """Comissão por professora sobre os pagamentos com data no mês (regime de caixa)."""
    rows = conn.execute(
        "SELECT pr.nome, pr.comissao AS pct, COALESCE(SUM(p.valor), 0) AS recebido,"
        " COALESCE(SUM(p.comissao), 0) AS comissao, COUNT(p.id) AS qtd"
        " FROM professoras pr LEFT JOIN pagamentos p ON p.professora = pr.nome AND substr(p.data, 1, 7) = ?"
        " WHERE pr.dona = 0 GROUP BY pr.nome ORDER BY pr.nome", (comp,)).fetchall()
    return [dict(r) for r in rows if r["qtd"] or r["pct"]]


def comissoes(conn, hoje: date, competencia: str | None = None, professora: str | None = None) -> str:
    """Extrato de comissão: cada pagamento dos alunos da professora no mês."""
    comp = s.validar_competencia(competencia) if competencia else s.competencia_de(hoje)
    equipe = [p for p in s.professoras(conn) if not p["dona"]]
    if professora:
        equipe = [s.encontrar_professora(conn, professora)]
    if not equipe:
        return "Não há professoras com comissão cadastradas."
    blocos = []
    for prof in equipe:
        pags = conn.execute(
            "SELECT p.*, a.nome AS aluno, m.competencia FROM pagamentos p JOIN alunos a ON a.id = p.aluno_id"
            " LEFT JOIN mensalidades m ON m.id = p.mensalidade_id"
            " WHERE p.professora = ? AND substr(p.data, 1, 7) = ? ORDER BY p.data, a.nome",
            (prof["nome"], comp)).fetchall()
        titulo = f"💼 *Comissão da {prof['nome']} — {s.nome_mes(comp)}*"
        if not pags:
            blocos.append(f"{titulo}\nNenhum pagamento de alunos dela neste mês.")
            continue
        linhas = [titulo]
        for p in pags:
            ref = f" ref. {s.nome_mes(p['competencia']).split('/')[0]}" if p["competencia"] else ""
            linhas.append(f"• {s.data_br(p['data'])} {p['aluno']} — {s.brl(p['valor'])}{ref} → {s.brl(p['comissao'])}")
        recebido = sum(p["valor"] for p in pags)
        com = sum(p["comissao"] for p in pags)
        linhas.append(f"Recebido dos alunos dela: {s.brl(recebido)}")
        linhas.append(f"*Comissão a pagar: {s.brl(com)}* ({prof['comissao']:g}%)")
        linhas.append(f"Fica para o estúdio: {s.brl(recebido - com)}")
        abertos = [m for m in s.mensalidades_com_saldo(conn, "a.professora = ? AND m.competencia <= ?",
                                                       (prof["nome"], comp)) if m["saldo"] > 0]
        if abertos:
            falta = sum(m["saldo"] for m in abertos)
            linhas.append(f"Ainda a receber dos alunos dela: {s.brl(falta)} "
                          f"(comissão futura de {s.brl(s.calcular_comissao(falta, prof['comissao']))})")
        blocos.append("\n".join(linhas))
    return "\n\n".join(blocos)


def vencimentos(conn, hoje: date, inicio: str | None = None, fim: str | None = None) -> str:
    """Quem vence no período (padrão: de hoje até domingo)."""
    ini = s.validar_data(inicio) if inicio else hoje
    fim_d = s.validar_data(fim) if fim else ini + timedelta(days=6 - ini.weekday())
    if fim_d < ini:
        raise s.ErroNegocio("A data final é anterior à inicial.")
    s.gerar_mensalidades(conn, hoje)
    ms = s.mensalidades_com_saldo(conn, "m.vencimento BETWEEN ? AND ?", (ini.isoformat(), fim_d.isoformat()))
    abertas = [m for m in ms if m["saldo"] > 0]
    pagas = [m for m in ms if m["saldo"] == 0]
    titulo = f"📅 *Vencimentos de {s.data_br(ini)} a {s.data_br(fim_d)}*"
    if not ms:
        return f"{titulo}\nNinguém vence nesse período. 🙌"
    linhas = [titulo]
    if abertas:
        linhas.append(f"A receber: {s.brl(sum(m['saldo'] for m in abertas))}")
        linhas += [_linha_mensalidade(m, hoje, conn=conn) for m in abertas]
    if pagas:
        linhas.append(f"Já pagaram adiantado: {', '.join(m['nome'] for m in pagas)} ✅")
    return "\n".join(linhas)


def atrasados(conn, hoje: date) -> str:
    ms = [m for m in s.mensalidades_com_saldo(conn, "m.vencimento < ?", (hoje.isoformat(),)) if m["saldo"] > 0]
    if not ms:
        return "✅ Ninguém em atraso. Tudo em dia!"
    linhas = [f"🔴 *Em atraso* — total {s.brl(sum(m['saldo'] for m in ms))}"]
    linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True, conn=conn) for m in ms]
    return "\n".join(linhas)


def lista_alunos(conn, hoje: date, quais: str = "ativos", professora: str | None = None) -> str:
    where = {"ativos": "ativo = 1", "inativos": "ativo = 0"}.get(quais, "1=1")
    params: tuple = ()
    if professora:
        prof = s.encontrar_professora(conn, professora)["nome"]
        where += " AND professora = ?"
        params = (prof,)
    alunos = conn.execute(f"SELECT * FROM alunos WHERE {where} ORDER BY nome", params).fetchall()
    if not alunos:
        return "Nenhum aluno cadastrado ainda." if quais != "inativos" else "Nenhum aluno inativo."
    comp = s.competencia_de(hoje)
    de_quem = f" da {prof}" if professora else ""
    linhas = [f"👥 *Alunos {quais}{de_quem}* ({len(alunos)})"]
    total = 0
    for a in alunos:
        m = s.mensalidades_com_saldo(conn, "m.aluno_id = ? AND m.competencia = ?", (a["id"], comp))
        if not a["ativo"]:
            st = "inativo"
        elif not m:
            st = f"começa {s.nome_mes(a['inicio_cobranca'])}"
        else:
            st = s.situacao(m[0], m[0]["pago"], hoje)
        if a["ativo"]:
            total += a["valor"]
        plano = f" · {a['plano']}" if a["plano"] else ""
        tag = "" if professora else _etiqueta(conn, a["professora"])
        linhas.append(f"#{a['id']} {a['nome']}{tag} — {s.brl(a['valor'])} dia {a['dia_vencimento']}{plano} — {st}")
    if quais != "inativos":
        linhas.append(f"\nSoma das mensalidades ativas: {s.brl(total)}")
    return "\n".join(linhas)


def ficha_aluno(conn, hoje: date, aluno_ref: str) -> str:
    a = s.encontrar_aluno(conn, aluno_ref)
    linhas = [
        f"👤 *{a['nome']}* (#{a['id']}){'' if a['ativo'] else ' — INATIVO'}",
        f"Mensalidade: {s.brl(a['valor'])}, vence todo dia {a['dia_vencimento']}"
        + (f" · {a['plano']}" if a["plano"] else ""),
    ]
    if _tem_equipe(conn):
        prof = conn.execute("SELECT * FROM professoras WHERE nome = ?", (a["professora"],)).fetchone()
        com = f" (comissão de {prof['comissao']:g}%)" if prof and prof["comissao"] else ""
        linhas.append(f"Professora: {a['professora']}{com}")
    if a["telefone"]:
        linhas.append(f"Telefone: {a['telefone']}")
    if a["observacoes"]:
        linhas.append(f"Obs.: {a['observacoes']}")
    linhas.append(f"Cobrança desde: {s.nome_mes(a['inicio_cobranca'])}")
    ms = s.mensalidades_com_saldo(conn, "m.aluno_id = ?", (a["id"],))
    abertas = [m for m in ms if m["saldo"] > 0]
    if abertas:
        linhas.append("\n*Em aberto:*")
        linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True, conn=conn) for m in abertas]
    else:
        linhas.append("\n✅ Nada em aberto.")
    pags = conn.execute(
        "SELECT p.*, m.competencia FROM pagamentos p LEFT JOIN mensalidades m ON m.id = p.mensalidade_id"
        " WHERE p.aluno_id = ? ORDER BY p.data DESC, p.id DESC LIMIT 6", (a["id"],)).fetchall()
    if pags:
        linhas.append("\n*Últimos pagamentos:*")
        for p in pags:
            ref = f" ref. {s.nome_mes(p['competencia'])}" if p["competencia"] else ""
            linhas.append(f"• {s.data_br(p['data'])} — {s.brl(p['valor'])} {p['forma'] or ''}{ref}".rstrip())
    return "\n".join(linhas)


def pagamentos_periodo(conn, inicio: str, fim: str) -> str:
    ini, fim_d = s.validar_data(inicio), s.validar_data(fim)
    pags = conn.execute(
        "SELECT p.*, a.nome, m.competencia FROM pagamentos p JOIN alunos a ON a.id = p.aluno_id"
        " LEFT JOIN mensalidades m ON m.id = p.mensalidade_id WHERE p.data BETWEEN ? AND ?"
        " ORDER BY p.data, a.nome", (ini.isoformat(), fim_d.isoformat())).fetchall()
    if not pags:
        return f"Nenhum pagamento entre {s.data_br(ini)} e {s.data_br(fim_d)}."
    linhas = [f"💰 *Recebimentos de {s.data_br(ini)} a {s.data_br(fim_d)}* — {s.brl(sum(p['valor'] for p in pags))}"]
    for p in pags:
        ref = f" ref. {s.nome_mes(p['competencia'])}" if p["competencia"] else ""
        linhas.append(f"• {s.data_br(p['data'])} {p['nome']}{_etiqueta(conn, p['professora'])} — "
                      f"{s.brl(p['valor'])} {p['forma'] or ''}{ref}")
    return "\n".join(linhas)


# ---------------------------------------------------------------- cobrança

def telefone_whatsapp(telefone: str | None) -> str | None:
    if not telefone:
        return None
    digitos = "".join(c for c in telefone if c.isdigit())
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    return digitos if len(digitos) >= 12 else None


def mensagem_cobranca(conn, hoje: date, aluno_ref: str, professora: str, chave_pix: str) -> str:
    a = s.encontrar_aluno(conn, aluno_ref)
    abertas = [m for m in s.mensalidades_com_saldo(conn, "m.aluno_id = ?", (a["id"],)) if m["saldo"] > 0]
    if not abertas:
        raise s.ErroNegocio(f"{a['nome']} não tem mensalidade em aberto.")
    primeiro_nome = a["nome"].split()[0]
    vencidas = [m for m in abertas if date.fromisoformat(m["vencimento"]) < hoje]
    if vencidas:
        alvo = vencidas
        total = sum(m["saldo"] for m in alvo)
        meses = ", ".join(s.nome_mes(m["competencia"]).split("/")[0] for m in alvo)
        corpo = (f"Oi, {primeiro_nome}! Tudo bem? 😊\n"
                 f"Passando para lembrar que a mensalidade do Pilates ({meses}) ficou em aberto, "
                 f"no total de {s.brl(total)}.")
    else:
        m = abertas[0]
        corpo = (f"Oi, {primeiro_nome}! Tudo bem? 😊\n"
                 f"Passando para lembrar da mensalidade do Pilates de {s.nome_mes(m['competencia']).split('/')[0]}, "
                 f"no valor de {s.brl(m['saldo'])}, com vencimento em {s.data_br(m['vencimento'])}.")
    if chave_pix:
        corpo += f"\nChave Pix: {chave_pix}"
    corpo += f"\nQualquer coisa, é só me chamar. Obrigada! 💚\n{professora}"

    tel = telefone_whatsapp(a["telefone"])
    if tel:
        link = f"https://wa.me/{tel}?text={quote(corpo)}"
        return f"Mensagem pronta para {a['nome']}:\n\n{corpo}\n\n👉 Toque para abrir no WhatsApp: {link}"
    return (f"Mensagem pronta para {a['nome']} (sem telefone cadastrado, copie e cole):\n\n{corpo}")


# ---------------------------------------------------------------- avisos automáticos

def aviso_semanal(conn, hoje: date) -> str:
    partes = [vencimentos(conn, hoje)]
    atras = atrasados(conn, hoje)
    if not atras.startswith("✅"):
        partes.append(atras)
    return "☀️ Bom dia! Sua semana:\n\n" + "\n\n".join(partes)


def aviso_diario(conn, hoje: date) -> str | None:
    """Só fala se alguém vence hoje ou venceu ontem sem pagar."""
    ontem = hoje - timedelta(days=1)
    hoje_ms = [m for m in s.mensalidades_com_saldo(conn, "m.vencimento = ?", (hoje.isoformat(),)) if m["saldo"]]
    ontem_ms = [m for m in s.mensalidades_com_saldo(conn, "m.vencimento = ?", (ontem.isoformat(),)) if m["saldo"]]
    if not hoje_ms and not ontem_ms:
        return None
    linhas = []
    if hoje_ms:
        linhas.append("📌 *Vence hoje:*")
        linhas += [f"• {m['nome']} — {s.brl(m['saldo'])}" for m in hoje_ms]
    if ontem_ms:
        linhas.append("🔴 *Venceu ontem e ainda não pagou:*")
        linhas += [f"• {m['nome']} — {s.brl(m['saldo'])}" for m in ontem_ms]
    linhas.append("\nSe já recebeu, é só me contar. Se quiser, peço a mensagem de cobrança pronta.")
    return "\n".join(linhas)


def aviso_mensal(conn, hoje: date) -> str:
    anterior = s.somar_meses(s.competencia_de(hoje), -1)
    texto = ("🗓️ Virou o mês! Fechamento e previsão:\n\n"
             + resumo_mes(conn, hoje, anterior) + "\n\n" + resumo_mes(conn, hoje))
    if _comissoes_do_mes(conn, anterior):
        texto += "\n\n" + comissoes(conn, hoje, anterior)
    return texto
