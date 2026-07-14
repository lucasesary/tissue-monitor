# DATA_MAP.md — Mapeamento de fontes de dados (Maio 2026)

Atualizado: 2026-07-13
Período coberto pelos arquivos mapeados: 2026-05-01 a 2026-05-31

---

## 1. Boletim de Produção

**Arquivo:** `dados/producao/Boletim de Fabricação_Maio26.xlsx`
**Aba:** `Dados Prod. Maio_26`
**Shape:** 1.626 linhas × 23 colunas | zero nulos

### Timestamp
| Campo | Nome exato | Dtype | Formato |
|---|---|---|---|
| Data/hora da bobina | `Data` | datetime64[us] | `2026-05-01 00:43:00` |

Granularidade: por bobina (evento, não intervalo fixo).
Cobertura: 2026-05-01 00:43 → 2026-05-31 23:51. Mês completo.

### Colunas relevantes
| Campo | Nome exato | Dtype | Notas |
|---|---|---|---|
| Chave de rastreamento | `Track Num` | int64 | ex: 70082205 |
| Peso estimado | `Peso Estimado` | int64 | kg |
| Peso balanço | `Peso Balanço` | int64 | kg |
| Turma | `Turma` | str | A / B / C / D / E |
| Diâmetro | `Diametro` | float64 | mm |
| Largura | `Largura` | float64 | mm |
| Comprimento | `Comprimento` | int64 | m |
| Gramatura | `Gr/m2` | float64 | g/m² |
| Quebras | `Quebras` | int64 | contagem |
| Velocidade | `Velocidade` | int64 | m/min |
| Duração | `Duração` | float64 | minutos |
| Corrida | `Corrida` | int64 | número da corrida |
| Família fabricada | `Familia Fabricada` | str | ex: BDRR153BR |
| Família atual | `Familia Atual` | str | pode diferir da fabricada |
| Status fabricado | `Status Fabricado` | str | só "H" no período |
| Status atual | `Status Atual` | str | G / H / C |
| Unidade | `Unidade` | str | ex: RKAAALKK |

---

## 2. Boletim de Qualidade

**Arquivo:** `dados/qualidade/Dados Qualidade_Maio26.xlsx`
**Aba:** `Dados Qualidade_Maio26`
**Shape:** 1.619 linhas × 68 colunas

### Atenção ao parsing
O cabeçalho real fica na **linha 0 do DataFrame** (não no cabeçalho do Excel).
Ler com `header=0`, usar `iloc[0]` como nomes de coluna, descartar a linha 0, reiniciar índice.
Todos os valores numéricos são string com **vírgula** como decimal — converter antes de calcular.

### Timestamp
| Campo | Nome exato | Dtype raw | Formato |
|---|---|---|---|
| Data/hora da bobina | `Data` | object | `2026-05-01 00:43:00` |

Granularidade: por bobina. Mesmo horário que Produção para a mesma bobina.
Cobertura: 2026-05-01 → 2026-05-31. Mês completo.

### Chave de rastreamento
| Campo | Nome exato | Dtype raw |
|---|---|---|
| Num. rastreamento | `Num.Rastreamento` | object (numérico como string) |

**Confirmado:** `Num.Rastreamento` (Qualidade) = `Track Num` (Produção).
Mesma chave numérica, mesmo valor, mesmo timestamp. Join direto por igualdade inteira.

### Cobertura do join
- Produção: 1.626 chaves únicas
- Qualidade: 1.619 chaves únicas
- Interseção: **1.619 bobinas** (100% da qualidade tem par na produção)
- Só em produção (sem qualidade): 7 bobinas — descartadas no join inner

