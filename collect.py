#!/usr/bin/env python3
"""
El Niño Watch — daily collector.

Downloads live data from public sources, keeps only the 35 tracked countries,
places everything in its province, and writes auto.json (+ satellite images).
The editorial file data.json is never touched.

Usage:
  FIRMS_KEY=xxxx python3 collect.py --base ..  [--prev path/to/previous/auto.json]

Sources:
  GDACS (floods, cyclones, droughts) · WMO SWIC (official weather warnings)
  NASA FIRMS (fire hotspots) · Open-Meteo (7-day forecast + air quality per city)
  NOAA NHC (Atlantic / East Pacific hurricanes) · Copernicus EMS (satellite emergency maps)
  NOAA CPC (weekly Niño indices) · NASA GIBS (daily satellite images)
"""
import argparse, csv, datetime as dt, io, json, math, os, re, sys, time, traceback
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from shapely.geometry import shape, Point
from shapely.strtree import STRtree

UA = {"User-Agent": "ElNinoWatch/1.0 (personal research map)"}
TODAY = dt.datetime.now(dt.timezone.utc).date()

LATAM = "ARG URY PRY BRA BOL PER ECU COL VEN CHL PAN CRI NIC HND SLV GTM MEX BLZ CUB HTI DOM GUY SUR".split()
SEA = "IDN MYS SGP BRN PHL VNM THA KHM LAO MMR TLS PNG".split()
TRACKED = set(LATAM + SEA)

# ----------------------------------------------------------------- helpers
def get(url, timeout=90, tries=3, binary=False):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # network hiccup or rate limit: wait and retry
            last = e
            code = getattr(e, "code", None)
            time.sleep(65 if code == 429 else 2 + 3 * i)
    raise last

def get_json(url, **kw):
    txt = get(url, **kw)
    return json.loads(txt) if txt.strip() else None

def iso_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None

class Geo:
    """Province lookup: point -> province key 'ISO|Name'."""
    def __init__(self, base):
        prov = json.load(open(os.path.join(base, "provinces.geojson")))
        self.feats, self.geoms = [], []
        for f in prov["features"]:
            if not f.get("geometry") or not f["properties"].get("name"):
                continue
            g = shape(f["geometry"]).buffer(0)
            self.feats.append(f["properties"])
            self.geoms.append(g)
        self.tree = STRtree(self.geoms)
        self.by_iso = {}
        for i, p in enumerate(self.feats):
            self.by_iso.setdefault(p["iso"], []).append(i)

    def key(self, i):
        p = self.feats[i]
        return p["iso"] + "|" + p["name"]

    def locate(self, lng, lat, iso=None, max_km=60):
        pt = Point(lng, lat)
        for i in self.tree.query(pt):
            if self.geoms[i].contains(pt) and (iso is None or self.feats[i]["iso"] == iso):
                return self.key(i)
        # near the coast or a border: nearest province within max_km
        cands = self.by_iso.get(iso, range(len(self.feats))) if iso else range(len(self.feats))
        best, bi = 1e9, None
        for i in cands:
            d = self.geoms[i].distance(pt)
            if d < best:
                best, bi = d, i
        if bi is not None and best * 111 <= max_km:
            return self.key(bi)
        return None

    def iso_of(self, lng, lat):
        k = self.locate(lng, lat, None, max_km=30)
        return k.split("|")[0] if k else None

class Report:
    def __init__(self):
        self.sources = {}
    def run(self, name, fn, *a, **kw):
        t0 = time.time()
        try:
            out, count = fn(*a, **kw)
            self.sources[name] = {"ok": True, "count": count, "seconds": round(time.time() - t0, 1),
                                  "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")}
            return out
        except Exception as e:
            self.sources[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300],
                                  "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")}
            traceback.print_exc()
            return None

# ----------------------------------------------------------------- GDACS
GDACS_TYPE = {"FL": "flood", "TC": "cyclone", "DR": "drought"}
GDACS_SEV = {"Red": "red", "Orange": "orange", "Green": "yellow"}

