#!/usr/bin/env python3
"""
analisar.py — Motor de análise para dados de processo Tissue (OPC UA).

Uso:
  python analisar.py [--arquivo ARQUIVO] [--saida PASTA] [--desvios N]
                     [--sem-graficos] [--filtrar-parada]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd

# ── constantes ────────────────────────────────────────────────────────────

NOMES_ARQUIVO = [
    "Parametros do projeto.xlsx",
    "parametros do projeto.xlsx",
    "Parametros_do_projeto.xlsx",
    "parametros_do_projeto.xlsx",
]

VELOCIDADE_PARADA_THRESHOLD = 50.0   # m/min abaixo = máquina parada

PARAMS_NEGATIVOS_OK = {
    "Diferencial", "Bulbo", "b",
}

SAIDA_PADRAO = Path("resultado")


# ── parsing do Excel ──────────────────────────────────────────────────────

_MESES_PT = {
    "jan": "01", "fev": "02", "mar": "03", "abr": "04",
    "mai": "05", "jun": "06", "jul": "07", "ago": "08",
    "set": "09", "out": "10", "nov": "11", "dez": "12",
}


def _normalizar_data_pt(s: str) -> str:
    """Converte '01-mai-26 00:00:00' → '01/05/2026 00:00:00'."""
    partes = s.strip().split("-")
    if len(partes) >= 3 and partes[1].lower() in _MESES_PT:
        dia = partes[0].zfill(2)
        mes = _MESES_PT[partes[1].lower()]
        resto = partes[2]  # '26 00:00:00'
        ano_hora = resto.split(" ", 1)
        ano = "20" + ano_hora[0].zfill(2) if len(ano_hora[0]) <= 2 else ano_hora[0]
        hora = ano_hora[1] if len(ano_hora) > 1 else "00:00:00"
        return f"{dia}/{mes}/{ano} {hora}"
    return s


def excel_para_datetime(val) -> datetime | None:
    """Converte valor de célula Excel para datetime."""
    if pd.isna(val):
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, (int, float)):
        try:
            return pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(val))
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        # formato com mês em português: '01-mai-26 00:00:00'
        if "-" in s and any(m in s.lower() for m in _MESES_PT):
            s = _normalizar_data_pt(s)
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _converter_numero(val) -> float | None:
    if pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def carregar_dados(caminho: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    Lê o Excel OPC UA e retorna (dados, tags, medias_exportadas).

    Estrutura esperada:
      Linha 0 — "Média:" seguida das médias calculadas pelo sistema
      Linha 1 — nomes dos parâmetros
      Linha 2 — tags OPC UA
      Linhas 3+ — dados de processo (timestamp, valores...)

    Retorna:
      dados   — DataFrame com colunas [timestamp, param1, param2, ...]
      tags    — DataFrame com tag OPC UA por parâmetro
      medias  — Series com a média exportada por parâmetro
    """
    caminho = Path(caminho)
    if caminho.suffix.lower() == ".csv":
        raw = pd.read_csv(caminho, sep=";", header=None, encoding="latin1",
                          low_memory=False)
    else:
        raw = pd.read_excel(caminho, header=None)

    # detecta linha de cabeçalho buscando "Média:" na coluna 0
    linha_media = None
    linha_nomes = None
    for i in range(min(10, len(raw))):
        v = str(raw.iloc[i, 0]).strip()
        if "dia" in v.lower():
            linha_media = i
            linha_nomes = i + 1
            break

    if linha_nomes is None:
        raise ValueError(f"Não encontrei a linha de cabeçalho no arquivo {caminho.name}")

    linha_tags  = linha_nomes + 1
    linha_dados = linha_tags + 1

    nomes = [str(v).strip() for v in raw.iloc[linha_nomes]]
    nomes[0] = "timestamp"

    # tags OPC UA
    tags_row = raw.iloc[linha_tags].tolist()
    tags = pd.DataFrame({
        "parametro": nomes[1:],
        "tag": [str(t).strip() if pd.notna(t) else "" for t in tags_row[1:]],
    })

    # médias exportadas (linha 0)
    medias_vals = raw.iloc[linha_media, 1:].tolist()
    medias = pd.Series(
        [_converter_numero(v) for v in medias_vals],
        index=nomes[1:],
        name="media_exportada",
    )

    # dados
    df_raw = raw.iloc[linha_dados:].copy()
    df_raw.columns = nomes + list(range(len(nomes), len(df_raw.columns)))
    df_raw = df_raw.iloc[:, :len(nomes)].copy()
    df_raw.columns = nomes

    # converte timestamp
    df_raw["timestamp"] = df_raw["timestamp"].apply(excel_para_datetime)
    df_raw = df_raw.dropna(subset=["timestamp"]).copy()
    df_raw["timestamp"] = pd.to_datetime(df_raw["timestamp"])
    df_raw = df_raw.sort_values("timestamp").reset_index(drop=True)

    # converte colunas numéricas
    for col in nomes[1:]:
        if col in df_raw.columns:
            df_raw[col] = df_raw[col].apply(_converter_numero)

    return df_raw, tags, medias