### Colunas relevantes para espessura
| Campo | Nome exato | Nulos | Faixa real (após conversão) |
|---|---|---|---|
| Espessura (média) | `Espessura` | 0 / 1619 | 0,80 – 1,05 mm |
| Espessura lado A | `Espessura A` | 0 / 1619 | — |
| Espessura lado C | `Espessura C` | 0 / 1619 | — |
| Espessura meio | `Espessura M` | 0 / 1619 | — |
| Umidade (lab) | `Umidade` | **122 / 1619** | — |
| Umidade lado A | `Umidade A` | — | — |
| Umidade lado C | `Umidade C` | — | — |
| Umidade QCS | `UmidadeQCS` | **415 / 1619** (74% cobertura) | 2,5 – 6,28 % |
| Gramatura | `Gramatura` | 1 / 1619 | — |
| Tração Longitudinal | `Tração Longitudinal` | 0 / 1619 | — |
| Tração Transversal | `Tração Transversal` | 0 / 1619 | — |
| Status | `Status` | 0 / 1619 | G (conforme) / C (não-conforme) |
| Código Refugo | `Código Refugo` | **1580 / 1619** (só 39 refugadas) | ver abaixo |
| Turma | `Turma` | 0 / 1619 | A / B / C / D / E |
| Bulk | `Bulk` | — | — |
| Handfeel | `Handfeel` | — | — |
| Maciez TSA | `Maciez TSA` | — | — |

### Códigos de refugo presentes no período
| Código | Descrição | N bobinas |
|---|---|---|
| HLD-HOLD | Retenção administrativa | — |
| 102-ESPESSURA OFFSPEC | Espessura fora de especificação | **4** (todas em 05/05) |
| 138-LARGURA MENOR | Largura abaixo do mínimo | — |
| 117-PINTAS BRANCAS | Defeito visual | — |
| 110-EXCESSO DE FUROS | Defeito de furação | — |

Bobinas refugadas por espessura: 4 bobinas, 05/05/2026, espessura medida 0,87–0,88 mm.
Universo pequeno — leitura de padrão com ressalva explícita (ver PASSO 4).

---

## 3. Histórico de Processo (OPC UA)

**Arquivo:** `dados/processo/Analise_parametros_maio26.csv`
**Encoding:** latin-1
**Separador:** ponto-e-vírgula (`;`)

### Estrutura especial do CSV
```
Linha 0: "Média:;4,1431;32,4599;..."   ← estatísticas de exportação, ignorar
Linha 1: "nan;Consistência Dump Tower;Potência Ref. 01;..."  ← NOMES DOS PARÂMETROS
Linha 2: "data;OPC UA.Tissue.1.4101-QC-204.F:ME;..."        ← tags OPC UA
Linha 3+: dados reais "01-mai-26 00:00:00;4,241;36,697;..."
```
Usar linha 1 como nomes de coluna, col[0] = "timestamp". Dados a partir da linha 3.

### Timestamp
| Campo | Nome na linha 2 | Formato raw | Após parse |
|---|---|---|---|
| Data/hora | `data` (col 0) | `01-mai-26 00:00:00` | datetime, PT-BR com meses abreviados |

Granularidade: **5 minutos** (mediana 300s, confirmado).
Cobertura: 2026-05-01 00:00 → 2026-05-31 23:55. **8.928 registros**.

### Regra de join com Produção/Qualidade
Usar o registro de processo com timestamp **imediatamente anterior** ao horário da bobina.
Implementar com `merge_asof(..., direction='backward')`.
Nunca arredondar para hora cheia — o registro anterior é o estado real durante a produção da bobina.