def gdacs(geo):
    out, frm = [], (TODAY - dt.timedelta(days=45)).isoformat()
    for et in ["FL", "TC", "DR"]:
        page = 1
        while True:
            url = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?"
                   f"eventlist={et}&fromdate={frm}&todate={TODAY.isoformat()}&alertlevel=Green;Orange;Red"
                   f"&pagesize=100&pagenumber={page}")
            d = get_json(url)
            feats = (d or {}).get("features", [])
            for f in feats:
                p = f["properties"]
                if str(p.get("iscurrent")).lower() != "true" and (iso_date(p.get("todate")) or TODAY) < TODAY - dt.timedelta(days=5):
                    continue
                isos = [c["iso3"] for c in (p.get("affectedcountries") or []) if c.get("iso3") in TRACKED]
                if not isos and p.get("iso3") in TRACKED:
                    isos = [p["iso3"]]
                if not isos:
                    continue
                sev = GDACS_SEV.get(p.get("alertlevel"), "yellow")
                if et == "DR" and sev == "yellow":
                    continue  # green droughts are background noise
                lng, lat = f["geometry"]["coordinates"][:2]
                for iso in isos:
                    pl, pa = lat, lng
                    if et == "DR" and len(isos) > 1:
                        pl, pa = None, None  # multi-country drought: placed per country below
                    prov = geo.locate(pa, pl, iso) if pl is not None else None
                    out.append({
                        "id": f"gdacs-{et}-{p['eventid']}-{iso}", "origin": "GDACS", "iso": iso,
                        "type": GDACS_TYPE[et], "severity": sev,
                        "date": str(p.get("fromdate"))[:10], "updated": str(p.get("todate"))[:10],
                        "lat": pl, "lng": pa, "prov": prov,
                        "title": p.get("name") or p.get("description"),
                        "text": re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", p.get("htmldescription") or ""))[:300],
                        "severityText": (p.get("severitydata") or {}).get("severitytext", "").strip(),
                        "source": "GDACS", "url": (p.get("url") or {}).get("report"),
                    })
            if len(feats) < 100:
                break
            page += 1
    return out, len(out)

# ----------------------------------------------------------------- WMO SWIC (official CAP warnings)
WMO_SEV = {4: "red", 3: "orange", 2: "yellow"}  # 1 = minor, skipped
KIND = [
    ("flood", r"inund|crecid|desbord|enchente|inunda|alagamento|banjir|flood|crue|lũ|ngập|อุทกภัย|น้ำท่วม"),
    ("rain", r"lluvi|precipit|chuva|chubasc|hujan|rain|mưa|ฝน|aguacero|temporal"),
    ("storm", r"torment|tempest|trovoad|petir|thunder|storm|giông|พายุ|electric|granizo|hail"),
    ("heat", r"calor|temperatura.*(alta|extrem|elevad)|heat|panas|nắng nóng|ร้อน|ola de calor"),
    ("fire", r"incend|fuego|queimad|kebakaran|karhutla|fire|cháy"),
    ("drought", r"sequ|seca|estiaje|kekeringan|drought|hạn hán|ภัยแล้ง|déficit h"),
    ("haze", r"humo|fumaça|asap|haze|smoke|calidad del aire|air quality|polvo|dust|kabut"),
]
def classify(text):
    t = (text or "").lower()
    for k, rx in KIND:
        if re.search(rx, t):
            return k
    return None

