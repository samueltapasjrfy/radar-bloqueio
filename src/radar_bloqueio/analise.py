"""Análise de um processo, ponta a ponta."""

from __future__ import annotations

import hashlib
import re
import logging
import time
from dataclasses import asdict
from datetime import date, timedelta

from . import cnj as cnjmod
from . import perfil
from .cache import CacheDocs
from .fatos import DocRef, Fatos, aplicar_sinais, extrair, norm
from .llm import AnalisadorLLM
from .pje_http import Autos, PjeHttp, ProcessoNaoEncontrado
from .risco import categoria, classificar, etapa
from .textos import extrair_texto
from . import eproc, esaj, projudi, projudi_go, projudi_pr, tjrj, tjse
from .timeline import Evento, parse_capa, parse_timeline

log = logging.getLogger(__name__)

MAX_DOCS = 16
MAX_LINHAS_TIMELINE = 140


def _assinatura(eventos: list[Evento]) -> str:
    base = "|".join(f"{e.data}{e.hora}{e.movimentos}{[d.doc_id for d in e.documentos]}" for e in eventos)
    return hashlib.sha1(base.encode()).hexdigest()


def _recorte(texto: str, cabeca: int, cauda: int) -> str:
    if len(texto) <= cabeca + cauda + 200:
        return texto
    return texto[:cabeca] + "\n[...]\n" + texto[-cauda:] if cauda else texto[:cabeca] + "\n[...]"


def selecionar(f: Fatos, eventos: list[Evento]) -> list[tuple[DocRef, int, int, str]]:
    """Escolhe (doc, cabeça, cauda, motivo). Ordem = prioridade."""
    sel: list[tuple[DocRef, int, int, str]] = []
    filhos = {d.doc_id: d for e in eventos for d in e.documentos if d.anexo_de}
    ref = f.data_ultima_sentenca

    for d in f.docs_sentenca[:2]:
        sel.append((d, 600, 3800, "sentença (dispositivo)"))
    for d in f.acordaos[:1]:
        sel.append((d, 2500, 1500, "acórdão"))
        for fid, fd in filhos.items():  # ementa anexa ao acórdão (JESP)
            if fd.anexo_de == d.doc_id and norm(fd.tipo) == "ementa":
                sel.append((DocRef(d.data, d.polo, fid, fd.tipo, fd.titulo, link_id=fd.link_id), 2500, 0, "ementa"))
    if f.certidoes_transito and not f.transito_mov:
        sel.append((f.certidoes_transito[0], 900, 0, "certidão de trânsito"))

    if ref is None:  # sem sentença: só as últimas decisões, para confirmar a fase
        for d in f.decisoes[:2]:
            sel.append((d, 1500, 500, "decisão recente"))
        return sel[:MAX_DOCS]

    corte = ref - timedelta(days=1)
    posteriores = sorted(
        [(d, "decisão/despacho") for d in f.decisoes if d.data >= corte][:6]
        + [(d, "petição do autor") for d in f.peticoes_autor if d.data >= corte][:5]
        + [(d, "petição da ré") for d in f.peticoes_reu if d.data >= corte][:6],
        key=lambda x: x[0].data, reverse=True)
    ja = {s[0].doc_id for s in sel}
    for d, motivo in posteriores:
        if d.doc_id in ja or norm(d.tipo) in ("sentenca", "intimacao"):
            continue
        ja.add(d.doc_id)
        if motivo == "decisão/despacho":
            sel.append((d, 1600, 900, motivo))
        else:
            sel.append((d, 2200, 900, motivo))
    return sel[:MAX_DOCS]


def _linha_evento(e: Evento) -> str:
    docs = []
    principais = [d for d in e.documentos if not d.anexo_de]
    for p in principais:
        anexos = [a for a in e.documentos if a.anexo_de == p.doc_id]
        s = f"#{p.doc_id} {p.tipo}" + (f" ({p.titulo})" if p.titulo else "")
        if anexos:
            s += " + anexos: " + "; ".join(f"#{a.doc_id} {a.tipo}" + (f" ({a.titulo})" if a.titulo else "")
                                            for a in anexos[:8])
        docs.append(s)
    movs = " / ".join(e.movimentos)
    return f"{e.data:%d/%m/%Y} {e.hora} [{e.polo}] {movs}{' | ' if movs and docs else ''}{' | '.join(docs)}"


