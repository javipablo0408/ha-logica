import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import runtime
for m in ("b_basic","b_seq","b_comfort","b_energy","b_audio","b_climate","b_stubs"):
    try: __import__(m)
    except ModuleNotFoundError as e:
        if e.name != m: raise
cat = json.load(open(os.path.join(os.path.dirname(__file__), "..", "catalogo_loxone.json")))
blocks = cat["bloques"] if isinstance(cat, dict) else cat
ids = [b["id"] for b in blocks]
reg = runtime.REGISTRY
stub = [i for i in ids if i in reg and getattr(reg[i], "STUB", False)]
real = [i for i in ids if i in reg and not getattr(reg[i], "STUB", False)]
miss = [i for i in ids if i not in reg]
if __name__ == "__main__":
    print(f"catálogo {len(ids)} | implementados {len(real)} | stub {len(stub)} | faltan {len(miss)}")
    if miss: print("FALTAN:", ", ".join(miss))
    if "-v" in sys.argv: print("STUB:", ", ".join(stub))
