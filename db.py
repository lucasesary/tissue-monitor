"""
db.py — camada de acesso ao banco Postgres (Neon, AT1).

Todas as funções públicas aceitam e retornam DataFrames do pandas,
mantendo compatibilidade com o restante do projeto.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

_DATABASE_URL = os.environ.get("DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida. Crie o arquivo .env com a string de conexão.")


@contextmanager
def _conn():
    con = psycopg2.connect(_DATABASE_URL)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── criação das tabelas ────────────────────────────────────────────────────

def init_db() -> None:
    """Cria todas as tabelas se ainda não existirem."""
    with _conn() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qualidade (
                id                       SERIAL PRIMARY KEY,
                unidade                  TEXT UNIQUE,
                data                     TIMESTAMPTZ,
                familia_produzida        TEXT,
                familia_atual            TEXT,
                turma                    TEXT,
                classe_atual             TEXT,
                status                   TEXT,
                gramatura                REAL,
                gramatura_pm1            REAL,
                espessura                REAL,
                tracao_longitudinal      REAL,
                tracao_transversal       REAL,
                tracao_transversal_umida REAL,
                handfeel                 REAL,
                brilho                   REAL,
                alvura                   REAL,
                umidade                  REAL,
                ingested_at              TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS producao (
                id              SERIAL PRIMARY KEY,
                unidade         TEXT UNIQUE,
                data            TIMESTAMPTZ,
                familia_fab     TEXT,
                familia_atual   TEXT,
                turma           TEXT,
                classe_atual    TEXT,
                status_fab      TEXT,
                status_atual    TEXT,
                diametro        REAL,
                largura         REAL,
                gr_m2           REAL,
                quebras         INTEGER,
                velocidade      REAL,
                duracao         REAL,
                corrida         INTEGER,
                ingested_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS downtime (
                id               SERIAL PRIMARY KEY,
                inicio           TIMESTAMPTZ,
                classe           TEXT,
                tipo             TEXT,
                causa            TEXT,
                duracao_minutos  REAL,
                maquina          TEXT,
                ingested_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (inicio, causa, maquina)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS temperatura_yankee (
                id        SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                la        REAL,
                meio      REAL,
                lc        REAL,
                operador  TEXT DEFAULT '',
                memo      TEXT DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dados_processo (
                id           SERIAL PRIMARY KEY,
                arquivo_nome TEXT NOT NULL,
                ts           TIMESTAMPTZ NOT NULL,
                params       JSONB NOT NULL,
                ingested_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (arquivo_nome, ts)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_proc_ts ON dados_processo (ts)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS specs (
                produto    TEXT NOT NULL,
                limite     TEXT NOT NULL,
                valores    JSONB NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (produto, limite)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingestao_log (
                id                 SERIAL PRIMARY KEY,
                email_from         TEXT,
                email_subject      TEXT,
                arquivo_nome       TEXT,
                tipo               TEXT,
                registros_inseridos INTEGER,
                status             TEXT,
                erro_msg           TEXT,
                ingested_at        TIMESTAMPTZ DEFAULT NOW()
            )
        """)


# ── qualidade ─────────────────────────────────────────────────────────────

_COL_QUAL = {
    "Unidade":                  "unidade",
    "Data":                     "data",
    "Familia Produzida":        "familia_produzida",
    "Familia Atual":            "familia_atual",
    "Turma":                    "turma",
    "Classe Atual":             "classe_atual",
    "Status":                   "status",
    "Gramatura":                "gramatura",
    "GramaturaPM1":             "gramatura_pm1",
    "Espessura":                "espessura",
    "Tração Longitudinal":      "tracao_longitudinal",
    "Tração Transversal":       "tracao_transversal",
    "Tração Transversal Úmida": "tracao_transversal_umida",
    "Handfeel":                 "handfeel",
    "Brilho":                   "brilho",
    "Alvura":                   "alvura",
    "Umidade":                  "umidade",
}


def upsert_qualidade(df: pd.DataFrame) -> int:
    """Insere ou atualiza dados de qualidade. Retorna nº de linhas inseridas."""
    df = df.rename(columns=_COL_QUAL)
    cols = [c for c in _COL_QUAL.values() if c in df.columns]
    if "unidade" not in cols:
        return 0

    inseridos = 0
    with _conn() as con:
        cur = con.cursor()
        for _, row in df[cols].iterrows():
            vals = {c: (None if pd.isna(row[c]) else row[c]) for c in cols}
            placeholders = ", ".join(f"%({c})s" for c in cols)
            col_names = ", ".join(cols)
            update_set = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in cols if c != "unidade"
            )
            cur.execute(f"""
                INSERT INTO qualidade ({col_names})
                VALUES ({placeholders})
                ON CONFLICT (unidade) DO UPDATE SET {update_set}
            """, vals)
            inseridos += cur.rowcount
    return inseridos


