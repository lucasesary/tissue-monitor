"""
migrar_specs.py — script local, roda uma vez para popular a tabela specs no Neon.
"""
from pathlib import Path
import pandas as pd
from integracao import _parsear_specs
from db import init_db, upsert_specs

qual_dir = Path("dados/qualidade")
arq = next(
    (p for p in sorted(qual_dir.glob("*.xls*"), key=lambda p: p.stat().st_mtime, reverse=True)
     if any("spec" in s.lower() or "especifica" in s.lower() for s in pd.ExcelFile(p).sheet_names)),
    None,
)
if not arq:
    print("Arquivo de specs não encontrado")
    raise SystemExit(1)

df = _parsear_specs(arq)
print(f"Specs lidas: {df.shape} — produtos: {df.index.get_level_values(0).unique().tolist()}")

init_db()
n = upsert_specs(df)
print(f"Linhas inseridas/atualizadas no banco: {n}")
