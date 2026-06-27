from dashboard import carregar_qualidade, carregar_producao, carregar_downtime_paradas, _opcoes_arquivo, BASE_DIR, carregar_pacote
from pathlib import Path

print("=== Processo ===")
opts = _opcoes_arquivo(BASE_DIR)
p = Path(opts[0]["value"])
pkg = carregar_pacote(p)
print("OK - {} registros, {} params".format(pkg["registros"], pkg["n_ativos"]))

print("=== Qualidade ===")
dq, specs = carregar_qualidade()
print("OK - {} registros, {} specs".format(len(dq), len(specs)))

print("=== Producao ===")
dp = carregar_producao()
print("OK - {} registros".format(len(dp)))

print("=== Downtime ===")
dd = carregar_downtime_paradas()
print("OK - {} registros".format(len(dd)))