# ── paradas de máquina ────────────────────────────────────────────────────

def _col_velocidade(dados: pd.DataFrame) -> str | None:
    for col in dados.columns:
        if "elocidade" in col and "MP" in col:
            return col
    for col in dados.columns:
        if "elocidade" in col:
            return col
    return None


def mask_parada(dados: pd.DataFrame) -> pd.Series:
    """Retorna boolean Series True onde a máquina está parada."""
    col = _col_velocidade(dados)
    if col is None:
        return pd.Series(False, index=dados.index)
    return dados[col].fillna(0) < VELOCIDADE_PARADA_THRESHOLD


def detectar_paradas(dados: pd.DataFrame) -> pd.DataFrame:
    """
    Retorna DataFrame com períodos de parada:
      inicio, fim, duracao_min
    """
    mask = mask_parada(dados)
    if not mask.any():
        return pd.DataFrame(columns=["inicio", "fim", "duracao_min"])

    eventos = []
    em_parada = False
    inicio = None

    for i, (ts, parado) in enumerate(zip(dados["timestamp"], mask)):
        if parado and not em_parada:
            em_parada = True
            inicio = ts
        elif not parado and em_parada:
            fim = dados["timestamp"].iloc[i - 1]
            dur = (fim - inicio).total_seconds() / 60
            eventos.append({"inicio": inicio, "fim": fim, "duracao_min": round(dur, 2)})
            em_parada = False

    if em_parada and inicio is not None:
        fim = dados["timestamp"].iloc[-1]
        dur = (fim - inicio).total_seconds() / 60
        eventos.append({"inicio": inicio, "fim": fim, "duracao_min": round(dur, 2)})

    return pd.DataFrame(eventos)


# ── estatísticas ──────────────────────────────────────────────────────────

def estatisticas(dados: pd.DataFrame,
                  filtrar_parada: bool = False) -> pd.DataFrame:
    """
    Retorna DataFrame de estatísticas por parâmetro.
    Colunas: media, std, cv_pct, min, p5, p25, p50, p75, p95, max,
             nulos_pct, negativos, media_exportada (se disponível)
    """
    df = dados.copy()
    if filtrar_parada:
        df = df[~mask_parada(df)]

    rows = []
    for col in df.columns[1:]:
        s = df[col].dropna()
        if s.empty:
            rows.append({"parametro": col, "nulos_pct": 100.0})
            continue
        media = s.mean()
        std   = s.std()
        row = {
            "parametro":  col,
            "media":      round(media, 4),
            "std":        round(std, 4),
            "cv_pct":     round(abs(std / media) * 100, 2) if media != 0 else None,
            "min":        round(s.min(), 4),
            "p5":         round(s.quantile(0.05), 4),
            "p25":        round(s.quantile(0.25), 4),
            "p50":        round(s.quantile(0.50), 4),
            "p75":        round(s.quantile(0.75), 4),
            "p95":        round(s.quantile(0.95), 4),
            "max":        round(s.max(), 4),
            "nulos_pct":  round((df[col].isna().sum() / len(df)) * 100, 1),
            "negativos":  int((s < 0).sum()) if col not in PARAMS_NEGATIVOS_OK else 0,
        }
        rows.append(row)

    return pd.DataFrame(rows).set_index("parametro")


# ── outliers ──────────────────────────────────────────────────────────────