def montar_dossie(numero: str, capa, f: Fatos, eventos: list[Evento],
                  trechos: list[tuple[DocRef, str, str, str]], hoje: date, sistema: str = "pje") -> str:
    ref = f.data_ultima_sentenca
    if ref:
        recentes = [e for e in eventos if e.data >= ref - timedelta(days=45)]
        antigos = [e for e in eventos if e.data < ref - timedelta(days=45)
                   and any(k in norm(" ".join(e.movimentos) + " ".join(d.tipo for d in e.documentos))
                           for k in ("contestacao", "acordo", "julgad", "sentenca", "apolice"))]
        linhas = recentes[:MAX_LINHAS_TIMELINE] + antigos[:15]
    else:
        linhas = eventos[:60]
    partes = [
        f"DATA DE HOJE: {hoje:%d/%m/%Y}",
        f"PROCESSO: {numero} | SISTEMA: {sistema.upper()}",
        f"CLASSE: {capa.classe} | ÓRGÃO: {capa.orgao_julgador} | VALOR DA CAUSA: {capa.valor_causa}",
        "POLO ATIVO: " + "; ".join(capa.polo_ativo[:4]),
        "POLO PASSIVO: " + "; ".join(capa.polo_passivo[:6]),
        *(["ATENÇÃO: nesta fonte (consulta do TJRJ/DCP) as juntadas de petição NÃO identificam quem peticionou "
           "nem trazem o conteúdo. Não classifique como 'so_comprovante_sem_peticao' só por isso: guia de "
           "depósito paga seguida de juntadas de petição indica 'peticao_com_comprovante'."]
          if sistema.lower().startswith("pjerj") else []),
        "",
        "FATOS JÁ VERIFICADOS POR REGRA (confiáveis):",
        *[f"- {x}" for x in f.resumo()],
        "",
        f"TIMELINE (mais recente primeiro; {len(linhas)} de {len(eventos)} eventos):",
        *[_linha_evento(e) for e in linhas],
        "",
        "TRECHOS DE DOCUMENTOS:",
    ]
    for d, motivo, juntado, texto in trechos:
        partes.append(f"\n=== #{d.doc_id} | {d.tipo} ({d.titulo}) | {d.data:%d/%m/%Y} | "
                      f"{juntado or d.polo} | lido como: {motivo}")
        if d.anexos:
            partes.append("Anexos: " + "; ".join(d.anexos))
        partes.append(texto or "[sem texto extraível — provavelmente PDF escaneado]")
    return "\n".join(partes)


def _resolver_polos_esaj(autos, eventos, f, cli, cache: CacheDocs, cnj: str, maximo: int = 14) -> None:
    adv_reu, adv_ativo = esaj.advogados(autos.html)
    adv_reu |= {norm(a.nome) for a in perfil.atual().advogados if a.nome}
    ref = f.data_ultima_sentenca or (eventos[-1].data if eventos else None)
    corte = (ref - timedelta(days=30)) if ref else None
    pendentes = [(e, d) for e in eventos if e.polo == "PARTE" and (corte is None or e.data >= corte)
                 for d in e.documentos][:maximo]
    em_cache = cache.varios(autos.sistema, [d.doc_id for _, d in pendentes])
    for ev, d in pendentes:
        if d.doc_id in em_cache:
            texto = em_cache[d.doc_id]["texto"]
        else:
            try:
                ctype, dados, _ = cli.baixar_documento(autos, d.link_id, d.doc_id)
                texto, pags = extrair_texto(ctype, dados)
                cache.salvar(autos.sistema, d.doc_id, texto=texto, paginas=pags, tipo=d.tipo, titulo=d.titulo,
                             content_type=ctype)
            except Exception as e:
                log.warning("%s: e-SAJ #%s não baixou: %s", cnj, d.doc_id, e)
                continue
        ev.polo = esaj.polo_por_assinatura(texto, adv_reu, adv_ativo) or "TERCEIRO"


