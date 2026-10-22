#!/usr/bin/env bash
# Instala o bot no Pop!_OS / Ubuntu e deixa rodando como serviço (liga sozinho com o PC).
# Uso: dentro da pasta pilates-bot, rode:  bash deploy/instalar.sh
set -euo pipefail

PASTA="$(cd "$(dirname "$0")/.." && pwd)"
USUARIO="$(whoami)"
cd "$PASTA"

echo "==> Instalando pacotes do sistema (pede sua senha)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3

echo "==> Criando ambiente Python em .venv"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo
    echo "!! Criei o arquivo .env. Preencha TELEGRAM_TOKEN, ANTHROPIC_API_KEY e CHAVE_PIX:"
    echo "   nano $PASTA/.env"
    echo "   Depois rode este script de novo."
    exit 0
fi

echo "==> Criando serviço systemd 'pilates-bot'"
sudo tee /etc/systemd/system/pilates-bot.service >/dev/null <<UNIT
[Unit]
Description=Bot de mensalidades do Pilates (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=$USUARIO
WorkingDirectory=$PASTA
ExecStart=$PASTA/.venv/bin/python -m pilates
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now pilates-bot
sleep 3
sudo systemctl --no-pager status pilates-bot | head -n 12
echo
echo "Pronto! Para ver o que o bot está fazendo:  journalctl -u pilates-bot -f"
