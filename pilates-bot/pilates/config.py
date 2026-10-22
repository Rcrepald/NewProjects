"""Configuração lida de variáveis de ambiente (arquivo .env)."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _carregar_env(caminho: Path) -> None:
    """Lê um .env simples (CHAVE=valor) sem sobrescrever o ambiente."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    telegram_token: str
    chats_autorizados: frozenset[int]
    modelo: str
    esforco: str
    banco: Path
    fuso: ZoneInfo
    nome_estudio: str
    nome_professora: str
    chave_pix: str
    whisper_modelo: str
    equipe: dict[str, float]  # professora -> % de comissão


def carregar(raiz: Path | None = None) -> Config:
    raiz = raiz or Path(__file__).resolve().parent.parent
    _carregar_env(raiz / ".env")
    chats = {
        int(x) for x in os.environ.get("CHATS_AUTORIZADOS", "").replace(" ", "").split(",") if x
    }
    banco = Path(os.environ.get("BANCO", raiz / "dados" / "pilates.db"))
    banco.parent.mkdir(parents=True, exist_ok=True)
    return Config(
        telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
        chats_autorizados=frozenset(chats),
        modelo=os.environ.get("MODELO", "claude-opus-5-5"),
        esforco=os.environ.get("ESFORCO", "low"),
        banco=banco,
        fuso=ZoneInfo(os.environ.get("FUSO", "America/Sao_Paulo")),
        nome_estudio=os.environ.get("NOME_ESTUDIO", "Pilates da Ellen"),
        nome_professora=os.environ.get("NOME_PROFESSORA", "Ellen"),
        chave_pix=os.environ.get("CHAVE_PIX", ""),
        whisper_modelo=os.environ.get("WHISPER_MODELO", "small"),
        equipe=_ler_equipe(os.environ.get("EQUIPE", "")),
    )


def _ler_equipe(texto: str) -> dict[str, float]:
    """'Amanda:40, Bia:35' -> {'Amanda': 40.0, 'Bia': 35.0}"""
    equipe = {}
    for item in texto.split(","):
        if ":" in item:
            nome, pct = item.split(":", 1)
            equipe[nome.strip()] = float(pct.strip().rstrip("%").replace(",", "."))
    return equipe