RE_PETICAO_SEM_AUTOR = re.compile(r"peti[cç][aã]o|impugna[cç][aã]o|embargos|exce[cç][aã]o|manifesta[cç][aã]o|guias? de",
                                  re.I)
RE_OAB = re.compile(r"([A-ZÀ-Ú][A-ZÀ-Ú .'-]{5,}?)\s+\(?(?:OAB[/ ]?)?[A-Z]{2}\s?\d{3,7}")


def _advs_capa(linhas: list[str]) -> set[str]:
    # eproc: "FULANO ( cpf ) - Pessoa Física JG ADVOGADO X MG123456" ("JG" = justiça gratuita)
    return {norm(re.sub(r"^(JG|PJ|PF)\s+", "", m.strip())) for ln in linhas for m in RE_OAB.findall(ln)
            if not perfil.eh_cliente(m)}


def polo_por_texto(texto: str, adv_reu: set[str], adv_ativo: set[str]) -> str | None:
    """Quem assinou/protocolou a peça: assinatura eletrônica, rodapé com o advogado ou abertura."""
    p = esaj.polo_por_assinatura(texto, adv_reu, adv_ativo)
    if p in ("PASSIVO", "ATIVO"):
        return p
    t = norm(texto)
    m = re.search(r"assinado eletronicamente por:? ([a-z .'-]{6,60})", t)
    nome = m.group(1).strip() if m else ""
    fim = t[-2500:]
    pf = perfil.atual()
    if (nome and any(a in nome or nome in a for a in adv_reu)) or re.search(pf.regex_advogados, fim):
        return "PASSIVO"
    if (nome and any(a in nome or nome in a for a in adv_ativo)) or any(a in fim for a in adv_ativo if len(a) > 8):
        return "ATIVO"
    v = re.search(r"\bvem\b|\bvem,", t[:4000])
    if v:
        antes = t[max(0, v.start() - 400):v.start()]
        citas = [b.start() for b in re.finditer(pf.regex_nomes, antes)]
        contra = r"contra|face|desfavor|contende|\bmove|movid|promov|requerid|executad|\breu\b|\bre:|litiga"
        sujeito_reu = bool(citas) and not re.search(contra, antes[max(0, citas[-1] - 80):citas[-1]])
        if sujeito_reu:
            return "PASSIVO"
        if nome and not re.search(r"juiz|escrivao|servidor|diretor|chefe|analista|tecnico", nome):
            return "ATIVO"  # assinou alguém que não é da ré
        if citas:  # a ré citada só como parte contrária
            return "ATIVO"
    return None


def _resolver_polos_peticoes(eventos, f, capa, cli, autos, docs_de, cache: CacheDocs, cnj: str,
                             maximo: int = 14) -> bool:
    adv_reu = {norm(a.nome) for a in perfil.atual().advogados if a.nome} | _advs_capa(capa.polo_passivo)
    adv_ativo = _advs_capa(capa.polo_ativo) - adv_reu
    ref = f.data_ultima_sentenca
    corte = (ref - timedelta(days=30)) if ref else (eventos[0].data - timedelta(days=365) if eventos else None)
    pend = [(e, d) for e in eventos if e.polo == "INTERNO" and (corte is None or e.data >= corte)
            and any(RE_PETICAO_SEM_AUTOR.search(m) for m in e.movimentos)
            for d in e.documentos[:1]][:maximo]
    mudou = False
    for ev, d in pend:
        c_doc, a_doc = docs_de.get(d.doc_id, (cli, autos))
        c = cache.obter(a_doc.sistema, d.doc_id)
        if c:
            texto = c["texto"]
        else:
            try:
                ctype, dados, _ = c_doc.baixar_documento(a_doc, d.link_id, d.doc_id)
                texto, pags = extrair_texto(ctype, dados)
                cache.salvar(a_doc.sistema, d.doc_id, texto=texto, paginas=pags, tipo=d.tipo, titulo=d.titulo,
                             content_type=ctype)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: petição #%s não baixou: %s", cnj, d.doc_id, e)
                continue
        polo = polo_por_texto(texto, adv_reu, adv_ativo)
        if polo:
            ev.polo = polo
            mudou = True
    return mudou


