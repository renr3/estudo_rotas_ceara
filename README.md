# Estudo de Rotas Alternativas — OAEs do Ceará

Esta pasta contém tudo o que é necessário para reproduzir o estudo de rotas
alternativas das OAEs (pontes/viadutos) do Ceará, do dado de entrada até as
figuras finais. É uma cópia organizada e independente do que estava
espalhado pela raiz do repositório — nada aqui depende de arquivos de fora
desta pasta.

## Estrutura

```
estudo_rotas_ceara/
├── codpro.csv                        # 310 CodPro (chave PROARTE/DNIT) do universo do estudo
├── OAEs.xlsx                         # Base nacional de OAEs (coordenadas, nome, extensão)
├── controle_geral.xlsx               # VMDa (volume médio diário) por CodPro
├── nordeste_enriquecido.gpkg         # Rede viária OSM da região Nordeste, enriquecida com
│                                     # a superfície (Superficie) do SNV — entrada do motor de rotas
├── SNV_ESTADUAL_planarizado.gpkg     # Malha federal (SNV) + estadual planarizada — usada só
│                                     # para desenhar as vias no mapa final (não no cálculo de rota)
│
├── batch_rotas_route_study.py        # Motor do estudo (recorte de rede, grafo, rota, KMLs)
├── consolidar_dados.py               # Lê análise_rotas/ e gera consolidado.csv/xlsx
├── executar_estudo_completo.py       # Orquestra o pipeline completo (ver abaixo)
├── processar_novas_oaes.py           # Script pontual que processou as 4 OAEs sem SGE (ver histórico)
│
├── análise_rotas/                    # Saída do motor: 1 subpasta por CodPro (KMLs + resultado_A*.txt)
├── consolidado.csv / consolidado.xlsx   # Tabela consolidada final (1 linha por OAE, indexada por CodPro)
├── visualizar_resultados.ipynb       # Notebook que gera as figuras e KMLs de investigação
└── mapa_*.png, histograma_*.png, scatter_a1_a2.png, mapa_calor_A*.kml   # Saídas do notebook
```

## O que o estudo faz

Para cada uma das 310 OAEs do Ceará listadas em `codpro.csv` — pontes e
viadutos sob administração federal cadastrados no PROARTE/DNIT —, calcula a
rota alternativa (desvio) caso a OAE seja interditada, em duas variantes:

- **Análise A1** — todas as superfícies de via (sem filtro).
- **Análise A2** — apenas vias pavimentadas / em duplicação (`PAV`, `DUP`,
  `EOD`) + segmentos sem superfície cadastrada.

O raio de busca padrão é 50 km em torno da OAE; falhas são reprocessadas com
raio ampliado (ver Fase 5 abaixo).

### Chave de referência: CodPro, não SGE

O universo do estudo é identificado pelo **CodPro** (chave do PROARTE/DNIT,
ex. `OAE657`), lido de `codpro.csv`. O **SGE**, quando a OAE tem um, é
mantido como coluna informativa em `consolidado.csv` e nos arquivos
`resultado_A*.txt` — mas não é mais a chave de nada no pipeline.

Isso importa porque **nem toda OAE do PROARTE tem SGE atribuído**. O estudo
original usava `sges.csv` (306 SGEs) como universo; ao trocar para
`codpro.csv` (310 CodPro), 4 OAEs que não tinham SGE — e por isso ficavam de
fora — entraram no estudo:

| CodPro  | OAE                                                    |
|---------|---------------------------------------------------------|
| OAE6835 | Ponte km 263,11                                          |
| OAE6836 | Ponte sobre o Rio Jaguaribe (LE) CICLOVIA                |
| OAE7291 | Ponte sobre o Rio Cangati (Ponte sobre o Braço do Rio Choró) |
| OAE7322 | Ponte sobre o Rio Cangati                                |

Essas 4 foram processadas por `processar_novas_oaes.py` (script pontual,
uso único — pode ser apagado; futuras execuções já processam as 310 direto
via `executar_estudo_completo.py`). As pastas de `análise_rotas/` das 306
OAEs originais foram renomeadas de `<SGE>/` para `<CodPro>/` para manter o
pipeline consistente numa única chave.

## Pipeline — como reproduzir do zero

```
python executar_estudo_completo.py     # roda o estudo inteiro (pode levar horas)
```

Isso executa, em sequência, o que historicamente foi rodado como 5 scripts
separados (`batch_rotas_route_study.py`, `_reanalise`, `_100km`,
`_colapsado`, `_refazer_a2` — hoje consolidados neste único
`executar_estudo_completo.py`):

1. **Fase 1** — passagem principal: A1 + A2, raio 50 km, todos os CodPro.
2. **Fase 2** — consolidação (`consolidar_dados.py` → `consolidado.csv`).
3. **Fase 3** — reanálise de qualquer CodPro sem status `encontrada`, mesmo raio.
4. **Fase 4** — consolidação.
5. **Fase 5** — falhas remanescentes reprocessadas com raio ampliado para
   100 km (A1 e A2 tratadas separadamente).
