#!/usr/bin/env python3
"""WPM 1-meter spatial resolution (CBERS-4A) - standalone script for Linux / Brazil Data Cube.

Usage:
    python wpm_bdc.py check      # verify Python libraries, TCLT, resources and network
    python wpm_bdc.py search     # list scenes for the ROI and date range
    python wpm_bdc.py process    # process the selected scenes

Author: Miguel Alexandre da Cunha
"""

# =============================== CONFIGURATION ===============================

TCLT_HOME = "/home/jovyan/tclt"            # folder containing bin/ lib/ share/
TCLT_EXE = None                            # None = TCLT_HOME/bin/tclt_exe
TCLT_RUNTIME = "auto"                      # Ubuntu 24.04 runtime: "auto", a path, or None

OUTPUT_DIR = "/home/jovyan/wpm_output"     # final images only
TEMP_DIR = "/home/jovyan/wpm_tmp"          # work files and thumbnails (cleaned after each date)

# Region of interest: a vector file OR a center point
ROI_FILE = None                            # e.g. "/home/jovyan/rois/pantanal.gpkg"
ROI_LAT = -17.5
ROI_LON = -56.9
ROI_SIZE_KM = 10                           # square side, 10-40 km
PROCESS_ENTIRE_TILE = False                # True = full tile, processed in blocks and mosaicked
BLOCK_SIZE_KM = 20                         # full-tile block size, 10-40 km

DATE_START = "2025-07-01"                  # YYYY-MM-DD; None = 60 days before DATE_END
DATE_END = "2025-08-31"                    # YYYY-MM-DD; None = today

SCENE_IDS = []                             # [] = choose interactively; or list of scene IDs

GENERATE_RGB = True                        # 8-bit RGB
GENERATE_NRGB = False                      # int16 NIR/R/G/B
OUTPUT_FORMAT = "JP2"                      # "JP2" or "TIFF"
LOSSLESS = True                            # JP2 only
JP2_QUALITY = 100                          # JP2 only, when LOSSLESS = False
OUTPUT_CRS = None                          # None = scene CRS; e.g. "EPSG:4674"

# Performance (None = auto-detect from the machine/container limits)
MAX_CPUS = None                            # e.g. 32
MAX_RAM_GB = None                          # e.g. 192
PARALLEL_JOBS = None                       # dates (ROI mode) or blocks (full tile) at the same time

CONTRAST_STRETCH = 800
ROI_BUFFER_KM = 2.0
SAVE_THUMBNAILS = True
SHOW_TCLT_OUTPUT = True
KEEP_TEMP = False
CONTINUE_ON_ERROR = False

DEFAULT_ACTION = "check"                   # used when no action is given

# ==============================================================================

import argparse
import gc
import glob
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from types import SimpleNamespace


# Use this Python's own PROJ/GDAL data even if another conda env is active (e.g. "(base)").
def _use_own_data(names, marker, folder):
    own = os.path.join(sys.prefix, "share", folder)
    if os.path.isfile(os.path.join(own, marker)):
        for name in names:
            if os.environ.get(name) and os.path.normpath(os.environ[name]) != own:
                os.environ[name] = own


_use_own_data(("PROJ_DATA", "PROJ_LIB"), "proj.db", "proj")
_use_own_data(("GDAL_DATA",), "gdalvrt.xsd", "gdal")


