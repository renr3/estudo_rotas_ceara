"""
batch_rotas_route_study.py — Estudo de rotas alternativas para OAEs do Ceará.

Universo de OAEs identificado por CodPro (chave do banco PROARTE/DNIT), lido
de codpro.csv. O SGE, quando existe, é só um dado informativo carregado da
planilha de OAEs — várias OAEs do PROARTE não têm SGE atribuído.

Para cada OAE listada na planilha (todas as OAEs do estado):
  Análise A1 — Todas as superfícies (sem filtro)
  Análise A2 — Somente superfícies PAV, DUP, EOD e NULL

  Parâmetros fixos:
    Raio de busca : 50 km em torno da OAE
    Zona exclusão : extensão da OAE (coluna Extensao), fallback 50 m

  Para cada análise, salva em  análise_rotas/<CodPro>/ :
    01_universo_A1.kml   — rede usada na Análise 1
    02_rota_A1.kml       — rota escolhida na Análise 1
    01_universo_A2.kml   — rede usada na Análise 2
    02_rota_A2.kml       — rota escolhida na Análise 2
    resultado_A1.txt     — dados estruturados A1 (legível por máquina)
    resultado_A2.txt     — dados estruturados A2 (legível por máquina)
    relatorio.txt        — sumário legível por humano

Dependências:
    pip install geopandas networkx pandas simplekml shapely openpyxl osmnx
"""

import os
import traceback
from datetime import datetime

import geopandas as gpd
import networkx as nx
import pandas as pd
import simplekml
from shapely.geometry import Point, LineString, MultiLineString
from shapely.ops import split, snap

# ============================================================
# CONFIGURAÇÕES — ajuste conforme necessário
# ============================================================

CSV_CODPRO    = "codpro.csv"         # CSV com coluna "CODPRO" (chave PROARTE, ex. "OAE657")
PLANILHA_OAES = "OAEs.xlsx"
PASTA_SAIDA   = "análise_rotas"

# GeoPackage OSM enriquecido com dados SNV (gerado por enriquecer_osm_com_snv.py)
GPKG_OSM      = "nordeste_enriquecido.gpkg"

# Superfícies aceitas na Análise 2 (segmentos NULL/vazio também são incluídos)
# EOD = Em Obras de Duplicação: a via existe e é pavimentada, apenas está sendo ampliada
SUPERFICIES_A2 = ["PAV", "DUP", "EOD"]

# CodPro cuja própria OAE está sobre via não pavimentada: restringir a Análise
# A2 a SUPERFICIES_A2 não faz sentido nesses casos, pois nem a via de origem
# (onde a OAE está) entraria na rede filtrada — resultando em "sem_interseccao"
# mesmo a OAE estando sobre uma via real. Para esses CodPro, A2 usa a mesma
# rede sem filtro da A1.
A2_SEM_FILTRO_CODPRO = {
    "OAE519", "OAE537", "OAE633", "OAE634", "OAE648", "OAE661",
}

# Raio de busca em torno da OAE (km)
RAIO_OSM_KM   = 50

# Quais análises executar: ["A1", "A2"] para ambas, ["A2"] para só A2, etc.
ANALISES      = ["A1", "A2"]

# Tipos de via OSM considerados
TIPOS_OSM     = [
    "motorway", "trunk", "motorway_link", "trunk_link",
    "primary", "secondary", "primary_link", "secondary_link",
    "tertiary", "tertiary_link",
]

OSM_TIMEOUT_S = 60   # usado somente no fallback Overpass

# CRS métrico nacional — cobre todo o Brasil sem precisar detectar zona UTM
CRS_METRICO = 5880   # SIRGAS 2000 / Brazil Polyconic

TOLERANCIA_NO_M = 50  # metros — raio de fusão de nós no grafo (Union-Find)

# Teto absoluto do raio de exclusão nas tentativas de detectar A/B (sem_interseccao).
# Além dos multiplicadores fixos (até 10x o raio-base), continua dobrando o
# raio até esse teto — evita halt em coordenadas de OAE muito imprecisas.
RAIO_EXCLUSAO_MAX_M = 500.0

# Modo SNV desativado para este estudo
USAR_SNV    = False
USAR_OSM    = True
RAIO_SNV_KM = 300   # não utilizado

# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================


def recortar_area(gdf, centro_m, raio_km):
    area = centro_m.buffer(raio_km * 1000)
    return gdf[gdf.geometry.intersects(area)].copy()


def dividir_segmentos_com_exclusao(gdf, exclusao_geom):
    """Remove trechos dentro da zona de exclusão; divide os parcialmente interceptados."""
    segmentos = []
    n_div = n_exc = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if not geom.intersects(exclusao_geom):
            segmentos.append(row)
            continue
        try:
            parte_ext = geom.difference(exclusao_geom)
            if parte_ext.is_empty:
                n_exc += 1
                continue
            partes = (
                [parte_ext] if isinstance(parte_ext, LineString)
                else list(parte_ext.geoms) if isinstance(parte_ext, MultiLineString)
                else None
            )
            if partes is None:
                segmentos.append(row)
                continue
            for p in partes:
                if p.length > 1.0:
                    nova = row.copy()
                    nova.geometry = p
                    segmentos.append(nova)
            n_div += 1
        except Exception:
            segmentos.append(row)
    return gpd.GeoDataFrame(segmentos, crs=gdf.crs), n_div, n_exc