6. **Fase 6** — consolidação.
7. **Fase 7** — reprocessa CodPro com status `no_colapsado` (só muda o
   resultado se algum parâmetro do motor, ex. `TOLERANCIA_NO_M` em
   `batch_rotas_route_study.py`, tiver sido ajustado manualmente antes).
8. **Fase 8** — consolidação.
9. **Fase 9** *(desligada por padrão — `REFAZER_A2 = False`)* — recalcula
   A2 para todos os CodPro já processados; só é necessária se a lista
   `SUPERFICIES_A2` mudar depois da Fase 1 (ex. inclusão de uma nova
   superfície aceita).
10. **Fase 10** — consolidação final.

Cada fase é pulada automaticamente se não houver nada a fazer (idempotente).

Depois, para gerar as figuras finais e os KMLs de investigação:

```
jupyter notebook visualizar_resultados.ipynb   # roda todas as células
```

Produz: `mapa_area_estudo.png`, `mapa_rotas_A1.png`, `mapa_rotas_A2.png`,
`mapa_impacto_veiculo_km_A2.png`, `histograma_rotas.png`,
`histograma_veiculo_km_desvio.png`, `scatter_a1_a2.png`,
`mapa_calor_A1.kml`, `mapa_calor_A2.kml`.

## O que fica de fora (e por quê)

Esta pasta guarda apenas os arquivos já processados que o motor usa
diretamente. Ficaram de fora, por serem dados brutos nacionais/regionais
muito grandes, reutilizados por outros estudos além do Ceará, e
re-obteníveis a qualquer momento:

- `nordeste.gpkg` (~1,9 GB) — extrato bruto da Geofabrik
  (https://download.geofabrik.de/south-america/brazil/nordeste-latest-free.gpkg.zip),
  usado por `enriquecer_osm_com_snv.py` para gerar `nordeste_enriquecido.gpkg`.
- `SNV_202604A.shp` (+ `.dbf/.prj/.shx/...`, ~126 MB) e `vw_cide_rod_2021.shp`
  (+ deps, ~290 MB) — shapefiles brutos do SNV/DNIT e da malha estadual
  (CIDE), usados por `preparar_rede.py` para gerar
  `SNV_ESTADUAL_planarizado.gpkg`.

Se precisar refazer a rede enriquecida do zero, os scripts de
pré-processamento são `preparar_rede.py` (gera o `_planarizado.gpkg` a
partir dos shapefiles brutos) e `enriquecer_osm_com_snv.py` (cruza esse
resultado com o `nordeste.gpkg` da Geofabrik) — ambos ficaram na raiz do
repositório por serem etapas nacionais/regionais, não específicas do Ceará.

Também não foram trazidos os scripts e notebooks anteriores ao estudo atual
(`batch_rotas.py`, `rota_alternativa_v5.py`, `preparar_rede_osm.py`,
`checkProperties.ipynb`, `insp_dbf.py`) — são exploração descartada ou
pipelines de outra região (ex. Centro-Oeste), sem relação com o estudo do
Ceará.

## O que fica de fora do controle de versão (`.gitignore`)

Esta pasta é pensada para uso local; ao publicar este projeto num
repositório Git, os arquivos abaixo **não devem ser versionados** (ver
`.gitignore`):

- **`controle_geral.xlsx`** — dado interno do DNIT/PROARTE: contém números
  de contrato (`MAN_Contrato`/`REAB_Contrato`), números de processo SEI
  (`MAN_Proc_SEI`/`REAB_Proc_SEI`), valores orçamentários previstos
  (`MAN_Valor_Previsto`/`REAB_Valor_Previsto`) e empresas supervisoras
  contratadas, para as 7.932 OAEs do banco nacional. **Não é dado público.**
- **`OAEs.xlsx`** — base nacional de OAEs (5.852 linhas); os campos em si
  (nome, coordenadas, extensão) não são sigilosos, mas é uma base
  proprietária de terceiros (PROARTE/DNIT), não gerada por este projeto —
  só ~5% das linhas (as 310 do Ceará) são de fato usadas aqui.
- **`*.gpkg`** (`nordeste_enriquecido.gpkg`, 94 MB; `SNV_ESTADUAL_planarizado.gpkg`,
  534 MB) — não são sigilosos (derivados de dados públicos: SNV/DNIT,
  OpenStreetMap/Geofabrik), mas excedem o limite de 100 MB por arquivo do
  GitHub (o segundo sozinho já ultrapassa).
- **`análise_rotas/`** (~4,3 GB) — saída regenerável do motor de rotas.

Sem `OAEs.xlsx` e `controle_geral.xlsx` presentes localmente (mas fora do
repo), os scripts e o notebook não rodam — quem clonar o repositório precisa
obter esses dois arquivos por fora (ex. diretamente do PROARTE/DNIT) e
colocá-los nesta pasta antes de executar o pipeline.