# -------------------------------------------------------------------- resources

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def detect_cpus():
    """CPUs this process may use (affinity and cgroup quota aware, e.g. a 32-vCPU pod on a big host)."""
    n = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    quotas = []
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2 and not v2.startswith("max"):
        quotas.append(v2.split()[:2])
    v1 = (_read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    if v1[0] and v1[1] and int(v1[0]) > 0:
        quotas.append(v1)
    for quota, period in quotas:
        n = min(n, max(1, math.ceil(int(quota) / int(period))))
    if MAX_CPUS:
        n = min(n, int(MAX_CPUS))
    return max(1, n)


def detect_ram_gb():
    """Available RAM in GB (cgroup memory limit aware)."""
    avail = None
    meminfo = _read("/proc/meminfo") or ""
    m = re.search(r"MemAvailable:\s+(\d+) kB", meminfo)
    if m:
        avail = int(m.group(1)) * 1024
    limit, usage = _read("/sys/fs/cgroup/memory.max"), _read("/sys/fs/cgroup/memory.current")
    if not limit:
        limit, usage = (_read("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
                        _read("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    if limit and limit.isdigit() and usage and int(limit) < 1 << 60:
        stat = _read("/sys/fs/cgroup/memory.stat") or _read("/sys/fs/cgroup/memory/memory.stat") or ""
        cache = re.search(r"^(?:total_)?inactive_file (\d+)", stat, re.M)
        used = int(usage) - (int(cache.group(1)) if cache else 0)
        free = int(limit) - used
        avail = free if avail is None else min(avail, free)
    gb = (avail or 8 * 1024 ** 3) / 1024 ** 3
    if MAX_RAM_GB:
        gb = min(gb, float(MAX_RAM_GB))
    return gb


# GDAL settings for fast remote (COG) reads and a shared block cache. Set before rasterio loads;
# not passed on to TCLT. Directory listing is disabled only around remote reads (see REMOTE_ENV).
_GDAL_KEYS_SET = []
for _key, _value in {
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF,.TIFF",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": str(64 * 1024 * 1024),
    "GDAL_CACHEMAX": str(int(min(8192, max(512, detect_ram_gb() * 1024 * 0.05)))),
}.items():
    if _key not in os.environ:
        os.environ[_key] = _value
        _GDAL_KEYS_SET.append(_key)
REMOTE_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"}

MISSING_LIBS = []
try:
    import numpy as np
except ImportError:
    MISSING_LIBS.append("numpy")
try:
    import requests
except ImportError:
    MISSING_LIBS.append("requests")
try:
    import geopandas as gpd
except ImportError:
    MISSING_LIBS.append("geopandas")
try:
    import rasterio
    import rasterio.shutil
    from rasterio import Affine
    from rasterio.crs import CRS
    from rasterio.features import geometry_mask
    from rasterio.fill import fillnodata
    from rasterio.io import MemoryFile
    from rasterio.mask import mask as rio_mask, raster_geometry_mask
    from rasterio.merge import merge as rio_merge
    from rasterio.transform import array_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    from rasterio.windows import Window, transform as window_transform
except ImportError:
    MISSING_LIBS.append("rasterio")
try:
    from shapely.geometry import Polygon, box, mapping, shape
    from shapely.ops import unary_union
except ImportError:
    MISSING_LIBS.append("shapely")

STAC_URL = "https://data.inpe.br/bdc/stac/v1/"
COLLECTION = "CB4A-WPM-L4-DN-1"
RUNTIME_NAME = "tclt_runtime_ubuntu2404"
MORAVEC_LADDER = [0.25, 0.15, 0.10, 0.06]   # tie-point correlation thresholds, strict -> tolerant
MIN_COVERAGE = 0.98
ROI_SIZE_RANGE_KM = (10, 40)
PAN_PIXEL_M2 = 4.0                           # BAND0 (pan) is 2 m
RAM_BYTES_PER_PAN_PIXEL = 24                 # peak RAM of band preparation per pan pixel
DISK_BYTES_PER_PAN_PIXEL = 4 * 5 * 25        # TCLT temporary files (conservative)
THREADS_PER_DATE_MIN = 8
EXPORT_BLOCK_ROWS = 1024
PCA_DIRECT_MATRIX = ("0.56744643403671413;0.51869586796396927;0.45771024318085091;0.4466099801207401;"
                     "-0.81958938283057925;0.33064924537518092;0.26527989286495773;0.38544895709519045;"
                     "0.078342475037743664;-0.40052355311853272;-0.42936005778122865;0.80566325520304494;"
                     "-0.011842493629609555;-0.67912190105108083;0.73196847781238716;0.05362225607353481")


class PipelineError(Exception):
    pass


class Cancelled(Exception):
    pass


# ------------------------------------------------------- logging & run control

_TLS = threading.local()                # per-thread log tag (the date being processed)
_PRINT_LOCK = threading.Lock()
_GEO_LOCK = threading.Lock()            # geopandas/pyproj calls from worker threads
CANCEL = threading.Event()
_PROCS = set()                          # running TCLT processes
_WORK_DIRS = set()                      # temp dirs to clean on interrupt


def log(*args):
    stamp = datetime.now().strftime("%H:%M:%S")
    tag = getattr(_TLS, "tag", None)
    prefix = "[{}]{} ".format(stamp, " [{}]".format(tag) if tag else "")
    with _PRINT_LOCK:
        for line in " ".join(str(a) for a in args).splitlines() or [""]:
            print(prefix + line, flush=True)


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled("Cancelled.")


def stop_everything():
    """Stop all running TCLT processes (whole process group, so helper processes stop too)."""
    import signal
    CANCEL.set()
    for proc in list(_PROCS):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass


def full_path(path):
    return os.path.abspath(os.path.expanduser(path))


def output_dir():
    path = full_path(OUTPUT_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def temp_root():
    path = full_path(TEMP_DIR or os.path.join(tempfile.gettempdir(), "wpm_tmp"))
    os.makedirs(path, exist_ok=True)
    return path


# ------------------------------------------------------------------------ TCLT

def find_tclt_homes(roots, max_depth=4):
    """Folders that contain bin/tclt_exe* and lib/."""
    found = []
    for root in map(full_path, roots):
        if not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("share", "include", "_tmp")]
            if dirpath.count(os.sep) - base >= max_depth:
                dirnames[:] = []
            if os.path.isdir(os.path.join(dirpath, "lib")) and glob.glob(os.path.join(dirpath, "bin", "tclt_exe*")):
                if dirpath not in found:
                    found.append(dirpath)
                dirnames[:] = []
    return found


def fix_zip_symlinks(folder):
    """Unzipping can turn symlinks into tiny text files holding the target name; restore them."""
    fixed = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 255:
            continue
        try:
            with open(path, encoding="ascii") as f:
                target = f.read().strip()
        except (UnicodeDecodeError, OSError):
            continue
        if target and target != name and "/" not in target and os.path.isfile(os.path.join(folder, target)):
            os.remove(path)
            os.symlink(target, path)
            fixed.append("{} -> {}".format(name, target))
    return fixed


def locate_tclt():
    """Return (exe, home)."""
    home = full_path(TCLT_HOME)
    if not (os.path.isdir(os.path.join(home, "bin")) and os.path.isdir(os.path.join(home, "lib"))):
        found = find_tclt_homes([home, "~"])
        if len(found) != 1:
            raise PipelineError("TCLT not found at {}. Set TCLT_HOME to the full path of the folder with bin/ and lib/.{}"
                                .format(home, " Candidates: " + ", ".join(found) if found else ""))
        log("  TCLT_HOME has no bin/ and lib/; using {} (set TCLT_HOME to it).".format(found[0]))
        home = found[0]

    for sub in ("bin", "lib"):
        for item in fix_zip_symlinks(os.path.join(home, sub)):
            log("  Symlink restored: {}/{}".format(sub, item))

    exe = full_path(TCLT_EXE) if TCLT_EXE else os.path.join(home, "bin", "tclt_exe")
    if not os.path.isfile(exe):
        candidates = sorted(c for c in glob.glob(os.path.join(home, "bin", "tclt_exe*")) if os.path.isfile(c))
        if not candidates:
            raise PipelineError("TCLT executable not found in {}/bin".format(home))
        exe = candidates[-1]

    real = os.path.realpath(exe)
    if not os.access(real, os.X_OK):
        try:
            os.chmod(real, os.stat(real).st_mode | 0o755)
        except OSError as exc:
            log("  Warning: cannot make {} executable: {}".format(real, exc))
    return exe, home


def runtime_loader(runtime):
    return os.path.join(runtime, "lib", "ld-linux-x86-64.so.2")


def extract_runtime(archive):
    log("  Extracting {} (first run only)...".format(archive))
    dest = os.path.dirname(archive)
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="tar")
        except TypeError:
            tf.extractall(dest)
    runtime = os.path.join(dest, RUNTIME_NAME)
    os.chmod(runtime_loader(runtime), 0o755)
    return runtime


def locate_runtime(home):
    """Ubuntu 24.04 runtime folder (glibc 2.39, Boost 1.83, GDAL 3.8...) or None."""
    if not TCLT_RUNTIME:
        return None
    if str(TCLT_RUNTIME).lower() != "auto":
        path = full_path(TCLT_RUNTIME)
        if os.path.isfile(path):
            folder = os.path.join(os.path.dirname(path), RUNTIME_NAME)
            path = folder if os.path.isfile(runtime_loader(folder)) else extract_runtime(path)
        if not os.path.isfile(runtime_loader(path)):
            raise PipelineError("TCLT_RUNTIME is not a valid runtime folder: {}".format(path))
        return path

    bases = [os.path.expanduser("~"), os.path.dirname(home), home, os.getcwd()]
    if "__file__" in globals():
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    bases = list(dict.fromkeys(bases))
    for base in bases:
        folder = os.path.join(base, RUNTIME_NAME)
        if os.path.isfile(runtime_loader(folder)):
            return folder
    for base in bases:
        for ext in (".tar.xz", ".tar.gz", ".tgz"):
            archive = os.path.join(base, RUNTIME_NAME + ext)
            if os.path.isfile(archive):
                return extract_runtime(archive)
    return None


def tclt_command(tclt, target, args, list_deps=False):
    """Return (cmd, env). With the runtime, TCLT starts through the runtime's loader and
    uses only TCLT's own libraries plus the runtime's (nothing from the host system)."""
    env = {k: v for k, v in os.environ.items() if k not in _GDAL_KEYS_SET}
    env["TERRALIB_HOME"] = tclt.home
    if tclt.runtime:
        rt = tclt.runtime
        env.pop("LD_LIBRARY_PATH", None)
        env.pop("LD_PRELOAD", None)
        env.update(GDAL_DATA=os.path.join(rt, "share", "gdal"),
                   PROJ_DATA=os.path.join(rt, "share", "proj"),
                   PROJ_LIB=os.path.join(rt, "share", "proj"),
                   GCONV_PATH=os.path.join(rt, "lib", "gconv"),
                   LC_ALL="C.UTF-8")
        lib_path = os.pathsep.join([os.path.join(tclt.home, "lib"), os.path.join(rt, "lib")])
        cmd = [runtime_loader(rt), "--library-path", lib_path] + (["--list"] if list_deps else [])
        return cmd + [os.path.realpath(target)] + list(args), env

    env["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in [os.path.join(tclt.home, "lib"), env.get("LD_LIBRARY_PATH")] if p)
    proj = os.path.join(tclt.home, "share", "proj")
    if os.path.isdir(proj):
        env["PROJ_DATA"] = env["PROJ_LIB"] = proj
    return (["ldd", target] if list_deps else [target] + list(args)), env


def get_tclt():
    exe, home = locate_tclt()
    return SimpleNamespace(exe=exe, home=home, runtime=locate_runtime(home))


def run_tclt(tclt, project_file, work_dir, log_file, threads):
    """Run TCLT directly (no shell, no .bat). Returns (exit_code, output_text)."""
    cmd, env = tclt_command(tclt, tclt.exe, ["--threads_number={}".format(int(threads)),
                                             "--project_file_name={}".format(project_file),
                                             "--output_directory={}".format(work_dir)])
    check_cancel()
    proc = subprocess.Popen(cmd, cwd=work_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            start_new_session=True)
    _PROCS.add(proc)
    if CANCEL.is_set():                 # stop requested while this process was starting
        stop_everything()
    try:
        with open(log_file, "w", encoding="utf-8") as lf:
            for line in proc.stdout:
                lf.write(line)
                if SHOW_TCLT_OUTPUT and line.strip():
                    log("    [TCLT] " + line.rstrip("\n"))
        proc.wait()
    finally:
        _PROCS.discard(proc)
    check_cancel()
    with open(log_file, encoding="utf-8", errors="replace") as f:
        return proc.returncode, f.read()


# ------------------------------------------------------------------- TCLT graph

def _vertex(name, vtype, resources=(), connections=(), params=(), extra=()):
    lines = ["  VERTICE_START", '    VERTICE_NAME "{}"'.format(name), '    VERTICE_TYPE "{}"'.format(vtype)]
    lines += ['    VERTICE_RESOURCE "{}" "{}"'.format(a, b) for a, b in resources]
    lines += ['    VERTICE_CONNECTION "{}" "{}"'.format(a, b) for a, b in connections]
    lines += ['    VERTICE_PARAM "{}" "{}"'.format(k, v) for k, v in params]
    lines += ["    " + e for e in extra]
    return "\n".join(lines + ["  VERTICE_END"])


def write_tclt_project(path, bands, roi_path, analysis, moravec):
    """TCLT graph: clip -> restoration -> tie points + registration -> PCA fusion -> contrast."""
    cs = CONTRAST_STRETCH
    uri = lambda p: "file://" + p
    sensors = {4: ("CBERS_4_MUX", 8), 3: ("CBERS_4_MUX", 7), 2: ("CBERS_4_MUX", 6),
               1: ("CBERS_4_MUX", 5), 0: ("CBERS2B_HRC", 1)}
    tie_point_params = [
        ("EXEC_MODE", 1), ("TP_LOC_ALGO", "MORAVEC"), ("BAND_1", 0), ("BAND_2", 0), ("MAX_TIE_POINTS", 1000),
        ("PIXEL_SIZE_RELATION", 0), ("TRANS_NAME", "Affine"), ("MAX_ERROR", 10), ("MIN_TP_AREA_PERCENT", 5),
        ("MIN_TP_NUMBER_FACTOR", 5), ("ENABLE_SUBSAMPLE_OPT", "YES"), ("MORAVEC_CORR_WINDOW_W", 21),
        ("MORAVEC_WINDOW_W", 21), ("MORAVEC_FILTER_IT", 1), ("MORAVEC_MIN_ABS_CORR", moravec),
        ("ENABLE_AUTO_REPROJ", "YES")]
    register_params = [
        ("OUT_SRID", 0), ("OUT_RES_X", 2), ("OUT_RES_Y", 2), ("INTERP_METHOD", "BILINEAR_INTERP"),
        ("OUT_NO_DATA_VALUE", -10000), ("TRANS_NAME", "Affine"), ("TP_REF_SRID_PROP_NAME", "SRID_1"),
        ("TP_REF_X_PROP_NAME", "X_1"), ("TP_REF_Y_PROP_NAME", "Y_1"), ("TP_ADJ_SRID_PROP_NAME", "SRID_2"),
        ("TP_ADJ_X_PROP_NAME", "X_2"), ("TP_ADJ_Y_PROP_NAME", "Y_2")]
    order = [4, 3, 2, 1, 0]

    v = [_vertex("POLYGONS_vertice", "URI", resources=[("vector resource 2", "URI_ALIAS")],
                 params=[("OUTPUT_TYPE", "VECTOR_URI_IO_TYPE")]),
         _vertex("POLYGONS_FINAL_vertice", "URI", resources=[("vector resource 3", "URI_ALIAS")],
                 params=[("OUTPUT_TYPE", "VECTOR_URI_IO_TYPE")])]
    v += [_vertex("RASTER_B_{}ToClip_vertice".format(b), "URI", resources=[("raster resource B_{}".format(b), "URI_ALIAS")],
                  params=[("OUTPUT_TYPE", "RASTER_URI_IO_TYPE")]) for b in order]
    v += [_vertex("Clipped_by_polygons_imageB_{}_vertice".format(b), "RASTERCLIPPING",
                  connections=[("RASTER_B_{}ToClip_vertice".format(b), "RASTER"), ("POLYGONS_vertice", "POLYGONS")])
          for b in order]
    v += [_vertex("restore_B{}_vertice".format(b), "RASTER_RESTORATION",
                  connections=[("Clipped_by_polygons_imageB_{}_vertice".format(b), "INPUT_RASTER")],
                  params=[("SENSOR_TYPE", sensors[b][0]), ("SENSOR_BAND_DESIGNATIONS", sensors[b][1]),
                          ("RASTER_BANDS", 0), ("SAMPLING_FACTOR", "SAMPLING_FACTOR_1_BY_2")]) for b in order]

    def tie_points(b):
        return _vertex("TiePointsB{}_Vertice".format(b), "TPLOCATOR",
                       connections=[("restore_B0_vertice", "INPUT_1"), ("restore_B{}_vertice".format(b), "INPUT_2")],
                       params=tie_point_params)

    def register(b, tp):
        return _vertex("RegisterB{}_UsingAdjXY_Vertice".format(b), "REGISTER",
                       connections=[("restore_B{}_vertice".format(b), "INPUT_RASTER"),
                                    ("TiePointsB{}_Vertice".format(tp), "INPUT_TIE_POINTS")],
                       params=register_params)

    v += [tie_points(4), register(4, 4), register(3, 4), tie_points(2), register(2, 2), register(1, 4)]
    v += [
        _vertex("Recomposed_registeredimage_vertice", "RECOMPOSE",
                connections=[("RegisterB{}_UsingAdjXY_Vertice".format(b), "INPUT_RASTER_{}".format(i))
                             for i, b in enumerate([4, 3, 2, 1], start=1)],
                params=[("BANDS_STRING", "INPUT_RASTER_1:0 INPUT_RASTER_2:0 INPUT_RASTER_3:0 INPUT_RASTER_4:0"),
                        ("VIRTUAL", "NO"), ("ALLOW_NO_DATA_PIXELS", "NO")]),
        _vertex("PanT2_vertice", "ARITHMETIC", connections=[("restore_B0_vertice", "INPUT_RASTER_1")],
                params=[("ARITHMETIC_STRING", "( INPUT_RASTER_1:0 * 2.0 )"), ("NORMALIZE_OUTPUT", "NO"),
                        ("INTERP_METHOD", "NN_INTERP"), ("OUT_RST_DATA_TYPE", "FLOAT_TYPE")]),
        _vertex("FUSION_Vertice", "FUSION",
                connections=[("PanT2_vertice", "HR_RASTER"), ("Recomposed_registeredimage_vertice", "LR_RASTER")],
                params=[("FUSION_METHOD", "PCA"), ("INTERP_METHOD", "BILINEAR_INTERP"), ("AUTO_ALIGN_AND_CLIP", "NO"),
                        ("PCA_HISTO_FIT_METHOD", "NO_FIT"), ("LR_BANDS_INDEXES", "0;1;2;3"), ("HR_BAND_IDX", 0),
                        ("OUT_NODATA_VALUE", -10000), ("PCA_DIRECT_MATRIX", PCA_DIRECT_MATRIX),
                        ("PCA_OUT_MIN_VALUE", 1), ("PCA_OUT_MAX_VALUE", 1023)],
                extra=['VERTICE_OUT_CACHE "YES"']),
        _vertex("NODATA_NRGB_Image_VERTICE", "RASTERTRANSFORM", connections=[("FUSION_Vertice", "INPUT_RASTER")],
                params=[("BANDS_INDEXES", "0;1;2;3"), ("OPERATIONS", "NODATA_OP_TYPE"), ("OLD_NODATA_VALUE", 0),
                        ("NEW_NODATA_VALUE", -10000), ("UPDATE_PIXEL_VALUES", "NO")]),
        _vertex("Recomposed_RGBimage_vertice", "RECOMPOSE", connections=[("NODATA_NRGB_Image_VERTICE", "INPUT_RASTER_1")],
                params=[("BANDS_STRING", "INPUT_RASTER_1:1 INPUT_RASTER_1:2 INPUT_RASTER_1:3"), ("VIRTUAL", "NO"),
                        ("ALLOW_NO_DATA_PIXELS", "NO")]),
        _vertex("SQUARE_ROOT{}_ContrastRGB_Vertice".format(cs), "CONTRAST",
                connections=[("Recomposed_RGBimage_vertice", "INPUT_RASTER")],
                params=[("BANDS_INDEXES", "0;1;2"), ("CONTRAST_TYPE", "SQUARE_ROOT"), ("OUT_RANGE_MIN", 1),
                        ("OUT_RANGE_MAX", 1023), ("SQUARE_R_MIN_INPUT", "0;0;0"),
                        ("SQUARE_R_MAX_INPUT", "{0};{0};{0}".format(cs))]),
        _vertex("NODATA_SQUARE_ROOT{}_ContrastRGB_Vertice".format(cs), "RASTERTRANSFORM",
                connections=[("SQUARE_ROOT{}_ContrastRGB_Vertice".format(cs), "INPUT_RASTER")],
                params=[("BANDS_INDEXES", "0;1;2"), ("OPERATIONS", "NODATA_OP_TYPE"), ("OLD_NODATA_VALUE", -10000),
                        ("NEW_NODATA_VALUE", 0), ("UPDATE_PIXEL_VALUES", "NO")]),
        _vertex("Linear_Contrast{}_RGB_Vertice".format(cs), "CONTRAST",
                connections=[("NODATA_SQUARE_ROOT{}_ContrastRGB_Vertice".format(cs), "INPUT_RASTER")],
                params=[("BANDS_INDEXES", "0;1;2"), ("CONTRAST_TYPE", "LINEAR"), ("OUT_RANGE_MIN", 1),
                        ("OUT_RANGE_MAX", 255), ("LC_MIN_INPUT", "1;1;1"), ("LC_MAX_INPUT", "1023;1023;1023")]),
    ]

    context = ["CONTEXT_START", '  CONTEXT_NAME "Context_Name_1"']
    context += ['  RESOURCE_URI "raster resource B_{}" "{}"'.format(b, uri(bands[b])) for b in range(5)]
    context += ['  RESOURCE_URI "vector resource 2" "{}"'.format(uri(roi_path)),
                '  RESOURCE_URI "vector resource 3" "{}"'.format(uri(roi_path)), "CONTEXT_END"]
    text = "\n".join(context) + "\n\nGRAPH_START\n  GRAPH_NAME \"Graph_Name_1\"\n\n" + "\n\n".join(v)
    text += '\n\nGRAPH_END\n\nPROJECT_START\n  PROJECT_NAME "Proj_1"\n'
    text += '  ANALYSIS "{}" "Context_Name_1" "Graph_Name_1"\nPROJECT_END\n'.format(analysis)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------------ ROI & STAC

def bbox_around(lat, lon, side_km):
    dlat = side_km / 2 / 111.0
    dlon = side_km / 2 / (111.0 * math.cos(math.radians(lat)))
    return [(lon - dlon, lat - dlat), (lon + dlon, lat - dlat), (lon + dlon, lat + dlat),
            (lon - dlon, lat + dlat), (lon - dlon, lat - dlat)]


def load_roi(work_dir, quiet=False):
    """ROI expanded by ROI_BUFFER_KM as a bounding box. Returns (GeoDataFrame, gpkg_path, area_m2)."""
    roi_file = globals().get("ROI_FILE")
    with _GEO_LOCK:
        if roi_file:
            path = full_path(roi_file)
            if not os.path.isfile(path):
                raise PipelineError("ROI_FILE not found: {}".format(path))
            roi = gpd.read_file(path)
            if roi.crs is None:
                raise PipelineError("ROI_FILE has no CRS.")
            label = path
        else:
            side = min(max(float(ROI_SIZE_KM), ROI_SIZE_RANGE_KM[0]), ROI_SIZE_RANGE_KM[1])
            roi = gpd.GeoDataFrame({"id": [1]}, geometry=[Polygon(bbox_around(float(ROI_LAT), float(ROI_LON), side))], crs=4326)
            label = "{:g} km square at lat {}, lon {}".format(side, ROI_LAT, ROI_LON)

        roi = roi[roi.geometry.notnull() & roi.geometry.is_valid]
        if roi.empty:
            raise PipelineError("ROI has no valid geometry.")
        try:
            utm = roi.estimate_utm_crs()
        except Exception:
            utm = roi.to_crs(4326).estimate_utm_crs()
        x0, y0, x1, y1 = roi.to_crs(utm).total_bounds
        pad = min(max(float(ROI_BUFFER_KM), 0.002), 10.0) * 1000.0
        area = (x1 - x0 + 2 * pad) * (y1 - y0 + 2 * pad)
        roi = gpd.GeoDataFrame({"id": [1]}, geometry=[box(x0 - pad, y0 - pad, x1 + pad, y1 + pad)], crs=utm).to_crs(roi.crs)
        path = os.path.join(work_dir, "roi.gpkg")
        roi.to_file(path, driver="GPKG")
    if not quiet:
        log("ROI: {}".format(label))
    return roi, path, area


def date_range():
    end = DATE_END or datetime.now().strftime("%Y-%m-%d")
    start = DATE_START or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        if datetime.strptime(end, "%Y-%m-%d") < datetime.strptime(start, "%Y-%m-%d"):
            raise PipelineError("DATE_END is before DATE_START.")
    except ValueError:
        raise PipelineError("Dates must be YYYY-MM-DD (got {} / {}).".format(start, end))
    return start, end


def tile_of(item):
    """PATH_ROW, e.g. CBERS_4A_WPM_20250712_207_133_L4 -> 207_133."""
    props = item.get("properties") or {}
    if props.get("path") is not None and props.get("row") is not None:
        try:
            return "{:03d}_{:03d}".format(int(props["path"]), int(props["row"]))
        except (TypeError, ValueError):
            pass
    m = re.search(r"(?<!\d)(\d{3}_\d{3})(?!\d)", item["id"])
    return m.group(1) if m else None


def date_of(item):
    props = item.get("properties") or {}
    value = props.get("datetime") or props.get("start_datetime")
    return value[:10] if value else None


def thumbnail_of(item):
    assets = item.get("assets") or {}
    for key in ("thumbnail", "browse", "preview"):
        if (assets.get(key) or {}).get("href"):
            return assets[key]["href"]
    for key, asset in assets.items():
        href = (asset or {}).get("href", "")
        if "thumbnail" in ((asset or {}).get("roles") or []) or "thumbnail" in key.lower() or href.lower().endswith(".png"):
            return href
    return None


def band_href(item, band):
    assets = item.get("assets") or {}
    for key in ("BAND{}".format(band), "band{}".format(band), "Band{}".format(band)):
        if (assets.get(key) or {}).get("href"):
            return assets[key]["href"]
    raise PipelineError("Asset BAND{} missing in {}".format(band, item.get("id")))


def vsi(href):
    return "/vsicurl/" + href if href.startswith(("http://", "https://")) else href


def search_scenes():
    start, end = date_range()
    work = tempfile.mkdtemp(prefix="search_", dir=temp_root())
    try:
        roi, _, _ = load_roi(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    roi84 = roi.to_crs(4326)
    area = unary_union(list(roi84.geometry))
    minx, miny, maxx, maxy = roi84.total_bounds

    log("Searching {} from {} to {}...".format(COLLECTION, start, end))
    url = STAC_URL.rstrip("/") + "/search"
    params = {"collections": COLLECTION, "datetime": "{}T00:00:00Z/{}T23:59:59Z".format(start, end),
              "bbox": "{},{},{},{}".format(minx, miny, maxx, maxy), "limit": 100}
    features = []
    for _ in range(50):
        resp = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        features += data.get("features", [])
        nxt = next((l for l in data.get("links", []) if l.get("rel") == "next" and l.get("method", "GET") == "GET"), None)
        if not nxt or not data.get("features"):
            break
        url, params = nxt["href"], None

    t0 = datetime.strptime(start, "%Y-%m-%d")
    scenes = []
    for it in features:
        day = date_of(it)
        if not day or not shape(it["geometry"]).intersects(area):
            continue
        props = it.get("properties") or {}
        cloud = next((props[k] for k in ("eo:cloud_cover", "cloud_cover") if props.get(k) is not None), None)
        scenes.append({"id": it["id"], "tile": tile_of(it), "date": day, "cloud": cloud,
                       "days": (datetime.strptime(day, "%Y-%m-%d") - t0).days,
                       "thumbnail": thumbnail_of(it), "item": it})
    scenes.sort(key=lambda s: (s["date"], s["tile"] or ""))
    log("{} scene(s) intersect the ROI.".format(len(scenes)))
    return scenes


def sun_elevation(item):
    """Sun elevation from the scene XML metadata (90 if unavailable)."""
    for key, asset in (item.get("assets") or {}).items():
        if (key.lower().endswith("xml") or key.lower() == "metadata") and (asset or {}).get("href"):
            try:
                r = requests.get(asset["href"], timeout=60)
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.text)
                image = next((c for c in root if "image" in c.tag and "imageMode" not in c.tag), None)
                for child in (image if image is not None else []):
                    if "sunPosition" in child.tag:
                        return float(child[0].text)
            except (requests.RequestException, ET.ParseError, ValueError, IndexError):
                pass
    return 90.0


# -------------------------------------------------------------------- pipeline

def crop_band(href, roi):
    with rasterio.Env(**REMOTE_ENV), rasterio.open(vsi(href)) as src:
        nodata = src.nodata if src.nodata is not None else 0
        with _GEO_LOCK:
            geoms = [mapping(g) for g in roi.to_crs(src.crs).geometry]
        data, transform = rio_mask(src, geoms, crop=True, filled=False, all_touched=True)
        profile = src.profile.copy()
    profile.update(height=data.shape[1], width=data.shape[2], transform=transform, nodata=nodata)
    return data, profile


def merge_crops(crops):
    """Mosaic crops from several tiles (reprojecting to the first tile's CRS if needed)."""
    def filled(arr, nodata):
        return arr.filled(nodata) if np.ma.isMaskedArray(arr) else np.asarray(arr)

    if len(crops) == 1:
        arr, profile = crops[0]
        return filled(arr, profile["nodata"]), profile

    ref_crs = crops[0][1]["crs"]
    mem_files, datasets = [], []
    try:
        for arr, profile in crops:
            data = filled(arr, profile["nodata"])
            if profile["crs"] != ref_crs:
                transform, w, h = calculate_default_transform(
                    profile["crs"], ref_crs, profile["width"], profile["height"],
                    *array_bounds(profile["height"], profile["width"], profile["transform"]))
                out = np.zeros((data.shape[0], h, w), dtype=data.dtype)
                reproject(source=data, destination=out, src_transform=profile["transform"], src_crs=profile["crs"],
                          dst_transform=transform, dst_crs=ref_crs, src_nodata=profile["nodata"],
                          dst_nodata=profile["nodata"], resampling=Resampling.cubic)
                data, profile = out, dict(profile, crs=ref_crs, transform=transform, width=w, height=h)
            mf = MemoryFile()
            with mf.open(**dict(profile, driver="GTiff", BIGTIFF="YES")) as ds:
                ds.write(data)
            mem_files.append(mf)
            datasets.append(mf.open())
        merged, transform = rio_merge(datasets, nodata=datasets[0].nodata)
        profile = datasets[0].profile.copy()
        profile.update(height=merged.shape[1], width=merged.shape[2], transform=transform)
        return merged, profile
    finally:
        for ds in datasets:
            ds.close()
        for mf in mem_files:
            mf.close()


def finish_band(band, crops, work_dir, scene_id, cos_az, threads):
    """Mosaic, fill 1-pixel nodata gaps, sun-elevation correction, 0.75-pixel shift, write GeoTIFF."""
    check_cancel()
    data, profile = merge_crops(crops)
    nodata = profile["nodata"]
    valid = (data[0] != nodata).astype("uint8") * 255
    out = fillnodata(data[0].astype("float32"), mask=valid, max_search_distance=1, smoothing_iterations=0)
    del data, valid
    for r in range(0, out.shape[0], 2048):      # float64 division in row chunks: exact, low RAM
        out[r:r + 2048] = (out[r:r + 2048] / np.float64(cos_az)).astype("float32")
    t = profile["transform"]
    profile.update(count=1, dtype="float32", driver="GTiff", compress="zstd", zstd_level=1, predictor=3,
                   tiled=True, blockxsize=512, blockysize=512, num_threads=max(1, threads), BIGTIFF="YES",
                   nodata=nodata, transform=Affine(t.a, t.b, t.c - t.a * 0.75, t.d, t.e, t.f + t.a * 0.75))
    path = os.path.join(work_dir, "{}_BAND{}.tif".format(scene_id, band))
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(out, 1)
    del out
    gc.collect()
    log("  BAND{} ready".format(band))
    return path


def prepare_bands(by_tile, roi, work_dir, scene_id, cos_az, threads):
    """Read the 5 bands of every tile in parallel (network I/O), then finish each band in its own thread."""
    tiles = sorted(by_tile)
    keys = [(b, t) for b in range(5) for t in tiles]
    with ThreadPoolExecutor(min(len(keys), max(4, threads))) as ex:
        futures = {k: ex.submit(crop_band, band_href(by_tile[k[1]], k[0]), roi) for k in keys}
        crops = {k: f.result() for k, f in futures.items()}
    check_cancel()
    log("  Bands downloaded ({} reads)".format(len(keys)))
    with ThreadPoolExecutor(5) as ex:
        futures = [ex.submit(finish_band, b, [crops.pop((b, t)) for t in tiles], work_dir, scene_id, cos_az, threads)
                   for b in range(5)]
        return [f.result() for f in futures]


def find_output(work_dir, vertex):
    for root, _, files in os.walk(work_dir):
        for f in files:
            if vertex.lower() in f.lower() and f.lower().endswith((".tif", ".tiff")):
                return os.path.join(root, f)
    raise PipelineError("TCLT output '{}' not found.".format(vertex))


def build_products(work_dir, roi_path, threads):
    """Crop the TCLT outputs to the ROI block by block (low RAM, parallel) into temporary GeoTIFFs.
    Pixels flagged invalid by the fusion (nodata/NaN) or outside the ROI become 0."""
    fusion_path = find_output(work_dir, "FUSION_Vertice")
    rgb_path = find_output(work_dir, "Linear_Contrast{}_RGB_Vertice".format(CONTRAST_STRETCH)) if GENERATE_RGB else None
    with rasterio.open(fusion_path) as src:
        with _GEO_LOCK:
            geoms = [mapping(g) for g in gpd.read_file(roi_path).to_crs(src.crs).geometry]
        _, out_transform, window = raster_geometry_mask(src, geoms, all_touched=True, crop=True)
        fusion_nodata = src.nodata if src.nodata is not None else -10000.0
        crs = src.crs
    width, height = int(window.width), int(window.height)
    base = dict(driver="GTiff", width=width, height=height, crs=crs, transform=out_transform, nodata=0, tiled=True,
                blockxsize=512, blockysize=512, compress="zstd", zstd_level=1, BIGTIFF="YES", num_threads=max(1, threads))
    targets = {}
    if GENERATE_NRGB:
        targets["NRGB"] = (os.path.join(work_dir, "product_NRGB.tif"), dict(base, count=4, dtype="int16"))
    if GENERATE_RGB:
        targets["RGB"] = (os.path.join(work_dir, "product_RGB.tif"), dict(base, count=3, dtype="uint8"))

    def compute(row):
        check_cancel()
        rows = min(EXPORT_BLOCK_ROWS, height - row)
        win = Window(window.col_off, window.row_off + row, width, rows)
        inside = geometry_mask(geoms, out_shape=(rows, width), all_touched=True, invert=True,
                               transform=window_transform(Window(0, row, width, rows), out_transform))
        with rasterio.open(fusion_path) as src:
            fusion = src.read(None if GENERATE_NRGB else 1, window=win, out_dtype="float32")
        band0 = fusion[0] if fusion.ndim == 3 else fusion
        drop = (band0 == fusion_nodata) | np.isnan(band0) | ~inside
        result = {}
        if GENERATE_NRGB:
            nrgb = np.clip(np.round(fusion), -32768, 32767).astype("int16")
            nrgb[:, drop] = 0
            result["NRGB"] = nrgb
        del fusion, band0
        if GENERATE_RGB:
            with rasterio.open(rgb_path) as src:
                rgb = src.read(window=win, out_dtype="float32")
            rgb = np.clip(np.round(rgb), 0, 255).astype("uint8")
            rgb[:, drop] = 0
            result["RGB"] = rgb
        return row, rows, result

    outs = {k: rasterio.open(path, "w", **prof) for k, (path, prof) in targets.items()}
    try:
        starts = list(range(0, height, EXPORT_BLOCK_ROWS))
        with ThreadPoolExecutor(max(1, threads)) as ex:
            for i in range(0, len(starts), 2 * max(1, threads)):          # bounded look-ahead keeps RAM low
                for row, rows, result in ex.map(compute, starts[i:i + 2 * max(1, threads)]):
                    for k, arr in result.items():
                        outs[k].write(arr, window=Window(0, row, width, rows))
    finally:
        for ds in outs.values():
            ds.close()
    return {k: path for k, (path, _) in targets.items()}


def export(tmp_path, dst_path, threads):
    """Final image: optional reprojection, then JP2 (lossless/lossy) or GeoTIFF (ZSTD).
    JP2 keeps nodata=0 in its .aux.xml sidecar (JPEG2000 cannot store nodata itself)."""
    with rasterio.open(tmp_path) as src:
        source = src
        if OUTPUT_CRS:
            dst_crs = CRS.from_epsg(OUTPUT_CRS) if isinstance(OUTPUT_CRS, int) else CRS.from_string(OUTPUT_CRS)
            transform, w, h = calculate_default_transform(src.crs, dst_crs, src.width, src.height, *src.bounds,
                                                          resolution=(abs(src.transform.a), abs(src.transform.e)))
            source = WarpedVRT(src, crs=dst_crs, transform=transform, width=w, height=h,
                               resampling=Resampling.cubic, src_nodata=0, nodata=0)
        try:
            if OUTPUT_FORMAT.upper() == "JP2":
                opts = dict(REVERSIBLE="YES", QUALITY=100) if LOSSLESS else dict(REVERSIBLE="NO", QUALITY=int(JP2_QUALITY))
                try:
                    with rasterio.Env(GDAL_NUM_THREADS=str(max(1, threads))):
                        rasterio.shutil.copy(source, dst_path, driver="JP2OpenJPEG", **opts)
                    return dst_path
                except Exception as exc:
                    log("  JP2 unavailable ({}); writing GeoTIFF.".format(exc))
            dst_path = os.path.splitext(dst_path)[0] + ".tif"
            with rasterio.Env(GDAL_PAM_ENABLED="NO"):
                rasterio.shutil.copy(source, dst_path, driver="GTiff", COMPRESS="ZSTD", ZSTD_LEVEL=9, PREDICTOR=2,
                                     TILED="YES", BIGTIFF="IF_SAFER", NUM_THREADS=str(max(1, threads)))
            return dst_path
        finally:
            if source is not src:
                source.close()


def new_work_dir(prefix="wpm_"):
    work = tempfile.mkdtemp(prefix=prefix, dir=temp_root())
    _WORK_DIRS.add(work)
    return work


def drop_work_dir(work):
    if KEEP_TEMP:
        log("Temporary files kept: {}".format(work))
    else:
        shutil.rmtree(work, ignore_errors=True)
    _WORK_DIRS.discard(work)


def folder_gb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 ** 3


def scene_info(items):
    """One scene per tile. Returns (by_tile, scene_id, tag) - tag = date_tiles."""
    by_tile = {}
    for it in items:
        tile = tile_of(it)
        if tile is None:
            raise PipelineError("Cannot read the tile of {}".format(it.get("id")))
        if tile in by_tile and by_tile[tile]["id"] != it["id"]:
            raise PipelineError("Two scenes for tile {} on the same date; select only one.".format(tile))
        by_tile[tile] = it
    tag = "{}_{}".format(min(date_of(it) for it in by_tile.values()).replace("-", ""), "_".join(sorted(by_tile)))
    return by_tile, "CBERS_4A_WPM_{}_L4".format(tag), tag


def footprint_of(by_tile):
    return unary_union([shape(it["geometry"]) for it in by_tile.values()])


def run_scene(by_tile, scene_id, tag, tclt, work, threads, clip, crop):
    """Bands -> TCLT -> products. `clip` (GeoDataFrame) limits what TCLT processes,
    `crop` is the final extent. Returns {kind: temporary GeoTIFF}."""
    clip_path, crop_path = os.path.join(work, "clip.gpkg"), os.path.join(work, "crop.gpkg")
    with _GEO_LOCK:
        clip.to_file(clip_path, driver="GPKG")
        crop.to_file(crop_path, driver="GPKG")
        area = unary_union(list(clip.to_crs(4326).geometry))
    outside = [t for t, it in by_tile.items() if not shape(it["geometry"]).intersects(area)]
    if outside:
        raise PipelineError("Scene(s) do not intersect the area: " + ", ".join(outside))

    elevation = sun_elevation(next(iter(by_tile.values())))
    cos_az = math.cos(math.radians(90.0 - elevation))
    log("Sun elevation: {:.2f} deg".format(elevation))
    bands = prepare_bands(by_tile, clip, work, scene_id, cos_az, threads)

    project = os.path.join(work, "wpm_1m.txt")
    tclt_log = os.path.join(work, "tclt_log.txt")
    for i, moravec in enumerate(MORAVEC_LADDER):
        write_tclt_project(project, bands, clip_path, "An_WPM_PCA_RGB321_" + tag, moravec)
        log("Running TCLT ({} threads, MORAVEC_MIN_ABS_CORR={})...".format(threads, moravec))
        code, output = run_tclt(tclt, project, work, tclt_log, threads)
        tie_point_failure = "Unable to locate tie-points" in output
        if code == 0 or not tie_point_failure:
            break
        if i < len(MORAVEC_LADDER) - 1:
            log("Not enough tie points; retrying with a lower threshold.")

    failed = [l for l in output.splitlines() if "Status:FAIL" in l]
    if code != 0 or failed:
        if not SHOW_TCLT_OUTPUT:
            log(output)
        if tie_point_failure:
            raise PipelineError("Band registration failed: not enough tie points (no recognizable features).")
        if code is not None and code < 0:
            raise PipelineError("TCLT was killed by signal {} (out of memory?).".format(-code))
        raise PipelineError("TCLT failed (exit code {}). The first 'Status:FAIL' line in the TCLT output is the cause."
                            .format(code))
    log("TCLT finished ({:.1f} GB of temporary files). Writing images...".format(folder_gb(work)))
    return build_products(work, crop_path, threads)


def product_name(kind, scene_id):
    ext = ".jp2" if OUTPUT_FORMAT.upper() == "JP2" else ".tif"
    return scene_id + "_NRGB" + ext if kind == "NRGB" else "{}_RGB_{}_8bit{}".format(scene_id, CONTRAST_STRETCH, ext)


def export_products(products, scene_id, threads):
    share = max(1, threads // max(1, len(products)))
    with ThreadPoolExecutor(max(1, len(products))) as ex:
        futures = [ex.submit(export, tmp, os.path.join(output_dir(), product_name(k, scene_id)), share)
                   for k, tmp in products.items()]
        outputs = [f.result() for f in futures]
    for path in outputs:
        log("Saved: {}".format(path))
    return outputs


def process_roi(items, tclt, threads):
    """ROI mode: one TCLT run for the ROI of one date (several tiles of the date are mosaicked)."""
    work = new_work_dir()
    try:
        roi, _, _ = load_roi(work, quiet=True)
        by_tile, scene_id, tag = scene_info(items)
        log("Scene(s): {}".format(", ".join(it["id"] for _, it in sorted(by_tile.items()))))
        with _GEO_LOCK:
            area = unary_union(list(roi.to_crs(4326).geometry))
        covered = area.intersection(footprint_of(by_tile)).area / area.area
        if covered < MIN_COVERAGE:
            raise PipelineError("Only {:.0%} of the ROI is covered by the selected scene(s) ({:.0%} required). "
                                "Move the ROI inside the scene, add the adjacent tile from the same date, "
                                "or set PROCESS_ENTIRE_TILE = True.".format(covered, MIN_COVERAGE))
        products = run_scene(by_tile, scene_id, tag, tclt, work, threads, roi, roi)
        return export_products(products, scene_id, threads)
    finally:
        drop_work_dir(work)


def tile_blocks(items):
    """Full-tile mode: a grid of BLOCK_SIZE_KM squares over the scene footprint.
    Returns (crs, [(core, clip)]): core = block part kept in the mosaic, clip = core + ROI_BUFFER_KM margin."""
    by_tile, _, _ = scene_info(items)
    with _GEO_LOCK:
        fp = gpd.GeoDataFrame({"id": [1]}, geometry=[footprint_of(by_tile)], crs=4326)
        crs = fp.estimate_utm_crs()
        fp = fp.to_crs(crs).geometry.iloc[0]
    step = min(max(float(BLOCK_SIZE_KM), ROI_SIZE_RANGE_KM[0]), ROI_SIZE_RANGE_KM[1]) * 1000.0
    pad = min(max(float(ROI_BUFFER_KM), 0.002), 10.0) * 1000.0
    x0, y0, x1, y1 = fp.bounds
    blocks = []
    for j in range(int(math.ceil((y1 - y0) / step))):
        for i in range(int(math.ceil((x1 - x0) / step))):
            cell = box(x0 + i * step, y1 - (j + 1) * step, x0 + (i + 1) * step, y1 - j * step)
            core = cell.intersection(fp)
            if core.area < 0.02 * cell.area:
                continue
            blocks.append((core, cell.buffer(pad, join_style=2).intersection(fp)))
    return crs, blocks


def process_block(items, tclt, threads, crs, core, clip, dest, index, total):
    """Full-tile mode: one block. Products are cropped to the block core and kept in `dest`.
    A block that fails (e.g. only water, no tie points) is skipped and left as a gap."""
    work = new_work_dir()
    try:
        by_tile, scene_id, tag = scene_info(items)
        with _GEO_LOCK:
            clip_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[clip], crs=crs)
            core_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[core], crs=crs)
        products = run_scene(by_tile, scene_id, "{}_b{:03d}".format(tag, index), tclt, work, threads, clip_gdf, core_gdf)
        kept = {}
        for kind, path in products.items():
            kept[kind] = os.path.join(dest, "block{:03d}_{}.tif".format(index, kind))
            shutil.move(path, kept[kind])
        log("Block {}/{} done".format(index, total))
        return kept
    except PipelineError as exc:
        log("Block {}/{} skipped: {}".format(index, total, exc))
        return {"error": str(exc)}
    finally:
        drop_work_dir(work)


def mosaic(paths, dst, threads):
    """Stream block images into one GeoTIFF (blocks share the same pixel grid; 0 = empty)."""
    srcs = [rasterio.open(p) for p in paths]
    try:
        ref = srcs[0]
        rx, ry = ref.res
        left, top = min(s.bounds.left for s in srcs), max(s.bounds.top for s in srcs)
        right, bottom = max(s.bounds.right for s in srcs), min(s.bounds.bottom for s in srcs)
        profile = dict(driver="GTiff", width=int(round((right - left) / rx)), height=int(round((top - bottom) / ry)),
                       count=ref.count, dtype=ref.dtypes[0], crs=ref.crs, transform=Affine(rx, 0, left, 0, -ry, top),
                       nodata=0, tiled=True, blockxsize=512, blockysize=512, compress="zstd", zstd_level=1,
                       BIGTIFF="YES", num_threads=max(1, threads))
        with rasterio.open(dst, "w+", **profile) as out:
            for s in srcs:
                col, row = (s.transform.c - left) / rx, (top - s.transform.f) / ry
                if abs(col - round(col)) > 0.01 or abs(row - round(row)) > 0.01:
                    log("  Warning: {} is off the common pixel grid by ({:.2f}, {:.2f}) px".format(
                        os.path.basename(s.name), col - round(col), row - round(row)))
                col, row = int(round(col)), int(round(row))
                for r in range(0, s.height, EXPORT_BLOCK_ROWS):
                    n = min(EXPORT_BLOCK_ROWS, s.height - r)
                    data = s.read(window=Window(0, r, s.width, n))
                    target = Window(col, row + r, s.width, n)
                    current = out.read(window=target)
                    take = (data != 0).any(axis=0)
                    current[:, take] = data[:, take]
                    out.write(current, window=target)
    finally:
        for s in srcs:
            s.close()
    return dst


def finish_tile(items, block_results, dest, threads):
    """Mosaic the blocks of one date and write the final images."""
    _, scene_id, _ = scene_info(items)
    done = [r for r in block_results if "error" not in r]
    failed = len(block_results) - len(done)
    if not done:
        raise PipelineError("All {} blocks failed. First error: {}".format(len(block_results), block_results[0]["error"]))
    if failed:
        log("Warning: {} of {} block(s) failed and are left empty in the mosaic.".format(failed, len(block_results)))
    log("Mosaicking {} block(s)...".format(len(done)))
    products = {}
    for kind in ("NRGB", "RGB"):
        paths = [r[kind] for r in done if kind in r]
        if paths:
            products[kind] = mosaic(paths, os.path.join(dest, "mosaic_{}.tif".format(kind)), threads)
    return export_products(products, scene_id, threads)


# ------------------------------------------------------------------------- CLI

def require_libs():
    if MISSING_LIBS:
        raise PipelineError("Missing Python libraries: {}. Install with: pip install {}"
                            .format(", ".join(MISSING_LIBS), " ".join(MISSING_LIBS)))


def plan_parallelism(n_jobs, pan_px_per_job, disk_gb_per_job=None):
    """Jobs run at the same time and TCLT/GDAL threads per job, from CPU, RAM and disk."""
    cpus, ram = detect_cpus(), detect_ram_gb()
    ram_per_job = 2.0 + pan_px_per_job * RAM_BYTES_PER_PAN_PIXEL / 1024 ** 3
    limits = [n_jobs, max(1, cpus // THREADS_PER_DATE_MIN), max(1, int(ram * 0.8 // ram_per_job))]
    if disk_gb_per_job:
        free = shutil.disk_usage(temp_root()).free / 1024 ** 3
        log("Disk: ~{:.0f} GB per block (conservative estimate), {:.0f} GB free in TEMP_DIR".format(disk_gb_per_job, free))
        if free < disk_gb_per_job and not PARALLEL_JOBS:
            raise PipelineError("Not enough disk in TEMP_DIR for one block (~{:.0f} GB estimated, {:.0f} GB free). "
                                "Reduce BLOCK_SIZE_KM, point TEMP_DIR to a larger volume, or set PARALLEL_JOBS = 1 "
                                "to try anyway.".format(disk_gb_per_job, free))
        limits.append(max(1, int(free // disk_gb_per_job)))
    parallel = max(1, min(n_jobs, int(PARALLEL_JOBS))) if PARALLEL_JOBS else max(1, min(limits))
    return parallel, max(1, cpus // parallel), cpus, ram, ram_per_job


def cmd_check():
    ok = True
    log("Python {} ({})".format(platform.python_version(), sys.executable))
    for mod in ("numpy", "requests", "geopandas", "rasterio", "shapely"):
        try:
            log("  OK       {:<10} {}".format(mod, getattr(__import__(mod), "__version__", "")))
        except ImportError:
            ok = False
            log("  MISSING  {}".format(mod))
    if "rasterio" not in MISSING_LIBS:
        with rasterio.Env() as env:
            jp2 = "JP2OpenJPEG" in env.drivers()
        log("  GDAL {} - JP2: {}".format(rasterio.__gdal_version__, "yes" if jp2 else "no (GeoTIFF will be used)"))

    log("TCLT")
    try:
        tclt = get_tclt()
    except PipelineError as exc:
        log("  ERROR: {}".format(exc))
        return False
    log("  home:     {}".format(tclt.home))
    log("  exe:      {}".format(os.path.realpath(tclt.exe)))
    log("  runtime:  {}".format(tclt.runtime or "not found (using host libraries)"))

    targets = [os.path.realpath(tclt.exe)] + sorted({os.path.realpath(f) for f in glob.glob(os.path.join(tclt.home, "lib", "*.so*"))
                                                    if os.path.isfile(f)})

    def deps(target):
        cmd, env = tclt_command(tclt, target, [], list_deps=True)
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ["{}: {}".format(os.path.basename(target), exc)]
        return [re.sub(r"\s*\(required by .*\)", "", l.strip()) for l in (r.stdout + r.stderr).splitlines() if "not found" in l]

    with ThreadPoolExecutor(8) as ex:
        missing = list(dict.fromkeys(m for found in ex.map(deps, targets) for m in found))
    if missing:
        ok = False
        log("  Missing libraries:")
        for item in missing[:30]:
            log("    " + item)
        if any("GLIBC" in m for m in missing) and not tclt.runtime:
            log("  -> Put {}.tar.xz in {} and run check again.".format(RUNTIME_NAME, os.path.expanduser("~")))
    else:
        log("  libraries: all found ({} files checked)".format(len(targets)))

    cmd, env = tclt_command(tclt, tclt.exe, ["--help"])
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, cwd=tclt.home)
        out = r.stdout + r.stderr
        load_error = any(s in out for s in ("error while loading shared libraries", "cannot open shared object",
                                            "failed to map segment", "' not found"))
        version = next((l.strip() for l in out.splitlines() if "Version" in l), "")
        if r.returncode in (0, 1) and not load_error:
            log("  run test: OK {}".format("({})".format(version) if version else ""))
        else:
            ok = False
            log("  run test: FAILED (exit code {})".format(r.returncode))
            for line in out.strip().splitlines()[:10]:
                log("    | " + line)
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok = False
        log("  run test: FAILED ({})".format(exc))

    log("Resources")
    cpus, ram = detect_cpus(), detect_ram_gb()
    log("  CPUs: {} | free RAM: {:.0f} GB | GDAL cache: {} MB".format(cpus, ram, os.environ.get("GDAL_CACHEMAX")))
    log("  up to {} job(s) at a time, {} threads each".format(
        max(1, cpus // THREADS_PER_DATE_MIN), max(1, cpus // max(1, cpus // THREADS_PER_DATE_MIN))))
    try:
        log("  temp dir: {} ({:.0f} GB free)".format(temp_root(), shutil.disk_usage(temp_root()).free / 1024 ** 3))
        log("  output:   {}".format(output_dir()))
    except OSError as exc:
        ok = False
        log("  ERROR: {}".format(exc))
    if "requests" not in MISSING_LIBS:
        try:
            log("  STAC: HTTP {}".format(requests.get(STAC_URL, timeout=30).status_code))
        except requests.RequestException as exc:
            ok = False
            log("  STAC: ERROR {}".format(exc))

    log("RESULT: {}".format("ready. Next: python wpm_bdc.py search" if ok else "fix the items above."))
    return ok


def print_scenes(scenes):
    log("{:>3}  {:<10}  {:<7}  {:>4}  {:>5}  {}".format("#", "Date", "Tile", "Days", "Cloud", "ID"))
    for i, s in enumerate(scenes, start=1):
        cloud = "N/A" if s["cloud"] is None else "{:.0f}%".format(float(s["cloud"]))
        log("{:>3}  {:<10}  {:<7}  {:>4}  {:>5}  {}".format(i, s["date"], s["tile"] or "?", s["days"], cloud, s["id"]))


def save_thumbnails(scenes):
    folder = os.path.join(temp_root(), "thumbnails")
    os.makedirs(folder, exist_ok=True)

    def fetch(args):
        i, s = args
        path = os.path.join(folder, "{:03d}_{}_{}.png".format(i, s["date"], s["tile"]))
        if not s["thumbnail"] or os.path.exists(path):
            return
        try:
            r = requests.get(s["thumbnail"], timeout=15)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        except requests.RequestException as exc:
            log("  Thumbnail {} failed: {}".format(i, exc))

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(fetch, enumerate(scenes, start=1)))
    log("Thumbnails: {}".format(folder))


def cmd_search():
    require_libs()
    scenes = search_scenes()
    if not scenes:
        log("No scenes found. Widen the dates or move the ROI.")
        return
    print_scenes(scenes)
    if SAVE_THUMBNAILS:
        save_thumbnails(scenes)


def in_jupyter():
    return "ipykernel" in sys.modules


def parse_selection(text, n):
    text = text.strip().lower()
    if text in ("all", "*"):
        return list(range(n))
    chosen = []
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        for k in range(int(lo), int(hi or lo) + 1):
            if not 1 <= k <= n:
                raise ValueError("{} is not in the list".format(k))
            if k - 1 not in chosen:
                chosen.append(k - 1)
    return chosen


def select_scenes(scenes):
    if SCENE_IDS:
        by_id = {s["id"]: s for s in scenes}
        missing = [i for i in SCENE_IDS if i not in by_id]
        if missing:
            raise PipelineError("SCENE_IDS not found in the search (check dates/ROI): " + ", ".join(missing))
        return [by_id[i] for i in SCENE_IDS]
    print_scenes(scenes)
    if not sys.stdin.isatty() and not in_jupyter():
        raise PipelineError("No terminal to choose scenes; fill SCENE_IDS.")
    while True:
        try:
            text = input("Scenes to process (e.g. 1,3,5-7 or 'all'; Enter = cancel): ")
        except EOFError:
            text = ""
        if not text.strip():
            raise Cancelled("No scene selected.")
        try:
            chosen = parse_selection(text, len(scenes))
            if chosen:
                return [scenes[i] for i in chosen]
        except ValueError as exc:
            print("  Invalid input ({}).".format(exc))


def group_by_date(scenes):
    groups = {}
    for s in scenes:
        groups.setdefault(s["date"], []).append(s)
    for day, group in groups.items():
        tiles = [s["tile"] for s in group]
        dupes = sorted({t for t in tiles if tiles.count(t) > 1})
        if dupes:
            raise PipelineError("Only one scene per tile per date (date {}, tile {}).".format(day, ", ".join(dupes)))
    return [(day, groups[day]) for day in sorted(groups)]


def cmd_process():
    require_libs()
    if not (GENERATE_RGB or GENERATE_NRGB):
        raise PipelineError("Enable GENERATE_RGB and/or GENERATE_NRGB.")
    if OUTPUT_FORMAT.upper() not in ("JP2", "TIFF"):
        raise PipelineError('OUTPUT_FORMAT must be "JP2" or "TIFF".')
    tclt = get_tclt()
    log("TCLT: {}{}".format(tclt.exe, " (runtime: {})".format(tclt.runtime) if tclt.runtime else ""))

    scenes = search_scenes()
    if not scenes:
        raise PipelineError("No scenes found. Widen the dates or move the ROI.")
    runs = group_by_date(select_scenes(scenes))

    # Work units: one per date (ROI mode) or one per block (full-tile mode)
    units, tiles = [], {}
    if PROCESS_ENTIRE_TILE:
        for day, group in runs:
            items = [s["item"] for s in group]
            crs, blocks = tile_blocks(items)
            dest = new_work_dir("tile_")
            tiles[day] = {"items": items, "dest": dest, "results": []}
            log("{}: full tile split into {} block(s) of {:g} km".format(day, len(blocks), BLOCK_SIZE_KM))
            for i, (core, clip) in enumerate(blocks, start=1):
                units.append({"day": day, "tag": "{} {}/{}".format(day, i, len(blocks)), "px": clip.area / PAN_PIXEL_M2,
                              "run": (lambda t, a=(items, tclt), b=(crs, core, clip, dest, i, len(blocks)):
                                      process_block(a[0], a[1], t, *b))})
        px = max(u["px"] for u in units)
        disk = px * DISK_BYTES_PER_PAN_PIXEL / 1024 ** 3
    else:
        work = new_work_dir("plan_")
        try:
            px = load_roi(work, quiet=True)[2] / PAN_PIXEL_M2
        finally:
            drop_work_dir(work)
        disk = None
        for day, group in runs:
            units.append({"day": day, "tag": day, "px": px,
                          "run": (lambda t, items=[s["item"] for s in group]: process_roi(items, tclt, t))})

    parallel, threads, cpus, ram, ram_per_unit = plan_parallelism(len(units), px, disk)
    log("Resources: {} CPUs, {:.0f} GB free RAM -> {} job(s) at a time, {} threads each (~{:.0f} GB RAM per job)"
        .format(cpus, ram, parallel, threads, ram_per_unit))

    def run_unit(unit):
        _TLS.tag = unit["tag"]
        return unit["run"](threads)

    outputs, failed = [], []
    executor = ThreadPoolExecutor(parallel)
    futures = {executor.submit(run_unit, u): u for u in units}
    try:
        for future in as_completed(futures):
            unit = futures[future]
            try:
                result = future.result()
                if unit["day"] in tiles:
                    tiles[unit["day"]]["results"].append(result)
                else:
                    outputs += result
                    log("[{}] Done".format(unit["day"]))
            except Cancelled:
                if not failed:
                    raise
            except Exception as exc:
                failed.append(unit["day"])
                log("ERROR ({}): {}".format(unit["tag"], exc))
                if not CONTINUE_ON_ERROR:
                    stop_everything()
                    for other in futures:
                        other.cancel()
        executor.shutdown(wait=True)

        # Full-tile mode: mosaic each date once all of its blocks are done
        pending = [(day, t) for day, t in tiles.items() if day not in failed]
        if pending:
            def run_tile(day, tile):
                _TLS.tag = day
                try:
                    return finish_tile(tile["items"], tile["results"], tile["dest"], max(1, cpus // len(pending)))
                finally:
                    drop_work_dir(tile["dest"])
            with ThreadPoolExecutor(len(pending)) as ex:
                tile_futures = {ex.submit(run_tile, day, t): day for day, t in pending}
                for future in as_completed(tile_futures):
                    try:
                        outputs += future.result()
                    except PipelineError as exc:
                        failed.append(tile_futures[future])
                        log("ERROR ({}): {}".format(tile_futures[future], exc))
    except KeyboardInterrupt:
        log("Interrupted - stopping TCLT and cleaning temporary files...")
        stop_everything()
        executor.shutdown(wait=False, cancel_futures=True)
        for work in list(_WORK_DIRS):
            shutil.rmtree(work, ignore_errors=True)
        if not in_jupyter():
            os._exit(130)
        raise Cancelled("Interrupted by user.")
    finally:
        for t in tiles.values():
            if os.path.isdir(t["dest"]) and not KEEP_TEMP:
                drop_work_dir(t["dest"])
    log("Finished: {} image(s) in {}".format(len(outputs), output_dir()))
    if failed:
        raise PipelineError("Failed date(s): {}".format(", ".join(sorted(set(failed)))))


def main(argv=None):
    if argv is None:
        argv = [] if in_jupyter() else sys.argv[1:]     # Jupyter passes its own --f=... argument
    parser = argparse.ArgumentParser(description="WPM 1-meter spatial resolution (CBERS-4A)")
    parser.add_argument("action", nargs="?", default=DEFAULT_ACTION, choices=["check", "search", "process"])
    args, _ = parser.parse_known_args([a for a in argv if not a.startswith("-f") and not a.startswith("--f=")])
    CANCEL.clear()
    try:
        if args.action == "check":
            return 0 if cmd_check() else 1
        if args.action == "search":
            cmd_search()
        else:
            cmd_process()
        return 0
    except (Cancelled, KeyboardInterrupt) as exc:
        log("Cancelled. {}".format(exc))
        return 130
    except PipelineError as exc:
        log("ERROR: {}".format(exc))
        return 1
    except Exception:
        import traceback
        log("Unexpected error:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    code = main()
    if not in_jupyter():
        sys.exit(code)