def wmo(geo):
    members = get_json("https://severeweather.wmo.int/json/wmo_member.json")
    mids = {}
    for ra in members:
        for m in ra["members"]:
            if m.get("code") in TRACKED:
                mids[m["mid"]] = (m["code"], m.get("dept") or m["name"])
    now = dt.datetime.now(dt.timezone.utc)
    groups = {}
    raw = 0
    for mid, (iso, dept) in mids.items():
        url = ("https://severeweather.wmo.int/f/wfs?request=GetFeature&version=1.1.0"
               "&typeName=local_postgis:postgis_geojsons&outputFormat=json"
               f"&cql_filter=mem='{mid}'")
        try:
            d = get_json(url, timeout=120, tries=2)
        except Exception:
            continue
        for f in (d or {}).get("features", []):
            p = f["properties"]
            raw += 1
            try:
                s = int(p.get("s") or 0)
            except ValueError:
                s = 0
            if s < 2 or str(p.get("marine")) == "1":
                continue
            exp = p.get("chk_expires") or p.get("expires")
            try:
                if exp and exp != "None" and dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) < now:
                    continue
            except ValueError:
                pass
            kind = classify(" ".join(str(p.get(k) or "") for k in ("event", "headline", "description")))
            if not kind or not f.get("geometry"):
                continue
            g = shape(f["geometry"])
            if g.is_empty:
                continue
            c = g.representative_point()
            prov = geo.locate(c.x, c.y, iso, max_km=80)
            if not prov:
                continue
            k = (prov, kind)
            gr = groups.setdefault(k, {"prov": prov, "iso": iso, "kind": kind, "severity": "yellow", "s": 0,
                                       "count": 0, "events": set(), "expires": None, "source": dept,
                                       "lat": round(c.y, 3), "lng": round(c.x, 3)})
            gr["count"] += 1
            gr["events"].add((p.get("event") or "").strip()[:60])
            if s > gr["s"]:
                gr["s"], gr["severity"] = s, WMO_SEV.get(s, "yellow")
                gr["lat"], gr["lng"] = round(c.y, 3), round(c.x, 3)
                gr["headline"] = (p.get("headline") or p.get("event") or "").strip()[:140]
                gr["description"] = re.sub(r"\s+", " ", p.get("description") or "").strip()[:280]
            if exp and exp != "None" and (gr["expires"] is None or exp > gr["expires"]):
                gr["expires"] = exp
    out = []
    for gr in groups.values():
        gr["events"] = sorted(e for e in gr["events"] if e)[:4]
        gr.pop("s", None)
        out.append(gr)
    out.sort(key=lambda x: ({"red": 0, "orange": 1, "yellow": 2}[x["severity"]], x["prov"]))
    return out, len(out)

# ----------------------------------------------------------------- NASA FIRMS
FIRMS_BOXES = {"latam": "-118,-56,-33,33", "sea": "90,-12,156,24"}