def inserir_ponto_na_rede(gdf, ponto_m, tolerancia=500):
    gdf = gdf.reset_index(drop=True)
    buf = ponto_m.buffer(tolerancia)
    candidatos = gdf[gdf.geometry.intersects(buf)].copy()
    if candidatos.empty:
        return gdf, ponto_m
    dists = candidatos.geometry.distance(ponto_m)
    idx_min = int(dists.idxmin())
    seg = candidatos.loc[idx_min]
    seg_geom = seg.geometry
    proj = seg_geom.interpolate(seg_geom.project(ponto_m))
    seg_snap = snap(seg_geom, proj, tolerance=0.01)
    partes = split(seg_snap, proj)
    if len(partes.geoms) < 2:
        return gdf, proj
    gdf = gdf.drop(index=idx_min).reset_index(drop=True)

    def _sv(v):
        if isinstance(v, pd.Series):
            v = v.iloc[0] if not v.empty else ""
        return str(v) if v is not None else ""

    novas_rows = []
    for parte in partes.geoms:
        novas_rows.append({
            "_id_rodovia": _sv(seg["_id_rodovia"]),
            "_uf":         _sv(seg.get("_uf")),
            "_fonte":      _sv(seg.get("_fonte")),
            "Superficie":  _sv(seg.get("Superficie")),
            "nm_tipo_tr":  seg.get("nm_tipo_tr"),
            "ds_local_i":  seg.get("ds_local_i"),
            "ds_local_f":  seg.get("ds_local_f"),
            "bridge":      seg.get("bridge"),
            "tunnel":      seg.get("tunnel"),
            "geometry":    parte,
        })
    gdf_novas = gpd.GeoDataFrame(pd.DataFrame(novas_rows), geometry="geometry", crs=gdf.crs)
    gdf = gpd.GeoDataFrame(
        pd.concat([gdf, gdf_novas], ignore_index=True),
        geometry="geometry", crs=gdf.crs,
    )
    return gdf, proj


def construir_grafo(gdf):
    """
    Constrói o grafo de roteamento.

    Usa Union-Find + STRtree (dwithin) para fundir endpoints dentro de
    TOLERANCIA_NO_M metros — robusto contra imprecisões topológicas do GPKG
    sem depender de grade regular, que falha quando o gap cruza um limite de
    célula.
    """
    import numpy as np
    import shapely as shp
    from collections import defaultdict

    gdf_clean = gdf.loc[:, ~gdf.columns.duplicated(keep="first")]
    records   = gdf_clean.to_dict("records")

    def _s(v, d="N/A"):
        return str(v) if (v is not None and str(v) not in ("nan", "None", "")) else d

    valid = []
    for i, rec in enumerate(records):
        geom = rec.get("geometry")
        if geom is None or geom.is_empty or geom.length < 1.0:
            continue
        valid.append((i, rec, geom))

    if not valid:
        return nx.Graph(), {}, {}

    # Planarização: divide vias longas em todos os cruzamentos geométricos.
    # Pontes e túneis são excluídos para evitar falsas interseções.
    if len(valid) >= 2:
        def _elevado(rec):
            return (str(rec.get("bridge", "F")).upper() in ("T", "TRUE", "1", "YES") or
                    str(rec.get("tunnel", "F")).upper() in ("T", "TRUE", "1", "YES"))

        _normal   = [(i, r, g) for i, r, g in valid if not _elevado(r)]
        _elevated = [(i, r, g) for i, r, g in valid if _elevado(r)]

        if len(_normal) >= 2:
            _geoms_raw = [g for _, _, g in _normal]
            _planar    = shp.unary_union(_geoms_raw)
            if _planar is not None and not _planar.is_empty:
                if _planar.geom_type == "LineString":
                    _plist = [_planar]
                elif _planar.geom_type == "MultiLineString":
                    _plist = list(_planar.geoms)
                else:
                    _plist = [g for g in _planar.geoms
                              if g.geom_type == "LineString" and g.length > 0.1]
                _ptree     = shp.STRtree(_geoms_raw)
                _new_valid = []
                for _g in _plist:
                    if _g.is_empty or _g.length < 0.1:
                        continue
                    _mid = _g.interpolate(0.5, normalized=True)
                    _k   = int(_ptree.nearest(_mid))
                    _, _rec, _ = _normal[_k]
                    _new_valid.append((len(_new_valid), _rec, _g))
                _base = len(_new_valid)
                for _j, (_i, _r, _g) in enumerate(_elevated):
                    _new_valid.append((_base + _j, _r, _g))
                if _new_valid:
                    valid = _new_valid

    ep_xy = np.empty((len(valid) * 2, 2), dtype=np.float64)
    for k, (_, _, geom) in enumerate(valid):
        ep_xy[k * 2]     = geom.coords[0]
        ep_xy[k * 2 + 1] = geom.coords[-1]

    n_ep   = len(ep_xy)
    parent = list(range(n_ep))

    def _find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _union(x, y):
        rx, ry = _find(x), _find(y)
        if rx != ry:
            parent[rx] = ry

    ep_pts       = shp.points(ep_xy)
    tree         = shp.STRtree(ep_pts)
    lhs, rhs     = tree.query(ep_pts, predicate="dwithin", distance=TOLERANCIA_NO_M)
    for a, b in zip(lhs, rhs):
        if a != b:
            _union(int(a), int(b))

    cluster_pts = defaultdict(list)
    for i in range(n_ep):
        cluster_pts[_find(i)].append(ep_xy[i])

    rep_coord = {}
    for root, pts in cluster_pts.items():
        arr = np.array(pts)
        rep_coord[root] = (round(float(arr[:, 0].mean()), 1),
                           round(float(arr[:, 1].mean()), 1))

    G           = nx.Graph()
    edge_to_seg = {}
    seg_info    = {}

    for k, (orig_i, rec, geom) in enumerate(valid):
        start = rep_coord[_find(k * 2)]
        end   = rep_coord[_find(k * 2 + 1)]
        G.add_edge(start, end, weight=geom.length, segment_id=orig_i)
        edge_to_seg[(start, end)] = orig_i
        edge_to_seg[(end, start)] = orig_i
        seg_info[orig_i] = {
            "id_rodovia": _s(rec.get("_id_rodovia")),
            "uf":         _s(rec.get("_uf")),
            "fonte":      _s(rec.get("_fonte")),
            "superficie": _s(rec.get("Superficie")),
            "nm_tipo_tr": rec.get("nm_tipo_tr"),
            "ds_local_i": rec.get("ds_local_i"),
            "ds_local_f": rec.get("ds_local_f"),
            "geometry":   geom,
        }

    return G, edge_to_seg, seg_info


def find_nearest_node(point, graph):
    min_dist, nearest = float("inf"), None
    for node in graph.nodes():
        d = point.distance(Point(node))
        if d < min_dist:
            min_dist, nearest = d, node
    return nearest, min_dist


def geom_para_kml_coords(geom, crs_orig):
    gs = gpd.GeoSeries([geom], crs=crs_orig).to_crs(4326)
    return [(lon, lat) for lon, lat in gs.iloc[0].coords]


