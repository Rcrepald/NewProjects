"""Bot do Telegram: recebe texto/áudio, conversa com o agente e manda avisos."""

import logging
import sqlite3
import tempfile
from datetime import datetime, time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import planilha, relatorios, servico, transcricao
from .agente import Agente, Pendente
from .config import Config, carregar

log = logging.getLogger("pilates")

AJUDA = """Oi! Eu sou sua assistente do estúdio 💚
Pode falar comigo por texto ou áudio, do seu jeito. Exemplos:

• "Cadastra a Maria Souza, 2x por semana, 280 reais, vence dia 10, telefone 11 99999-8888"
• "A Maria pagou no Pix hoje"
• "O João pagou 300 em dinheiro ontem"
• "Como tá o mês?" / "Quem vence essa semana?" / "Quem tá atrasado?"
• "Como tá a Maria?"
• "Manda a cobrança da Ana"
• "Em dezembro a Carla paga só 200" / "Janeiro a Bia tá de férias, não cobra"
• "A Júlia saiu do estúdio"
• "Apaga o último pagamento do João, lancei errado"

Atalhos: /resumo /semana /atrasados /alunos /backup /novo (começa conversa do zero)

Tudo que eu for gravar, te mostro antes com ✅ Confirmar / ❌ Cancelar."""


# ---------------------------------------------------------------- envio

async def enviar(bot, chat_id: int, texto: str, teclado=None):
    """Manda texto com Markdown; se a formatação quebrar, manda sem."""
    partes = [texto[i:i + 4000] for i in range(0, len(texto), 4000)] or [""]
    msg = None
    for i, parte in enumerate(partes):
        kb = teclado if i == len(partes) - 1 else None
        try:
            msg = await bot.send_message(chat_id, parte, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
                                         disable_web_page_preview=True)
        except BadRequest:
            msg = await bot.send_message(chat_id, parte, reply_markup=kb, disable_web_page_preview=True)
    return msg


def teclado_confirmacao(p: Pendente) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data=f"ok:{p.numero}"),
        InlineKeyboardButton("❌ Cancelar", callback_data=f"no:{p.numero}"),
    ]])


# ---------------------------------------------------------------- app