def _parse(autos: Autos):
    """(capa, eventos) com o leitor do sistema de onde a página veio."""
    if autos.sistema.startswith("esaj"):
        return esaj.parse_capa(autos.html), esaj.parse_timeline(autos.html)
    if autos.sistema.startswith("eproc"):
        return eproc.parse_capa(autos.html), eproc.parse_timeline(autos.html)
    if autos.sistema.startswith("pjerj"):
        return tjrj.parse_capa(autos.html), tjrj.parse_timeline(autos.html)
    if autos.sistema.startswith("portaladv"):
        return tjse.parse_capa(autos.html), tjse.parse_timeline(autos.html)
    if autos.sistema.startswith("projudi") and 'class="projudi-go-advs"' in autos.html:  # TJGO
        return projudi_go.parse_capa(autos.html), projudi_go.parse_timeline(autos.html)
    if autos.sistema.startswith("projudi") and 'class="projudi-partes"' in autos.html:  # TJPR/TJAM/TJRR
        return projudi_pr.parse_capa(autos.html), projudi_pr.parse_timeline(autos.html)
    if autos.sistema.startswith("projudi"):
        return projudi.parse_capa(autos.html), projudi.parse_timeline(autos.html)
    return parse_capa(autos.html), parse_timeline(autos.html)


RE_CLASSE_EXEC = re.compile(r"cum|exec|exe?ti|liq", re.I)  # siglas PJe: CumSen, CumPrSe, ExTiEx, LiqSen...


def apensos_pje(cli, autos: Autos, capa, cnj: str) -> list[tuple]:
    """Cumprimento de sentença em autos apartados no PJe (aba Associados > Dependência).
    Entra no grupo só o associado por dependência em que a ré é parte e um dos dois é
    execução/cumprimento: prevenção (outra ação do mesmo autor) não entra."""
    if not capa.associados or not hasattr(cli, "associados"):
        return []
    try:
        lista = cli.associados(autos)
    except Exception as e:  # noqa: BLE001
        log.warning("%s: aba Associados não abriu: %s", cnj, e)
        return []
    principal_exec = bool(RE_CLASSE_EXEC.search(capa.classe or "")) or "cumprimento" in norm(capa.classe or "")
    saida = []
    for a in lista:
        if a["numero"] == cnj or a["tipo"].lower().startswith("preven") or not perfil.eh_cliente(a["descricao"]):
            continue
        if RE_CLASSE_EXEC.search(a["classe"]) or principal_exec:
            rot = "APENSO CS" if RE_CLASSE_EXEC.search(a["classe"]) else "PROCESSO PRINCIPAL"
            saida.append((cli, a["numero"], f"{rot} {a['classe']}"))
    log.info("%s: associados %s -> no grupo %s", cnj, [(a["numero"], a["classe"], a["tipo"]) for a in lista],
             [n for _, n, _ in saida])
    return saida[:4]


def _nomes(linhas: list[str]) -> set[str]:
    """Nomes do polo (parte e advogados), sem OAB/CPF/qualificação."""
    out = set()
    for ln in linhas or []:
        for pedaco in re.split(r"\s+registrad[oa]\(a\) civilmente como\s+|;", ln):
            n = norm(re.split(r"\s+-\s+|\s*\(|,", pedaco)[0]).strip()
            if len(n) >= 8 and not perfil.eh_cliente(n):
                out.add(n)
    return out


