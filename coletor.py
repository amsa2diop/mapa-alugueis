"""Coletor de anúncios de aluguel (Rio de Janeiro + Niterói).

Fontes:
  - QuintoAndar: API pública de busca do site (completa a cada execução).
  - VivaReal (mesma base do ZAP): API "glue" do site. A cada hora busca os mais
    novos; a cada FULL_SWEEP_HORAS faz uma varredura completa para tirar os
    anúncios que saíram do ar.
  - OLX: melhor esforço. Tenta abrir a listagem uma vez; se o site bloquear
    (desafio anti-robô), desiste nessa rodada e mantém os dados anteriores.

Saída: dados/anuncios.json (estado) e site/dados.js (lido pela página do mapa).
"""
import json, os, re, sys, time, random, unicodedata, urllib.request, urllib.parse, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------- configuração ----------------
TOTAL_MAX = 200_000       # praticamente sem teto (só descarta preço absurdo); o filtro fica na página
MAX_FOTOS = 80          # todas as fotos; o site leva 4 no arquivo principal e o resto sob demanda
AREA_MIN = 0              # sem mínimo na coleta; o filtro de área fica na página
FULL_SWEEP_HORAS = 6      # VivaReal: varredura completa a cada N horas
OLX_VALIDADE_HORAS = 24 * 3   # OLX: some do mapa se não aparecer na varredura diária (3 dias de folga)
CIDADES_OK = {"rio de janeiro", "niteroi"}

BASE = Path(__file__).resolve().parent
DADOS = BASE / "dados"
SITE = BASE / "site"
ESTADO = DADOS / "anuncios.json"
GEOCACHE = DADOS / "geocache.json"
META = DADOS / "meta.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

def agora():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower().strip()

LOGFILE = DADOS / "coletor.log"

def log(*a):
    linha = " ".join([datetime.now().strftime("%Y-%m-%d %H:%M:%S")] + [str(x) for x in a])
    if sys.stdout:  # sob pythonw (agendador) não há console
        print(linha, flush=True)
    try:
        DADOS.mkdir(exist_ok=True)
        if LOGFILE.exists() and LOGFILE.stat().st_size > 2_000_000:
            LOGFILE.replace(LOGFILE.with_suffix(".log.1"))
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass

def http_json(url, headers=None, tentativas=3):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    for t in range(tentativas):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or t == tentativas - 1:
                raise
        except Exception:
            if t == tentativas - 1:
                raise
        time.sleep(3 * (t + 1))

def num(x):
    try:
        return float(x) if x not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0

def carregar(p, padrao):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return padrao