def firms(geo, key, prev):
    if not key:
        raise RuntimeError("FIRMS_KEY not set")
    pts = []
    for name, box in FIRMS_BOXES.items():
        for sensor in ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT"]:
            txt = get(f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{sensor}/{box}/2", timeout=180)
            for r in csv.DictReader(io.StringIO(txt)):
                try:
                    if r.get("confidence") == "l":
                        continue
                    pts.append((float(r["latitude"]), float(r["longitude"]), float(r.get("frp") or 0), r.get("acq_date")))
                except (KeyError, ValueError):
                    continue
    # grid 0.25° for the globe
    grid = {}
    for lat, lng, frp, d in pts:
        k = (round(lat * 4) / 4, round(lng * 4) / 4)
        g = grid.setdefault(k, [0, 0.0])
        g[0] += 1
        g[1] += frp
    cells = [[k[0], k[1], v[0], round(v[1])] for k, v in grid.items()]
    # per province counts (grid-cell centres, weighted by count — fast and close enough)
    by_prov, frp_prov, max_cell = {}, {}, {}
    for lat, lng, n, frp in cells:
        pk = geo.locate(lng, lat, None, max_km=15)
        if pk and pk.split("|")[0] in TRACKED:
            by_prov[pk] = by_prov.get(pk, 0) + n
            # intensity, not just count: total fire radiative power (MW) and the hottest 0.25° cell
            frp_prov[pk] = frp_prov.get(pk, 0) + frp
            max_cell[pk] = max(max_cell.get(pk, 0), frp)
    prev_by = ((prev or {}).get("fires") or {}).get("byProv") or {}
    trend = {k: v - prev_by.get(k, 0) for k, v in by_prov.items()}
    out = {"days": 2, "points": len(pts), "grid": sorted(cells, key=lambda c: -c[2]),
           "byProv": dict(sorted(by_prov.items(), key=lambda kv: -kv[1])), "change": trend,
           "frpByProv": dict(sorted(frp_prov.items(), key=lambda kv: -kv[1])),
           "maxCellFrp": max_cell,
           "date": TODAY.isoformat()}
    return out, len(pts)

# ----------------------------------------------------------------- Open-Meteo forecast + air quality
def pick_cities(base, n_per_country=5, extra_min_pop=800000):
    cities = json.load(open(os.path.join(base, "cities.json")))  # [name, iso, lat, lng, pop], sorted by pop
    chosen, per = [], {}
    for c in cities:
        iso = c[1]
        if per.get(iso, 0) < n_per_country or c[4] >= extra_min_pop:
            chosen.append(c)
            per[iso] = per.get(iso, 0) + 1
    return chosen

def openmeteo(base):
    chosen = pick_cities(base)
    res = {}
    failed = 0
    for i in range(0, len(chosen), 25):
        batch = chosen[i:i + 25]
        lat = ",".join(str(c[2]) for c in batch)
        lng = ",".join(str(c[3]) for c in batch)
        try:
          fc = get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lng, "forecast_days": 7, "timezone": "auto",
            "daily": "precipitation_sum,precipitation_probability_max,temperature_2m_max,temperature_2m_min,weather_code"}))
        except Exception:
            failed += len(batch)
            continue
        aq = None
        try:
            aq = get_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode({
                "latitude": lat, "longitude": lng, "forecast_days": 3, "timezone": "auto",
                "hourly": "us_aqi,pm2_5"}))
        except Exception:
            aq = None
        fc = fc if isinstance(fc, list) else [fc]
        aq = aq if isinstance(aq, list) else ([aq] if aq else [None] * len(batch))
        for c, f, a in zip(batch, fc, aq):
            dd = f.get("daily", {})
            days = []
            for j, day in enumerate(dd.get("time", [])):
                days.append({"d": day,
                             "rain": dd["precipitation_sum"][j], "pop": dd["precipitation_probability_max"][j],
                             "tmax": dd["temperature_2m_max"][j], "tmin": dd["temperature_2m_min"][j],
                             "code": dd["weather_code"][j]})
            rain7 = round(sum((x["rain"] or 0) for x in days), 1)
            tmax = max([x["tmax"] for x in days if x["tmax"] is not None] or [None]) if days else None
            maxday = max([x["rain"] or 0 for x in days] or [0])
            aqi_max = None
            if a and a.get("hourly", {}).get("us_aqi"):
                vals = [v for v in a["hourly"]["us_aqi"][:48] if v is not None]
                aqi_max = max(vals) if vals else None
            flags = []
            if maxday >= 50 or rain7 >= 100: flags.append("heavy-rain")
            if tmax is not None and tmax >= 38: flags.append("heat")
            if aqi_max is not None and aqi_max >= 151: flags.append("unhealthy-air")
            res[f"{c[1]}|{c[0]}"] = {"name": c[0], "iso": c[1], "lat": c[2], "lng": c[3],
                                    "rain7": rain7, "tmax": tmax, "aqiMax": aqi_max, "flags": flags, "days": days}
        time.sleep(8)  # stay well under Open-Meteo's free-tier per-minute limit
    if not res:
        raise RuntimeError(f"no forecasts ({failed} cities failed)")
    return res, len(res)

# ----------------------------------------------------------------- AccuWeather (alerts per key city)
ACCU_SEV = {"Red": "red", "Orange": "orange", "Yellow": "yellow"}

