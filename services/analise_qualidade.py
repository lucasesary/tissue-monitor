"""
Análise de variáveis de qualidade: cruzamento Produção × Qualidade × Processo.

A variável-alvo é parametrizável — Espessura, Umidade, Tração, Handfeel, etc.
O dashboard importa `resumo_qualidade(variavel_alvo)` e exibe o resultado.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_DADOS = Path(__file__).parent.parent / "dados"

_MESES_PT: dict[str, str] = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}

# Variáveis de qualidade que podem ser alvo OU preditoras
# (quando uma é alvo, é excluída da lista de preditoras automaticamente)
_VARS_QUALIDADE_TODAS = [
    "Espessura", "Umidade", "UmidadeQCS", "Gramatura",
    "Tração Longitudinal", "Tração Transversal", "Tração Transversal Úmida",
    "Alongamento", "Handfeel", "Maciez TSA", "Bulk",
    "Alvura", "GMT", "GramaturaPM1",
]

# Variáveis de processo a usar como preditoras (independente do alvo)
_VARS_PROCESSO = [
    "PRENSA",
    "Potência Ref. 01",
    "Potência Ref. 02",
    "Pressão Entrada Ref 01",
    "Pressão Saída Ref 01",
    "Pressão Entrada Ref 02",
    "Pressão Saída Ref 02",
    "Temperatura Capota LS",
    "Temperatura Capota LU",
    "Redry",
    "Extração Vapor",
    "umidade QCS",
    "Consistência TQ Máquina",
    "Jato/Tela",
    "Velocidade MP",
    "Crepe",
    "Tensão Tela",
    "Tensão Feltro",
    "Bulbo",
    "Diferencial",
]

_VARS_PRODUCAO_EXTRA = ["Velocidade", "Gr/m2"]

_COLS_YANKEE = [
    "Temperatura Superfície Yankee LA",
    "Temperatura Superfície Yankee LC",
]

# Unidades por variável — usadas nas strings de magnitude (sem r= exposto)
_UNIDADES: dict[str, str] = {
    "Espessura": "mm",
    "Gramatura": "g/m²",
    "GramaturaPM1": "g/m²",
    "Umidade": "%",
    "UmidadeQCS": "%",
    "Alongamento": "%",
    "Tração Longitudinal": "N/m",
    "Tração Transversal": "N/m",
    "Tração Transversal Úmida": "N/m",
    "Bulk": "cm³/g",
    "Handfeel": "pts",
    "Maciez TSA": "pts",
    "Alvura": "%ISO",
    "GMT": "N/m",
}

# Lista exportada para o dropdown da interface (label visível, valor interno)
VARIAVEIS_QUALIDADE: list[dict] = [
    {"label": v, "value": v}
    for v in [
        "Espessura",
        "Gramatura",
        "Tração Longitudinal",
        "Tração Transversal",
        "Alongamento",
        "Umidade",
        "Handfeel",
        "Maciez TSA",
        "Bulk",
        "Alvura",
        "GMT",
        "Tração Transversal Úmida",
        "UmidadeQCS",
        "GramaturaPM1",
    ]
]


# ── Helpers internos ──────────────────────────────────────────────────────────

def _parse_ts_pt(v: str) -> "pd.Timestamp":
    m = re.match(r"(\d+)-(\w+)-(\d+)\s+(\d+:\d+:\d+)", str(v).strip())
    if not m:
        return pd.NaT
    d, mes, y, t = m.groups()
    y = "20" + y if len(y) == 2 else y
    return pd.to_datetime(
        f"{y}-{_MESES_PT.get(mes.lower(), mes)}-{d} {t}", errors="coerce"
    )


def _to_float(s: "pd.Series") -> "pd.Series":
    return pd.to_numeric(
        s.astype(str).str.replace(",", ".").str.strip(), errors="coerce"
    )


def _mais_recente(diretorio: Path, glob: str) -> Path | None:
    candidatos = sorted(
        diretorio.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidatos[0] if candidatos else None


def _safe_float(row: "pd.Series", col: str) -> float | None:
    v = row.get(col)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v), 3)


def _unidade(variavel: str) -> str:
    return _UNIDADES.get(variavel, "u")


# ── Carregamento das fontes ───────────────────────────────────────────────────

def _carregar_producao(path: Path) -> "pd.DataFrame":
    df = pd.read_excel(path, sheet_name=0)
    df["Data"] = pd.to_datetime(df["Data"], errors="coerce")
    df["Track Num"] = pd.to_numeric(df["Track Num"], errors="coerce")
    return df.dropna(subset=["Track Num", "Data"]).reset_index(drop=True)


def _carregar_qualidade(path: Path) -> "pd.DataFrame":
    raw = pd.read_excel(path, sheet_name=0, header=0)
    real_cols = [
        str(c).strip() if pd.notna(c) else f"_col{i}"
        for i, c in enumerate(raw.iloc[0])
    ]
    raw.columns = real_cols
    df = raw.iloc[1:].reset_index(drop=True)

    df["Data"] = pd.to_datetime(df["Data"], errors="coerce")
    df["Num.Rastreamento"] = pd.to_numeric(df["Num.Rastreamento"], errors="coerce")

    # Converter todas as colunas numéricas possíveis (vírgula decimal)
    cols_num = [
        "Espessura", "Espessura A", "Espessura C", "Espessura M",
        "Umidade", "Umidade A", "Umidade C", "Umidade M", "UmidadeQCS",
        "Gramatura", "Gramatura A", "Gramatura C", "Gramatura M", "GramaturaPM1",
        "Tração Longitudinal", "Tração Longitudinal A", "Tração Longitudinal C", "Tração Longitudinal M",
        "Tração Transversal", "Tração Transversal A", "Tração Transversal C", "Tração Transversal M",
        "Tração Transversal Úmida",
        "Alongamento", "Alongamento A", "Alongamento C", "Alongamento M",
        "Bulk", "Handfeel", "Maciez TSA", "Alvura", "GMT",
        "Densidade Aparente", "Fator de Orientação MD/CD", "Emenda",
        "Diâmetro bobina", "Largura da bobina", "Teor Seco",
        "Furos - Diâmetro", "Furos - Quantidade",
        "TS7", "TS750", "RIGIDEZ - D",
    ]
    for col in cols_num:
        if col in df.columns:
            df[col] = _to_float(df[col])

    return df.dropna(subset=["Num.Rastreamento", "Data"]).reset_index(drop=True)


def _carregar_processo(path: Path) -> "pd.DataFrame":
    raw = pd.read_csv(
        path, encoding="latin-1", sep=";", header=None, low_memory=False
    )
    param_names = raw.iloc[1].tolist()
    col_names = ["timestamp"] + [
        str(v).strip() if (pd.notna(v) and str(v).strip()) else f"_col{i}"
        for i, v in enumerate(param_names[1:], start=1)
    ]
    df = raw.iloc[3:].copy().reset_index(drop=True)
    df.columns = col_names[: len(df.columns)]

    df["timestamp"] = df["timestamp"].apply(_parse_ts_pt)
    df = df.dropna(subset=["timestamp"])

    for col in df.columns[1:]:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", ".").str.strip(), errors="coerce"
        )

    for col in _COLS_YANKEE:
        if col in df.columns:
            df[col] = df[col].replace(0, np.nan)

    return df.sort_values("timestamp").reset_index(drop=True)


def carregar_bases(
    path_prod: "Path | str | None" = None,
    path_qual: "Path | str | None" = None,
    path_proc: "Path | str | None" = None,
) -> dict:
    path_prod = Path(path_prod) if path_prod else _mais_recente(_DADOS / "producao", "*.xlsx")
    path_qual = Path(path_qual) if path_qual else _mais_recente(_DADOS / "qualidade", "*.xlsx")
    path_proc = Path(path_proc) if path_proc else _mais_recente(_DADOS / "processo", "*.csv")

    erros = []
    if not path_prod or not path_prod.exists():
        erros.append("Boletim de Produção não encontrado em dados/producao/")
    if not path_qual or not path_qual.exists():
        erros.append("Boletim de Qualidade não encontrado em dados/qualidade/")
    if not path_proc or not path_proc.exists():
        erros.append("Histórico de Processo não encontrado em dados/processo/")
    if erros:
        raise FileNotFoundError(" | ".join(erros))

    return {
        "producao":  _carregar_producao(path_prod),
        "qualidade": _carregar_qualidade(path_qual),
        "processo":  _carregar_processo(path_proc),
        "fonte":     "local",
        "arquivos": {
            "producao": path_prod.name,
            "qualidade": path_qual.name,
            "processo":  path_proc.name,
        },
    }


def carregar_bases_db(dias: int = 90) -> dict:
    """Carrega as três fontes do banco Neon. Levanta exceção se o banco não estiver acessível."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from db import carregar_qualidade_db, carregar_producao_db, carregar_processo_db

    df_qual = carregar_qualidade_db(dias=dias)
    df_prod = carregar_producao_db(dias=dias)
    df_proc = carregar_processo_db(dias=dias)

    if df_qual.empty:
        raise ValueError(f"Sem dados de qualidade no banco para os últimos {dias} dias.")
    if df_prod.empty:
        raise ValueError("Sem dados de produção no banco.")
    if df_proc.empty:
        raise ValueError("Sem dados de processo no banco.")

    # Normaliza timestamps para tz-naive (processo já é tz-naive após carregar_processo_db)
    for df in (df_qual, df_prod):
        if df["Data"].dt.tz is not None:
            df["Data"] = df["Data"].dt.tz_localize(None)

    return {
        "producao":  df_prod,
        "qualidade": df_qual,
        "processo":  df_proc,
        "fonte":     "db",
        "arquivos":  {"producao": "Neon DB", "qualidade": "Neon DB", "processo": "Neon DB"},
    }


