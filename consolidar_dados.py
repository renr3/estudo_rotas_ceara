"""
consolidar_dados.py — Consolida os resultados de batch_rotas_route_study.py.

Lê todos os resultado_A1.txt e resultado_A2.txt em análise_rotas/<CodPro>/
e monta um DataFrame com as distâncias de rota por OAE, indexado por CodPro
(chave PROARTE/DNIT, ex. "OAE657"). O SGE, quando a OAE tiver um, é mantido
como coluna informativa (fica vazio/NaN para OAEs sem SGE atribuído).

Saída:
  consolidado.csv   — arquivo principal (UTF-8 com BOM, compatível com Excel)
  consolidado.xlsx  — mesma tabela em Excel (se openpyxl estiver instalado)
"""

import os
import re
import pandas as pd

PASTA_ANALISE = "análise_rotas"
ARQUIVO_CSV   = "consolidado.csv"
ARQUIVO_XLSX  = "consolidado.xlsx"

_RE_CODPRO = re.compile(r"^OAE\d+$", re.IGNORECASE)


def _ler_resultado(caminho):
    """Lê um resultado_*.txt (formato key=value) e retorna dict."""
    dados = {}
    if not os.path.exists(caminho):
        return dados
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if "=" in linha:
                chave, _, valor = linha.partition("=")
                dados[chave.strip()] = valor.strip()
    return dados


def main():
    if not os.path.isdir(PASTA_ANALISE):
        raise FileNotFoundError(
            f"Pasta '{PASTA_ANALISE}' não encontrada. "
            "Execute batch_rotas_route_study.py antes de consolidar."
        )

    registros = []
    sem_resultado = []

    for codpro in sorted(os.listdir(PASTA_ANALISE)):
        pasta_codpro = os.path.join(PASTA_ANALISE, codpro)
        if not os.path.isdir(pasta_codpro):
            continue
        if not _RE_CODPRO.match(codpro):
            continue

        r1 = _ler_resultado(os.path.join(pasta_codpro, "resultado_A1.txt"))
        r2 = _ler_resultado(os.path.join(pasta_codpro, "resultado_A2.txt"))

        if not r1 and not r2:
            sem_resultado.append(codpro)
            continue

        def _km(d):
            try:
                return float(d["KM_TOTAL"])
            except (KeyError, ValueError):
                return None

        def _nvias(d):
            try:
                return int(d["N_VIAS"])
            except (KeyError, ValueError):
                return None

        def _sge(d):
            try:
                return int(d["SGE"])
            except (KeyError, ValueError):
                return None

        registros.append({
            "CodPro":      codpro,
            "SGE":         _sge(r1) or _sge(r2),
            "status_A1":   r1.get("STATUS", ""),
            "km_A1":       _km(r1),
            "n_vias_A1":   _nvias(r1),
            "ids_vias_A1": r1.get("IDS_VIAS", ""),
            "status_A2":   r2.get("STATUS", ""),
            "km_A2":       _km(r2),
            "n_vias_A2":   _nvias(r2),
            "ids_vias_A2": r2.get("IDS_VIAS", ""),
        })

    if not registros:
        print("Nenhum resultado encontrado em", PASTA_ANALISE)
        return

    df = (
        pd.DataFrame(registros)
        .set_index("CodPro")
        .sort_index()
    )

    # ── Sumário ──────────────────────────────────────────────
    total = len(df)

    # Legenda dos status possíveis
    _DESC = {
        "encontrada":    "Rota encontrada",
        "grafo_desconexo": "Sem rota (grafo desconexo — ponte sem alternativa)",
        "sem_interseccao": "Sem interseção (exclusão não atingiu nenhuma via)",
        "no_colapsado":  "Nós colapsados no grafo (exclusão insuficiente)",
        "rede_vazia":    "Rede vazia na área",
        "erro_osm":      "Erro ao carregar OSM",
        "nao_encontrada":"Não encontrada (status legado)",
        "":              "Status ausente (arquivo incompleto)",
    }

    print(f"{'─'*60}")
    print(f"OAEs processadas : {total}")
    if sem_resultado:
        print(f"Pastas sem .txt  : {len(sem_resultado)}  {sem_resultado}")
    print(f"{'─'*60}")

    for label in ("A1", "A2"):
        col = f"status_{label}"
        contagens = df[col].value_counts()
        print(f"\nAnálise {label}:")
        for status, n in contagens.items():
            desc = _DESC.get(status, status)
            print(f"  {n:>4}  ({n/total*100:5.1f}%)  {desc}")

    print(f"\n{'─'*60}")
    for label, col in [("A1", "km_A1"), ("A2", "km_A2")]:
        serie = df.loc[df[f"status_{label}"] == "encontrada", col].dropna()
        if serie.empty:
            continue
        print(f"km {label}  — min {serie.min():.2f}  |  med {serie.median():.2f}"
              f"  |  max {serie.max():.2f}  |  média {serie.mean():.2f}")

    print(f"{'─'*60}")

    # ── Salvar ───────────────────────────────────────────────
    df.to_csv(ARQUIVO_CSV, encoding="utf-8-sig")
    print(f"Salvo : {ARQUIVO_CSV}")

    try:
        df.to_excel(ARQUIVO_XLSX)
        print(f"Salvo : {ARQUIVO_XLSX}")
    except Exception as e:
        print(f"Excel não gerado : {e}")

    return df


if __name__ == "__main__":
    main()