def accuweather(base, geo, key, budget=150, per_country=2):
    if not key:
        raise RuntimeError("ACCU_KEY not set")
    hdr = {**UA, "Authorization": f"Bearer {key}"}
    def call(path):
        req = urllib.request.Request("https://dataservice.accuweather.com/" + path, headers=hdr)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    cache_fn = os.path.join(base, "accu_keys.json")
    cache = json.load(open(cache_fn)) if os.path.exists(cache_fn) else {}
    cities = json.load(open(os.path.join(base, "cities.json")))
    chosen, per = [], {}
    for c in cities:  # biggest cities first
        if c[1] in TRACKED and per.get(c[1], 0) < per_country:
            chosen.append(c); per[c[1]] = per.get(c[1], 0) + 1
    calls, groups = 0, {}
    for name, iso, lat, lng, _ in chosen:
        ck = f"{iso}|{name}"
        if ck not in cache:
            if calls >= budget: break
            loc = call(f"locations/v1/cities/geoposition/search?q={lat},{lng}"); calls += 1
            cache[ck] = loc.get("Key")
        if calls >= budget: break
        alerts = call(f"alerts/v1/{cache[ck]}?details=true"); calls += 1
        for a in alerts or []:
            desc = (a.get("Description") or {}).get("English") or ""
            kind = classify(desc + " " + (a.get("Category") or ""))
            if not kind:
                continue
            sev = ACCU_SEV.get(a.get("AlarmLevel"), "yellow")
            prov = geo.locate(lng, lat, iso, max_km=60)
            if not prov:
                continue
            area = (a.get("Area") or [{}])[0]
            gk = (prov, kind)
            g = groups.setdefault(gk, {"prov": prov, "iso": iso, "kind": kind, "severity": sev, "count": 0, "events": [],
                                       "expires": area.get("EndTime"), "source": (a.get("Source") or "").strip() + " via AccuWeather",
                                       "lat": lat, "lng": lng, "headline": desc, "description": (area.get("Summary") or area.get("Text") or "")[:280],
                                       "url": (a.get("Link") or "").replace("http://", "https://"), "origin": "AccuWeather"})
            g["count"] += 1
            if desc not in g["events"]: g["events"].append(desc)
            order = {"yellow": 0, "orange": 1, "red": 2}
            if order[sev] > order[g["severity"]]:
                g["severity"], g["headline"] = sev, desc
        time.sleep(0.3)
    json.dump(cache, open(cache_fn, "w"), ensure_ascii=False)
    out = list(groups.values())
    return out, len(out)

# ----------------------------------------------------------------- NOAA NHC
def nhc(geo):
    d = get_json("https://www.nhc.noaa.gov/CurrentStorms.json")
    out = []
    for s in (d or {}).get("activeStorms", []):
        lat, lng = s.get("latitudeNumeric"), s.get("longitudeNumeric")
        out.append({"id": "nhc-" + s.get("id", ""), "name": s.get("name"), "classification": s.get("classification"),
                    "intensity_kt": s.get("intensity"), "pressure": s.get("pressure"),
                    "lat": lat, "lng": lng, "movement": f"{s.get('movementDir', '')}° at {s.get('movementSpeed', '')} kt",
                    "lastUpdate": s.get("lastUpdate"),
                    "url": (s.get("publicAdvisory") or {}).get("url") or "https://www.nhc.noaa.gov/",
                    "cone": (s.get("trackCone") or {}).get("kmzFile")})
    return out, len(out)

# ----------------------------------------------------------------- Copernicus EMS (RSS)
def ems(geo, country_names):
    xml = get("https://mapping.emergency.copernicus.eu/latest/feed/")
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip().replace("http://", "https://")
        date = it.findtext("pubDate") or ""
        try:
            d = dt.datetime.strptime(date[:16], "%a, %d %b %Y").date().isoformat()
        except ValueError:
            d = None
        iso = next((i for n, i in country_names.items() if n.lower() in title.lower()), None)
        if not iso or not re.search(r"flood|fire|drought|storm|cyclone|hurricane|typhoon|landslide|rain", title.lower()):
            continue  # only weather-related activations (no earthquakes, volcanoes, industrial accidents)
        code = re.search(r"EMSR\d+", desc + " " + link)
        out.append({"iso": iso, "title": title, "date": d, "url": link, "code": code.group(0) if code else None,
                    "text": desc[:300]})
    return out, len(out)

