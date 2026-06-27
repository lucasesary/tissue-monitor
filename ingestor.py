"""
ingestor.py — worker IMAP que monitora caixa de e-mail.

Operadores enviam planilhas por e-mail → ingestor classifica, parseia e
salva no Postgres (Neon) via db.py. Roda em loop ou como job único.

Variáveis de ambiente (.env):
    IMAP_HOST            — servidor IMAP (ex: imap.gmail.com)
    IMAP_USER            — e-mail da caixa monitorada
    IMAP_PASS            — senha de app (Google App Password)
    IMAP_FOLDER          — pasta a monitorar (padrão: INBOX)
    INGESTOR_INTERVAL_S  — intervalo de polling em segundos (padrão: 300)
    ALLOWED_SENDERS      — e-mails autorizados, separados por vírgula (vazio = todos)
"""

from __future__ import annotations

import email
import gc
import imaplib
import logging
import os
import re
import shutil
import tempfile
import time
from email.header import decode_header
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import db
from integracao import (
    carregar_downtime,
    carregar_producao,
    carregar_qualidade,
    classificar_arquivo,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ingestor")

IMAP_HOST   = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_USER   = os.environ.get("IMAP_USER", "")
IMAP_PASS   = os.environ.get("IMAP_PASS", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
INTERVAL_S  = int(os.environ.get("INGESTOR_INTERVAL_S", "300"))

_raw_allowed = os.environ.get("ALLOWED_SENDERS", "")
ALLOWED_SENDERS: set[str] = (
    {s.strip().lower() for s in _raw_allowed.split(",") if s.strip()}
    if _raw_allowed.strip()
    else set()
)

_EXT_SUPORTADAS = {".xlsx", ".xls", ".csv"}


# ── helpers ───────────────────────────────────────────────────────────────


def _decode_header_value(raw: str) -> str:
    parts = decode_header(raw or "")
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extrair_remetente(msg: email.message.Message) -> str:
    raw = msg.get("From", "")
    match = re.search(r"<([^>]+)>", raw)
    return match.group(1).lower() if match else raw.strip().lower()


def _baixar_anexos(msg: email.message.Message, tmpdir: str) -> list[Path]:
    arquivos: list[Path] = []
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        nome_raw = part.get_filename() or ""
        nome = _decode_header_value(nome_raw)
        if not nome:
            continue
        sufixo = Path(nome).suffix.lower()
        if sufixo not in _EXT_SUPORTADAS:
            continue
        dados = part.get_payload(decode=True)
        if not dados:
            continue
        dest = Path(tmpdir) / nome
        dest.write_bytes(dados)
        arquivos.append(dest)
    return arquivos


def _processar_arquivo(
    caminho: Path,
    remetente: str,
    assunto: str,
) -> tuple[str, int, str]:
    """Classifica, parseia e faz upsert. Retorna (tipo, n_inseridos, erro_msg)."""
    try:
        tipo = classificar_arquivo(caminho)
    except ValueError as exc:
        return "desconhecido", 0, str(exc)

    try:
        if tipo == "qualidade":
            df, _ = carregar_qualidade(caminho)
            n = db.upsert_qualidade(df) if not df.empty else 0
        elif tipo == "producao":
            df = carregar_producao(caminho)
            n = db.upsert_producao(df) if not df.empty else 0
        elif tipo == "downtime":
            df = carregar_downtime(caminho)
            n = db.upsert_downtime(df) if not df.empty else 0
        elif tipo == "processo":
            from analisar import carregar_dados as _cd
            df_opc, _, _ = _cd(caminho)
            n = db.upsert_processo(df_opc, caminho.name) if not df_opc.empty else 0
    except Exception as exc:  # noqa: BLE001
        return tipo, 0, str(exc)

    return tipo, n, ""


# ── core ──────────────────────────────────────────────────────────────────


def _build_search_criteria() -> str:
    """Monta critério IMAP SEARCH: UNSEEN + filtro de remetentes + últimos 30 dias."""
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
    base = f"(UNSEEN SINCE {since})"

    if not ALLOWED_SENDERS:
        return base

    senders = list(ALLOWED_SENDERS)
    # constrói OR aninhado: (OR FROM a (OR FROM b FROM c))
    def _or_chain(lst: list[str]) -> str:
        if len(lst) == 1:
            return f'FROM "{lst[0]}"'
        return f'(OR FROM "{lst[0]}" {_or_chain(lst[1:])})'

    return f"({_or_chain(senders)} UNSEEN SINCE {since})"


def processar_emails_novos() -> int:
    """
    Conecta ao IMAP, processa e-mails não lidos dos últimos 30 dias (de remetentes
    autorizados se ALLOWED_SENDERS estiver definido) e marca como lidos somente
    os que tiveram anexos processados. Retorna o número de arquivos inseridos.
    """
    if not IMAP_USER or not IMAP_PASS:
        raise RuntimeError(
            "IMAP_USER e IMAP_PASS não definidos no .env. "
            "Configure antes de rodar o ingestor."
        )

    db.init_db()

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(IMAP_USER, IMAP_PASS)
    imap.select(IMAP_FOLDER)

    criteria = _build_search_criteria()
    log.info("IMAP search: %s", criteria)
    _, data = imap.search(None, criteria)
    ids = data[0].split()
    log.info("E-mails encontrados: %d", len(ids))

    sucesso_total = 0

    for msg_id in ids:
        _, raw = imap.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(raw[0][1])

        remetente = _extrair_remetente(msg)
        assunto   = _decode_header_value(msg.get("Subject", ""))

        if ALLOWED_SENDERS and remetente not in ALLOWED_SENDERS:
            log.warning("Remetente não autorizado: %s — ignorado", remetente)
            # marca como lido mesmo assim para não repetir a mensagem
            imap.store(msg_id, "+FLAGS", "\\Seen")
            continue

        log.info("Processando e-mail de %s | %s", remetente, assunto)

        tmpdir = tempfile.mkdtemp()
        try:
            anexos = _baixar_anexos(msg, tmpdir)

            if not anexos:
                log.info("  Sem anexos suportados — pulando (mantém não lido)")
                continue

            for arq in anexos:
                tipo, n, erro = _processar_arquivo(arq, remetente, assunto)
                status = "ok" if not erro else "erro"

                db.log_ingestao(
                    email_from=remetente,
                    email_subject=assunto,
                    arquivo_nome=arq.name,
                    tipo=tipo,
                    registros=n,
                    status=status,
                    erro_msg=erro,
                )

                if erro:
                    log.error("  %s → ERRO: %s", arq.name, erro)
                else:
                    log.info("  %s → %s | %d registros inseridos", arq.name, tipo, n)
                    sucesso_total += 1

            # força liberação de handles antes de deletar a pasta (Windows)
            gc.collect()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        # só marca como lido se tinha anexos (já processados com sucesso ou erro)
        imap.store(msg_id, "+FLAGS", "\\Seen")

    imap.logout()
    return sucesso_total


def run_loop() -> None:
    """Loop contínuo: processa e-mails a cada INTERVAL_S segundos."""
    log.info(
        "Ingestor iniciado | host=%s | user=%s | intervalo=%ds",
        IMAP_HOST, IMAP_USER, INTERVAL_S,
    )
    while True:
        try:
            n = processar_emails_novos()
            log.info("Ciclo concluído — %d arquivo(s) ingerido(s)", n)
        except Exception as exc:  # noqa: BLE001
            log.error("Erro no ciclo: %s", exc)
        time.sleep(INTERVAL_S)


# ── entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        n = processar_emails_novos()
        print(f"Concluído — {n} arquivo(s) ingerido(s).")
    else:
        run_loop()
