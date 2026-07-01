"""
integracao.py — carrega e cruza as fontes de dados externas ao OPC UA.

Fontes:
  dados/qualidade/   — planilha com abas "Qualidade" e "Especificação por produto"
  dados/producao/    — planilha de jumbos produzidos (velocidade, quebras, duração)
  dados/downtime/    — eventos de parada com classe, tipo e local
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

BASE     = Path(__file__).resolve().parent / "dados"
BASE_DIR = Path(__file__).resolve().parent

# AT1 = Fábrica 63, Máquina TR
_AT1_FABRICA = 63
_AT1_MAQUINA = "TR"

# ── banco de dados local ───────────────────────────────────────────────────

_DB = BASE_DIR / "temperatura_yankee.db"


def _init_db() -> None:
    con = sqlite3.connect(_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS temperatura_yankee (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    NOT NULL,
            la        REAL,
            meio      REAL,
            lc        REAL,
            operador  TEXT    DEFAULT '',
            memo      TEXT    DEFAULT ''
        )
    """)
    # migração: adiciona colunas novas se o banco já existia sem elas
    cur = con.execute("PRAGMA table_info(temperatura_yankee)")
    cols_existentes = {r[1] for r in cur.fetchall()}
    for col, tipo in [("meio", "REAL"), ("memo", "TEXT DEFAULT ''")]:
        if col not in cols_existentes:
            con.execute(f"ALTER TABLE temperatura_yankee ADD COLUMN {col} {tipo}")
    con.commit()
    con.close()


def salvar_temperatura_yankee(la: float | None, meio: float | None,
                               lc: float | None, operador: str = "",
                               memo: str = "") -> None:
    """Registra uma leitura manual de temperatura superfície Yankee (LA, Meio, LC)."""
    _init_db()
    con = sqlite3.connect(_DB)
    con.execute(
        "INSERT INTO temperatura_yankee (timestamp, la, meio, lc, operador, memo) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         la, meio, lc, operador.strip(), memo.strip()),
    )
    con.commit()
    con.close()


def carregar_temperaturas_yankee(dias: int = 30) -> pd.DataFrame:
    """Retorna leituras dos últimos N dias. Colunas: timestamp, la, meio, lc, operador, memo."""
    _init_db()
    desde = (pd.Timestamp.now() - pd.Timedelta(days=dias)).isoformat()
    con = sqlite3.connect(_DB)
    df = pd.read_sql(
        "SELECT timestamp, la, meio, lc, operador, memo FROM temperatura_yankee "
        "WHERE timestamp >= ? ORDER BY timestamp",
        con, params=(desde,),
    )
    con.close()
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df

# ── helpers ───────────────────────────────────────────────────────────────