def carregar_qualidade_db(dias: int = 60) -> pd.DataFrame:
    """Retorna dados de qualidade dos últimos N dias."""
    desde = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=dias)
    with _conn() as con:
        df = pd.read_sql(
            "SELECT * FROM qualidade WHERE data >= %s ORDER BY data",
            con, params=(desde,)
        )
    if not df.empty:
        df = df.rename(columns={v: k for k, v in _COL_QUAL.items()})
        df["Data"] = pd.to_datetime(df["Data"], utc=True)
    return df


# ── produção ──────────────────────────────────────────────────────────────

_COL_PROD = {
    "Unidade":           "unidade",
    "Data":              "data",
    "Familia Fabricada": "familia_fab",
    "Familia Atual":     "familia_atual",
    "Turma":             "turma",
    "Classe Atual":      "classe_atual",
    "Status Fabricado":  "status_fab",
    "Status Atual":      "status_atual",
    "Diametro":          "diametro",
    "Largura":           "largura",
    "Gr/m2":             "gr_m2",
    "Quebras":           "quebras",
    "Velocidade":        "velocidade",
    "Duração":           "duracao",
    "Corrida":           "corrida",
}


def upsert_producao(df: pd.DataFrame) -> int:
    df = df.rename(columns=_COL_PROD)
    cols = [c for c in _COL_PROD.values() if c in df.columns]
    if "unidade" not in cols:
        return 0

    inseridos = 0
    with _conn() as con:
        cur = con.cursor()
        for _, row in df[cols].iterrows():
            vals = {c: (None if pd.isna(row[c]) else row[c]) for c in cols}
            placeholders = ", ".join(f"%({c})s" for c in cols)
            col_names = ", ".join(cols)
            update_set = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in cols if c != "unidade"
            )
            cur.execute(f"""
                INSERT INTO producao ({col_names})
                VALUES ({placeholders})
                ON CONFLICT (unidade) DO UPDATE SET {update_set}
            """, vals)
            inseridos += cur.rowcount
    return inseridos


def carregar_producao_db(dias: int = 60) -> pd.DataFrame:
    desde = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=dias)
    with _conn() as con:
        df = pd.read_sql(
            "SELECT * FROM producao WHERE data >= %s ORDER BY data",
            con, params=(desde,)
        )
    if not df.empty:
        df = df.rename(columns={v: k for k, v in _COL_PROD.items()})
        df["Data"] = pd.to_datetime(df["Data"], utc=True)
    return df


# ── downtime ──────────────────────────────────────────────────────────────

def upsert_downtime(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    inseridos = 0
    with _conn() as con:
        cur = con.cursor()
        for _, row in df.iterrows():
            inicio = row.get("Início")
            if pd.isna(inicio):
                continue
            cur.execute("""
                INSERT INTO downtime (inicio, classe, tipo, causa, duracao_minutos, maquina)
                VALUES (%(inicio)s, %(classe)s, %(tipo)s, %(causa)s, %(duracao)s, %(maquina)s)
                ON CONFLICT (inicio, causa, maquina) DO NOTHING
            """, {
                "inicio":  inicio.to_pydatetime() if hasattr(inicio, "to_pydatetime") else inicio,
                "classe":  str(row.get("Classe", "")),
                "tipo":    str(row.get("Tipo", "")),
                "causa":   str(row.get("Causa", "")),
                "duracao": float(row.get("Duração em Minutos", 0)),
                "maquina": str(row.get("Máquina", "TR")),
            })
            inseridos += cur.rowcount
    return inseridos


def carregar_downtime_db(dias: int = 60) -> pd.DataFrame:
    desde = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=dias)
    with _conn() as con:
        df = pd.read_sql(
            "SELECT inicio, classe, tipo, causa, duracao_minutos, maquina "
            "FROM downtime WHERE inicio >= %s AND maquina = 'TR' ORDER BY inicio",
            con, params=(desde,)
        )
    if not df.empty:
        df = df.rename(columns={
            "inicio":          "Início",
            "classe":          "Classe",
            "tipo":            "Tipo",
            "causa":           "Causa",
            "duracao_minutos": "Duração em Minutos",
            "maquina":         "Máquina",
        })
        df["Início"] = pd.to_datetime(df["Início"], utc=True)
    return df


# ── dados_processo (OPC UA) ──────────────────────────────────────────────