### Colunas disponíveis para análise de espessura
| Parâmetro | Nome exato | Não-nulos | Coluna no CSV |
|---|---|---|---|
| Prensa ViscoNip | `PRENSA` | 8919/8928 | col[34] |
| Potência Ref. 01 entrada | `Potência Ref. 01` | 8922/8928 | col[2] |
| Potência Ref. 02 entrada | `Potência Ref. 02` | 8922/8928 | col[3] |
| Pressão entrada Ref 01 | `Pressão Entrada Ref 01` | 8928/8928 | col[23] |
| Pressão saída Ref 01 | `Pressão Saída Ref 01` | 8928/8928 | col[25] |
| Pressão entrada Ref 02 | `Pressão Entrada Ref 02` | 8928/8928 | col[24] |
| Pressão saída Ref 02 | `Pressão Saída Ref 02` | 8928/8928 | col[26] |
| Temperatura Capota LS | `Temperatura Capota LS` | 8928/8928 | col[12] |
| Temperatura Capota LU | `Temperatura Capota LU` | 8928/8928 | col[13] |
| Redry | `Redry` | 8928/8928 | col[15] |
| Extração Vapor | `Extração Vapor` | 8928/8928 | col[18] |
| Umidade QCS (processo) | `umidade QCS` | 8928/8928 | col[33] |
| Consistência TQ Máquina | `Consistência TQ Máquina` | 8928/8928 | col[4] |
| Consistência TQ Mistura | `Consistência TQ Mistura` | 8928/8928 | col[5] |
| Consistência Dump Tower | `Consistência Dump Tower` | 8928/8928 | col[1] |
| Fluxo de Massa | `Fluxo de Massa` | 8928/8928 | col[6] |
| Velocidade MP | `Velocidade MP` | 8928/8928 | col[27] |
| Jato/Tela | `Jato/Tela` | 8106/8928 | col[8] — 822 nulos |
| Crepe | `Crepe` | 8913/8928 | col[22] |
| Tensão Tela | `Tensão Tela` | 8928/8928 | col[9] |
| Tensão Feltro | `Tensão Feltro` | 8928/8928 | col[10] |
| Bulbo | `Bulbo` | 8928/8928 | col[14] |
| Diferencial | `Diferencial` | 8928/8928 | col[17] |
| QCS Gramatura | `QCS GRAMATURA` | 8928/8928 | col[32] |
| Dumper LA | `Dumper LA` | 8897/8928 | col[28] |
| Dumper LC | `Dumper LC` | 8367/8928 | col[29] |

### Colunas presentes mas sem dados (0 valores não-nulos)
Exportadas pelo PI DataLink mas sem leitura no período — **não usar**:
- `Temperatura Superfície Yankee LA` / `LC`
- `Velocidade Recirculação Capotas`
- `Velocidade Extração Capotas`
- `Pressão Entrada Vapor`
- `Temperatura Vapor`
- `Nível dos tanques de massa`
- `Vácuo da pickup`
- `Quebras` (coluna de processo — diferente da coluna de mesmo nome na produção)

### col_35 — coluna sem nome de parâmetro
Tag OPC UA: `AT01_Qualidade.Produto Aracruz.89f738ba-...`
Provavelmente produto atual vindo do QCS — não usar em correlações numéricas, investigar separadamente.

---

## 4. Correspondência de chaves entre fontes

| Fonte | Campo | Tipo | Join |
|---|---|---|---|
| Produção | `Track Num` | int64 | = |
| Qualidade | `Num.Rastreamento` | object → int | = |
| Processo | timestamp 5 min | datetime | `merge_asof backward` |

**Track Num = Num.Rastreamento:** confirmado por valores idênticos e timestamps coincidentes
em múltiplas amostras verificadas manualmente.

**Tratamento de identificadores:**
- `Track Num` / `Num.Rastreamento`: chave pura — excluir de qualquer cálculo de correlação
- `Turma`: variável categórica com significado operacional — manter como contexto de segmentação, não correlacionar numericamente

---

## 5. Alertas e restrições para o Passo 2

1. **Espessura como string:** valores vêm com vírgula decimal em Qualidade — converter
   com `.str.replace(",", ".").astype(float)` antes de qualquer cálculo.

2. **Refugo por espessura:** apenas 4 bobinas, concentradas em 05/05.
   Amostra muito pequena para inferência de padrão — reportar explicitamente.

3. **UmidadeQCS em Qualidade:** 26% de nulos (415/1619). Usar `umidade QCS` do
   processo (8928/8928) como complemento; são fontes diferentes com cobertura diferente.

4. **Temperatura Yankee:** coluna existe no CSV de processo mas está **completamente vazia**.
   Dado de temperatura Yankee vem apenas do SQLite `temperatura_yankee.db` (leituras manuais),
   que não entra neste cruzamento automaticamente.

5. **Jato/Tela:** 822 nulos (9,2%) no processo — verificar se coincidem com paradas
   antes de imputar ou descartar.
