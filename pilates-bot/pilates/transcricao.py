"""Transcrição de áudio local com faster-whisper (grátis e sem enviar o áudio para fora)."""

import asyncio
import logging
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _modelo(nome: str):
    from faster_whisper import WhisperModel  # import tardio: só carrega se chegar áudio

    log.info("carregando modelo whisper '%s'...", nome)
    return WhisperModel(nome, device="cpu", compute_type="int8")


def _transcrever(caminho: str, nome_modelo: str) -> str:
    segmentos, _ = _modelo(nome_modelo).transcribe(
        caminho, language="pt", vad_filter=True,
        initial_prompt="Conversa sobre alunos de Pilates, mensalidades, pagamentos, Pix, reais.",
    )
    return " ".join(seg.text.strip() for seg in segmentos).strip()


async def transcrever(caminho: str, nome_modelo: str) -> str:
    return await asyncio.to_thread(_transcrever, caminho, nome_modelo)


def pre_carregar(nome_modelo: str) -> None:
    """Baixa/carrega o modelo na inicialização para o primeiro áudio não demorar."""
    try:
        _modelo(nome_modelo)
    except Exception:
        log.exception("não consegui carregar o modelo de transcrição")