def encontrar_pontos_AB(gdf_rede, exclusao_geom):
    """
    Retorna os dois pontos (A_m, B_m) onde a malha viária cruza a borda
    da zona de exclusão — extremidades do trecho bloqueado.

    Se houver mais de dois pontos de interseção (múltiplas vias), escolhe
    o par com maior distância entre si.
    Retorna (None, None) se nenhuma via cruzar a zona.
    """
    borda  = exclusao_geom.boundary
    pontos = []

    for _, row in gdf_rede.iterrows():
        geom = row.geometry
        if not geom.intersects(exclusao_geom):
            continue
        ints = geom.intersection(borda)
        if ints.is_empty:
            continue
        if ints.geom_type == "Point":
            pontos.append(ints)
        elif ints.geom_type == "MultiPoint":
            pontos.extend(list(ints.geoms))
        elif ints.geom_type == "GeometryCollection":
            for g in ints.geoms:
                if g.geom_type == "Point":
                    pontos.append(g)

    if len(pontos) < 2:
        return None, None

    max_d, A, B = 0.0, pontos[0], pontos[1]
    for i in range(len(pontos)):
        for j in range(i + 1, len(pontos)):
            d = pontos[i].distance(pontos[j])
            if d > max_d:
                max_d, A, B = d, pontos[i], pontos[j]
    return A, B


# ============================================================
# CARREGAMENTO DE REDE OSM — fallback via API Overpass
# ============================================================

def carregar_osm_overpass(centro_latlon, raio_km, tipos_osm, crs_metrico):
    """Baixa a rede via API Overpass (fallback quando GPKG_OSM não existe)."""
    import time
    try:
        import osmnx as ox
    except ImportError:
        raise ImportError("Pacote 'osmnx' não encontrado. Instale com: pip install osmnx")

    ox.settings.timeout = OSM_TIMEOUT_S
    print(f"        Centro : {centro_latlon}  |  raio: {raio_km} km  |  timeout: {OSM_TIMEOUT_S}s")
    print(f"        Consultando API Overpass...", flush=True)
    t0 = time.time()

    G_osm = ox.graph_from_point(
        centro_latlon, dist=raio_km * 1000, network_type="drive", simplify=True,
    )
    _, edges = ox.graph_to_gdfs(G_osm)

    def _to_list(v):
        return v if isinstance(v, list) else ([v] if isinstance(v, str) else [])

    if tipos_osm:
        mask  = edges["highway"].apply(lambda h: bool(set(_to_list(h)) & set(tipos_osm)))
        edges = edges[mask].copy()

    edges = edges.to_crs(crs_metrico).explode(index_parts=False).reset_index(drop=True)

    def _get_id(row):
        for col in ("ref", "name"):
            v = row.get(col)
            if v and str(v) not in ("nan", "None", ""):
                return str(v)
        hw = row.get("highway", "via")
        return f"OSM-{hw[0] if isinstance(hw, list) else str(hw)}"

    edges["_id_rodovia"] = edges.apply(_get_id, axis=1)
    edges["_uf"]         = ""
    edges["_fonte"]      = "OSM"
    edges["Superficie"]  = "PAV"
    edges["nm_tipo_tr"]  = edges["highway"].apply(
        lambda h: h[0] if isinstance(h, list) else str(h)
    )
    edges["ds_local_i"]  = edges["name"] if "name" in edges.columns else None
    edges["ds_local_f"]  = None

    cols = ["_id_rodovia", "_uf", "_fonte", "Superficie",
            "nm_tipo_tr", "ds_local_i", "ds_local_f", "geometry"]
    gdf = gpd.GeoDataFrame(
        edges[[c for c in cols if c in edges.columns]],
        geometry="geometry", crs=crs_metrico,
    )
    print(f"        {len(gdf)} segmentos  ({time.time() - t0:.1f}s)")
    return gdf


# ============================================================
# GERAÇÃO DE KMLs
# ============================================================

