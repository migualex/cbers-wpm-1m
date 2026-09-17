# -*- coding: utf-8 -*-
# Created by Miguel Alexandre da Cunha
import os, re, glob, shutil, tempfile, subprocess, gc, threading
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET

import numpy as np
import requests

import pyproj
pyproj.set_use_global_context(True)

import geopandas as gpd
import rasterio
from rasterio.io import MemoryFile
from rasterio.mask import mask as rio_mask
from rasterio.merge import merge as rio_merge
from rasterio.fill import fillnodata
from rasterio import Affine
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.transform import array_bounds
from shapely.geometry import mapping, box

class PipelineError(Exception):
    """Erro esperado/tratado durante o processamento (mensagem já é amigável)."""
    pass

class PipelineCancelled(Exception):
    """Levantada quando o usuário cancela o processamento em andamento."""
    pass

_PROJ_LOCK = threading.Lock()

FIXED_MORAVEC_MIN_ABS_CORR = 0.25
MORAVEC_RELAX_LADDER = [0.25, 0.15, 0.10, 0.06]

MIN_BUFFER_KM = 0.002
MAX_BUFFER_KM = 10.0

POINT_BUFFER_MIN_KM = 2
POINT_BUFFER_MAX_KM = 10

STAC_URL   = "https://data.inpe.br/bdc/stac/v1/"
COLLECTION = "CB4A-WPM-L4-DN-1"

_TILE_RE = re.compile(r'(\d{3}_\d{3})')

def _clamp_buffer_km(value, default=2.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return min(max(value, MIN_BUFFER_KM), MAX_BUFFER_KM)

def _extract_tile(item):
    m = _TILE_RE.search(item["id"])
    return m.group(1) if m else None

def _item_date_str(item):
    dt_str = item["properties"].get("datetime") or item["properties"].get("start_datetime")
    return dt_str[:10] if dt_str else None

def _get_thumbnail_href(item):
    """BDC/STAC items normally carry a `thumbnail` asset (a .png quicklook of the
    scene). Fall back to any asset that looks like a thumbnail/preview image in
    case the collection names it differently."""
    assets = item.get("assets", {}) or {}
    for key in ("thumbnail", "browse", "preview"):
        asset = assets.get(key)
        if asset and asset.get("href"):
            return asset["href"]
    for key, asset in assets.items():
        href = (asset or {}).get("href", "")
        roles = (asset or {}).get("roles") or []
        if "thumbnail" in roles or "thumbnail" in key.lower() or href.lower().endswith(".png"):
            return href
    return None

def _load_and_buffer_roi(roi_shapefile, roi_coordinates, roi_buffer_km, temp_dir, log):
    """Lê o ROI (shapefile OU coordenadas) e aplica o pequeno buffer de contexto.
    Devolve (roi_gdf, roi_vector_path) - roi_gdf está na CRS original do ROI."""
    from shapely.geometry import Polygon

    have_shapefile = bool(roi_shapefile) and str(roi_shapefile).strip() != ""
    have_coords = roi_coordinates is not None and len(roi_coordinates) > 0

    if have_shapefile and have_coords:
        raise PipelineError("Preencha apenas UM de ROI_SHAPEFILE ou ROI_COORDINATES, e deixe o outro vazio.")
    if not have_shapefile and not have_coords:
        raise PipelineError("Informe ROI_SHAPEFILE ou ROI_COORDINATES na célula de configuração.")

    if have_shapefile:
        with _PROJ_LOCK:
            roi_gdf = gpd.read_file(roi_shapefile)
            if roi_gdf.crs is None:
                raise PipelineError("O arquivo do ROI não tem um CRS definido - corrija o arquivo de entrada.")
        roi_vector_path = roi_shapefile
        log(f"Entrada do ROI: shapefile ({roi_shapefile})")
    else:
        if len(roi_coordinates) < 3:
            raise PipelineError("ROI_COORDINATES precisa de pelo menos 3 vértices (lon, lat) para formar um polígono.")
        roi_polygon = Polygon(roi_coordinates)
        with _PROJ_LOCK:
            roi_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[roi_polygon], crs=4326)
            roi_vector_path = os.path.join(temp_dir, "roi_from_coordinates.gpkg")
            roi_gdf.to_file(roi_vector_path, driver="GPKG")
        log(f"Entrada do ROI: {len(roi_coordinates)} vértices de coordenadas digitadas (EPSG:4326)")

    roi_gdf = roi_gdf[roi_gdf.geometry.notnull() & roi_gdf.geometry.is_valid]
    if len(roi_gdf) == 0:
        raise PipelineError("O ROI não possui geometrias válidas e não nulas.")
    log(f"ROI carregado: {len(roi_gdf)} geometria(s), CRS = {roi_gdf.crs}")

    roi_buffer_km = _clamp_buffer_km(roi_buffer_km)

    if roi_buffer_km > 0:
        with _PROJ_LOCK:
            try:
                metric_crs = roi_gdf.estimate_utm_crs()
            except Exception:
                metric_crs = roi_gdf.to_crs(4326).estimate_utm_crs()
            roi_metric = roi_gdf.to_crs(metric_crs)
            _minx, _miny, _maxx, _maxy = roi_metric.total_bounds
            pad = roi_buffer_km * 1000.0
            roi_metric["geometry"] = box(_minx - pad, _miny - pad, _maxx + pad, _maxy + pad)
            roi_gdf = roi_metric.to_crs(roi_gdf.crs)
        roi_vector_path = os.path.join(temp_dir, "roi_buffered.gpkg")
        roi_gdf.to_file(roi_vector_path, driver="GPKG")
        log(f"ROI expandido em {roi_buffer_km:.3f} km para contexto (bounding box retangular)")

    return roi_gdf, roi_vector_path

def search_available_scenes(params, log=print):
    """Busca no STAC do BDC as cenas CBERS-4A/WPM que intersectam o ROI dentro do
    período [`date_start`, `date_end`] informado pelo usuário. Não levanta erro se
    nada for encontrado - devolve uma lista vazia, para que a interface possa
    sugerir ao usuário ampliar o período ou revisar o ROI, em vez de falhar direto.

    Devolve uma lista de dicts, ordenada por data (e tile):
        {id, tile, date, datetime, cloud_cover, thumbnail_url, item}
    Miniaturas NÃO são baixadas aqui (seriam N downloads por busca, a maioria
    descartada); a interface baixa sob demanda, uma por vez, ao selecionar uma
    linha - ver fetch_thumbnail_bytes() abaixo.
    `item` é o GeoJSON Feature STAC bruto (necessário depois em run_pipeline,
    via params["stac_items"], para pular a busca e processar exatamente a(s)
    cena(s) escolhida(s) pelo usuário)."""
    from shapely.geometry import shape as shapely_shape

    roi_shapefile = params.get("roi_shapefile")
    roi_coordinates = params.get("roi_coordinates")
    roi_buffer_km = params.get("roi_buffer_km", 2.0)
    date_start = params.get("date_start")
    date_end = params.get("date_end")
    if not date_start or not date_end:
        raise PipelineError("Informe date_start e date_end (período de busca) na célula de configuração.")

    start_dt = datetime.strptime(date_start, "%Y-%m-%d")
    end_dt = datetime.strptime(date_end, "%Y-%m-%d")
    if end_dt < start_dt:
        raise PipelineError("A data final do período de busca não pode ser anterior à data inicial.")

    temp_dir = tempfile.mkdtemp(prefix="cbers_wpm_search_")
    try:
        roi_gdf, _ = _load_and_buffer_roi(roi_shapefile, roi_coordinates, roi_buffer_km, temp_dir, log)
        with _PROJ_LOCK:
            roi_gdf_wgs84 = roi_gdf.to_crs(4326)
            roi_geoms_wgs84 = list(roi_gdf_wgs84.geometry.values)
            minx, miny, maxx, maxy = roi_gdf_wgs84.total_bounds
        log("Limites do ROI (lon/lat):", (minx, miny, maxx, maxy))

        def roi_intersects(other_geom_wgs84):
            return any(other_geom_wgs84.intersects(g) for g in roi_geoms_wgs84)

        start = start_dt.strftime("%Y-%m-%dT00:00:00Z")
        end   = end_dt.strftime("%Y-%m-%dT23:59:59Z")

        search_url = STAC_URL.rstrip('/') + '/search'
        query = {
            "collections": COLLECTION,
            "datetime": f"{start}/{end}",
            "bbox": f"{minx},{miny},{maxx},{maxy}",
            "limit": 100,
        }
        log(f"Buscando cenas no STAC entre {date_start} e {date_end}...")
        resp = requests.get(search_url, params=query, headers={"Accept": "application/json"}, timeout=60)
        resp.raise_for_status()
        feats = resp.json().get("features", [])
        feats = [f for f in feats if roi_intersects(shapely_shape(f["geometry"]))]
        log(f"Encontrada(s) {len(feats)} cena(s) candidata(s) intersectando o ROI.")

        results = []
        for i, it in enumerate(feats):
            date_str = _item_date_str(it)
            if date_str is None:
                continue
            tile = _extract_tile(it)
            item_dt = datetime.strptime(date_str, "%Y-%m-%d")
            props = it.get("properties", {})
            if i == 0:
                log("Chaves das properties STAC do primeiro resultado:", sorted(props.keys()))
            cloud_cover = None
            for key in ("eo:cloud_cover", "cloud_cover", "bdc:cloud_cover", "cbers:cloud_cover"):
                if props.get(key) is not None:
                    cloud_cover = props.get(key)
                    break
            entry = {
                "id": it["id"],
                "tile": tile,
                "date": date_str,
                "datetime": props.get("datetime"),
                "cloud_cover": cloud_cover,
                "days_from_start": (item_dt - start_dt).days,
                "thumbnail_url": _get_thumbnail_href(it),
                "item": it,
            }
            results.append(entry)

        results.sort(key=lambda e: (e["date"], e["tile"] or ""))
        if feats and all(e["cloud_cover"] is None for e in results):
            log("Nota: esta coleção STAC não publica valor de cobertura de nuvens para estes itens "
                "(a cobertura de nuvens aparecerá como N/A em todos os resultados).")
        return results
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def fetch_thumbnail_bytes(url, timeout=12):
    """Baixa uma única miniatura (PNG) sob demanda. Usada pela interface, em uma
    thread separada, só quando o usuário seleciona uma cena na lista - evita
    baixar N miniaturas de uma busca inteira quando só 1 ou 2 serão olhadas."""
    r = requests.get(url, timeout=timeout, headers={"Accept": "image/*"})
    r.raise_for_status()
    return r.content

