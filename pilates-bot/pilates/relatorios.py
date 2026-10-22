"""Relatórios em texto curto, prontos para o Telegram."""

from datetime import date, timedelta
from urllib.parse import quote

from . import servico as s

DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _linha_mensalidade(m: dict, hoje: date, mostrar_mes: bool = False) -> str:
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
    return f"{marca} {m['nome']} — {valor} — {quando}{mes}{extra}"


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
    if pendentes:
        linhas.append("")
        linhas.append("*Pendentes:*")
        linhas += [_linha_mensalidade(m, hoje) for m in pendentes]

    atrasadas_antes = s.mensalidades_com_saldo(conn, "m.competencia < ? AND m.vencimento < ?",
                                               (comp, hoje.isoformat()))
    atrasadas_antes = [m for m in atrasadas_antes if m["saldo"] > 0]
    if atrasadas_antes:
        linhas.append("")
        linhas.append(f"⚠️ *Atrasados de meses anteriores:* {s.brl(sum(m['saldo'] for m in atrasadas_antes))}")
        linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True) for m in atrasadas_antes]

    caixa = conn.execute(
        "SELECT COALESCE(forma, 'não informada') AS forma, SUM(valor) AS total FROM pagamentos"
        " WHERE substr(data, 1, 7) = ? GROUP BY 1 ORDER BY 2 DESC", (comp,)).fetchall()
    if caixa:
        total = sum(r["total"] for r in caixa)
        formas = ", ".join(f"{r['forma']} {s.brl(r['total'])}" for r in caixa)
        linhas.append("")
        linhas.append(f"💰 Entrou no caixa em {s.nome_mes(comp)}: {s.brl(total)} ({formas})")
    return "\n".join(linhas)


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
        linhas += [_linha_mensalidade(m, hoje) for m in abertas]
    if pagas:
        linhas.append(f"Já pagaram adiantado: {', '.join(m['nome'] for m in pagas)} ✅")
    return "\n".join(linhas)


def atrasados(conn, hoje: date) -> str:
    ms = [m for m in s.mensalidades_com_saldo(conn, "m.vencimento < ?", (hoje.isoformat(),)) if m["saldo"] > 0]
    if not ms:
        return "✅ Ninguém em atraso. Tudo em dia!"
    linhas = [f"🔴 *Em atraso* — total {s.brl(sum(m['saldo'] for m in ms))}"]
    linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True) for m in ms]
    return "\n".join(linhas)


def lista_alunos(conn, hoje: date, quais: str = "ativos") -> str:
    where = {"ativos": "ativo = 1", "inativos": "ativo = 0"}.get(quais, "1=1")
    alunos = conn.execute(f"SELECT * FROM alunos WHERE {where} ORDER BY nome").fetchall()
    if not alunos:
        return "Nenhum aluno cadastrado ainda." if quais != "inativos" else "Nenhum aluno inativo."
    comp = s.competencia_de(hoje)
    linhas = [f"👥 *Alunos {quais}* ({len(alunos)})"]
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
        linhas.append(f"#{a['id']} {a['nome']} — {s.brl(a['valor'])} dia {a['dia_vencimento']}{plano} — {st}")
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
    if a["telefone"]:
        linhas.append(f"Telefone: {a['telefone']}")
    if a["observacoes"]:
        linhas.append(f"Obs.: {a['observacoes']}")
    linhas.append(f"Cobrança desde: {s.nome_mes(a['inicio_cobranca'])}")
    ms = s.mensalidades_com_saldo(conn, "m.aluno_id = ?", (a["id"],))
    abertas = [m for m in ms if m["saldo"] > 0]
    if abertas:
        linhas.append("\n*Em aberto:*")
        linhas += [_linha_mensalidade(m, hoje, mostrar_mes=True) for m in abertas]
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
        linhas.append(f"• {s.data_br(p['data'])} {p['nome']} — {s.brl(p['valor'])} {p['forma'] or ''}{ref}")
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
    return ("🗓️ Virou o mês! Fechamento e previsão:\n\n"
            + resumo_mes(conn, hoje, anterior) + "\n\n" + resumo_mes(conn, hoje))