def apenso_confirmado(capa_principal: dict, capa_cand) -> str | None:
    """Número citado nos autos é o cumprimento apenso (ou o processo principal) deste caso?
    Exige a ré (ou advogado do escritório) no polo passivo, alguém do polo ativo em comum (autor ou
    advogado) e que exatamente um dos dois seja execução/cumprimento: dois cumprimentos do mesmo
    autor costumam ser ações diferentes (outro contrato). Devolve o rótulo do grupo ou None."""
    passivo = " ".join(capa_cand.polo_passivo or [])
    if not perfil.eh_cliente(passivo) and not perfil.atual().advogado_no_texto(passivo):
        return None
    if not (_nomes(capa_principal.get("polo_ativo")) & _nomes(capa_cand.polo_ativo)):
        return None
    cand_exec = bool(RE_CLASSE_EXEC.search(norm(capa_cand.classe or "")))
    princ_exec = bool(RE_CLASSE_EXEC.search(norm(capa_principal.get("classe") or "")))
    if cand_exec and not princ_exec:
        return f"APENSO CS (citado nos autos) {capa_cand.classe[:40]}"
    if princ_exec and not cand_exec:
        return f"PROCESSO PRINCIPAL (citado nos autos) {capa_cand.classe[:40]}"
    return None


MAX_EVENTOS_SAIDA = 120


def _evento_saida(e: Evento) -> dict:
    return {"data": e.data.isoformat(), "hora": e.hora or "", "polo": e.polo, "movimentos": e.movimentos[:6],
            "documentos": [{"id": d.doc_id, "tipo": d.tipo, "titulo": d.titulo, "anexoDe": d.anexo_de or None}
                           for d in e.documentos[:12]]}