def detectar_outliers(dados: pd.DataFrame,
                       n_desvios: float = 3.0,
                       filtrar_parada: bool = False) -> pd.DataFrame:
    """
    Detecta outliers por ±n_desvios σ.
    Adiciona coluna `em_parada` para distinguir anomalia de processo vs parada.
    """
    parada_mask = mask_parada(dados)
    registros = []

    for col in dados.columns[1:]:
        s = dados[col].dropna()
        if len(s) < 10:
            continue
        media = s.mean()
        std   = s.std()
        if std == 0:
            continue

        for idx in s.index:
            val  = s[idx]
            dev  = abs(val - media) / std
            if dev >= n_desvios:
                registros.append({
                    "timestamp":        dados.loc[idx, "timestamp"],
                    "parametro":        col,
                    "valor":            round(float(val), 4),
                    "media":            round(float(media), 4),
                    "desvios_da_media": round(float(dev), 3),
                    "em_parada":        bool(parada_mask.loc[idx]),
                })

    if not registros:
        return pd.DataFrame(columns=["timestamp","parametro","valor",
                                      "media","desvios_da_media","em_parada"])

    df = pd.DataFrame(registros).sort_values("timestamp").reset_index(drop=True)
    if filtrar_parada:
        df = df[~df["em_parada"]].reset_index(drop=True)
    return df


def agrupar_eventos_outlier(outliers: pd.DataFrame,
                             intervalo_min: float = 30.0) -> pd.DataFrame:
    """
    Agrupa leituras de outlier consecutivas (mesmo parâmetro, dentro de
    `intervalo_min` minutos) em um único evento.

    Retorna DataFrame com:
      parametro, inicio, fim, ocorrencias, desvios_max, valor_max_desvio, em_parada
    """
    if outliers.empty:
        return pd.DataFrame(columns=["parametro","inicio","fim",
                                      "ocorrencias","desvios_max","valor_max_desvio","em_parada"])

    eventos = []
    for param, grupo in outliers.groupby("parametro"):
        g = grupo.sort_values("timestamp").reset_index(drop=True)
        ini  = g.loc[0, "timestamp"]
        fim  = g.loc[0, "timestamp"]
        devs = [g.loc[0, "desvios_da_media"]]
        vals = [g.loc[0, "valor"]]
        em_p = [g.loc[0, "em_parada"]]

        for i in range(1, len(g)):
            delta = (g.loc[i, "timestamp"] - fim).total_seconds() / 60
            if delta <= intervalo_min:
                fim  = g.loc[i, "timestamp"]
                devs.append(g.loc[i, "desvios_da_media"])
                vals.append(g.loc[i, "valor"])
                em_p.append(g.loc[i, "em_parada"])
            else:
                max_idx = int(np.argmax(devs))
                eventos.append({
                    "parametro":         param,
                    "inicio":            ini,
                    "fim":               fim,
                    "ocorrencias":       len(devs),
                    "desvios_max":       round(max(devs), 3),
                    "valor_max_desvio":  vals[max_idx],
                    "em_parada":         any(em_p),
                })
                ini  = g.loc[i, "timestamp"]
                fim  = g.loc[i, "timestamp"]
                devs = [g.loc[i, "desvios_da_media"]]
                vals = [g.loc[i, "valor"]]
                em_p = [g.loc[i, "em_parada"]]

        max_idx = int(np.argmax(devs))
        eventos.append({
            "parametro":         param,
            "inicio":            ini,
            "fim":               fim,
            "ocorrencias":       len(devs),
            "desvios_max":       round(max(devs), 3),
            "valor_max_desvio":  vals[max_idx],
            "em_parada":         any(em_p),
        })

    return pd.DataFrame(eventos).sort_values("inicio").reset_index(drop=True)


# ── gráficos ──────────────────────────────────────────────────────────────

CORES = ["#3b82f6","#10b981","#f59e0b","#8b5cf6","#06b6d4",
         "#f97316","#ec4899","#14b8a6","#a3e635","#fb923c"]


