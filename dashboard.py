#!/usr/bin/env python3
"""
Dashboard Tissue — análise de processo com filtro por período.

Arquitetura de callbacks sem race condition:
  poll → pkg Store → atualizar_kpis → sel-params + slider
  sel-params | slider (+ pkg como State) → atualizar_graficos
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import ALL, Dash, Input, Output, State, callback, dcc, html, no_update, ctx
from dash.exceptions import PreventUpdate

from analisar import (
    NOMES_ARQUIVO,
    carregar_dados,
    detectar_outliers,
    detectar_paradas,
    agrupar_eventos_outlier,
    estatisticas,
)
from integracao import (
    carregar_qualidade,
    carregar_producao,
    carregar_downtime_paradas,
    resumo_conformidade,
    pareto_downtime,
    correlacionar_processo_qualidade,
    salvar_temperatura_yankee,
    carregar_temperaturas_yankee,
    salvar_snapshot_correlacoes,
    carregar_historico_snapshots,
    comparar_correlacoes_por_periodo,
    PARAMS_QUALIDADE_PRINCIPAIS,
    TARGETS_PQ,
)
from relatorio_pdf import gerar_pdf_relatorio

# ── fontes de dados: Postgres (Neon) substitui leitura de arquivos locais ─
# Redefinimos as 4 funções importadas de integracao para que todos os
# callbacks usem o banco sem precisar alterar nenhuma outra linha do arquivo.

from db import (
    carregar_qualidade_db  as _cq_db,
    carregar_producao_db   as _cp_db,
    carregar_downtime_db   as _cd_db,
    carregar_processo_db   as _cproc_db,
)
from integracao import _parsear_specs, BASE as _BASE_DADOS

_DIAS_DB = 90  # janela de dados carregada do banco


def carregar_qualidade(caminho=None):  # type: ignore[assignment]
    df = _cq_db(dias=_DIAS_DB)
    _arq = next(
        (p for p in sorted((_BASE_DADOS / "qualidade").glob("*.xls*"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
         if any("spec" in s.lower() or "especifica" in s.lower()
                for s in pd.ExcelFile(p).sheet_names)),
        None,
    )
    specs = _parsear_specs(_arq) if _arq else pd.DataFrame()
    return df, specs


def carregar_producao(caminho=None):  # type: ignore[assignment]
    return _cp_db(dias=_DIAS_DB)


def carregar_downtime_paradas(incluir_hayout: bool = False):  # type: ignore[assignment]
    df = _cd_db(dias=_DIAS_DB)
    if not incluir_hayout and not df.empty and "Classe" in df.columns:
        classe_norm = df["Classe"].str.replace(r"\s+", "", regex=True).str.upper()
        df = df[~classe_norm.isin(["HAY-HAYOUT", "HAYHAYOUT"])].reset_index(drop=True)
    return df


def pareto_downtime(incluir_hayout: bool = False):  # type: ignore[assignment]
    df = carregar_downtime_paradas(incluir_hayout)
    if df.empty or "Tipo" not in df.columns or "Duração em Minutos" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["Tipo"] = df["Tipo"].fillna("").str.strip().replace("", "SEM PREENCHIMENTO")
    grp_cols = [c for c in ["Tipo", "Classe"] if c in df.columns]
    grp = (
        df.groupby(grp_cols)["Duração em Minutos"]
        .agg(total_min="sum", ocorrencias="count")
        .reset_index()
        .sort_values("total_min", ascending=False)
    )
    total = grp["total_min"].sum()
    grp["pct_acumulado"] = (grp["total_min"].cumsum() / total * 100).round(1) if total else 0.0
    grp["sem_preench"] = grp["Tipo"] == "SEM PREENCHIMENTO"
    total_occ = len(df)
    sp_min = df.loc[df["Tipo"] == "SEM PREENCHIMENTO", "Duração em Minutos"].sum()
    sp_occ = (df["Tipo"] == "SEM PREENCHIMENTO").sum()
    grp.attrs["pct_sem_preench_min"] = round(sp_min / total * 100, 1) if total else 0
    grp.attrs["pct_sem_preench_occ"] = round(sp_occ / total_occ * 100, 1) if total_occ else 0
    return grp

_DB_PROC = "__db__"  # valor especial para "carregar dados do banco"


def _carregar_pacote_de_df(dados: "pd.DataFrame", nome: str) -> dict:
    """Versão de carregar_pacote que recebe DataFrame já carregado (ex.: do banco)."""
    medias   = pd.Series(dtype=float)
    stats    = estatisticas(dados)
    paradas  = detectar_paradas(dados)
    outliers = detectar_outliers(dados)
    eventos  = agrupar_eventos_outlier(outliers)
    ativos   = _ativos(dados)
    ev_proc  = eventos[~eventos["em_parada"]]

    alertas = []
    for _, r in ev_proc.head(80).iterrows():
        sev, cor = _sev(float(r["desvios_max"]))
        alertas.append(dict(
            tipo="processo", sev=sev, cor=cor,
            inicio=r["inicio"].strftime("%d/%m %H:%M"),
            fim=r["fim"].strftime("%d/%m %H:%M"),
            param=r["parametro"],
            n=int(r["ocorrencias"]),
            sigma=round(float(r["desvios_max"]), 2),
            val=round(float(r["valor_max_desvio"]), 3),
        ))
    for _, r in eventos[eventos["em_parada"]].iterrows():
        alertas.append(dict(
            tipo="parada", sev="PARADA", cor=P["stop"],
            inicio=r["inicio"].strftime("%d/%m %H:%M"),
            fim=r["fim"].strftime("%d/%m %H:%M"),
            param=r["parametro"],
            n=int(r["ocorrencias"]),
            sigma=round(float(r["desvios_max"]), 2),
            val=round(float(r["valor_max_desvio"]), 3),
        ))
    sem = stats[stats["nulos_pct"] == 100].index.tolist()
    for p in sem:
        alertas.append(dict(tipo="sem_dados", sev="SEM DADOS", cor=P["muted2"],
                            inicio="—", fim="—", param=p, n=0, sigma=0, val="—"))

    ts   = dados["timestamp"]
    tmin = _ts_to_unix(ts.min())
    tmax = _ts_to_unix(ts.max())

    marcas = {}
    for t in pd.date_range(ts.min().date(), ts.max().date(), freq="D"):
        u = _ts_to_unix(t)
        if tmin <= u <= tmax:
            marcas[u] = t.strftime("%d/%m")

    pjson = paradas.to_json(orient="records", date_format="iso") if not paradas.empty else "[]"
    ojson = outliers[["timestamp", "parametro", "valor", "desvios_da_media", "em_parada"]].to_json(
        orient="records", date_format="iso"
    )

    return dict(
        arquivo=nome,
        carregado=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        periodo_ini=ts.min().strftime("%d/%m/%Y %H:%M"),
        periodo_fim=ts.max().strftime("%d/%m/%Y %H:%M"),
        registros=len(dados),
        n_ativos=len(ativos),
        n_total=len(dados.columns) - 1,
        n_eventos=len(ev_proc),
        n_criticos=sum(1 for a in alertas if a["sev"] == "CRÍTICO" and a["tipo"] == "processo"),
        n_paradas=len(paradas),
        h_parada=float(paradas["duracao_min"].sum() / 60) if not paradas.empty else 0.0,
        params=ativos,
        default=_top_cv(dados),
        ts_min=tmin,
        ts_max=tmax,
        slider_marks=marcas,
        dados_json=dados.to_json(orient="split", date_format="iso"),
        medias_json=medias.dropna().to_json(),
        paradas_json=pjson,
        outliers_json=ojson,
        alertas=alertas,
    )

# ─────────────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).resolve().parent
DADOS_DIR   = BASE_DIR / "dados"
LIMITES_PATH = BASE_DIR / "limites.json"
POLL_MS     = 30_000

# ── paleta ────────────────────────────────────────────────────────────────
P = {
    "bg":      "#0A0E1B",
    "surf":    "#0F1524",
    "card":    "#161D31",
    "card2":   "#1C2340",
    "plot":    "#0F1524",
    "border":  "rgba(255,255,255,0.07)",
    "border2": "rgba(255,255,255,0.13)",
    "text":    "#FFFFFF",
    "muted":   "#8892A4",
    "muted2":  "#5E6A7C",
    "accent":  "#00D4FF",
    "accent2": "#0099CC",
    "gold":    "#C9A020",
    "ok":      "#00E676",
    "warn":    "#FFB300",
    "crit":    "#FF4081",
    "stop":    "#5E6A7C",
    "lines":   ["#00D4FF","#A855F7","#FF4081","#00E676","#FFB300","#F97316","#06B6D4","#EC4899"],
}

SEV_THRESH = [("CRÍTICO", 5.0, "#ef4444"), ("ALERTA", 3.0, "#f59e0b")]

# ── helpers ───────────────────────────────────────────────────────────────

def _listar_arquivos(d: Path) -> list[Path]:
    """Retorna todos os arquivos de parâmetros OPC (Excel ou CSV) nas pastas de processo."""
    vistos: set[Path] = set()
    pastas = [d, d / "dados" / "processo"]
    for pasta in pastas:
        if not pasta.exists():
            continue
        for n in NOMES_ARQUIVO:
            p = pasta / n
            if p.exists():
                vistos.add(p)
        for p in pasta.glob("*arametros*.xlsx"):
            vistos.add(p)
        for p in pasta.glob("*arametros*.csv"):
            vistos.add(p)
        for p in pasta.glob("Analise_parametros*.csv"):
            vistos.add(p)
        for p in pasta.glob("Analise_parametros*.xlsx"):
            vistos.add(p)
    return sorted(vistos, key=lambda p: p.stat().st_mtime, reverse=True)


def _opcoes_arquivo(d: Path) -> list[dict]:
    arqs = _listar_arquivos(d)
    opts = []
    for p in arqs:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
        opts.append({"label": f"{p.name}  ·  {mtime}", "value": str(p)})
    return opts


def _sig(p: Path) -> str:
    s = p.stat()
    return f"{p.name}|{s.st_mtime_ns}|{s.st_size}"


def _ativos(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns[1:] if df[c].notna().sum() > 0]


def _top_cv(df: pd.DataFrame, n: int = 4) -> list[str]:
    cv = {}
    for col in _ativos(df):
        s = df[col].dropna()
        m = s.mean()
        if len(s) >= 10 and m != 0:
            cv[col] = abs(s.std() / m)
    return sorted(cv, key=cv.get, reverse=True)[:n]


def _sev(d: float) -> tuple[str, str]:
    for nome, lim, cor in SEV_THRESH:
        if d >= lim:
            return nome, cor
    return "INFO", P["muted"]


def _ts_to_unix(ts) -> float:
    return pd.Timestamp(ts).timestamp()


def _carregar_limites() -> dict:
    if LIMITES_PATH.exists():
        try:
            return json.loads(LIMITES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _salvar_limites(limites: dict) -> None:
    LIMITES_PATH.write_text(
        json.dumps(limites, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── carregamento ──────────────────────────────────────────────────────────

def carregar_pacote(caminho: Path) -> dict:
    dados, _, medias = carregar_dados(caminho)
    stats    = estatisticas(dados)
    paradas  = detectar_paradas(dados)
    outliers = detectar_outliers(dados)
    eventos  = agrupar_eventos_outlier(outliers)
    ativos   = _ativos(dados)
    ev_proc  = eventos[~eventos["em_parada"]]

    alertas = []
    for _, r in ev_proc.head(80).iterrows():
        sev, cor = _sev(float(r["desvios_max"]))
        alertas.append(dict(
            tipo="processo", sev=sev, cor=cor,
            inicio=r["inicio"].strftime("%d/%m %H:%M"),
            fim=r["fim"].strftime("%d/%m %H:%M"),
            param=r["parametro"],
            n=int(r["ocorrencias"]),
            sigma=round(float(r["desvios_max"]), 2),
            val=round(float(r["valor_max_desvio"]), 3),
        ))
    for _, r in eventos[eventos["em_parada"]].iterrows():
        alertas.append(dict(
            tipo="parada", sev="PARADA", cor=P["stop"],
            inicio=r["inicio"].strftime("%d/%m %H:%M"),
            fim=r["fim"].strftime("%d/%m %H:%M"),
            param=r["parametro"],
            n=int(r["ocorrencias"]),
            sigma=round(float(r["desvios_max"]), 2),
            val=round(float(r["valor_max_desvio"]), 3),
        ))
    sem = stats[stats["nulos_pct"] == 100].index.tolist()
    for p in sem:
        alertas.append(dict(tipo="sem_dados", sev="SEM DADOS", cor=P["muted2"],
                            inicio="—", fim="—", param=p, n=0, sigma=0, val="—"))

    ts   = dados["timestamp"]
    tmin = _ts_to_unix(ts.min())
    tmax = _ts_to_unix(ts.max())

    # Marcas do slider: um por dia
    marcas = {}
    for t in pd.date_range(ts.min().date(), ts.max().date(), freq="D"):
        u = _ts_to_unix(t)
        if tmin <= u <= tmax:
            marcas[u] = t.strftime("%d/%m")

    pjson = paradas.to_json(orient="records", date_format="iso") if not paradas.empty else "[]"
    ojson = outliers[["timestamp","parametro","valor","desvios_da_media","em_parada"]].to_json(orient="records", date_format="iso")

    return dict(
        arquivo=caminho.name,
        carregado=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        periodo_ini=ts.min().strftime("%d/%m/%Y %H:%M"),
        periodo_fim=ts.max().strftime("%d/%m/%Y %H:%M"),
        registros=len(dados),
        n_ativos=len(ativos),
        n_total=len(dados.columns) - 1,
        n_eventos=len(ev_proc),
        n_criticos=sum(1 for a in alertas if a["sev"] == "CRÍTICO" and a["tipo"] == "processo"),
        n_paradas=len(paradas),
        h_parada=float(paradas["duracao_min"].sum() / 60) if not paradas.empty else 0.0,
        params=ativos,
        default=_top_cv(dados),
        ts_min=tmin,
        ts_max=tmax,
        slider_marks=marcas,
        dados_json=dados.to_json(orient="split", date_format="iso"),
        medias_json=medias.dropna().to_json(),
        paradas_json=pjson,
        outliers_json=ojson,
        alertas=alertas,
    )


# ── figuras ───────────────────────────────────────────────────────────────

def _empty_fig(msg: str, h: int = 360) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=h,
        margin=dict(l=10, r=10, t=10, b=10),
        annotations=[dict(text=msg, showarrow=False,
                          font=dict(color=P["muted2"], size=13))],
    )
    return fig


def fig_trend(dados: pd.DataFrame, params: list[str], medias: pd.Series,
              paradas: pd.DataFrame, outliers: pd.DataFrame,
              limites: dict | None = None) -> go.Figure:
    fig = go.Figure()

    # bandas de parada
    for _, s in paradas.iterrows():
        fig.add_vrect(x0=s["inicio"], x1=s["fim"],
                      fillcolor=P["stop"], opacity=0.15,
                      layer="below", line_width=0)

    for i, p in enumerate(params):
        if p not in dados.columns:
            continue
        cor = P["lines"][i % len(P["lines"])]
        fig.add_trace(go.Scatter(
            x=dados["timestamp"], y=dados[p],
            name=p, mode="lines",
            line=dict(color=cor, width=1.8),
            hovertemplate="<b>%{x|%d/%m %H:%M}</b><br>%{y:.3f}<extra>" + p + "</extra>",
        ))
        # outliers de processo como marcadores ×
        po = outliers[(outliers["parametro"] == p) & (~outliers["em_parada"])]
        if not po.empty:
            fig.add_trace(go.Scatter(
                x=po["timestamp"], y=po["valor"],
                mode="markers",
                marker=dict(color=P["crit"], size=9, symbol="x-thin",
                            line=dict(width=2.5, color=P["crit"])),
                hovertemplate="⚠ <b>Outlier %{y:.3f}</b><br>%{x|%d/%m %H:%M}<extra>" + p + "</extra>",
                showlegend=False,
            ))
        ref = medias.get(p)
        if pd.notna(ref):
            fig.add_hline(y=float(ref), line=dict(color=cor, width=1, dash="dot"),
                          opacity=0.4, annotation_text="ref",
                          annotation_font=dict(size=9, color=cor),
                          annotation_position="top left")

        # limites configuráveis: LI verde, LS vermelho
        if limites and p in limites:
            li = limites[p].get("li")
            ls = limites[p].get("ls")
            lbl = p[:18]
            if li is not None:
                fig.add_hline(
                    y=float(li),
                    line=dict(color=P["ok"], width=1.8, dash="dashdot"),
                    annotation_text=f"LI {lbl}",
                    annotation_font=dict(size=9, color=P["ok"]),
                    annotation_position="bottom right",
                )
            if ls is not None:
                fig.add_hline(
                    y=float(ls),
                    line=dict(color=P["crit"], width=1.8, dash="dashdot"),
                    annotation_text=f"LS {lbl}",
                    annotation_font=dict(size=9, color=P["crit"]),
                    annotation_position="top right",
                )

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=360,
        margin=dict(l=54, r=18, t=32, b=42),
        legend=dict(orientation="h", y=1.04, x=0, font=dict(size=11, color=P["text"]),
                    bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
                   showgrid=True, zeroline=False, linecolor="rgba(0,212,255,0.2)"),
        yaxis=dict(gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
                   showgrid=True, zeroline=False, title="Valor", linecolor="rgba(0,212,255,0.2)"),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0F1524", font_size=12, font_color="#FFFFFF", bordercolor="#00D4FF"),
    )
    return fig


def fig_corr(dados: pd.DataFrame, params: list[str]) -> go.Figure:
    cols = [p for p in params if p in dados.columns and dados[p].notna().sum() >= 5]
    if len(cols) < 2:
        return _empty_fig("Selecione 2 ou mais parâmetros para calcular a correlação", h=380)

    corr = dados[cols].corr().round(3)
    n    = len(cols)
    fs   = max(7, min(11, int(120 / n)))  # fonte adaptativa ao número de params

    fig = go.Figure(go.Heatmap(
        z=corr.values,
        x=[c[:22] for c in corr.columns],
        y=[c[:22] for c in corr.index],
        text=[[f"{v:.2f}" for v in row] for row in corr.values],
        texttemplate="%{text}",
        textfont=dict(size=fs, color=P["text"]),
        colorscale=[[0, "#dc2626"], [0.5, "#ffffff"], [1, "#3b82f6"]],
        zmin=-1, zmax=1,
        colorbar=dict(
            thickness=10, len=0.85,
            tickfont=dict(size=9, color="#CBD5E1"),
            title=dict(text="r", font=dict(size=10, color=P["muted2"])),
        ),
        hovertemplate="<b>%{x}</b> × <b>%{y}</b><br>r = %{z}<extra></extra>",
    ))
    ml = max(90, min(160, n * 12))
    mb = max(90, min(160, n * 12))
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=380,
        margin=dict(l=ml, r=18, t=28, b=mb),
        xaxis=dict(tickangle=-40, tickfont=dict(size=fs, color="#CBD5E1"), side="bottom"),
        yaxis=dict(tickfont=dict(size=fs, color="#CBD5E1"), autorange="reversed"),
    )
    return fig


def fig_stats(dados: pd.DataFrame, params: list[str]) -> go.Figure:
    """Gráfico de box-plot dos parâmetros selecionados para o período."""
    cols = [p for p in params if p in dados.columns and dados[p].notna().sum() >= 5]
    if not cols:
        return _empty_fig("Sem dados para o período", h=280)

    fig = go.Figure()
    for i, p in enumerate(cols):
        cor = P["lines"][i % len(P["lines"])]
        fig.add_trace(go.Box(
            y=dados[p].dropna(), name=p[:25],
            marker_color=cor, line_color=cor,
            boxmean="sd",
            hovertemplate="<b>" + p + "</b><br>%{y:.3f}<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=280,
        margin=dict(l=54, r=18, t=28, b=60),
        showlegend=False,
        xaxis=dict(tickfont=dict(size=9, color="#CBD5E1"), tickangle=-30,
                   linecolor="rgba(0,212,255,0.2)"),
        yaxis=dict(gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
                   linecolor="rgba(0,212,255,0.2)"),
    )
    return fig


def fig_scatter_matrix(dados: pd.DataFrame, params: list[str]) -> go.Figure:
    """Scatter matrix (SPLOM) dos parâmetros selecionados."""
    cols = [p for p in params if p in dados.columns and dados[p].notna().sum() >= 5]
    if len(cols) < 2:
        return _empty_fig("Selecione 2 ou mais parâmetros", 420)

    df = dados[cols].dropna()
    n  = len(cols)
    fs = max(7, min(10, int(90 / n)))

    dimensions = [dict(label=c[:18], values=df[c]) for c in cols]

    fig = go.Figure(go.Splom(
        dimensions=dimensions,
        showupperhalf=False,
        diagonal_visible=True,
        marker=dict(
            size=3, opacity=0.45, color=P["accent"],
            line=dict(width=0),
        ),
        hovertemplate="%{xaxis.title.text}: %{x:.3f}<br>%{yaxis.title.text}: %{y:.3f}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"],
        height=max(420, n * 110),
        margin=dict(l=10, r=10, t=32, b=10),
        title=dict(text="Scatter matrix — par a par", font=dict(size=12, color=P["text"]), x=0.01),
        font=dict(size=fs, color=P["text"]),
        dragmode="select",
    )
    return fig


def fig_crosscorr(dados: pd.DataFrame, param1: str, param2: str,
                  max_lag_min: int = 180) -> go.Figure:
    """Cross-correlação com defasagem: descobre o lag ótimo entre dois parâmetros."""
    if not param1 or not param2:
        return _empty_fig("Selecione dois parâmetros", 320)
    if param1 not in dados.columns or param2 not in dados.columns:
        return _empty_fig(f"Parâmetro não encontrado", 320)
    if param1 == param2:
        return _empty_fig("Selecione parâmetros diferentes", 320)

    df = dados[["timestamp", param1, param2]].dropna()
    if len(df) < 10:
        return _empty_fig("Dados insuficientes no período", 320)

    # Intervalo em minutos
    try:
        dt_min = max(1, int(df["timestamp"].diff().median().total_seconds() / 60))
    except Exception:
        dt_min = 5

    max_lag = max_lag_min // dt_min
    s1 = df[param1].values
    s2 = df[param2].values

    lags, rs = [], []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a, b = s1[:len(s1)-lag] if lag > 0 else s1, s2[lag:] if lag > 0 else s2
        else:
            a, b = s1[-lag:], s2[:len(s2)+lag]
        if len(a) < 5:
            continue
        try:
            r = float(np.corrcoef(a, b)[0, 1])
        except Exception:
            r = float("nan")
        lags.append(lag * dt_min)
        rs.append(r)

    if not rs:
        return _empty_fig("Não foi possível calcular a correlação", 320)

    rs_arr  = np.array(rs)
    best_i  = int(np.nanargmax(np.abs(rs_arr)))
    best_lag_min = lags[best_i]
    best_r  = rs[best_i]

    cores = [P["ok"] if r > 0 else P["crit"] for r in rs]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=lags, y=rs,
        marker_color=cores, marker_line_width=0,
        hovertemplate="Lag: %{x:+d} min<br>r = %{y:.3f}<extra></extra>",
        name="r(lag)",
    ))
    fig.add_vline(x=best_lag_min, line=dict(color=P["warn"], width=2, dash="dot"))
    fig.add_vline(x=0, line=dict(color=P["border2"], width=1))
    fig.add_annotation(
        x=best_lag_min, y=best_r,
        text=f"Lag ótimo: {best_lag_min:+d} min<br>r = {best_r:.3f}",
        showarrow=True, arrowhead=2, arrowcolor=P["warn"],
        font=dict(size=10, color=P["text"]),
        bgcolor=P["card"], bordercolor=P["border"], borderpad=6,
    )

    direcao = f"{param1[:16]} → {param2[:16]}" if best_lag_min > 0 else \
              f"{param2[:16]} → {param1[:16]}" if best_lag_min < 0 else "simultâneos"
    titulo  = f"Cross-correlação  |  lag ótimo: {best_lag_min:+d} min  |  {direcao}"

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=340,
        title=dict(text=titulo, font=dict(size=11, color=P["text"]), x=0.01),
        margin=dict(l=60, r=20, t=52, b=54),
        xaxis=dict(
            title="Defasagem (min)  — positivo: param A precede param B",
            gridcolor=P["border"], zeroline=True,
            zerolinecolor="rgba(0,212,255,0.2)", zerolinewidth=2,
            tickfont=dict(size=10, color="#CBD5E1"),
        ),
        yaxis=dict(
            title="r de Pearson", range=[-1.05, 1.05],
            gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
        ),
        showlegend=False,
    )
    return fig


def fig_imr(conf: pd.DataFrame, param: str) -> go.Figure:
    """Carta de controle IMR (Individual + Moving Range) por jumbo."""
    if conf.empty or "parametro" not in conf.columns:
        return _empty_fig(f"Sem especificações para {param}", 420)
    df = conf[conf["parametro"] == param].sort_values("Data").copy()
    vals = df["valor"].dropna().reset_index(drop=True)

    if len(vals) < 5:
        return _empty_fig(f"Dados insuficientes para carta IMR de {param}", 420)

    media    = vals.mean()
    mr       = vals.diff().abs().dropna()
    mr_bar   = mr.mean()
    sigma    = mr_bar / 1.128        # d2 para n=1
    ucl_i    = media + 3 * sigma
    lcl_i    = media - 3 * sigma
    ucl_mr   = 3.267 * mr_bar

    datas    = df["Data"].reset_index(drop=True)
    cor_pts  = [P["crit"] if (v > ucl_i or v < lcl_i) else P["accent"] for v in vals]
    n_fora   = sum(1 for c in cor_pts if c == P["crit"])

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.06,
        subplot_titles=[
            f"Carta I — {param}  ({n_fora} ponto{'s' if n_fora != 1 else ''} fora dos limites)",
            "Amplitude Móvel (MR)",
        ],
    )

    # ── Carta Individual ─────────────────────────────────────────────────
    familia_vals = df["Familia"].reset_index(drop=True) if "Familia" in df.columns else pd.Series([""] * len(vals))
    unidade_vals = df["Unidade"].reset_index(drop=True) if "Unidade" in df.columns else pd.Series([""] * len(vals))
    customdata_i = list(zip(familia_vals, unidade_vals))

    fig.add_trace(go.Scatter(
        x=datas, y=vals,
        mode="lines+markers",
        line=dict(color=P["accent"], width=1.5),
        marker=dict(color=cor_pts, size=6, line=dict(width=1, color=P["card"])),
        customdata=customdata_i,
        hovertemplate=(
            "<b>%{x|%d/%m %H:%M}</b><br>"
            "Valor: <b>%{y:.4f}</b><br>"
            "Produto: %{customdata[0]}<br>"
            "Jumbo: %{customdata[1]}"
            "<extra>I</extra>"
        ),
        name="Individual",
    ), row=1, col=1)

    for y_val, cor, lbl, pos in [
        (ucl_i,  P["crit"],   f"UCL = {ucl_i:.4f}",  "top right"),
        (media,  P["muted2"], f"X̄ = {media:.4f}",     "top left"),
        (lcl_i,  P["warn"],   f"LCL = {lcl_i:.4f}",   "bottom right"),
    ]:
        fig.add_hline(y=y_val, line=dict(color=cor, width=1.3, dash="dash"),
                      annotation_text=lbl, annotation_font=dict(size=9, color=cor),
                      annotation_position=pos, row=1, col=1)

    # Specs (LSE/LSC) se disponíveis
    lse = df["LSE"].dropna().iloc[0] if "LSE" in df and df["LSE"].notna().any() else None
    lsc = df["LSC"].dropna().iloc[0] if "LSC" in df and df["LSC"].notna().any() else None
    if lse is not None:
        fig.add_hline(y=lse, line=dict(color=P["crit"], width=1, dash="dot"),
                      annotation_text=f"LSE={lse:.4f}", annotation_font=dict(size=9, color=P["crit"]),
                      annotation_position="top left", row=1, col=1)
    if lsc is not None:
        fig.add_hline(y=lsc, line=dict(color=P["warn"], width=1, dash="dot"),
                      annotation_text=f"LSC={lsc:.4f}", annotation_font=dict(size=9, color=P["warn"]),
                      annotation_position="bottom left", row=1, col=1)

    # ── Carta MR ─────────────────────────────────────────────────────────
    fig.add_trace(go.Bar(
        x=datas.iloc[1:], y=mr,
        marker_color=P["accent"], opacity=0.65,
        hovertemplate="%{y:.4f}<extra>MR</extra>",
        name="MR",
    ), row=2, col=1)

    for y_val, cor, lbl in [
        (ucl_mr, P["crit"],   f"UCL MR = {ucl_mr:.4f}"),
        (mr_bar, P["muted2"], f"MR̄ = {mr_bar:.4f}"),
    ]:
        fig.add_hline(y=y_val, line=dict(color=cor, width=1.3, dash="dash"),
                      annotation_text=lbl, annotation_font=dict(size=9, color=cor),
                      annotation_position="top right", row=2, col=1)

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=430,
        margin=dict(l=60, r=110, t=50, b=42),
        showlegend=False,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0F1524", font_size=11, font_color="#FFFFFF", bordercolor="#00D4FF"),
    )
    fig.update_xaxes(gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1"))
    fig.update_yaxes(gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1"))
    return fig


def fig_qualidade_param(conf: pd.DataFrame, param: str) -> go.Figure:
    """Série temporal de um parâmetro de qualidade por jumbo com LSE/LSC/Meta."""
    if conf.empty or "parametro" not in conf.columns:
        return _empty_fig(f"Sem especificações para {param}", 320)
    df = conf[conf["parametro"] == param].sort_values("Data").copy()
    if df.empty:
        return _empty_fig(f"Sem dados para {param}", 320)

    cores_status = {"OK": P["ok"], "FORA_LSE": P["crit"], "FORA_LSC": P["warn"], "SEM_SPEC": P["muted"]}
    fig = go.Figure()

    for status, grupo in df.groupby("status"):
        cd = list(zip(
            grupo["Familia"].tolist() if "Familia" in grupo.columns else [""] * len(grupo),
            grupo["Unidade"].tolist() if "Unidade" in grupo.columns else [""] * len(grupo),
        ))
        fig.add_trace(go.Scatter(
            x=grupo["Data"], y=grupo["valor"],
            mode="markers", name=status,
            marker=dict(color=cores_status.get(status, P["muted"]), size=7, opacity=0.8),
            customdata=cd,
            hovertemplate=(
                "<b>%{x|%d/%m %H:%M}</b><br>"
                "Valor: <b>%{y:.3f}</b><br>"
                "Produto: %{customdata[0]}<br>"
                "Jumbo: %{customdata[1]}"
                "<extra>%{fullData.name}</extra>"
            ),
        ))

    # limites como linhas horizontais
    lse = df["LSE"].dropna().iloc[0] if df["LSE"].notna().any() else None
    lsc = df["LSC"].dropna().iloc[0] if df["LSC"].notna().any() else None
    meta = df["Meta"].dropna().iloc[0] if df["Meta"].notna().any() else None

    if lse is not None:
        fig.add_hline(y=lse, line=dict(color=P["crit"], dash="dash", width=1.5),
                      annotation_text="LSE", annotation_font=dict(size=10, color=P["crit"]))
    if lsc is not None:
        fig.add_hline(y=lsc, line=dict(color=P["warn"], dash="dash", width=1.5),
                      annotation_text="LSC", annotation_font=dict(size=10, color=P["warn"]))
    if meta is not None:
        fig.add_hline(y=meta, line=dict(color=P["ok"], dash="dot", width=1.2),
                      annotation_text="Meta", annotation_font=dict(size=10, color=P["ok"]))

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=320,
        title=dict(text=param, font=dict(size=13, color=P["text"]), x=0.01),
        margin=dict(l=54, r=18, t=42, b=42),
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=10, color=P["text"])),
        xaxis=dict(gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1")),
        yaxis=dict(gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1")),
        hovermode="x unified",
    )
    return fig


def fig_conformidade_produto(conf: pd.DataFrame) -> go.Figure:
    """Barras empilhadas: conformidade por produto."""
    if conf.empty:
        return _empty_fig("Sem dados de conformidade", 280)

    grp = (conf[conf["status"] != "SEM_SPEC"]
           .groupby(["Familia", "status"])["Unidade"]
           .count()
           .reset_index(name="count"))

    if grp.empty:
        return _empty_fig("Sem especificações cadastradas", 280)

    status_cores = {"OK": P["ok"], "FORA_LSE": P["crit"], "FORA_LSC": P["warn"]}
    fig = go.Figure()
    for status, cor in status_cores.items():
        d = grp[grp["status"] == status]
        if d.empty:
            continue
        fig.add_trace(go.Bar(
            x=d["Familia"], y=d["count"], name=status,
            marker_color=cor, opacity=0.85,
            hovertemplate="<b>%{x}</b><br>" + status + ": %{y}<extra></extra>",
        ))

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=280, barmode="stack",
        margin=dict(l=50, r=18, t=28, b=50),
        legend=dict(orientation="h", y=1.08, x=0, font=dict(size=10, color=P["text"])),
        xaxis=dict(tickfont=dict(size=11, color=P["text"])),
        yaxis=dict(gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
                   title="Medições"),
    )
    return fig


def fig_pareto_valores(conf: pd.DataFrame, param: str) -> go.Figure:
    """Pareto de resultado × quantidade: cada valor arredondado = uma barra, sorted por freq."""
    if conf.empty or "parametro" not in conf.columns:
        return _empty_fig("Selecione um parâmetro", 300)
    df = conf[conf["parametro"] == param].copy()
    if df.empty:
        return _empty_fig(f"Sem dados para {param}", 300)

    # determina casas decimais pela dispersão dos valores
    spread = df["valor"].max() - df["valor"].min() if len(df) > 1 else 1
    decimais = 1 if spread > 5 else (3 if spread < 0.1 else 2)

    df["val_arred"] = df["valor"].round(decimais)

    # conta e status predominante de cada bin (para cor)
    grp = df.groupby("val_arred").agg(
        quantidade=("valor", "count"),
        status_pior=("status", lambda s: (
            "FORA_LSE" if "FORA_LSE" in s.values else
            "FORA_LSC" if "FORA_LSC" in s.values else "OK"
        )),
    ).reset_index().sort_values("quantidade", ascending=False)

    # acumulado
    grp["acum"] = grp["quantidade"].cumsum() / grp["quantidade"].sum() * 100

    cores = grp["status_pior"].map({"OK": P["ok"], "FORA_LSE": P["crit"], "FORA_LSC": P["warn"]}).tolist()
    labels = [str(v) for v in grp["val_arred"]]

    lse = df["LSE"].dropna().iloc[0] if df["LSE"].notna().any() else None
    lsc = df["LSC"].dropna().iloc[0] if df["LSC"].notna().any() else None

    lse_str = f" | LSE={lse:.{decimais}f}" if lse is not None else ""
    lsc_str = f" | LSC={lsc:.{decimais}f}" if lsc is not None else ""

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=labels, y=grp["quantidade"],
        marker_color=cores, opacity=0.85,
        customdata=list(zip(grp["val_arred"], grp["quantidade"])),
        hovertemplate="<b>%{x}</b><br>%{y} jumbos<extra></extra>",
        name="Quantidade",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=labels, y=grp["acum"],
        mode="lines+markers",
        line=dict(color=P["warn"], width=2),
        marker=dict(size=5),
        hovertemplate="%{y:.1f}%<extra>Acumulado</extra>",
        name="% Acumulado",
    ), secondary_y=True)

    fig.update_layout(
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=300,
        title=dict(text=f"Pareto — {param}{lsc_str}{lse_str}",
                   font=dict(size=12, color=P["text"]), x=0.01),
        margin=dict(l=50, r=60, t=36, b=60),
        legend=dict(orientation="h", y=1.1, x=0, font=dict(size=10, color=P["text"])),
        bargap=0.15,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0F1524", font_size=11, font_color="#FFFFFF", bordercolor="#00D4FF"),
    )
    fig.update_xaxes(tickfont=dict(size=10, color="#CBD5E1"), tickangle=-45,
                     title_text=param, title_font=dict(size=10, color=P["muted"]),
                     gridcolor=P["border"])
    fig.update_yaxes(title_text="Jumbos", title_font=dict(size=10, color=P["muted"]),
                     gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1"), secondary_y=False)
    fig.update_yaxes(title_text="% Acumulado", range=[0, 105],
                     tickfont=dict(size=10, color="#CBD5E1"), secondary_y=True)

    return fig


_META_DT_MIN   = 60   # meta diária downtime (minutos)
_META_DT_DES   = 45   # meta desafio downtime (minutos)
_META_PROD_TON = 150  # meta diária produção (toneladas)


def fig_producao_downtime_semanal(dq: pd.DataFrame, dd: pd.DataFrame) -> go.Figure:
    """Barras duplas semanais: produção (ton) × downtime (min), com metas."""
    if dq.empty:
        return _empty_fig("Sem dados de produção", 360)

    # ── produção por semana ────────────────────────────────────────────────
    dq2 = dq.copy()
    dq2["semana"] = dq2["Data"].dt.isocalendar().week.astype(int)
    # usa Peso se disponível, senão conta jumbos como proxy de produção
    if "Peso" in dq2.columns:
        prod = dq2.groupby("semana").agg(
            producao_ton=("Peso", lambda x: x.sum() / 1000),
            dias=("Data",         lambda x: x.dt.date.nunique()),
        ).reset_index()
        eixo_prod = "Produção (ton)"
    else:
        prod = dq2.groupby("semana").agg(
            producao_ton=("Unidade", "count"),
            dias=("Data",            lambda x: x.dt.date.nunique()),
        ).reset_index()
        eixo_prod = "Jumbos produzidos"

    # ── downtime por semana (excluindo HAY) ───────────────────────────────
    if not dd.empty and "Início" in dd.columns:
        dd2 = dd.copy()
        dd2["semana"] = dd2["Início"].dt.isocalendar().week.astype(int)
        dt = dd2.groupby("semana")["Duração em Minutos"].sum().reset_index()
        dt.columns = ["semana", "downtime_min"]
        prod = prod.merge(dt, on="semana", how="left")
    else:
        prod["downtime_min"] = 0.0
    prod["downtime_min"] = prod["downtime_min"].fillna(0)

    # ── metas escaladas por dias na semana ────────────────────────────────
    prod["meta_prod"]    = prod["dias"] * _META_PROD_TON
    prod["meta_dt"]      = prod["dias"] * _META_DT_MIN
    prod["meta_desafio"] = prod["dias"] * _META_DT_DES

    labels = [f"Sem {w}" for w in prod["semana"]]

    # cores produção: verde ≥ meta, amarelo ≥ 90 %, vermelho abaixo
    cor_prod = [
        P["ok"]   if v >= m else
        P["warn"] if v >= m * 0.9 else P["crit"]
        for v, m in zip(prod["producao_ton"], prod["meta_prod"])
    ]
    # cores downtime: verde ≤ desafio, amarelo ≤ meta, vermelho acima
    cor_dt = [
        P["ok"]   if v <= d else
        P["warn"] if v <= m else P["crit"]
        for v, d, m in zip(prod["downtime_min"], prod["meta_desafio"], prod["meta_dt"])
    ]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # barras de produção (eixo esquerdo)
    fig.add_trace(go.Bar(
        name=eixo_prod, x=labels, y=prod["producao_ton"],
        marker_color=cor_prod, opacity=0.85,
        customdata=list(zip(prod["producao_ton"], prod["meta_prod"], prod["dias"])),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Produção: <b>%{customdata[0]:.1f} ton</b><br>"
            "Meta: %{customdata[1]:.0f} ton (%{customdata[2]} dias)"
            "<extra>Produção</extra>"
        ),
    ), secondary_y=False)

    # barras de downtime (eixo direito)
    fig.add_trace(go.Bar(
        name="Downtime (min)", x=labels, y=prod["downtime_min"],
        marker_color=cor_dt, opacity=0.75,
        customdata=list(zip(prod["downtime_min"], prod["meta_dt"], prod["meta_desafio"])),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Downtime: <b>%{customdata[0]:.0f} min</b><br>"
            "Meta: %{customdata[1]:.0f} min | Desafio: %{customdata[2]:.0f} min"
            "<extra>Downtime</extra>"
        ),
    ), secondary_y=True)

    # linhas de meta de produção
    fig.add_trace(go.Scatter(
        x=labels, y=prod["meta_prod"], mode="lines+markers",
        line=dict(color=P["ok"], width=1.5, dash="dash"),
        marker=dict(size=5), name="Meta prod.",
        hovertemplate="%{y:.0f} ton<extra>Meta prod.</extra>",
    ), secondary_y=False)

    # linhas de meta de downtime
    fig.add_trace(go.Scatter(
        x=labels, y=prod["meta_dt"], mode="lines",
        line=dict(color=P["warn"], width=1.5, dash="dot"),
        name="Meta DT (60 min/dia)",
        hovertemplate="%{y:.0f} min<extra>Meta DT</extra>",
    ), secondary_y=True)

    fig.add_trace(go.Scatter(
        x=labels, y=prod["meta_desafio"], mode="lines",
        line=dict(color=P["crit"], width=1.5, dash="dot"),
        name="Desafio DT (45 min/dia)",
        hovertemplate="%{y:.0f} min<extra>Desafio DT</extra>",
    ), secondary_y=True)

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=360,
        barmode="group",
        margin=dict(l=60, r=70, t=40, b=42),
        legend=dict(orientation="h", y=1.08, x=0, font=dict(size=10, color=P["text"])),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#0F1524", font_size=11, font_color="#FFFFFF", bordercolor="#00D4FF"),
    )
    fig.update_xaxes(tickfont=dict(size=11, color=P["text"]))
    fig.update_yaxes(title_text=eixo_prod, gridcolor=P["border"],
                     tickfont=dict(size=10, color="#CBD5E1"), secondary_y=False)
    fig.update_yaxes(title_text="Downtime (min)", gridcolor=P["border"],
                     tickfont=dict(size=10, color="#CBD5E1"), secondary_y=True)
    return fig


def fig_pareto_downtime(pareto: pd.DataFrame) -> go.Figure:
    """Gráfico de Pareto de downtime com destaque para SEM PREENCHIMENTO."""
    if pareto.empty:
        return _empty_fig("Sem dados de downtime", 320)

    pct_sp_min = pareto.attrs.get("pct_sem_preench_min", 0)
    pct_sp_occ = pareto.attrs.get("pct_sem_preench_occ", 0)

    tipos = [str(t)[:50] for t in pareto["Tipo"]]
    sem_preench = pareto.get("sem_preench", pd.Series([False] * len(pareto))).tolist()

    cores = [P["muted"] if sp else P["accent"] for sp in sem_preench]

    hover_extra = []
    for _, row in pareto.iterrows():
        cls = str(row.get("Classe", "")).split("-")[-1].strip() if "Classe" in pareto.columns else ""
        occ = int(row.get("ocorrencias", 0))
        hover_extra.append(f"{cls} · {occ} ocorrência{'s' if occ != 1 else ''}")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=tipos, y=pareto["total_min"],
        name="Minutos",
        marker_color=cores, opacity=0.85,
        customdata=hover_extra,
        hovertemplate="<b>%{x}</b><br>%{y:.1f} min<br>%{customdata}<extra></extra>",
        text=[f"{v:.0f}" for v in pareto["total_min"]],
        textposition="outside",
        textfont=dict(size=9),
    ))
    fig.add_trace(go.Scatter(
        x=tipos, y=pareto["pct_acumulado"],
        name="% Acumulado", yaxis="y2",
        line=dict(color=P["warn"], width=2),
        marker=dict(size=6),
        hovertemplate="%{y:.1f}%<extra>Acumulado</extra>",
    ))
    fig.add_hline(y=80, yref="y2", line=dict(color=P["crit"], dash="dot", width=1),
                  annotation_text="80%", annotation_font=dict(size=9, color=P["crit"]))

    titulo = "Pareto de causas — maio/26"
    if pct_sp_min > 0:
        titulo += f"  |  ⚠ Sem preenchimento: {pct_sp_occ}% das ocorrências · {pct_sp_min}% do tempo"

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=340,
        title=dict(text=titulo, font=dict(size=11, color=P["warn"] if pct_sp_min > 0 else P["text"]), x=0.01),
        margin=dict(l=54, r=54, t=46, b=130),
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=10, color=P["text"])),
        xaxis=dict(tickangle=-40, tickfont=dict(size=9, color=P["text"])),
        yaxis=dict(title="Minutos", gridcolor=P["border"],
                   tickfont=dict(size=10, color="#CBD5E1")),
        yaxis2=dict(title="% Acumulado", overlaying="y", side="right",
                    range=[0, 115], tickfont=dict(size=10, color="#CBD5E1")),
        hovermode="x unified",
    )
    return fig


def fig_downtime_timeline(dd: pd.DataFrame) -> go.Figure:
    """Linha do tempo de eventos de downtime."""
    if dd.empty:
        return _empty_fig("Sem eventos de parada", 200)

    cores_classe = {
        "PPR-PARADA PROGRAMADA": P["muted2"],
        "MCR-MANUTENÇÃO CORRETIVA": P["crit"],
        "PME-PARADAS MENORES": P["warn"],
        "LMP-LIMPEZA PROGRAMADA": P["ok"],
    }

    fig = go.Figure()
    for _, row in dd.iterrows():
        cor = cores_classe.get(str(row.get("Classe", "")), P["accent"])
        ini = row.get("Inicio") or row.get("Início")
        fim = row.get("Fim")
        tipo = str(row.get("Tipo", ""))[:50]
        dur  = row.get("Duração em Minutos", 0)
        fig.add_trace(go.Scatter(
            x=[ini, fim], y=[1, 1],
            mode="lines", line=dict(color=cor, width=18),
            name=str(row.get("Classe", "")),
            showlegend=False,
            hovertemplate=f"<b>{tipo}</b><br>{dur:.0f} min<extra></extra>",
        ))

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=140,
        margin=dict(l=30, r=18, t=18, b=30),
        xaxis=dict(tickfont=dict(size=10, color="#CBD5E1"), gridcolor=P["border"]),
        yaxis=dict(visible=False),
        showlegend=False,
        hovermode="x",
    )
    return fig


def fig_corr_pq(df_corr: pd.DataFrame, top_n: int = 20) -> go.Figure:
    """Heatmap de correlação processo × qualidade (top N variáveis)."""
    if df_corr.empty:
        return _empty_fig("Sem dados suficientes para correlação P×Q", 420)

    df = df_corr.head(top_n).copy()
    targets = list(df.columns)
    variaveis = list(df.index)
    n_var = len(variaveis)
    fs = max(8, min(11, int(140 / max(n_var, 1))))

    def _cell_label(v):
        if not pd.notna(v):
            return ""
        a = abs(v)
        if a >= 0.6:
            return "FORTE"
        elif a >= 0.35:
            return "MED"
        elif a >= 0.15:
            return "fraca"
        return ""

    def _cell_hover(v, var, tgt):
        if not pd.notna(v):
            return ""
        forca, desc, _ = _forca_label(v)
        return f"<b>{var}</b> → <b>{tgt}</b><br>r = {v:.2f}  |  {forca}<br>{desc}<extra></extra>"

    cell_text   = [[_cell_label(v) for v in row] for row in df.values]
    hover_texts = [[_cell_hover(v, var, tgt)
                    for tgt, v in zip(targets, row)]
                   for var, row in zip(variaveis, df.values)]

    fig = go.Figure(go.Heatmap(
        z=df.values,
        x=targets,
        y=[v[:30] for v in variaveis],
        text=cell_text,
        texttemplate="%{text}",
        textfont=dict(size=fs, color=P["text"]),
        customdata=hover_texts,
        hovertemplate="%{customdata}",
        colorscale=[[0, "#dc2626"], [0.5, "#ffffff"], [1, "#3b82f6"]],
        zmin=-1, zmax=1,
        colorbar=dict(
            thickness=10, len=0.85,
            tickfont=dict(size=9, color="#CBD5E1"),
            title=dict(text="r", font=dict(size=10, color=P["muted2"])),
        ),
    ))
    ml = max(120, min(200, n_var * 8))
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=max(380, n_var * 22),
        margin=dict(l=ml, r=20, t=40, b=60),
        title=dict(text="Correlação Processo × Qualidade", font=dict(size=13, color=P["text"]), x=0.01),
        xaxis=dict(tickfont=dict(size=11, color=P["text"]), side="top"),
        yaxis=dict(tickfont=dict(size=fs, color=P["text"]), autorange="reversed"),
    )
    return fig


def fig_scatter_pq(df_j: pd.DataFrame, var_proc: str, target: str) -> go.Figure:
    """Scatter plot entre uma variável de processo e um target de qualidade."""
    col_x = f"opc_{var_proc}" if f"opc_{var_proc}" in df_j.columns else var_proc
    if col_x not in df_j.columns or target not in df_j.columns:
        return _empty_fig("Selecione variável e target", 300)

    df = df_j[[col_x, target, "Familia"]].dropna()
    if len(df) < 5:
        return _empty_fig("Dados insuficientes", 300)

    familias = df["Familia"].unique()
    cores_familia = {f: P["lines"][i % len(P["lines"])] for i, f in enumerate(sorted(familias))}

    fig = go.Figure()
    for fam in sorted(familias):
        sub = df[df["Familia"] == fam]
        fig.add_trace(go.Scatter(
            x=sub[col_x], y=sub[target],
            mode="markers", name=fam,
            marker=dict(color=cores_familia[fam], size=7, opacity=0.75),
            hovertemplate=f"<b>{fam}</b><br>{var_proc}: %{{x:.3f}}<br>{target}: %{{y:.3f}}<extra></extra>",
        ))

    # linha de tendência
    x_all = pd.to_numeric(df[col_x], errors="coerce").dropna()
    y_all = pd.to_numeric(df[target], errors="coerce").dropna()
    idx_c = x_all.index.intersection(y_all.index)
    if len(idx_c) >= 5:
        z = np.polyfit(x_all.loc[idx_c], y_all.loc[idx_c], 1)
        x_line = [float(x_all.loc[idx_c].min()), float(x_all.loc[idx_c].max())]
        y_line = [z[0] * v + z[1] for v in x_line]
        r = float(x_all.loc[idx_c].corr(y_all.loc[idx_c]))
        fig.add_trace(go.Scatter(
            x=x_line, y=y_line, mode="lines",
            line=dict(color=P["muted2"], width=1.5, dash="dot"),
            showlegend=False,
            hovertemplate=f"r = {r:.3f}<extra>Tendência</extra>",
        ))
        titulo = f"{var_proc} × {target}  (r = {r:.3f})"
    else:
        titulo = f"{var_proc} × {target}"

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
        height=300,
        title=dict(text=titulo, font=dict(size=12, color=P["text"]), x=0.01),
        margin=dict(l=54, r=18, t=42, b=42),
        xaxis=dict(title=var_proc[:30], gridcolor=P["border"],
                   tickfont=dict(size=10, color="#CBD5E1")),
        yaxis=dict(title=target, gridcolor=P["border"],
                   tickfont=dict(size=10, color="#CBD5E1")),
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=10, color=P["text"])),
    )
    return fig


def _forca_label(r: float) -> tuple[str, str, str]:
    """Retorna (label, desc_direcao, cor) para um coeficiente de correlação."""
    a = abs(r)
    if a >= 0.6:
        forca = "FORTE"
    elif a >= 0.35:
        forca = "MODERADA"
    elif a >= 0.15:
        forca = "FRACA"
    else:
        forca = "MUITO FRACA"

    if r > 0:
        desc = "se sobe → sobe junto"
        cor  = P["ok"] if a >= 0.35 else P["muted"]
    else:
        desc = "se sobe → cai"
        cor  = P["crit"] if a >= 0.35 else P["muted"]

    return forca, desc, cor


def _barra_forca(r: float) -> html.Div:
    """Barra visual de força da correlação (0–10 blocos)."""
    a        = abs(r)
    n_cheios = round(a * 10)
    cor      = P["ok"] if r > 0 else P["crit"]
    cor_vazio = P["border2"]

    blocos = []
    for i in range(10):
        blocos.append(html.Span(style={
            "display": "inline-block",
            "width": "10px", "height": "8px",
            "borderRadius": "2px",
            "marginRight": "2px",
            "background": cor if i < n_cheios else cor_vazio,
        }))
    return html.Div(blocos, style={"display": "flex", "alignItems": "center",
                                    "marginTop": "4px", "marginBottom": "2px"})


def _top_influenciadores(df_corr: pd.DataFrame, target: str, n: int = 5) -> list:
    """Retorna cards visuais com força e direção da correlação — legível por operadores."""
    if df_corr.empty or target not in df_corr.columns:
        return [html.Div("Sem dados", className="empty")]

    col = df_corr[target].dropna().sort_values(key=abs, ascending=False).head(n)
    items = []
    for var, r in col.items():
        forca, desc, cor = _forca_label(r)
        items.append(html.Div(style={
            "padding": "10px 12px", "borderRadius": "8px", "marginBottom": "8px",
            "background": P["surf"], "border": f"1px solid {P['border']}",
            "borderLeft": f"3px solid {cor}",
        }, children=[
            html.Div(style={"display": "flex", "justifyContent": "space-between",
                            "alignItems": "center"}, children=[
                html.Span(str(var)[:28], style={"fontSize": "0.8rem",
                                                 "fontWeight": 600, "color": P["text"]}),
                html.Span(forca, style={"fontSize": "0.65rem", "fontWeight": 700,
                                        "color": cor, "letterSpacing": "0.05em"}),
            ]),
            _barra_forca(r),
            html.Div(desc, style={"fontSize": "0.7rem", "color": P["muted2"]}),
        ]))
    return items


# ── componentes ───────────────────────────────────────────────────────────

def kpi_card(titulo: str, valor: str, sub: str = "", cor: str = P["accent"]) -> html.Div:
    return html.Div(style={
        "background": P["card"],
        "border": f"1px solid {P['border']}",
        "borderRadius": "10px",
        "padding": "16px 18px 14px",
        "boxShadow": "0 2px 12px rgba(0,0,0,0.4)",
        "display": "flex", "flexDirection": "column", "gap": "6px",
    }, children=[
        html.P(titulo, style={"margin": 0, "fontSize": "0.68rem", "color": P["muted"],
                               "textTransform": "uppercase", "letterSpacing": "0.08em",
                               "fontWeight": 500}),
        html.H3(valor, style={"margin": 0, "fontSize": "1.7rem",
                               "fontWeight": 700, "color": cor, "lineHeight": 1.1}),
        html.P(sub, style={"margin": 0, "fontSize": "0.71rem",
                            "color": P["muted"], "fontWeight": 400}) if sub else None,
        # barra de progresso decorativa
        html.Div(style={"marginTop": "8px", "height": "3px",
                        "borderRadius": "99px", "background": P["surf"]}, children=[
            html.Div(style={"width": "60%", "height": "100%",
                            "borderRadius": "99px", "background": cor,
                            "boxShadow": f"0 0 6px {cor}88"}),
        ]),
    ])


def badge(txt: str, cor: str) -> html.Span:
    return html.Span(txt, style={
        "display": "inline-block", "padding": "2px 9px",
        "borderRadius": "4px", "fontSize": "0.62rem", "fontWeight": 700,
        "background": f"{cor}22",
        "color": cor,
        "border": f"1px solid {cor}44",
        "letterSpacing": "0.05em",
    })


def section(titulo: str, *children, style_extra: dict | None = None) -> html.Div:
    base = {
        "background": P["card"],
        "border": f"1px solid {P['border']}",
        "borderRadius": "10px",
        "padding": "18px 20px",
        "boxShadow": "0 2px 12px rgba(0,0,0,0.4)",
    }
    if style_extra:
        base.update(style_extra)
    lbl = html.Div(style={"display": "flex", "alignItems": "center",
                           "gap": "8px", "marginBottom": "16px"}, children=[
        html.Div(style={"width": "3px", "height": "14px", "borderRadius": "2px",
                        "background": P["accent"],
                        "boxShadow": f"0 0 8px {P['accent']}"}),
        html.Span(titulo, style={
            "fontSize": "0.72rem", "fontWeight": 700, "color": P["text"],
            "textTransform": "uppercase", "letterSpacing": "0.1em",
        }),
    ])
    return html.Div(style=base, children=[lbl, *children])


# ── CSS ───────────────────────────────────────────────────────────────────

CSS = f"""
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:'Inter',system-ui,sans-serif;
  background:{P['bg']};color:{P['text']};
  font-size:14px;line-height:1.5;min-height:100vh;
}}
::-webkit-scrollbar{{width:4px;height:4px}}
::-webkit-scrollbar-track{{background:{P['surf']}}}
::-webkit-scrollbar-thumb{{background:rgba(255,255,255,0.15);border-radius:99px}}
::-webkit-scrollbar-thumb:hover{{background:rgba(255,255,255,0.28)}}

