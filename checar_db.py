import db

df = db.carregar_qualidade_db(dias=60)
print(f"Registros qualidade no banco: {len(df)}")
if not df.empty:
    print(f"Periodo: {df['Data'].min().strftime('%d/%m/%Y')} ate {df['Data'].max().strftime('%d/%m/%Y')}")
    print(f"Ultimas 3 unidades: {df['Unidade'].tail(3).tolist()}")

log = db.carregar_qualidade_db.__module__
import psycopg2, os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path("C:/Users/Lucas Brígido/teste do claude/analisar_tissue.py/.env"))
con = psycopg2.connect(os.environ["DATABASE_URL"])
cur = con.cursor()
cur.execute("SELECT status, registros_inseridos, arquivo_nome, ingested_at FROM ingestao_log ORDER BY ingested_at DESC LIMIT 5")
rows = cur.fetchall()
print("\n--- Ultimos logs de ingestao ---")
for r in rows:
    print(r)
con.close()
