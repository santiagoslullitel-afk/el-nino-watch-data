"""Empaqueta index.html + archivos de apoyo en un solo HTML autocontenido.
Uso: python3 bundle.py <carpeta_con_archivos> <salida.html>
La página no se modifica: un pequeño script previo sirve los archivos desde memoria
cuando el código pide fetch("data.json") o una imagen como "sat/truecolor.jpg".
"""
import base64, json, os, sys

base, out = sys.argv[1], sys.argv[2]
TEXT = ["data.json", "auto.json", "provinces.geojson", "countries.geojson", "cities.json", "accu_keys.json"]
IMG = {"earth-blue-marble.jpg": "image/jpeg", "earth-topology.png": "image/png",
       "sat/truecolor.jpg": "image/jpeg", "sat/smoke.png": "image/png",
       "sat/rain.png": "image/png", "sat/fires.png": "image/png"}

files = {}
for n in TEXT:
    p = os.path.join(base, n)
    if os.path.exists(p):
        files[n] = open(p, encoding="utf-8").read()
imgs = {}
for n, mime in IMG.items():
    p = os.path.join(base, n)
    if os.path.exists(p):
        imgs[n] = f"data:{mime};base64," + base64.b64encode(open(p, "rb").read()).decode()

def js(o):
    return json.dumps(o, ensure_ascii=False).replace("</", "<\\/")

shim = """<script>
(function(){
  var F = %s, I = %s;
  function key(u){ try { var s = String(u); s = s.replace(location.origin, "").replace(/^\\.?\\//, "").split("?")[0].split("#")[0]; return s; } catch(e){ return u; } }
  var of = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function(u, o){
    var k = key(u && u.url ? u.url : u);
    if (Object.prototype.hasOwnProperty.call(F, k)) return Promise.resolve(new Response(F[k], {status:200, headers:{"Content-Type":"application/json"}}));
    if (Object.prototype.hasOwnProperty.call(I, k)) return of(I[k]);
    if (/\\.(json|geojson)$/.test(k)) return Promise.resolve(new Response("null", {status:404}));
    return of(u, o);
  };
  var d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
  Object.defineProperty(HTMLImageElement.prototype, "src", {
    configurable: true, enumerable: d.enumerable,
    get: function(){ return d.get.call(this); },
    set: function(v){ var k = key(v); d.set.call(this, Object.prototype.hasOwnProperty.call(I, k) ? I[k] : v); }
  });
  var sa = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(n, v){
    if (this instanceof HTMLImageElement && String(n).toLowerCase() === "src") { var k = key(v); if (Object.prototype.hasOwnProperty.call(I, k)) v = I[k]; }
    return sa.call(this, n, v);
  };
})();
</script>
""" % (js(files), js(imgs))

head = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
page = open(os.path.join(base, "index.html"), encoding="utf-8").read()
open(out, "w", encoding="utf-8").write(head + shim + page)
print(out, round(os.path.getsize(out) / 1e6, 2), "MB;", len(files), "datos,", len(imgs), "imágenes")
