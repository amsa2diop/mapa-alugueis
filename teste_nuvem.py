"""Teste: quais fontes respondem a partir dos servidores do GitHub (só lê, não publica nada)."""
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coletor as c

res = []
def roda(nome, fn):
    t0 = time.time()
    try:
        r = fn()
        if isinstance(r, tuple):
            itens, extra = r[0], r[1]
        else:
            itens, extra = r, ""
        n = len(itens) if itens is not None else 0
        ok = itens is not None and n > 0
        res.append((nome, "OK" if ok else "FALHOU", n, f"{time.time()-t0:.0f}s", str(extra)[:120]))
    except Exception as e:
        traceback.print_exc()
        res.append((nome, "ERRO", 0, f"{time.time()-t0:.0f}s", repr(e)[:160]))
    print(res[-1], flush=True)

roda("QuintoAndar", c.coletar_quintoandar)
roda("VivaReal/ZAP", lambda: c.coletar_vivareal_novos(set()))
roda("Chaves na Mão", c.coletar_chavesnamao)
roda("Imobiliárias (Kenlo)", c.coletar_imobiliarias)
roda("Self Spin", c.coletar_selfspin)
roda("Firestore (perímetro)", lambda: (c.ler_perimetro() or {}).get("features"))
roda("Nominatim (geocódigo)", lambda: [x for x in [c.geocodificar("Botafogo", "Rio de Janeiro", {})] if x])
roda("OLX", lambda: c.coletar_olx(False))
roda("Imovelweb", c.coletar_imovelweb)

linhas = ["| Fonte | Resultado | Anúncios | Tempo | Obs |", "|---|---|---|---|---|"]
linhas += [f"| {a} | {b} | {n} | {t} | {o} |" for a, b, n, t, o in res]
txt = "\n".join(linhas)
print(txt)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8").write(txt + "\n")