def upsert_processo(df: pd.DataFrame, arquivo_nome: str) -> int:
    """Salva DataFrame OPC UA no banco. df deve ter coluna 'timestamp' + colunas de parâmetros."""
    if df.empty:
        return 0

    param_cols = [c for c in df.columns if c != "timestamp"]
    inseridos = 0

    with _conn() as con:
        cur = con.cursor()
        for _, row in df.iterrows():
            ts = row["timestamp"]
            if pd.isna(ts):
                continue
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)

            params_dict = {}
            for c in param_cols:
                v = row[c]
                params_dict[c] = None if pd.isna(v) else float(v)

            cur.execute("""
                INSERT INTO dados_processo (arquivo_nome, ts, params)
                VALUES (%(arq)s, %(ts)s, %(params)s)
                ON CONFLICT (arquivo_nome, ts) DO NOTHING
            """, {
                "arq":    arquivo_nome,
                "ts":     ts,
                "params": psycopg2.extras.Json(params_dict),
            })
            inseridos += cur.rowcount
    return inseridos


def carregar_processo_db(dias: int = 90) -> pd.DataFrame:
    """Retorna dados OPC UA dos últimos N dias como DataFrame largo (timestamp + colunas de parâmetros)."""
    desde = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=dias)
    with _conn() as con:
        df = pd.read_sql(
            "SELECT ts, params FROM dados_processo WHERE ts >= %s ORDER BY ts",
            con, params=(desde,),
        )
    if df.empty:
        return pd.DataFrame()

    params_df = pd.json_normalize(df["params"])
    result = pd.concat([df["ts"].rename("timestamp"), params_df], axis=1)
    # Remove timezone para compatibilidade com o restante do código (tz-naive)
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True).dt.tz_localize(None)

    for col in params_df.columns:
        result[col] = pd.to_numeric(result[col], errors="coerce")

    return result.reset_index(drop=True)


# ── temperatura Yankee ────────────────────────────────────────────────────

def salvar_temperatura_yankee(la: float | None, meio: float | None,
                               lc: float | None, operador: str = "",
                               memo: str = "") -> None:
    with _conn() as con:
        con.cursor().execute(
            "INSERT INTO temperatura_yankee (timestamp, la, meio, lc, operador, memo) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (datetime.now(timezone.utc), la, meio, lc,
             operador.strip(), memo.strip()),
        )


def carregar_temperaturas_yankee(dias: int = 30) -> pd.DataFrame:
    desde = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=dias)
    with _conn() as con:
        df = pd.read_sql(
            "SELECT timestamp, la, meio, lc, operador, memo "
            "FROM temperatura_yankee WHERE timestamp >= %s ORDER BY timestamp",
            con, params=(desde,)
        )
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


# ── log de ingestão ───────────────────────────────────────────────────────

def log_ingestao(email_from: str, email_subject: str, arquivo_nome: str,
                 tipo: str, registros: int, status: str,
                 erro_msg: str = "") -> None:
    with _conn() as con:
        con.cursor().execute("""
            INSERT INTO ingestao_log
                (email_from, email_subject, arquivo_nome, tipo,
                 registros_inseridos, status, erro_msg)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (email_from, email_subject, arquivo_nome, tipo,
              registros, status, erro_msg))


# ── specs de produto ──────────────────────────────────────────────────────

def upsert_specs(df: pd.DataFrame) -> int:
    """Salva DataFrame de specs (MultiIndex produto×limite) no banco. Retorna nº de linhas."""
    if df.empty:
        return 0
    inseridos = 0
    with _conn() as con:
        cur = con.cursor()
        for (produto, limite), row in df.iterrows():
            valores = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
            cur.execute("""
                INSERT INTO specs (produto, limite, valores)
                VALUES (%s, %s, %s)
                ON CONFLICT (produto, limite)
                DO UPDATE SET valores = EXCLUDED.valores, updated_at = NOW()
            """, (produto, limite, psycopg2.extras.Json(valores)))
            inseridos += cur.rowcount
    return inseridos


def carregar_specs_db() -> pd.DataFrame:
    """Retorna specs de produto como DataFrame com MultiIndex (produto, limite)."""
    with _conn() as con:
        cur = con.cursor()
        cur.execute("SELECT produto, limite, valores FROM specs ORDER BY produto, limite")
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    registros = [{"produto": p, "limite": l, **(v or {})} for p, l, v in rows]
    df = pd.DataFrame(registros).set_index(["produto", "limite"])
    return df.apply(pd.to_numeric, errors="coerce")


# ── inicialização rápida ──────────────────────────────────────────────────

if __name__ == "__main__":
    print("Conectando ao banco...")
    init_db()
    print("Tabelas criadas com sucesso.")
