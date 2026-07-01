"""
executar_estudo_completo.py — Orquestra, do zero, o pipeline completo do
estudo de rotas alternativas das OAEs do Ceará.

Universo de OAEs identificado por CodPro (chave PROARTE/DNIT, lida de
codpro.csv) — não mais por SGE, já que várias OAEs do PROARTE não têm
código SGE atribuído.

Historicamente esse estudo foi rodado como vários scripts separados,
executados manualmente em sequência (batch_rotas_route_study.py,
depois _reanalise, depois _100km, depois _colapsado, depois _refazer_a2).
Este script substitui todos eles: roda as mesmas fases, na mesma ordem,
em uma única execução.

  Fase 1 — Passagem principal (raio 50 km, A1 + A2, todos os CodPro de codpro.csv)
  Fase 2 — Consolidação (gera consolidado.csv)
  Fase 3 — Reanálise das falhas (status != "encontrada"), mesmo raio 50 km
  Fase 4 — Consolidação
  Fase 5 — Falhas remanescentes com raio ampliado para 100 km
           (A1 e A2 processados separadamente, cada um só com seus próprios CodPro falhos)
  Fase 6 — Consolidação
  Fase 7 — Reprocessa CodPro com status "no_colapsado" (idempotente: só muda
           o resultado se algum parâmetro do motor, ex. TOLERANCIA_NO_M em
           batch_rotas_route_study.py, tiver sido ajustado manualmente antes)
  Fase 8 — Consolidação
  Fase 9 — Recalcula a Análise A2 para TODOS os CodPro já processados
           (desligada por padrão — só necessária se SUPERFICIES_A2 mudar
           depois da Fase 1, ex. inclusão de uma nova superfície aceita)
  Fase 10 — Consolidação final

Cada fase lê o consolidado.csv mais recente e é pulada automaticamente se
não houver nada a fazer. Rodar este script com análise_rotas/ vazia (ou
inexistente) reproduz integralmente o consolidado.csv final do estudo.

Uso:
    python executar_estudo_completo.py
"""

import os
import re
import pandas as pd

import batch_rotas_route_study as brs
import consolidar_dados

CONSOLIDADO_CSV = "consolidado.csv"
PASTA_ANALISE   = "análise_rotas"
_RE_CODPRO      = re.compile(r"^OAE\d+$", re.IGNORECASE)

# Refaz a Análise A2 para todos os CodPro já processados (Fase 9). Deixe False
# num run do zero; ative manualmente só depois de mudar SUPERFICIES_A2.
REFAZER_A2 = False

# Parâmetros "de fábrica" do motor (batch_rotas_route_study.py) — usados para
# restaurar o estado do módulo entre fases, já que todas elas rodam no mesmo
# processo Python e compartilham as variáveis globais de brs.
_CSV_CODPRO_PADRAO = "codpro.csv"
_ANALISES_PADRAO   = ["A1", "A2"]
_RAIO_PADRAO_KM    = 50


def _restaurar_padroes():
    brs.CSV_CODPRO  = _CSV_CODPRO_PADRAO
    brs.ANALISES    = list(_ANALISES_PADRAO)
    brs.RAIO_OSM_KM = _RAIO_PADRAO_KM


def _titulo(texto, char="="):
    print(f"\n{char * 70}\n{texto}\n{char * 70}")


def _consolidar(fase):
    _titulo(f"{fase} — consolidar_dados", char="#")
    consolidar_dados.main()


def _ler_consolidado():
    if not os.path.exists(CONSOLIDADO_CSV):
        return None
    return pd.read_csv(CONSOLIDADO_CSV, index_col="CodPro")


def _rodar_com_lista(codpros, analises, tmp_csv, titulo):
    """Grava lista temporária de CodPro, ajusta o módulo do motor e roda brs.main()."""
    pd.DataFrame({"CODPRO": codpros}).to_csv(tmp_csv, index=False)
    brs.CSV_CODPRO = tmp_csv
    brs.ANALISES   = analises
    _titulo(f"{titulo} — {len(codpros)} CodPro  |  {analises}")
    try:
        brs.main()
    finally:
        if os.path.exists(tmp_csv):
            os.remove(tmp_csv)