/* ── topbar ── */
.topbar{{
  display:flex;justify-content:space-between;align-items:center;
  padding:0 28px;height:72px;
  background:{P['surf']};
  border-bottom:1px solid {P['border']};
  position:sticky;top:0;z-index:100;
}}
.brand{{display:flex;align-items:center;gap:14px}}
.logo-img{{height:52px;width:auto;object-fit:contain;opacity:.95}}
.brand-name{{
  font-size:1.05rem;font-weight:700;color:{P['text']};letter-spacing:.01em;
}}
.brand-sub{{font-size:0.68rem;color:{P['muted']};margin-top:2px;letter-spacing:.04em}}
.pill{{
  display:inline-flex;align-items:center;gap:7px;padding:5px 14px;
  border-radius:6px;
  background:{P['card']};
  border:1px solid {P['border']};
  font-size:0.73rem;color:{P['muted']};
}}
.dot{{
  width:7px;height:7px;border-radius:50%;
  background:{P['ok']};
  box-shadow:0 0 6px {P['ok']}99;
  animation:pulse 2s infinite;
}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.file-sel-wrap{{
  display:flex;align-items:center;gap:8px;
  background:#1C2340;
  border:1px solid rgba(255,255,255,0.18);
  border-radius:8px;padding:5px 12px;
}}
.file-sel-label{{
  font-size:0.67rem;font-weight:600;color:#8892A4;
  text-transform:uppercase;letter-spacing:.08em;white-space:nowrap;
}}
.file-sel-wrap .Select-control{{
  background:#1C2340!important;border:none!important;
  min-height:26px!important;width:260px;
}}
/* todos os estados do valor selecionado */
.file-sel-wrap .Select-value-label,
.file-sel-wrap .Select--single .Select-value .Select-value-label,
.file-sel-wrap .Select-value span,
.file-sel-wrap .Select-value,
.file-sel-wrap .Select-multi-value-wrapper {{
  font-size:0.78rem!important;color:#FFFFFF!important;font-weight:500!important;
}}
.file-sel-wrap .Select-placeholder{{font-size:0.76rem!important;color:#8892A4!important}}
/* menu dropdown do seletor */
.file-sel-wrap .Select-menu-outer{{background:#1C2340!important;border-color:rgba(255,255,255,0.18)!important}}
.file-sel-wrap .Select-option{{background:#1C2340!important;color:#FFFFFF!important}}
.file-sel-wrap .Select-option:hover,.file-sel-wrap .Select-option.is-focused{{background:#252D4A!important;color:#00D4FF!important}}
/* input de busca */
.file-sel-wrap .Select-input input{{color:#FFFFFF!important;background:transparent!important;}}
.file-sel-wrap .Select-input{{color:#FFFFFF!important;}}

/* ── layout ── */
.page{{padding:16px 24px 36px;max-width:1920px;margin:0 auto}}
.kpi-row{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:14px}}
.main-grid{{display:grid;grid-template-columns:1fr 320px;gap:12px;align-items:start}}
.left-col{{display:flex;flex-direction:column;gap:12px}}
.right-col{{display:flex;flex-direction:column;gap:12px}}
.bottom-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}

/* ── cards ── */
.section-card{{
  background:{P['card']};
  border:1px solid {P['border']};
  border-radius:10px;padding:18px 20px;
  box-shadow:0 2px 12px rgba(0,0,0,.35);
}}

/* ── slider ── */
.rc-slider-rail{{background:rgba(255,255,255,0.1)!important;height:3px!important}}
.rc-slider-track{{background:{P['accent']}!important;height:3px!important}}
.rc-slider-handle{{
  border-color:{P['accent']}!important;background:{P['bg']}!important;
  width:13px!important;height:13px!important;margin-top:-5px!important;
  box-shadow:0 0 0 2px {P['accent']}55!important;
}}
.rc-slider-handle:hover,.rc-slider-handle:focus{{
  box-shadow:0 0 0 3px {P['accent']}77!important;
}}
.rc-slider-mark-text{{color:{P['muted']}!important;font-size:11px!important}}
.rc-slider-dot{{border-color:rgba(255,255,255,0.15)!important;background:{P['bg']}!important}}
.rc-slider-dot-active{{border-color:{P['accent']}!important}}

/* ── dropdown — cobertura total ── */
.Select,.Select.is-focused,.Select.is-open,.Select.has-value{{background:{P['surf']}!important}}
.Select-control,.Select.is-focused>.Select-control,.Select.is-open>.Select-control{{
  background:{P['surf']}!important;border-color:rgba(255,255,255,0.18)!important;
  border-radius:7px!important;min-height:34px!important;box-shadow:none!important;
}}
.Select-control:hover{{border-color:rgba(255,255,255,0.32)!important}}
.Select.is-open>.Select-control{{border-radius:7px 7px 0 0!important;border-color:{P['accent']}88!important}}
.Select-menu-outer{{
  background:{P['card']}!important;
  border:1px solid rgba(255,255,255,0.18)!important;border-top:none!important;
  border-radius:0 0 7px 7px!important;box-shadow:0 8px 32px rgba(0,0,0,.7)!important;z-index:9999!important;
}}
.Select-option{{background:{P['card']}!important;color:{P['text']}!important;font-size:13px!important;padding:8px 12px!important}}
.Select-option:hover,.Select-option.is-focused{{background:{P['surf']}!important;color:{P['accent']}!important}}
.Select-option.is-selected{{background:rgba(0,212,255,0.12)!important;color:{P['accent']}!important}}
.Select-value-label,.Select--single>.Select-control .Select-value{{color:{P['text']}!important;font-size:13px!important}}
.Select-placeholder{{color:{P['muted']}!important;font-size:13px!important}}
.Select-input,.Select-input>input{{background:transparent!important;color:{P['text']}!important}}
.Select-multi-value-wrapper{{gap:3px;padding:2px!important;background:{P['surf']}!important}}
.Select-value{{background:rgba(0,212,255,0.15)!important;border-color:rgba(0,212,255,0.3)!important;border-radius:5px!important}}
.Select-value-icon{{border-right-color:rgba(0,212,255,0.3)!important;color:{P['accent']}!important}}
.Select-arrow{{border-top-color:{P['muted']}!important}}
.dash-dropdown,.VirtualizedSelectOption,.VirtualizedSelectFocusedOption{{background:{P['surf']}!important;color:{P['text']}!important}}

/* ── nav tabs ── */
.nav-tabs{{
  display:flex;gap:0;padding:0 24px;
  background:{P['surf']};
  border-bottom:1px solid {P['border']};
}}
.nav-tab{{
  padding:14px 20px;font-size:0.73rem;font-weight:600;cursor:pointer;
  border:none;border-bottom:2px solid transparent;background:transparent;
  color:{P['muted']};transition:color .15s,border-color .15s;
  font-family:inherit;margin-bottom:-1px;letter-spacing:.06em;text-transform:uppercase;
}}
.nav-tab:hover{{color:{P['text']}}}
.nav-tab.on{{color:{P['accent']};border-bottom-color:{P['accent']}}}

/* ── tab buttons internos ── */
.tab-btn{{
  padding:5px 13px;border-radius:6px;font-size:0.71rem;font-weight:600;
  cursor:pointer;border:1px solid rgba(255,255,255,0.18);
  background:{P['surf']};
  color:{P['muted']};transition:all .15s;font-family:inherit;letter-spacing:.04em;
}}
.tab-btn:hover{{border-color:rgba(255,255,255,0.35);color:{P['text']};background:{P['card2']}}}
.tab-btn.on{{
  background:rgba(0,212,255,0.15);border-color:{P['accent']};
  color:{P['accent']};font-weight:700;
  box-shadow:0 0 10px rgba(0,212,255,0.2);
}}

/* ── alerts ── */
.alert-item{{padding:9px 0;border-bottom:1px solid {P['border']}}}
.alert-hd{{display:flex;align-items:center;gap:8px;margin-bottom:2px}}
.alert-dt{{font-size:0.71rem;color:{P['muted']}}}
.alerts-scroll{{max-height:360px;overflow-y:auto;padding-right:4px}}
.empty{{color:{P['muted']};font-size:.83rem;padding:24px 0;text-align:center}}

/* ── legend ── */
.legend{{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:8px}}
.leg-item{{display:flex;align-items:center;gap:5px;font-size:.71rem;color:{P['muted']}}}
.leg-box{{width:12px;height:8px;border-radius:2px;opacity:.85}}
.leg-line{{width:16px;height:2px;background:{P['crit']}}}

/* ── kpi card ── */
.kpi-card{{
  background:{P['card']};
  border:1px solid {P['border']};
  border-radius:10px;padding:16px 18px 14px;
  box-shadow:0 2px 12px rgba(0,0,0,.35);
  transition:border-color .15s;
}}
.kpi-card:hover{{border-color:rgba(255,255,255,0.14)}}

/* ── seção título com marcador vertical ── */
.sec-label-bar{{
  width:3px;height:14px;border-radius:2px;
  background:{P['accent']};flex-shrink:0;
}}

@media(max-width:1300px){{
  .main-grid{{grid-template-columns:1fr}}
  .right-col{{flex-direction:row;flex-wrap:wrap}}
  .right-col>*{{flex:1;min-width:260px}}
  .bottom-row{{grid-template-columns:1fr}}
}}
@media(max-width:800px){{
  .kpi-row{{grid-template-columns:repeat(2,1fr)}}
  .page{{padding:12px 14px 28px}}
  .topbar{{padding:0 14px}}
}}
"""

# ── app ───────────────────────────────────────────────────────────────────

def criar_app() -> Dash:
    app = Dash(__name__, title="Tissue Monitor")
    _fonts = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
    )
    app.index_string = app.index_string.replace(
        "</head>", f"{_fonts}<style>{CSS}</style></head>"
    )

    app.layout = html.Div(style={"background": P["bg"], "minHeight": "100vh",
                                   "position": "relative", "zIndex": 1}, children=[

        # fundo: gradiente radial com brilho ciano
        html.Div(style={
            "position":   "fixed",
            "top": 0, "left": 0, "right": 0, "bottom": 0,
            "background": (
                "radial-gradient(ellipse 80% 50% at 20% 0%, rgba(0,212,255,0.07) 0%, transparent 60%),"
                "radial-gradient(ellipse 60% 40% at 80% 100%, rgba(0,153,204,0.05) 0%, transparent 55%),"
                f"{P['bg']}"
            ),
            "zIndex":       -1,
            "pointerEvents":"none",
        }),

        dcc.Interval(id="poll", interval=POLL_MS, n_intervals=0),
        dcc.Store(id="sig"),
        dcc.Store(id="pkg"),
        dcc.Store(id="tab", data="processo"),
        dcc.Store(id="limites-store",  data=_carregar_limites()),
        dcc.Store(id="tipo-grafico",   data="serie"),
        dcc.Store(id="tipo-qual",      data="scatter"),
        dcc.Store(id="pq-snapshot",    data=None),
        dcc.Download(id="download-pdf"),

        # ── topo ─────────────────────────────────────────────────────────
        html.Div(className="topbar", children=[
            html.Div(className="brand", children=[
                html.Img(src="/assets/neve_care.png", className="logo-img"),
                html.Div([
                    html.Div("Tissue Monitor", className="brand-name"),
                    html.Div(id="meta", children="Aguardando dados...", className="brand-sub"),
                ]),
            ]),

            # seletor de arquivo
            html.Div(className="file-sel-wrap", children=[
                html.Span("Arquivo", className="file-sel-label"),
                dcc.Dropdown(
                    id="sel-arquivo",
                    clearable=False,
                    searchable=False,
                    placeholder="Nenhum arquivo encontrado",
                    style={"border": "none", "background": "transparent"},
                ),
            ]),

            html.Div(className="pill", children=[
                html.Span(className="dot"),
                html.Span(id="status", children="Inicializando"),
            ]),
        ]),

        # ── navegação principal ───────────────────────────────────────────
        html.Div(className="nav-tabs", children=[
            html.Button("Processo",    id="nav-processo",  className="nav-tab on", n_clicks=0),
            html.Button("Qualidade",   id="nav-qualidade", className="nav-tab",    n_clicks=0),
            html.Button("Downtime",    id="nav-downtime",  className="nav-tab",    n_clicks=0),
            html.Button("Processo × Qualidade", id="nav-pq", className="nav-tab", n_clicks=0),
            html.Button("Temp. Yankee", id="nav-yankee",  className="nav-tab",    n_clicks=0),
            html.Button("Relatórios",   id="nav-relatorio", className="nav-tab",  n_clicks=0),
        ]),

        dcc.Store(id="nav-ativa", data="processo"),

        # ══ ABA PROCESSO ══════════════════════════════════════════════════
        html.Div(id="aba-processo", className="page", children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="k0"), html.Div(id="k1"), html.Div(id="k2"),
                html.Div(id="k3"), html.Div(id="k4"),
            ]),

            html.Div(className="main-grid", children=[

                html.Div(className="left-col", children=[

                    section("Período de análise",
                        html.Div(id="slider-periodo-label", style={
                            "fontSize": "0.8rem", "color": P["accent"],
                            "marginBottom": "12px", "fontWeight": 600,
                        }),
                        dcc.RangeSlider(
                            id="slider-periodo",
                            min=0, max=1, value=[0, 1],
                            step=None, marks={},
                            tooltip={"placement": "bottom", "always_visible": False},
                            allowCross=False,
                        ),
                        html.Div(style={"marginTop": "8px", "display": "flex",
                                        "justifyContent": "space-between",
                                        "fontSize": "0.72rem", "color": P["muted2"]}, children=[
                            html.Span(id="slider-label-ini"),
                            html.Span(id="slider-label-fim"),
                        ]),
                    ),

                    section("Tendência dos parâmetros",
                        # seletor de tipo de gráfico
                        html.Div(style={"display": "flex", "gap": "6px", "marginBottom": "12px"}, children=[
                            html.Button("Série temporal",   id="btn-tipo-serie",   className="tab-btn on", n_clicks=0),
                            html.Button("Scatter matrix",   id="btn-tipo-scatter", className="tab-btn",    n_clicks=0),
                            html.Button("Cross-correlação", id="btn-tipo-cross",   className="tab-btn",    n_clicks=0),
                        ]),
                        # opções extras para cross-correlação (ocultas por padrão)
                        html.Div(id="crosscorr-opts", style={"display": "none", "marginBottom": "12px"}, children=[
                            html.Div("Parâmetro B — resultado / variável dependente", style={
                                "fontSize": "0.69rem", "color": P["muted2"], "fontWeight": 600,
                                "textTransform": "uppercase", "marginBottom": "6px",
                            }),
                            dcc.Dropdown(id="crosscorr-p2", clearable=False,
                                         placeholder="Selecione o parâmetro B...",
                                         style={"fontSize": "13px"}),
                            html.Div(
                                "Lag positivo = Param A (sel. acima) precede Param B no tempo",
                                style={"fontSize": "0.69rem", "color": P["muted2"], "marginTop": "5px"},
                            ),
                        ]),
                        # legenda (oculta no modo scatter/cross)
                        html.Div(id="trend-legend", className="legend", children=[
                            html.Div(className="leg-item", children=[
                                html.Div(className="leg-box", style={"background": P["stop"]}),
                                "Parada de máquina",
                            ]),
                            html.Div(className="leg-item", children=[
                                html.Div(className="leg-line"),
                                "Outlier de processo",
                            ]),
                            html.Div(className="leg-item", children=[
                                html.Div(style={"width":"18px","height":"0","borderTop":f"2px dashed {P['ok']}"}),
                                "LI",
                            ]),
                            html.Div(className="leg-item", children=[
                                html.Div(style={"width":"18px","height":"0","borderTop":f"2px dashed {P['crit']}"}),
                                "LS",
                            ]),
                        ]),
                        dcc.Graph(id="g-trend",
                                  config={"displayModeBar": True,
                                          "modeBarButtonsToRemove": ["lasso2d","select2d"],
                                          "toImageButtonOptions": {"format": "png", "scale": 2}},
                                  figure=_empty_fig("Selecione parâmetros para visualizar", 360)),
                    ),

                    section("Distribuição no período",
                        dcc.Graph(id="g-box", config={"displayModeBar": False},
                                  figure=_empty_fig("Selecione parâmetros", 280)),
                    ),
                ]),

                html.Div(className="right-col", children=[

                    section("Parâmetros monitorados",
                        dcc.Dropdown(id="sel", multi=True,
                                     placeholder="Escolha os parâmetros...",
                                     style={"fontSize": "13px"}),
                        html.Div(id="sel-info", style={
                            "marginTop": "10px", "fontSize": "0.72rem", "color": P["muted2"],
                        }),
                        # ── editor de limites ──────────────────────────────
                        html.Div(style={
                            "display": "flex", "justifyContent": "space-between",
                            "alignItems": "center", "marginTop": "14px",
                            "paddingTop": "12px", "borderTop": f"1px solid {P['border']}",
                        }, children=[
                            html.Span("Limites de controle", style={
                                "fontSize": "0.72rem", "fontWeight": 600, "color": P["muted2"],
                                "textTransform": "uppercase", "letterSpacing": "0.06em",
                            }),
                            html.Button("▾ Editar", id="btn-toggle-limites",
                                        className="tab-btn", n_clicks=0),
                        ]),
                        html.Div(id="limites-wrap", style={"display": "none"}, children=[
                            html.Div(id="limites-editor", style={"marginTop": "10px"}),
                            html.Div(style={"display": "flex", "gap": "10px",
                                            "alignItems": "center", "marginTop": "10px"}, children=[
                                html.Button("Salvar", id="btn-salvar-limites",
                                            className="tab-btn on", n_clicks=0),
                                html.Span(id="limites-status", style={
                                    "fontSize": "0.72rem", "color": P["ok"],
                                }),
                            ]),
                        ]),
                    ),

                    section("Correlação no período",
                        html.Div(id="corr-info", style={
                            "fontSize": "0.72rem", "color": P["muted2"], "marginBottom": "8px",
                        }),
                        dcc.Graph(id="g-corr", config={"displayModeBar": False},
                                  figure=_empty_fig("Selecione 2+ parâmetros", 380)),
                    ),

                    section("Alertas",
                        html.Div(style={"display": "flex", "gap": "8px", "marginBottom": "12px"}, children=[
                            html.Button("Processo",  id="t-proc",   className="tab-btn on", n_clicks=0),
                            html.Button("Paradas",   id="t-stop",   className="tab-btn",    n_clicks=0),
                            html.Button("Sem dados", id="t-nodata", className="tab-btn",    n_clicks=0),
                        ]),
                        html.Div(id="alert-sum", style={"fontSize": "0.72rem", "color": P["muted2"],
                                                         "marginBottom": "10px"}),
                        html.Div(id="alert-list", className="alerts-scroll"),
                    ),
                ]),
            ]),
        ]),

        # ══ ABA QUALIDADE ═════════════════════════════════════════════════
        html.Div(id="aba-qualidade", className="page", style={"display": "none"}, children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="qk0"), html.Div(id="qk1"), html.Div(id="qk2"),
                html.Div(id="qk3"), html.Div(id="qk4"),
            ]),

            html.Div(style={"display": "flex", "alignItems": "center", "gap": "10px",
                            "padding": "6px 0 10px 0"}, children=[
                html.Span("Produto:", style={"fontSize": "0.8rem", "fontWeight": 600,
                                              "color": P["muted2"], "whiteSpace": "nowrap"}),
                dcc.Dropdown(
                    id="sel-qual-produto",
                    options=[{"label": "Todos", "value": "__todos__"}],
                    value="__todos__",
                    clearable=False,
                    style={"fontSize": "13px", "minWidth": "260px", "background": P["surf"]},
                ),
            ]),

            html.Div(className="main-grid", children=[

                html.Div(className="left-col", children=[

                    section("Parâmetro de qualidade",
                        html.Div(style={"display": "flex", "gap": "6px", "marginBottom": "12px"}, children=[
                            html.Button("Carta IMR",       id="btn-qual-imr",     className="tab-btn on", n_clicks=0),
                            html.Button("Dispersão temporal", id="btn-qual-scatter", className="tab-btn", n_clicks=0),
                        ]),
                        dcc.Dropdown(
                            id="sel-qual-param",
                            options=[{"label": p, "value": p} for p in PARAMS_QUALIDADE_PRINCIPAIS],
                            value="Espessura",
                            clearable=False,
                            style={"fontSize": "13px", "marginBottom": "14px", "background": P["surf"]},
                        ),
                        dcc.Graph(id="g-qual-param", config={"displayModeBar": False},
                                  figure=_empty_fig("Carregando...", 430)),
                    ),

                    section("Conformidade por produto",
                        dcc.Graph(id="g-conf-produto", config={"displayModeBar": False},
                                  figure=_empty_fig("Carregando...", 280)),
                    ),

                    section("Pareto — resultado × quantidade",
                        dcc.Graph(id="g-qual-pareto", config={"displayModeBar": False},
                                  figure=_empty_fig("Selecione um parâmetro", 300)),
                    ),
                ]),

                html.Div(className="right-col", children=[

                    section("Resumo de conformidade",
                        html.Div(id="qual-resumo"),
                    ),

                    section("Jumbos fora de especificação",
                        html.Div(id="qual-alertas", className="alerts-scroll"),
                    ),
                ]),
            ]),
        ]),

        # ══ ABA DOWNTIME ══════════════════════════════════════════════════
        html.Div(id="aba-downtime", className="page", style={"display": "none"}, children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="dk0"), html.Div(id="dk1"), html.Div(id="dk2"),
                html.Div(id="dk3"), html.Div(id="dk4"),
            ]),

            html.Div(style={"display": "flex", "flexDirection": "column", "gap": "14px"}, children=[

                section("Produção × Downtime — visão semanal",
                    dcc.Graph(id="g-dt-semanal", config={"displayModeBar": False},
                              figure=_empty_fig("Carregando...", 360)),
                ),

                html.Div(className="main-grid",
                         style={"gridTemplateColumns": "1.4fr 1fr"},
                         children=[

                    html.Div(className="left-col", children=[
                        section("Pareto de causas",
                            dcc.Graph(id="g-dt-pareto", config={"displayModeBar": False},
                                      figure=_empty_fig("Carregando...", 380)),
                        ),
                        section("Top 3 paradas do mês",
                            html.Div(id="dt-top3"),
                        ),
                    ]),

                    html.Div(className="right-col", children=[
                        section(html.Span([
                            html.Span("🔴 ", style={"fontSize": "0.9rem"}),
                            "Manutenção / Paradas / PPR",
                        ]),
                            html.Div(id="dt-lista-criticas", className="alerts-scroll"),
                        ),

                        section(html.Span([
                            html.Span("🟡 ", style={"fontSize": "0.9rem"}),
                            "Limpeza Programada",
                        ]),
                            html.Div(id="dt-lista-lmp", className="alerts-scroll"),
                        ),
                    ]),
                ]),
            ]),
        ]),

        # ══ ABA PROCESSO × QUALIDADE ══════════════════════════════════════
        html.Div(id="aba-pq", className="page", style={"display": "none"}, children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="pqk0"), html.Div(id="pqk1"),
                html.Div(id="pqk2"), html.Div(id="pqk3"), html.Div(id="pqk4"),
            ]),

            html.Div(style={"display": "flex", "gap": "10px", "alignItems": "center",
                            "padding": "8px 0", "marginBottom": "4px"}, children=[
                dcc.Input(id="pq-obs", type="text", placeholder="Observação (opcional)…",
                          style={"flex": 1, "padding": "6px 10px", "borderRadius": "6px",
                                 "border": f"1px solid {P['border']}", "fontSize": "13px",
                                 "background": P["surf"], "color": P["text"]}),
                html.Button("Salvar análise", id="pq-btn-salvar", n_clicks=0,
                            style={"padding": "6px 16px", "borderRadius": "6px",
                                   "background": P["accent"], "color": "#fff",
                                   "border": "none", "cursor": "pointer", "fontWeight": 600,
                                   "fontSize": "13px"}),
                html.Span(id="pq-salvar-msg", style={"fontSize": "0.8rem", "color": P["ok"]}),
            ]),

            html.Div(className="main-grid", children=[

                html.Div(className="left-col", children=[

                    section("Mapa de correlação — Processo × Qualidade",
                        # guia de leitura
                        html.Div(style={
                            "display": "flex", "gap": "16px", "flexWrap": "wrap",
                            "padding": "10px 12px", "borderRadius": "8px",
                            "background": P["surf"], "border": f"1px solid {P['border']}",
                            "marginBottom": "12px",
                        }, children=[
                            html.Div(style={"fontSize": "0.72rem", "color": P["muted2"],
                                            "fontWeight": 600, "alignSelf": "center",
                                            "whiteSpace": "nowrap"},
                                     children="Como ler:"),
                            html.Div(style={"display": "flex", "alignItems": "center",
                                            "gap": "6px"}, children=[
                                html.Div(style={"width": "24px", "height": "12px",
                                                "borderRadius": "3px",
                                                "background": "#3b82f6"}),
                                html.Span("Azul = sobe junto",
                                          style={"fontSize": "0.72rem", "color": P["text"]}),
                            ]),
                            html.Div(style={"display": "flex", "alignItems": "center",
                                            "gap": "6px"}, children=[
                                html.Div(style={"width": "24px", "height": "12px",
                                                "borderRadius": "3px",
                                                "background": "#dc2626"}),
                                html.Span("Vermelho = efeito oposto",
                                          style={"fontSize": "0.72rem", "color": P["text"]}),
                            ]),
                            html.Div(style={"display": "flex", "alignItems": "center",
                                            "gap": "6px"}, children=[
                                html.Div(style={"width": "24px", "height": "12px",
                                                "borderRadius": "3px",
                                                "background": "#e2e8f0"}),
                                html.Span("Branco = sem relação",
                                          style={"fontSize": "0.72rem", "color": P["text"]}),
                            ]),
                            html.Div(style={"fontSize": "0.72rem", "color": P["muted2"],
                                            "borderLeft": f"1px solid {P['border']}",
                                            "paddingLeft": "12px"},
                                     children="Intensidade: >0,6 forte · 0,3–0,6 moderada · <0,3 fraca"),
                        ]),
                        dcc.Graph(id="g-pq-heatmap", config={"displayModeBar": False},
                                  figure=_empty_fig("Carregando...", 420)),
                    ),

                    section("Scatter — detalhe da correlação",
                        html.Div(style={"display": "flex", "gap": "10px", "marginBottom": "12px"}, children=[
                            html.Div(style={"flex": 1}, children=[
                                html.Div("Variável de processo", style={"fontSize": "0.69rem",
                                         "color": P["muted2"], "fontWeight": 600,
                                         "textTransform": "uppercase", "marginBottom": "6px"}),
                                dcc.Dropdown(id="pq-sel-var", clearable=False,
                                             style={"fontSize": "13px", "background": P["surf"], "color": P["text"]}),
                            ]),
                            html.Div(style={"flex": 1}, children=[
                                html.Div("Target de qualidade", style={"fontSize": "0.69rem",
                                         "color": P["muted2"], "fontWeight": 600,
                                         "textTransform": "uppercase", "marginBottom": "6px"}),
                                dcc.Dropdown(
                                    id="pq-sel-target",
                                    options=[{"label": t, "value": t} for t in TARGETS_PQ + ["Quebras", "Alongamento"]],
                                    value="Espessura", clearable=False,
                                    style={"fontSize": "13px", "background": P["surf"], "color": P["text"]},
                                ),
                            ]),
                        ]),
                        dcc.Graph(id="g-pq-scatter", config={"displayModeBar": False},
                                  figure=_empty_fig("Selecione variável e target", 300)),
                    ),
                ]),

                html.Div(className="right-col", children=[

                    section("Top influenciadores — Espessura",
                        html.Div(id="pq-inf-espessura"),
                    ),

                    section("Top influenciadores — Handfeel",
                        html.Div(id="pq-inf-handfeel"),
                    ),

                    section("Top influenciadores — Maciez TSA",
                        html.Div(id="pq-inf-maciez"),
                    ),

                    section("Top influenciadores — Umidade Lab",
                        html.Div(id="pq-inf-umidade"),
                    ),

                    section("Top influenciadores — Umidade QCS",
                        html.Div(id="pq-inf-umidadeqcs"),
                    ),

                    section("Top influenciadores — Quebras",
                        html.Div(id="pq-inf-quebras"),
                    ),
                ]),
            ]),
        ]),
        # ══ ABA TEMPERATURA YANKEE ════════════════════════════════════════
        html.Div(id="aba-yankee", className="page", style={"display": "none"}, children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="yk0"), html.Div(id="yk1"),
                html.Div(id="yk2"), html.Div(id="yk3"), html.Div(id="yk4"),
            ]),

            html.Div(style={"display": "grid", "gridTemplateColumns": "420px 1fr",
                            "gap": "14px", "alignItems": "start"}, children=[

                # ── formulário de registro ────────────────────────────────
                section("Registrar leitura manual",
                    html.Div(style={"fontSize": "0.72rem", "color": P["muted2"],
                                    "marginBottom": "14px"},
                             children="Preencha a cada 2h. Valores em °C."),

                    html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr 1fr",
                                    "gap": "10px", "marginBottom": "12px"}, children=[
                        html.Div([
                            html.Div("LA — Acionamento (°C)", style={
                                "fontSize": "0.69rem", "fontWeight": 600,
                                "color": P["muted2"], "textTransform": "uppercase",
                                "marginBottom": "6px",
                            }),
                            dcc.Input(id="yt-la", type="number", placeholder="ex: 92.5",
                                      min=60, max=130, step=0.1,
                                      style={"width": "100%", "padding": "8px 10px",
                                             "fontSize": "1rem", "fontWeight": 600,
                                             "border": f"1px solid {P['border']}",
                                             "borderRadius": "8px", "background": P["card"],
                                             "color": P["text"]}),
                        ]),
                        html.Div([
                            html.Div("Meio — Centro (°C)", style={
                                "fontSize": "0.69rem", "fontWeight": 600,
                                "color": P["lines"][1], "textTransform": "uppercase",
                                "marginBottom": "6px",
                            }),
                            dcc.Input(id="yt-meio", type="number", placeholder="ex: 93.0",
                                      min=60, max=130, step=0.1,
                                      style={"width": "100%", "padding": "8px 10px",
                                             "fontSize": "1rem", "fontWeight": 600,
                                             "border": f"1px solid {P['border']}",
                                             "borderRadius": "8px", "background": P["card"],
                                             "color": P["text"]}),
                        ]),
                        html.Div([
                            html.Div("LC — Comando (°C)", style={
                                "fontSize": "0.69rem", "fontWeight": 600,
                                "color": P["muted2"], "textTransform": "uppercase",
                                "marginBottom": "6px",
                            }),
                            dcc.Input(id="yt-lc", type="number", placeholder="ex: 91.0",
                                      min=60, max=130, step=0.1,
                                      style={"width": "100%", "padding": "8px 10px",
                                             "fontSize": "1rem", "fontWeight": 600,
                                             "border": f"1px solid {P['border']}",
                                             "borderRadius": "8px", "background": P["card"],
                                             "color": P["text"]}),
                        ]),
                    ]),

                    html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr",
                                    "gap": "10px", "marginBottom": "12px"}, children=[
                        html.Div([
                            html.Div("Operador", style={
                                "fontSize": "0.69rem", "fontWeight": 600,
                                "color": P["muted2"], "textTransform": "uppercase",
                                "marginBottom": "6px",
                            }),
                            dcc.Input(id="yt-op", type="text", placeholder="Nome do operador",
                                      maxLength=40,
                                      style={"width": "100%", "padding": "8px 10px",
                                             "fontSize": "0.9rem",
                                             "border": f"1px solid {P['border']}",
                                             "borderRadius": "8px", "background": P["card"],
                                             "color": P["text"]}),
                        ]),
                        html.Div([
                            html.Div("Memo / Observação", style={
                                "fontSize": "0.69rem", "fontWeight": 600,
                                "color": P["muted2"], "textTransform": "uppercase",
                                "marginBottom": "6px",
                            }),
                            dcc.Input(id="yt-memo", type="text",
                                      placeholder="ex: após limpeza, início de turno...",
                                      maxLength=120,
                                      style={"width": "100%", "padding": "8px 10px",
                                             "fontSize": "0.9rem",
                                             "border": f"1px solid {P['border']}",
                                             "borderRadius": "8px", "background": P["card"],
                                             "color": P["text"]}),
                        ]),
                    ]),

                    html.Div(style={"display": "flex", "gap": "10px", "alignItems": "center"}, children=[
                        html.Button("Registrar", id="yt-btn", n_clicks=0,
                                    style={
                                        "padding": "9px 20px", "borderRadius": "8px",
                                        "border": "none", "cursor": "pointer",
                                        "background": P["accent"], "color": "#fff",
                                        "fontSize": "0.85rem", "fontWeight": 600,
                                        "fontFamily": "inherit",
                                    }),
                        html.Span(id="yt-msg", style={"fontSize": "0.8rem", "color": P["ok"]}),
                    ]),
                ),

                # ── histórico e gráfico ───────────────────────────────────
                html.Div(style={"display": "flex", "flexDirection": "column", "gap": "14px"}, children=[

                    section("Tendência de temperatura — últimos 30 dias",
                        dcc.Graph(id="g-yankee-trend", config={"displayModeBar": False},
                                  figure=_empty_fig("Sem leituras registradas", 300)),
                    ),

                    section("Últimas leituras",
                        html.Div(id="yankee-lista", className="alerts-scroll"),
                    ),
                ]),
            ]),
        ]),

        # ══ ABA RELATÓRIOS ════════════════════════════════════════════════
        html.Div(id="aba-relatorio", className="page", style={"display": "none"}, children=[

            html.Div(className="kpi-row", children=[
                html.Div(id="rk0"), html.Div(id="rk1"),
                html.Div(id="rk2"), html.Div(id="rk3"), html.Div(id="rk4"),
            ]),

            html.Div(style={"display": "grid", "gridTemplateColumns": "1fr 1fr",
                            "gap": "14px", "alignItems": "start"}, children=[

                html.Div(style={"display": "flex", "flexDirection": "column", "gap": "14px"}, children=[

                    section("Comparar por período",
                        html.Div(style={"display": "flex", "gap": "14px", "alignItems": "center",
                                        "flexWrap": "wrap", "marginBottom": "10px"}, children=[
                            html.Div(style={"display": "flex", "gap": "6px"}, children=[
                                html.Button("Semana",  id="rel-freq-w",  n_clicks=0,
                                            className="nav-tab on",
                                            style={"fontSize": "0.8rem", "padding": "4px 12px"}),
                                html.Button("Mês",     id="rel-freq-ms", n_clicks=0,
                                            className="nav-tab",
                                            style={"fontSize": "0.8rem", "padding": "4px 12px"}),
                            ]),
                            dcc.Dropdown(
                                id="rel-target-drop",
                                options=[{"label": t, "value": t}
                                         for t in ["Espessura", "Handfeel", "Maciez TSA",
                                                   "Umidade", "Quebras"]],
                                value="Espessura",
                                clearable=False,
                                style={"fontSize": "13px", "width": "200px", "background": P["surf"]},
                                placeholder="Target…",
                            ),
                        ]),
                        dcc.Graph(id="g-corr-evolucao", config={"displayModeBar": False},
                                  figure=_empty_fig("Selecione frequência e target", 360)),
                    ),

                ]),

                html.Div(style={"display": "flex", "flexDirection": "column", "gap": "14px"}, children=[

                    section("Salvar análise atual (Processo × Qualidade)",
                        html.Div(style={"display": "flex", "flexDirection": "column", "gap": "8px"}, children=[
                            html.Div(style={"fontSize": "0.72rem", "color": P["muted2"]},
                                     children="Salva o snapshot de correlações da aba Processo × Qualidade."),
                            html.Div(style={"display": "flex", "gap": "8px"}, children=[
                                dcc.Input(id="rel-obs", type="text",
                                          placeholder="Observação…",
                                          style={"flex": 1, "padding": "6px 10px",
                                                 "borderRadius": "6px",
                                                 "border": f"1px solid {P['border']}",
                                                 "fontSize": "13px",
                                                 "background": P["surf"], "color": P["text"]}),
                                html.Button("Salvar", id="rel-btn-salvar", n_clicks=0,
                                            style={"padding": "6px 16px", "borderRadius": "6px",
                                                   "background": P["accent"], "color": "#fff",
                                                   "border": "none", "cursor": "pointer",
                                                   "fontWeight": 600, "fontSize": "13px"}),
                            ]),
                            html.Span(id="rel-salvar-msg",
                                      style={"fontSize": "0.8rem", "color": P["ok"]}),
                        ]),
                    ),

                    section("Gerar PDF",
                        html.Div(style={"display": "flex", "flexDirection": "column", "gap": "8px"}, children=[
                            html.Div(style={"fontSize": "0.72rem", "color": P["muted2"]},
                                     children="Exporta: correlações atuais, histórico e evolução semana/mês."),
                            html.Button("Baixar PDF", id="btn-gerar-pdf", n_clicks=0,
                                        style={"padding": "8px 20px", "borderRadius": "6px",
                                               "background": P["accent2"], "color": "#fff",
                                               "border": "none", "cursor": "pointer",
                                               "fontWeight": 600, "fontSize": "13px",
                                               "width": "fit-content"}),
                        ]),
                    ),

                ]),
            ]),

            section("Análises salvas",
                html.Div(id="rel-historico-tabela"),
            ),
        ]),
    ])

    # ── callbacks ─────────────────────────────────────────────────────────

    # ── 1. a cada poll: atualiza lista de arquivos no dropdown ───────────
    @callback(
        Output("sel-arquivo", "options"),
        Output("sel-arquivo", "value"),
        Input("poll",         "n_intervals"),
        State("sel-arquivo",  "value"),
    )
    def poll_lista(_, sel_atual):
        opts = _opcoes_arquivo(BASE_DIR)
        # Adiciona opção do banco se houver dados disponíveis
        try:
            from db import _conn as _db_conn
            with _db_conn() as _c:
                _cur = _c.cursor()
                _cur.execute("SELECT COUNT(1) FROM dados_processo")
                _n = _cur.fetchone()[0]
            if _n > 0:
                opts = [{"label": f"Banco de dados — {_n:,} registros", "value": _DB_PROC}] + opts
        except Exception as _e:
            print(f"[PROCESSO] erro ao consultar banco: {_e}", flush=True)
        if not opts:
            return [], no_update
        value_out = opts[0]["value"] if sel_atual is None else no_update
        return opts, value_out

    # ── 2. carrega pkg quando arquivo selecionado muda ou é modificado ───
    @callback(
        Output("sig",    "data"),
        Output("pkg",    "data"),
        Output("meta",   "children"),
        Output("status", "children"),
        Input("sel-arquivo", "value"),   # usuário trocou de arquivo
        Input("poll",        "n_intervals"),  # verifica se arquivo atual foi modificado
        State("sig",         "data"),
    )
    def carregar_arquivo(sel, _, sig_cur):
        if not sel:
            return no_update, no_update, "Nenhum arquivo selecionado", "Aguardando"

        # Carrega do banco de dados
        if sel == _DB_PROC:
            if ctx.triggered_id == "poll" and sig_cur == _DB_PROC:
                return no_update, no_update, no_update, no_update
            try:
                df_proc = _cproc_db(dias=365)
            except Exception as e:
                return sig_cur, no_update, f"Erro ao carregar banco: {e}", "Erro"
            if df_proc.empty:
                return sig_cur, no_update, "Banco sem dados de processo", "Sem dados"
            try:
                pkg = _carregar_pacote_de_df(df_proc, "Banco de dados — 90 dias")
            except Exception as e:
                return sig_cur, no_update, f"Erro ao processar dados: {e}", "Erro"
            return _DB_PROC, pkg, f"Banco de dados  ·  {len(df_proc):,} registros", "Conectado"

        p = Path(sel)
        if not p.exists():
            return no_update, no_update, f"Arquivo não encontrado: {p.name}", "Erro"

        new_sig = _sig(p)

        # Se disparado pelo poll e assinatura não mudou, não recarrega
        if ctx.triggered_id == "poll" and new_sig == sig_cur:
            return no_update, no_update, no_update, no_update

        try:
            pkg = carregar_pacote(p)
        except Exception as e:
            return sig_cur, no_update, f"Erro ao ler {p.name}: {e}", "Erro"

        status = "Atualizado" if sig_cur else "Conectado"
        meta   = f"{p.name}  ·  {pkg['carregado']}"
        return new_sig, pkg, meta, status

    # ── após pkg carregar: atualiza KPIs, dropdown e slider ──────────────
    @callback(
        Output("k0", "children"), Output("k1", "children"),
        Output("k2", "children"), Output("k3", "children"),
        Output("k4", "children"),
        Output("sel",              "options"),
        Output("sel",              "value"),
        Output("crosscorr-p2",    "options"),
        Output("slider-periodo",  "min"),
        Output("slider-periodo",  "max"),
        Output("slider-periodo",  "value"),
        Output("slider-periodo",  "marks"),
        Input("pkg", "data"),
    )
    def kpis(pkg):
        vz = kpi_card("—", "—")
        if not pkg:
            return vz, vz, vz, vz, vz, [], [], [], 0, 1, [0, 1], {}
        opts = [{"label": p, "value": p} for p in pkg["params"]]
        tmin, tmax = pkg["ts_min"], pkg["ts_max"]
        return (
            kpi_card("Período", pkg["periodo_ini"], pkg["periodo_fim"], P["accent"]),
            kpi_card("Registros", f"{pkg['registros']:,}".replace(",","."), "leituras", P["accent2"]),
            kpi_card("Parâmetros", f"{pkg['n_ativos']}/{pkg['n_total']}", "ativos", P["ok"]),
            kpi_card("Eventos", str(pkg["n_eventos"]), "anomalias",
                     P["crit"] if pkg["n_criticos"] else P["warn"] if pkg["n_eventos"] else P["ok"]),
            kpi_card("Paradas", str(pkg["n_paradas"]), f"{pkg['h_parada']:.1f}h total", P["stop"]),
            opts,
            pkg["default"],
            opts,
            tmin, tmax,
            [tmin, tmax],
            pkg["slider_marks"],
        )

    # ── atualiza label do slider ──────────────────────────────────────────
    @callback(
        Output("slider-periodo-label", "children"),
        Output("slider-label-ini",     "children"),
        Output("slider-label-fim",     "children"),
        Input("slider-periodo", "value"),
    )
    def label_slider(val):
        if not val or len(val) < 2:
            return "", "", ""
        ini = datetime.fromtimestamp(val[0]).strftime("%d/%m/%Y %H:%M")
        fim = datetime.fromtimestamp(val[1]).strftime("%d/%m/%Y %H:%M")
        duracao = (val[1] - val[0]) / 3600
        return f"{ini}  →  {fim}  ({duracao:.1f}h)", ini, fim

    # ── info do seletor de parâmetros ─────────────────────────────────────
    @callback(
        Output("sel-info", "children"),
        Input("sel", "value"),
        State("pkg", "data"),
    )
    def sel_info(params, pkg):
        if not params or not pkg:
            return "Nenhum parâmetro selecionado"
        n = len(params)
        total = pkg.get("registros", 0)
        return f"{n} parâmetro{'s' if n > 1 else ''} selecionado{'s' if n > 1 else ''}  ·  {total:,} registros".replace(",",".")

    # ── gráficos: disparados por sel, slider, limites e tipo ─────────────
    @callback(
        Output("g-trend",   "figure"),
        Output("g-corr",    "figure"),
        Output("g-box",     "figure"),
        Output("corr-info", "children"),
        Input("sel",            "value"),
        Input("slider-periodo", "value"),
        Input("limites-store",  "data"),
        Input("tipo-grafico",   "data"),
        Input("crosscorr-p2",   "value"),
        State("pkg",            "data"),
    )
    def graficos(params, periodo, limites, tipo, p2, pkg):
        empty_t = _empty_fig("Selecione parâmetros para visualizar", 360)
        empty_c = _empty_fig("Selecione 2+ parâmetros", 380)
        empty_b = _empty_fig("Selecione parâmetros", 280)

        if not pkg or not params:
            return empty_t, empty_c, empty_b, ""

        # carrega dados
        dados    = pd.read_json(StringIO(pkg["dados_json"]), orient="split")
        dados["timestamp"] = pd.to_datetime(dados["timestamp"])
        medias   = pd.read_json(StringIO(pkg["medias_json"]), typ="series")
        paradas  = pd.read_json(StringIO(pkg["paradas_json"]), orient="records")
        outliers = pd.read_json(StringIO(pkg["outliers_json"]), orient="records")

        if not paradas.empty:
            paradas["inicio"] = pd.to_datetime(paradas["inicio"])
            paradas["fim"]    = pd.to_datetime(paradas["fim"])
        if not outliers.empty:
            outliers["timestamp"] = pd.to_datetime(outliers["timestamp"])

        # filtra pelo slider
        if periodo and len(periodo) == 2:
            ts_ini = pd.Timestamp(periodo[0], unit="s")
            ts_fim = pd.Timestamp(periodo[1], unit="s")
            mask   = (dados["timestamp"] >= ts_ini) & (dados["timestamp"] <= ts_fim)
            dados  = dados[mask].reset_index(drop=True)
            if not outliers.empty:
                om       = (outliers["timestamp"] >= ts_ini) & (outliers["timestamp"] <= ts_fim)
                outliers = outliers[om]
            if not paradas.empty:
                pm      = (paradas["fim"] >= ts_ini) & (paradas["inicio"] <= ts_fim)
                paradas = paradas[pm]

        if dados.empty:
            return empty_t, empty_c, empty_b, "Nenhum dado no período"

        # label da correlação
        n      = len(dados)
        ini_s  = dados["timestamp"].iloc[0].strftime("%d/%m %H:%M")
        fim_s  = dados["timestamp"].iloc[-1].strftime("%d/%m %H:%M")
        c_info = f"{ini_s} → {fim_s}  ·  {n:,} registros  ·  {len(params)} parâmetros".replace(",",".")

        tipo = tipo or "serie"
        if tipo == "scatter":
            g = fig_scatter_matrix(dados, params)
        elif tipo == "cross":
            p1 = params[0] if params else None
            g  = fig_crosscorr(dados, p1, p2)
        else:
            g = fig_trend(dados, params, medias, paradas, outliers, limites or {})

        return g, fig_corr(dados, params), fig_stats(dados, params), c_info

    # ── abas de alertas ───────────────────────────────────────────────────
    @callback(
        Output("tab",     "data"),
        Output("t-proc",  "className"),
        Output("t-stop",  "className"),
        Output("t-nodata","className"),
        Input("t-proc",   "n_clicks"),
        Input("t-stop",   "n_clicks"),
        Input("t-nodata", "n_clicks"),
    )
    def trocar_tab(*_):
        mapa = {"t-proc": "processo", "t-stop": "parada", "t-nodata": "sem_dados"}
        ativo = mapa.get(ctx.triggered_id, "processo")
        cls = lambda k: "tab-btn on" if mapa.get(k) == ativo else "tab-btn"
        return ativo, cls("t-proc"), cls("t-stop"), cls("t-nodata")

    @callback(
        Output("alert-sum",  "children"),
        Output("alert-list", "children"),
        Input("pkg",         "data"),
        Input("tab",         "data"),
    )
    def alertas(pkg, filtro):
        if not pkg:
            return "", html.Div("Sem dados.", className="empty")
        al   = pkg.get("alertas", [])
        proc = [a for a in al if a["tipo"] == "processo"]
        stop = [a for a in al if a["tipo"] == "parada"]
        nd   = [a for a in al if a["tipo"] == "sem_dados"]
        summ = f"{len(proc)} processo  ·  {len(stop)} paradas  ·  {len(nd)} sem dados"
        lst  = {"processo": proc, "parada": stop, "sem_dados": nd}.get(filtro, proc)
        if not lst:
            return summ, html.Div("Nenhum evento nesta categoria.", className="empty")
        items = []
        for a in lst[:60]:
            if a["tipo"] == "sem_dados":
                det = "Tag sem leituras no período"
            else:
                det = f"{a['inicio']} → {a['fim']}  ·  {a['n']} leituras  ·  {a['sigma']}σ"
            items.append(html.Div(className="alert-item", children=[
                html.Div(className="alert-hd", children=[
                    badge(a["sev"], a["cor"]),
                    html.Strong(a["param"], style={"fontSize": "0.83rem"}),
                ]),
                html.Div(det, className="alert-dt"),
            ]))
        return summ, items

    # ── navegação principal ───────────────────────────────────────────────
    @callback(
        Output("nav-ativa",       "data"),
        Output("nav-processo",    "className"),
        Output("nav-qualidade",   "className"),
        Output("nav-downtime",    "className"),
        Output("nav-pq",          "className"),
        Output("nav-yankee",      "className"),
        Output("nav-relatorio",   "className"),
        Output("aba-processo",    "style"),
        Output("aba-qualidade",   "style"),
        Output("aba-downtime",    "style"),
        Output("aba-pq",          "style"),
        Output("aba-yankee",      "style"),
        Output("aba-relatorio",   "style"),
        Input("nav-processo",     "n_clicks"),
        Input("nav-qualidade",    "n_clicks"),
        Input("nav-downtime",     "n_clicks"),
        Input("nav-pq",           "n_clicks"),
        Input("nav-yankee",       "n_clicks"),
        Input("nav-relatorio",    "n_clicks"),
    )
    def nav(*_):
        mapa = {"nav-processo": "processo", "nav-qualidade": "qualidade",
                "nav-downtime": "downtime", "nav-pq": "pq", "nav-yankee": "yankee",
                "nav-relatorio": "relatorio"}
        ativa = mapa.get(ctx.triggered_id, "processo")
        cls   = lambda k: "nav-tab on" if mapa.get(k) == ativa else "nav-tab"
        show  = {"display": "block"}
        hide  = {"display": "none"}
        return (
            ativa,
            cls("nav-processo"), cls("nav-qualidade"),
            cls("nav-downtime"), cls("nav-pq"), cls("nav-yankee"), cls("nav-relatorio"),
            show if ativa == "processo"  else hide,
            show if ativa == "qualidade" else hide,
            show if ativa == "downtime"  else hide,
            show if ativa == "pq"        else hide,
            show if ativa == "yankee"    else hide,
            show if ativa == "relatorio" else hide,
        )

    # ── aba qualidade: KPIs ───────────────────────────────────────────────
    @callback(
        Output("qk0", "children"), Output("qk1", "children"),
        Output("qk2", "children"), Output("qk3", "children"),
        Output("qk4", "children"),
        Output("g-qual-param",      "figure"),
        Output("g-conf-produto",    "figure"),
        Output("qual-resumo",       "children"),
        Output("qual-alertas",      "children"),
        Output("sel-qual-produto",  "options"),
        Output("g-qual-pareto",     "figure"),
        Input("nav-ativa",          "data"),
        Input("sel-qual-param",     "value"),
        Input("tipo-qual",          "data"),
        Input("sel-qual-produto",   "value"),
    )
    def aba_qualidade(nav_ativa, param, tipo_qual, produto_sel):
        vz = kpi_card("—", "—")
        empty = _empty_fig("Sem dados", 320)
        empty_pareto = _empty_fig("Selecione um parâmetro", 300)
        opts_vazio = [{"label": "Todos", "value": "__todos__"}]
        if nav_ativa != "qualidade":
            raise PreventUpdate

        try:
            dq, specs = carregar_qualidade()
        except Exception as e:
            return vz, vz, vz, vz, vz, empty, empty, f"Erro: {e}", [], opts_vazio, empty_pareto

        if dq.empty:
            return vz, vz, vz, vz, vz, empty, empty, "Sem dados de qualidade", [], opts_vazio, empty_pareto

        # opções de produto a partir dos dados carregados
        col_prod = "Familia Atual" if "Familia Atual" in dq.columns else (
                   "Familia" if "Familia" in dq.columns else None)
        if col_prod:
            prods = sorted(dq[col_prod].dropna().unique().tolist())
            produto_opts = [{"label": "Todos", "value": "__todos__"}] + [
                {"label": p, "value": p} for p in prods]
        else:
            produto_opts = opts_vazio

        # filtrar por produto selecionado
        dq_f = dq
        if produto_sel and produto_sel != "__todos__" and col_prod:
            dq_f = dq[dq[col_prod] == produto_sel].copy()

        conf = resumo_conformidade(dq_f, specs)

        n_jumbos  = len(dq_f)
        produtos  = dq_f[col_prod].nunique() if col_prod else 0
        periodo   = f"{dq_f['Data'].min().strftime('%d/%m')} → {dq_f['Data'].max().strftime('%d/%m/%Y')}" if not dq_f.empty else "—"

        if conf.empty:
            ok_pct = fora = criticos = 0
        else:
            total_c = len(conf[conf["status"] != "SEM_SPEC"])
            ok_n    = len(conf[conf["status"] == "OK"])
            ok_pct  = round(ok_n / total_c * 100, 1) if total_c else 0
            fora    = len(conf[conf["status"].isin(["FORA_LSE","FORA_LSC"])])
            criticos = len(conf[conf["status"] == "FORA_LSE"])

        cor_ok = P["ok"] if ok_pct >= 90 else P["warn"] if ok_pct >= 70 else P["crit"]

        prod_label = f" — {produto_sel}" if produto_sel and produto_sel != "__todos__" else ""
        kpis = (
            kpi_card("Período", periodo, "", P["accent"]),
            kpi_card("Jumbos", str(n_jumbos), f"analisados{prod_label}", P["accent"]),
            kpi_card("Produtos", str(produtos), "famílias", P["muted2"]),
            kpi_card("Conformidade", f"{ok_pct}%", "dentro da especificação", cor_ok),
            kpi_card("Fora de spec", str(fora), f"{criticos} acima do LSE", P["crit"] if fora else P["ok"]),
        )

        if param:
            g_param = fig_imr(conf, param) if tipo_qual == "imr" else fig_qualidade_param(conf, param)
            g_pareto = fig_pareto_valores(conf, param)
        else:
            g_param = empty
            g_pareto = empty_pareto
        g_conf = fig_conformidade_produto(conf)

        # resumo textual
        resumo_items = []
        if not conf.empty:
            por_status = conf[conf["status"] != "SEM_SPEC"].groupby("status")["Unidade"].count()
            for s, n in por_status.items():
                cor = {"OK": P["ok"], "FORA_LSE": P["crit"], "FORA_LSC": P["warn"]}.get(s, P["muted"])
                resumo_items.append(html.Div(style={"display":"flex","justifyContent":"space-between",
                                                     "padding":"8px 0","borderBottom":f"1px solid {P['border']}"},
                    children=[badge(s, cor), html.Span(f"{n} medições", style={"fontSize":"0.82rem","color":P["text"]})]))

        # jumbos fora de spec — segue seleção de produto E parâmetro
        alertas_items = []
        if not conf.empty:
            conf_fora = conf[conf["status"].isin(["FORA_LSE","FORA_LSC"])]
            if param:
                conf_fora = conf_fora[conf_fora["parametro"] == param]
            fora_df = conf_fora.sort_values("Data", ascending=False).head(40)
            titulo_fora = f"Jumbos fora — {param}" if param else "Jumbos fora de especificação"
            for _, r in fora_df.iterrows():
                cor = P["crit"] if r["status"] == "FORA_LSE" else P["warn"]
                alertas_items.append(html.Div(className="alert-item", children=[
                    html.Div(className="alert-hd", children=[
                        badge(r["status"], cor),
                        html.Strong(r["parametro"], style={"fontSize":"0.83rem"}),
                        html.Span(f" · {r['Familia']}", style={"fontSize":"0.75rem","color":P["muted2"]}),
                    ]),
                    html.Div(
                        f"{r['Unidade']}  ·  {r['Data'].strftime('%d/%m %H:%M')}  ·  valor: {r['valor']:.3f}"
                        + (f"  (LSE: {r['LSE']:.3f})" if r["status"]=="FORA_LSE" and r["LSE"] else
                           f"  (LSC: {r['LSC']:.3f})" if r["status"]=="FORA_LSC" and r["LSC"] else ""),
                        className="alert-dt"),
                ]))

        return (*kpis, g_param, g_conf,
                resumo_items or html.Div("Sem especificações cadastradas.", className="empty"),
                alertas_items or html.Div("Nenhum jumbo fora de especificação.", className="empty"),
                produto_opts,
                g_pareto)

    # ── aba downtime ──────────────────────────────────────────────────────
    @callback(
        Output("dk0", "children"), Output("dk1", "children"),
        Output("dk2", "children"), Output("dk3", "children"),
        Output("dk4", "children"),
        Output("g-dt-pareto",      "figure"),
        Output("g-dt-semanal",     "figure"),
        Output("dt-lista-criticas","children"),
        Output("dt-lista-lmp",     "children"),
        Output("dt-top3",          "children"),
        Input("nav-ativa",         "data"),
    )
    def aba_downtime(nav_ativa):
        vz    = kpi_card("—", "—")
        empty = _empty_fig("Sem dados", 320)
        sem   = html.Div("—", className="empty")
        if nav_ativa != "downtime":
            raise PreventUpdate

        try:
            dd       = carregar_downtime_paradas(incluir_hayout=True)
            dd_reais = carregar_downtime_paradas(incluir_hayout=False)
            pareto   = pareto_downtime()
            dq, _    = carregar_qualidade()
        except Exception as e:
            err = html.Div(f"Erro: {e}", className="empty")
            return vz, vz, vz, vz, vz, empty, empty, err, err, err

        if dd.empty:
            msg = html.Div("Sem dados de downtime.", className="empty")
            return vz, vz, vz, vz, vz, empty, empty, msg, msg, msg

        col_ini = next((c for c in dd.columns if c.lower().replace("í","i").startswith("ini")), None)
        col_dur = next((c for c in dd.columns if "ura" in c.lower() and "min" in c.lower()), None)

        total_min = dd_reais[col_dur].sum() if col_dur else 0
        n_eventos = len(dd_reais)
        periodo   = ""
        if col_ini and not dd[col_ini].isna().all():
            ini = pd.to_datetime(dd[col_ini]).min()
            fim = pd.to_datetime(dd[col_ini]).max()
            periodo = f"{ini.strftime('%d/%m')} → {fim.strftime('%d/%m/%Y')}"

        classes_reais = dd_reais["Classe"].value_counts().to_dict() if "Classe" in dd_reais.columns else {}
        corretiva_min = (
            dd_reais[dd_reais["Classe"].str.contains("MCR", na=False)][col_dur].sum()
            if col_dur and "Classe" in dd_reais.columns else 0
        )

        kpis = (
            kpi_card("Período",              periodo,                    "",             P["accent"]),
            kpi_card("Eventos",              str(n_eventos),             "paradas reais", P["crit"] if n_eventos > 5 else P["warn"]),
            kpi_card("Tempo perdido",        f"{total_min/60:.1f}h",     "paradas reais", P["crit"] if total_min > 240 else P["warn"]),
            kpi_card("Manutenção corretiva", f"{corretiva_min/60:.1f}h","MCR",           P["crit"] if corretiva_min > 60 else P["muted2"]),
            kpi_card("Classes",              str(len(classes_reais)),    "tipos",         P["muted2"]),
        )

        g_pareto  = fig_pareto_downtime(pareto)
        g_semanal = fig_producao_downtime_semanal(dq, dd_reais)

        _LMP_CLASSES = {"LMP", "LIMP", "LIMPEZA"}

        def _is_lmp(cls_val):
            s = str(cls_val).strip().upper() if pd.notna(cls_val) else ""
            return any(k in s for k in _LMP_CLASSES)

        def _str(v):
            return str(v).strip() if pd.notna(v) else ""

        def _compact_row(r):
            dur_raw = r.get(col_dur) if col_dur else None
            dur     = float(dur_raw) if pd.notna(dur_raw) else 0.0
            ini     = r.get(col_ini) if col_ini else None
            tipo    = _str(r.get("Tipo")) or _str(r.get("Causa")) or "—"
            cls     = _str(r.get("Classe"))
            cls_label = cls.split("-")[0].strip() if "-" in cls else cls
            cor     = P["crit"] if "MCR" in cls else P["warn"] if "PME" in cls else P["muted2"]
            try:
                data_str = pd.Timestamp(ini).strftime("%d/%m %H:%M") if pd.notna(ini) else "—"
            except Exception:
                data_str = "—"
            return html.Div(style={
                "display": "flex", "alignItems": "center", "gap": "8px",
                "padding": "5px 8px", "borderBottom": f"1px solid {P['border']}",
                "fontSize": "0.78rem", "lineHeight": "1.3",
            }, children=[
                badge(cls_label, cor),
                html.Span(tipo[:60], style={"flex": "1", "overflow": "hidden",
                          "textOverflow": "ellipsis", "whiteSpace": "nowrap"}),
                html.Span(data_str, style={"color": P["muted2"], "whiteSpace": "nowrap"}),
                html.Span(f"{dur:.0f} min", style={
                    "fontWeight": "600", "color": cor, "whiteSpace": "nowrap",
                    "minWidth": "52px", "textAlign": "right",
                }),
            ])

        sort_col = col_dur if col_dur and col_dur in dd_reais.columns else None
        df_sorted = dd_reais.sort_values(sort_col, ascending=False) if sort_col else dd_reais

        rows_crit, rows_lmp = [], []
        for _, r in df_sorted.iterrows():
            cls = str(r.get("Classe",""))
            row = _compact_row(r)
            if _is_lmp(cls):
                rows_lmp.append(row)
            else:
                rows_crit.append(row)

        mask_lmp  = dd_reais["Classe"].apply(_is_lmp) if "Classe" in dd_reais.columns else pd.Series(False, index=dd_reais.index)
        dur_crit  = dd_reais.loc[~mask_lmp, col_dur].sum() if col_dur else 0
        dur_lmp   = dd_reais.loc[ mask_lmp, col_dur].sum() if col_dur else 0

        def _panel(rows, total_min, empty_label):
            if not rows:
                return html.Div(empty_label, className="empty")
            header = html.Div(
                f"{len(rows)} evento(s)  ·  {total_min:.0f} min total",
                style={"fontSize": "0.75rem", "color": P["muted2"],
                       "padding": "4px 8px 6px", "borderBottom": f"1px solid {P['border']}"},
            )
            return [header] + rows

        lista_crit = _panel(rows_crit, dur_crit, "Nenhuma parada de manutenção/operacional.")
        lista_lmp  = _panel(rows_lmp,  dur_lmp,  "Nenhuma limpeza programada.")

        # ── top 3 + barra sem lançamento ──────────────────────────────────
        pct_sp_min = pareto.attrs.get("pct_sem_preench_min", 0) if not pareto.empty else 0
        pct_sp_occ = pareto.attrs.get("pct_sem_preench_occ", 0) if not pareto.empty else 0

        # barra de sem lançamento
        bar_fill = f"linear-gradient(90deg, {P['warn']} {pct_sp_min}%, {P['border']} {pct_sp_min}%)"
        barra_sem = html.Div([
            html.Div(style={"display": "flex", "justifyContent": "space-between",
                            "fontSize": "0.76rem", "marginBottom": "5px"}, children=[
                html.Span("Paradas sem lançamento", style={"color": P["muted2"]}),
                html.Span(f"{pct_sp_min:.1f}% do tempo  ·  {pct_sp_occ:.1f}% das ocorrências",
                          style={"fontWeight": "600", "color": P["warn"]}),
            ]),
            html.Div(style={
                "height": "8px", "borderRadius": "4px",
                "background": bar_fill,
                "marginBottom": "14px",
            }),
        ])

        # top 3 por duração
        if col_dur and not dd_reais.empty:
            top3 = dd_reais.nlargest(3, col_dur)
        else:
            top3 = pd.DataFrame()

        rank_colors = [P["crit"], P["warn"], P["muted2"]]
        rank_labels = ["1°", "2°", "3°"]
        top3_rows = []
        for i, (_, r) in enumerate(top3.iterrows()):
            dur_v  = float(r.get(col_dur) or 0)
            ini_v  = r.get(col_ini) if col_ini else None
            tipo_v = _str(r.get("Tipo")) or _str(r.get("Causa")) or "—"
            cls_v  = _str(r.get("Classe"))
            cls_lb = cls_v.split("-")[0].strip() if "-" in cls_v else cls_v
            cor    = P["crit"] if "MCR" in cls_v else P["warn"] if "PME" in cls_v else P["muted2"]
            try:
                data_v = pd.Timestamp(ini_v).strftime("%d/%m") if pd.notna(ini_v) else "—"
            except Exception:
                data_v = "—"
            top3_rows.append(html.Div(style={
                "display": "flex", "alignItems": "center", "gap": "8px",
                "padding": "6px 4px", "borderBottom": f"1px solid {P['border']}",
                "fontSize": "0.8rem",
            }, children=[
                html.Span(rank_labels[i], style={
                    "fontWeight": "700", "color": rank_colors[i],
                    "minWidth": "22px", "fontSize": "0.85rem",
                }),
                badge(cls_lb, cor),
                html.Span(tipo_v[:48], style={"flex": "1", "overflow": "hidden",
                          "textOverflow": "ellipsis", "whiteSpace": "nowrap"}),
                html.Span(data_v, style={"color": P["muted2"], "fontSize": "0.75rem",
                          "whiteSpace": "nowrap"}),
                html.Span(f"{dur_v:.0f} min", style={
                    "fontWeight": "700", "color": cor,
                    "minWidth": "52px", "textAlign": "right",
                }),
            ]))

        top3_content = [barra_sem] + (top3_rows or [html.Div("Sem dados.", className="empty")])

        return (*kpis, g_pareto, g_semanal, lista_crit, lista_lmp, top3_content)

    # ── aba P×Q ───────────────────────────────────────────────────────────
    @callback(
        Output("pqk0", "children"), Output("pqk1", "children"),
        Output("pqk2", "children"), Output("pqk3", "children"),
        Output("pqk4", "children"),
        Output("g-pq-heatmap",     "figure"),
        Output("pq-sel-var",       "options"),
        Output("pq-sel-var",       "value"),
        Output("pq-inf-espessura",   "children"),
        Output("pq-inf-handfeel",   "children"),
        Output("pq-inf-maciez",     "children"),
        Output("pq-inf-umidade",    "children"),
        Output("pq-inf-umidadeqcs", "children"),
        Output("pq-inf-quebras",    "children"),
        Output("pq-snapshot",      "data"),
        Input("nav-ativa",         "data"),
        State("pkg",               "data"),
    )
    def aba_pq(nav_ativa, pkg):
        vz    = kpi_card("—", "—")
        empty = _empty_fig("Sem dados", 420)
        vazios = [html.Div("—", className="empty")]
        if nav_ativa != "pq":
            return vz, vz, vz, vz, vz, empty, [], None, *[vazios]*6, None

        try:
            dq, _ = carregar_qualidade()
            dp    = carregar_producao()
        except Exception as e:
            return vz, vz, vz, vz, vz, empty, [], None, *[[html.Div(f"Erro: {e}")]]*6, None

        if pkg:
            from io import StringIO
            dados_opc = pd.read_json(StringIO(pkg["dados_json"]), orient="split")
            dados_opc["timestamp"] = pd.to_datetime(dados_opc["timestamp"])
        else:
            dados_opc = pd.DataFrame()

        df_j, df_corr = correlacionar_processo_qualidade(dados_opc, dq, dp)

        if df_j.empty:
            msg = [html.Div("Carregue um arquivo de processo na aba Processo primeiro.", className="empty")]
            return vz, vz, vz, vz, vz, empty, [], None, *[msg]*6, None

        n_jumbos = len(df_j)
        n_vars   = len([c for c in df_j.columns if c.startswith("opc_")])
        periodo  = f"{df_j['Data'].min().strftime('%d/%m')} → {df_j['Data'].max().strftime('%d/%m/%Y')}"

        kpis = (
            kpi_card("Período", periodo, "", P["accent"]),
            kpi_card("Jumbos cruzados", str(n_jumbos), "OPC UA + qualidade", P["accent"]),
            kpi_card("Vars processo", str(n_vars), "parâmetros OPC UA", P["muted2"]),
            kpi_card("Targets", str(len([t for t in TARGETS_PQ + ["Quebras"] if t in df_j.columns])),
                     "variáveis de qualidade", P["ok"]),
            kpi_card("Produtos", str(df_j["Familia"].nunique()), "famílias", P["muted2"]),
        )

        g_heat = fig_corr_pq(df_corr, top_n=20)

        # opções do scatter
        opc_cols = sorted([c.replace("opc_", "") for c in df_j.columns if c.startswith("opc_")])
        prod_cols = [v for v in ["Quebras","Velocidade","Gr/m2"] if v in df_j.columns]
        opts = [{"label": v, "value": v} for v in opc_cols + prod_cols]
        default_var = opc_cols[0] if opc_cols else None

        inf_esp  = _top_influenciadores(df_corr, "Espessura")
        inf_hf   = _top_influenciadores(df_corr, "Handfeel")
        inf_mac  = _top_influenciadores(df_corr, "Maciez TSA")
        inf_um   = _top_influenciadores(df_corr, "Umidade")
        inf_uqcs = _top_influenciadores(df_corr, "UmidadeQCS")
        inf_qbr  = _top_influenciadores(df_corr, "Quebras")

        # snapshot para a aba de relatórios
        snap = {
            "corr_json": df_corr.to_json(orient="split") if not df_corr.empty else None,
            "periodo_ini": df_j["Data"].min().strftime("%Y-%m-%d"),
            "periodo_fim": df_j["Data"].max().strftime("%Y-%m-%d"),
            "produto": str(df_j["Familia"].mode().iloc[0]) if not df_j.empty else "",
            "n_jumbos": n_jumbos,
            "dados_opc_json": dados_opc.to_json(orient="split") if not dados_opc.empty else None,
        }

        return (*kpis, g_heat, opts, default_var,
                inf_esp, inf_hf, inf_mac, inf_um, inf_uqcs, inf_qbr, snap)

    @callback(
        Output("g-pq-scatter", "figure"),
        Input("pq-sel-var",    "value"),
        Input("pq-sel-target", "value"),
        Input("nav-ativa",     "data"),
        State("pkg",           "data"),
    )
    def scatter_pq(var_proc, target, nav_ativa, pkg):
        if nav_ativa != "pq" or not var_proc or not target or not pkg:
            return _empty_fig("Selecione variável e target", 300)
        from io import StringIO
        dados_opc = pd.read_json(StringIO(pkg["dados_json"]), orient="split")
        dados_opc["timestamp"] = pd.to_datetime(dados_opc["timestamp"])
        dq, _ = carregar_qualidade()
        dp    = carregar_producao()
        df_j, _ = correlacionar_processo_qualidade(dados_opc, dq, dp)
        return fig_scatter_pq(df_j, var_proc, target)

    # ── tipo de gráfico — processo ────────────────────────────────────────
    @callback(
        Output("tipo-grafico",     "data"),
        Output("btn-tipo-serie",   "className"),
        Output("btn-tipo-scatter", "className"),
        Output("btn-tipo-cross",   "className"),
        Input("btn-tipo-serie",    "n_clicks"),
        Input("btn-tipo-scatter",  "n_clicks"),
        Input("btn-tipo-cross",    "n_clicks"),
    )
    def toggle_tipo_grafico(*_):
        mapa = {"btn-tipo-serie": "serie", "btn-tipo-scatter": "scatter", "btn-tipo-cross": "cross"}
        ativo = mapa.get(ctx.triggered_id, "serie")
        cls   = lambda k: "tab-btn on" if mapa.get(k) == ativo else "tab-btn"
        return ativo, cls("btn-tipo-serie"), cls("btn-tipo-scatter"), cls("btn-tipo-cross")

    @callback(
        Output("crosscorr-opts", "style"),
        Output("trend-legend",   "style"),
        Input("tipo-grafico",    "data"),
    )
    def toggle_crosscorr_opts(tipo):
        if tipo == "cross":
            return {"display": "block", "marginBottom": "12px"}, {"display": "none"}
        elif tipo == "scatter":
            return {"display": "none"}, {"display": "none"}
        return {"display": "none"}, {}

    # ── tipo de gráfico — qualidade ───────────────────────────────────────
    @callback(
        Output("tipo-qual",        "data"),
        Output("btn-qual-imr",     "className"),
        Output("btn-qual-scatter", "className"),
        Input("btn-qual-imr",      "n_clicks"),
        Input("btn-qual-scatter",  "n_clicks"),
    )
    def toggle_tipo_qual(*_):
        mapa  = {"btn-qual-imr": "imr", "btn-qual-scatter": "scatter"}
        ativo = mapa.get(ctx.triggered_id, "imr")
        cls   = lambda k: "tab-btn on" if mapa.get(k) == ativo else "tab-btn"
        return ativo, cls("btn-qual-imr"), cls("btn-qual-scatter")

    # ── limites: toggle visibilidade do editor ────────────────────────────
    @callback(
        Output("limites-wrap",         "style"),
        Output("btn-toggle-limites",   "children"),
        Input("btn-toggle-limites",    "n_clicks"),
    )
    def toggle_limites(n):
        if n and n % 2 == 1:
            return {"display": "block", "marginTop": "4px"}, "▴ Fechar"
        return {"display": "none"}, "▾ Editar"

    # ── limites: renderiza inputs para os parâmetros selecionados ─────────
    @callback(
        Output("limites-editor", "children"),
        Input("sel",             "value"),
        State("limites-store",   "data"),
    )
    def renderizar_editor(params, limites):
        limites = limites or {}
        if not params:
            return html.Div("Selecione parâmetros acima para configurar os limites.",
                            style={"fontSize": "0.75rem", "color": P["muted2"], "padding": "8px 0"})

        _inp = lambda id_, val: dcc.Input(
            id=id_, type="number", value=val, placeholder="—", debounce=True,
            style={
                "width": "80px", "fontSize": "0.78rem", "padding": "4px 6px",
                "border": f"1px solid {P['border']}", "borderRadius": "6px",
                "background": P["card"], "color": P["text"], "textAlign": "center",
            },
        )

        header = html.Div(style={
            "display": "grid", "gridTemplateColumns": "1fr 80px 80px",
            "gap": "6px", "marginBottom": "6px",
        }, children=[
            html.Span("Parâmetro", style={"fontSize": "0.66rem", "color": P["muted2"], "fontWeight": 700}),
            html.Span("LI", style={"fontSize": "0.66rem", "color": P["ok"], "fontWeight": 700, "textAlign": "center"}),
            html.Span("LS", style={"fontSize": "0.66rem", "color": P["crit"], "fontWeight": 700, "textAlign": "center"}),
        ])

        rows = [header]
        for param in params:
            lim = limites.get(param, {})
            rows.append(html.Div(style={
                "display": "grid", "gridTemplateColumns": "1fr 80px 80px",
                "gap": "6px", "alignItems": "center", "marginBottom": "6px",
            }, children=[
                html.Span(param[:24], style={
                    "fontSize": "0.75rem", "color": P["text"],
                    "overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap",
                }),
                _inp({"type": "lim-li", "index": param}, lim.get("li")),
                _inp({"type": "lim-ls", "index": param}, lim.get("ls")),
            ]))
        return rows

    # ── limites: salva em disco ───────────────────────────────────────────
    @callback(
        Output("limites-store",      "data"),
        Output("limites-status",     "children"),
        Input("btn-salvar-limites",  "n_clicks"),
        State({"type": "lim-li", "index": ALL}, "value"),
        State({"type": "lim-li", "index": ALL}, "id"),
        State({"type": "lim-ls", "index": ALL}, "value"),
        prevent_initial_call=True,
    )
    def salvar_limites_cb(n_clicks, vals_li, ids, vals_ls):
        if not n_clicks or not ids:
            return no_update, no_update
        limites = _carregar_limites()
        for i, id_ in enumerate(ids):
            param = id_["index"]
            limites[param] = {
                "li": vals_li[i] if i < len(vals_li) else None,
                "ls": vals_ls[i] if i < len(vals_ls) else None,
            }
        _salvar_limites(limites)
        return limites, f"✓ Salvo às {datetime.now().strftime('%H:%M:%S')}"

    # ── aba Temperatura Yankee ────────────────────────────────────────────

    @callback(
        Output("yt-msg",   "children"),
        Output("yk0",      "children"),
        Input("yt-btn",    "n_clicks"),
        State("yt-la",     "value"),
        State("yt-meio",   "value"),
        State("yt-lc",     "value"),
        State("yt-op",     "value"),
        State("yt-memo",   "value"),
        prevent_initial_call=True,
    )
    def registrar_temperatura(n, la, meio, lc, op, memo):
        if not n:
            return no_update, no_update
        if la is None and meio is None and lc is None:
            return "Preencha ao menos um valor.", no_update
        try:
            salvar_temperatura_yankee(
                la=float(la)   if la   is not None else None,
                meio=float(meio) if meio is not None else None,
                lc=float(lc)   if lc   is not None else None,
                operador=op or "",
                memo=memo or "",
            )
            msg = f"✓ Registrado às {datetime.now().strftime('%H:%M:%S')}"
        except Exception as e:
            return f"Erro: {e}", no_update
        ref = la or meio or lc
        ult = kpi_card("Última leitura",
                       f"{ref:.1f} °C" if ref is not None else "—",
                       datetime.now().strftime("%d/%m %H:%M"), P["accent"])
        return msg, ult

    @callback(
        Output("yk1",           "children"),
        Output("yk2",           "children"),
        Output("yk3",           "children"),
        Output("yk4",           "children"),
        Output("g-yankee-trend","figure"),
        Output("yankee-lista",  "children"),
        Input("nav-ativa",      "data"),
        Input("yt-btn",         "n_clicks"),
    )
    def aba_yankee(nav_ativa, _btn):
        vz    = kpi_card("—", "—")
        empty = _empty_fig("Sem leituras registradas", 300)
        if nav_ativa != "yankee":
            raise PreventUpdate

        df = carregar_temperaturas_yankee(dias=30)

        if df.empty:
            return (
                kpi_card("LA média", "—", "sem dados", P["muted2"]),
                kpi_card("LC média", "—", "sem dados", P["muted2"]),
                kpi_card("Leituras", "0", "últimos 30 dias", P["muted2"]),
                kpi_card("Diferencial", "—", "LA − LC", P["muted2"]),
                empty,
                [html.Div("Nenhuma leitura registrada ainda.", className="empty")],
            )

        la_med   = df["la"].dropna().mean()   if "la"   in df else float("nan")
        meio_med = df["meio"].dropna().mean() if "meio" in df else float("nan")
        lc_med   = df["lc"].dropna().mean()   if "lc"   in df else float("nan")
        dif      = (df["la"] - df["lc"]).dropna() if "la" in df and "lc" in df else pd.Series(dtype=float)
        dif_med  = dif.mean() if not dif.empty else None
        n_leit   = len(df)

        cor_dif = P["ok"] if dif_med is not None and abs(dif_med) <= 5 else P["warn"]

        kpis = (
            kpi_card("LA média", f"{la_med:.1f} °C" if pd.notna(la_med) else "—",
                     "Acionamento", P["accent"]),
            kpi_card("Meio média", f"{meio_med:.1f} °C" if pd.notna(meio_med) else "—",
                     "Centro", P["lines"][1]),
            kpi_card("LC média", f"{lc_med:.1f} °C" if pd.notna(lc_med) else "—",
                     "Comando", P["lines"][2]),
            kpi_card("Leituras", str(n_leit), "últimos 30 dias", P["muted2"]),
            kpi_card("Diferencial LA−LC", f"{dif_med:.1f} °C" if dif_med is not None else "—",
                     "perfil transversal", cor_dif),
        )

        # gráfico de tendência
        fig = go.Figure()
        for col, nome, cor in [
            ("la",   "LA — Acionamento", P["accent"]),
            ("meio", "Meio — Centro",    P["lines"][1]),
            ("lc",   "LC — Comando",     P["lines"][2]),
        ]:
            if col in df.columns and df[col].notna().any():
                fig.add_trace(go.Scatter(
                    x=df["timestamp"], y=df[col],
                    mode="lines+markers", name=nome,
                    line=dict(color=cor, width=2),
                    marker=dict(size=6),
                    hovertemplate=f"<b>%{{x|%d/%m %H:%M}}</b><br>{nome}: %{{y:.1f}} °C<extra></extra>",
                ))
        fig.add_hline(y=95, line=dict(color=P["ok"], dash="dot", width=1.2),
                      annotation_text="Referência 95°C",
                      annotation_font=dict(size=9, color=P["ok"]))
        fig.update_layout(
            template="plotly_white",
            paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
            height=300,
            margin=dict(l=54, r=18, t=28, b=42),
            legend=dict(orientation="h", y=1.1, x=0, font=dict(size=11, color=P["text"])),
            xaxis=dict(gridcolor=P["border"], tickfont=dict(size=10, color="#CBD5E1")),
            yaxis=dict(gridcolor="rgba(0,212,255,0.1)", tickfont=dict(size=10, color="#E2E8F0"),
                       title="°C"),
            hovermode="x unified",
        )

        # lista das últimas leituras
        lista = []
        for _, r in df.sort_values("timestamp", ascending=False).head(20).iterrows():
            la_v   = f"{r['la']:.1f}°C"   if pd.notna(r.get("la"))   else "—"
            meio_v = f"{r['meio']:.1f}°C"  if pd.notna(r.get("meio")) else "—"
            lc_v   = f"{r['lc']:.1f}°C"   if pd.notna(r.get("lc"))   else "—"
            op     = str(r.get("operador", "")).strip() or "—"
            memo   = str(r.get("memo", "")).strip()
            lista.append(html.Div(className="alert-item", children=[
                html.Div(className="alert-hd", children=[
                    badge("LA " + la_v,    P["accent"]),
                    badge("Meio " + meio_v, P["lines"][1]),
                    badge("LC " + lc_v,    P["lines"][2]),
                    html.Span(f" · {op}", style={"fontSize": "0.75rem", "color": P["muted2"]}),
                ]),
                html.Div(style={"display": "flex", "justifyContent": "space-between"}, children=[
                    html.Span(pd.Timestamp(r["timestamp"]).strftime("%d/%m/%Y %H:%M"),
                              className="alert-dt"),
                    html.Span(memo, style={"fontSize": "0.72rem", "color": P["muted2"],
                                           "fontStyle": "italic"}) if memo else None,
                ]),
            ]))

        return (*kpis, fig, lista or [html.Div("Sem registros.", className="empty")])

    # ── salvar análise (botão na aba PxQ) ────────────────────────────────
    @callback(
        Output("pq-salvar-msg", "children"),
        Input("pq-btn-salvar",  "n_clicks"),
        State("pq-obs",         "value"),
        State("pq-snapshot",    "data"),
        prevent_initial_call=True,
    )
    def salvar_analise_pq(n, obs, snap):
        if not n or not snap or not snap.get("corr_json"):
            return ""
        from io import StringIO
        df_corr = pd.read_json(StringIO(snap["corr_json"]), orient="split")
        n_rows = salvar_snapshot_correlacoes(
            snap.get("periodo_ini", ""),
            snap.get("periodo_fim", ""),
            snap.get("produto", ""),
            snap.get("n_jumbos", 0),
            df_corr,
            obs or "",
        )
        return f"✓ {n_rows} correlações salvas"

    # ── aba Relatórios ────────────────────────────────────────────────────
    @callback(
        Output("rk0", "children"), Output("rk1", "children"),
        Output("rk2", "children"), Output("rk3", "children"),
        Output("rk4", "children"),
        Output("g-corr-evolucao",     "figure"),
        Output("rel-historico-tabela","children"),
        Output("rel-freq-w",          "className"),
        Output("rel-freq-ms",         "className"),
        Input("nav-ativa",            "data"),
        Input("rel-freq-w",           "n_clicks"),
        Input("rel-freq-ms",          "n_clicks"),
        Input("rel-target-drop",      "value"),
        Input("rel-btn-salvar",       "n_clicks"),
        State("rel-obs",              "value"),
        State("pq-snapshot",          "data"),
    )
    def aba_relatorio(nav_ativa, _w, _ms, target, n_salvar, obs_rel, snap):
        vz    = kpi_card("—", "—")
        empty = _empty_fig("Sem dados", 360)
        hide_cls = "nav-tab"
        on_cls   = "nav-tab on"

        if nav_ativa != "relatorio":
            raise PreventUpdate

        # determina frequência pelo botão disparador
        freq = "MS" if ctx.triggered_id == "rel-freq-ms" else "W"
        cls_w  = on_cls  if freq == "W"  else hide_cls
        cls_ms = on_cls  if freq == "MS" else hide_cls

        # salvar se clicado
        if ctx.triggered_id == "rel-btn-salvar" and n_salvar and snap and snap.get("corr_json"):
            from io import StringIO
            df_c = pd.read_json(StringIO(snap["corr_json"]), orient="split")
            salvar_snapshot_correlacoes(
                snap.get("periodo_ini", ""), snap.get("periodo_fim", ""),
                snap.get("produto", ""), snap.get("n_jumbos", 0),
                df_c, obs_rel or "",
            )

        hist = carregar_historico_snapshots()

        # KPIs
        n_snaps   = hist["salvo_em"].nunique() if not hist.empty and "salvo_em" in hist.columns else 0
        n_jumbos  = int(hist["n_jumbos"].max()) if not hist.empty and "n_jumbos" in hist.columns else 0
        n_fortes  = int((hist["forca"].isin(["forte","moderada"])).sum()) if not hist.empty else 0
        ult_salvo = hist["salvo_em"].max()[:10] if not hist.empty and "salvo_em" in hist.columns else "—"
        per_ini   = hist["periodo_ini"].min()[:10] if not hist.empty and "periodo_ini" in hist.columns else "—"
        per_fim   = hist["periodo_fim"].max()[:10] if not hist.empty and "periodo_fim" in hist.columns else "—"

        kpis = (
            kpi_card("Snapshots", str(n_snaps), "análises salvas", P["accent"]),
            kpi_card("Maior lote", str(n_jumbos), "jumbos", P["accent2"]),
            kpi_card("Correlações fortes", str(n_fortes), "forte/moderada", P["ok"]),
            kpi_card("Último salvamento", ult_salvo, "", P["muted2"]),
            kpi_card("Período coberto", per_ini, per_fim, P["muted2"]),
        )

        # heatmap evolução por semana/mês usando snapshot OPC atual
        fig_evol = empty
        if snap and snap.get("dados_opc_json"):
            try:
                from io import StringIO
                dados_opc = pd.read_json(StringIO(snap["dados_opc_json"]), orient="split")
                dados_opc["timestamp"] = pd.to_datetime(dados_opc["timestamp"])
                dq, _ = carregar_qualidade()
                dp    = carregar_producao()
                comp  = comparar_correlacoes_por_periodo(dados_opc, dq, dp, freq=freq)
                if not comp.empty and target:
                    sub = comp[comp["var_qualidade"] == target]
                    if not sub.empty:
                        piv = sub.pivot(index="var_processo", columns="periodo", values="r")
                        fig_evol = go.Figure(go.Heatmap(
                            z=piv.values,
                            x=[str(c) for c in piv.columns],
                            y=list(piv.index),
                            colorscale="RdBu",
                            zmid=0, zmin=-1, zmax=1,
                            colorbar=dict(title="r", thickness=12, len=0.7),
                            hovertemplate="<b>%{y}</b><br>%{x}<br>r = %{z:.3f}<extra></extra>",
                        ))
                        fig_evol.update_layout(
                            title=f"Evolução — {target} ({('Semana' if freq=='W' else 'Mês')})",
                            template="plotly_white",
                            paper_bgcolor=P["card"], plot_bgcolor=P["plot"], font=dict(color=P["text"]),
                            height=360,
                            margin=dict(l=160, r=18, t=40, b=60),
                            xaxis=dict(tickangle=-35, tickfont=dict(size=9)),
                            yaxis=dict(tickfont=dict(size=9)),
                        )
            except Exception:
                pass

        # tabela histórico
        if hist.empty:
            tabela = html.Div("Nenhuma análise salva ainda.", className="empty")
        else:
            cols_show = ["salvo_em", "periodo_ini", "periodo_fim", "produto",
                         "n_jumbos", "var_processo", "var_qualidade", "r", "forca", "observacao"]
            cols_show = [c for c in cols_show if c in hist.columns]
            header = html.Tr([html.Th(c, style={"padding": "6px 10px", "background": P["surf"],
                                                 "fontSize": "0.72rem", "color": P["muted2"],
                                                 "fontWeight": 600, "textTransform": "uppercase",
                                                 "borderBottom": f"2px solid {P['border']}"})
                              for c in cols_show])
            rows = []
            for i, (_, row) in enumerate(hist.head(80).iterrows()):
                bg = P["card"] if i % 2 == 0 else P["surf"]
                r_val = row.get("r", None)
                r_str = f"{r_val:+.3f}" if pd.notna(r_val) else "—"
                forca = str(row.get("forca", "")) or "—"
                cor_forca = (P["ok"] if forca == "forte"
                             else P["warn"] if forca == "moderada"
                             else P["muted2"])
                cells = []
                for c in cols_show:
                    val = row.get(c, "")
                    if c == "r":
                        cells.append(html.Td(r_str, style={"padding": "5px 10px",
                                                            "fontWeight": 600,
                                                            "color": P["ok"] if pd.notna(r_val) and r_val > 0 else P["crit"]}))
                    elif c == "forca":
                        cells.append(html.Td(forca, style={"padding": "5px 10px",
                                                            "color": cor_forca, "fontWeight": 600}))
                    else:
                        v = str(val)[:40] if pd.notna(val) else "—"
                        if c == "salvo_em" and isinstance(val, str):
                            v = val[:16].replace("T", " ")
                        cells.append(html.Td(v, style={"padding": "5px 10px", "fontSize": "0.78rem"}))
                rows.append(html.Tr(cells, style={"background": bg}))
            tabela = html.Div(
                html.Table([html.Thead(header), html.Tbody(rows)],
                           style={"width": "100%", "borderCollapse": "collapse",
                                  "fontSize": "0.8rem"}),
                style={"overflowX": "auto"},
            )

        return (*kpis, fig_evol, tabela, cls_w, cls_ms)

    # ── salvar análise (botão na aba Relatórios) ─────────────────────────
    @callback(
        Output("rel-salvar-msg", "children"),
        Input("rel-btn-salvar",  "n_clicks"),
        State("rel-obs",         "value"),
        State("pq-snapshot",     "data"),
        prevent_initial_call=True,
    )
    def salvar_analise_rel(n, obs, snap):
        if not n or not snap or not snap.get("corr_json"):
            return "Sem análise disponível (abra a aba Processo × Qualidade primeiro)"
        from io import StringIO
        df_corr = pd.read_json(StringIO(snap["corr_json"]), orient="split")
        n_rows = salvar_snapshot_correlacoes(
            snap.get("periodo_ini", ""), snap.get("periodo_fim", ""),
            snap.get("produto", ""), snap.get("n_jumbos", 0),
            df_corr, obs or "",
        )
        return f"✓ {n_rows} correlações salvas"

    # ── gerar PDF ─────────────────────────────────────────────────────────
    @callback(
        Output("download-pdf",  "data"),
        Input("btn-gerar-pdf",  "n_clicks"),
        State("pq-snapshot",    "data"),
        State("rel-freq-w",     "className"),
        prevent_initial_call=True,
    )
    def gerar_pdf(n, snap, cls_w):
        if not n:
            return no_update

        hist = carregar_historico_snapshots()

        df_corr_atual = None
        comp = pd.DataFrame()
        freq = "W" if "on" in (cls_w or "") else "MS"
        freq_label = "Semana" if freq == "W" else "Mês"

        if snap:
            if snap.get("corr_json"):
                from io import StringIO
                df_corr_atual = pd.read_json(StringIO(snap["corr_json"]), orient="split")
            if snap.get("dados_opc_json"):
                try:
                    from io import StringIO
                    dados_opc = pd.read_json(StringIO(snap["dados_opc_json"]), orient="split")
                    dados_opc["timestamp"] = pd.to_datetime(dados_opc["timestamp"])
                    dq, _ = carregar_qualidade()
                    dp    = carregar_producao()
                    comp  = comparar_correlacoes_por_periodo(dados_opc, dq, dp, freq=freq)
                except Exception:
                    pass

        pdf_bytes = gerar_pdf_relatorio(
            historico_df=hist,
            comp_df=comp,
            df_corr_atual=df_corr_atual,
            freq_label=freq_label,
            titulo="Relatório de Correlações — AT1",
        )
        nome = f"relatorio_at1_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        return dcc.send_bytes(pdf_bytes, nome)

    return app


# WSGI entrypoint para Gunicorn / Render.com
server = criar_app().server


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import socket
    ip_local = socket.gethostbyname(socket.gethostname())
    print(f"Monitorando: {BASE_DIR}")
    print(f"PC (local):  http://127.0.0.1:8050")
    print(f"Celular:     http://{ip_local}:8050")
    server.run(host="0.0.0.0", port=8050, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