def _primeiro_excel(pasta: Path) -> Optional[Path]:
    arqs = sorted(pasta.glob("*.xls*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return arqs[0] if arqs else None


def _primeiro_arquivo(pasta: Path) -> Optional[Path]:
    """Aceita Excel ou arquivo sem extensão (como 'downtime')."""
    candidatos = []
    for p in pasta.iterdir():
        if p.is_file() and not p.name.startswith("."):
            candidatos.append(p)
    if not candidatos:
        return None
    return sorted(candidatos, key=lambda p: p.stat().st_mtime, reverse=True)[0]


# ── qualidade ─────────────────────────────────────────────────────────────

def classificar_arquivo(caminho: Path) -> str:
    """Identifica o tipo de planilha: 'processo', 'qualidade', 'producao' ou 'downtime'.

    Tenta pelo nome do arquivo primeiro, depois pela estrutura interna.
    Levanta ValueError se não conseguir classificar.
    """
    caminho = Path(caminho)
    nome = caminho.name.lower()

    if any(k in nome for k in ["parametro", "analise_param", "opc"]):
        return "processo"
    if any(k in nome for k in ["qualidade", "quality"]):
        return "qualidade"
    if any(k in nome for k in ["fabricac", "boletim", "producao", "produção"]):
        return "producao"
    if any(k in nome for k in ["parada", "downtime", "stoppage"]):
        return "downtime"

    try:
        if caminho.suffix.lower() == ".csv":
            raw = pd.read_csv(caminho, sep=";", header=None, encoding="latin1", nrows=10)
        else:
            raw = pd.read_excel(caminho, header=None, nrows=10)
        conteudo = " ".join(str(v) for v in raw.values.flatten() if pd.notna(v)).lower()

        if "média:" in conteudo or "velocidade mp" in conteudo:
            return "processo"
        if "gramatura" in conteudo and "espessura" in conteudo and "unidade" in conteudo:
            return "qualidade"
        if "boletim" in conteudo or ("gr/m2" in conteudo and "quebras" in conteudo):
            return "producao"
        if ("horário" in conteudo or "horario" in conteudo) and "classe" in conteudo:
            return "downtime"
    except Exception:
        pass

    raise ValueError(f"Não foi possível classificar o arquivo: {caminho.name}")


def carregar_qualidade(caminho: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Retorna (df_qualidade, df_specs).

    df_qualidade: um jumbo por linha, colunas limpas, Data como datetime.
    df_specs: MultiIndex (produto, limite) × parâmetro — valores LSE/LSC/Meta/LIC/LIE.

    caminho: se fornecido, lê esse arquivo diretamente; caso contrário busca em dados/qualidade/.
    """
    arq = Path(caminho) if caminho is not None else _primeiro_excel(BASE / "qualidade")
    if arq is None:
        return pd.DataFrame(), pd.DataFrame()

    # ── aba Qualidade — tenta nome canônico, senão usa a primeira aba ────
    xl = pd.ExcelFile(arq)
    sheet_qual = next(
        (s for s in xl.sheet_names if "qualidade" in s.lower() or s == "Qualidade"),
        xl.sheet_names[0],
    )
    raw = xl.parse(sheet_qual, header=None)

    # ── specs: usa qualquer arquivo da pasta que tenha a aba de especificações ──
    # (as specs não mudam por mês, então pode ser um arquivo mais antigo)
    _arq_specs = next(
        (p for p in sorted((BASE / "qualidade").glob("*.xls*"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
         if any("spec" in s.lower() or "especifica" in s.lower()
                for s in pd.ExcelFile(p).sheet_names)),
        None,
    )

    # detecta automaticamente a linha de cabeçalho (primeira com >= 10 não-nulos)
    header_row = 0
    for i in range(min(5, len(raw))):
        if raw.iloc[i].notna().sum() >= 10:
            header_row = i
            break

    headers = raw.iloc[header_row].tolist()
    df = raw.iloc[header_row + 1:].copy()
    df.columns = headers
    df = df.reset_index(drop=True)

    # limpa nomes de coluna, remove \xa0 e resolve duplicatas
    cols = [str(c).strip().replace("\xa0", " ") for c in df.columns]
    seen: dict[str, int] = {}
    unique_cols = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            unique_cols.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            unique_cols.append(c)
    df.columns = unique_cols

    # converte Data
    if "Data" in df.columns:
        df["Data"] = pd.to_datetime(df["Data"], errors="coerce")

    # limpa espaços em campos string
    for col in ["Familia Produzida", "Familia Atual", "Unidade", "Status",
                "Turma", "Classe Atual"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    # converte colunas numéricas
    skip = {"Data", "Unidade", "Familia Produzida", "Familia Atual", "Status",
            "Turma", "Classe Atual", "Razão", "Tipo", "Local", "Observações",
            "Código Refugo", "Atributos Não Conforme", "Avaliação da condição do tubete da bobina",
            "Bobina de concessão", "Direcionamento", "Emenda", "Posição Bobina"}
    for col in df.columns:
        if col not in skip:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "."), errors="coerce"
            )

    # remove coluna vazia inicial se existir
    if "nan" in df.columns:
        df = df.drop(columns=["nan"])

    df = df.dropna(subset=["Data"]).reset_index(drop=True)

    # ── aba Especificação por produto ──────────────────────────────────────
    df_specs = _parsear_specs(_arq_specs) if _arq_specs else pd.DataFrame()

    return df, df_specs


def _parsear_specs(arq: Path) -> pd.DataFrame:
    """
    Lê a aba 'Especificação por produto' e retorna DataFrame com:
      índice: (produto, limite)   — ex: ('BDRR153BR', 'LSE')
      colunas: parâmetros         — ex: 'Gramatura', 'Espessura', ...
    """
    xl = pd.ExcelFile(arq)
    sheet_spec = next(
        (s for s in xl.sheet_names if "spec" in s.lower() or "especifica" in s.lower()),
        None,
    )
    if sheet_spec is None:
        return pd.DataFrame()
    raw = xl.parse(sheet_spec, header=None)
    raw = raw.dropna(axis=1, how="all").dropna(axis=0, how="all")
    raw = raw.reset_index(drop=True)

    LIMITES = {"LSE", "LSC", "Meta", "LIC", "LIE"}
    registros = []

    produto_atual = None
    colunas_atuais: list[str] = []

    for _, row in raw.iterrows():
        vals = [str(v).strip() if pd.notna(v) else "" for v in row]

        # detecta linha de produto (código como BDRR153BR)
        for v in vals:
            if v and v.startswith("B") and len(v) >= 8 and v[1:].replace("-","").isalnum():
                produto_atual = v
                break

        # detecta linha de cabeçalho de parâmetros
        non_empty = [v for v in vals if v and v not in LIMITES and not v.startswith("B")]
        if len(non_empty) >= 5 and any(k in " ".join(non_empty) for k in
                                        ["Gramatura", "Espessura", "Tração", "Umidade"]):
            colunas_atuais = vals
            continue

        # detecta linha de limite
        for i, v in enumerate(vals):
            if v in LIMITES and produto_atual and colunas_atuais:
                reg = {"produto": produto_atual, "limite": v}
                for j, col in enumerate(colunas_atuais):
                    if col and col not in LIMITES and j < len(vals):
                        try:
                            num = float(str(vals[j]).replace(",", "."))
                            reg[col] = num
                        except (ValueError, TypeError):
                            pass
                registros.append(reg)
                break

    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros).set_index(["produto", "limite"])
    df = df.apply(pd.to_numeric, errors="coerce")

    # BTRR157BR: mesma spec do BDRR153BR, só Handfeel LSC = 67.5
    if "BDRR153BR" in df.index.get_level_values(0) and "BTRR157BR" not in df.index.get_level_values(0):
        base = df.xs("BDRR153BR", level="produto").copy()
        novos = []
        for lim in base.index:
            row = base.loc[lim].copy()
            if lim == "LSC" and "Handfeel" in row.index:
                row["Handfeel"] = 67.5
            novos.append({"produto": "BTRR157BR", "limite": lim, **row.to_dict()})
        df_novo = pd.DataFrame(novos).set_index(["produto", "limite"])
        df = pd.concat([df, df_novo])

    return df


# ── produção (jumbos) ─────────────────────────────────────────────────────

def carregar_producao(caminho: Path | None = None) -> pd.DataFrame:
    """
    Retorna DataFrame com dados de produção por jumbo:
    Data, Unidade, Familia, Diametro, Largura, Gr/m2, Quebras, Velocidade,
    Duração, Corrida, Classe Atual.

    caminho: se fornecido, lê esse arquivo diretamente; caso contrário busca em dados/producao/.
    """
    arq = Path(caminho) if caminho is not None else _primeiro_excel(BASE / "producao")
    if arq is None:
        return pd.DataFrame()

    df = pd.read_excel(arq)
    df.columns = [str(c).strip() for c in df.columns]

    if "Data" in df.columns:
        df["Data"] = pd.to_datetime(df["Data"], errors="coerce")

    for col in ["Familia Fabricada", "Familia Atual", "Unidade", "Turma",
                "Classe Atual", "Status Fabricado", "Status Atual"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    df = df.dropna(subset=["Data"]).reset_index(drop=True)
    return df


# ── downtime ──────────────────────────────────────────────────────────────

def _dt_flat(raw: pd.DataFrame) -> pd.DataFrame:
    """Parser para formato flat: uma linha por evento, Início já é timestamp completo."""
    df = raw.copy()
    df.columns = [str(v).strip() for v in df.iloc[0]]
    df = df.iloc[1:].reset_index(drop=True)

    def _col(kws):
        for c in df.columns:
            cl = c.lower()
            if all(k in cl for k in kws):
                return c
        return None

    col_inicio = _col(["cio"])   # "Início" → encoding garble, mas "cio" final é ASCII
    col_dur    = _col(["minuto"])
    col_classe = _col(["classe"])
    col_tipo   = next((c for c in df.columns if c.lower() == "tipo"), None)
    col_causa  = _col(["causa"])
    col_maq    = next((c for c in df.columns
                       if "quina" in c.lower() and "tipo" not in c.lower()), None)

    if col_inicio is None or col_dur is None:
        return pd.DataFrame()

    out = pd.DataFrame({
        "Início":             pd.to_datetime(df[col_inicio], errors="coerce"),
        "Classe":             df[col_classe].astype(str).str.strip() if col_classe else "",
        "Tipo":               df[col_tipo].astype(str).str.strip() if col_tipo else "",
        "Causa":              df[col_causa].astype(str).str.strip() if col_causa else "",
        "Duração em Minutos": pd.to_numeric(df[col_dur], errors="coerce"),
        "Máquina":            df[col_maq].astype(str).str.strip() if col_maq else _AT1_MAQUINA,
    })
    out = out.dropna(subset=["Início", "Duração em Minutos"])
    return out[out["Duração em Minutos"] > 0].reset_index(drop=True)


def carregar_downtime(caminho: Path | None = None) -> pd.DataFrame:
    """
    Lê o relatório de paradas. Suporta dois formatos:
    - Flat (novo): uma linha por evento com Início/Fim como timestamps completos
    - Hierárquico (antigo): linhas pai/filho com Horário 'HH:MM -> HH:MM'

    Retorna: Início, Classe, Tipo, Causa, Duração em Minutos, Máquina
    """
    arq = Path(caminho) if caminho is not None else _primeiro_arquivo(BASE / "downtime")
    if arq is None:
        return pd.DataFrame()

    try:
        raw = pd.read_excel(arq, header=None)
    except Exception as e:
        raise ValueError(f"Não foi possível ler o arquivo de downtime '{arq.name}': {e}") from e

    # detecta formato pela primeira linha: flat tem "Início" + "Minutos"
    first = [str(v).strip().lower() for v in raw.iloc[0] if pd.notna(v)]
    if any("cio" in v for v in first) and any("minuto" in v for v in first):
        return _dt_flat(raw)

    # — formato hierárquico (arquivo antigo) —
    # localiza linha de cabeçalho (contém 'Data' e 'Classe')
    header_row = None
    for i in range(min(30, len(raw))):
        vals = [str(v).strip() for v in raw.iloc[i].tolist()]
        if "Data" in vals and "Classe" in vals:
            header_row = i
            break
    if header_row is None:
        return pd.DataFrame()

    header = [str(v).strip() for v in raw.iloc[header_row].tolist()]

    def _find(name):
        return next((i for i, v in enumerate(header) if v == name), None)

    COL_DATA   = _find("Data")
    COL_MAQ    = next((i for i, v in enumerate(header)
                       if "quina" in v.lower() and "tipo" not in v.lower()), None)
    COL_CLASSE = _find("Classe")
    COL_DESC   = next((i for i, v in enumerate(header) if "scri" in v.lower()), None)
    COL_HORA   = next((i for i, v in enumerate(header)
                       if v.lower() in ("horário", "horario")), None)
    COL_TEMPO  = _find("Tempo Total")

    if COL_HORA is None or COL_TEMPO is None:
        return pd.DataFrame()

    data_rows = raw.iloc[header_row + 1:].reset_index(drop=True)

    records    = []
    cur_data   = None
    cur_maq    = _AT1_MAQUINA
    cur_classe = ""
    cur_causa  = ""   # último rótulo sem horário/tempo

    for _, row in data_rows.iterrows():
        raw_data   = row.iloc[COL_DATA]   if COL_DATA   is not None else None
        raw_maq    = row.iloc[COL_MAQ]    if COL_MAQ    is not None else None
        raw_classe = row.iloc[COL_CLASSE] if COL_CLASSE is not None else None
        raw_desc   = row.iloc[COL_DESC]   if COL_DESC   is not None else None
        raw_hora   = row.iloc[COL_HORA]   if COL_HORA   is not None else None
        raw_tempo  = row.iloc[COL_TEMPO]  if COL_TEMPO  is not None else None

        hora_str  = str(raw_hora).strip() if pd.notna(raw_hora) else ""
        tem_hora  = "->" in hora_str
        desc_str  = str(raw_desc).strip() if pd.notna(raw_desc) and str(raw_desc).strip() not in ("nan", "") else ""
        duracao   = pd.to_numeric(raw_tempo, errors="coerce") if pd.notna(raw_tempo) else float("nan")

        # linha pai: atualiza contexto (Data, Máquina, Classe)
        if pd.notna(raw_data) and str(raw_data).strip() not in ("nan", "NaT", "Data"):
            cur_data = raw_data
            if pd.notna(raw_maq) and str(raw_maq).strip() not in ("nan", ""):
                cur_maq = str(raw_maq).strip()

        if pd.notna(raw_classe) and str(raw_classe).strip() not in ("nan", "Classe", ""):
            cur_classe = str(raw_classe).strip()
            cur_causa  = desc_str  # descrição da linha pai = causa do bloco

        # linha rótulo: sem horário e sem tempo → atualiza causa para próximo evento
        elif desc_str and not tem_hora and (pd.isna(duracao) or duracao == 0):
            cur_causa = desc_str
            continue

        # linha evento: tem horário '->'; registra o evento individual
        if not tem_hora or pd.isna(duracao) or duracao <= 0 or cur_data is None:
            continue

        # monta timestamp de início
        inicio = pd.NaT
        try:
            hora_ini = hora_str.split("->")[0].strip()
            data_dt  = pd.to_datetime(cur_data, dayfirst=True, errors="coerce")
            if not pd.isna(data_dt):
                inicio = pd.to_datetime(f"{data_dt.strftime('%Y-%m-%d')} {hora_ini}",
                                        errors="coerce")
        except Exception:
            pass
        if pd.isna(inicio):
            inicio = pd.to_datetime(cur_data, dayfirst=True, errors="coerce")

        tipo = desc_str if desc_str else cur_causa

        records.append({
            "Início":             inicio,
            "Classe":             cur_classe,
            "Tipo":               tipo,
            "Causa":              cur_causa,
            "Duração em Minutos": duracao,
            "Máquina":            cur_maq,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["Classe"] = df["Classe"].str.strip()
    df["Tipo"]   = df["Tipo"].str.strip()
    df["Causa"]  = df["Causa"].str.strip()
    return df


def carregar_downtime_paradas(incluir_hayout: bool = False) -> pd.DataFrame:
    """Retorna apenas os eventos de parada real (sem HAY-HAYOUT por padrão)."""
    df = carregar_downtime()
    if df.empty:
        return df
    if not incluir_hayout and "Classe" in df.columns:
        # normaliza espaços antes de comparar ("HAY        - HAYOUT" == "HAY-HAYOUT")
        classe_norm = df["Classe"].str.replace(r"\s+", "", regex=True).str.upper()
        df = df[~classe_norm.isin(["HAY-HAYOUT", "HAYHAYOUT"])].reset_index(drop=True)
    return df


# ── cruzamento qualidade × produção ──────────────────────────────────────

def cruzar_qualidade_producao() -> pd.DataFrame:
    """
    Junta qualidade e produção pelo campo Unidade (ID do jumbo).
    Retorna um DataFrame por jumbo com parâmetros de ambas as fontes.
    """
    dq, _ = carregar_qualidade()
    dp    = carregar_producao()

    if dq.empty or dp.empty:
        return pd.DataFrame()

    # renomear colunas duplicadas da produção antes do merge
    sufixo_prod = {c: c + "_prod" for c in dp.columns
                   if c in dq.columns and c != "Unidade"}
    dp = dp.rename(columns=sufixo_prod)

    merged = dq.merge(dp, on="Unidade", how="left", suffixes=("", "_prod"))
    return merged


# ── resumo de conformidade ────────────────────────────────────────────────

PARAMS_QUALIDADE_PRINCIPAIS = [
    "Gramatura", "GramaturaPM1", "Espessura",
    "Tração Longitudinal", "Tração Transversal", "Tração Transversal Úmida",
    "Alongamento", "Alvura", "Umidade", "Bulk", "Handfeel", "Maciez TSA",
]


def resumo_conformidade(df_qual: pd.DataFrame,
                         df_specs: pd.DataFrame) -> pd.DataFrame:
    """
    Para cada jumbo e parâmetro principal, verifica se está dentro da especificação.

    Retorna DataFrame com colunas:
      Unidade, Data, Familia, parametro, valor, LSE, LSC, Meta, status
      status: 'OK' | 'FORA_LSE' | 'FORA_LSC' | 'SEM_SPEC'
    """
    if df_qual.empty or df_specs.empty:
        return pd.DataFrame()

    params = [p for p in PARAMS_QUALIDADE_PRINCIPAIS if p in df_qual.columns]
    if not params:
        return pd.DataFrame()

    # qualidade: wide → long (uma linha por jumbo×parâmetro)
    id_cols = [c for c in ["Unidade", "Data", "Familia Atual"] if c in df_qual.columns]
    long = (
        df_qual[id_cols + params]
        .melt(id_vars=id_cols, value_vars=params, var_name="parametro", value_name="valor")
        .dropna(subset=["valor"])
        .rename(columns={"Familia Atual": "Familia"})
    )

    # specs: wide → long → pivot com LSE/LSC/Meta como colunas
    spec_params = [p for p in params if p in df_specs.columns]
    if spec_params:
        spec_pivot = (
            df_specs[spec_params].reset_index()
            .melt(id_vars=["produto", "limite"], value_vars=spec_params,
                  var_name="parametro", value_name="val")
            .dropna(subset=["val"])
            .pivot_table(index=["produto", "parametro"], columns="limite",
                         values="val", aggfunc="first")
            .reset_index()
        )
        for lim in ["LSE", "LSC", "Meta"]:
            if lim not in spec_pivot.columns:
                spec_pivot[lim] = float("nan")
        long = long.merge(
            spec_pivot[["produto", "parametro", "LSE", "LSC", "Meta"]],
            left_on=["Familia", "parametro"],
            right_on=["produto", "parametro"],
            how="left",
        ).drop(columns=["produto"])
    else:
        long["LSE"] = long["LSC"] = long["Meta"] = float("nan")

    # status vetorizado
    val = pd.to_numeric(long["valor"], errors="coerce")
    lse = pd.to_numeric(long["LSE"],   errors="coerce")
    lsc = pd.to_numeric(long["LSC"],   errors="coerce")
    long["status"] = np.select(
        [lse.isna() & lsc.isna(),
         ~lse.isna() & (val > lse),
         ~lsc.isna() & (val < lsc)],
        ["SEM_SPEC", "FORA_LSE", "FORA_LSC"],
        default="OK",
    )
    long["valor"] = val.round(4)
    return long.reset_index(drop=True)


# ── correlação processo × qualidade ──────────────────────────────────────

# Parâmetros de qualidade que queremos explicar com variáveis de processo
TARGETS_PQ = ["Gramatura", "Espessura", "Alongamento",
               "Handfeel", "Maciez TSA", "Umidade", "UmidadeQCS"]

# Parâmetros de produção que também entram como variáveis explicativas
VARS_PRODUCAO = ["Quebras", "Velocidade", "Gr/m2", "Diametro"]


def correlacionar_processo_qualidade(
    dados_opc: pd.DataFrame,
    dq: pd.DataFrame | None = None,
    dp: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Para cada jumbo cruza os parâmetros OPC UA médios do período de produção
    com os resultados de qualidade e variáveis de produção.

    Retorna:
      df_jumbos — um jumbo por linha com colunas de processo + qualidade
      df_corr   — matriz de correlação (targets × vars processo)
    """
    if dq is None:
        dq, _ = carregar_qualidade()
    if dp is None:
        dp = carregar_producao()

    if dq.empty or dp.empty or dados_opc.empty:
        return pd.DataFrame(), pd.DataFrame()

    # junta qualidade + produção por Unidade (ID do jumbo)
    sufixo = {c: c + "_prod" for c in dp.columns if c in dq.columns and c != "Unidade"}
    dp_r = dp.rename(columns=sufixo)
    jumbos = dq.merge(dp_r, on="Unidade", how="inner")

    # coluna de duração
    col_dur = next((c for c in dp_r.columns if "ura" in c.lower() and c not in ("Unidade",)), None)
    col_ts  = "Data" if "Data" in jumbos.columns else None

    if col_ts is None:
        return pd.DataFrame(), pd.DataFrame()

    opc_ts = dados_opc["timestamp"]
    opc_params = [c for c in dados_opc.columns if c != "timestamp"]

    registros = []
    for _, row in jumbos.iterrows():
        fim = pd.Timestamp(row[col_ts])
        # normaliza para tz-naive para comparar com timestamps do CSV (sem fuso)
        if fim.tzinfo is not None:
            fim = fim.tz_localize(None)
        dur = float(row[col_dur]) if col_dur and pd.notna(row.get(col_dur)) else 90.0
        ini = fim - pd.Timedelta(minutes=dur)

        mask = (opc_ts >= ini) & (opc_ts <= fim)
        janela = dados_opc.loc[mask, opc_params]

        if len(janela) < 2:
            continue

        rec = {"Unidade": row["Unidade"], "Data": fim,
               "Familia": str(row.get("Familia Atual", "")).strip()}

        # médias OPC UA no janela
        for p in opc_params:
            rec[f"opc_{p}"] = janela[p].mean()

        # targets de qualidade
        for t in TARGETS_PQ:
            if t in jumbos.columns:
                rec[t] = row.get(t)

        # variáveis de produção
        for v in VARS_PRODUCAO:
            col_v = v if v in jumbos.columns else v + "_prod"
            if col_v in jumbos.columns:
                rec[v] = row.get(col_v)

        registros.append(rec)

    if not registros:
        return pd.DataFrame(), pd.DataFrame()

    df_j = pd.DataFrame(registros)

    # converte para numérico onde possível
    for col in df_j.columns:
        if col not in ("Unidade", "Data", "Familia"):
            df_j[col] = pd.to_numeric(df_j[col], errors="coerce")

    # matriz de correlação: targets vs variáveis de processo
    opc_cols    = [c for c in df_j.columns if c.startswith("opc_")]
    prod_cols   = [v for v in VARS_PRODUCAO if v in df_j.columns]
    var_cols    = opc_cols + prod_cols
    target_cols = [t for t in TARGETS_PQ + ["Quebras"] if t in df_j.columns]

    if not var_cols or not target_cols:
        return df_j, pd.DataFrame()

    # correlação de cada var com cada target
    corr_rows = []
    for t in target_cols:
        y = df_j[t].dropna()
        for v in var_cols:
            x = df_j[v].dropna()
            idx = x.index.intersection(y.index)
            if len(idx) < 5:
                continue
            r = float(x.loc[idx].corr(y.loc[idx]))
            nome = v.replace("opc_", "")
            corr_rows.append({"target": t, "variavel": nome, "r": round(r, 3)})

    if not corr_rows:
        return df_j, pd.DataFrame()

    df_corr = (pd.DataFrame(corr_rows)
               .pivot(index="variavel", columns="target", values="r"))

    # ordena por magnitude máxima
    df_corr["_max"] = df_corr.abs().max(axis=1)
    df_corr = df_corr.sort_values("_max", ascending=False).drop(columns="_max")

    return df_j, df_corr


# ── pareto de downtime ────────────────────────────────────────────────────

def pareto_downtime(incluir_hayout: bool = False) -> pd.DataFrame:
    """
    Retorna Pareto de causas de downtime ordenado por tempo total (minutos).
    Colunas: Tipo, Classe, total_min, ocorrencias, pct_acumulado, sem_preench
    Paradas sem descrição aparecem como 'SEM PREENCHIMENTO'.
    """
    df = carregar_downtime_paradas(incluir_hayout)
    if df.empty or "Tipo" not in df.columns:
        return pd.DataFrame()

    if "Duração em Minutos" not in df.columns:
        return pd.DataFrame()

    # normaliza vazios para rótulo explícito
    df = df.copy()
    df["Tipo"] = df["Tipo"].fillna("").str.strip()
    df["Tipo"] = df["Tipo"].replace("", "SEM PREENCHIMENTO")

    grp_cols = [c for c in ["Tipo", "Classe"] if c in df.columns]
    grp = (
        df.groupby(grp_cols)["Duração em Minutos"]
        .agg(total_min="sum", ocorrencias="count")
        .reset_index()
        .sort_values("total_min", ascending=False)
    )
    grp["pct_acumulado"] = (grp["total_min"].cumsum() / grp["total_min"].sum() * 100).round(1)

    # marca linhas sem preenchimento para destaque visual
    grp["sem_preench"] = grp["Tipo"] == "SEM PREENCHIMENTO"

    # percentual geral de paradas sem preenchimento (por tempo e por ocorrência)
    total_min  = df["Duração em Minutos"].sum()
    total_occ  = len(df)
    sp_min = df.loc[df["Tipo"] == "SEM PREENCHIMENTO", "Duração em Minutos"].sum()
    sp_occ = (df["Tipo"] == "SEM PREENCHIMENTO").sum()
    grp.attrs["pct_sem_preench_min"] = round(sp_min / total_min * 100, 1) if total_min else 0
    grp.attrs["pct_sem_preench_occ"] = round(sp_occ / total_occ * 100, 1) if total_occ else 0

    return grp


# ── histórico de correlações (SQLite) ────────────────────────────────────────

_DB_ANALISES = BASE_DIR / "analises_salvas.db"


def _init_db_analises() -> None:
    with sqlite3.connect(_DB_ANALISES) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS historico_correlacoes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                salvo_em      TEXT    NOT NULL,
                periodo_ini   TEXT,
                periodo_fim   TEXT,
                produto       TEXT,
                n_jumbos      INTEGER,
                var_processo  TEXT    NOT NULL,
                var_qualidade TEXT    NOT NULL,
                r             REAL    NOT NULL,
                forca         TEXT    NOT NULL,
                observacao    TEXT
            )
        """)
        con.commit()


def salvar_snapshot_correlacoes(
    periodo_ini: str,
    periodo_fim: str,
    produto: str,
    n_jumbos: int,
    df_corr: "pd.DataFrame",
    observacao: str = "",
) -> int:
    """Persiste df_corr (index=var_processo, columns=var_qualidade) no SQLite.
    Retorna o número de linhas inseridas."""
    _init_db_analises()
    agora = datetime.now().isoformat(timespec="seconds")

    def _forca(r_val: float) -> str:
        a = abs(r_val)
        if a >= 0.7:
            return "forte"
        if a >= 0.5:
            return "moderada"
        if a >= 0.35:
            return "fraca"
        return "sem"

    rows = []
    for var_proc in df_corr.index:
        for var_qual in df_corr.columns:
            r_val = df_corr.at[var_proc, var_qual]
            if pd.isna(r_val):
                continue
            rows.append((
                agora, periodo_ini, periodo_fim, produto, n_jumbos,
                str(var_proc), str(var_qual), float(r_val),
                _forca(r_val), observacao,
            ))

    if not rows:
        return 0

    with sqlite3.connect(_DB_ANALISES) as con:
        con.executemany("""
            INSERT INTO historico_correlacoes
                (salvo_em, periodo_ini, periodo_fim, produto, n_jumbos,
                 var_processo, var_qualidade, r, forca, observacao)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, rows)
        con.commit()
    return len(rows)


def carregar_historico_snapshots() -> "pd.DataFrame":
    """Retorna todos os snapshots salvos como DataFrame."""
    _init_db_analises()
    with sqlite3.connect(_DB_ANALISES) as con:
        df = pd.read_sql("SELECT * FROM historico_correlacoes ORDER BY salvo_em DESC", con)
    return df


def comparar_correlacoes_por_periodo(
    dados_opc: "pd.DataFrame",
    dq: "pd.DataFrame",
    dp: "pd.DataFrame",
    freq: str = "W",
) -> "pd.DataFrame":
    """Roda correlação processo×qualidade separada por semana ('W') ou mês ('MS').

    Retorna DataFrame com colunas:
        periodo, var_processo, var_qualidade, r, forca, n_pontos
    """
    if dados_opc is None or dados_opc.empty:
        return pd.DataFrame()

    col_ts = next((c for c in dados_opc.columns if "timestamp" in c.lower()), None)
    if col_ts is None:
        return pd.DataFrame()

    dados_opc = dados_opc.copy()
    dados_opc[col_ts] = pd.to_datetime(dados_opc[col_ts], errors="coerce")
    dados_opc["_periodo"] = dados_opc[col_ts].dt.to_period(freq)

    periodos = sorted(dados_opc["_periodo"].dropna().unique())
    resultados = []

    def _forca(r_val: float) -> str:
        a = abs(r_val)
        if a >= 0.7:
            return "forte"
        if a >= 0.5:
            return "moderada"
        if a >= 0.35:
            return "fraca"
        return "sem"

    for periodo in periodos:
        mask = dados_opc["_periodo"] == periodo
        slice_opc = dados_opc[mask].drop(columns="_periodo")
        if len(slice_opc) < 5:
            continue
        try:
            _, df_c = correlacionar_processo_qualidade(slice_opc, dq, dp)
        except Exception:
            continue
        if df_c is None or df_c.empty:
            continue
        for var_proc in df_c.index:
            for var_qual in df_c.columns:
                r_val = df_c.at[var_proc, var_qual]
                if pd.isna(r_val):
                    continue
                resultados.append({
                    "periodo":       str(periodo),
                    "var_processo":  str(var_proc),
                    "var_qualidade": str(var_qual),
                    "r":             float(r_val),
                    "forca":         _forca(r_val),
                    "n_pontos":      int(mask.sum()),
                })

    return pd.DataFrame(resultados) if resultados else pd.DataFrame()
    return grp.reset_index(drop=True)