def analisar_processo(numero_txt: str, cli: PjeHttp, cache: CacheDocs, llm: AnalisadorLLM | None,
                      hoje: date | None = None, anterior: dict | None = None, forcar_llm: bool = False,
                      complemento=None) -> dict:
    """Abre os autos, extrai os fatos, lê os documentos que pesam no risco e classifica.

    `anterior`: {"assinatura", "checklist"} da última análise deste processo. Timeline igual
    (mesma assinatura) reaproveita o checklist do LLM; o risco é sempre recalculado (prazos
    dependem de `hoje`).
    `complemento`: outro processo que entra no mesmo grupo/timeline, como
    (cliente, número[, rótulo]) ou uma função autos -> essa tupla (ou None).
    Usos: continuação migrada do e-SAJ para o eproc com número novo (rótulo "MIGRADO P/ EPROC")
    e processo de origem de um recurso do eproc 2º grau (rótulo "ORIGINÁRIO 1º GRAU").
    Os documentos de cada processo são baixados pelo cliente de onde vieram."""
    hoje = hoje or date.today()
    numero = cnjmod.parse(numero_txt)
    t0 = time.perf_counter()
    req0 = cli.n_req
    for tentativa in (1, 2, 3):
        # PB (Cloudflare) e RN (Akamai) às vezes devolvem a página pela metade: sem timeline ou sem
        # o link dos autos no "Abrir". Na segunda tentativa a mesma consulta vem completa.
        try:
            autos: Autos = cli.abrir_autos(numero)
        except RuntimeError as e:
            if "URL dos autos" not in str(e) or tentativa == 3:
                raise
            log.warning("%s: %s; tentando de novo", numero.formatado, e)
            time.sleep(5 * tentativa)
            continue
        capa, eventos = _parse(autos)
        if eventos:
            break
        if tentativa == 3:  # leitor não reconheceu a página: nunca classificar sem timeline
            raise RuntimeError(f"timeline vazia em {autos.sistema} (layout não reconhecido)")
        log.warning("%s: timeline vazia em %s; tentando de novo", numero.formatado, autos.sistema)
        time.sleep(5 * tentativa)
    sistema_principal = autos.sistema
    f = extrair(capa, eventos)
    if autos.sistema.startswith("esaj"):
        # A pasta do e-SAJ não diz quem juntou cada petição: resolve pelo assinante do PDF
        # (só nas peças posteriores à sentença, que são as que pesam no risco).
        _resolver_polos_esaj(autos, eventos, f, cli, cache, numero.formatado)
        f = extrair(capa, eventos)
    docs_de: dict[str, tuple] = {}
    if callable(complemento):
        complemento = complemento(autos)
    grupo = [complemento] if complemento and not isinstance(complemento, list) else list(complemento or [])
    if not grupo and not autos.sistema.startswith(("esaj", "eproc", "portaladv")):
        grupo = apensos_pje(cli, autos, capa, numero.formatado)  # e-SAJ/eproc/TJSE já trazem o grupo
    anexados = []
    for comp in grupo:
        cli2, num2, rotulo = (tuple(comp) + ("MIGRADO P/ EPROC",))[:3]
        num2 = cnjmod.parse(num2)
        try:
            autos2 = cli2.abrir_autos(num2)
        except ProcessoNaoEncontrado:
            log.warning("%s: processo complementar %s não encontrado", numero.formatado, num2.formatado)
            continue
        _, ev2 = _parse(autos2)
        prefixo = f"[RELACIONADO {num2.formatado} - {rotulo}] "
        for e in ev2:
            e.movimentos = [prefixo + m for m in e.movimentos]
            for d in e.documentos:
                docs_de[d.doc_id] = (cli2, autos2)
        eventos = sorted(eventos + ev2, key=lambda e: (e.data, e.hora or ""), reverse=True)
        for i, e in enumerate(eventos):
            e.seq = i
        autos.paginas += autos2.paginas
        anexados.append((autos2, num2, rotulo))
        log.info("%s: grupo %s + %s %s %s (%d eventos)", numero.formatado, autos.sistema, autos2.sistema,
                 num2.formatado, rotulo, len(ev2))
    if anexados:
        f = extrair(capa, eventos)
        f.migracoes = []  # a continuação no eproc já está na timeline
    if autos.sistema.startswith("eproc") or any(a.sistema.startswith("eproc") for a, *_ in anexados):
        # processos migrados para o eproc: as petições da fase anterior chegam como "Juntada de Petição"
        # sem dizer quem peticionou; o polo sai do texto (assinatura/abertura)
        if _resolver_polos_peticoes(eventos, f, capa, cli, autos, docs_de, cache, numero.formatado):
            f = extrair(capa, eventos)
        f.migracoes = []  # no eproc o evento de migração é só histórico (chegou aqui)

    assinatura = _assinatura(eventos)
    uso: dict = {}
    selecao = selecionar(f, eventos)
    reaproveitar = bool(anterior and anterior.get("assinatura") == assinatura and anterior.get("checklist")
                        and not forcar_llm)
    # Documentos sempre passam pelos sinais (do cache quando possível): as regras de
    # risco dependem deles mesmo quando o checklist do LLM é reaproveitado.
    anexos_de: dict[str, list] = {}
    for e in eventos:
        for doc in e.documentos:
            if doc.anexo_de:
                anexos_de.setdefault(doc.anexo_de, []).append(doc)

    def obter(doc_id: str, link_id: str, tipo: str, titulo: str) -> tuple[str, str]:
        c_doc, a_doc = docs_de.get(doc_id, (cli, autos))
        sistema_doc = a_doc.sistema if a_doc is not autos else sistema_principal
        c = cache.obter(sistema_doc, doc_id)
        if c:
            return c["texto"], c["juntado_por"]
        try:
            ctype, dados, juntado = c_doc.baixar_documento(a_doc, link_id, doc_id)
            texto, pags = extrair_texto(ctype, dados)
            cache.salvar(sistema_doc, doc_id, texto=texto, juntado_por=juntado, paginas=pags, tipo=tipo,
                         titulo=titulo, content_type=ctype)
            return texto, juntado
        except Exception as e:  # um documento ruim não derruba a análise
            log.warning("%s: falha ao baixar #%s: %s", numero.formatado, doc_id, e)
            return f"[falha ao baixar: {e}]", ""

    trechos = []
    for d, cabeca, cauda, motivo in selecao:
        texto, juntado = obter(d.doc_id, d.link_id, d.tipo, d.titulo)
        if len(texto.strip()) < 300 and anexos_de.get(d.doc_id):
            # petição principal vazia ("ANEXO"): o conteúdo está no 1º anexo
            filhos = anexos_de[d.doc_id]
            alvo = next((a for a in filhos if norm(a.tipo) in ("peticao", "manifestacao",
                         "impugnacao ao cumprimento de sentenca", "impugnacao")), filhos[0])
            t2, _ = obter(alvo.doc_id, alvo.link_id, alvo.tipo, alvo.titulo)
            texto = f"{texto}\n[conteúdo do anexo #{alvo.doc_id} {alvo.tipo} ({alvo.titulo})]\n{t2}"
        sinais = aplicar_sinais(f, d, texto)
        recorte = _recorte(texto, cabeca, cauda)
        if sinais:
            recorte = "SINAIS NO TEXTO INTEGRAL (verificados por regra): " + " | ".join(sinais) + "\n" + recorte
        trechos.append((d, motivo, juntado, recorte))
    docs_lidos = [d.doc_id for d, *_ in selecao]
    # Outros processos citados nos documentos lidos: possível apenso/CS em autos próprios.
    proprio = numero.formatado
    citados = sorted({m for _, _, _, t in trechos for m in re.findall(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", t)}
                     - {proprio})
    # processos que entraram no grupo (incidente e-SAJ, relacionado eproc/TJSE, apenso PJe)
    apensos = list(dict.fromkeys(
        re.sub(r"\s+", " ", m.group(1)).strip() for e in eventos for mv in e.movimentos[:1]
        if (m := re.match(r"\[(?:RELACIONADO|INCIDENTE) ([^\]]+)\]", mv))))
    if autos.sistema.startswith("esaj"):
        cls = esaj.classes_incidentes(autos.html)
        apensos = [f"{a} - {cls[a]}" if cls.get(a) else a for a in apensos]
    apensos = [a for a in apensos if a.split(" ")[0] != numero.formatado]
    no_grupo = {a.split(" ")[0] for a in apensos}
    vinculos = [f"NO GRUPO: {a}" for a in apensos] + \
               ([f"{capa.associados} processo(s) associado(s) no tribunal"] if capa.associados else []) + \
               [f"citado nos autos: {c}" for c in citados if c not in no_grupo][:5]
    if reaproveitar:
        checklist = anterior["checklist"]
        uso = {"reaproveitado": True}
        log.info("%s: timeline sem mudança — reaproveitando checklist", numero.formatado)
    else:
        dossie = montar_dossie(numero.formatado, capa, f, eventos, trechos, hoje, autos.sistema)
        if llm is None:
            raise RuntimeError("LLM não configurado (OPENAI_API_KEY)")
        checklist, uso = llm.analisar(dossie)
        uso["chars_dossie"] = len(dossie)

    risco = classificar(checklist, f, hoje)
    if anexados:
        autos.sistema = "+".join(dict.fromkeys([autos.sistema] + [a.sistema for a, *_ in anexados]))
    uso.update({"requisicoes": cli.n_req - req0, "segundos_total": round(time.perf_counter() - t0, 1),
                "paginas_timeline": autos.paginas, "n_eventos": len(eventos)})
    return {
        "cnj": numero.formatado, "sistema": autos.sistema, "data_analise": hoje.isoformat(),
        "assinatura": assinatura,
        "grau_risco": risco.grau, "ha_risco": risco.ha_risco, "fase": risco.fase,
        "conclusao": risco.conclusao, "motivos": risco.motivos, "acao_sugerida": risco.acao_sugerida,
        "etapa": etapa(risco), "categoria": categoria(risco),
        "bloqueio_realizado": risco.ha_risco == "BLOQUEIO JÁ REALIZADO",
        "checklist": risco.checklist, "checklist_llm": checklist,
        "fatos": {"resumo": f.resumo(), "juizado": f.juizado, "advogado_habilitado": f.advogado_habilitado,
                  "vinculos": vinculos, "apensos": apensos,
                  "citados": [c for c in citados if c not in no_grupo][:10]},
        "docs_lidos": docs_lidos, "uso": uso,
        "capa": asdict(capa) if hasattr(capa, "__dataclass_fields__") else {},
        "eventos": [_evento_saida(e) for e in eventos[:MAX_EVENTOS_SAIDA]],
        "n_eventos": len(eventos),
    }