class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = servico.conectar(cfg.banco)
        self.agente = Agente(cfg, self.conn)

    def hoje(self):
        return datetime.now(self.cfg.fuso).date()

    def autorizado(self, update: Update) -> bool:
        return update.effective_chat is not None and update.effective_chat.id in self.cfg.chats_autorizados

    # ------------------------------------------------------------ handlers

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.autorizado(update):
            await update.message.reply_text(
                f"Olá! Este bot é privado.\nSeu código de chat é: {chat_id}\n"
                "Envie esse número para quem está configurando o bot.")
            log.info("chat não autorizado pediu /start: %s (%s)", chat_id, update.effective_user.full_name)
            return
        await enviar(ctx.bot, chat_id, AJUDA)

    async def comando_relatorio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.autorizado(update):
            return
        servico.gerar_mensalidades(self.conn, self.hoje())
        cmd = update.message.text.split()[0].lstrip("/").split("@")[0]
        h = self.hoje()
        texto = {
            "resumo": lambda: relatorios.resumo_mes(self.conn, h),
            "semana": lambda: relatorios.vencimentos(self.conn, h),
            "atrasados": lambda: relatorios.atrasados(self.conn, h),
            "alunos": lambda: relatorios.lista_alunos(self.conn, h),
        }[cmd]()
        await enviar(ctx.bot, update.effective_chat.id, texto)

    async def novo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.autorizado(update):
            return
        self.agente.esquecer(update.effective_chat.id)
        await update.message.reply_text("Pronto, comecei uma conversa nova 🙂")

    async def backup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.autorizado(update):
            return
        await self.enviar_backup(ctx.bot, [update.effective_chat.id])

    async def texto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.autorizado(update):
            return
        await self.conversar(update, ctx, update.message.text)

    async def audio(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.autorizado(update):
            return
        chat_id = update.effective_chat.id
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        midia = update.message.voice or update.message.audio
        arquivo = await midia.get_file()
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "audio.ogg"
            await arquivo.download_to_drive(caminho)
            try:
                texto = await transcricao.transcrever(str(caminho), self.cfg.whisper_modelo)
            except Exception:
                log.exception("falha na transcrição")
                await update.message.reply_text("Não consegui ouvir esse áudio 😕. Pode mandar de novo ou digitar?")
                return
        if not texto:
            await update.message.reply_text("O áudio veio sem fala que eu entendesse. Pode repetir?")
            return
        await update.message.reply_text(f"🎙️ “{texto}”")
        await self.conversar(update, ctx, texto)

    async def conversar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, texto: str):
        chat_id = update.effective_chat.id
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        servico.gerar_mensalidades(self.conn, self.hoje())
        resposta = await self.agente.responder(chat_id, texto)
        if resposta.pendente:
            if resposta.texto:
                await enviar(ctx.bot, chat_id, resposta.texto)
            corpo = "📝 *Vou gravar:*\n\n" + resposta.pendente.resumo()
            await enviar(ctx.bot, chat_id, corpo, teclado_confirmacao(resposta.pendente))
        elif resposta.texto:
            await enviar(ctx.bot, chat_id, resposta.texto)

    async def botao(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        consulta = update.callback_query
        if not self.autorizado(update):
            await consulta.answer()
            return
        acao, _, numero = (consulta.data or "").partition(":")
        resultado = self.agente.resolver_pendente(update.effective_chat.id, int(numero or 0), acao == "ok")
        await consulta.answer()
        if resultado is None:
            await consulta.edit_message_reply_markup(None)
            await consulta.message.reply_text("Esse pedido já tinha sido resolvido ou expirou. Pode pedir de novo?")
            return
        original = consulta.message.text or ""
        try:
            await consulta.edit_message_text(f"{original}\n\n{resultado}", disable_web_page_preview=True)
        except BadRequest:
            await consulta.edit_message_reply_markup(None)
            await consulta.message.reply_text(resultado)

    async def erro(self, update: object, ctx: ContextTypes.DEFAULT_TYPE):
        log.error("erro não tratado", exc_info=ctx.error)
        if isinstance(update, Update) and update.effective_chat and self.autorizado(update):
            await ctx.bot.send_message(update.effective_chat.id, "Ops, deu um erro aqui 😕. Tenta de novo?")

    # ------------------------------------------------------------ avisos agendados

    async def _avisar(self, bot, texto: str | None):
        if not texto:
            return
        for chat_id in self.cfg.chats_autorizados:
            await enviar(bot, chat_id, texto)

    async def job_diario(self, ctx: ContextTypes.DEFAULT_TYPE):
        h = self.hoje()
        servico.gerar_mensalidades(self.conn, h)
        if h.day == 1:
            await self._avisar(ctx.bot, relatorios.aviso_mensal(self.conn, h))
        if h.weekday() == 0:
            await self._avisar(ctx.bot, relatorios.aviso_semanal(self.conn, h))
        else:
            await self._avisar(ctx.bot, relatorios.aviso_diario(self.conn, h))

    async def job_backup(self, ctx: ContextTypes.DEFAULT_TYPE):
        await self.enviar_backup(ctx.bot, list(self.cfg.chats_autorizados))

    async def enviar_backup(self, bot, chats: list[int]):
        h = self.hoje()
        pasta = self.cfg.banco.parent / "backups"
        pasta.mkdir(exist_ok=True)
        xlsx = planilha.exportar(self.conn, h, pasta / f"pilates-{h:%Y-%m-%d}.xlsx")
        copia = pasta / f"pilates-{h:%Y-%m-%d}.db"
        destino = sqlite3.connect(copia)
        self.conn.backup(destino)
        destino.close()
        for chat_id in chats:
            with open(xlsx, "rb") as fh:
                await bot.send_document(chat_id, fh, filename=xlsx.name,
                                        caption="📦 Backup semanal: planilha com alunos, mensalidades e pagamentos.")
            with open(copia, "rb") as fh:
                await bot.send_document(chat_id, fh, filename=copia.name,
                                        caption="Arquivo do sistema (guarde, serve para restaurar tudo).")

    # ------------------------------------------------------------ montagem

    def construir(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).build()
        app.add_handler(CommandHandler(["start", "ajuda", "help"], self.start))
        app.add_handler(CommandHandler(["resumo", "semana", "atrasados", "alunos"], self.comando_relatorio))
        app.add_handler(CommandHandler("novo", self.novo))
        app.add_handler(CommandHandler("backup", self.backup))
        app.add_handler(CallbackQueryHandler(self.botao))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.audio))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.texto))
        app.add_error_handler(self.erro)

        fuso = self.cfg.fuso
        app.job_queue.run_daily(self.job_diario, time(8, 0, tzinfo=fuso), name="avisos")
        app.job_queue.run_daily(self.job_backup, time(20, 0, tzinfo=fuso), days=(0,), name="backup")  # domingo
        return app


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = carregar()
    if not cfg.telegram_token:
        raise SystemExit("Falta TELEGRAM_TOKEN no arquivo .env")
    if not cfg.chats_autorizados:
        log.warning("CHATS_AUTORIZADOS vazio: o bot só vai responder /start mostrando o código do chat.")
    app = App(cfg)
    servico.gerar_mensalidades(app.conn, app.hoje())
    transcricao.pre_carregar(cfg.whisper_modelo)
    log.info("bot no ar (modelo %s)", cfg.modelo)
    app.construir().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
