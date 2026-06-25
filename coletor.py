#!/usr/bin/env python3
"""
Coletor de dados de mercado — roda no GitHub Actions (rede real, sem bloqueio de bot).

Produz um snapshot JSON por dia util:  snapshots/YYYY-MM-DD.json

Cada fonte e coletada de forma INDEPENDENTE: se uma falhar, as demais entram
e a lacuna fica registrada em snapshot["status"]["fontes"]. Nunca derruba o resto.

Fontes (todas gratuitas):
  - Acoes B3 ........... brapi.dev (token via env BRAPI_TOKEN) -> fallback yfinance (.SA)
  - CDI ............... BCB SGS serie 12
  - Tesouro + curva ... Tesouro Transparente (CSV oficial PrecoTaxaTesouroDireto).
                        Marca a mercado cada titulo e monta a curva (nominal/real)
                        a partir das taxas dos proprios papeis.

Config: config/ativos.json
"""
import os, sys, json, csv, io, datetime, traceback
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config", "ativos.json")
SNAP_DIR = os.path.join(ROOT, "snapshots")
UA = "Mozilla/5.0 (compatible; dados-mercado-collector/1.0)"
TIMEOUT = 60

TESOURO_CSV = ("https://www.tesourotransparente.gov.br/ckan/dataset/"
               "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
               "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv")