MIN_FREE_RAM_GB_FULL_TILE = 16

def _estimate_entire_tile_disk_gb(stac_items, log=print):
    """Estima o espaço em disco temporário (GB) necessário para o TCLT processar
    o(s) tile(s) completo(s) informados, usando apenas metadados das bandas (sem
    baixar dados de pixel). `stac_items` é uma lista de STAC Items crus (dicts),
    como os presentes em params["stac_items"]. Devolve None se não for possível
    estimar (nesse caso a checagem de espaço em disco é pulada, não bloqueada)."""
    total_pixels = 0
    try:
        for item in stac_items:
            assets = item.get("assets", {})
            href = None
            for key in ("BAND0", "band0", "Band0"):
                if key in assets and "href" in assets[key]:
                    href = assets[key]["href"]
                    break
            if href is None:
                raise PipelineError(f"Asset BAND0 não encontrado no item {item.get('id')}.")
            vsi_path = href
            if vsi_path.startswith('http://') or vsi_path.startswith('https://'):
                vsi_path = '/vsicurl/' + vsi_path
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
                with rasterio.open(vsi_path) as src0:
                    total_pixels += src0.width * src0.height
    except Exception as exc:
        log(f"Aviso: não foi possível estimar o tamanho do tile a partir dos metadados ({exc}) "
            "- a checagem de espaço em disco será pulada.")
        return None

    if total_pixels <= 0:
        return None

    # O TCLT gera diversos produtos intermediários em resolução total ao longo do
    # grafo (recorte, restauração de nodata, registro entre bandas, fusão PCA, RGB) -
    # na prática, várias dezenas de cópias do tamanho de uma banda cheia (float32)
    # circulam pela pasta temporária durante a execução. 25x é uma estimativa
    # deliberadamente conservadora (para cima) do pico de uso de disco.
    BYTES_PER_PIXEL_F32 = 4
    N_BANDS = 5
    INTERMEDIATE_COPIES_ESTIMATE = 25
    return (total_pixels * BYTES_PER_PIXEL_F32 * N_BANDS * INTERMEDIATE_COPIES_ESTIMATE) / (1024 ** 3)

def check_entire_tile_resources(stac_items, temp_dir=None, log=print):
    """Verifica RAM livre e espaço em disco disponível ANTES de processar um tile
    completo, e lança PipelineError de imediato se os recursos forem insuficientes -
    interrompendo tudo, sem permitir que o processamento comece. Pode ser chamada
    tanto pela interface (antes de iniciar o processamento, bloqueando o início)
    quanto internamente por run_pipeline (checagem de segurança, já dentro do
    subprocesso). `temp_dir`, se informado, é a pasta cujo volume será checado
    quanto a espaço livre; por padrão, usa a pasta temporária padrão do sistema."""
    try:
        import psutil
        avail_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        if avail_ram_gb < MIN_FREE_RAM_GB_FULL_TILE:
            raise PipelineError(
                "Memória RAM livre insuficiente para processar o tile completo ({:.1f} GB "
                "disponíveis; recomendado no mínimo {} GB livres). Processar o tile inteiro (em "
                "vez de uma ROI recortada) exige bem mais memória RAM, pois as bandas em resolução "
                "total do tile precisam ser mantidas e mescladas na memória durante o processamento. "
                "Libere recursos, feche outros programas, ou desmarque a opção \"Processar tile "
                "inteiro\" e utilize uma ROI menor.".format(avail_ram_gb, MIN_FREE_RAM_GB_FULL_TILE))
    except ImportError:
        log("Aviso: biblioteca 'psutil' não encontrada - não foi possível verificar a RAM livre "
            "antes de processar o tile completo.")

    log("Consultando os metadados das bandas para estimar o espaço em disco necessário "
        "(sem baixar os dados)...")
    estimated_gb = _estimate_entire_tile_disk_gb(stac_items, log=log)
    if estimated_gb is not None:
        log(f"Estimativa de espaço temporário necessário para o TCLT processar o tile "
            f"completo: ~{estimated_gb:.0f} GB (estimativa aproximada - o uso real depende "
            f"do comportamento interno do TCLT).")
        check_dir = temp_dir or tempfile.gettempdir()
        try:
            free_gb = shutil.disk_usage(check_dir).free / (1024 ** 3)
        except OSError:
            free_gb = None
        if free_gb is not None:
            log(f"Espaço livre na unidade correspondente: {free_gb:.1f} GB.")
            if free_gb < estimated_gb:
                raise PipelineError(
                    "Espaço em disco provavelmente insuficiente para processar o tile completo: "
                    "estima-se a necessidade de aproximadamente {:.0f} GB de espaço temporário, "
                    "mas a unidade correspondente possui apenas "
                    "{:.1f} GB livres.".format(estimated_gb, free_gb))

