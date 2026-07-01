"""
processar_novas_oaes.py — script TEMPORÁRIO, uso único.

Processa apenas as 4 OAEs que passaram a fazer parte do universo do estudo
depois da troca da chave de referência de SGE para CodPro (elas não têm SGE
atribuído, então não entravam no antigo sges.csv, mas têm CodPro cadastrado
no PROARTE e por isso aparecem em codpro.csv):

    OAE6835 — Ponte km 263,11
    OAE6836 — Ponte sobre o Rio Jaguaribe (LE) CICLOVIA
    OAE7291 — Ponte sobre o Rio Cangati (Ponte sobre o Braço do Rio Choró)
    OAE7322 — Ponte sobre o Rio Cangati

Roda A1 + A2 (raio padrão 50 km) só para esses 4 CodPro, salva os KMLs em
análise_rotas/<CodPro>/ como qualquer outra OAE, e ao final re-consolida
análise_rotas/ inteira (as 306 OAEs já existentes + essas 4), atualizando
consolidado.csv/xlsx para as 310 OAEs do universo completo.

Depois de rodado uma vez com sucesso, este script pode ser apagado — seu
papel é só preencher a lacuna criada pela migração para CodPro; qualquer
reprocessamento futuro já passa a usar codpro.csv (310 entradas) direto em
executar_estudo_completo.py.

Uso:
    python processar_novas_oaes.py
"""

import os
import pandas as pd

import batch_rotas_route_study as brs
import consolidar_dados

NOVOS_CODPRO = ["OAE6835", "OAE6836", "OAE7291", "OAE7322"]
_TMP_CSV     = "_codpro_novas_oaes.csv"


def main():
    print(f"{'=' * 70}")
    print(f"Processando {len(NOVOS_CODPRO)} OAEs novas (CodPro sem SGE)")
    print(f"{'=' * 70}")
    for cp in NOVOS_CODPRO:
        print(f"  {cp}")

    pd.DataFrame({"CODPRO": NOVOS_CODPRO}).to_csv(_TMP_CSV, index=False)
    brs.CSV_CODPRO  = _TMP_CSV
    brs.ANALISES    = ["A1", "A2"]
    brs.RAIO_OSM_KM = 50

    try:
        brs.main()
    finally:
        if os.path.exists(_TMP_CSV):
            os.remove(_TMP_CSV)

    print(f"\n{'=' * 70}")
    print("Re-consolidando análise_rotas/ inteira (306 antigas + 4 novas)")
    print(f"{'=' * 70}")
    consolidar_dados.main()


if __name__ == "__main__":
    main()