# ----------------------------------------------------------------- NOAA CPC weekly Niño indices
def cpc():
    txt = get("https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for")
    rows = []
    for line in txt.splitlines():
        m = re.match(r"\s*(\d{2}[A-Z]{3}\d{4})\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", line)
        if m:
            d = dt.datetime.strptime(m.group(1), "%d%b%Y").date().isoformat()
            rows.append({"week": d, "nino12": float(m.group(3)), "nino3": float(m.group(5)),
                         "nino34": float(m.group(7)), "nino4": float(m.group(9))})
    rows = rows[-26:]
    return {"weekly": rows, "latest": rows[-1] if rows else None,
            "note": "Absolute anomalies vs 1991-2020 (OISST). NOAA's official strength uses the relative index (RONI), about 1 °C lower."}, len(rows)

# ----------------------------------------------------------------- NASA GIBS images
GIBS_LAYERS = {
    "truecolor": ("VIIRS_SNPP_CorrectedReflectance_TrueColor", "image/jpeg", "jpg", 1),
    "smoke": ("MODIS_Combined_Value_Added_AOD", "image/png", "png", 1),
    "rain": ("IMERG_Precipitation_Rate", "image/png", "png", 0),
    "fires": ("VIIRS_NOAA20_Thermal_Anomalies_375m_All", "image/png", "png", 1),
}
def gibs(base):
    os.makedirs(os.path.join(base, "sat"), exist_ok=True)
    out = {}
    for name, (layer, fmt, ext, lag) in GIBS_LAYERS.items():
        for back in range(lag, lag + 3):  # newest day with data
            day = (TODAY - dt.timedelta(days=back)).isoformat()
            url = ("https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1"
                   f"&LAYERS={layer}&SRS=EPSG:4326&BBOX=-180,-90,180,90&WIDTH=4096&HEIGHT=2048&FORMAT={urllib.parse.quote(fmt)}"
                   f"&TRANSPARENT=true&TIME={day}")
            data = get(url, timeout=180, binary=True)
            if len(data) > 60000:
                fn = f"sat/{name}.{ext}"
                open(os.path.join(base, fn), "wb").write(data)
                out[name] = {"file": fn, "date": day, "layer": layer}
                break
    return out, len(out)

# ----------------------------------------------------------------- Worldview links
WV_LAYERS = {
    "fire": "VIIRS_NOAA20_Thermal_Anomalies_375m_All,VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "haze": "MODIS_Combined_Value_Added_AOD,VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "flood": "MODIS_Terra_CorrectedReflectance_Bands721",
    "cyclone": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "default": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
}
def worldview(lat, lng, kind, day=None):
    if lat is None or lng is None:
        return None
    day = day or (TODAY - dt.timedelta(days=1)).isoformat()
    d = 2.5
    v = f"{lng - d:.2f},{lat - d:.2f},{lng + d:.2f},{lat + d:.2f}"
    layers = WV_LAYERS.get(kind, WV_LAYERS["default"])
    return f"https://worldview.earthdata.nasa.gov/?v={v}&l={layers}&t={day}-T12:00:00Z"

# ----------------------------------------------------------------- dedupe against curated alerts
def km(a, b):
    r = math.pi / 180
    dlat, dlng = (b[0] - a[0]) * r, (b[1] - a[1]) * r
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin(dlng / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))

def dedupe(auto, curated):
    keep = []
    for a in auto:
        dup = False
        for c in curated:
            same_kind = (a["type"] == c["type"]) or (a["type"] in ("flood", "rain") and c["type"] == "flood")
            if c["iso"] == a["iso"] and same_kind:
                if a["lat"] is None or km((a["lat"], a["lng"]), (c["lat"], c["lng"])) < 350:
                    dup = True
                    a["sameAs"] = c["id"]
                    break
        keep.append(a)
    return keep

# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="..")
    ap.add_argument("--prev", default=None)
    ap.add_argument("--skip", default="", help="comma list of sources to skip, e.g. gibs,firms")
    args = ap.parse_args()
    base = os.path.abspath(args.base)
    skip = set(s for s in args.skip.split(",") if s)
    prev = None
    if args.prev and os.path.exists(args.prev):
        prev = json.load(open(args.prev))

    geo = Geo(base)
    rep = Report()
    data = json.load(open(os.path.join(base, "data.json")))
    world = json.load(open(os.path.join(base, "countries.geojson")))
    country_names = {f["properties"]["name"]: f["properties"]["iso"] for f in world["features"] if f["properties"]["iso"] in TRACKED}
    country_names.update({"Singapore": "SGP", "Viet Nam": "VNM", "Dominican Republic": "DOM", "Brasil": "BRA", "México": "MEX", "Perú": "PER", "Panamá": "PAN"})

    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")}
    gd = [] if "gdacs" in skip else (rep.run("GDACS", gdacs, geo) or [])
    # multi-country droughts have no single point: place them at the country's centre
    centre = {}
    for f in world["features"]:
        g = shape(f["geometry"])
        if f["properties"]["iso"] in TRACKED and not g.is_empty:
            c = g.representative_point(); centre[f["properties"]["iso"]] = (round(c.y, 2), round(c.x, 2))
    for a in gd:
        if a["lat"] is None and a["iso"] in centre:
            a["lat"], a["lng"] = centre[a["iso"]]
            a["countryWide"] = True
    out["warnings"] = [] if "wmo" in skip else (rep.run("WMO official warnings", wmo, geo) or [])
    for w in out["warnings"]:
        w.setdefault("origin", "WMO")
    if "accuweather" not in skip:
        acc = rep.run("AccuWeather", accuweather, base, geo, os.environ.get("ACCU_KEY")) or []
        have = {(w["prov"], w["kind"]) for w in out["warnings"]}
        out["warnings"] += [w for w in acc if (w["prov"], w["kind"]) not in have]
    out["fires"] = None if "firms" in skip else rep.run("NASA FIRMS", firms, geo, os.environ.get("FIRMS_KEY"), prev)
    out["forecast"] = {} if "openmeteo" in skip else (rep.run("Open-Meteo", openmeteo, base) or {})
    out["storms"] = [] if "nhc" in skip else (rep.run("NOAA NHC", nhc, geo) or [])
    out["ems"] = [] if "ems" in skip else (rep.run("Copernicus EMS", ems, geo, country_names) or [])
    out["enso"] = None if "cpc" in skip else rep.run("NOAA CPC", cpc)
    out["satellite"] = {} if "gibs" in skip else (rep.run("NASA GIBS", gibs, base) or {})

    # carry 'firstSeen' so the page can flag what is new since yesterday
    seen_before = {a["id"]: a.get("firstSeen") for a in ((prev or {}).get("alerts") or [])}
    alerts = dedupe(gd, data["alerts"])
    for a in alerts:
        a["firstSeen"] = seen_before.get(a["id"]) or TODAY.isoformat()
        a["worldview"] = worldview(a["lat"], a["lng"], a["type"])
    for c in data["alerts"]:
        c_wv = worldview(c["lat"], c["lng"], c["type"], c["date"])
        out.setdefault("curatedLinks", {})[c["id"]] = {"worldview": c_wv}
    out["alerts"] = alerts
    out["sources"] = rep.sources

    json.dump(out, open(os.path.join(base, "auto.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    # human-readable summary for the daily run
    print("\n=== SUMMARY ===")
    for k, v in rep.sources.items():
        print(f"{k:24s} {'OK ' if v['ok'] else 'ERR'} {v.get('count', '')} {v.get('error', '')}")
    print("auto.json:", round(os.path.getsize(os.path.join(base, 'auto.json')) / 1024), "KB")

if __name__ == "__main__":
    main()