# ── Join das três fontes ──────────────────────────────────────────────────────

def cruzar_fontes(bases: dict) -> "pd.DataFrame":
    """
    1. Inner join Produção × Qualidade.
       - fonte "db": join em Unidade (chave do Neon)
       - fonte "local": join em Track Num = Num.Rastreamento (arquivos Excel)
    2. merge_asof backward com Processo no timestamp da bobina.
    """
    fonte = bases.get("fonte", "local")
    df_prod = bases["producao"].rename(columns={"Data": "timestamp_prod"}).copy()
    df_qual = bases["qualidade"].rename(columns={"Data": "timestamp_qual"}).copy()
    df_proc = bases["processo"].copy()

    if fonte == "db":
        base = pd.merge(
            df_prod, df_qual, on="Unidade",
            suffixes=("_prod", "_qual"), how="inner",
        )
    else:
        df_qual = df_qual.rename(columns={"Num.Rastreamento": "Track Num"})
        base = pd.merge(
            df_prod, df_qual, on="Track Num",
            suffixes=("_prod", "_qual"), how="inner",
        )
    if base.empty:
        return base

    cols_proc = ["timestamp"] + [
        c for c in (_VARS_PROCESSO + _COLS_YANKEE) if c in df_proc.columns
    ]
    df_proc_sel = df_proc[cols_proc].sort_values("timestamp")
    base = base.sort_values("timestamp_prod").reset_index(drop=True)

    return pd.merge_asof(
        base,
        df_proc_sel.rename(columns={"timestamp": "ts_proc"}),
        left_on="timestamp_prod",
        right_on="ts_proc",
        direction="backward",
    )


