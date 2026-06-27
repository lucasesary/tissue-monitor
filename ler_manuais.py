# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
import fitz  # pymupdf
from pathlib import Path

BASE = Path("conhecimento/manuais_processo/Manuais")

arquivos = {
    "Introducao":   BASE / "Code02 Tissue Machine/Ara1_000_Introduction_RevA_pt-BR.pdf",
    "Headbox":      BASE / "Code02 Tissue Machine/Ara1_100_Headbox_RevA_pt-BR.pdf",
    "Formacao":     BASE / "Code02 Tissue Machine/Ara1_200_Former RevA_pt-BR.pdf",
    "Prensa":       BASE / "Code02 Tissue Machine/Ara1_300_PressSection_RevA_pt-BR.pdf",
    "ViscoNip":     BASE / "Code02 Tissue Machine/Ara1_340_ViscoNipRoll_RevA_pt-BR.pdf",
    "Yankee":       BASE / "Code02 Tissue Machine/Ara1_500_YankeeSection_RevA_pt-BR.pdf",
    "StockPrep":    BASE / "Code07 Process Equipment/Reference/Ara1_StockPrep_TrainingMaterial_STO_brpt_00.pdf",
    "AirCap":       BASE / "Code03 AirCap & HotAir/Aracruz - AAC - Training material_pt-BR.pdf",
    "ReDry":        BASE / "Code03 AirCap & HotAir/Aracruz - ReDry ViscoNip - Training material_pt-BR.pdf",
    "Vapor_Yankee": BASE / "Code15 Utility System/Yankee Steam & Condensate System Training Aracruz AT1 RevA_pt-BR.pdf",
}

def extrair_texto(pdf_path: Path, max_pages: int = 8) -> str:
    try:
        doc = fitz.open(str(pdf_path))
        textos = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            textos.append(page.get_text())
        return "\n".join(textos)
    except Exception as e:
        return f"[ERRO: {e}]"

for nome, arq in arquivos.items():
    print(f"\n{'='*60}")
    print(f"  {nome.upper()} — {arq.name}")
    print('='*60)
    texto = extrair_texto(arq, max_pages=6)
    # mostra primeiros 2000 chars
    print(texto[:2500])
    print("...")
