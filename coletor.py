#!/usr/bin/env python3
"""
Coletor de dados de mercado — roda no GitHub Actions (rede real, sem bloqueio de bot).

Produz um snapshot JSON por dia util:  snapshots/YYYY-MM-DD.json

Cada fonte e coletada de forma INDEPENDENTE: se uma falhar, as demais entram
e a lacuna fica registrada em snapshot["status"]["fontes"]. Nunca derruba o resto.

Fontes:
  - Acoes B3 ............ brapi.dev (token via env BRAPI_TOKEN) -> fallback yfinance (.SA)
  - CDI ................. BCB SGS serie 12
  - Tesouro Direto ...... API oficial treasurybondsinfo.json  (marcacao a mercado dos titulos)
  - Curva ANBIMA ETTJ ... vertices nominais (pre) e reais (IPCA)  [PARTE FRAGIL - validar 1a exec]

Config: config/ativos.json
"""
import os, sys, json, datetime, traceback
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config", "ativos.json")
SNAP_DIR = os.path.join(ROOT, "snapshots")
UA = "Mozilla/5.0 (compatible; dados-mercado-collector/1.0)"
TIMEOUT = 30


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_get_json(url, headers=None):
    return json.loads(http_get(url, headers).decode("utf-8", "replace"))


# ----------------------------------------------------------------------------- ACOES
def coleta_acoes(tickers):
    """Retorna ({TICKER: {preco, var_dia_pct}}, fonte_usada, erro_ou_None)."""
    token = os.environ.get("BRAPI_TOKEN", "").strip()
    out = {}
    # --- tentativa 1: brapi.dev ---
    try:
        lista = ",".join(tickers)
        url = f"https://brapi.dev/api/quote/{lista}?range=1d&interval=1d"
        if token:
            url += f"&token={token}"
        data = http_get_json(url)
        for r in data.get("results", []):
            sym = r.get("symbol")
            preco = r.get("regularMarketPrice")
            var = r.get("regularMarketChangePercent")
            if sym and preco is not None:
                out[sym] = {"preco": round(float(preco), 4),
                            "var_dia_pct": round(float(var), 4) if var is not None else None}
        faltando = [t for t in tickers if t not in out]
        if not faltando:
            return out, "brapi.dev", None
        erro_brapi = f"brapi nao retornou: {faltando}"
    except Exception as e:
        erro_brapi = f"brapi falhou: {e}"

    # --- fallback: yfinance ---
    try:
        import yfinance as yf
        faltando = [t for t in tickers if t not in out]
        for t in faltando:
            tk = yf.Ticker(t + ".SA")
            hist = tk.history(period="2d")
            if len(hist) >= 1:
                preco = float(hist["Close"].iloc[-1])
                if len(hist) >= 2:
                    ant = float(hist["Close"].iloc[-2])
                    var = (preco / ant - 1) * 100 if ant else None
                else:
                    var = None
                out[t] = {"preco": round(preco, 4),
                          "var_dia_pct": round(var, 4) if var is not None else None}
        faltando = [t for t in tickers if t not in out]
        fonte = "brapi.dev+yfinance" if out else "yfinance"
        return out, fonte, (f"ainda faltando: {faltando}" if faltando else None)
    except Exception as e:
        return out, "brapi.dev(parcial)", f"{erro_brapi}; yfinance falhou: {e}"


# ----------------------------------------------------------------------------- CDI
def coleta_cdi():
    """CDI do dia (% a.d.) via BCB SGS 12. Retorna (valor_pct, data_str, erro)."""
    try:
        url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados/ultimos/1?formato=json"
        data = http_get_json(url)
        item = data[-1]
        return float(item["valor"]), item["data"], None
    except Exception as e:
        return None, None, f"BCB CDI falhou: {e}"


# ----------------------------------------------------------------------------- TESOURO
def coleta_tesouro(titulos_cfg):
    """
    Marcacao a mercado oficial dos titulos do Tesouro Direto.
    titulos_cfg: lista de {id, match} onde match e um trecho do nome oficial.
    Retorna ({id: {preco, taxa, nome}}, erro).
    """
    try:
        url = ("https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto/"
               "service/api/treasurybondsinfo.json")
        data = http_get_json(url)
        lista = data.get("response", {}).get("TrsrBdTradgList", [])
        catalogo = []
        for it in lista:
            bd = it.get("TrsrBd", {})
            catalogo.append({
                "nome": bd.get("nm", ""),
                "preco": bd.get("untrRedVal"),   # preco unitario de resgate (mark-to-market)
                "taxa": bd.get("anulRedRate"),    # taxa de resgate (% a.a.)
            })
        out = {}
        for cfg in titulos_cfg:
            alvo = cfg["match"].lower()
            achou = next((c for c in catalogo if alvo in c["nome"].lower()), None)
            if achou:
                out[cfg["id"]] = {
                    "nome": achou["nome"],
                    "preco": round(float(achou["preco"]), 4) if achou["preco"] is not None else None,
                    "taxa": round(float(achou["taxa"]), 4) if achou["taxa"] is not None else None,
                }
        faltando = [c["id"] for c in titulos_cfg if c["id"] not in out]
        return out, (f"titulos nao encontrados: {faltando}" if faltando else None)
    except Exception as e:
        return {}, f"Tesouro Direto falhou: {e}"