# ── Detecção de mudança de regime ─────────────────────────────────────────────

def detectar_mudanca_regime(
    df_proc: "pd.DataFrame",
    col: str = "PRENSA",
    janela_h: int = 4,
    limiar_sigma: float = 3.0,
) -> list[dict[str, Any]]:
    """
    Critério objetivo: variação entre médias de janelas consecutivas de
    janela_h horas acima de limiar_sigma × desvio padrão das médias de janela.
    Lista vazia = sem evidência objetiva — segmentação não é forçada.
    """
    if col not in df_proc.columns:
        return []
    serie = df_proc.set_index("timestamp")[col].dropna()
    if serie.empty:
        return []
    medias = serie.resample(f"{janela_h}h").mean().dropna()
    if len(medias) < 3:
        return []
    std_global = float(medias.std())
    if std_global == 0:
        return []

    mudancas = []
    for ts, delta in medias.diff().dropna().items():
        ratio = abs(delta) / std_global
        if ratio > limiar_sigma:
            mudancas.append({
                "timestamp": ts,
                "coluna": col,
                "delta": round(float(delta), 3),
                "sigma_ratio": round(float(ratio), 2),
                "direcao": "subida" if delta > 0 else "queda",
                "criterio": f"Δ = {delta:+.2f} ({ratio:.1f}σ) em janela de {janela_h}h",
            })
    return sorted(mudancas, key=lambda x: x["timestamp"])