def _gerar_kml_universo(gdf_routable, A_latlon, B_latlon, exc_ring, crs_metrico, caminho, G=None, seg_info=None, node_A_graph=None):
    kml = simplekml.Kml()
    pastas = {
        "SNV":      (kml.newfolder(name="Rodovias Federais (SNV)"),  simplekml.Color.red),
        "ESTADUAL": (kml.newfolder(name="Rodovias Estaduais"),        simplekml.Color.yellow),
        "OSM":      (kml.newfolder(name="OpenStreetMap"),             simplekml.Color.orange),
    }
    _COR_TIPO = {
        "motorway":       simplekml.Color.rgb(220,  20,  60),
        "motorway_link":  simplekml.Color.rgb(220,  20,  60),
        "trunk":          simplekml.Color.rgb(255, 140,   0),
        "trunk_link":     simplekml.Color.rgb(255, 140,   0),
        "primary":        simplekml.Color.rgb(255, 215,   0),
        "primary_link":   simplekml.Color.rgb(255, 215,   0),
        "secondary":      simplekml.Color.rgb( 50, 205,  50),
        "secondary_link": simplekml.Color.rgb( 50, 205,  50),
        "tertiary":       simplekml.Color.rgb(135, 206, 235),
        "tertiary_link":  simplekml.Color.rgb(135, 206, 235),
    }

    for _, row in gdf_routable.iterrows():
        try:
            coords = geom_para_kml_coords(row.geometry, crs_metrico)
        except Exception:
            continue
        fonte = row.get("_fonte", "")
        if fonte not in pastas:
            continue
        fld, _ = pastas[fonte]
        tipo = str(row.get("nm_tipo_tr") or "")
        cor  = _COR_TIPO.get(tipo, simplekml.Color.white)
        ln = fld.newlinestring(name=row["_id_rodovia"])
        ln.style.linestyle.color = cor
        ln.style.linestyle.width = 2
        ln.coords = coords
        ln.description = (
            f"Tipo: {tipo or '—'}\n"
            f"Superfície: {row.get('Superficie') or '—'}\n"
            f"De: {row.get('ds_local_i') or '—'}\n"
            f"Até: {row.get('ds_local_f') or '—'}"
        )

    poly = kml.newpolygon(name="Zona de Exclusão")
    poly.outerboundaryis = exc_ring
    poly.style.polystyle.color = simplekml.Color.changealphaint(80, simplekml.Color.red)
    poly.style.linestyle.color = simplekml.Color.red
    poly.style.linestyle.width = 2

    for nome, pt, cor in [
        ("A – Origem",  A_latlon, simplekml.Color.blue),
        ("B – Destino", B_latlon, simplekml.Color.orange),
    ]:
        p = kml.newpoint(name=nome, coords=[(pt[1], pt[0])])
        p.style.iconstyle.color = cor
        p.style.iconstyle.scale = 1.5

    if G is not None and seg_info is not None:
        import math
        from shapely.geometry import Point as _Pt, LineString as _LS

        TICK_M = 80

        nos_jun = [(x, y) for (x, y), d in G.degree() if d >= 3]
        fld_nos = kml.newfolder(name="Conectividade da rede")
        fld_fim = fld_nos.newfolder(name="Extremidades sem conexão (barra vermelha)")
        fld_jun = fld_nos.newfolder(name=f"Entroncamentos — grau ≥ 3  ({len(nos_jun)})")

        n_barras = 0
        for node, degree in G.degree():
            if degree != 1:
                continue
            neighbors = list(G.neighbors(node))
            if not neighbors:
                continue
            seg_id = G[node][neighbors[0]].get("segment_id")
            if seg_id is None or seg_id not in seg_info:
                continue
            info = seg_info[seg_id]
            geom = info.get("geometry")
            if geom is None or geom.is_empty:
                continue
            coords_list = list(geom.coords)
            if len(coords_list) < 2:
                continue

            node_pt = _Pt(node)
            if _Pt(coords_list[0]).distance(node_pt) < _Pt(coords_list[-1]).distance(node_pt):
                dead_xy, dir_xy = coords_list[0], coords_list[1]
            else:
                dead_xy, dir_xy = coords_list[-1], coords_list[-2]

            dx = dead_xy[0] - dir_xy[0]
            dy = dead_xy[1] - dir_xy[1]
            norm = math.sqrt(dx * dx + dy * dy)
            if norm == 0:
                continue
            dx /= norm
            dy /= norm

            perp_x, perp_y = -dy, dx
            arm1 = (dead_xy[0] + perp_x * TICK_M, dead_xy[1] + perp_y * TICK_M)
            arm2 = (dead_xy[0] - perp_x * TICK_M, dead_xy[1] - perp_y * TICK_M)
            tick_wgs = gpd.GeoSeries(
                [_LS([arm1, dead_xy, arm2])], crs=crs_metrico
            ).to_crs(4326).iloc[0]

            ln = fld_fim.newlinestring(
                name=info.get("id_rodovia", "—"),
                coords=[(c[0], c[1]) for c in tick_wgs.coords],
            )
            ln.style.linestyle.color = simplekml.Color.red
            ln.style.linestyle.width = 8
            ln.description = (
                f"Extremidade sem conexão\n"
                f"Via: {info.get('id_rodovia', '—')}\n"
                f"Tipo: {info.get('nm_tipo_tr', '—')}"
            )
            n_barras += 1

        fld_fim.name = f"Extremidades sem conexão — {n_barras} barras"

        if nos_jun:
            pts_wgs = gpd.GeoSeries(
                [_Pt(x, y) for x, y in nos_jun], crs=crs_metrico
            ).to_crs(4326)
            for pt in pts_wgs:
                p = fld_jun.newpoint(coords=[(pt.x, pt.y)])
                p.style.iconstyle.color = simplekml.Color.green
                p.style.iconstyle.scale = 1.4
                p.style.iconstyle.icon.href = (
                    "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"
                )

    if G is not None and seg_info is not None and node_A_graph is not None:
        try:
            reachable = nx.node_connected_component(G, node_A_graph)
        except Exception:
            reachable = set()

        fld_reach = kml.newfolder(name="Alcançabilidade a partir de A")
        fld_ok  = fld_reach.newfolder(name="Alcançáveis de A")
        fld_iso = fld_reach.newfolder(name="Não alcançáveis de A")

        n_ok = n_iso = 0
        for n1, n2, data in G.edges(data=True):
            seg_id = data.get("segment_id")
            if seg_id is None or seg_id not in seg_info:
                continue
            info = seg_info[seg_id]
            geom = info.get("geometry")
            if geom is None or geom.is_empty:
                continue
            try:
                coords_wgs = geom_para_kml_coords(geom, crs_metrico)
            except Exception:
                continue

            is_reach = (n1 in reachable) and (n2 in reachable)
            fld_dest = fld_ok if is_reach else fld_iso
            cor = simplekml.Color.rgb(0, 220, 255) if is_reach else simplekml.Color.rgb(160, 160, 160)

            ln = fld_dest.newlinestring(name=info.get("id_rodovia", "—"), coords=coords_wgs)
            ln.style.linestyle.color = cor
            ln.style.linestyle.width = 5
            ln.description = (
                f"{'✓ Alcançável de A' if is_reach else '✗ Isolado — não alcançável de A'}\n"
                f"Via: {info.get('id_rodovia', '—')}\n"
                f"Tipo: {info.get('nm_tipo_tr', '—')}"
            )
            if is_reach:
                n_ok += 1
            else:
                n_iso += 1

        fld_ok.name  = f"Alcançáveis de A ({n_ok})"
        fld_iso.name = f"Não alcançáveis de A — componentes isolados ({n_iso})"

    kml.save(caminho)