def fase_1_principal():
    _restaurar_padroes()
    _titulo(f"FASE 1 — Passagem principal (raio {_RAIO_PADRAO_KM} km, todos os CodPro)", char="#")
    brs.main()


def fase_3_reanalise():
    df = _ler_consolidado()
    if df is None:
        print("consolidado.csv ausente — Fase 3 pulada.")
        return
    codpros = df[(df["status_A1"] != "encontrada") | (df["status_A2"] != "encontrada")].index.tolist()
    if not codpros:
        print("\nFASE 3 — nenhuma falha em A1/A2. Pulando.")
        return
    _restaurar_padroes()
    _rodar_com_lista(codpros, list(_ANALISES_PADRAO), "_codpro_reanalise.csv",
                     f"FASE 3 — Reanálise (raio {_RAIO_PADRAO_KM} km)")


def fase_5_100km():
    df = _ler_consolidado()
    if df is None:
        print("consolidado.csv ausente — Fase 5 pulada.")
        return
    codpros_a1 = df[df["status_A1"] != "encontrada"].index.tolist()
    codpros_a2 = df[df["status_A2"] != "encontrada"].index.tolist()
    if not codpros_a1 and not codpros_a2:
        print("\nFASE 5 — nenhuma falha remanescente em A1/A2. Pulando.")
        return

    _restaurar_padroes()
    brs.RAIO_OSM_KM = 100
    if codpros_a1:
        _rodar_com_lista(codpros_a1, ["A1"], "_codpro_100km_a1.csv", "FASE 5.1 — Raio 100 km (A1)")
    if codpros_a2:
        _rodar_com_lista(codpros_a2, ["A2"], "_codpro_100km_a2.csv", "FASE 5.2 — Raio 100 km (A2)")


def fase_7_colapsado():
    df = _ler_consolidado()
    if df is None:
        print("consolidado.csv ausente — Fase 7 pulada.")
        return
    codpros = df[(df["status_A1"] == "no_colapsado") | (df["status_A2"] == "no_colapsado")].index.tolist()
    if not codpros:
        print("\nFASE 7 — nenhum CodPro com status 'no_colapsado'. Pulando.")
        return
    _restaurar_padroes()
    _rodar_com_lista(codpros, list(_ANALISES_PADRAO), "_codpro_colapsado.csv",
                     "FASE 7 — Reprocessa nós colapsados")


def fase_9_refazer_a2():
    if not os.path.isdir(PASTA_ANALISE):
        print(f"'{PASTA_ANALISE}' não encontrada — Fase 9 pulada.")
        return
    codpros = [
        nome for nome in sorted(os.listdir(PASTA_ANALISE))
        if os.path.isdir(os.path.join(PASTA_ANALISE, nome)) and _RE_CODPRO.match(nome)
    ]
    if not codpros:
        print("\nFASE 9 — nenhum CodPro processado em análise_rotas/. Pulando.")
        return
    _restaurar_padroes()
    print(f"\nFASE 9 — Filtro A2 atual: {brs.SUPERFICIES_A2} + NULL")
    _rodar_com_lista(codpros, ["A2"], "_codpro_refazer_a2.csv", "FASE 9 — Refazer A2 (todos os CodPro)")


def main():
    fase_1_principal()
    _consolidar("FASE 2")

    fase_3_reanalise()
    _consolidar("FASE 4")

    fase_5_100km()
    _consolidar("FASE 6")

    fase_7_colapsado()
    _consolidar("FASE 8")

    if REFAZER_A2:
        fase_9_refazer_a2()
        _consolidar("FASE 10")

    _titulo("ESTUDO COMPLETO — resultados finais em consolidado.csv / consolidado.xlsx")
    print("Para gerar as figuras finais, execute visualizar_resultados.ipynb.")


if __name__ == "__main__":
    main()