def segmentar_regimes(
    base_bobina: "pd.DataFrame",
    mudancas: list[dict],
    col_regime: str = "PRENSA",
) -> tuple["pd.DataFrame", dict[str, str]]:
    df = base_bobina.copy()
    avisos: dict[str, str] = {}

    if not mudancas:
        df["regime"] = "Período único"
        avisos["geral"] = (
            f"Sem mudança de regime detectada com critério objetivo "
            f"(limiar 3σ em janelas de 4h em {col_regime}) — análise sem segmentação."
        )
        return df, avisos

    breakpoints = sorted(m["timestamp"] for m in mudancas)

    def _regime(ts: "pd.Timestamp") -> str:
        for i, bp in enumerate(breakpoints):
            if ts < bp:
                return f"Regime {i + 1} (antes de {bp.strftime('%d/%m %Hh')})"
        return f"Regime {len(breakpoints) + 1} (após {breakpoints[-1].strftime('%d/%m %Hh')})"

    df["regime"] = df["timestamp_prod"].apply(_regime)
    for regime, n in df["regime"].value_counts().items():
        if n < 10:
            avisos[regime] = (
                f"Baixa confiança: apenas {n} bobina(s) neste regime "
                f"— leitura estatística não confiável."
            )
    return df, avisos


# ── Correlação com magnitude em unidade real ──────────────────────────────────

def _correlacao_par(
    x: "pd.Series", y: "pd.Series",
    nome_x: str, unidade_alvo: str,
) -> dict | None:
    mask = x.notna() & y.notna()
    n = int(mask.sum())
    if n < 5:
        return None
    xv, yv = x[mask].to_numpy(float), y[mask].to_numpy(float)
    r = float(np.corrcoef(xv, yv)[0, 1])
    if not np.isfinite(r):
        return None
    slope = float(np.polyfit(xv, yv, 1)[0])
    x_std = float(np.std(xv))
    abs_r = abs(r)

    forca = (
        "forte" if abs_r >= 0.70 else
        "moderada" if abs_r >= 0.50 else
        "fraca" if abs_r >= 0.35 else
        "ausente"
    )
    return {
        "variavel": nome_x,
        "n": n,
        "direcao": "positiva" if r > 0 else "negativa",
        "forca": forca,
        "sustenta_hipotese": abs_r >= 0.35,
        "magnitude_1u": (
            f"{slope:+.5f} {unidade_alvo} por unidade de {nome_x}"
        ),
        "magnitude_1std": (
            f"{slope * x_std:+.4f} {unidade_alvo} por 1σ ({x_std:.2f}) de {nome_x}"
        ),
        "_r": round(r, 4),
        "_slope": round(slope, 6),
    }


