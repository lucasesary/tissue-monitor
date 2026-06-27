# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from integracao import carregar_qualidade, carregar_producao, correlacionar_processo_qualidade
from analisar import carregar_dados, _encontrar_arquivo

base = Path(".")
arq = _encontrar_arquivo(base)
print(f"OPC UA: {arq}")
dados_opc, _, _ = carregar_dados(arq)
print(f"OPC UA: {len(dados_opc)} registros | {dados_opc['timestamp'].min()} ate {dados_opc['timestamp'].max()}")

dq, specs = carregar_qualidade()
dp = carregar_producao()
print(f"Qualidade: {len(dq)} jumbos | {dq['Data'].min()} ate {dq['Data'].max()}")
print(f"Producao:  {len(dp)} jumbos")
print(f"Specs BTRR157BR: {'SIM' if 'BTRR157BR' in specs.index.get_level_values(0) else 'NAO'}")
print(f"Todos produtos com spec: {specs.index.get_level_values(0).unique().tolist()}")

df_j, df_corr = correlacionar_processo_qualidade(dados_opc, dq, dp)
print(f"\nJumbos cruzados: {len(df_j)}")
print(f"Matriz correlacao shape: {df_corr.shape}")
if not df_corr.empty:
    print("\nTop 12 variaveis mais correlacionadas:")
    print(df_corr.head(12).to_string())
    print("\nColunas (targets):", list(df_corr.columns))