def _gerar_kml_rota(path, path_length, edge_to_seg, seg_info,
                    A_latlon, B_latlon, exc_ring, crs_metrico, caminho):
    kml = simplekml.Kml()
    fld = kml.newfolder(name=f"Rota Calculada ({path_length / 1000:.2f} km)")
    _COR = {
        "SNV":      simplekml.Color.blue,
        "ESTADUAL": simplekml.Color.cyan,
        "OSM":      simplekml.Color.magenta,
    }
    for i in range(len(path) - 1):
        seg_idx = edge_to_seg.get((path[i], path[i + 1]))
        if seg_idx is None:
            continue
        info = seg_info[seg_idx]
        try:
            coords = geom_para_kml_coords(info["geometry"], crs_metrico)
        except Exception:
            continue
        cor = _COR.get(info["fonte"], simplekml.Color.white)
        ln  = fld.newlinestring(name=f"{info['id_rodovia']} (trecho {i + 1})")
        ln.coords = coords
        ln.style.linestyle.color = cor
        ln.style.linestyle.width = 5
        ln.description = (
            f"Via: {info['id_rodovia']}\n"
            f"Fonte: {info['fonte']}\n"
            f"Superfície: {info['superficie']}\n"
            f"Tipo: {info.get('nm_tipo_tr') or '—'}\n"
            f"De: {info['ds_local_i']}\nAté: {info['ds_local_f']}"
        )

    poly = kml.newpolygon(name="Zona de Exclusão")
    poly.outerboundaryis = exc_ring
    poly.style.polystyle.color = simplekml.Color.changealphaint(80, simplekml.Color.red)
    poly.style.linestyle.color = simplekml.Color.red
    poly.style.linestyle.width = 2

    for nome, pt, cor in [
        ("A – Origem",  A_latlon, simplekml.Color.blue),
        ("B – Destino", B_latlon, simplekml.Color.orange),
    ]:
        p = kml.newpoint(name=nome, coords=[(pt[1], pt[0])])
        p.style.iconstyle.color = cor
        p.style.iconstyle.scale = 1.5

    kml.save(caminho)


# ============================================================
# ARQUIVO DE RESULTADO ESTRUTURADO (legível por máquina)
# ============================================================

def _salvar_resultado_txt(codpro_code, sge_val, label, descricao, resultado, caminho):
    """
    Salva um arquivo texto com formato key=value para compilação automática.

    Campos:
      CODPRO, SGE, ANALISE, DESCRICAO, STATUS, KM_TOTAL, N_VIAS, IDS_VIAS, SEQUENCIA
    IDS_VIAS e SEQUENCIA usam ';' como separador interno.
    SGE fica vazio quando a OAE não tem código SGE atribuído.
    """
    lines = [
        f"CODPRO={codpro_code}",
        f"SGE={'' if pd.isna(sge_val) else int(sge_val)}",
        f"ANALISE={label}",
        f"DESCRICAO={descricao}",
        f"STATUS={resultado['status']}",
        f"KM_TOTAL={resultado['km_total']:.4f}",
        f"N_VIAS={resultado['n_vias']}",
        f"IDS_VIAS={';'.join(resultado['ids_vias'])}",
        f"SEQUENCIA={resultado['sequencia']}",
    ]
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ============================================================
# NÚCLEO: CALCULAR UMA ANÁLISE
# ============================================================

_RES_VAZIO = {
    "status":    "nao_encontrada",
    "km_total":  0.0,
    "n_vias":    0,
    "ids_vias":  [],
    "sequencia": "",
}