def num_br(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace("%", "").strip()
    if s in ("", "-"):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ----------------------------------------------------------------------------- ACOES
def coleta_acoes(tickers):
    token = os.environ.get("BRAPI_TOKEN", "").strip()
    out = {}
    try:
        lista = ",".join(tickers)
        url = f"https://brapi.dev/api/quote/{lista}?range=1d&interval=1d"
        if token:
            url += f"&token={token}"
        data = http_get_json(url)
        for r in data.get("results", []):
            sym, preco, var = r.get("symbol"), r.get("regularMarketPrice"), r.get("regularMarketChangePercent")
            if sym and preco is not None:
                out[sym] = {"preco": round(float(preco), 4),
                            "var_dia_pct": round(float(var), 4) if var is not None else None}
        if not [t for t in tickers if t not in out]:
            return out, "brapi.dev", None
        erro_brapi = f"brapi nao retornou: {[t for t in tickers if t not in out]}"
    except Exception as e:
        erro_brapi = f"brapi falhou: {e}"
    try:
        import yfinance as yf
        for t in [t for t in tickers if t not in out]:
            hist = yf.Ticker(t + ".SA").history(period="2d")
            if len(hist) >= 1:
                preco = float(hist["Close"].iloc[-1])
                var = ((preco / float(hist["Close"].iloc[-2]) - 1) * 100) if len(hist) >= 2 else None
                out[t] = {"preco": round(preco, 4), "var_dia_pct": round(var, 4) if var is not None else None}
        faltando = [t for t in tickers if t not in out]
        return out, ("brapi.dev+yfinance" if out else "yfinance"), (f"faltando: {faltando}" if faltando else None)
    except Exception as e:
        return out, "brapi.dev(parcial)", f"{erro_brapi}; yfinance falhou: {e}"


# ----------------------------------------------------------------------------- CDI
def coleta_cdi():
    try:
        url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados/ultimos/1?formato=json"
        item = http_get_json(url)[-1]
        return float(item["valor"]), item["data"], None
    except Exception as e:
        return None, None, f"BCB CDI falhou: {e}"


# ----------------------------------------------------- TESOURO TRANSPARENTE (+ curva)
def coleta_tesouro(titulos_cfg):
    """
    Le o CSV oficial do Tesouro Transparente (stream, sem carregar tudo na memoria)
    e, para cada titulo configurado, guarda a linha da Data Base mais recente.
    Casa por (Tipo Titulo, Data Vencimento). Retorna ({id: {...}}, erro).
    """
    alvo = {(c["tipo"].strip().lower(), c["vencimento"].strip()): c for c in titulos_cfg}
    melhor = {}  # id -> (data_base_date, taxa, preco)
    try:
        req = urllib.request.Request(TESOURO_CSV, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=180) as r:
            stream = io.TextIOWrapper(r, encoding="latin-1", newline="")
            reader = csv.reader(stream, delimiter=";")
            header = [h.strip() for h in next(reader)]
            idx = {h: i for i, h in enumerate(header)}
            ci_tipo = idx.get("Tipo Titulo")
            ci_venc = idx.get("Data Vencimento")
            ci_base = idx.get("Data Base")
            ci_taxa = idx.get("Taxa Compra Manha")
            ci_pu = idx.get("PU Base Manha", idx.get("PU Venda Manha"))
            if None in (ci_tipo, ci_venc, ci_base):
                return {}, f"colunas inesperadas no CSV: {header[:8]}"
            for row in reader:
                if len(row) <= max(ci_tipo, ci_venc, ci_base):
                    continue
                key = (row[ci_tipo].strip().lower(), row[ci_venc].strip())
                cfg = alvo.get(key)
                if not cfg:
                    continue
                try:
                    db = datetime.datetime.strptime(row[ci_base].strip(), "%d/%m/%Y").date()
                except ValueError:
                    continue
                cid = cfg["id"]
                if cid not in melhor or db > melhor[cid][0]:
                    taxa = num_br(row[ci_taxa]) if ci_taxa is not None and ci_taxa < len(row) else None
                    preco = num_br(row[ci_pu]) if ci_pu is not None and ci_pu < len(row) else None
                    melhor[cid] = (db, taxa, preco)
        out = {}
        for c in titulos_cfg:
            if c["id"] in melhor:
                db, taxa, preco = melhor[c["id"]]
                out[c["id"]] = {"nome": c.get("carteira"), "indexador": c.get("indexador"),
                                "vencimento": c["vencimento"], "data_base": db.strftime("%Y-%m-%d"),
                                "preco": round(preco, 4) if preco is not None else None,
                                "taxa": round(taxa, 4) if taxa is not None else None}
        faltando = [c["id"] for c in titulos_cfg if c["id"] not in out]
        return out, (f"titulos nao encontrados: {faltando}" if faltando else None)
    except Exception as e:
        return {}, f"Tesouro Transparente falhou: {e}"


def monta_curva(tesouro):
    """Curva (nominal/real) a partir das taxas dos titulos: prefixado->nominal, ipca->real."""
    nominal, real = {}, {}
    for cid, d in tesouro.items():
        if d.get("taxa") is None:
            continue
        ix = (d.get("indexador") or "").lower()
        if ix == "prefixado":
            nominal[cid] = d["taxa"]
        elif ix == "ipca":
            real[cid] = d["taxa"]
    return {"nominal": nominal, "real": real}


# ----------------------------------------------------------------------------- MAIN
def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    hoje = datetime.date.today()
    if hoje.weekday() >= 5:
        print(f"[skip] {hoje} e fim de semana, sem pregao B3.")
        return 0

    acoes, fonte_acoes, err_acoes = coleta_acoes(cfg.get("acoes", []))
    cdi, cdi_data, err_cdi = coleta_cdi()
    tesouro, err_tesouro = coleta_tesouro(cfg.get("tesouro", []))
    curva = monta_curva(tesouro)

    snap = {
        "data": hoje.strftime("%Y-%m-%d"),
        "gerado_em": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "acoes": acoes,
        "cdi_dia": round(cdi, 6) if cdi is not None else None,
        "cdi_data_ref": cdi_data,
        "tesouro": tesouro,
        "curva": curva,
        "status": {"fontes": {
            "acoes": {"fonte": fonte_acoes, "ok": bool(acoes) and not err_acoes, "erro": err_acoes},
            "cdi": {"fonte": "BCB SGS 12", "ok": cdi is not None, "erro": err_cdi},
            "tesouro": {"fonte": "Tesouro Transparente", "ok": bool(tesouro) and not err_tesouro, "erro": err_tesouro},
            "curva": {"fonte": "derivada do Tesouro", "ok": bool(curva["nominal"] or curva["real"]),
                      "erro": None if (curva["nominal"] or curva["real"]) else "sem taxas de titulos"},
        }},
    }

    os.makedirs(SNAP_DIR, exist_ok=True)
    out_path = os.path.join(SNAP_DIR, f"{snap['data']}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"[ok] snapshot salvo: {out_path}")
    print(json.dumps(snap["status"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