def calcular_correlacoes(
    base_bobina: "pd.DataFrame",
    variavel_alvo: str,
    regime: str | None = None,
) -> list[dict]:
    """
    Correlaciona variavel_alvo com variáveis de processo, produção e demais
    variáveis de qualidade (excluindo a própria variavel_alvo dos preditores).
    """
    df = base_bobina if regime is None else base_bobina[
        base_bobina.get("regime", pd.Series(dtype=str)) == regime
    ]
    if variavel_alvo not in df.columns:
        return []

    y = df[variavel_alvo]
    unidade = _unidade(variavel_alvo)
    resultados = []

    # Preditoras de qualidade = todas exceto a variável-alvo
    vars_qual_pred = [v for v in _VARS_QUALIDADE_TODAS if v != variavel_alvo]

    for nome, origem in (
        [(v, "processo") for v in _VARS_PROCESSO]
        + [(v, "qualidade") for v in vars_qual_pred]
        + [(v, "producao") for v in _VARS_PRODUCAO_EXTRA]
    ):
        if nome not in df.columns:
            continue
        r = _correlacao_par(df[nome], y, nome_x=nome, unidade_alvo=unidade)
        if r is not None:
            r["origem"] = origem
            resultados.append(r)

    return sorted(resultados, key=lambda x: (not x["sustenta_hipotese"], -abs(x["_r"])))


# ── Casos fora de especificação ───────────────────────────────────────────────

def casos_fora_spec(
    base_bobina: "pd.DataFrame",
    variavel_alvo: str,
) -> dict:
    """
    Status J (Fora de Especificação) e C (Refugo) separados.
    Para cada bobina: horário, regime, valor da variável-alvo e contexto de processo.
    Status é por bobina inteira (decisão integrada do sistema de qualidade),
    não específico por variável — o que muda é qual valor é reportado.
    """
    col_status = "Status_qual" if "Status_qual" in base_bobina.columns else "Status"
    resultado: dict[str, Any] = {}

    for status in ("J", "C"):
        subset = base_bobina[base_bobina[col_status] == status].copy()

        if subset.empty:
            resultado[status] = {
                "n": 0, "bobinas": [],
                "padrao_identificado": None,
                "nota": "Nenhuma ocorrência neste período.",
            }
            continue

        bobinas = []
        for _, row in subset.iterrows():
            # DB usa Unidade como ID de bobina; local usa Track Num
            bobina_id = row.get("Unidade") or str(int(row.get("Track Num", 0) or 0))
            bobinas.append({
                "bobina_id": str(bobina_id),
                "timestamp": str(row.get("timestamp_prod", ""))[:16],
                "regime": row.get("regime", "N/A"),
                "variavel_alvo": variavel_alvo,
                "valor": _safe_float(row, variavel_alvo),
                "unidade": _unidade(variavel_alvo),
                "codigo_refugo": str(row.get("Código Refugo", "")).strip(),
                "familia": str(row.get("Familia Fabricada", "")),
                "turma": str(row.get(
                    "Turma_prod", row.get("Turma_qual", row.get("Turma", ""))
                )),
                "contexto": {
                    "prensa_kn_m":       _safe_float(row, "PRENSA"),
                    "umidade_qcs_pct":   _safe_float(row, "umidade QCS"),
                    "potencia_ref01_kw": _safe_float(row, "Potência Ref. 01"),
                    "potencia_ref02_kw": _safe_float(row, "Potência Ref. 02"),
                    "velocidade_m_min":  _safe_float(row, "Velocidade"),
                    "capota_ls_c":       _safe_float(row, "Temperatura Capota LS"),
                    "redry_pct":         _safe_float(row, "Redry"),
                },
            })

        padrao, nota = _detectar_padrao_spec(subset, len(bobinas))
        resultado[status] = {
            "n": len(bobinas), "bobinas": bobinas,
            "padrao_identificado": padrao, "nota": nota,
        }

    return resultado


def _detectar_padrao_spec(subset: "pd.DataFrame", n: int) -> tuple[bool, str]:
    if n < 3:
        return False, (
            f"Amostra muito pequena ({n} bobina(s)) — "
            "sem base estatística para identificar padrão."
        )
    if "PRENSA" not in subset.columns:
        return False, "PRENSA não disponível para análise de padrão."
    prensas = subset["PRENSA"].dropna()
    if prensas.empty or prensas.mean() == 0:
        return False, "Dados de PRENSA ausentes ou zerados para estas bobinas."
    cv = prensas.std() / prensas.mean()
    if cv < 0.15:
        return True, (
            f"PRENSA consistente em {prensas.mean():.1f} ± {prensas.std():.1f} kN/m "
            f"(CV = {cv * 100:.1f}%) nas {n} ocorrências."
        )
    return False, (
        f"Ocorrências dispersas: PRENSA variou de {prensas.min():.1f} "
        f"a {prensas.max():.1f} kN/m (CV = {cv * 100:.1f}%) "
        "— sem padrão claro identificável nos dados disponíveis."
    )