def calcular_modo(codpro_code, lat, lon, C_m, exclusao_geom,
                  gdf_snv_global, gdf_osm_global, modo, pasta,
                  raio_excl_m=None, label=None):
    """
    Calcula a rota alternativa para um modo (SNV ou OSM).

    label   : rótulo usado nos nomes de arquivo (ex.: "A1", "A2").
              Se None, usa o valor de modo.

    Retorna (encontrada: bool, relatorio_texto: str, resultado: dict).
    resultado tem chaves: status, km_total, n_vias, ids_vias, sequencia.
    """
    _label        = label if label is not None else modo
    centro_latlon = (lat, lon)

    # --- 1. Preparar rede ---
    if modo == "SNV":
        raio_km = RAIO_SNV_KM
        gdf = recortar_area(gdf_snv_global, C_m, raio_km)
        print(f"      [{_label}] Rede SNV recortada: {len(gdf)} segmentos")
    else:
        raio_km = RAIO_OSM_KM
        if gdf_osm_global is not None:
            gdf = recortar_area(gdf_osm_global, C_m, raio_km)
            print(f"      [{_label}] Rede OSM recortada: {len(gdf)} segmentos  (GPKG local)")
        else:
            print(f"      [{_label}] OSM via API Overpass  |  raio {raio_km} km", flush=True)
            try:
                gdf = carregar_osm_overpass(centro_latlon, raio_km, TIPOS_OSM, CRS_METRICO)
            except Exception as e_osm:
                msg = (
                    f"=== ANÁLISE: {_label} (raio {raio_km} km) ===\n"
                    f"Falha na API Overpass: {e_osm}\n"
                    f"Sem rota. Execute enriquecer_osm_com_snv.py para usar arquivo local.\n"
                )
                print(f"      ERRO OSM: {e_osm}")
                return False, msg, {**_RES_VAZIO, "status": "erro_osm"}

    if gdf.empty:
        msg = f"=== ANÁLISE: {_label} (raio {raio_km} km) ===\nRede vazia. Sem rota.\n"
        return False, msg, {**_RES_VAZIO, "status": "rede_vazia"}

    for col in ["nm_tipo_tr", "ds_local_i", "ds_local_f"]:
        if col not in gdf.columns:
            gdf[col] = None

    # --- 2. Detectar A e B (retry automático se o raio não intersectar nenhuma via) ---
    _raio_base = raio_excl_m if raio_excl_m is not None else 50.0
    A_m = B_m = None
    exclusao_usada = exclusao_geom
    raio_final     = _raio_base

    # Tentativas fixas (até 10x o raio-base); se nenhuma intersectar, continua
    # dobrando o raio em valor absoluto até RAIO_EXCLUSAO_MAX_M antes de desistir.
    _tentativas_m = [_raio_base * m for m in (1.0, 1.5, 2.0, 3.0, 5.0, 10.0)]
    _ultimo = _tentativas_m[-1]
    while _ultimo < RAIO_EXCLUSAO_MAX_M:
        _ultimo = min(_ultimo * 2.0, RAIO_EXCLUSAO_MAX_M)
        _tentativas_m.append(_ultimo)

    for _raio_tent in _tentativas_m:
        _excl_tent = C_m.buffer(_raio_tent)
        A_m, B_m   = encontrar_pontos_AB(gdf, _excl_tent)
        # Exige dist(A,B) > 2×TOLERANCIA_NO_M para impedir fusão transitiva:
        # pela desigualdade triangular, nenhum ponto E pode fundir A e B via
        # Union-Find se |AB| > 2×TOLERANCIA_NO_M (qualquer E ficaria a >50 m de A ou B)
        if A_m is not None and B_m is not None and A_m.distance(B_m) > 2 * TOLERANCIA_NO_M:
            exclusao_usada = _excl_tent
            raio_final     = _raio_tent
            if _raio_tent > _raio_base:
                print(f"      [{_label}] Raio ampliado: {_raio_base:.0f} m → {_raio_tent:.0f} m")
            break

    if A_m is None or B_m is None or A_m.distance(B_m) <= 2 * TOLERANCIA_NO_M:
        msg = (
            f"=== ANÁLISE: {_label} (raio {raio_km} km) ===\n"
            f"Zona de exclusão ({_raio_base:.0f} m, max tentativa {_tentativas_m[-1]:.0f} m): "
            f"nenhuma via interseccionada.\n"
            f"Pontos A e B não detectados. Sem rota.\n"
            f"Sugestão: verifique as coordenadas da OAE e a cobertura do GPKG.\n"
        )
        return False, msg, {**_RES_VAZIO, "status": "sem_interseccao"}

    A_wgs    = gpd.GeoSeries([A_m], crs=CRS_METRICO).to_crs(4326).iloc[0]
    B_wgs    = gpd.GeoSeries([B_m], crs=CRS_METRICO).to_crs(4326).iloc[0]
    A_latlon = (A_wgs.y, A_wgs.x)
    B_latlon = (B_wgs.y, B_wgs.x)
    print(f"      [{_label}] A=({A_latlon[0]:.6f}, {A_latlon[1]:.6f})  "
          f"B=({B_latlon[0]:.6f}, {B_latlon[1]:.6f})  "
          f"dist={A_m.distance(B_m):.1f} m")

    # --- 3. Aplicar zona de exclusão ---
    gdf["_id_rodovia"] = gdf["_id_rodovia"].apply(
        lambda x: x if isinstance(x, str)
        else str(next(iter(x.values()))) if isinstance(x, dict)
        else str(x)
    )
    gdf["_fonte"]     = gdf["_fonte"].astype(str)
    gdf["Superficie"] = gdf["Superficie"].astype(str)

    gdf_routable, n_div, n_exc = dividir_segmentos_com_exclusao(gdf, exclusao_usada)
    print(f"      [{_label}] Exclusão: {n_div} divididos, {n_exc} excluídos, {len(gdf_routable)} restantes")

    # --- 4. Inserir A e B na rede roteável ---
    gdf_routable, node_A_geom = inserir_ponto_na_rede(gdf_routable, A_m)
    gdf_routable, node_B_geom = inserir_ponto_na_rede(gdf_routable, B_m)
    for col in ("_id_rodovia", "_fonte", "Superficie"):
        gdf_routable[col] = gdf_routable[col].astype(str)

    # --- 5. Grafo e rota ---
    G, edge_to_seg, seg_info = construir_grafo(gdf_routable)
    node_A, _ = find_nearest_node(node_A_geom, G)
    node_B, _ = find_nearest_node(node_B_geom, G)

    if node_A == node_B:
        print(f"      [{_label}] node_A == node_B — exclusão ainda insuficiente no grafo.")
        return False, (
            f"=== ANÁLISE: {_label} (raio {raio_km} km) ===\n"
            f"Zona de exclusão ({raio_final:.0f} m): A e B colapsaram no mesmo nó do grafo.\n"
            f"Sem rota alternativa válida.\n"
        ), {**_RES_VAZIO, "status": "no_colapsado"}

    path = path_length = None
    try:
        path        = nx.shortest_path(G, node_A, node_B, weight="weight")
        path_length = nx.shortest_path_length(G, node_A, node_B, weight="weight")
        if path_length < 1.0:
            print(f"      [{_label}] Rota de {path_length:.1f} m descartada (A=B efetivo).")
            path = path_length = None
        else:
            print(f"      [{_label}] Rota: {path_length / 1000:.2f} km, {len(path) - 1} trechos")
    except nx.NetworkXNoPath:
        print(f"      [{_label}] Sem rota (grafo desconexo).")

    # --- 6. KMLs ---
    exc_wgs  = gpd.GeoSeries([exclusao_usada], crs=CRS_METRICO).to_crs(4326).iloc[0]
    exc_ring = [(lx, ly) for lx, ly in exc_wgs.exterior.coords]

    _gerar_kml_universo(
        gdf_routable, A_latlon, B_latlon, exc_ring, CRS_METRICO,
        os.path.join(pasta, f"01_universo_{_label}.kml"),
        G=G, seg_info=seg_info, node_A_graph=node_A,
    )
    if path is not None:
        _gerar_kml_rota(
            path, path_length, edge_to_seg, seg_info,
            A_latlon, B_latlon, exc_ring, CRS_METRICO,
            os.path.join(pasta, f"02_rota_{_label}.kml"),
        )

    # --- 7. Texto do relatório ---
    lines = [
        f"=== ANÁLISE: {_label} (raio {raio_km} km) ===",
        f"Zona de exclusão : {raio_final:.0f} m em torno de C=({lat:.6f}, {lon:.6f})",
        f"Ponto A detectado: ({A_latlon[0]:.6f}, {A_latlon[1]:.6f})",
        f"Ponto B detectado: ({B_latlon[0]:.6f}, {B_latlon[1]:.6f})",
        f"Dist. A-B (reta)  : {A_m.distance(B_m):.1f} m",
        "",
    ]

    resultado = dict(_RES_VAZIO)

    if path is None:
        resultado["status"] = "grafo_desconexo"
        lines += [
            "RESULTADO: Nenhuma rota alternativa encontrada.",
            "Sugestões: verifique a cobertura do GPKG ou aumente o raio de busca.",
        ]
    else:
        route_segs = []
        for i in range(len(path) - 1):
            seg_idx = edge_to_seg.get((path[i], path[i + 1]))
            if seg_idx is not None:
                route_segs.append(seg_info[seg_idx])

        grupos, cur_id, cur_dist, cur_segs = [], None, 0.0, []
        for seg in route_segs:
            rid = seg["id_rodovia"]
            d   = seg["geometry"].length / 1000
            if rid == cur_id:
                cur_dist += d
                cur_segs.append(seg)
            else:
                if cur_id:
                    grupos.append({"id": cur_id, "dist": cur_dist, "segs": cur_segs})
                cur_id, cur_dist, cur_segs = rid, d, [seg]
        if cur_id:
            grupos.append({"id": cur_id, "dist": cur_dist, "segs": cur_segs})

        ids_unicos = list(dict.fromkeys(g["id"] for g in grupos))

        resultado = {
            "status":    "encontrada",
            "km_total":  path_length / 1000,
            "n_vias":    len(grupos),
            "ids_vias":  ids_unicos,
            "sequencia": ";".join(g["id"] for g in grupos),
        }

        lines += [
            f"RESULTADO          : Rota encontrada",
            f"Distância total    : {path_length / 1000:.2f} km",
            f"Número de vias     : {len(grupos)}",
            f"Sequência          : {' → '.join(g['id'] for g in grupos)}",
            f"Rodovias únicas    : {', '.join(ids_unicos)}",
            "",
            "Detalhamento por via:",
        ]
        for i, g in enumerate(grupos, 1):
            s0   = g["segs"][0]
            tipo = s0.get("nm_tipo_tr") or "—"
            ini  = s0.get("ds_local_i") or "—"
            fim  = g["segs"][-1].get("ds_local_f") or "—"
            lines.append(
                f"  {i:2d}. {g['id']}  [{s0['fonte']}]  "
                f"{g['dist']:.2f} km  sup:{s0['superficie']}  tipo:{tipo}"
            )
            if ini not in ("N/A", None, "—", "nan"):
                lines.append(f"       De: {ini}  →  Até: {fim}")

    return path is not None, "\n".join(lines), resultado


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def main():
    print("=" * 70)
    print("ESTUDO DE ROTAS ALTERNATIVAS — CEARÁ")
    if "A1" in ANALISES:
        print("  Análise A1 : Todas as superfícies")
    if "A2" in ANALISES:
        print("  Análise A2 : PAV + DUP + EOD + NULL")
    print("=" * 70)

    # --- 1. Carregar lista de CodPro ---
    if not os.path.exists(CSV_CODPRO):
        raise FileNotFoundError(
            f"Arquivo '{CSV_CODPRO}' não encontrado.\n"
            "Crie um CSV com uma coluna 'CODPRO' contendo as chaves PROARTE (ex. 'OAE657')."
        )
    df_codpro   = pd.read_csv(CSV_CODPRO)
    col_codpro  = next(
        (c for c in df_codpro.columns if "CODPRO" in c.upper()),
        df_codpro.columns[0],
    )
    codpro_list = df_codpro[col_codpro].dropna().astype(str).str.strip().tolist()
    print(f"CodPros a processar: {len(codpro_list)}")

    # --- 2. Carregar OAEs ---
    if not os.path.exists(PLANILHA_OAES):
        raise FileNotFoundError(f"Arquivo '{PLANILHA_OAES}' não encontrado.")
    df_oaes = pd.read_excel(PLANILHA_OAES, header=0)
    _orig_cols = list(df_oaes.columns)
    col_rename = ["CodPro", "SGE", "Nome", "Latitude", "Longitude"]
    df_oaes.columns = col_rename + [f"extra_{i}" for i in range(len(df_oaes.columns) - 5)]
    import unicodedata as _ucd
    def _norm_col(s):
        return _ucd.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    for _ci, _cn in enumerate(_orig_cols[len(col_rename):]):
        if _norm_col(_cn) == "extensao":
            df_oaes = df_oaes.rename(columns={f"extra_{_ci}": "Extensao"})
            break
    df_oaes = df_oaes.dropna(subset=["CodPro"]).copy()
    df_oaes["SGE"] = pd.to_numeric(df_oaes["SGE"], errors="coerce")  # NaN quando a OAE não tem SGE
    oae_index = df_oaes.set_index("CodPro")
    print(f"OAEs disponíveis : {len(oae_index)}")

    # --- 3. Pré-carregar rede OSM (uma única vez) ---
    def _carregar_gpkg(caminho, label, filtro_superficie=None, incluir_null=False):
        if not os.path.exists(caminho):
            print(f"  AVISO: {caminho} não encontrado.")
            return None
        print(f"\nCarregando {label}: {caminho} ...")

        _LAYER_GEO = "gis_osm_roads_free"
        try:
            import pyogrio
            _camadas   = [r[0] for r in pyogrio.list_layers(caminho)]
            _geofabrik = _LAYER_GEO in _camadas
        except Exception:
            _geofabrik = False

        if _geofabrik:
            gdf = gpd.read_file(caminho, layer=_LAYER_GEO)
            if gdf.crs and gdf.crs.to_epsg() != CRS_METRICO:
                gdf = gdf.to_crs(CRS_METRICO)
            if TIPOS_OSM:
                gdf = gdf[gdf["fclass"].isin(TIPOS_OSM)].copy()
            def _get_id_geo(row):
                for col in ("ref", "name"):
                    v = row.get(col)
                    if v and str(v) not in ("nan", "None", ""):
                        return str(v)
                return f"OSM-{row.get('fclass', 'via')}"
            gdf["_id_rodovia"] = gdf.apply(_get_id_geo, axis=1)
            gdf["_uf"]         = ""
            gdf["_fonte"]      = "OSM"
            gdf["Superficie"]  = "PAV"
            gdf["nm_tipo_tr"]  = gdf["fclass"]
            gdf["ds_local_i"]  = gdf["name"] if "name" in gdf.columns else None
            gdf["ds_local_f"]  = None
            cols = ["_id_rodovia", "_uf", "_fonte", "Superficie",
                    "nm_tipo_tr", "ds_local_i", "ds_local_f",
                    "bridge", "tunnel", "geometry"]
            gdf = gpd.GeoDataFrame(
                gdf[[c for c in cols if c in gdf.columns]],
                geometry="geometry", crs=CRS_METRICO,
            )
        else:
            gdf = gpd.read_file(caminho)
            if gdf.crs and gdf.crs.to_epsg() != CRS_METRICO:
                gdf = gdf.to_crs(CRS_METRICO)
            if filtro_superficie and "Superficie" in gdf.columns:
                f = [s.upper() for s in filtro_superficie]
                _sup = gdf["Superficie"].fillna("").astype(str).str.upper()
                _pav = _sup.isin(f)
                _nulo = gdf["Superficie"].isna() | (_sup == "")
                gdf = gdf[_pav | (incluir_null & _nulo)].copy()

        print(f"  {len(gdf):,} segmentos carregados")
        return gdf

    # A1: rede completa sem filtro de superfície
    gdf_osm_a1 = None
    if "A1" in ANALISES or "A2" in ANALISES:
        gdf_osm_a1 = _carregar_gpkg(GPKG_OSM, "OSM (A1 — todas as superfícies)",
                                      filtro_superficie=None)

    # A2: derivada da A1, filtrada para PAV + DUP + EOD + NULL
    gdf_osm_a2 = None
    if "A2" in ANALISES and gdf_osm_a1 is not None:
        print(f"\nAplicando filtro A2 (PAV + DUP + EOD + NULL)...")
        _sup_a2 = gdf_osm_a1["Superficie"].fillna("").astype(str).str.upper()
        _pav_a2 = _sup_a2.isin([s.upper() for s in SUPERFICIES_A2])
        _nul_a2 = gdf_osm_a1["Superficie"].isna() | (_sup_a2 == "")
        gdf_osm_a2 = gdf_osm_a1[_pav_a2 | _nul_a2].copy()
        print(f"  {len(gdf_osm_a2):,} segmentos após filtro A2 "
              f"(removidos: {len(gdf_osm_a1) - len(gdf_osm_a2):,})")

    os.makedirs(PASTA_SAIDA, exist_ok=True)

    # --- 4. Processar cada CodPro ---
    n_ok = n_sem_oae = n_erro = 0

    for codpro_code in codpro_list:
        print(f"\n{'─' * 70}")
        print(f"CodPro {codpro_code}")

        if codpro_code not in oae_index.index:
            print(f"  CodPro não encontrado em {PLANILHA_OAES}. Pulando.")
            n_sem_oae += 1
            continue

        row_oae  = oae_index.loc[codpro_code]
        lat      = float(row_oae["Latitude"])
        lon      = float(row_oae["Longitude"])
        nome_oae = str(row_oae.get("Nome", ""))
        sge_val  = row_oae.get("SGE")
        sge_txt  = "—" if pd.isna(sge_val) else str(int(sge_val))
        print(f"  {nome_oae}  (SGE {sge_txt})")
        print(f"  C = ({lat:.6f}, {lon:.6f})")

        try:
            _ext = float(row_oae.get("Extensao", 0) or 0)
            raio_excl_m = _ext if _ext > 0 else 50.0
        except (TypeError, ValueError):
            raio_excl_m = 50.0
        print(f"  Exclusão: {raio_excl_m:.0f} m (Extensao da OAE)")

        pasta = os.path.join(PASTA_SAIDA, str(codpro_code))
        os.makedirs(pasta, exist_ok=True)

        C_m           = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(CRS_METRICO).iloc[0]
        exclusao_geom = C_m.buffer(raio_excl_m)

        relatorio = [
            f"ESTUDO DE ROTA ALTERNATIVA — CODPRO {codpro_code} (SGE {sge_txt})",
            f"OAE     : {nome_oae}",
            f"Coord C : ({lat:.6f}, {lon:.6f})",
            f"Exclusão: raio {raio_excl_m:.0f} m",
            f"Data    : {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        try:
            # --- Análise A1: todas as superfícies ---
            if "A1" in ANALISES:
                print(f"  [A1] Todas as superfícies  |  raio {RAIO_OSM_KM} km")
                found_a1, rel_a1, res_a1 = calcular_modo(
                    codpro_code, lat, lon, C_m, exclusao_geom,
                    None, gdf_osm_a1, "OSM", pasta,
                    raio_excl_m=raio_excl_m, label="A1",
                )
                relatorio.append(rel_a1)
                relatorio.append("")
                _salvar_resultado_txt(
                    codpro_code, sge_val, "A1", "Todas as superficies (sem filtro)", res_a1,
                    os.path.join(pasta, "resultado_A1.txt"),
                )

            # --- Análise A2: PAV + DUP + EOD + NULL (exceção: A2_SEM_FILTRO_CODPRO) ---
            if "A2" in ANALISES:
                if codpro_code in A2_SEM_FILTRO_CODPRO:
                    gdf_a2_efetivo = gdf_osm_a1
                    desc_a2        = "Todas as superficies (sem filtro - OAE em via nao pavimentada)"
                    print(f"  [A2] Sem filtro de superficie (OAE em via nao pavimentada)  |  raio {RAIO_OSM_KM} km")
                else:
                    gdf_a2_efetivo = gdf_osm_a2
                    desc_a2        = "PAV + DUP + EOD + NULL"
                    print(f"  [A2] PAV + DUP + EOD + NULL  |  raio {RAIO_OSM_KM} km")
                found_a2, rel_a2, res_a2 = calcular_modo(
                    codpro_code, lat, lon, C_m, exclusao_geom,
                    None, gdf_a2_efetivo, "OSM", pasta,
                    raio_excl_m=raio_excl_m, label="A2",
                )
                relatorio.append(rel_a2)
                _salvar_resultado_txt(
                    codpro_code, sge_val, "A2", desc_a2, res_a2,
                    os.path.join(pasta, "resultado_A2.txt"),
                )

            n_ok += 1

        except Exception as e:
            msg = f"ERRO: {e}\n{traceback.format_exc()}"
            print(f"  ✗ {e}")
            relatorio.append(f"\nERRO AO PROCESSAR:\n{msg}")
            n_erro += 1

        with open(os.path.join(pasta, "relatorio.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(relatorio))
        print(f"  Salvo em: {pasta}/")

    # --- 5. Resumo ---
    print(f"\n{'=' * 70}")
    print("CONCLUÍDO")
    print(f"  Processados com sucesso : {n_ok}")
    print(f"  Não encontrados na OAE  : {n_sem_oae}")
    print(f"  Erros                   : {n_erro}")
    print(f"  Resultados em           : {PASTA_SAIDA}/")


if __name__ == "__main__":
    main()
