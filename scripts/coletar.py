#!/usr/bin/env python3
"""
Coleta as avaliações do Google Maps das lojas em lojas.json (via SerpApi),
classifica os temas das avaliações abaixo de 4 estrelas e atualiza dados.json.

Pensado para caber no plano GRATUITO do SerpApi (250 buscas/mês):
  1. Coleta diária: busca as avaliações mais novas de cada loja e para na primeira
     já salva (normalmente 1 busca por loja).
  2. Histórico aos poucos: as buscas que sobram no mês continuam de onde pararam
     ontem, trazendo avaliações antigas, sempre guardando uma reserva para a
     coleta diária até o fim do mês. O progresso fica em estado.json.

Variáveis de ambiente:
  SERPAPI_KEY            (obrigatória) chave do SerpApi
  ANTHROPIC_API_KEY      (opcional) classifica temas com o Claude; sem ela, usa palavras-chave
  MODO                   "diario" (padrão) | "completo" = busca todo o histórico de uma vez (plano pago)
  DATA_INICIO            (opcional, AAAA-MM-DD) não busca avaliações anteriores a essa data
  HISTORICO_POR_DIA      (opcional) máximo de páginas de histórico por dia, somando as lojas (padrão 5)
  RESERVA_BUSCAS         (opcional) buscas guardadas para a coleta diária (padrão 3 lojas x 31 dias)

Usa só a biblioteca padrão do Python.
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
ARQ_LOJAS = RAIZ / "lojas.json"
ARQ_DADOS = RAIZ / "dados.json"
ARQ_ESTADO = RAIZ / "estado.json"
FUSO = ZoneInfo("America/Sao_Paulo")
API = "https://serpapi.com/search.json"

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
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", "replace")
            try:
                return json.loads(corpo)  # o SerpApi devolve {"error": "..."} com status de erro
            except ValueError:
                if t == tentativas - 1:
                    raise
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


def ler_json(caminho, padrao):
    return json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else padrao


def gravar_json(caminho, obj, compacto=False):
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if compacto \
        else json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    caminho.write_text(txt, encoding="utf-8")


# ---------------- SerpApi ----------------
class Contador:
    def __init__(self):
        self.usadas = 0


def buscas_restantes(chave):
    """Consulta o saldo da conta (essa consulta não gasta busca)."""
    try:
        r = get_json("https://serpapi.com/account.json", {"api_key": chave})
        return int(r.get("plan_searches_left", r.get("total_searches_left", 0)))
    except Exception as e:  # noqa: BLE001
        log(f"Não consegui ler o saldo da conta ({e}); histórico fica para amanhã.")
        return 0


def resolver_data_id(loja, chave, cont):
    log(f"Procurando o data_id de {loja['nome']} ({loja['busca']})")
    r = get_json(API, {"engine": "google_maps", "type": "search", "q": loja["busca"], "hl": "pt-BR", "api_key": chave})
    cont.usadas += 1
    if r.get("place_results", {}).get("data_id"):
        return r["place_results"]["data_id"]
    for item in r.get("local_results", []):
        if "rancho texano" in item.get("title", "").lower() and item.get("data_id"):
            return item["data_id"]
    raise RuntimeError(f"Não encontrei a loja '{loja['busca']}'. Preencha o data_id dela no lojas.json.")


def buscar_pagina(loja, chave, token, cont):
    params = {"engine": "google_maps_reviews", "data_id": loja["data_id"], "hl": "pt-BR",
              "sort_by": "newestFirst", "api_key": chave}
    if token:
        params.update({"next_page_token": token, "num": 20})
    r = get_json(API, params)
    if r.get("error"):
        # "sem resultados" no fim da lista não é falha
        if "hasn't returned any results" in r["error"]:
            return [], None
        raise RuntimeError(r["error"])
    cont.usadas += 1
    return r.get("reviews", []), (r.get("serpapi_pagination") or {}).get("next_page_token")


def converter(rv, loja_id):
    rid, data = rv.get("review_id"), data_local(rv.get("iso_date"))
    nota = int(round(float(rv.get("rating") or 0)))
    if not rid or not data or not 1 <= nota <= 5:
        return None
    reg = {"id": rid, "loja": loja_id, "data": data, "nota": nota}
    if nota < 4:
        texto = (rv.get("extracted_snippet") or {}).get("original") or rv.get("snippet") or ""
        reg["comentario"] = texto.strip()
        reg["respondida"] = bool(rv.get("response"))
    return reg


def coleta_recentes(loja, chave, por_id, cont, max_paginas, data_inicio):
    """Mais novas primeiro, até achar uma já salva. Devolve (novas, token_da_proxima_pagina)."""
    novas, token = 0, None
    for pagina in range(1, max_paginas + 1):
        revs, prox = buscar_pagina(loja, chave, token, cont)
        achou_conhecida = False
        for rv in revs:
            reg = converter(rv, loja["id"])
            if not reg:
                continue
            if data_inicio and reg["data"] < data_inicio:
                return novas, None
            if reg["id"] in por_id:
                achou_conhecida = True
                continue
            por_id[reg["id"]] = reg
            novas += 1
        log(f"  recentes, página {pagina}: {novas} novas")
        token = prox
        if achou_conhecida or not prox or not revs:
            break
    return novas, token


def coleta_historico(loja, chave, por_id, cont, est, data_inicio):
    """Uma página de histórico, continuando do token salvo. Devolve quantas novas."""
    try:
        revs, prox = buscar_pagina(loja, chave, est.get("token"), cont)
    except RuntimeError as e:
        log(f"  token do histórico não funcionou ({e}); recomeça amanhã a partir das mais novas")
        est.update({"token": None, "iniciado": False})
        return 0
    novas, passou_inicio = 0, False
    for rv in revs:
        reg = converter(rv, loja["id"])
        if not reg:
            continue
        if data_inicio and reg["data"] < data_inicio:
            passou_inicio = True
            break
        if reg["id"] not in por_id:
            por_id[reg["id"]] = reg
            novas += 1
    est["token"] = prox
    est["paginas"] = est.get("paginas", 0) + 1
    if not prox or not revs or passou_inicio:
        est["completo"] = True
        est["token"] = None
        log(f"  histórico de {loja['nome']} completo")
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
            f"desta lista fechada: {', '.join(TEMAS)}. Use 'Outros' só se nenhum servir.\n"
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
    modo = (os.environ.get("MODO") or "diario").strip()
    data_inicio = (os.environ.get("DATA_INICIO") or "").strip() or None

    lojas = ler_json(ARQ_LOJAS, [])
    dados = ler_json(ARQ_DADOS, {"avaliacoes": []})
    estado = ler_json(ARQ_ESTADO, {"historico": {}})
    por_id = {a["id"]: a for a in dados.get("avaliacoes", []) if a.get("id")}
    cont = Contador()

    if any(not l.get("data_id") for l in lojas):
        for loja in lojas:
            if not loja.get("data_id"):
                loja["data_id"] = resolver_data_id(loja, chave, cont)
        gravar_json(ARQ_LOJAS, lojas)

    antes = len(por_id)
    hist = estado.setdefault("historico", {})

    # 1. Coleta das mais recentes
    for loja in lojas:
        log(f"{loja['nome']}: avaliações recentes")
        est = hist.setdefault(loja["id"], {"token": None, "iniciado": False, "completo": False, "paginas": 0})
        primeira_vez = not any(a["loja"] == loja["id"] for a in por_id.values())
        _, token = coleta_recentes(loja, chave, por_id, cont, 5 if primeira_vez else 3, data_inicio)
        if not est.get("iniciado") and not est.get("completo"):
            # o histórico começa logo depois das páginas recentes já lidas
            est.update({"token": token, "iniciado": True, "completo": token is None})

    # 2. Histórico
    pendentes = [l for l in lojas if not hist[l["id"]].get("completo") and hist[l["id"]].get("token")]
    if pendentes:
        if modo == "completo":
            limite = 10_000
        else:
            reserva = int(os.environ.get("RESERVA_BUSCAS") or 3 * 31)
            por_dia = int(os.environ.get("HISTORICO_POR_DIA") or 5)
            saldo = buscas_restantes(chave)
            limite = max(0, min(por_dia, saldo - reserva))
            log(f"Histórico: saldo {saldo} buscas, reserva {reserva}, hoje até {limite} páginas")
        feitas = 0
        while feitas < limite and pendentes:
            for loja in list(pendentes):
                if feitas >= limite:
                    break
                est = hist[loja["id"]]
                n = coleta_historico(loja, chave, por_id, cont, est, data_inicio)
                feitas += 1
                log(f"  {loja['nome']}: +{n} do histórico (página {est.get('paginas')})")
                if est.get("completo") or not est.get("token"):
                    pendentes.remove(loja)

    avaliacoes = sorted(por_id.values(), key=lambda a: (a["data"], a["loja"], a["id"]))
    classificar(avaliacoes)

    progresso = {}
    for loja in lojas:
        datas = [a["data"] for a in avaliacoes if a["loja"] == loja["id"]]
        progresso[loja["id"]] = {"completo": bool(hist.get(loja["id"], {}).get("completo")),
                                 "desde": min(datas) if datas else None}

    estado["ultima_execucao"] = datetime.now(FUSO).isoformat(timespec="seconds")
    estado["buscas_ultima_execucao"] = cont.usadas
    gravar_json(ARQ_ESTADO, estado)
    gravar_json(ARQ_DADOS, {
        "atualizado_em": estado["ultima_execucao"],
        "fonte": "Avaliações públicas do Google Maps",
        "lojas": [{"id": l["id"], "nome": l["nome"]} for l in lojas],
        "historico": progresso,
        "avaliacoes": avaliacoes,
    }, compacto=True)
    log(f"Pronto: {len(por_id) - antes} avaliações novas, {len(avaliacoes)} no total, {cont.usadas} buscas usadas.")


if __name__ == "__main__":
    main()