def run_pipeline(params, log=print, should_cancel=None):
    """Executa o pipeline completo do CBERS-4A/WPM e retorna a lista de
    caminhos dos produtos finais gerados (RGB e NRGB).

    `params` é um dict com as mesmas opções da célula de CONFIGURATION do
    notebook (roi_shapefile, roi_coordinates, target_date, tclt_exe,
    final_output_dir, contrast_stretch, threads, cropped, output_crs,
    output_format, lossless, jp2_quality).
    `log` é chamado com uma única string de mensagem por vez.
    `should_cancel` é uma função sem argumentos que retorna True quando o
    usuário pediu cancelamento (normalmente QgsTask.isCanceled)."""

    if should_cancel is None:
        should_cancel = lambda: False

    _raw_log = log

    def log(*args):
        _raw_log(" ".join(str(a) for a in args))

    def _check_cancel():
        if should_cancel():
            raise PipelineCancelled("Processamento cancelado pelo usuário.")

    ROI_SHAPEFILE     = params.get("roi_shapefile")
    ROI_COORDINATES   = params.get("roi_coordinates")
    ROI_BUFFER_KM     = params.get("roi_buffer_km", 2.0)
    TARGET_DATE       = params.get("target_date")
    STAC_ITEMS        = params.get("stac_items")
    TCLT_EXE          = params["tclt_exe"]
    FINAL_OUTPUT_DIR  = params["final_output_dir"]
    CONTRAST_STRETCH  = params.get("contrast_stretch", 800)
    THREADS           = params.get("threads", 10)
    OUTPUT_CRS        = params.get("output_crs")
    OUTPUT_FORMAT     = params.get("output_format", "JP2")
    LOSSLESS          = params.get("lossless", False)
    JP2_QUALITY       = params.get("jp2_quality", 100)
    GENERATE_RGB      = params.get("generate_rgb", True)
    GENERATE_NGB      = params.get("generate_ngb", False)

    DATE_SEARCH_STEP_DAYS = 5
    DATE_SEARCH_MAX_DAYS  = 60

    if not STAC_ITEMS and not TARGET_DATE:
        raise PipelineError("Informe stac_items (cenas selecionadas na interface) ou target_date.")

    os.makedirs(FINAL_OUTPUT_DIR, exist_ok=True)
    TEMP_DIR = tempfile.mkdtemp(prefix="cbers_wpm_")
    log("Diretório de trabalho temporário:", TEMP_DIR)

    PROCESS_ENTIRE_TILE = bool(params.get("process_entire_tile"))

    try:

        roi_gdf, ROI_VECTOR_PATH = _load_and_buffer_roi(
            ROI_SHAPEFILE, ROI_COORDINATES, ROI_BUFFER_KM, TEMP_DIR, log)

        with _PROJ_LOCK:
            roi_gdf_wgs84 = roi_gdf.to_crs(4326)
            roi_geoms_wgs84 = list(roi_gdf_wgs84.geometry.values)
            minx, miny, maxx, maxy = roi_gdf_wgs84.total_bounds
        log("Limites do ROI (lon/lat):", (minx, miny, maxx, maxy))

        def roi_intersects(other_geom_wgs84):
            return any(other_geom_wgs84.intersects(g) for g in roi_geoms_wgs84)

        _roi_crs_cache = {}
        def roi_in_crs(target_crs):
            key = target_crs.to_string()
            if key not in _roi_crs_cache:
                with _PROJ_LOCK:
                    _roi_crs_cache[key] = roi_gdf.to_crs(target_crs)
            return _roi_crs_cache[key]

        from shapely.geometry import shape as shapely_shape

        extract_tile = _extract_tile

        if STAC_ITEMS:
            selected_items = {}
            for it in STAC_ITEMS:
                tile = extract_tile(it)
                if tile is None:
                    raise PipelineError(f"Não foi possível extrair o id do tile da cena selecionada '{it.get('id')}'.")
                if tile in selected_items and selected_items[tile]["id"] != it["id"]:
                    raise PipelineError(
                        f"Duas cenas diferentes foram selecionadas para o mesmo tile ({tile}): "
                        f"'{selected_items[tile]['id']}' e '{it['id']}'. Selecione apenas uma cena por tile.")
                selected_items[tile] = it

            not_intersecting = [
                tile for tile, it in selected_items.items()
                if not roi_intersects(shapely_shape(it["geometry"]))
            ]
            if not_intersecting:
                raise PipelineError(
                    "A(s) seguinte(s) cena(s) selecionada(s) não intersectam o ROI: " + ", ".join(not_intersecting))

            from shapely.ops import unary_union
            tiles_union_wgs84 = unary_union([shapely_shape(it["geometry"]) for it in selected_items.values()])
            roi_union_wgs84 = unary_union(roi_geoms_wgs84)
            roi_area = roi_union_wgs84.area
            if roi_area > 0:
                covered_fraction = roi_union_wgs84.intersection(tiles_union_wgs84).area / roi_area
                if covered_fraction < 0.98:
                    raise PipelineError(
                    "O bounding box solicitado ultrapassa a área de cobertura da(s) "
                    "cena(s) selecionada(s) (apenas {:.0f}% da área solicitada está coberta). Reduza o "
                    "tamanho do ROI ou selecione um tile adjacente adicional, caso esteja "
                    "disponível para essa data.".format(covered_fraction * 100))

            log("Usando a(s) cena(s) selecionada(s) pelo usuário:")
            for tile, it in selected_items.items():
                log(f"  tile {tile}  ->  item '{it['id']}'  (date {it['properties'].get('datetime')})")
            if len(selected_items) > 1:
                log("OBSERVAÇÃO: o ROI cobre mais de um tile; as bandas de todos os tiles serão mescladas/mosaicadas antes do processamento.")

            _dates = sorted(_item_date_str(it) for it in selected_items.values() if _item_date_str(it))
            EFFECTIVE_DATE = _dates[0] if _dates else datetime.now().strftime("%Y-%m-%d")

        else:
            def stac_search_by_bbox(stac_url, collection, bbox, target_date, step_days, max_days):
                search_url = stac_url.rstrip('/') + '/search'
                minx, miny, maxx, maxy = bbox
                target_dt = datetime.strptime(target_date, "%Y-%m-%d")
                window = step_days
                while window <= max_days:
                    start = (target_dt - timedelta(days=window)).strftime("%Y-%m-%dT00:00:00Z")
                    end   = (target_dt + timedelta(days=window)).strftime("%Y-%m-%dT23:59:59Z")
                    search_params = {
                        "collections": collection,
                        "datetime": f"{start}/{end}",
                        "bbox": f"{minx},{miny},{maxx},{maxy}",
                        "limit": 100,
                    }
                    resp = requests.get(search_url, params=search_params, headers={"Accept": "application/json"}, timeout=60)
                    resp.raise_for_status()
                    feats = resp.json().get("features", [])

                    feats = [f for f in feats if roi_intersects(shapely_shape(f["geometry"]))]

                    if feats:
                        return feats, window
                    log(f"  Nenhuma imagem intersectando o ROI dentro de +/-{window} dias de {target_date}; ampliando janela de busca...")
                    window += step_days
                raise PipelineError(f"Nenhum item STAC intersectando o ROI foi encontrado dentro de +/-{max_days} dias de {target_date}.")

            items, used_window = stac_search_by_bbox(STAC_URL, COLLECTION, (minx, miny, maxx, maxy),
                                                      TARGET_DATE, DATE_SEARCH_STEP_DAYS, DATE_SEARCH_MAX_DAYS)
            log(f"Encontrado(s) {len(items)} item(ns) STAC candidato(s) intersectando o ROI, dentro de +/-{used_window} dia(s) de {TARGET_DATE}.")

            def item_date(item):
                dt_str = item["properties"].get("datetime") or item["properties"].get("start_datetime")
                return datetime.strptime(dt_str[:10], "%Y-%m-%d")

            target_dt = datetime.strptime(TARGET_DATE, "%Y-%m-%d")
            best_by_tile = {}
            for it in items:
                tile = extract_tile(it)
                if tile is None:
                    continue
                diff = abs((item_date(it) - target_dt).days)
                if tile not in best_by_tile or diff < best_by_tile[tile][0]:
                    best_by_tile[tile] = (diff, it)

            if not best_by_tile:
                raise PipelineError("Não foi possível extrair o id do tile de nenhum dos itens STAC retornados.")

            selected_items = {tile: val[1] for tile, val in best_by_tile.items()}
            log("Tile(s) selecionado(s) automaticamente por intersectarem o ROI:")
            for tile, it in selected_items.items():
                log(f"  tile {tile}  ->  item '{it['id']}'  (date {it['properties'].get('datetime')})")
            if len(selected_items) > 1:
                log("OBSERVAÇÃO: o ROI cobre mais de um tile; as bandas de todos os tiles serão mescladas/mosaicadas antes do processamento.")

            EFFECTIVE_DATE = TARGET_DATE

        if PROCESS_ENTIRE_TILE:
            from shapely.ops import unary_union
            tiles_union_wgs84 = unary_union([shapely_shape(it["geometry"]) for it in selected_items.values()])
            with _PROJ_LOCK:
                roi_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[tiles_union_wgs84], crs=4326)
                ROI_VECTOR_PATH = os.path.join(TEMP_DIR, "roi_full_tile.gpkg")
                roi_gdf.to_file(ROI_VECTOR_PATH, driver="GPKG")
            roi_geoms_wgs84 = list(roi_gdf.geometry.values)
            _roi_crs_cache.clear()
            log("Opção \"Processar tile inteiro\" ativada: a extensão total do(s) tile(s) selecionado(s) "
                "será processada, sem recorte pela ROI original.")

        def get_band_href(item, band_idx):
            assets = item.get("assets", {})
            for key in (f"BAND{band_idx}", f"band{band_idx}", f"Band{band_idx}"):
                if key in assets and "href" in assets[key]:
                    return assets[key]["href"]
            raise PipelineError(f"Asset BAND{band_idx} não encontrado no item {item['id']}.")

        def get_xml_content(item):
            assets = item.get("assets", {})
            for key, asset in assets.items():
                if key.lower().endswith("xml") or key.lower() == "metadata":
                    href = asset.get("href")
                    if href:
                        r = requests.get(href, timeout=60)
                        if r.status_code == 200:
                            return r.text
            return None

        def get_elev_from_xml(xml_content):
            if not xml_content:
                return 90.0
            root = ET.fromstring(xml_content)
            image_elem = None
            for child in root:
                if 'image' in child.tag and 'imageMode' not in child.tag:
                    image_elem = child
                    break
            if image_elem is None:
                return 90.0
            for child in image_elem:
                if 'sunPosition' in child.tag:
                    return float(child[0].text)
            return 90.0

        def to_vsicurl(href):
            if href.startswith('/vsicurl/') or href.startswith('/vsis3/'):
                return href
            if href.startswith('http://') or href.startswith('https://'):
                return '/vsicurl/' + href
            return href

        if PROCESS_ENTIRE_TILE:
            check_entire_tile_resources(list(selected_items.values()), temp_dir=TEMP_DIR, log=log)

        def crop_remote_band(href):
            vsi_path = to_vsicurl(href)
            with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
                with rasterio.open(vsi_path) as src:
                    nodata = src.nodata if src.nodata is not None else 0
                    roi_native = roi_in_crs(src.crs)
                    geoms = [mapping(g) for g in roi_native.geometry]
                    out_image, out_transform = rio_mask(src, geoms, crop=True, filled=False, all_touched=True)
                    profile = src.profile.copy()
                    profile.update(height=out_image.shape[1], width=out_image.shape[2],
                                    transform=out_transform, nodata=nodata)
            return out_image, profile

        def merge_band_crops(crops):
            if len(crops) == 1:
                arr, profile = crops[0]
                return np.asarray(arr.filled(profile['nodata']) if np.ma.isMaskedArray(arr) else arr), profile

            ref_crs = crops[0][1]['crs']

            mem_files, datasets = [], []
            try:
                for arr, profile in crops:
                    data = arr.filled(profile['nodata']) if np.ma.isMaskedArray(arr) else arr
                    if profile['crs'] != ref_crs:
                        dst_transform, dst_w, dst_h = calculate_default_transform(
                            profile['crs'], ref_crs, profile['width'], profile['height'],
                            *array_bounds(profile['height'], profile['width'], profile['transform']))
                        dst_profile = profile.copy()
                        dst_profile.update(crs=ref_crs, transform=dst_transform, width=dst_w, height=dst_h)
                        dst_data = np.zeros((data.shape[0], dst_h, dst_w), dtype=data.dtype)
                        reproject(
                            source=data, destination=dst_data,
                            src_transform=profile['transform'], src_crs=profile['crs'],
                            dst_transform=dst_transform, dst_crs=ref_crs,
                            src_nodata=profile['nodata'], dst_nodata=profile['nodata'],
                            resampling=Resampling.cubic)
                        data, profile = dst_data, dst_profile
                    mf = MemoryFile()
                    mem_profile = profile.copy()
                    mem_profile.update(driver='GTiff', BIGTIFF='YES')
                    with mf.open(**mem_profile) as ds:
                        ds.write(data)
                    mem_files.append(mf)
                    datasets.append(mf.open())
                merged_arr, merged_transform = rio_merge(datasets, nodata=datasets[0].nodata)
                ref_profile = datasets[0].profile.copy()
                ref_profile.update(height=merged_arr.shape[1], width=merged_arr.shape[2], transform=merged_transform)
                return merged_arr, ref_profile
            finally:
                for ds in datasets:
                    ds.close()
                for mf in mem_files:
                    mf.close()

        scene_date_tag = EFFECTIVE_DATE.replace('-', '')
        tile_tag = "_".join(sorted(selected_items.keys()))
        SCENE_ID = f"CBERS_4A_WPM_{scene_date_tag}_{tile_tag}_L4"

        primary_item = next(iter(selected_items.values()))
        xml_content = get_xml_content(primary_item)
        elev = get_elev_from_xml(xml_content)
        azimuth = 90.0 - elev
        cos_azimuth = np.cos(np.radians(azimuth))
        log(f"Elevação solar: {elev:.2f} graus  ->  cos(azimute) = {cos_azimuth:.4f}")

        def process_band(band_idx):
            crops = []
            for tile, item in selected_items.items():
                href = get_band_href(item, band_idx)
                log(f"  BAND{band_idx}: lendo + recortando tile {tile} a partir do asset STAC (rede, em memória)...")
                crops.append(crop_remote_band(href))

            band_arr, profile = merge_band_crops(crops)
            nodata = profile['nodata']

            valid_mask = (band_arr[0] != nodata).astype('uint8') * 255
            filled = fillnodata(band_arr[0].astype('float32'), mask=valid_mask,
                                 max_search_distance=1, smoothing_iterations=0)
            filled = (filled / cos_azimuth).astype('float32')

            t = profile['transform']
            shifted_transform = Affine(t.a, t.b, t.c - t.a * 0.75,
                                        t.d, t.e, t.f + t.a * 0.75)

            out_profile = profile.copy()
            out_profile.update(count=1, dtype='float32', transform=shifted_transform,
                                driver='GTiff', compress='lzw', predictor=3, tiled=True,
                                blockxsize=512, blockysize=512, num_threads='ALL_CPUS',
                                BIGTIFF='YES', nodata=nodata)

            out_path = os.path.join(TEMP_DIR, f"{SCENE_ID}_BAND{band_idx}_FD_Elev_Shift.tif")
            with rasterio.open(out_path, 'w', **out_profile) as dst:
                dst.write(filled, 1)

            del crops, band_arr, valid_mask, filled
            gc.collect()
            return out_path

        preprocessed_paths = {}
        for i in range(5):
            _check_cancel()
            log(f"Processando BAND{i}...")
            preprocessed_paths[f"BAND{i}"] = process_band(i)
        log("Bandas pré-processadas prontas na pasta temporária.")

        def to_uri(path):
            return "file://" + path.replace("\\\\", "/")

        def generate_tclt_txt(output_txt_path, preproc_files, roi_path, final_roi_path, analysis_name, moravec_min_abs_corr):
            restore_src = {
                b: f"Clipped_by_polygons_imageB_{b}_vertice"
                for b in range(5)
            }

            clip_block = """
          VERTICE_START
            VERTICE_NAME "Clipped_by_polygons_imageB_4_vertice"
            VERTICE_TYPE "RASTERCLIPPING"
            VERTICE_CONNECTION "RASTER_B_4ToClip_vertice" "RASTER"
            VERTICE_CONNECTION "POLYGONS_vertice" "POLYGONS"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "Clipped_by_polygons_imageB_3_vertice"
            VERTICE_TYPE "RASTERCLIPPING"
            VERTICE_CONNECTION "RASTER_B_3ToClip_vertice" "RASTER"
            VERTICE_CONNECTION "POLYGONS_vertice" "POLYGONS"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "Clipped_by_polygons_imageB_2_vertice"
            VERTICE_TYPE "RASTERCLIPPING"
            VERTICE_CONNECTION "RASTER_B_2ToClip_vertice" "RASTER"
            VERTICE_CONNECTION "POLYGONS_vertice" "POLYGONS"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "Clipped_by_polygons_imageB_1_vertice"
            VERTICE_TYPE "RASTERCLIPPING"
            VERTICE_CONNECTION "RASTER_B_1ToClip_vertice" "RASTER"
            VERTICE_CONNECTION "POLYGONS_vertice" "POLYGONS"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "Clipped_by_polygons_imageB_0_vertice"
            VERTICE_TYPE "RASTERCLIPPING"
            VERTICE_CONNECTION "RASTER_B_0ToClip_vertice" "RASTER"
            VERTICE_CONNECTION "POLYGONS_vertice" "POLYGONS"
          VERTICE_END
        """

            content = f"""#Define arquivos imagem de entrada multiespectrais e Pancromatica
        CONTEXT_START
          CONTEXT_NAME "Context_Name_1"
          RESOURCE_URI "raster resource B_0" "{to_uri(preproc_files['BAND0'])}"
          RESOURCE_URI "raster resource B_1" "{to_uri(preproc_files['BAND1'])}"
          RESOURCE_URI "raster resource B_2" "{to_uri(preproc_files['BAND2'])}"
          RESOURCE_URI "raster resource B_3" "{to_uri(preproc_files['BAND3'])}"
          RESOURCE_URI "raster resource B_4" "{to_uri(preproc_files['BAND4'])}"
          RESOURCE_URI "vector resource 2" "{to_uri(roi_path)}"
          RESOURCE_URI "vector resource 3" "{to_uri(final_roi_path)}"
        CONTEXT_END

        GRAPH_START
          GRAPH_NAME "Graph_Name_1"

          VERTICE_START
            VERTICE_NAME "POLYGONS_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "vector resource 2" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "VECTOR_URI_IO_TYPE"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "POLYGONS_FINAL_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "vector resource 3" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "VECTOR_URI_IO_TYPE"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "RASTER_B_4ToClip_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "raster resource B_4" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "RASTER_URI_IO_TYPE"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "RASTER_B_3ToClip_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "raster resource B_3" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "RASTER_URI_IO_TYPE"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "RASTER_B_2ToClip_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "raster resource B_2" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "RASTER_URI_IO_TYPE"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "RASTER_B_1ToClip_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "raster resource B_1" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "RASTER_URI_IO_TYPE"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "RASTER_B_0ToClip_vertice"
            VERTICE_TYPE "URI"
            VERTICE_RESOURCE "raster resource B_0" "URI_ALIAS"
            VERTICE_PARAM "OUTPUT_TYPE" "RASTER_URI_IO_TYPE"
          VERTICE_END
        {clip_block}
          VERTICE_START
            VERTICE_NAME "restore_B4_vertice"
            VERTICE_TYPE "RASTER_RESTORATION"
            VERTICE_CONNECTION "{restore_src[4]}" "INPUT_RASTER"
            VERTICE_PARAM "SENSOR_TYPE" "CBERS_4_MUX"
            VERTICE_PARAM "SENSOR_BAND_DESIGNATIONS" "8"
            VERTICE_PARAM "RASTER_BANDS" "0"
            VERTICE_PARAM "SAMPLING_FACTOR" "SAMPLING_FACTOR_1_BY_2"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "restore_B3_vertice"
            VERTICE_TYPE "RASTER_RESTORATION"
            VERTICE_CONNECTION "{restore_src[3]}" "INPUT_RASTER"
            VERTICE_PARAM "SENSOR_TYPE" "CBERS_4_MUX"
            VERTICE_PARAM "SENSOR_BAND_DESIGNATIONS" "7"
            VERTICE_PARAM "RASTER_BANDS" "0"
            VERTICE_PARAM "SAMPLING_FACTOR" "SAMPLING_FACTOR_1_BY_2"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "restore_B2_vertice"
            VERTICE_TYPE "RASTER_RESTORATION"
            VERTICE_CONNECTION "{restore_src[2]}" "INPUT_RASTER"
            VERTICE_PARAM "SENSOR_TYPE" "CBERS_4_MUX"
            VERTICE_PARAM "SENSOR_BAND_DESIGNATIONS" "6"
            VERTICE_PARAM "RASTER_BANDS" "0"
            VERTICE_PARAM "SAMPLING_FACTOR" "SAMPLING_FACTOR_1_BY_2"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "restore_B1_vertice"
            VERTICE_TYPE "RASTER_RESTORATION"
            VERTICE_CONNECTION "{restore_src[1]}" "INPUT_RASTER"
            VERTICE_PARAM "SENSOR_TYPE" "CBERS_4_MUX"
            VERTICE_PARAM "SENSOR_BAND_DESIGNATIONS" "5"
            VERTICE_PARAM "RASTER_BANDS" "0"
            VERTICE_PARAM "SAMPLING_FACTOR" "SAMPLING_FACTOR_1_BY_2"
          VERTICE_END
          VERTICE_START
            VERTICE_NAME "restore_B0_vertice"
            VERTICE_TYPE "RASTER_RESTORATION"
            VERTICE_CONNECTION "{restore_src[0]}" "INPUT_RASTER"
            VERTICE_PARAM "SENSOR_TYPE" "CBERS2B_HRC"
            VERTICE_PARAM "SENSOR_BAND_DESIGNATIONS" "1"
            VERTICE_PARAM "RASTER_BANDS" "0"
            VERTICE_PARAM "SAMPLING_FACTOR" "SAMPLING_FACTOR_1_BY_2"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "TiePointsB4_Vertice"
            VERTICE_TYPE "TPLOCATOR"
            VERTICE_CONNECTION "restore_B0_vertice" "INPUT_1"
            VERTICE_CONNECTION "restore_B4_vertice" "INPUT_2"
            VERTICE_PARAM "EXEC_MODE" "1"
            VERTICE_PARAM "TP_LOC_ALGO" "MORAVEC"
            VERTICE_PARAM "BAND_1" "0"
            VERTICE_PARAM "BAND_2" "0"
            VERTICE_PARAM "MAX_TIE_POINTS" "1000"
            VERTICE_PARAM "PIXEL_SIZE_RELATION" "0"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "MAX_ERROR" "10"
            VERTICE_PARAM "MIN_TP_AREA_PERCENT" "5"
            VERTICE_PARAM "MIN_TP_NUMBER_FACTOR" "5"
            VERTICE_PARAM "ENABLE_SUBSAMPLE_OPT" "YES"
            VERTICE_PARAM "MORAVEC_CORR_WINDOW_W" "21"
            VERTICE_PARAM "MORAVEC_WINDOW_W" "21"
            VERTICE_PARAM "MORAVEC_FILTER_IT" "1"
            VERTICE_PARAM "MORAVEC_MIN_ABS_CORR" "{moravec_min_abs_corr}"
            VERTICE_PARAM "ENABLE_AUTO_REPROJ" "YES"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "RegisterB4_UsingAdjXY_Vertice"
            VERTICE_TYPE "REGISTER"
            VERTICE_CONNECTION "restore_B4_vertice" "INPUT_RASTER"
            VERTICE_CONNECTION "TiePointsB4_Vertice" "INPUT_TIE_POINTS"
            VERTICE_PARAM "OUT_SRID" "0"
            VERTICE_PARAM "OUT_RES_X" "2"
            VERTICE_PARAM "OUT_RES_Y" "2"
            VERTICE_PARAM "INTERP_METHOD" "BILINEAR_INTERP"
            VERTICE_PARAM "OUT_NO_DATA_VALUE" "-10000"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "TP_REF_SRID_PROP_NAME" "SRID_1"
            VERTICE_PARAM "TP_REF_X_PROP_NAME" "X_1"
            VERTICE_PARAM "TP_REF_Y_PROP_NAME" "Y_1"
            VERTICE_PARAM "TP_ADJ_SRID_PROP_NAME" "SRID_2"
            VERTICE_PARAM "TP_ADJ_X_PROP_NAME" "X_2"
            VERTICE_PARAM "TP_ADJ_Y_PROP_NAME" "Y_2"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "RegisterB3_UsingAdjXY_Vertice"
            VERTICE_TYPE "REGISTER"
            VERTICE_CONNECTION "restore_B3_vertice" "INPUT_RASTER"
            VERTICE_CONNECTION "TiePointsB4_Vertice" "INPUT_TIE_POINTS"
            VERTICE_PARAM "OUT_SRID" "0"
            VERTICE_PARAM "OUT_RES_X" "2"
            VERTICE_PARAM "OUT_RES_Y" "2"
            VERTICE_PARAM "INTERP_METHOD" "BILINEAR_INTERP"
            VERTICE_PARAM "OUT_NO_DATA_VALUE" "-10000"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "TP_REF_SRID_PROP_NAME" "SRID_1"
            VERTICE_PARAM "TP_REF_X_PROP_NAME" "X_1"
            VERTICE_PARAM "TP_REF_Y_PROP_NAME" "Y_1"
            VERTICE_PARAM "TP_ADJ_SRID_PROP_NAME" "SRID_2"
            VERTICE_PARAM "TP_ADJ_X_PROP_NAME" "X_2"
            VERTICE_PARAM "TP_ADJ_Y_PROP_NAME" "Y_2"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "TiePointsB2_Vertice"
            VERTICE_TYPE "TPLOCATOR"
            VERTICE_CONNECTION "restore_B0_vertice" "INPUT_1"
            VERTICE_CONNECTION "restore_B2_vertice" "INPUT_2"
            VERTICE_PARAM "EXEC_MODE" "1"
            VERTICE_PARAM "TP_LOC_ALGO" "MORAVEC"
            VERTICE_PARAM "BAND_1" "0"
            VERTICE_PARAM "BAND_2" "0"
            VERTICE_PARAM "MAX_TIE_POINTS" "1000"
            VERTICE_PARAM "PIXEL_SIZE_RELATION" "0"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "MAX_ERROR" "10"
            VERTICE_PARAM "MIN_TP_AREA_PERCENT" "5"
            VERTICE_PARAM "MIN_TP_NUMBER_FACTOR" "5"
            VERTICE_PARAM "ENABLE_SUBSAMPLE_OPT" "YES"
            VERTICE_PARAM "MORAVEC_CORR_WINDOW_W" "21"
            VERTICE_PARAM "MORAVEC_WINDOW_W" "21"
            VERTICE_PARAM "MORAVEC_FILTER_IT" "1"
            VERTICE_PARAM "MORAVEC_MIN_ABS_CORR" "{moravec_min_abs_corr}"
            VERTICE_PARAM "ENABLE_AUTO_REPROJ" "YES"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "RegisterB2_UsingAdjXY_Vertice"
            VERTICE_TYPE "REGISTER"
            VERTICE_CONNECTION "restore_B2_vertice" "INPUT_RASTER"
            VERTICE_CONNECTION "TiePointsB2_Vertice" "INPUT_TIE_POINTS"
            VERTICE_PARAM "OUT_SRID" "0"
            VERTICE_PARAM "OUT_RES_X" "2"
            VERTICE_PARAM "OUT_RES_Y" "2"
            VERTICE_PARAM "INTERP_METHOD" "BILINEAR_INTERP"
            VERTICE_PARAM "OUT_NO_DATA_VALUE" "-10000"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "TP_REF_SRID_PROP_NAME" "SRID_1"
            VERTICE_PARAM "TP_REF_X_PROP_NAME" "X_1"
            VERTICE_PARAM "TP_REF_Y_PROP_NAME" "Y_1"
            VERTICE_PARAM "TP_ADJ_SRID_PROP_NAME" "SRID_2"
            VERTICE_PARAM "TP_ADJ_X_PROP_NAME" "X_2"
            VERTICE_PARAM "TP_ADJ_Y_PROP_NAME" "Y_2"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "RegisterB1_UsingAdjXY_Vertice"
            VERTICE_TYPE "REGISTER"
            VERTICE_CONNECTION "restore_B1_vertice" "INPUT_RASTER"
            VERTICE_CONNECTION "TiePointsB4_Vertice" "INPUT_TIE_POINTS"
            VERTICE_PARAM "OUT_SRID" "0"
            VERTICE_PARAM "OUT_RES_X" "2"
            VERTICE_PARAM "OUT_RES_Y" "2"
            VERTICE_PARAM "INTERP_METHOD" "BILINEAR_INTERP"
            VERTICE_PARAM "OUT_NO_DATA_VALUE" "-10000"
            VERTICE_PARAM "TRANS_NAME" "Affine"
            VERTICE_PARAM "TP_REF_SRID_PROP_NAME" "SRID_1"
            VERTICE_PARAM "TP_REF_X_PROP_NAME" "X_1"
            VERTICE_PARAM "TP_REF_Y_PROP_NAME" "Y_1"
            VERTICE_PARAM "TP_ADJ_SRID_PROP_NAME" "SRID_2"
            VERTICE_PARAM "TP_ADJ_X_PROP_NAME" "X_2"
            VERTICE_PARAM "TP_ADJ_Y_PROP_NAME" "Y_2"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "Recomposed_registeredimage_vertice"
            VERTICE_TYPE "RECOMPOSE"
            VERTICE_CONNECTION "RegisterB4_UsingAdjXY_Vertice" "INPUT_RASTER_1"
            VERTICE_CONNECTION "RegisterB3_UsingAdjXY_Vertice" "INPUT_RASTER_2"
            VERTICE_CONNECTION "RegisterB2_UsingAdjXY_Vertice" "INPUT_RASTER_3"
            VERTICE_CONNECTION "RegisterB1_UsingAdjXY_Vertice" "INPUT_RASTER_4"
            VERTICE_PARAM "BANDS_STRING" "INPUT_RASTER_1:0 INPUT_RASTER_2:0 INPUT_RASTER_3:0 INPUT_RASTER_4:0"
            VERTICE_PARAM "VIRTUAL" "NO"
            VERTICE_PARAM "ALLOW_NO_DATA_PIXELS" "NO"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "PanT2_vertice"
            VERTICE_TYPE "ARITHMETIC"
            VERTICE_CONNECTION "restore_B0_vertice" "INPUT_RASTER_1"
            VERTICE_PARAM "ARITHMETIC_STRING" "( INPUT_RASTER_1:0 * 2.0 )"
            VERTICE_PARAM "NORMALIZE_OUTPUT" "NO"
            VERTICE_PARAM "INTERP_METHOD" "NN_INTERP"
            VERTICE_PARAM "OUT_RST_DATA_TYPE" "FLOAT_TYPE"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "FUSION_Vertice"
            VERTICE_TYPE "FUSION"
            VERTICE_CONNECTION "PanT2_vertice" "HR_RASTER"
            VERTICE_CONNECTION "Recomposed_registeredimage_vertice" "LR_RASTER"
            VERTICE_PARAM "FUSION_METHOD" "PCA"
            VERTICE_PARAM "INTERP_METHOD" "BILINEAR_INTERP"
            VERTICE_PARAM "AUTO_ALIGN_AND_CLIP" "NO"
            VERTICE_PARAM "PCA_HISTO_FIT_METHOD" "NO_FIT"
            VERTICE_PARAM "LR_BANDS_INDEXES" "0;1;2;3"
            VERTICE_PARAM "HR_BAND_IDX" "0"
            VERTICE_PARAM "OUT_NODATA_VALUE" "-10000"
            VERTICE_PARAM "PCA_DIRECT_MATRIX" "0.56744643403671413;0.51869586796396927;0.45771024318085091;0.4466099801207401;-0.81958938283057925;0.33064924537518092;0.26527989286495773;0.38544895709519045;0.078342475037743664;-0.40052355311853272;-0.42936005778122865;0.80566325520304494;-0.011842493629609555;-0.67912190105108083;0.73196847781238716;0.05362225607353481"
            VERTICE_PARAM "PCA_OUT_MIN_VALUE" "1"
            VERTICE_PARAM "PCA_OUT_MAX_VALUE" "1023"
            VERTICE_OUT_CACHE "YES"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "NODATA_NRGB_Image_VERTICE"
            VERTICE_TYPE "RASTERTRANSFORM"
            VERTICE_CONNECTION "FUSION_Vertice" "INPUT_RASTER"
            VERTICE_PARAM "BANDS_INDEXES" "0;1;2;3"
            VERTICE_PARAM "OPERATIONS" "NODATA_OP_TYPE"
            VERTICE_PARAM "OLD_NODATA_VALUE" "0"
            VERTICE_PARAM "NEW_NODATA_VALUE" "-10000"
            VERTICE_PARAM "UPDATE_PIXEL_VALUES" "NO"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "Recomposed_RGBimage_vertice"
            VERTICE_TYPE "RECOMPOSE"
            VERTICE_CONNECTION "NODATA_NRGB_Image_VERTICE" "INPUT_RASTER_1"
            VERTICE_PARAM "BANDS_STRING" "INPUT_RASTER_1:1 INPUT_RASTER_1:2 INPUT_RASTER_1:3"
            VERTICE_PARAM "VIRTUAL" "NO"
            VERTICE_PARAM "ALLOW_NO_DATA_PIXELS" "NO"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "SQUARE_ROOT800_ContrastRGB_Vertice"
            VERTICE_TYPE "CONTRAST"
            VERTICE_CONNECTION "Recomposed_RGBimage_vertice" "INPUT_RASTER"
            VERTICE_PARAM "BANDS_INDEXES" "0;1;2"
            VERTICE_PARAM "CONTRAST_TYPE" "SQUARE_ROOT"
            VERTICE_PARAM "OUT_RANGE_MIN" "1"
            VERTICE_PARAM "OUT_RANGE_MAX" "1023"
            VERTICE_PARAM "SQUARE_R_MIN_INPUT" "0;0;0"
            VERTICE_PARAM "SQUARE_R_MAX_INPUT" "{CONTRAST_STRETCH};{CONTRAST_STRETCH};{CONTRAST_STRETCH}"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "NODATA_SQUARE_ROOT800_ContrastRGB_Vertice"
            VERTICE_TYPE "RASTERTRANSFORM"
            VERTICE_CONNECTION "SQUARE_ROOT800_ContrastRGB_Vertice" "INPUT_RASTER"
            VERTICE_PARAM "BANDS_INDEXES" "0;1;2"
            VERTICE_PARAM "OPERATIONS" "NODATA_OP_TYPE"
            VERTICE_PARAM "OLD_NODATA_VALUE" "-10000"
            VERTICE_PARAM "NEW_NODATA_VALUE" "0"
            VERTICE_PARAM "UPDATE_PIXEL_VALUES" "NO"
          VERTICE_END

          VERTICE_START
            VERTICE_NAME "Linear_Contrast800_RGB_Vertice"
            VERTICE_TYPE "CONTRAST"
            VERTICE_CONNECTION "NODATA_SQUARE_ROOT800_ContrastRGB_Vertice" "INPUT_RASTER"
            VERTICE_PARAM "BANDS_INDEXES" "0;1;2"
            VERTICE_PARAM "CONTRAST_TYPE" "LINEAR"
            VERTICE_PARAM "OUT_RANGE_MIN" "1"
            VERTICE_PARAM "OUT_RANGE_MAX" "255"
            VERTICE_PARAM "LC_MIN_INPUT" "1;1;1"
            VERTICE_PARAM "LC_MAX_INPUT" "1023;1023;1023"
          VERTICE_END

        GRAPH_END

        PROJECT_START
          PROJECT_NAME "Proj_1"
          ANALYSIS "{analysis_name}" "Context_Name_1" "Graph_Name_1"
        PROJECT_END
        """
            with open(output_txt_path, 'w', encoding='utf-8') as f:
                f.write(content)

        project_txt = os.path.join(TEMP_DIR, "wpm_1m.txt")
        analysis_name = f"An_WPM_PCA_RGB321_{scene_date_tag}_{tile_tag}"

        batch_file = os.path.join(TEMP_DIR, "wpm_1m.bat")
        log_file = os.path.join(TEMP_DIR, "ClipLog.txt")

        def run_tclt_once(moravec_value):
            generate_tclt_txt(project_txt, preprocessed_paths, ROI_VECTOR_PATH, ROI_VECTOR_PATH,
                               analysis_name, moravec_value)
            log(f"Projeto TCLT gerado em (MORAVEC_MIN_ABS_CORR={moravec_value}):", project_txt)

            bat_content = f"""@echo off
        if exist "{log_file}" del "{log_file}"

        "{TCLT_EXE}" --threads_number={THREADS} --project_file_name="{project_txt}" --output_directory="{TEMP_DIR}" >> "{log_file}"
        set TCLT_ERRORLEVEL=%ERRORLEVEL%

        if %TCLT_ERRORLEVEL% == 0 goto :next
        echo "Erros encontrados durante a execução. Encerrado com status: %TCLT_ERRORLEVEL%"
        exit /b %TCLT_ERRORLEVEL%

        :next
        echo "Concluído"
        exit /b 0
        """
            with open(batch_file, 'w', encoding='utf-8') as f:
                f.write(bat_content)

            _check_cancel()
            log("Executando o TCLT (isso pode demorar um pouco)...")
            result = subprocess.run(batch_file, shell=True, cwd=TEMP_DIR, capture_output=True, text=True)
            log(result.stdout)

            log_text = ""
            if os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                    log_text = f.read()

            return result, log_text

        # O TCLT falhando por "Unable to locate tie-points" normalmente é falta de
        # feições reconhecíveis na área (mata fechada, água, solo homogêneo) - um
        # limiar de correlação (MORAVEC_MIN_ABS_CORR) menos exigente costuma resolver
        # sem comprometer a qualidade do registro nos casos em que ele de fato consegue
        # achar pontos suficientes. Por isso tentamos a lista de valores abaixo, do mais
        # exigente (melhor qualidade) ao mais tolerante, e só desistimos depois de
        # esgotar todas as opções.
        result = None
        log_text = ""
        tie_point_failure = False
        for attempt, moravec_value in enumerate(MORAVEC_RELAX_LADDER, start=1):
            result, log_text = run_tclt_once(moravec_value)
            if result.returncode == 0:
                break
            tie_point_failure = "Unable to locate tie-points" in log_text
            if not tie_point_failure:
                break
            if attempt < len(MORAVEC_RELAX_LADDER):
                log(f"Registro falhou por falta de pontos de controle com MORAVEC_MIN_ABS_CORR={moravec_value}; "
                    f"tentando novamente com um limiar mais tolerante...")

        if result.returncode != 0:
            log("Execução do TCLT falhou. Código de retorno:", result.returncode)
            log("stderr:", result.stderr)
            if log_text:
                log("Conteúdo do log:")
                log(log_text)
            if tie_point_failure:
                raise PipelineError(
                    "Não foi possível registrar as bandas: a área não tem feições suficientes para o "
                    "algoritmo encontrar pontos de controle confiáveis, mesmo após afrouxar o limiar de "
                    "correlação progressivamente. Selecione uma nova ROI ou uma imagem diferente para essa data.")
            raise PipelineError("A execução do TCLT falhou - veja o log acima.")

        log("TCLT finalizado com sucesso (código de saída do processo 0).")

        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8', errors='replace') as lf:
                log_content = lf.read()
            fail_lines = [l for l in log_content.splitlines() if "Status:FAIL" in l]
            if fail_lines:
                log(f"O TCLT reportou {len(fail_lines)} falha(s) de vértice - log completo abaixo:")
                log(log_content)
                raise PipelineError(
                    "O TCLT falhou em um ou mais vértices do grafo (veja o log completo acima - a primeira "
                    "falha é a causa raiz real; as falhas seguintes são apenas consequências em cascata).")

        _check_cancel()
        gc.collect()

        def find_vertice_output(vertice_name):
            for root, dirs, files in os.walk(TEMP_DIR):
                for f in files:
                    if vertice_name.lower() in f.lower() and f.lower().endswith(('.tif', '.tiff')):
                        return os.path.join(root, f)
            return None

        pca_path = find_vertice_output("FUSION_Vertice")
        if pca_path is None:
            raise PipelineError("Não foi possível localizar o raster de fusão (FUSION_Vertice) "
                                 "que o TCLT deveria ter produzido - verifique o log acima.")

        rgb_path = find_vertice_output(f"Linear_Contrast{CONTRAST_STRETCH}_RGB_Vertice")
        if rgb_path is None:
            raise PipelineError(f"Não foi possível localizar o raster RGB final (Linear_Contrast{CONTRAST_STRETCH}_RGB_Vertice) "
                                 "que o TCLT deveria ter produzido - verifique o log acima.")

        with rasterio.open(pca_path) as src:
            pca = src.read().astype('float64')
            pca_nodata = src.nodata if src.nodata is not None else -10000.0
            pca_profile = src.profile.copy()

        invalid = (pca[0] == pca_nodata) | np.isnan(pca[0])
        nrgb = pca.copy()
        nrgb[:, invalid] = 0
        nrgb_int16 = np.clip(np.round(nrgb), -32768, 32767).astype('int16')

        with rasterio.open(rgb_path) as src:
            rgb_data = src.read().astype('float64')
        rgb_uint8 = np.clip(np.round(rgb_data), 0, 255).astype('uint8')
        rgb_uint8[:, invalid] = 0

        with _PROJ_LOCK:
            final_roi_native = gpd.read_file(ROI_VECTOR_PATH).to_crs(pca_profile['crs'])
        final_geoms = [mapping(g) for g in final_roi_native.geometry]

        def crop_to_final_roi(arr, nodata_val):
            mem_profile = pca_profile.copy()
            mem_profile.update(count=arr.shape[0], dtype=arr.dtype, nodata=nodata_val,
                                driver='GTiff', BIGTIFF='YES')
            with MemoryFile() as mf:
                with mf.open(**mem_profile) as ds:
                    ds.write(arr)
                with mf.open() as ds:
                    cropped, cropped_transform = rio_mask(ds, final_geoms, crop=True, filled=True, all_touched=True, nodata=nodata_val)
            out_profile = mem_profile.copy()
            out_profile.update(height=cropped.shape[1], width=cropped.shape[2], transform=cropped_transform)
            return cropped, out_profile

        nrgb_cropped, nrgb_profile = crop_to_final_roi(nrgb_int16, 0)
        rgb_cropped, rgb_profile = crop_to_final_roi(rgb_uint8, 0)

        nrgb_tmp_path = os.path.join(TEMP_DIR, "computed_NRGB.tif")
        nrgb_profile.update(driver='GTiff', BIGTIFF='YES', tiled=True,
                             blockxsize=512, blockysize=512, num_threads='ALL_CPUS')
        with rasterio.open(nrgb_tmp_path, 'w', **nrgb_profile) as dst:
            dst.write(nrgb_cropped)

        rgb_tmp_path = os.path.join(TEMP_DIR, f"computed_RGB_{CONTRAST_STRETCH}.tif")
        rgb_profile.update(driver='GTiff', BIGTIFF='YES', tiled=True,
                            blockxsize=512, blockysize=512, num_threads='ALL_CPUS')
        with rasterio.open(rgb_tmp_path, 'w', **rgb_profile) as dst:
            dst.write(rgb_cropped)

        del pca, nrgb, nrgb_int16, rgb_data, rgb_uint8, nrgb_cropped, rgb_cropped
        gc.collect()

        from rasterio.warp import calculate_default_transform, reproject, Resampling
        from rasterio.crs import CRS as RioCRS

        def to_uint8(data, nodata):
            out_bands = []
            for b in range(data.shape[0]):
                band = data[b].astype('float64')
                valid = (band != nodata) if nodata is not None else np.ones_like(band, dtype=bool)
                if valid.any() and band[valid].min() >= 0 and band[valid].max() <= 255:
                    out = np.clip(np.round(band), 0, 255).astype('uint8')
                else:
                    vmin, vmax = (band[valid].min(), band[valid].max()) if valid.any() else (0.0, 1.0)
                    if vmax > vmin:
                        scaled = (band - vmin) / (vmax - vmin) * 255.0
                    else:
                        scaled = np.zeros_like(band)
                    out = np.clip(np.round(scaled), 0, 255).astype('uint8')
                    log(f"  Nota: a banda {b + 1} foi reescalada linearmente de [{vmin:.2f}, {vmax:.2f}] para [0, 255] para compressão em uint8.")
                if nodata is None:
                    out[~valid] = 0
                out_bands.append(out)
            return np.stack(out_bands)

        def to_int16(data, nodata):
            out_bands = []
            for b in range(data.shape[0]):
                band = data[b].astype('float64')
                valid = (band != nodata) if nodata is not None else np.ones_like(band, dtype=bool)
                out = np.clip(np.round(band), -32768, 32767).astype('int16')
                if nodata is None:
                    out[~valid] = -10000
                out_bands.append(out)
            return np.stack(out_bands)

        def parse_crs(value):
            return RioCRS.from_epsg(value) if isinstance(value, int) else RioCRS.from_string(value)

        def compress_final_product(src_path, dst_path, target_dtype='uint8'):
            with rasterio.open(src_path) as src:
                data = src.read()
                nodata = src.nodata
                arr = to_uint8(data, nodata) if target_dtype == 'uint8' else to_int16(data, nodata)
                profile = src.profile.copy()
                profile.update(dtype=target_dtype, nodata=0, count=arr.shape[0])
                del data
                gc.collect()

                if OUTPUT_CRS:
                    dst_crs = parse_crs(OUTPUT_CRS)
                    src_res_x = abs(profile['transform'].a)
                    src_res_y = abs(profile['transform'].e)
                    transform, width, height = calculate_default_transform(
                        src.crs, dst_crs, profile['width'], profile['height'], *src.bounds,
                        resolution=(src_res_x, src_res_y))
                    reproj_arr = np.zeros((arr.shape[0], height, width), dtype=target_dtype)
                    for b in range(arr.shape[0]):
                        reproject(
                            source=arr[b], destination=reproj_arr[b],
                            src_transform=profile['transform'], src_crs=src.crs,
                            dst_transform=transform, dst_crs=dst_crs,
                            resampling=Resampling.cubic, src_nodata=0, dst_nodata=0)
                    del arr
                    arr = reproj_arr
                    profile.update(crs=dst_crs, transform=transform, width=width, height=height)

                profile.update(tiled=True, blockxsize=256, blockysize=256)

                if OUTPUT_FORMAT.upper() == "JP2":
                    profile['driver'] = 'JP2OpenJPEG'
                    if LOSSLESS:
                        profile.update(REVERSIBLE='YES', QUALITY=100)
                        label = 'JP2OpenJPEG (lossless, REVERSIBLE=YES)'
                    else:
                        profile.update(REVERSIBLE='NO', QUALITY=JP2_QUALITY)
                        label = f'JP2OpenJPEG (lossy, QUALITY={JP2_QUALITY})'
                    try:
                        with rasterio.open(dst_path, 'w', **profile) as dst:
                            dst.write(arr)
                        del arr
                        gc.collect()
                        return label, dst_path
                    except Exception as e:
                        log(f"  Driver JP2OpenJPEG indisponível nesta versão do GDAL ({e}); usando GTiff/ZSTD sem perdas como alternativa.")

                profile['driver'] = 'GTiff'
                for k in ('REVERSIBLE', 'QUALITY'):
                    profile.pop(k, None)
                dst_path = os.path.splitext(dst_path)[0] + '.tif'
                zstd_profile = profile.copy()
                zstd_profile.update(compress='ZSTD', zstd_level=22, predictor=2,
                                     num_threads='ALL_CPUS', BIGTIFF='YES')
                try:
                    with rasterio.open(dst_path, 'w', **zstd_profile) as dst:
                        dst.write(arr)
                    label = 'GTiff + ZSTD (level 22, lossless)'
                except Exception as e:
                    log(f"  Compressão ZSTD indisponível nesta versão do GDAL ({e}); usando DEFLATE nível 9 como alternativa.")
                    deflate_profile = profile.copy()
                    deflate_profile.update(compress='DEFLATE', zlevel=9, predictor=2, BIGTIFF='YES')
                    with rasterio.open(dst_path, 'w', **deflate_profile) as dst:
                        dst.write(arr)
                    label = 'GTiff + DEFLATE (level 9, lossless)'
                del arr
                gc.collect()
                return label, dst_path

        out_ext = '.jp2' if OUTPUT_FORMAT.upper() == "JP2" else '.tif'
        desired_products = []
        if GENERATE_NGB:
            desired_products.append((nrgb_tmp_path, f"{SCENE_ID}_NRGB{out_ext}", 'int16'))
        if GENERATE_RGB:
            desired_products.append((rgb_tmp_path, f"{SCENE_ID}_RGB_{CONTRAST_STRETCH}_8bit{out_ext}", 'uint8'))
        if not desired_products:
            raise PipelineError("Nenhum produto foi selecionado (generate_rgb / generate_ngb estão ambos como False).")

        outputs = []
        for src, outname, dtype in desired_products:
            dst = os.path.join(FINAL_OUTPUT_DIR, outname)
            used, written_path = compress_final_product(src, dst, target_dtype=dtype)
            log(f"Produto final gravado ({used}, dtype={dtype}): {written_path}")
            outputs.append(written_path)

        log(f"{len(outputs)} produto(s) solicitado(s) gravado(s) em:", FINAL_OUTPUT_DIR)

        log("A pasta de saída final agora contém apenas o(s) produto(s) solicitado(s):")
        for f in sorted(os.listdir(FINAL_OUTPUT_DIR)):
            log("  " + f)
        return outputs
    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        gc.collect()
        log("Diretório de trabalho temporário removido: " + TEMP_DIR)