def gerar_graficos(dados: pd.DataFrame, saida: Path,
                    paradas: pd.DataFrame | None = None) -> None:
    """Gera PNG de série temporal para cada parâmetro."""
    saida_g = saida / "graficos"
    saida_g.mkdir(parents=True, exist_ok=True)

    ativos = [c for c in dados.columns[1:] if dados[c].notna().sum() > 0]
    ts = dados["timestamp"]

    for i, col in enumerate(ativos):
        fig, ax = plt.subplots(figsize=(14, 4))
        cor = CORES[i % len(CORES)]

        # bandas de parada
        if paradas is not None and not paradas.empty:
            for _, p in paradas.iterrows():
                ax.axvspan(p["inicio"], p["fim"], color="#94a3b8", alpha=0.18)

        ax.plot(ts, dados[col], color=cor, linewidth=1.2, alpha=0.9)
        ax.set_title(col, fontsize=11, pad=8)
        ax.set_xlabel("Tempo", fontsize=9)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        nome = "".join(c if c.isalnum() else "_" for c in col)[:50]
        fig.savefig(saida_g / f"{nome}.png", dpi=150, bbox_inches="tight")
        plt.close(fig)


def gerar_heatmap_correlacao(dados: pd.DataFrame, saida: Path) -> None:
    """Gera heatmap de correlação entre parâmetros."""
    ativos = [c for c in dados.columns[1:] if dados[c].notna().sum() >= 30]
    if len(ativos) < 2:
        return

    corr = dados[ativos].corr()
    n    = len(ativos)
    fig, ax = plt.subplots(figsize=(max(10, n * 0.6), max(8, n * 0.5)))
    im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(n)); ax.set_yticks(range(n))
    ax.set_xticklabels([c[:20] for c in ativos], rotation=45, ha="right", fontsize=7)
    ax.set_yticklabels([c[:20] for c in ativos], fontsize=7)
    plt.colorbar(im, ax=ax, label="r")
    ax.set_title("Matriz de Correlação", fontsize=12)
    fig.tight_layout()
    fig.savefig(saida / "correlacao_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


# ── exportação ────────────────────────────────────────────────────────────

def exportar(dados: pd.DataFrame, tags: pd.DataFrame, medias: pd.Series,
              stats: pd.DataFrame, outliers: pd.DataFrame,
              eventos: pd.DataFrame, paradas: pd.DataFrame,
              saida: Path) -> None:
    saida.mkdir(parents=True, exist_ok=True)
    dados.to_csv(saida / "dados_series_temporais.csv", index=False)
    tags.to_csv(saida / "tags_opc_ua.csv", index=False)
    stats.to_csv(saida / "estatisticas.csv")
    outliers.to_csv(saida / "outliers.csv", index=False)
    eventos.to_csv(saida / "eventos_outlier.csv", index=False)
    paradas.to_csv(saida / "paradas.csv", index=False)

    corr_df = dados[[c for c in dados.columns[1:]
                      if dados[c].notna().sum() >= 10]].corr()
    corr_df.to_csv(saida / "correlacao.csv")


# ── CLI ───────────────────────────────────────────────────────────────────

def _encontrar_arquivo(base: Path) -> Path | None:
    for nome in NOMES_ARQUIVO:
        p = base / nome
        if p.exists():
            return p
    for p in base.glob("*arametros*.xlsx"):
        return p
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Análise de processo Tissue")
    parser.add_argument("--arquivo",        type=Path, default=None)
    parser.add_argument("--saida",          type=Path, default=SAIDA_PADRAO)
    parser.add_argument("--desvios",        type=float, default=3.0)
    parser.add_argument("--sem-graficos",   action="store_true")
    parser.add_argument("--filtrar-parada", action="store_true")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    arq  = args.arquivo or _encontrar_arquivo(base)
    if arq is None:
        print("Erro: arquivo de parâmetros não encontrado.", file=sys.stderr)
        return 1

    print(f"Carregando: {arq.name}")
    dados, tags, medias = carregar_dados(arq)
    print(f"  {len(dados)} registros  |  {len(dados.columns)-1} parâmetros")

    paradas = detectar_paradas(dados)
    print(f"  {len(paradas)} períodos de parada detectados")

    stats    = estatisticas(dados, filtrar_parada=args.filtrar_parada)
    outliers = detectar_outliers(dados, n_desvios=args.desvios,
                                  filtrar_parada=args.filtrar_parada)
    eventos  = agrupar_eventos_outlier(outliers)
    print(f"  {len(outliers)} outliers  →  {len(eventos)} eventos agrupados")

    exportar(dados, tags, medias, stats, outliers, eventos, paradas, args.saida)
    print(f"  Exportado para {args.saida}/")

    if not args.sem_graficos:
        print("  Gerando gráficos...")
        gerar_graficos(dados, args.saida, paradas)
        gerar_heatmap_correlacao(dados, args.saida)

    print("Concluído.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