def salvar(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

def tipo_imovel(texto):
    """'Apartamento', 'Casa' ou None (comercial, terreno, quarto etc. ficam de fora)."""
    t = sem_acento(texto or "").replace("_", " ")
    if not t:
        return "Apartamento"
    if "comerc" in t or "office" in t or "sala" in t.split() or "loja" in t:
        return None
    if any(k in t for k in ("apart", "penthouse", "cobertura", "flat", "kitnet", "kitinete", "studio", "loft",
                            "conjugado", "duplex", "triplex")):
        return "Apartamento"
    if any(k in t for k in ("casa", "home", "sobrado", "village", "two story", "house", "condominium")):
        return "Casa"
    return None

# ---------------- QuintoAndar ----------------
QA_URL = "https://apigw.prod.quintoandar.com.br/house-listing-search/v2/search/list"
QA_CAMPOS = ["id", "location", "rent", "totalCost", "iptuPlusCondominium", "condominium", "iptu", "area",
             "bedrooms", "bathrooms", "parkingSpots", "address", "neighbourhood", "regionName", "city", "type",
             "imageList", "isFurnished", "shortRentDescription"]

def coletar_quintoandar():
    base = [(f"fields[{i}]", f) for i, f in enumerate(QA_CAMPOS)] + [
        ("context.deviceId", "buscador-local"), ("context.listShowing", "true"), ("context.mapShowing", "true"),
        ("context.numPhotos", "1"), ("context.isSSR", "false"), ("filters.businessContext", "RENT"),
        ("filters.location.coordinate.lat", "-22.92"), ("filters.location.coordinate.lng", "-43.30"),
        ("filters.location.viewport.east", "-42.95"), ("filters.location.viewport.north", "-22.74"),
        ("filters.location.viewport.south", "-23.10"), ("filters.location.viewport.west", "-43.80"),
        ("filters.location.countryCode", "BR"),
        # o "custo total" do QuintoAndar inclui seguro e taxa; buscamos com folga e filtramos depois
        ("filters.priceRange[0].costType", "TOTAL_COST"), ("filters.priceRange[0].range.min", "0"),
        ("filters.priceRange[0].range.max", str(TOTAL_MAX + 500)),
        ("filters.availability", "ANY"), ("filters.occupancy", "ANY"),
        ("filters.houseSpecs.houseTypes[0]", "APARTMENT"), ("filters.houseSpecs.houseTypes[1]", "HOUSE"),
        ("filters.origin", "HYBRID"),  # sem filtro de área no site: anúncios sem metragem também vêm
    ]
    hdr = {"Origin": "https://www.quintoandar.com.br", "Referer": "https://www.quintoandar.com.br/"}
    itens, offset, total = [], 0, None
    while True:
        q = base + [("pagination.pageSize", "500"), ("pagination.offset", str(offset))]
        d = http_json(QA_URL + "?" + urllib.parse.urlencode(q), hdr)
        hits = d["hits"]["hits"]
        total = d["hits"]["total"]["value"]
        for h in hits:
            s = h["_source"]
            loc = s.get("location") or {}
            if not loc.get("lat"):
                continue
            aluguel = num(s.get("rent"))
            condo, iptu = num(s.get("condominium")), num(s.get("iptu"))
            if not condo and not iptu:  # fallback: só veio a soma
                condo = num(s.get("iptuPlusCondominium"))
            fotos = [f"https://www.quintoandar.com.br/img/med/{f}" for f in (s.get("imageList") or [])[:MAX_FOTOS]]
            tipo = tipo_imovel(s.get("type"))
            if not tipo:
                continue
            itens.append({
                "id": f"qa-{s['id']}", "fonte": "QuintoAndar", "tipo": tipo,
                "url": f"https://www.quintoandar.com.br/imovel/{s['id']}/alugar",
                "lat": loc["lat"], "lng": loc["lon"], "aprox": False,
                "aluguel": aluguel, "condominio": condo, "iptu": iptu,
                "total": aluguel + condo, "total_site": num(s.get("totalCost")), "fotos": fotos,
                "area": num(s.get("area")), "quartos": int(num(s.get("bedrooms"))),
                "banheiros": int(num(s.get("bathrooms"))), "vagas": int(num(s.get("parkingSpots"))),
                "bairro": s.get("neighbourhood") or s.get("regionName") or "",
                "cidade": s.get("city") or "", "endereco": s.get("address") or "",
                "mobiliado": bool(s.get("isFurnished")),
                "descricao": (s.get("shortRentDescription") or "").strip(),
            })
        offset += len(hits)
        if not hits or offset >= total:
            break
        time.sleep(1.5)
    log(f"QuintoAndar: {len(itens)} anúncios (API informou {total})")
    return itens

# ---------------- VivaReal ----------------
VR_URL = "https://glue-api.vivareal.com/v2/listings"
VR_HDR = {"x-domain": ".vivareal.com.br", "Origin": "https://www.vivareal.com.br", "Referer": "https://www.vivareal.com.br/"}
VR_CIDADES = [("Rio de Janeiro", "BR>Rio de Janeiro>NULL>Rio de Janeiro"), ("Niterói", "BR>Rio de Janeiro>NULL>Niteroi")]
VR_CAMPOS = ("search(result(listings(listing(id,title,address,pricingInfos,usableAreas,bedrooms,bathrooms,"
             "parkingSpaces,createdAt,updatedAt,unitTypes,unitSubTypes),medias)),totalCount)")
VR_TAM = 30          # limite de página aceito pela API
VR_FROM_MAX = 990    # a API recusa "from" acima de ~1000

def vr_req(cidade, locid, frm, pmin=None, pmax=TOTAL_MAX, ordenar=False, campos=VR_CAMPOS, tam=VR_TAM):
    q = {"business": "RENTAL", "listingType": "USED", "unitTypes": "APARTMENT,HOME,CONDOMINIUM", "usageTypes": "RESIDENTIAL",
         "addressCity": cidade, "addressState": "Rio de Janeiro", "addressLocationId": locid,
         "addressCountry": "Brasil", "size": str(tam), "from": str(frm), "includeFields": campos,
         "categoryPage": "RESULT", "priceMax": str(pmax)}  # área filtrada aqui (mantém sem metragem)
    if pmin is not None:
        q["priceMin"] = str(pmin)
    if ordenar:
        q["sort"] = "createdAt DESC"
    d = http_json(VR_URL + "?" + urllib.parse.urlencode(q), VR_HDR)
    s = d["search"]
    return s.get("totalCount", 0), (s.get("result") or {}).get("listings", [])

def vr_item(x):
    L = x["listing"]
    a = L.get("address") or {}
    pt = a.get("point") or {}
    lat, lng = pt.get("lat"), pt.get("lon")
    if not lat and pt.get("approximateLat"):  # anunciante escondeu o endereço exato
        lat, lng = pt["approximateLat"], pt["approximateLon"]
        pt = {**pt, "aproximated": True}
    if not lat:
        return None
    p = next((p for p in L.get("pricingInfos") or [] if p.get("businessType") == "RENTAL"), None)
    if not p or (p.get("rentalInfo") or {}).get("period") in ("DAILY", "WEEKLY"):
        return None
    aluguel, condo, iptu = num(p.get("price")), num(p.get("monthlyCondoFee")), num(p.get("yearlyIptu")) / 12
    fotos = [m["url"].replace("{description}", "foto").replace("{action}", "crop")
             .replace("{width}", "640").replace("{height}", "420")
             for m in (x.get("medias") or []) if m.get("type") == "IMAGE" and m.get("url")][:MAX_FOTOS]
    q = (L.get("bedrooms") or [0])[0]
    ut = " ".join((L.get("unitTypes") or []) + (L.get("unitSubTypes") or []))
    tipo = tipo_imovel(ut) if ut else "Apartamento"
    if not tipo:
        return None
    return {
        "id": f"vr-{L['id']}", "fonte": "VivaReal", "tipo": tipo,
        "url": f"https://www.vivareal.com.br/imovel/{'casa' if tipo == 'Casa' else 'apartamento'}-{str(q) + '-quartos-' if q else ''}id-{L['id']}/",
        "lat": lat, "lng": lng, "aprox": bool(pt.get("aproximated")),
        "aluguel": aluguel, "condominio": condo, "iptu": round(iptu),
        "total": round(aluguel + condo), "area": num((L.get("usableAreas") or [0])[0]),
        "quartos": int(num(q)), "banheiros": int(num((L.get("bathrooms") or [0])[0])),
        "vagas": int(num((L.get("parkingSpaces") or [0])[0])),
        "bairro": a.get("neighborhood") or "", "cidade": a.get("city") or "",
        "endereco": " ".join(v for v in [a.get("street"), a.get("streetNumber")] if v),
        "fotos": fotos, "titulo": L.get("title") or "", "criado": L.get("createdAt"),
        "video": next((m["url"] for m in (x.get("medias") or []) if m.get("type") in ("VIDEO", "VIDEO_TOUR") and m.get("url")), None),
    }

def vr_faixa(cidade, locid, pmin, pmax, saida):
    """Coleta uma faixa de preço; divide ao meio se passar do limite de paginação."""
    total, _ = vr_req(cidade, locid, 0, pmin, pmax, campos="search(totalCount)", tam=1)
    if total > VR_FROM_MAX + VR_TAM and pmax - pmin > 50:
        meio = (pmin + pmax) // 2
        vr_faixa(cidade, locid, pmin, meio, saida)
        vr_faixa(cidade, locid, meio + 1, pmax, saida)
        return
    frm = 0
    while frm < min(total, VR_FROM_MAX + VR_TAM):
        _, ls = vr_req(cidade, locid, frm, pmin, pmax)
        for x in ls:
            it = vr_item(x)
            if it:
                saida[it["id"]] = it
        if not ls:
            break
        frm += VR_TAM
        time.sleep(random.uniform(0.8, 1.6))

def coletar_vivareal_completo():
    saida = {}
    for cidade, locid in VR_CIDADES:
        antes = len(saida)
        vr_faixa(cidade, locid, 0, TOTAL_MAX, saida)
        log(f"VivaReal completo {cidade}: {len(saida) - antes}")
    return list(saida.values())

def coletar_vivareal_novos(conhecidos):
    """Só os mais recentes: para quando uma página inteira já é conhecida."""
    saida = {}
    for cidade, locid in VR_CIDADES:
        for pag in range(10):
            _, ls = vr_req(cidade, locid, pag * VR_TAM, ordenar=True)
            novos = 0
            for x in ls:
                it = vr_item(x)
                if it:
                    if it["id"] not in conhecidos and passa_filtro(it):
                        novos += 1
                    saida[it["id"]] = it
            if not ls or novos == 0:
                break
            time.sleep(random.uniform(0.8, 1.6))
    log(f"VivaReal incremental: {len(saida)} vistos")
    return list(saida.values())

# ---------------- OLX (melhor esforço) ----------------
# Os dados de cada anúncio vêm no JSON embutido da página (fotos, condomínio, IPTU, área, data).
OLX_BASE = "https://www.olx.com.br/imoveis/aluguel/apartamentos/estado-rj/rio-de-janeiro-e-regiao/"
OLX_BASE_CASAS = "https://www.olx.com.br/imoveis/aluguel/casas/estado-rj/rio-de-janeiro-e-regiao/"
# no OLX o município do Rio fica em ".../centro"; Niterói em ".../niteroi"
OLX_MUNICIPIOS = [("centro", 4, 25), ("niteroi", 2, 11)]   # (caminho, páginas por hora, páginas na varredura diária)
# bairros com endereço próprio no OLX (Zona Sul, Centro, Niterói); a Zona Norte não tem
OLX_BAIRROS = ["zona-sul/copacabana", "zona-sul/botafogo", "zona-sul/flamengo", "zona-sul/laranjeiras",
               "zona-sul/catete", "zona-sul/gloria", "zona-sul/humaita", "zona-sul/lagoa", "zona-sul/jardim-botanico",
               "zona-sul/leme", "zona-sul/ipanema", "zona-sul/leblon", "centro/centro", "centro/santa-teresa",
               "niteroi/centro", "niteroi/inga", "niteroi/icarai"]
# bairros sem endereço próprio: busca por texto dentro do município do Rio
OLX_BUSCAS = ["tijuca", "maracana", "praca da bandeira", "rio comprido", "estacio", "lapa", "catumbi"]
OLX_VARREDURA_HORAS = 24
OLX_PAGINAS_BAIRRO = 8

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')

def _fecha(s, j, abre, fecha):
    d, k = 0, j
    while k < len(s):
        ch = s[k]
        if ch == '"':
            k += 1
            while k < len(s) and s[k] != '"':
                k += 2 if s[k] == "\\" else 1
        elif ch == abre:
            d += 1
        elif ch == fecha:
            d -= 1
            if d == 0:
                return k
        k += 1
    return -1

def olx_anuncios_da_pagina(html):
    texto = ""
    for m in _PUSH.finditer(html):
        try:
            texto += json.loads(m.group(1))
        except Exception:
            pass
    out, i = [], texto.find('"ads":[')
    while i >= 0:
        j = i + 6
        k = _fecha(texto, j, "[", "]")
        if k < 0:
            break
        try:
            out += [a for a in json.loads(texto[j:k + 1]) if isinstance(a, dict) and a.get("listId")]
        except Exception:
            pass
        i = texto.find('"ads":[', k)
    return out

def _brl(s):
    return num(re.sub(r"[^\d,]", "", s or "").replace(",", ".")) if s else 0.0

def olx_item(a):
    props = {p.get("name"): p.get("value") for p in a.get("properties") or []}
    loc = a.get("locationDetails") or {}
    cidade, bairro = loc.get("municipality") or "", loc.get("neighbourhood") or ""
    aluguel = _brl(a.get("price") or a.get("priceValue"))
    if not aluguel:
        return None
    tipo = tipo_imovel(a.get("category") or props.get("category") or props.get("real_estate_type"))
    if not tipo:
        return None
    condo, iptu = _brl(props.get("condominio")), _brl(props.get("iptu"))
    area = num(re.sub(r"[^\d]", "", props.get("size") or ""))
    fotos = [im.get("originalWebp") or im.get("original") for im in a.get("images") or [] if im.get("original")][:MAX_FOTOS]
    data = a.get("date")
    return {
        "id": f"olx-{a['listId']}", "fonte": "OLX", "tipo": tipo, "url": a.get("url"), "aprox": True,
        "aluguel": aluguel, "condominio": condo, "iptu": iptu, "total": aluguel + condo, "area": area,
        "quartos": int(num(props.get("rooms"))), "banheiros": int(num(props.get("bathrooms"))),
        "vagas": int(num(props.get("garage_spaces"))), "bairro": bairro, "cidade": cidade, "endereco": "",
        "fotos": fotos, "titulo": (a.get("subject") or "")[:140],
        "criado": iso(datetime.fromtimestamp(data, timezone.utc)) if isinstance(data, (int, float)) else None,
        "video": a.get("url") if a.get("videoCount") else None,
    }

def geocodificar(bairro, cidade, cache):
    chave = f"{sem_acento(bairro)}|{sem_acento(cidade)}"
    if chave in cache:
        return cache[chave]
    q = urllib.parse.urlencode({"q": f"{bairro}, {cidade}, RJ, Brasil", "format": "json", "limit": 1, "countrycodes": "br"})
    try:
        r = http_json("https://nominatim.openstreetmap.org/search?" + q,
                      {"User-Agent": "buscador-apartamentos-local/1.0"}, tentativas=1)
        cache[chave] = [float(r[0]["lat"]), float(r[0]["lon"])] if r else None
    except Exception:
        return None  # não grava no cache; tenta de novo na próxima rodada
    time.sleep(1.1)  # política de uso do Nominatim: 1 requisição/segundo
    return cache[chave]

def _jitter(chave, escala=0.0018):
    """Deslocamento fixo por anúncio (não muda a cada hora) para espalhar pinos do mesmo bairro."""
    import hashlib, math
    h = int(hashlib.md5(chave.encode()).hexdigest()[:12], 16)
    ang = (h % 3600) / 3600 * 2 * math.pi
    r = math.sqrt(((h // 3600) % 1000) / 1000) * escala
    return r * math.sin(ang), r * math.cos(ang)

def olx_paginas(varredura):
    """Lista de (rótulo, url, máx. de páginas)."""
    filtro = f"pe={TOTAL_MAX}&sf=1"  # sem "ss": o OLX tiraria os anúncios sem metragem
    out = [(m, f"{OLX_BASE}{m}?{filtro}", deep if varredura else hora) for m, hora, deep in OLX_MUNICIPIOS]
    if varredura:
        out += [(b, f"{OLX_BASE}{b}?{filtro}", OLX_PAGINAS_BAIRRO) for b in OLX_BAIRROS]
        out += [(f"busca:{q}", f"{OLX_BASE}centro?{filtro}&q={urllib.parse.quote(q)}", 4) for q in OLX_BUSCAS]
    # casas: volume bem menor; 1 página por hora, mais na varredura diária
    out += [(f"casas:{m}", f"{OLX_BASE_CASAS}{m}?{filtro}", (deep // 3 or 1) if varredura else 1) for m, hora, deep in OLX_MUNICIPIOS]
    if varredura:
        out += [(f"casas:{b}", f"{OLX_BASE_CASAS}{b}?{filtro}", 3) for b in OLX_BAIRROS]
    return out

OLX_DETALHES_POR_RODADA = 12   # a página do anúncio bloqueia mais rápido que a lista; poucos e devagar

def olx_detalhe(html_txt):
    """Da página do anúncio: centro do mapa (CEP), rua, descrição e fotos."""
    import html as H
    h = H.unescape(html_txt)
    out = {}
    la, lo = re.search(r'"mapLati":(-?[\d.]+)', h), re.search(r'"mapLong":(-?[\d.]+)', h)
    if la and lo:
        out["lat"], out["lng"] = float(la.group(1)), float(lo.group(1))
    m = re.search(r'"address":"([^"]{3,120})","neighbourhood"', h)
    if m:
        out["endereco"] = m.group(1)
    for bloco in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html_txt, re.S):
        try:
            j = json.loads(bloco)
        except Exception:
            continue
        for o in (j if isinstance(j, list) else [j]):
            if isinstance(o, dict) and o.get("description") and o.get("image"):
                out["descricao"] = re.sub(r"<br\s*/?>", "\n", o["description"]).strip()
                ims = o["image"] if isinstance(o["image"], list) else [o["image"]]
                out["fotos"] = [i.get("contentUrl") if isinstance(i, dict) else i for i in ims if i][:MAX_FOTOS]
    return out

def coletar_olx(varredura=False, detalhados=frozenset()):
    try:
        import os
        # navegador dentro da pasta do projeto (o AppData do app do Claude é virtualizado)
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / ".navegadores"))
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("OLX: Playwright não instalado; pulando")
        return None, False
    brutos, bloqueado, paginas = {}, False, 0
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chromium")
        except Exception:
            b = p.chromium.launch(headless=True)
        pg = b.new_context(locale="pt-BR", viewport={"width": 1366, "height": 900}, user_agent=UA).new_page()
        for rotulo, base, maxp in olx_paginas(varredura):
            for n in range(1, maxp + 1):
                url = base + (f"&o={n}" if n > 1 else "")
                try:
                    r = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                    pg.wait_for_timeout(2500)
                except Exception as e:
                    log("OLX: erro ao abrir", url, repr(e)[:100])
                    break
                if (r and r.status >= 400) or "Attention Required" in pg.title() or "Just a moment" in pg.title():
                    log(f"OLX: bloqueado pelo site (HTTP {r.status if r else '?'}) após {paginas} páginas")
                    bloqueado = True
                    break
                paginas += 1
                ads = olx_anuncios_da_pagina(pg.content())
                novos = sum(1 for a in ads if a["listId"] not in brutos)
                for a in ads:
                    brutos[a["listId"]] = a
                if len(ads) < 30 or (n > 1 and novos == 0):  # última página ou repetiu
                    break
                time.sleep(random.uniform(1.5, 3.0))
            if bloqueado:
                break
        # anúncios ainda sem detalhe: abre a página de cada um (poucos por rodada)
        detalhes = {}
        if not bloqueado:
            fila = [a for a in brutos.values() if f"olx-{a['listId']}" not in detalhados
                    and sem_acento((a.get("locationDetails") or {}).get("municipality")) in CIDADES_OK]
            for a in fila[:OLX_DETALHES_POR_RODADA]:
                try:
                    r = pg.goto(a["url"], wait_until="domcontentloaded", timeout=45000)
                    pg.wait_for_timeout(1500)
                except Exception:
                    continue
                if (r and r.status >= 400) or "Attention Required" in pg.title() or "Just a moment" in pg.title():
                    log("OLX: bloqueado ao abrir anúncios; detalhes param aqui")
                    break
                d = olx_detalhe(pg.content())
                if d:
                    detalhes[f"olx-{a['listId']}"] = d
                time.sleep(random.uniform(6.0, 12.0))
            log(f"OLX: detalhes lidos de {len(detalhes)} anúncios (faltavam {len(fila)})")
        b.close()
    if not brutos:
        return None, bloqueado
    cache = carregar(GEOCACHE, {})
    itens = []
    for a in brutos.values():
        it = olx_item(a)
        if not it or not it["bairro"] or sem_acento(it["cidade"]) not in CIDADES_OK:
            continue
        g = geocodificar(it["bairro"], it["cidade"], cache)
        if not g:
            continue
        dy, dx = _jitter(it["id"])
        it["lat"], it["lng"] = g[0] + dy, g[1] + dx
        d = detalhes.get(it["id"])
        if d:
            it.update({k: v for k, v in d.items() if v})
            it["det"] = True
        itens.append(it)
    salvar(GEOCACHE, cache)
    log(f"OLX{' (varredura)' if varredura else ''}: {paginas} páginas, {len(brutos)} anúncios, {len(itens)} no Rio/Niterói com local")
    return itens, bloqueado

# ---------------- bairros do perímetro (para fontes que buscam por bairro) ----------------
BAIRROS_BUSCA = {
    "rio-de-janeiro": ["tijuca", "praca-da-bandeira", "maracana", "rio-comprido", "estacio", "cidade-nova", "centro",
                       "lapa", "santa-teresa", "gloria", "catete", "flamengo", "laranjeiras", "cosme-velho", "botafogo",
                       "humaita", "urca", "leme", "copacabana", "ipanema", "leblon", "lagoa", "jardim-botanico"],
    "niteroi": ["centro", "inga", "sao-domingos", "icarai", "boa-viagem", "gragoata"],
}
CIDADE_NOME = {"rio-de-janeiro": "Rio de Janeiro", "niteroi": "Niterói"}
BAIRROS_BUSCA_FIXOS = {k: list(v) for k, v in BAIRROS_BUSCA.items()}

def _flight(html):
    out = []
    for m in _PUSH.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:
            pass
    return "".join(out)

def _objeto(txt, ini):
    """Objeto JSON que começa em txt[ini] == '{'."""
    k = _fecha(txt, ini, "{", "}")
    return json.loads(txt[ini:k + 1]) if k > 0 else None

# ---------------- Chaves na Mão (agrega muitas imobiliárias pequenas) ----------------
CNM_BASE = "https://www.chavesnamao.com.br/apartamentos-para-alugar/"
CNM_TIPOS = {"Apartamento": "https://www.chavesnamao.com.br/apartamentos-para-alugar/",
             "Casa": "https://www.chavesnamao.com.br/casas-para-alugar/"}
CNM_IMG = "https://www.chavesnamao.com.br/imn/0640X0480/N/80/imoveis/"

def _get_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf8", "replace")

def cnm_pagina(url):
    t = _flight(_get_html(url))
    total = re.search(r'"totalResults":(\d+)', t)
    total = int(total.group(1)) if total else 0
    # imagem e coordenada vêm na lista de pontos do mapa
    pontos = {}
    for m in re.finditer(r'\{"id":(\d+),"lat":', t):
        o = _objeto(t, m.start())
        if o:
            pontos[o["id"]] = o
    # as fotos de cada anúncio ficam num objeto "pictures" separado; o caminho traz o id do anúncio
    fotos = {}
    for m in re.finditer(r'"pictures":\{', t):
        try:
            o = _objeto(t, m.start() + len('"pictures":'))
        except Exception:
            o = None
        lst = _cnm_todas((o or {}).get("list") or [], (o or {}).get("count"))
        if lst:
            partes = lst[0].split("/")
            if len(partes) > 1 and partes[1].isdigit():
                fotos[int(partes[1])] = [CNM_IMG + f for f in lst]
    anuncios, vistos = [], set()
    for m in re.finditer(r'"prices":\{"main"', t):
        ini = _inicio_objeto(t, m.start())   # objeto do anúncio que contém "prices"
        o = _objeto(t, ini) if ini is not None else None
        if o and o.get("id") not in vistos and o.get("transaction") in ("RENT", "rent") and o.get("location"):
            vistos.add(o.get("id"))
            if isinstance(o.get("pictures"), dict) and o["pictures"].get("list"):
                o["_fotos"] = [CNM_IMG + f for f in _cnm_todas(o["pictures"]["list"], o["pictures"].get("count"))]
            elif o.get("id") in fotos:
                o["_fotos"] = fotos[o["id"]]
            d = o.get("descriptionRaw") or o.get("description")
            o["_descricao"] = _texto_ref(t, d) if isinstance(d, str) else ""
            anuncios.append(o)
    return total, anuncios, pontos

def _cnm_todas(lst, total):
    """A busca traz só as 5 primeiras fotos, mas os nomes seguem -1.jpg, -2.jpg, ... até o total informado."""
    if lst and total and total > len(lst):
        m = re.match(r"(.*-)(\d+)(\.\w+)$", lst[-1])
        if m and int(m.group(2)) == len(lst):
            lst = lst + [f"{m.group(1)}{k}{m.group(3)}" for k in range(len(lst) + 1, min(total, MAX_FOTOS) + 1)]
    return lst

def _texto_ref(t, d):
    """Textos longos do Next.js vêm como referência "$105" para uma linha "105:T<tam>,<texto>"."""
    if not d.startswith("$"):
        return d
    m = re.search(r"(?<![0-9a-zA-Z])" + re.escape(d[1:]) + r":T([0-9a-f]+),", t)
    if not m:
        return ""
    bruto = t[m.end():m.end() + int(m.group(1), 16)]
    return bruto.encode("utf-8")[:int(m.group(1), 16)].decode("utf-8", "ignore")

def _inicio_objeto(txt, i):
    """Posição do '{' do objeto que contém a posição i (ignora chaves dentro de strings de forma simples)."""
    d, k = 0, i
    while k > 0:
        c = txt[k]
        if c == "}":
            d += 1
        elif c == "{":
            if d == 0:
                return k
            d -= 1
        k -= 1
    return None

def _cnt(x):
    return num(x.get("count")) if isinstance(x, dict) else num(x) if isinstance(x, (int, float, str)) and not str(x).startswith("$") else 0

def _d(x):
    return x if isinstance(x, dict) else {}

def cnm_item(o, ponto, cidade):
    pr = _d(o.get("prices"))
    aluguel = num(pr.get("rawPrice")) or _brl(pr.get("main"))
    if not aluguel:
        return None
    loc = _d(o.get("location"))
    geo = _d(loc.get("geoposition"))
    publico = loc.get("publicAddress") and geo.get("lat")
    if not publico:
        geo = _d(_d(loc.get("neighborhood")).get("geoposition"))
    lat = num(geo.get("lat")) or (ponto or {}).get("lat")
    lng = num(geo.get("lon")) or (ponto or {}).get("lon")
    prev = _d(_d(ponto).get("listingPreview"))
    foto = prev.get("image")
    return {
        "id": f"cnm-{o['id']}", "fonte": "Chaves na Mão", "tipo": tipo_imovel(_d(o.get("realtyType")).get("name") or prev.get("realtyType")) or "Apartamento", "url": "https://www.chavesnamao.com.br" + (o.get("url") or ""),
        "lat": lat or None, "lng": lng or None, "aprox": not publico,
        "aluguel": aluguel, "condominio": _brl(pr.get("condominiumFee")), "iptu": _brl(pr.get("iptuValue")),
        "total": aluguel + _brl(pr.get("condominiumFee")),
        "area": num(_d(o.get("area")).get("useful") or _d(o.get("area")).get("total") or prev.get("area")),
        "quartos": int(_cnt(o.get("bedrooms")) or num(prev.get("bedrooms"))), "banheiros": int(_cnt(o.get("bathrooms")) or num(prev.get("bathrooms"))),
        "vagas": int(_cnt(o.get("garages")) or num(prev.get("garages"))),
        "bairro": _d(loc.get("neighborhood")).get("name") or "", "cidade": _d(loc.get("city")).get("name") or cidade,
        "endereco": (_d(loc.get("street")).get("name") or "") if publico else "",
        "fotos": (o.get("_fotos") or ([CNM_IMG + foto] if foto else []))[:MAX_FOTOS], "titulo": (o.get("title") or "")[:140],
        "descricao": (o.get("_descricao") or "").strip(),
        "anunciante": (_d(o.get("publisher")).get("name") or "").strip(),
        "video": _d(o.get("media")).get("videoUrl") or None,
    }

def coletar_chavesnamao():
    itens, paginas = {}, 0
    for cid, bairros in BAIRROS_BUSCA.items():
        for b in bairros:
          for base_tipo in CNM_TIPOS.values():
            base = f"{base_tipo}rj-{cid}/{b}/"
            pg, total = 1, None
            while True:
                url = base + (f"?pg={pg}" if pg > 1 else "")
                try:
                    total, anuncios, pontos = cnm_pagina(url)
                except Exception as e:
                    log("Chaves na Mão: erro em", url, repr(e)[:100])
                    break
                paginas += 1
                for o in anuncios:
                    it = cnm_item(o, pontos.get(o["id"]), CIDADE_NOME[cid])
                    if it:
                        itens[it["id"]] = it
                if not anuncios or pg * 30 >= total or pg >= 40:
                    break
                pg += 1
                time.sleep(random.uniform(0.8, 1.5))
            time.sleep(random.uniform(0.5, 1.0))
    log(f"Chaves na Mão: {paginas} páginas, {len(itens)} anúncios")
    return list(itens.values())

# ---------------- Imovelweb (melhor esforço; bloqueia robôs às vezes) ----------------
IW_BASE = "https://www.imovelweb.com.br/apartamentos-aluguel-"
IW_TIPOS = {"Apartamento": "https://www.imovelweb.com.br/apartamentos-aluguel-", "Casa": "https://www.imovelweb.com.br/casas-aluguel-"}
JS_IW = r"""
() => [...document.querySelectorAll('[data-qa="posting PROPERTY"]')].map(c => {
  const q = s => { const e = c.querySelector(`[data-qa="${s}"]`); return e ? e.innerText : ''; };
  const fotos = [...c.querySelectorAll('img')].map(i => i.getAttribute('src') || i.getAttribute('data-flickity-lazyload') || '')
     .filter(u => u.includes('imovelwebcdn.com/avisos'));
  const end = c.querySelector('[class*="location-address"]');
  return {id: c.dataset.id, url: (c.dataset.toPosting || '').split('?')[0], preco: q('POSTING_CARD_PRICE'),
          feat: q('POSTING_CARD_FEATURES'), local: q('POSTING_CARD_LOCATION'), endereco: end ? end.innerText : '',
          anunciante: (c.querySelector('[data-qa="POSTING_CARD_PUBLISHER"] img') || {}).alt || '', fotos: [...new Set(fotos)]};
})
"""

def iw_item(c, cidade):
    precos = re.findall(r"R\$\s*([\d\.]+)", c.get("preco") or "")
    if not precos:
        return None
    aluguel = _brl(precos[0])
    mc = (re.search(r"R\$\s*([\d\.]+)\s*Condom", c.get("preco") or "", re.I)
          or re.search(r"Cond\w*\.?\s*:?\s*R\$\s*([\d\.]+)", c.get("preco") or "", re.I))
    feat = c.get("feat") or ""
    ma = re.search(r"(\d+)\s*m²\s*(?:útil|tot)", feat) or re.search(r"(\d+)\s*m²", feat)
    mq = re.search(r"(\d+)\s*qu", feat)
    mb = re.search(r"(\d+)\s*ban", feat)
    mv = re.search(r"(\d+)\s*vaga", feat)
    local = [p.strip() for p in (c.get("local") or "").split(",")]
    bairro = local[0] if local else ""
    fotos = [f.replace("/360x266/", "/720x532/") for f in c.get("fotos") or []][:MAX_FOTOS]
    return {
        "id": f"iw-{c['id']}", "fonte": "Imovelweb", "tipo": c.get("_tipo") or "Apartamento", "url": "https://www.imovelweb.com.br" + c["url"], "aprox": True,
        "aluguel": aluguel, "condominio": _brl(mc.group(1)) if mc else 0.0, "iptu": 0.0,
        "total": aluguel + (_brl(mc.group(1)) if mc else 0.0), "area": num(ma.group(1)) if ma else 0.0,
        "quartos": int(mq.group(1)) if mq else 0, "banheiros": int(mb.group(1)) if mb else 0,
        "vagas": int(mv.group(1)) if mv else 0, "bairro": bairro, "cidade": cidade,
        "endereco": (c.get("endereco") or "").strip(), "fotos": fotos, "titulo": "",
        "anunciante": c.get("anunciante") or "",
    }

def coletar_imovelweb():
    try:
        import os
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BASE / ".navegadores"))
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, False
    brutos, bloqueado, paginas = [], False, 0
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="chromium")
        except Exception:
            b = p.chromium.launch(headless=True)
        pg = b.new_context(locale="pt-BR", viewport={"width": 1366, "height": 900}, user_agent=UA).new_page()
        for cid, bairros in BAIRROS_BUSCA.items():
            for bai in bairros:
              for tipo_iw, base_iw in IW_TIPOS.items():
                if bloqueado:
                    break
                for n in range(1, 11):
                    url = f"{base_iw}{bai}-{cid}" + (f"-pagina-{n}" if n > 1 else "") + ".html"
                    try:
                        r = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                        pg.wait_for_timeout(2500)
                    except Exception as e:
                        log("Imovelweb: erro ao abrir", url, repr(e)[:80]); break
                    if (r and r.status == 403) or "Just a moment" in pg.title() or "Attention" in pg.title():
                        log(f"Imovelweb: bloqueado após {paginas} páginas"); bloqueado = True; break
                    if r and r.status >= 400:
                        break
                    paginas += 1
                    cards = pg.evaluate(JS_IW)
                    for c in cards:
                        c["_cidade"] = CIDADE_NOME[cid]
                        c["_tipo"] = tipo_iw
                    brutos += cards
                    if len(cards) < 20:
                        break
                    time.sleep(random.uniform(1.5, 3.0))
                if bloqueado:
                    break
            if bloqueado:
                break
        b.close()
    if not brutos:
        return None, bloqueado
    cache = carregar(GEOCACHE, {})
    itens = {}
    for c in brutos:
        it = iw_item(c, c["_cidade"])
        if not it:
            continue
        g = geocodificar(it["bairro"], it["cidade"], cache) if it["bairro"] else None
        if g:
            dy, dx = _jitter(it["id"])
            it["lat"], it["lng"] = g[0] + dy, g[1] + dx
        else:
            it["lat"] = it["lng"] = None
        itens[it["id"]] = it
    salvar(GEOCACHE, cache)
    log(f"Imovelweb: {paginas} páginas, {len(itens)} anúncios")
    return list(itens.values()), bloqueado

# ---------------- Imobiliárias de Niterói com site próprio (plataforma Kenlo) ----------------
# Um mesmo leitor serve para todas as imobiliárias que usam o Kenlo.
KENLO_SITES = {
    "Kiffer Imóveis": "https://www.kifferimoveis.com.br",
    "Accioly Group": "https://www.acciolygroup.com.br",
    "Coluna Imóveis": "https://www.colunaimoveis.com.br",
    "Marchipp Imóveis": "https://www.marchippimoveis.com.br",
    "AST Imóveis": "https://www.astimoveis.com.br",
    "Movv": "https://www.movv.imb.br",
    "Predial Invest": "https://www.predialinvest.com.br",
}
KENLO_HORAS = 6

def kenlo_pagina(html):
    out, i = [], html.find('"index":"prd_sites_listings_v2"')
    while i >= 0:
        ini = _inicio_objeto(html, i)
        fim = _fecha(html, ini, "{", "}") if ini is not None else -1
        if fim > 0:
            try:
                out.append(json.loads(html[ini:fim + 1]))
            except Exception:
                pass
        i = html.find('"index":"prd_sites_listings_v2"', max(i + 1, fim))
    return out

def _faixa(v):
    return num(v[0]) if isinstance(v, list) and v else num(v)

def kenlo_item(o, imob, site):
    aluguel = _faixa(o.get("rent_price"))
    tipo = tipo_imovel(o.get("property_type") or "")
    if not aluguel or not tipo:
        return None
    iptu = num(o.get("property_tax"))
    if o.get("property_tax_payment") == "YEARLY":
        iptu /= 12
    fotos = [p.get("picture_full") or p.get("picture_thumb") for p in o.get("photos") or [] if p.get("picture_full") or p.get("picture_thumb")]
    ref = o.get("property_full_reference") or o.get("url")
    # o "total_rent" do Kenlo = aluguel + condomínio + IPTU mensal
    condo = num(o.get("condo_fees")) or max(0.0, round(num(o.get("total_rent")) - aluguel - iptu, 2))
    rua = (o.get("full_address") or "").split(" - ")[0].strip()
    return {
        "id": f"imob-{sem_acento(imob).replace(' ', '')}-{ref}", "fonte": "Imobiliárias", "tipo": tipo, "anunciante": imob,
        "url": site + (o.get("url") or ""), "aprox": True,
        "aluguel": aluguel, "condominio": condo, "iptu": round(iptu),
        "total": aluguel + condo, "area": _faixa(o.get("area")),
        "quartos": int(_faixa(o.get("bedrooms"))), "banheiros": int(_faixa(o.get("bathrooms"))),
        "vagas": int(_faixa(o.get("garages"))), "bairro": o.get("neighborhood_display") or o.get("neighborhood") or "",
        "cidade": o.get("city") or "", "endereco": rua, "fotos": fotos[:MAX_FOTOS],
        "titulo": (o.get("heading1") or o.get("website_title") or "")[:140],
        "descricao": (o.get("listing_description") or "").replace("**", "").strip(),
        "video": (o.get("video_url") or "").strip() or None,
    }

def coletar_imobiliarias():
    cache = carregar(GEOCACHE, {})
    itens, paginas, falhas = {}, 0, []
    for imob, site in KENLO_SITES.items():
        antes, pular = len(itens), set()
        for n, cat in [(n, cat) for cat in ("apartamento", "casa") for n in range(1, 41)]:
            if cat in pular:
                continue
            url = f"{site}/imoveis/para-alugar/{cat}/niteroi" + (f"?pagina={n}" if n > 1 else "")
            try:
                objs = kenlo_pagina(_get_html(url))
            except urllib.error.HTTPError as e:
                if e.code == 404:   # acabaram as páginas (ou a imobiliária não tem essa categoria)
                    pular.add(cat); continue
                falhas.append(imob); log(f"Imobiliárias: {imob} falhou em {url}: HTTP {e.code}")
                pular.add(cat); continue
            except Exception as e:
                falhas.append(imob); log(f"Imobiliárias: {imob} falhou em {url}: {repr(e)[:80]}")
                pular.add(cat); continue
            paginas += 1
            novos = 0
            for o in objs:
                it = kenlo_item(o, imob, site)
                if it and it["id"] not in itens:
                    novos += 1
                    itens[it["id"]] = it
            if len(objs) < 12 or novos == 0:
                pular.add(cat); continue
            time.sleep(random.uniform(0.6, 1.2))
        log(f"Imobiliárias: {imob}: {len(itens) - antes} apartamentos para alugar em Niterói")
    for it in itens.values():
        g = geocodificar(it["bairro"], it["cidade"], cache) if it["bairro"] else None
        if g:
            dy, dx = _jitter(it["id"])
            it["lat"], it["lng"] = g[0] + dy, g[1] + dx
        else:
            it["lat"] = it["lng"] = None
    salvar(GEOCACHE, cache)
    log(f"Imobiliárias: {paginas} páginas, {len(itens)} anúncios")
    return list(itens.values()), falhas

# ---------------- Self Spin (maior imobiliária de Niterói; site na plataforma Arbo) ----------------
# A busca da cidade traz todos os pontos (código, aluguel, coordenada); o detalhe de cada imóvel
# vem da página dele. Só abrimos o detalhe de quem está no perímetro e na faixa de valor.
SELFSPIN = "https://www.selfspin.com.br"

def _next_data(url):
    h = _get_html(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    return json.loads(m.group(1))["props"]["pageProps"] if m else {}

def selfspin_item(v):
    aluguel = num(v.get("imv_preco_locacao"))
    tipo = tipo_imovel((v.get("tipo_imovel") or {}).get("timv_nome") or "")
    if not aluguel or not tipo:
        return None
    loc = v.get("imv_location") or {}
    fotos = [f.get("imvft_url") for f in v.get("fotos") or [] if f.get("imvft_url")]
    oculto = bool(v.get("imv_hide_address_wl"))
    return {
        "id": f"imob-selfspin-{v.get('imv_cod_gaia')}", "fonte": "Imobiliárias", "tipo": tipo, "anunciante": "Self Spin",
        "url": v.get("url_amiga") or f"{SELFSPIN}/imovel/apartamento/locacao/niteroi/rj/x/{v.get('imv_cod_gaia')}",
        "lat": loc.get("lat"), "lng": loc.get("lng"), "aprox": oculto,
        "aluguel": aluguel, "condominio": num(v.get("imv_preco_cond")), "iptu": num(v.get("imv_preco_iptu")),
        "total": aluguel + num(v.get("imv_preco_cond")), "area": num(v.get("imv_area_util") or v.get("imv_area_total")),
        "quartos": int(num(v.get("imv_qtd_dorm"))), "banheiros": int(num(v.get("imv_qtd_banheiros"))),
        "vagas": int(num(v.get("imv_qtd_vagas"))), "bairro": v.get("imv_bairro") or "", "cidade": v.get("imv_cidade") or "Niterói",
        "endereco": "" if oculto else " ".join(str(p) for p in (v.get("imv_endereco"), v.get("imv_end_numero")) if p),
        "fotos": fotos[:MAX_FOTOS], "titulo": (v.get("imv_titulo") or "")[:140], "criado": v.get("createdAt"),
        "descricao": (v.get("imv_obs") or "").strip(),
        "video": (v.get("imv_videourl") or "").strip() or None,
    }

def coletar_selfspin():
    perim = ler_perimetro()
    pontos = _next_data(f"{SELFSPIN}/imoveis/para-alugar/niteroi-rj").get("locations") or []
    alvo = [p for p in pontos if num(p.get("imv_preco_locacao")) and num(p["imv_preco_locacao"]) <= TOTAL_MAX
            and (p.get("imv_location") or {}).get("lat")
            and no_perimetro(p["imv_location"]["lat"], p["imv_location"]["lng"], perim)]
    itens = []
    for p in alvo:
        try:
            v = _next_data(f"{SELFSPIN}/imovel/apartamento/locacao/niteroi/rj/x/{p['imv_cod_gaia']}").get("imovel") or {}
        except Exception as e:
            log("Self Spin: erro no detalhe", p.get("imv_cod_gaia"), repr(e)[:80])
            continue
        it = selfspin_item(v)
        if it:
            itens.append(it)
        time.sleep(random.uniform(0.5, 1.0))
    log(f"Self Spin: {len(pontos)} imóveis no site, {len(alvo)} no perímetro até R$ {TOTAL_MAX}, {len(itens)} apartamentos")
    return itens

# ---------------- Loft (marketplace; volume pequeno no RJ) ----------------
LOFT = "https://loft.com.br"
LOFT_HORAS = 3
LOFT_BUSCAS = [(t, c, nome) for t in ("apartamentos", "casas")
               for c, nome in (("rj/rio-de-janeiro", "Rio de Janeiro"), ("rj/niteroi", "Niterói"))]

def loft_pagina(url):
    h = _get_html(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    if not m:
        return [], 0
    for q in json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]:
        if q["queryKey"] and q["queryKey"][0] == "Listing:Search":
            d = q["state"].get("data") or {}
            return [x.get("listing") or {} for x in d.get("listings") or []], (d.get("pagination") or {}).get("totalPages") or 0
    return [], 0

def loft_item(L, cidade):
    if L.get("transactionType") != "FOR_RENT" or (L.get("rentalPeriod") or "MONTHLY") != "MONTHLY":
        return None
    aluguel = num(L.get("rentalPrice"))
    tipo = {"apartment": "Apartamento", "house": "Casa"}.get(L.get("homeType")) or tipo_imovel(L.get("propertyType") or "")
    if not aluguel or not tipo:
        return None
    a = L.get("address") or {}
    geo = L.get("_geoloc") or {}
    condo = num(L.get("complexFee"))
    return {
        "id": f"loft-{L['id']}", "fonte": "Loft", "tipo": tipo, "anunciante": L.get("agencyName") or "",
        "url": f"{LOFT}/imovel/{'casa' if tipo == 'Casa' else 'apartamento'}/{L['id']}?tipoTransacao=aluguel",
        "lat": num(geo.get("lat")) or num(a.get("lat")) or None, "lng": num(geo.get("lng")) or num(a.get("lng")) or None,
        "aprox": not a.get("number"),
        "aluguel": aluguel, "condominio": condo, "iptu": round(num(L.get("propertyTax"))),
        "total": aluguel + condo, "area": num(L.get("area")), "quartos": int(num(L.get("bedrooms"))),
        "banheiros": int(num(L.get("restrooms"))), "vagas": int(num(L.get("parkingSpots"))),
        "bairro": a.get("neighborhood") or "", "cidade": cidade,
        "endereco": " ".join(str(p) for p in (a.get("streetName"), a.get("number")) if p),
        "fotos": [f"https://content.loft.com.br/homes/{L['id']}/mobile_{f}" for f in L.get("photos") or []][:MAX_FOTOS],
        "titulo": "", "descricao": (L.get("description") or "").strip(), "criado": L.get("createdAt"),
        "mobiliado": "FURNISHED" in (L.get("unitFeatures") or []),
    }

def coletar_loft():
    itens, paginas = {}, 0
    for tipo, cid, nome in LOFT_BUSCAS:
        pag, total = 1, 1
        while pag <= max(total, 1) and pag <= 30:
            lst, total = loft_pagina(f"{LOFT}/aluguel/{tipo}/{cid}" + (f"?pagina={pag}" if pag > 1 else ""))
            paginas += 1
            for L in lst:
                it = loft_item(L, nome)
                if it:
                    itens[it["id"]] = it
            if not lst:
                break
            pag += 1
            time.sleep(random.uniform(1.0, 2.0))
    log(f"Loft: {paginas} páginas, {len(itens)} anúncios")
    return list(itens.values())

# ---------------- consolidação ----------------
def passa_filtro(it):
    # sem metragem informada não é motivo para tirar: só sai quem declara área abaixo do mínimo
    # locação por temporada/diária não é aluguel mensal (o preço é por dia/semana)
    if re.search(r"temporada|di[aá]ria|por noite|airbnb", sem_acento(it.get("titulo") or "")):
        return False
    return (0 < it["total"] <= TOTAL_MAX and (not it["area"] or it["area"] >= AREA_MIN)
            and sem_acento(it.get("cidade")) in CIDADES_OK)

SUMIDOS = DADOS / "sumidos.json"   # anúncios que saíram do ar (para reconhecer quando voltam com outro id)

def _assinatura(v):
    """Mesmo imóvel re-anunciado: mesma fonte, tipo, quartos, área, condomínio e bairro."""
    if not v.get("area") or not v.get("quartos"):
        return None
    return "|".join([v["fonte"], v.get("tipo") or "", str(v["quartos"]), str(round(v["area"])),
                     str(round((v.get("condominio") or 0) / 10)), sem_acento(v.get("bairro") or "")])

def _mesmo_imovel(a, b):
    if abs((a.get("aluguel") or 0) - (b.get("aluguel") or 0)) > 0.12 * max(a.get("aluguel") or 1, 1):
        return False
    if a.get("lat") and b.get("lat") and not a.get("aprox") and not b.get("aprox"):
        if abs(a["lat"] - b["lat"]) > 0.0015 or abs(a["lng"] - b["lng"]) > 0.0015:
            return False
    return True

def registrar_sumidos(estado_antes, estado, t):
    sum_ = carregar(SUMIDOS, {})
    for k, v in estado_antes.items():
        if k not in estado:
            sum_[k] = {c: v.get(c) for c in ("fonte", "tipo", "quartos", "area", "condominio", "aluguel", "bairro",
                                              "lat", "lng", "aprox", "primeiro", "antes")}
            sum_[k]["sumiu"] = iso(t)
    lim = t - timedelta(days=90)
    sum_ = {k: v for k, v in sum_.items() if datetime.fromisoformat(v["sumiu"]) > lim}
    salvar(SUMIDOS, sum_)
    return sum_

def ligar_reanuncios(lista, estado, ativos):
    """Liga cada anúncio ativo aos ids antigos do mesmo imóvel (que saíram do ar ou expiraram),
    para o favorito/visto seguir o anúncio quando ele é republicado."""
    pool = {}
    for k, v in list(carregar(SUMIDOS, {}).items()) + [(k, v) for k, v in estado.items() if k not in ativos]:
        a = _assinatura(v)
        if a:
            pool.setdefault(a, []).append((k, v))
    n = 0
    for v in lista:
        a = _assinatura(v)
        if not a or a not in pool:
            continue
        antes = set()
        for k, o in pool[a]:
            if k != v["id"] and _mesmo_imovel(v, o) and (o.get("primeiro") or "") <= (v.get("primeiro") or "9"):
                antes.add(k)
                antes.update(o.get("antes") or [])
        antes.discard(v["id"])
        if antes:
            v["antes"] = sorted(antes)
            n += 1
    log(f"Re-anúncios reconhecidos: {n}")

def mesclar(estado, itens, t, fonte_completa=None):
    """Atualiza o estado. Se fonte_completa, marca como removidos os que não vieram."""
    vistos = set()
    fontes_com_historico = {v["fonte"] for v in estado.values()}
    for it in itens:
        if not passa_filtro(it):
            continue
        vistos.add(it["id"])
        antigo = estado.get(it["id"])
        if antigo and antigo.get("det") and not it.get("det"):
            for k in ("lat", "lng", "endereco", "descricao"):
                if antigo.get(k):
                    it[k] = antigo[k]
            if len(antigo.get("fotos") or []) > len(it.get("fotos") or []):
                it["fotos"] = antigo["fotos"]
            it["det"] = True
        if antigo:
            it["primeiro"] = antigo["primeiro"]
            if antigo.get("inicial"):
                it["inicial"] = True
        else:
            it["primeiro"] = iso(t)
            # na primeira coleta de uma fonte tudo seria "novo"; marca como carga inicial
            if it["fonte"] not in fontes_com_historico:
                it["inicial"] = True
        if it.get("criado"):  # VivaReal informa a data de publicação
            try:
                c = datetime.fromisoformat(it["criado"])
                if c < datetime.fromisoformat(it["primeiro"]):
                    it["primeiro"] = iso(c)
                it.pop("inicial", None)
            except ValueError:
                pass
        it["visto"] = iso(t)
        if antigo and antigo.get("total") and antigo["total"] != it["total"]:
            it["total_anterior"] = antigo["total"]
        elif antigo and antigo.get("total_anterior"):
            it["total_anterior"] = antigo["total_anterior"]
        estado[it["id"]] = it
    if fonte_completa:
        for k in [k for k, v in estado.items() if v["fonte"] == fonte_completa and k not in vistos]:
            del estado[k]
    return len(vistos)

# ---------------- perímetro (lido da memória compartilhada no GitHub Gist) ----------------
CONFIG = BASE / "config.json"
PERIM_CACHE = DADOS / "perimetro.json"
PERIM_PADRAO = BASE / "perimetro_padrao.json"
# folga em volta do desenho (metros), pelo erro medido de cada fonte: comparando o mesmo imóvel
# anunciado com endereço exato num site e aproximado em outro, ~70% dos aproximados caem a até esta distância
MARGEM_EXATO = 60
MARGEM_APROX = {"Loft": 250, "VivaReal": 400, "Imobiliárias": 800, "OLX": 900, "Chaves na Mão": 900, "Imovelweb": 1000}
MARGEM_APROX_PADRAO = 800

def margem_de(v):
    return MARGEM_APROX.get(v.get("fonte"), MARGEM_APROX_PADRAO) if v.get("aprox") else MARGEM_EXATO

PERIM_T = 0   # carimbo (ms) do perímetro usado na última geração do site

def ler_perimetro():
    cfg = carregar(CONFIG, {})
    pid = (cfg.get("firebase") or {}).get("projectId")
    if pid:  # perímetro público no Firestore (gravado pela página depois do login)
        try:
            d = http_json(f"https://firestore.googleapis.com/v1/projects/{pid}/databases/(default)/documents/publico/perimetro", tentativas=2)
            per = json.loads(d["fields"]["geojson"]["stringValue"])
            global PERIM_T
            PERIM_T = int((d["fields"].get("t") or {}).get("integerValue") or 0)
            if per and per.get("features"):
                salvar(PERIM_CACHE, per)
                return per
        except Exception as e:
            log("Perímetro: Firestore indisponível, tento o gist:", repr(e)[:120])
    gid = cfg.get("gist_id")
    if gid:
        try:
            g = http_json(f"https://api.github.com/gists/{gid}", {"Accept": "application/vnd.github+json"}, tentativas=2)
            mem = json.loads(g["files"]["memoria.json"]["content"])
            if mem.get("perimetro", {}).get("features"):
                salvar(PERIM_CACHE, mem["perimetro"])
                return mem["perimetro"]
        except Exception as e:
            log("Perímetro: não consegui ler o gist, uso o último salvo:", repr(e)[:120])
    return carregar(PERIM_CACHE, None) or carregar(PERIM_PADRAO, None)

def _dentro(lng, lat, anel):
    d = False
    j = len(anel) - 1
    for i in range(len(anel)):
        xi, yi = anel[i][0], anel[i][1]
        xj, yj = anel[j][0], anel[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            d = not d
        j = i
    return d

def _dist_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / ((dx * dx + dy * dy) or 1e-12)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5

def no_perimetro(lat, lng, perim, margem_m=400):
    if not perim or not perim.get("features"):
        return True
    import math
    kx = math.cos(math.radians(lat))  # distância em graus de latitude: corrige a longitude
    lim = margem_m / 111_000
    for f in perim["features"]:
        g = f.get("geometry") or {}
        polis = [g["coordinates"]] if g.get("type") == "Polygon" else g.get("coordinates", []) if g.get("type") == "MultiPolygon" else []
        for poli in polis:
            anel = poli[0]
            if _dentro(lng, lat, anel):
                return True
            if any(_dist_seg(lng * kx, lat, anel[i][0] * kx, anel[i][1], anel[i + 1][0] * kx, anel[i + 1][1]) < lim
                   for i in range(len(anel) - 1)):
                return True
    return False

PRIORIDADE = {"QuintoAndar": 0, "VivaReal": 1, "Chaves na Mão": 2, "Imobiliárias": 3, "Loft": 4, "Imovelweb": 5, "OLX": 6}
BAIRROS_PERIM = {sem_acento(b.replace("-", " ")) for bs in BAIRROS_BUSCA.values() for b in bs}

def agrupar_repetidos(lista):
    """O mesmo apartamento costuma estar em vários sites (imobiliárias publicam em todos).
    Junta quem tem mesma cidade, mesmo aluguel, área igual (±1 m²), quartos compatíveis e está
    no mesmo bairro ou a menos de ~600 m. Fica o anúncio da fonte mais completa; os outros viram 'também em'."""
    lista = sorted(lista, key=lambda v: (PRIORIDADE.get(v["fonte"], 9), v["id"]))
    baldes, saida = {}, []
    for v in lista:
        if not v.get("aluguel") or not v.get("area"):
            saida.append(v); continue
        cid, alug, area = sem_acento(v.get("cidade")), int(round(v["aluguel"])), int(round(v["area"]))
        dono = None
        for a in (area - 1, area, area + 1):
            for p in baldes.get((cid, alug, a), []):
                if p["fonte"] == v["fonte"]:
                    continue
                if p.get("quartos") and v.get("quartos") and p["quartos"] != v["quartos"]:
                    continue
                perto = (p.get("lat") and v.get("lat") and abs(p["lat"] - v["lat"]) < 0.006 and abs(p["lng"] - v["lng"]) < 0.006)
                mesmo_bairro = sem_acento(p.get("bairro")) and sem_acento(p.get("bairro")) == sem_acento(v.get("bairro"))
                if perto or mesmo_bairro:
                    dono = p; break
            if dono:
                break
        if dono:
            dono.setdefault("tambem", []).append({"fonte": v["fonte"], "url": v["url"]})
            for k in ("condominio", "iptu", "quartos", "banheiros", "vagas", "endereco", "criado"):
                if not dono.get(k) and v.get(k):
                    dono[k] = v[k]
            if len(v.get("fotos") or []) > len(dono.get("fotos") or []):
                dono["fotos"] = v["fotos"]
            if (not dono.get("lat") or dono.get("aprox")) and v.get("lat") and not v.get("aprox"):
                dono["lat"], dono["lng"], dono["aprox"] = v["lat"], v["lng"], False
            dono["total"] = dono["aluguel"] + (dono.get("condominio") or 0)
            if v.get("primeiro") and (not dono.get("primeiro") or v["primeiro"] < dono["primeiro"]):
                dono["primeiro"] = v["primeiro"]
        else:
            baldes.setdefault((cid, alug, area), []).append(v)
            saida.append(v)
    return saida

def gerar_site(estado, meta):
    t = agora()
    lim_olx = t - timedelta(hours=OLX_VALIDADE_HORAS)
    perim = ler_perimetro()
    def entra(v):
        if v["fonte"] == "OLX" and datetime.fromisoformat(v["visto"]) < lim_olx:
            return False
        if not passa_filtro(v):  # regras novas valem também para o que já estava guardado
            return False
        if v.get("lat") is None:  # sem endereço: entra se o bairro é do perímetro (ou não foi informado)
            return not v.get("bairro") or sem_acento(v["bairro"]) in BAIRROS_PERIM
        return no_perimetro(v["lat"], v["lng"], perim, margem_de(v))
    ativos = {k for k, v in estado.items() if entra(v)}
    lista = [dict(estado[k]) for k in ativos]
    try:
        ligar_reanuncios(lista, estado, ativos)
    except Exception as e:
        log("Re-anúncios: falhou:", repr(e)[:150])
    lista = agrupar_repetidos(lista)
    campos = ["id", "fonte", "url", "lat", "lng", "aprox", "aluguel", "condominio", "iptu", "total", "area",
              "quartos", "banheiros", "vagas", "bairro", "cidade", "endereco", "fotos", "titulo", "primeiro",
              "criado", "inicial", "total_anterior", "mobiliado", "anunciante", "tambem", "tipo", "video", "antes"]
    FOTOS_SITE, DESC_SITE = 4, 180
    import hashlib, shutil
    det_dir = SITE / "det"
    shutil.rmtree(det_dir, ignore_errors=True)
    det_dir.mkdir(parents=True, exist_ok=True)
    pedacos = {}
    enxuto = []
    for v in lista:
        e = {k: v[k] for k in campos if v.get(k) not in (None, "", False, [])}
        fotos, desc = v.get("fotos") or [], re.sub(r"\s+\n", "\n", (v.get("descricao") or "")).strip()
        extra = {}
        if len(fotos) > FOTOS_SITE:
            e["fotos"], e["nf"] = fotos[:FOTOS_SITE], len(fotos)
            extra["fotos"] = fotos
        if desc:
            e["desc"] = desc[:DESC_SITE]
            if len(desc) > DESC_SITE:
                e["desc_mais"] = 1
                extra["descricao"] = desc[:4000]
        if extra:
            n = int(hashlib.md5(v["id"].encode()).hexdigest(), 16) % 64
            pedacos.setdefault(n, {})[v["id"]] = extra
        enxuto.append(e)
    for n, conteudo in pedacos.items():
        (det_dir / f"{n}.json").write_text(json.dumps(conteudo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Repetidos entre sites juntados: {sum(len(v.get('tambem', [])) for v in lista)}")
    SITE.mkdir(exist_ok=True)
    payload = {"gerado": iso(t), "perimetro_t": PERIM_T, "filtros": {"total_max": TOTAL_MAX, "area_min": AREA_MIN},
               "margens": {"exato": MARGEM_EXATO, "aprox": MARGEM_APROX, "padrao": MARGEM_APROX_PADRAO},
               "fontes": meta.get("fontes", {}), "anuncios": enxuto}
    (SITE / "dados.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    import shutil
    shutil.copyfile(PERIM_PADRAO, SITE / "perimetro_padrao.json")
    cfg = carregar(CONFIG, {})
    (SITE / "config.json").write_text(json.dumps({"firebase": cfg.get("firebase")}), encoding="utf-8")
    old = SITE / "dados.js"
    if old.exists():
        old.unlink()
    log(f"Site atualizado: {len(enxuto)} anúncios dentro do perímetro (de {len(estado)} coletados)")

# ---------------- publicação no GitHub Pages ----------------
PUBLICAR = BASE / "publicar"

def git(*args):
    import subprocess
    r = subprocess.run(["git", *args], cwd=PUBLICAR, capture_output=True, text=True, encoding="utf-8",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout

def publicar():
    import shutil
    if not (PUBLICAR / ".git").exists():
        log("Publicação: pasta publicar/ não configurada; pulando")
        return
    for nome in ("index.html", "dados.json", "config.json", "perimetro_padrao.json"):
        shutil.copyfile(SITE / nome, PUBLICAR / nome)
    shutil.rmtree(PUBLICAR / "det", ignore_errors=True)
    if (SITE / "det").exists():
        shutil.copytree(SITE / "det", PUBLICAR / "det")
    (PUBLICAR / ".nojekyll").touch()
    git("add", "-A")
    if not git("status", "--porcelain").strip():
        log("Publicação: nada mudou")
        return
    # um único commit sempre (amend + force) para o repositório não crescer a cada hora
    tem_commit = bool(git("rev-list", "--all", "--max-count=1").strip())
    if tem_commit:
        git("commit", "-q", "--amend", "-m", "dados atualizados")
    else:
        git("commit", "-q", "-m", "dados atualizados")
    git("push", "-q", "--force", "origin", "HEAD:gh-pages")
    log("Publicação: enviado ao GitHub Pages")

def _slug(txt):
    return re.sub(r"[^a-z0-9]+", "-", sem_acento(txt)).strip("-")

def bairros_do_perimetro(estado):
    """Bairros (slug) que têm anúncios com coordenada exata dentro de qualquer área desenhada
    (ligada ou desligada), somados à lista fixa. Assim, ampliar a área leva a busca junto."""
    perim = ler_perimetro()
    cont = {}
    for v in estado.values():
        if v.get("lat") is None or v.get("aprox") or not v.get("bairro") or v["fonte"] not in ("VivaReal", "QuintoAndar"):
            continue
        if not no_perimetro(v["lat"], v["lng"], perim):
            continue
        cid = "niteroi" if sem_acento(v.get("cidade")) == "niteroi" else "rio-de-janeiro"
        k = (cid, _slug(v["bairro"]))
        cont[k] = cont.get(k, 0) + 1
    out = {k: set(v) for k, v in BAIRROS_BUSCA_FIXOS.items()}
    for (cid, b), n in cont.items():
        if n >= 2:
            out.setdefault(cid, set()).add(b)
    return {k: sorted(v) for k, v in out.items()}

def main():
    t = agora()
    estado = carregar(ESTADO, {})
    estado_antes = {k: {c: v.get(c) for c in ("fonte", "tipo", "quartos", "area", "condominio", "aluguel", "bairro",
                                              "lat", "lng", "aprox", "primeiro", "antes")} for k, v in estado.items()}
    meta = carregar(META, {"fontes": {}})
    fontes = meta.setdefault("fontes", {})

    def registra(nome, ok, n=None, obs=""):
        f = fontes.setdefault(nome, {})
        f["tentativa"] = iso(t)
        f["ok"] = ok
        if ok:
            f["sucesso"] = iso(t)
            f["qtd"] = n
        f["obs"] = obs

    # QuintoAndar
    try:
        n = mesclar(estado, coletar_quintoandar(), t, fonte_completa="QuintoAndar")
        registra("QuintoAndar", True, n)
    except Exception as e:
        log("QuintoAndar falhou:", repr(e)); registra("QuintoAndar", False, obs=str(e)[:200])

    # VivaReal
    try:
        ult = fontes.get("VivaReal", {}).get("completo")
        completo = not ult or t - datetime.fromisoformat(ult) > timedelta(hours=FULL_SWEEP_HORAS) or "--completo" in sys.argv
        if completo:
            mesclar(estado, coletar_vivareal_completo(), t, fonte_completa="VivaReal")
        else:
            mesclar(estado, coletar_vivareal_novos({k for k in estado if k.startswith("vr-")}), t)
        n = sum(1 for v in estado.values() if v["fonte"] == "VivaReal")
        registra("VivaReal", True, n, "varredura completa" if completo else "só novos")
        if completo:
            fontes["VivaReal"]["completo"] = iso(t)
    except Exception as e:
        log("VivaReal falhou:", repr(e)); registra("VivaReal", False, obs=str(e)[:200])

    # OLX
    if "--sem-olx" not in sys.argv:
        try:
            ult = fontes.get("OLX", {}).get("varredura")
            varredura = (not ult or t - datetime.fromisoformat(ult) > timedelta(hours=OLX_VARREDURA_HORAS)
                         or "--completo" in sys.argv)
            itens, bloqueado = coletar_olx(varredura, frozenset(k for k, v in estado.items() if v.get("det")))
            if itens is None:
                registra("OLX", False, obs="bloqueado pelo site nesta rodada" if bloqueado else "nenhum anúncio lido")
            else:
                mesclar(estado, itens, t)
                n = sum(1 for v in estado.values() if v["fonte"] == "OLX")
                registra("OLX", True, n, ("varredura parcial (bloqueado no meio)" if bloqueado else "varredura completa")
                         if varredura else "só novos")
                if varredura and not bloqueado:
                    fontes["OLX"]["varredura"] = iso(t)
        except Exception as e:
            log("OLX falhou:", repr(e)); registra("OLX", False, obs=str(e)[:200])

    # bairros a buscar nas fontes por bairro (Chaves na Mão, Imovelweb) = os que caem no perímetro desenhado
    try:
        global BAIRROS_BUSCA, BAIRROS_PERIM
        BAIRROS_BUSCA = bairros_do_perimetro(estado)
        BAIRROS_PERIM = {sem_acento(b.replace("-", " ")) for bs in BAIRROS_BUSCA.values() for b in bs}
        log("Bairros buscados:", {k: len(v) for k, v in BAIRROS_BUSCA.items()})
    except Exception as e:
        log("Bairros do perímetro: falhou, uso a lista fixa:", repr(e)[:120])

    # Chaves na Mão: lê todos os bairros do perímetro a cada hora (volume pequeno)
    try:
        n = mesclar(estado, coletar_chavesnamao(), t, fonte_completa="Chaves na Mão")
        registra("Chaves na Mão", True, n)
    except Exception as e:
        log("Chaves na Mão falhou:", repr(e)); registra("Chaves na Mão", False, obs=str(e)[:200])

    # Imobiliárias de Niterói (sites próprios Kenlo): a cada 6 h
    ult = fontes.get("Imobiliárias", {}).get("sucesso")
    if not ult or t - datetime.fromisoformat(ult) > timedelta(hours=KENLO_HORAS) or "--completo" in sys.argv:
        try:
            itens, falhas = coletar_imobiliarias()
            try:
                itens += coletar_selfspin()
            except Exception as e:
                falhas.append("Self Spin"); log("Self Spin falhou:", repr(e)[:150])
            mesclar(estado, itens, t, fonte_completa="Imobiliárias" if not falhas else None)
            n = sum(1 for v in estado.values() if v["fonte"] == "Imobiliárias")
            registra("Imobiliárias", True, n, ("sem resposta: " + ", ".join(falhas)) if falhas else "")
        except Exception as e:
            log("Imobiliárias falhou:", repr(e)); registra("Imobiliárias", False, obs=str(e)[:200])

    # Loft: a cada 3 h (volume pequeno no RJ)
    ult = fontes.get("Loft", {}).get("sucesso")
    if not ult or t - datetime.fromisoformat(ult) > timedelta(hours=LOFT_HORAS) or "--completo" in sys.argv:
        try:
            n = mesclar(estado, coletar_loft(), t, fonte_completa="Loft")
            registra("Loft", True, n)
        except Exception as e:
            log("Loft falhou:", repr(e)); registra("Loft", False, obs=str(e)[:200])

    # Imovelweb: uma varredura por dia (bloqueia robôs; melhor esforço)
    if "--sem-imovelweb" not in sys.argv:
        ult = fontes.get("Imovelweb", {}).get("varredura")
        if not ult or t - datetime.fromisoformat(ult) > timedelta(hours=24) or "--completo" in sys.argv:
            try:
                itens, bloqueado = coletar_imovelweb()
                if itens is None:
                    registra("Imovelweb", False, obs="bloqueado pelo site" if bloqueado else "nenhum anúncio lido")
                else:
                    mesclar(estado, itens, t, fonte_completa=None if bloqueado else "Imovelweb")
                    n = sum(1 for v in estado.values() if v["fonte"] == "Imovelweb")
                    registra("Imovelweb", True, n, "varredura parcial" if bloqueado else "varredura completa")
                    if not bloqueado:
                        fontes["Imovelweb"]["varredura"] = iso(t)
            except Exception as e:
                log("Imovelweb falhou:", repr(e)); registra("Imovelweb", False, obs=str(e)[:200])

    try:
        registrar_sumidos(estado_antes, estado, t)
    except Exception as e:
        log("Registro de sumidos falhou:", repr(e)[:150])
    salvar(ESTADO, estado)
    salvar(META, meta)
    gerar_site(estado, meta)
    if "--sem-publicar" not in sys.argv:
        try:
            publicar()
        except Exception as e:
            log("Publicação falhou:", repr(e)[:300])

TRAVA = DADOS / "coleta.lock"

class Trava:
    """Arquivo-trava: a coleta horária e o recálculo do vigia não escrevem ao mesmo tempo."""
    def __init__(self, espera_max=3600):
        self.espera_max = espera_max
    def __enter__(self):
        ini = time.time()
        DADOS.mkdir(exist_ok=True)
        while True:
            try:
                fd = os.open(TRAVA, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:  # trava esquecida por um processo que morreu
                    if time.time() - TRAVA.stat().st_mtime > 3 * 3600:
                        TRAVA.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - ini > self.espera_max:
                    raise TimeoutError("coleta ocupada")
                time.sleep(5)
    def __exit__(self, *a):
        try:
            TRAVA.unlink()
        except FileNotFoundError:
            pass

def recalcular_site():
    """Recálculo rápido depois de redesenhar a área: usa o que já foi coletado (cidades inteiras
    no QuintoAndar, VivaReal e OLX) e só refaz o recorte pelo perímetro novo e a publicação."""
    with Trava(espera_max=1800):
        estado = carregar(ESTADO, {})
        meta = carregar(META, {"fontes": {}})
        gerar_site(estado, meta)
        publicar()

if __name__ == "__main__":
    try:
        if "--recalcular" in sys.argv:
            recalcular_site()
        else:
            with Trava():
                main()
    except Exception:
        log("ERRO GERAL:\n" + traceback.format_exc())
        sys.exit(1)