# ----------------------------------------------------------------------------- ANBIMA
def coleta_anbima(vertices):
    """
    Curva ANBIMA ETTJ — vertices nominais (pre) e reais (IPCA), em dias uteis.

    ATENCAO: esta e a parte FRAGIL. O formato do arquivo da ANBIMA muda. Esta
    funcao tenta a API publica de dados; se falhar, devolve lacuna e o snapshot
    segue sem a curva. NA PRIMEIRA EXECUCAO REAL, rode com ANBIMA_DEBUG=1 para
    salvar o conteudo bruto (anbima_raw.txt) e ajustar o parsing.

    Retorna ({"nominal": {v: taxa}, "real": {v: taxa}}, erro).
    """
    debug = os.environ.get("ANBIMA_DEBUG", "").strip() == "1"
    hoje = datetime.date.today().strftime("%d/%m/%Y")
    # A ANBIMA expoe a ETTJ via formulario POST em CZ.asp (retorna texto/HTML).
    # Estrategia: baixar e localizar os blocos "PREFIXADOS" e "IPCA".
    try:
        import urllib.parse
        body = urllib.parse.urlencode({
            "escolha": "2",         # 2 = data especifica
            "Idioma": "PT",
            "saida": "csv",
            "Dt_Ref": hoje,
        }).encode()
        req = urllib.request.Request(
            "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp",
            data=body,
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("latin-1", "replace")
        if debug:
            with open(os.path.join(ROOT, "anbima_raw.txt"), "w", encoding="utf-8") as f:
                f.write(raw)
        return _parse_anbima(raw, vertices)
    except Exception as e:
        return {"nominal": {}, "real": {}}, f"ANBIMA falhou: {e}"


def _parse_anbima(raw, vertices):
    """
    Parser tolerante do dump ANBIMA ETTJ. Procura linhas com
    'prazo(du) ; ETTJ IPCA ; ETTJ PREFIXADOS ; ...' (separador ; ou tab).
    Ajuste aqui apos ver anbima_raw.txt na primeira execucao.
    """
    nominal, real = {}, {}
    alvos = set(int(v) for v in vertices)
    for linha in raw.replace("\r", "").split("\n"):
        partes = [p.strip().replace(".", "").replace(",", ".")
                  for p in linha.replace("\t", ";").split(";") if p.strip() != ""]
        if len(partes) < 3:
            continue
        try:
            prazo = int(float(partes[0]))
        except ValueError:
            continue
        if prazo in alvos:
            # heuristica: col1 = IPCA (real), col2 = PREFIXADOS (nominal)
            try:
                real[str(prazo)] = round(float(partes[1]), 4)
                nominal[str(prazo)] = round(float(partes[2]), 4)
            except (ValueError, IndexError):
                pass
    erro = None
    if not nominal and not real:
        erro = "parser ANBIMA nao encontrou vertices (rode com ANBIMA_DEBUG=1 e ajuste _parse_anbima)"
    return {"nominal": nominal, "real": real}, erro


# ----------------------------------------------------------------------------- MAIN
def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    hoje = datetime.date.today()
    # Domingo=6, Sabado=5 -> sem pregao, nao gera snapshot
    if hoje.weekday() >= 5:
        print(f"[skip] {hoje} e fim de semana, sem pregao B3.")
        return 0

    acoes, fonte_acoes, err_acoes = coleta_acoes(cfg.get("acoes", []))
    cdi, cdi_data, err_cdi = coleta_cdi()
    tesouro, err_tesouro = coleta_tesouro(cfg.get("tesouro", []))
    curva, err_curva = coleta_anbima(cfg.get("vertices", [252, 504, 1260, 2520]))

    snap = {
        "data": hoje.strftime("%Y-%m-%d"),
        "gerado_em": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "acoes": acoes,
        "cdi_dia": round(cdi, 6) if cdi is not None else None,
        "cdi_data_ref": cdi_data,
        "tesouro": tesouro,
        "curva_anbima": curva,
        "status": {
            "fontes": {
                "acoes": {"fonte": fonte_acoes, "ok": bool(acoes) and not err_acoes, "erro": err_acoes},
                "cdi": {"fonte": "BCB SGS 12", "ok": cdi is not None, "erro": err_cdi},
                "tesouro": {"fonte": "Tesouro Direto API", "ok": bool(tesouro) and not err_tesouro, "erro": err_tesouro},
                "curva_anbima": {"fonte": "ANBIMA ETTJ", "ok": bool(curva["nominal"]), "erro": err_curva},
            }
        },
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
