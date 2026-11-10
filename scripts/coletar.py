#!/usr/bin/env python3
"""
Coleta as avaliações do Google Maps das lojas em lojas.json (via SerpApi),
classifica os temas das avaliações abaixo de 4 estrelas e atualiza dados.json.

Variáveis de ambiente:
  SERPAPI_KEY        (obrigatória) chave do SerpApi
  ANTHROPIC_API_KEY  (opcional)    classifica temas com o Claude; sem ela, usa palavras-chave
  MODO               "diario" (padrão) busca só as novas | "completo" busca o histórico
  DATA_INICIO        (opcional, AAAA-MM-DD) no modo completo, para ao chegar nessa data
  MAX_PAGINAS        (opcional) limite de páginas por loja; padrão 5 no diário, 500 no completo

Usa só a biblioteca padrão do Python.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
ARQ_LOJAS = RAIZ / "lojas.json"
ARQ_DADOS = RAIZ / "dados.json"
FUSO = ZoneInfo("America/Sao_Paulo")

TEMAS = ["Tempo de espera", "Atendimento", "Comida", "Preço", "Ambiente", "Limpeza", "Delivery", "Outros"]

PALAVRAS = {
    "Tempo de espera": ["demor", "espera", "esperamos", "esperei", "fila", "lento", "minutos", "hora para", "horas"],
    "Atendimento": ["atend", "garçom", "garcom", "garçonete", "grosse", "mal educad", "educação", "simpati", "ignor", "gerente", "funcionári"],
    "Comida": ["comida", "carne", "frio", "fria", "sem sal", "salgad", "cru", "crua", "passad", "sabor", "gosto", "prato", "costela", "batata", "queimad", "sobremesa"],
    "Preço": ["caro", "muito cara", "cara demais", "caríssim", "preço", "preco", "valor", "cobra", "taxa", "porção pequena", "porcao pequena", "conta"],
    "Ambiente": ["barulh", "música", "musica", "ambiente", "calor", "ar condicionado", "ar-condicionado", "apertad", "estacionamento", "lotado"],
    "Limpeza": ["sujo", "suja", "limpeza", "banheiro", "mosca", "barata", "higien", "cabelo"],
    "Delivery": ["delivery", "entrega", "ifood", "app", "motoboy", "embalagem"],
}


# ---------------- utilidades ----------------
def log(*a):
    print(*a, flush=True)


def get_json(url, params, tentativas=3):
    q = url + "?" + urllib.parse.urlencode(params)
    for t in range(tentativas):
        try:
            with urllib.request.urlopen(q, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if t == tentativas - 1:
                raise
            log(f"  falha ({e}); tentando de novo em {5 * (t + 1)}s")
            time.sleep(5 * (t + 1))


def data_local(iso):
    """Converte o iso_date (UTC) para a data no fuso de São Paulo."""
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO).date().isoformat()


# ---------------- SerpApi ----------------
def resolver_data_id(loja, chave):
    log(f"Procurando o data_id de {loja['nome']} ({loja['busca']})")
    r = get_json("https://serpapi.com/search.json",
                 {"engine": "google_maps", "type": "search", "q": loja["busca"], "hl": "pt-BR", "api_key": chave})
    if r.get("place_results", {}).get("data_id"):
        return r["place_results"]["data_id"]
    for item in r.get("local_results", []):
        if "rancho texano" in item.get("title", "").lower() and item.get("data_id"):
            return item["data_id"]
    raise RuntimeError(f"Não encontrei a loja '{loja['busca']}'. Preencha o data_id dela no lojas.json.")


def coletar_loja(loja, chave, conhecidos, modo, max_paginas, data_inicio):
    novas, token, paginas, seguidas_conhecidas = [], None, 0, 0
    while paginas < max_paginas:
        params = {"engine": "google_maps_reviews", "data_id": loja["data_id"], "hl": "pt-BR",
                  "sort_by": "newestFirst", "api_key": chave}
        if token:
            params.update({"next_page_token": token, "num": 20})
        r = get_json("https://serpapi.com/search.json", params)
        if r.get("error"):
            raise RuntimeError(f"SerpApi: {r['error']}")
        paginas += 1
        revs = r.get("reviews", [])
        parar = False
        for rv in revs:
            rid = rv.get("review_id")
            if not rid:
                continue
            data = data_local(rv.get("iso_date"))
            if data_inicio and data and data < data_inicio:
                parar = True
                break
            if rid in conhecidos:
                seguidas_conhecidas += 1
                if modo == "diario" and seguidas_conhecidas >= 10:
                    parar = True
                    break
                continue
            seguidas_conhecidas = 0
            texto = (rv.get("extracted_snippet") or {}).get("original") or rv.get("snippet") or ""
            nota = int(round(float(rv.get("rating") or 0)))
            if not 1 <= nota <= 5 or not data:
                continue
            reg = {"id": rid, "loja": loja["id"], "data": data, "nota": nota}
            if nota < 4:
                reg["comentario"] = texto.strip()
                reg["respondida"] = bool(rv.get("response"))
            novas.append(reg)
        token = (r.get("serpapi_pagination") or {}).get("next_page_token")
        log(f"  página {paginas}: {len(revs)} avaliações, {len(novas)} novas até agora")
        if parar or not token or not revs:
            break
    return novas


# ---------------- classificação de temas ----------------
def temas_por_palavra(texto):
    t = texto.lower()
    achados = [tema for tema, ps in PALAVRAS.items() if any(p in t for p in ps)]
    return achados[:3] or ["Outros"]


def temas_com_claude(itens, chave):
    """itens: lista de (id, nota, texto). Retorna {id: [temas]}."""
    resultado = {}
    for i in range(0, len(itens), 25):
        lote = itens[i:i + 25]
        linhas = "\n".join(json.dumps({"id": k, "nota": n, "texto": t[:1200]}, ensure_ascii=False) for k, n, t in lote)
        prompt = (
            "Você classifica avaliações negativas de restaurantes. Para cada avaliação, escolha de 1 a 3 temas "
            f"desta lista fechada: {', '.join(TEMAS)}. Use 'Outros' só se nenhum servir. Avaliação sem texto vira ['Outros'].\n"
            "Responda APENAS com um array JSON no formato [{\"id\": \"...\", \"temas\": [\"...\"]}], sem texto extra.\n\n"
            + linhas
        )
        corpo = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 4000,
                            "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=corpo, headers={
            "x-api-key": chave, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode())
            texto = "".join(b.get("text", "") for b in resp.get("content", []))
            arr = json.loads(re.search(r"\[.*\]", texto, re.S).group(0))
            for x in arr:
                ts = [t for t in x.get("temas", []) if t in TEMAS]
                if ts:
                    resultado[x["id"]] = ts[:3]
        except Exception as e:  # noqa: BLE001
            log(f"  Claude falhou neste lote ({e}); usando palavras-chave")
    return resultado


def classificar(avaliacoes):
    pend = [a for a in avaliacoes if a["nota"] < 4 and not a.get("temas")]
    if not pend:
        return
    log(f"Classificando temas de {len(pend)} avaliações abaixo de 4★")
    chave = os.environ.get("ANTHROPIC_API_KEY")
    via_claude = {}
    if chave:
        com_texto = [(a["id"], a["nota"], a["comentario"]) for a in pend if a.get("comentario")]
        via_claude = temas_com_claude(com_texto, chave)
    for a in pend:
        a["temas"] = via_claude.get(a["id"]) or (temas_por_palavra(a["comentario"]) if a.get("comentario") else ["Outros"])


# ---------------- principal ----------------
def main():
    chave = os.environ.get("SERPAPI_KEY")
    if not chave:
        sys.exit("Defina SERPAPI_KEY (no GitHub: Settings > Secrets and variables > Actions).")
    modo = os.environ.get("MODO", "diario").strip() or "diario"
    max_paginas = int(os.environ.get("MAX_PAGINAS") or (500 if modo == "completo" else 5))
    data_inicio = (os.environ.get("DATA_INICIO") or "").strip() or None

    lojas = json.loads(ARQ_LOJAS.read_text(encoding="utf-8"))
    dados = json.loads(ARQ_DADOS.read_text(encoding="utf-8")) if ARQ_DADOS.exists() else {"avaliacoes": []}
    por_id = {a["id"]: a for a in dados.get("avaliacoes", []) if a.get("id")}

    mudou_lojas = False
    for loja in lojas:
        if not loja.get("data_id"):
            loja["data_id"] = resolver_data_id(loja, chave)
            mudou_lojas = True
    if mudou_lojas:
        ARQ_LOJAS.write_text(json.dumps(lojas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_novas = 0
    for loja in lojas:
        log(f"Coletando {loja['nome']} (modo {modo})")
        conhecidos = {k for k, a in por_id.items() if a["loja"] == loja["id"]}
        novas = coletar_loja(loja, chave, conhecidos, modo, max_paginas, data_inicio)
        for n in novas:
            por_id[n["id"]] = n
        total_novas += len(novas)

    avaliacoes = sorted(por_id.values(), key=lambda a: (a["data"], a["loja"], a["id"]))
    classificar(avaliacoes)

    dados = {
        "atualizado_em": datetime.now(FUSO).isoformat(timespec="seconds"),
        "fonte": "Avaliações públicas do Google Maps",
        "lojas": [{"id": l["id"], "nome": l["nome"]} for l in lojas],
        "avaliacoes": avaliacoes,
    }
    ARQ_DADOS.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Pronto: {total_novas} avaliações novas, {len(avaliacoes)} no total.")


if __name__ == "__main__":
    main()
