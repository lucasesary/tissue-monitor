"""
relatorio_pdf.py — gera relatório PDF de correlações processo×qualidade.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_TARGETS = ["Espessura", "Handfeel", "Maciez TSA", "Umidade", "Quebras"]

_GREEN_LIGHT = colors.HexColor("#d4edda")
_RED_LIGHT   = colors.HexColor("#f8d7da")
_HEADER_BG   = colors.HexColor("#1a2744")
_HEADER_FG   = colors.white
_ALT_ROW     = colors.HexColor("#f2f4f8")


def _cor_celula(r: float) -> Optional[colors.Color]:
    if pd.isna(r):
        return None
    intensidade = min(abs(r) / 1.0, 1.0)
    if r > 0:
        g = int(212 + (255 - 212) * (1 - intensidade))
        return colors.Color(0.83, g / 255, 0.85)
    else:
        r_ = int(248 + (255 - 248) * (1 - intensidade))
        return colors.Color(r_ / 255, 0.84, 0.85)


def _cor_r(r: float) -> colors.Color:
    if pd.isna(r):
        return colors.black
    a = abs(r)
    if a >= 0.7:
        return colors.HexColor("#155724") if r > 0 else colors.HexColor("#721c24")
    if a >= 0.35:
        return colors.HexColor("#383d41")
    return colors.HexColor("#6c757d")


def _forca_label(r: float) -> str:
    if pd.isna(r):
        return "—"
    a = abs(r)
    sinal = "+" if r > 0 else "−"
    if a >= 0.7:
        return f"{sinal} Forte ({r:+.2f})"
    if a >= 0.5:
        return f"{sinal} Moderada ({r:+.2f})"
    if a >= 0.35:
        return f"{sinal} Fraca ({r:+.2f})"
    return f"Sem ({r:+.2f})"


def _tabela_corr(df_corr: pd.DataFrame, styles: list) -> Table:
    """Monta tabela de correlações com células coloridas."""
    targets_presentes = [t for t in _TARGETS if t in df_corr.columns]
    if not targets_presentes:
        targets_presentes = list(df_corr.columns)

    cabecalho = ["Variável de Processo"] + targets_presentes
    data = [cabecalho]

    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("TEXTCOLOR",  (0, 0), (-1, 0), _HEADER_FG),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 7.5),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",       (0, 0), (-1, -1), 0.4, colors.HexColor("#dee2e6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ALT_ROW]),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]

    for i, var in enumerate(df_corr.index, start=1):
        row = [var]
        for j, tgt in enumerate(targets_presentes, start=1):
            r_val = df_corr.at[var, tgt] if tgt in df_corr.columns else float("nan")
            row.append(_forca_label(r_val))
            cor = _cor_celula(r_val)
            if cor:
                ts.append(("BACKGROUND", (j, i), (j, i), cor))
            ts.append(("TEXTCOLOR", (j, i), (j, i), _cor_r(r_val) if not pd.isna(r_val) else colors.black))
        data.append(row)

    col_w = [5.5 * cm] + [3.5 * cm] * len(targets_presentes)
    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(ts))
    return t


def _tabela_historico(hist_df: pd.DataFrame) -> Table:
    colunas = ["salvo_em", "periodo_ini", "periodo_fim", "produto",
               "n_jumbos", "var_processo", "var_qualidade", "r", "forca", "observacao"]
    colunas = [c for c in colunas if c in hist_df.columns]
    cabecalhos = {
        "salvo_em": "Salvo em", "periodo_ini": "De", "periodo_fim": "Até",
        "produto": "Produto", "n_jumbos": "Jumbos",
        "var_processo": "Variável Processo", "var_qualidade": "Variável Qualidade",
        "r": "r", "forca": "Força", "observacao": "Observação",
    }

    df = hist_df[colunas].copy()
    if "r" in df.columns:
        df["r"] = df["r"].apply(lambda v: f"{v:+.3f}" if pd.notna(v) else "—")
    if "salvo_em" in df.columns:
        df["salvo_em"] = df["salvo_em"].apply(
            lambda v: v[:16].replace("T", " ") if isinstance(v, str) else v)

    data = [[cabecalhos.get(c, c) for c in colunas]]
    for _, row in df.head(60).iterrows():
        data.append([str(row[c]) if pd.notna(row[c]) else "—" for c in colunas])

    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("TEXTCOLOR",  (0, 0), (-1, 0), _HEADER_FG),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 6.5),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ALT_ROW]),
        ("LEFTPADDING",  (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
    ]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle(ts))
    return t


def _tabela_evolucao(comp_df: pd.DataFrame, freq_label: str) -> list:
    """Uma tabela por target mostrando a evolução r por período."""
    flowables = []
    styles = getSampleStyleSheet()
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=9, spaceAfter=4)

    for tgt in _TARGETS:
        sub = comp_df[comp_df["var_qualidade"] == tgt] if not comp_df.empty else pd.DataFrame()
        if sub.empty:
            continue

        periodos = sorted(sub["periodo"].unique())
        variaveis = sub["var_processo"].unique().tolist()

        flowables.append(Paragraph(f"{freq_label} — {tgt}", h3))

        cab = ["Variável"] + [str(p) for p in periodos]
        data = [cab]
        ts = [
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("TEXTCOLOR",  (0, 0), (-1, 0), _HEADER_FG),
            ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",   (0, 0), (-1, -1), 7),
            ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
            ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ALT_ROW]),
            ("LEFTPADDING",  (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING",   (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ]

        for i, var in enumerate(variaveis, start=1):
            row = [var]
            for j, per in enumerate(periodos, start=1):
                match = sub[(sub["var_processo"] == var) & (sub["periodo"] == per)]
                if match.empty:
                    row.append("—")
                else:
                    r_val = float(match.iloc[0]["r"])
                    row.append(f"{r_val:+.2f}")
                    cor = _cor_celula(r_val)
                    if cor:
                        ts.append(("BACKGROUND", (j, i), (j, i), cor))
                    ts.append(("TEXTCOLOR", (j, i), (j, i), _cor_r(r_val)))
            data.append(row)

        col_w = [4.5 * cm] + [max(2.2 * cm, 19 * cm / max(len(periodos), 1))] * len(periodos)
        t = Table(data, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle(ts))
        flowables.append(t)
        flowables.append(Spacer(1, 0.3 * cm))

    return flowables


def gerar_pdf_relatorio(
    historico_df: pd.DataFrame,
    comp_df: pd.DataFrame,
    df_corr_atual: Optional[pd.DataFrame] = None,
    freq_label: str = "Semana",
    titulo: str = "Relatório de Correlações — AT1",
) -> bytes:
    """Gera PDF e retorna os bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=titulo,
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14,
                        textColor=_HEADER_BG, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11,
                        textColor=_HEADER_BG, spaceAfter=4)
    normal = ParagraphStyle("n", parent=styles["Normal"], fontSize=8,
                            textColor=colors.HexColor("#495057"))
    legenda = ParagraphStyle("leg", parent=styles["Normal"], fontSize=7.5,
                             textColor=colors.HexColor("#6c757d"), leftIndent=8)

    gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")
    story = []

    # cabeçalho
    story.append(Paragraph(titulo, h1))
    story.append(Paragraph(f"Gerado em {gerado_em} — Suzano S.A. / Aracruz AT1 (Valmet DCT200HS)", normal))
    story.append(HRFlowable(width="100%", thickness=1, color=_HEADER_BG, spaceAfter=6))

    # correlações atuais
    if df_corr_atual is not None and not df_corr_atual.empty:
        story.append(Paragraph("Análise de Correlações (seleção atual)", h2))
        story.append(_tabela_corr(df_corr_atual, styles))
        story.append(Spacer(1, 0.4 * cm))

    # top críticas (|r| >= 0.35)
    if historico_df is not None and not historico_df.empty and "r" in historico_df.columns:
        criticas = (historico_df[historico_df["r"].abs() >= 0.35]
                    .sort_values("r", key=abs, ascending=False)
                    .drop_duplicates(subset=["var_processo", "var_qualidade"])
                    .head(20))
        if not criticas.empty:
            story.append(Paragraph("Top Correlações Críticas (|r| ≥ 0,35)", h2))
            story.append(_tabela_historico(criticas))
            story.append(Spacer(1, 0.4 * cm))

    # evolução por período
    if comp_df is not None and not comp_df.empty:
        story.append(Paragraph(f"Evolução das Correlações por {freq_label}", h2))
        story.extend(_tabela_evolucao(comp_df, freq_label))

    # histórico completo
    if historico_df is not None and not historico_df.empty:
        story.append(Paragraph("Histórico de Análises Salvas", h2))
        story.append(_tabela_historico(historico_df))
        story.append(Spacer(1, 0.4 * cm))

    # legenda
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#dee2e6"), spaceBefore=4))
    story.append(Paragraph("Interpretação:", ParagraphStyle("leg_h", parent=legenda, fontName="Helvetica-Bold")))
    story.append(Paragraph("Verde: correlação positiva (variáveis sobem juntas)   |   "
                            "Vermelho: correlação negativa (variáveis opostas)", legenda))
    story.append(Paragraph("|r| ≥ 0,70 → Forte   |   0,50–0,69 → Moderada   |   0,35–0,49 → Fraca   |   < 0,35 → Sem correlação significativa", legenda))

    doc.build(story)
    return buf.getvalue()
