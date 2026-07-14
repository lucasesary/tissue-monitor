"""
Análise de espessura: cruzamento Produção × Qualidade × Processo (OPC UA).

Toda a lógica de negócio fica aqui. O dashboard importa apenas
`resumo_espessura()` e exibe o resultado — sem math ou join embutido
na camada de visualização.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Raiz do projeto: analisar_tissue.py/ (um nível acima de services/)
_DADOS = Path(__file__).parent.parent / "dados"

_MESES_PT: dict[str, str] = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}

# Variáveis de processo para correlacionar com espessura
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

_VARS_QUALIDADE_EXTRA = ["Umidade", "UmidadeQCS", "Tração Longitudinal", "Tração Transversal"]
_VARS_PRODUCAO_EXTRA = ["Velocidade", "Gr/m2"]

# Colunas de temperatura Yankee no CSV de processo: zero = dado ausente, não valor real
_COLS_YANKEE = [
    "Temperatura Superfície Yankee LA",
    "Temperatura Superfície Yankee LC",
]


# ── Helpers internos ──────────────────────────────────────────────────────────

def _parse_ts_pt(v: str) -> "pd.Timestamp":
    """'01-mai-26 00:00:00' → Timestamp. Retorna NaT se inválido."""
    m = re.match(r"(\d+)-(\w+)-(\d+)\s+(\d+:\d+:\d+)", str(v).strip())
    if not m:
        return pd.NaT
    d, mes, y, t = m.groups()
    y = "20" + y if len(y) == 2 else y
    return pd.to_datetime(
        f"{y}-{_MESES_PT.get(mes.lower(), mes)}-{d} {t}", errors="coerce"
    )


def _to_float(s: "pd.Series") -> "pd.Series":
    """String com vírgula decimal → float. Strings inválidas → NaN."""
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


# ── Carregamento das fontes ───────────────────────────────────────────────────

def _carregar_producao(path: Path) -> "pd.DataFrame":
    df = pd.read_excel(path, sheet_name=0)
    df["Data"] = pd.to_datetime(df["Data"], errors="coerce")
    df["Track Num"] = pd.to_numeric(df["Track Num"], errors="coerce")
    return df.dropna(subset=["Track Num", "Data"]).reset_index(drop=True)


def _carregar_qualidade(path: Path) -> "pd.DataFrame":
    raw = pd.read_excel(path, sheet_name=0, header=0)
    # Linha 0 do DataFrame contém os nomes reais de coluna (o Excel tem
    # um cabeçalho genérico como primeira linha do arquivo)
    real_cols = [
        str(c).strip() if pd.notna(c) else f"_col{i}"
        for i, c in enumerate(raw.iloc[0])
    ]
    raw.columns = real_cols
    df = raw.iloc[1:].reset_index(drop=True)

    df["Data"] = pd.to_datetime(df["Data"], errors="coerce")
    df["Num.Rastreamento"] = pd.to_numeric(df["Num.Rastreamento"], errors="coerce")

    cols_num = [
        "Espessura", "Espessura A", "Espessura C", "Espessura M",
        "Umidade", "Umidade A", "Umidade C", "Umidade M", "UmidadeQCS",
        "Gramatura", "Tração Longitudinal", "Tração Transversal",
        "Bulk", "Handfeel", "Maciez TSA",
    ]
    for col in cols_num:
        if col in df.columns:
            df[col] = _to_float(df[col])

    return df.dropna(subset=["Num.Rastreamento", "Data"]).reset_index(drop=True)


def _carregar_processo(path: Path) -> "pd.DataFrame":
    raw = pd.read_csv(
        path, encoding="latin-1", sep=";", header=None, low_memory=False
    )
    # Linha 0: estatísticas de exportação (ignorar)
    # Linha 1: nomes dos parâmetros (col 0 vazio → será "timestamp")
    # Linha 2: tags OPC UA (col 0 = "data")
    # Linha 3+: dados reais
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

    # Zero = dado ausente em temperatura Yankee (máquina opera ~95 °C na superfície)
    for col in _COLS_YANKEE:
        if col in df.columns:
            df[col] = df[col].replace(0, np.nan)

    return df.sort_values("timestamp").reset_index(drop=True)


def carregar_bases(
    path_prod: "Path | str | None" = None,
    path_qual: "Path | str | None" = None,
    path_proc: "Path | str | None" = None,
) -> dict:
    """
    Carrega as três fontes. Sem argumentos, usa o arquivo mais recente
    em cada subdiretório de dados/.
    """
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
        "arquivos": {
            "producao": path_prod.name,
            "qualidade": path_qual.name,
            "processo":  path_proc.name,
        },
    }


# ── Join das três fontes ──────────────────────────────────────────────────────

def cruzar_fontes(bases: dict) -> "pd.DataFrame":
    """
    1. Inner join Produção × Qualidade por Track Num = Num.Rastreamento (igualdade inteira).
    2. merge_asof backward com Processo no timestamp da bobina.
       Regra documentada: usa o registro de processo imediatamente anterior
       ao horário da bobina, capturando o estado real da máquina durante
       sua produção — nunca arredonda para hora cheia.
    """
    df_prod = bases["producao"].rename(columns={"Data": "timestamp_prod"}).copy()
    df_qual = bases["qualidade"].rename(
        columns={"Num.Rastreamento": "Track Num", "Data": "timestamp_qual"}
    ).copy()
    df_proc = bases["processo"].copy()

    base = pd.merge(
        df_prod, df_qual,
        on="Track Num",
        suffixes=("_prod", "_qual"),
        how="inner",
    )
    if base.empty:
        return base

    # Selecionar colunas de processo úteis para não inflar o dataframe
    cols_proc = ["timestamp"] + [
        c for c in (_VARS_PROCESSO + _COLS_YANKEE)
        if c in df_proc.columns
    ]
    df_proc_sel = df_proc[cols_proc].sort_values("timestamp")

    base = base.sort_values("timestamp_prod").reset_index(drop=True)

    merged = pd.merge_asof(
        base,
        df_proc_sel.rename(columns={"timestamp": "ts_proc"}),
        left_on="timestamp_prod",
        right_on="ts_proc",
        direction="backward",
    )
    return merged


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

    Retorna lista com timestamp exato, coluna, delta em unidade real e ratio em σ.
    Lista vazia = sem evidência objetiva de mudança — segmentação não é forçada.
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
                "criterio": (
                    f"Δ = {delta:+.2f} unidades ({ratio:.1f}σ) "
                    f"em janela de {janela_h}h"
                ),
            })

    return sorted(mudancas, key=lambda x: x["timestamp"])


def segmentar_regimes(
    base_bobina: "pd.DataFrame",
    mudancas: list[dict],
) -> tuple["pd.DataFrame", dict[str, str]]:
    """
    Adiciona coluna 'regime' ao dataframe de bobinas.
    Só segmenta se houver mudanças com critério objetivo confirmado.
    Avisa (campo 'avisos') quando n < 10 bobinas em qualquer regime.
    """
    df = base_bobina.copy()
    avisos: dict[str, str] = {}

    if not mudancas:
        df["regime"] = "Período único"
        avisos["geral"] = (
            "Sem mudança de regime detectada com critério objetivo "
            f"(limiar 3σ em janelas de 4h na PRENSA) — análise sem segmentação."
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
    x: "pd.Series", y: "pd.Series", nome_x: str
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

    if abs_r >= 0.70:
        forca = "forte"
    elif abs_r >= 0.50:
        forca = "moderada"
    elif abs_r >= 0.35:
        forca = "fraca"
    else:
        forca = "ausente"

    return {
        "variavel": nome_x,
        "n": n,
        "direcao": "positiva" if r > 0 else "negativa",
        "forca": forca,
        "sustenta_hipotese": abs_r >= 0.35,
        # Magnitude expressa em unidade real, sem r= exposto
        "magnitude_1u": f"{slope:+.5f} mm por unidade de {nome_x}",
        "magnitude_1std": (
            f"{slope * x_std:+.4f} mm por 1σ ({x_std:.2f}) de {nome_x}"
        ),
        "_r": round(r, 4),      # interno — não expor na UI principal
        "_slope": round(slope, 6),
    }


def calcular_correlacoes_espessura(
    base_bobina: "pd.DataFrame",
    regime: str | None = None,
) -> list[dict]:
    """
    Correlaciona Espessura com variáveis de processo, qualidade extra e produção.
    Se regime fornecido, filtra o dataframe antes de calcular.
    Retorna lista ordenada: sustenta hipótese primeiro, depois por |r| decrescente.
    """
    df = base_bobina if regime is None else base_bobina[
        base_bobina.get("regime", pd.Series("")) == regime
    ]
    y = df["Espessura"]
    resultados = []

    for nome, origem in (
        [(v, "processo") for v in _VARS_PROCESSO]
        + [(v, "qualidade") for v in _VARS_QUALIDADE_EXTRA]
        + [(v, "producao") for v in _VARS_PRODUCAO_EXTRA]
    ):
        if nome not in df.columns:
            continue
        r = _correlacao_par(df[nome], y, nome_x=nome)
        if r is not None:
            r["origem"] = origem
            resultados.append(r)

    return sorted(resultados, key=lambda x: (not x["sustenta_hipotese"], -abs(x["_r"])))


# ── Casos fora de especificação ───────────────────────────────────────────────

def casos_fora_spec(base_bobina: "pd.DataFrame") -> dict:
    """
    Filtra bobinas por status separando:
      J = Fora de Especificação (pode coexistir com aprovação condicional)
      C = Refugo (descarte definitivo)
    Para cada categoria: horário, regime, contexto de processo no momento exato.
    Reporta padrão apenas se houver evidência objetiva — nunca força narrativa.
    """
    col_status = "Status_qual" if "Status_qual" in base_bobina.columns else "Status"
    resultado: dict[str, Any] = {}

    for status in ("J", "C"):
        subset = base_bobina[base_bobina[col_status] == status].copy()

        if subset.empty:
            resultado[status] = {
                "n": 0,
                "bobinas": [],
                "padrao_identificado": None,
                "nota": "Nenhuma ocorrência neste período.",
            }
            continue

        bobinas = []
        for _, row in subset.iterrows():
            bobinas.append({
                "track_num": int(row.get("Track Num", 0)),
                "timestamp": str(row.get("timestamp_prod", ""))[:16],
                "regime": row.get("regime", "N/A"),
                "espessura_mm": _safe_float(row, "Espessura"),
                "codigo_refugo": str(row.get("Código Refugo", "")).strip(),
                "familia": str(row.get("Familia Fabricada", "")),
                "turma": str(row.get("Turma_prod", row.get("Turma_qual", row.get("Turma", "")))),
                "contexto": {
                    "prensa_kn_m":        _safe_float(row, "PRENSA"),
                    "umidade_qcs_pct":    _safe_float(row, "umidade QCS"),
                    "potencia_ref01_kw":  _safe_float(row, "Potência Ref. 01"),
                    "potencia_ref02_kw":  _safe_float(row, "Potência Ref. 02"),
                    "velocidade_m_min":   _safe_float(row, "Velocidade"),
                    "capota_ls_c":        _safe_float(row, "Temperatura Capota LS"),
                    "redry_pct":          _safe_float(row, "Redry"),
                },
            })

        padrao, nota = _detectar_padrao_spec(subset, len(bobinas))
        resultado[status] = {
            "n": len(bobinas),
            "bobinas": bobinas,
            "padrao_identificado": padrao,
            "nota": nota,
        }

    return resultado


def _detectar_padrao_spec(subset: "pd.DataFrame", n: int) -> tuple[bool, str]:
    """Padrão identificado apenas se CV% de PRENSA < 15% com n ≥ 3."""
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

def resumo_espessura(
    path_prod: "Path | str | None" = None,
    path_qual: "Path | str | None" = None,
    path_proc: "Path | str | None" = None,
) -> dict:
    """
    Ponto de entrada único para o dashboard.
    Retorna sempre um dict com 'dados_teste': True e 'ok': bool.
    Em caso de erro, 'ok' é False e 'erro' contém a mensagem.
    """
    try:
        bases = carregar_bases(path_prod, path_qual, path_proc)
        base = cruzar_fontes(bases)

        if base.empty:
            return _resultado_erro("Nenhuma bobina resultou do cruzamento das três fontes.", bases)

        # Detecção de regime na PRENSA (principal variável com efeito em espessura)
        mudancas = detectar_mudanca_regime(bases["processo"], col="PRENSA")
        base, avisos_regime = segmentar_regimes(base, mudancas)

        # Correlações globais
        corrs_globais = calcular_correlacoes_espessura(base)

        # Correlações por regime (só se houver segmentação real)
        corrs_por_regime: dict[str, list] = {}
        if mudancas:
            for regime in base["regime"].unique():
                corrs_por_regime[regime] = calcular_correlacoes_espessura(base, regime=regime)

        casos = casos_fora_spec(base)

        esp = base["Espessura"].dropna()
        return {
            "dados_teste": True,
            "ok": True,
            "arquivos": bases["arquivos"],
            "periodo": {
                "ini": str(base["timestamp_prod"].min())[:16],
                "fim": str(base["timestamp_prod"].max())[:16],
            },
            "n_bobinas": len(base),
            "espessura_resumo": {
                "media_mm": round(float(esp.mean()), 4),
                "std_mm":   round(float(esp.std()),  4),
                "min_mm":   round(float(esp.min()),  4),
                "max_mm":   round(float(esp.max()),  4),
                "cv_pct":   round(float(esp.std() / esp.mean() * 100), 2),
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
        return {"dados_teste": True, "ok": False, "erro": str(exc)}


def _resultado_erro(msg: str, bases: dict | None = None) -> dict:
    return {
        "dados_teste": True,
        "ok": False,
        "erro": msg,
        "arquivos": bases["arquivos"] if bases else {},
    }