# ── Ponto de entrada único ────────────────────────────────────────────────────

def resumo_qualidade(
    variavel_alvo: str = "Espessura",
    dias: int = 90,
    path_prod: "Path | str | None" = None,
    path_qual: "Path | str | None" = None,
    path_proc: "Path | str | None" = None,
) -> dict:
    """
    Ponto de entrada único para o dashboard.
    Tenta carregar do banco Neon primeiro; cai para arquivos locais se o banco
    não estiver disponível. Retorna sempre dict com 'ok': bool.
    """
    bases = None
    fonte = "db"
    try:
        bases = carregar_bases_db(dias=dias)
    except Exception:
        fonte = "local"
        try:
            bases = carregar_bases(path_prod, path_qual, path_proc)
        except Exception as exc_local:
            return {"ok": False, "dados_teste": True, "fonte": "none",
                    "erro": str(exc_local)}

    try:
        base = cruzar_fontes(bases)

        if base.empty:
            return _erro("Nenhuma bobina resultou do cruzamento das três fontes.", bases)
        if variavel_alvo not in base.columns:
            disponiveis = [c for c in _VARS_QUALIDADE_TODAS if c in base.columns]
            return _erro(
                f"Variável '{variavel_alvo}' não disponível nesta fonte de dados. "
                f"Disponíveis: {', '.join(disponiveis) or 'nenhuma'}.",
                bases,
            )

        mudancas = detectar_mudanca_regime(bases["processo"], col="PRENSA")
        base, avisos_regime = segmentar_regimes(base, mudancas, col_regime="PRENSA")

        corrs_globais = calcular_correlacoes(base, variavel_alvo)
        corrs_por_regime: dict[str, list] = {}
        if mudancas:
            for regime in base["regime"].unique():
                corrs_por_regime[regime] = calcular_correlacoes(
                    base, variavel_alvo, regime=regime
                )

        casos = casos_fora_spec(base, variavel_alvo)

        val = base[variavel_alvo].dropna()
        unidade = _unidade(variavel_alvo)

        return {
            "ok": True,
            "fonte": fonte,
            "dados_teste": fonte == "local",
            "variavel_alvo": variavel_alvo,
            "unidade": unidade,
            "arquivos": bases["arquivos"],
            "periodo": {
                "ini": str(base["timestamp_prod"].min())[:16],
                "fim": str(base["timestamp_prod"].max())[:16],
            },
            "n_bobinas": len(base),
            "var_resumo": {
                "media":  round(float(val.mean()), 4),
                "std":    round(float(val.std()),  4),
                "min":    round(float(val.min()),  4),
                "max":    round(float(val.max()),  4),
                "cv_pct": round(float(val.std() / val.mean() * 100), 2),
                "unidade": unidade,
            },
            "mudancas_regime": [
                {**m, "timestamp": str(m["timestamp"])[:16]} for m in mudancas
            ],
            "avisos_regime": avisos_regime,
            "correlacoes_globais": corrs_globais,
            "correlacoes_por_regime": corrs_por_regime,
            "casos_fora_spec": casos,
        }

    except Exception as exc:
        return {"ok": False, "dados_teste": fonte == "local", "fonte": fonte,
                "erro": str(exc)}


def _erro(msg: str, bases: dict | None = None) -> dict:
    fonte = bases.get("fonte", "local") if bases else "none"
    return {
        "ok": False, "dados_teste": fonte == "local", "fonte": fonte,
        "erro": msg,
        "arquivos": bases["arquivos"] if bases else {},
    }
