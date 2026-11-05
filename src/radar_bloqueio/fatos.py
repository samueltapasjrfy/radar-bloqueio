"""Fatos determinísticos extraídos da timeline (custo zero, sem LLM).

Os nomes de movimento seguem a Tabela Processual Unificada (TPU/CNJ), então valem para
qualquer PJe. Servem para (1) escolher quais documentos baixar e (2) dar ao LLM pistas
já verificadas, com data e ID.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from . import perfil
from .timeline import Capa, Documento, Evento


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower().strip()


# --- padrões (sobre texto normalizado, sem acento) ---------------------------------
RE_SENTENCA_MOV = re.compile(
    r"^(julgad[oa]s? (procedente|improcedente)|julgado procedente em parte|"
    r"homologad[ao] a transacao|homologad[ao] o acordo|extinto o processo|"
    r"julgad[oa]s? (procedentes|improcedentes) os pedidos|concedida a seguranca|"
    r"denegada a seguranca|homologada a desistencia|julgado extinto)")
RE_TRANSITO_MOV = re.compile(r"transitad[oa] em julgado\D{0,25}(\d{2}/\d{2}/\d{4})")
RE_RECURSO_MOV = re.compile(r"remetidos os autos \(em grau de recurso\)|"
                            r"juntada de peticao de (apelacao|recurso inominado|recurso)")
RE_RETORNO_MOV = re.compile(r"recebidos os autos|baixa definitiva|retorno dos autos")
# mudança de classe só conta no sentido PARA cumprimento/execução ("retificada de CS para
# procedimento comum" é o contrário)
RE_CLASSE_CS = re.compile(r"(classe( processual)? (retificada|evoluida)|evolucao da classe( processual)?|evoluida a classe)"
                          r".*\bpara:? (\[civel\] )?(cumprimento de sentenca|execucao)")
RE_CS_MOV = re.compile(RE_CLASSE_CS.pattern +
                       r"|iniciad[oa] (a execucao|o cumprimento)|cumprimento de sentenca iniciado"
                       r"|juntada de peticao de (cumprimento de sentenca|execucao)"
                       # e-SAJ "Execução/Cumprimento de Sentença Iniciada (o)", eproc "Execução ou cumprimento de
                       # sentença Iniciada (o)", DCP/TJRJ "Início da Execução", peça "Cumprimento Definitivo de Sentença"
                       r"|^(execucao|cumprimento)[ /a-z]{0,40}iniciad|^inicio da execucao"
                       r"|cumprimento (definitivo|provisorio) de sentenca|execucao (definitiva|provisoria)/")


def re_decorrido_cliente() -> re.Pattern:
    """"Decorrido prazo de <cliente ou advogado do escritório> em dd/mm/aaaa"."""
    p = perfil.atual()
    return re.compile(rf"decorrido prazo de ({p.regex_nomes}|{p.regex_advogados})[^0-9]*(\d{{2}}/\d{{2}}/\d{{4}})")


RE_BAIXA_RECURSO = re.compile(r"^recebidos os autos\.?$|certidao de baixa|baixa definitiva|recebidos os autos do tribunal|"
                              r"retorno dos autos|autos devolvidos (pelo|do) tribunal|recebidos os autos da turma")
RE_PERICIA_CALCULO = re.compile(r"(nomead[oa]|designad[oa]|determinad[oa]).{0,40}(perit|pericia)|pericia contabil|"
                                r"remessa (dos autos )?(a|ao) contador|remetidos os autos (a|ao) contador|"
                                r"contadoria judicial|calculos (do|pelo) contador")
RE_DESBLOQUEIO = re.compile(r"pedido de desbloqueio|defiro.{0,60}desbloqueio|minuta de desbloqueio|"
                            r"desbloqueio de (valores|ativos)|determino o desbloqueio")
RE_ORDEM_BLOQUEIO = re.compile(r"determinad[oa] o bloqueio|deferid[oa] (o )?bloqueio|penhora on ?-?line determinada"
                               r"|penhora on ?-?line - central sisbajud")
# TJGO: "Certidão Expedida - PENHORA ONLINE - CENTRAL SISBAJUD" = autos encaminhados à central para PROTOCOLAR a
# ordem (bloqueio iminente), não o resultado
RE_ENVIO_CENTRAL_SISBAJUD = re.compile(r"penhora on ?-?line - central sisbajud")
RE_BLOQUEIO_FEITO = re.compile(
    r"(realizad[oa]|efetivad[oa]|positiv[oa]|cumprid[oa]).{0,40}(bloqueio|penhora|sisbajud|bacenjud)"
    r"|(bloqueio|penhora|sisbajud|bacenjud).{0,40}(realizad[oa]|efetivad[oa]|positiv[oa]|parcial|total)"
    r"|transferencia de valores|desbloqueio|penhora (on-?line|realizada)|juntada de (resultado|detalhamento).{0,30}(sisbajud|bacenjud)")
RE_BLOQUEIO_DOC = re.compile(r"sisbajud|bacenjud|bloqueio|detalhamento de ordem|penhora")
RE_ALVARA = re.compile(r"alvara|levantamento|transferencia eletronica")
RE_EXTINCAO = re.compile(r"extinta a execucao|extinto o cumprimento|arquivado definitivamente|"
                         r"baixa definitiva|satisfacao da obrigacao|extinta a obrigacao")
# sentença que extingue a execução (não basta arquivar): encerra o risco mesmo sem o pagamento à vista
# vale o NOME do movimento ou o dispositivo explícito; "satisfação da obrigação" solto aparece em
# fundamentação e em relatórios
RE_EXTINCAO_EXEC = re.compile(r"^(extinta a execucao|extinto o cumprimento|extinta a obrigacao)"
                              r"|julgo extint[ao] (a |o )?(presente )?(execucao|cumprimento)|extingo a (execucao|presente execucao)")
RE_TRANSACAO = re.compile(r"^homologad[ao] (a transacao|o acordo)")
# suspensão do processo (morte da parte/herdeiros, sobrestamento): atos executivos ficam vedados (CPC 313/314).
# Suspensão para cumprir acordo NÃO conta: descumprido o acordo, a execução volta (é o caso típico de bloqueio).
RE_SUSPENSAO = re.compile(r"^(autos suspensos|processo suspenso|suspens[ao] do processo|suspenso o processo|"
                          r"determinada a suspensao|sobrestad)|suspensao do processo por (morte|falecimento)|"
                          r"habilitacao de herdeiros|falecimento d[ao] (autor|autora|parte|exequente)|"
                          r"obito d[ao] (autor|autora|parte|exequente)")
RE_SUSPENSAO_ACORDO = re.compile(r"acordo|transacao|parcelamento")
RE_FIM_SUSPENSAO = re.compile(r"levantad[ao] a suspensao|levantad[ao] o sobrestamento|retomad[ao]|"
                              r"habilitacao (deferida|homologada)|homologad[ao] a habilitacao|deferida a habilitacao")
# impugnação/embargos/exceção do executado juntados e julgamento deles, pelo NOME do movimento
RE_IMPUG_MOV = re.compile(r"impugnacao ao cumprimento|impugnacao a execucao|impugnacao (aos )?calculos?\b|"
                          r"embargos a execucao|embargos do devedor|excecao de pre-?executividade")
RE_IMPUG_POS_BLOQUEIO = re.compile(r"impugnacao a penhora|impugnacao ao bloqueio|desbloqueio|manifestacao sobre a impugnacao")
RE_IMPUG_JULG_MOV = re.compile(
    r"(nao[- ]?acolh|acolh|rejeit|julg|improceden|proceden|indefer)\w*.{0,80}(impugna|excecao|embargos a exec)"
    r"|(impugna|excecao de pre|embargos a exec)\w*.{0,80}(nao[- ]?acolh|acolhid|rejeitad|julgad|improceden|proceden)"
    r"|nao-acolhimento|homologo os calculos|homologad[oa]s? os calculos")
# sinais, no texto dos movimentos, de que o executado já foi intimado a pagar (art. 523) ou o prazo passou
RE_INTIMACAO_PAGAR = re.compile(
    r"decurso (do |de )?prazo sem (noticia de )?pagamento|sem (noticia de )?pagamento voluntario|"
    r"nao (houve|efetuou|realizou) o? ?pagamento|pagamento voluntario|art(igo)?\.? ?523|"
    r"intim\w* (o|a)s? (executad|devedor|banco|re\b|requerid)[^.]{0,100}(pag|deposit)|"
    r"para (efetuar o )?pagamento (do debito|da condenacao|do valor)|sob pena de (multa|penhora)")
# ordem para a ré depositar honorários periciais (não pagar = penhora, mesmo antes da sentença) e indícios de depósito
RE_HONOR_PERITO = re.compile(r"(intim|deposit|recolh|pag)\w*[^.]{0,80}honorarios (periciais|do perito)|"
                             r"honorarios (periciais|do perito)[^.]{0,80}(sob pena|no prazo|deposit)")


def re_honor_perito_cliente() -> re.Pattern:
    return re.compile(rf"\b(re|reu|requerid[oa]|banco|executad[oa]|parte re|{perfil.atual().regex_nomes})\b")


# depósito PRÉVIO para a perícia acontecer: a sanção é perder a prova (preclusão), não penhora
RE_HONOR_PERITO_PRECLUSAO = re.compile(r"preclus|perda da prova|sob pena de (nao realizacao|desistencia)|"
                                       r"para (o )?inicio d(os|a) (trabalhos|pericia)|antecipa")
RE_DEPOSITO = re.compile(r"deposito|guia|comprovante|honorarios periciais (depositados|pagos|recolhidos)|"
                         r"mandado de pagamento em favor do perito|levantamento (pelo|em favor do) perito")
# atos do advogado que não pedem nada (ciência, confirmação de intimação)
RE_ATO_ROTINA = re.compile(r"ciencia|renuncia ao prazo|confirmada a intimacao|comunicacoes|decorrido prazo")
RE_PAGTO_ANEXO = re.compile(r"comprovante|guia|deposito|pagamento|idcusta|pasta\d+valor|dje|boleto|"
                            r"transferencia|ted\b|recibo")
RE_MIGRACAO = re.compile(r"migrada a tramitacao|migrado para o (sistema|eproc)|remetidos os autos para o (eproc|sistema)|"
                         r"migracao para outro sistema|passara a tramitar eletronicamente no sistema eproc")
# aviso de migração FUTURA da vara ("após migrado... será lançada a movimentação"), não migração do processo
RE_MIGRACAO_FUTURA = re.compile(r"apos migrado|sera lancada a movimentacao|cronograma (de|dessa) implantacao")
RE_IMPUG_JULGADA = re.compile(r"(acolhida|rejeitada|acolhida em parte|nao acolhida|julgada procedente|julgada improcedente)"
                              r"s? a impugnacao|embargos a execucao (acolhidos|rejeitados|julgados)|"
                              r"(procedentes|improcedentes) os embargos|excecao de pre-?executividade (acolhida|rejeitada)")
RE_APOLICE = re.compile(r"apolice|seguro[ -]?garantia|garantia do juizo|carta de fianca|fianca bancaria")
RE_IMPUG = re.compile(r"impugnacao ao cumprimento|impugnacao a execucao|embargos a execucao|"
                      r"embargos do devedor|excecao de pre-?executividade|impugnacao aos calculos|"
                      r"impugnacao a penhora")


@dataclass
class DocRef:
    data: date
    polo: str
    doc_id: str
    tipo: str
    titulo: str
    anexos: list[str] = field(default_factory=list)  # "tipo (titulo)" dos anexos
    link_id: str = ""
    movs: str = ""   # movimentos do evento em que a peça foi juntada (podem dizer quem peticionou)

    def linha(self) -> str:
        a = f" | anexos: {'; '.join(self.anexos)}" if self.anexos else ""
        return f"{self.data:%d/%m/%Y} [{self.polo}] #{self.doc_id} {self.tipo} ({self.titulo}){a}"


@dataclass
class Fatos:
    juizado: bool
    classe: str
    advogado_habilitado: bool | None   # None = perfil sem advogados do escritório
    sentencas: list[tuple[date, str, str]] = field(default_factory=list)   # (data, movimento, doc_id)
    docs_sentenca: list[DocRef] = field(default_factory=list)
    acordaos: list[DocRef] = field(default_factory=list)
    certidoes_transito: list[DocRef] = field(default_factory=list)
    transito_mov: list[date] = field(default_factory=list)
    recursos: list[tuple[date, str]] = field(default_factory=list)
    cs_inicio: list[tuple[date, str]] = field(default_factory=list)
    decorridos_reu: list[date] = field(default_factory=list)
    bloqueios: list[tuple[date, str]] = field(default_factory=list)
    alvaras: list[tuple[date, str]] = field(default_factory=list)
    extincoes: list[tuple[date, str]] = field(default_factory=list)
    extincoes_execucao: list[tuple[date, str]] = field(default_factory=list)  # por sentença (inclui acordo)
    classe_cs_migracao: list[date] = field(default_factory=list)  # classe ajustada na migração PJe->eproc
    sinais_intimacao_pagar: list[date] = field(default_factory=list)
    honorarios_perito_pendentes: tuple[date, str] | None = None  # ordem à ré sem depósito depois
    suspensao: tuple[date, str] | None = None        # suspensão em vigor (não levantada)
    impugnacao_pendente_mov: date | None = None       # impugnação juntada e ainda sem julgamento (movimentos)
    migracoes: list[tuple[date, str]] = field(default_factory=list)
    ordens_bloqueio: list[tuple[date, str]] = field(default_factory=list)
    baixas_recurso: list[tuple[date, str]] = field(default_factory=list)
    pericias_calculo: list[tuple[date, str]] = field(default_factory=list)
    desbloqueios: list[tuple[date, str]] = field(default_factory=list)
    sinal_pagamento_reu: list[tuple[date, str]] = field(default_factory=list)  # do texto integral
    impug_julgadas: list[tuple[date, str]] = field(default_factory=list)
    peticoes_reu: list[DocRef] = field(default_factory=list)
    peticoes_autor: list[DocRef] = field(default_factory=list)
    decisoes: list[DocRef] = field(default_factory=list)
    pagamentos_reu: list[DocRef] = field(default_factory=list)   # petições da ré com anexo de pagamento
    apolices_reu: list[DocRef] = field(default_factory=list)
    impugnacoes_reu: list[DocRef] = field(default_factory=list)

    atividade_apos_extincao: bool = False  # processo relacionado/incidente se moveu depois
    outros_reus: int = 0  # corréus no polo passivo (além do cliente): petição do polo passivo pode não ser dele
    cs_apenso_ativo: date | None = None  # último movimento relevante do cumprimento em autos apartados

    @property
    def data_ultima_sentenca(self) -> date | None:
        datas = [s[0] for s in self.sentencas] + [d.data for d in self.docs_sentenca]
        return max(datas) if datas else None

    def resumo(self) -> list[str]:
        """Pistas para o LLM (fatos verificados por regra, com data)."""
        f = []
        f.append(f"Rito: {'JUIZADO ESPECIAL (Lei 9.099)' if self.juizado else 'JUSTIÇA COMUM (CPC)'}; classe atual: {self.classe}")
        if self.advogado_habilitado is not None:
            f.append(f"Advogado do escritório ({perfil.atual().nomes_advogados}) cadastrado no polo passivo: "
                     f"{'SIM' if self.advogado_habilitado else 'NÃO'}")
        if self.cs_apenso_ativo:
            f.append(f"Cumprimento/liquidação em autos apartados (processo relacionado) com movimento em "
                     f"{self.cs_apenso_ativo:%d/%m/%Y}, depois da sentença: o cumprimento FOI iniciado")
        for d, m, i in self.sentencas:
            f.append(f"Movimento de julgamento: {d:%d/%m/%Y} — {m}" + (f" (#{i})" if i else ""))
        for d in self.transito_mov:
            f.append(f"Movimento 'Transitado em julgado em {d:%d/%m/%Y}'")
        for d, m in self.recursos:
            f.append(f"Recurso/remessa: {d:%d/%m/%Y} — {m}")
        for d, m in self.cs_inicio:
            f.append(f"Indício de início de cumprimento de sentença: {d:%d/%m/%Y} — {m}")
        for d in self.decorridos_reu[:6]:
            f.append(f"Decorrido prazo da ré ({perfil.atual().nome}) em {d:%d/%m/%Y}")
        for d, m in self.baixas_recurso[:3]:
            f.append(f"RETORNO/BAIXA DO RECURSO (recurso já julgado): {d:%d/%m/%Y} — {m}")
        for d, m in self.desbloqueios[:3]:
            f.append(f"DESBLOQUEIO PEDIDO/DEFERIDO (prova que houve bloqueio): {d:%d/%m/%Y} — {m[:200]}")
        for d, m in self.pericias_calculo[:2]:
            f.append(f"PERÍCIA/CONTADORIA PARA APURAR VALOR: {d:%d/%m/%Y} — {m[:200]}")
        for d, m in self.ordens_bloqueio:
            f.append(f"ORDEM DE BLOQUEIO/PENHORA ON-LINE: {d:%d/%m/%Y} — {m}")
        for d, m in self.bloqueios:
            f.append(f"Indício de bloqueio/penhora: {d:%d/%m/%Y} — {m}")
        for d, m in self.alvaras[:5]:
            f.append(f"Alvará/levantamento: {d:%d/%m/%Y} — {m}")
        for d, m in self.extincoes:
            f.append(f"Extinção/arquivamento: {d:%d/%m/%Y} — {m}")
        for d, m in self.extincoes_execucao:
            f.append(f"SENTENÇA EXTINGUIU A EXECUÇÃO (pagamento/acordo): {d:%d/%m/%Y} — {m}")
        if self.honorarios_perito_pendentes:
            d, m = self.honorarios_perito_pendentes
            f.append(f"ORDEM À RÉ PARA DEPOSITAR HONORÁRIOS PERICIAIS em {d:%d/%m/%Y}, sem depósito visível depois — {m[:160]}")
        if self.suspensao:
            f.append(f"PROCESSO SUSPENSO desde {self.suspensao[0]:%d/%m/%Y} — {self.suspensao[1][:160]}")
        if self.impugnacao_pendente_mov:
            f.append(f"Impugnação/embargos/exceção do executado juntada em {self.impugnacao_pendente_mov:%d/%m/%Y} "
                     "e ainda sem julgamento nos movimentos")
        for d in self.classe_cs_migracao[:1]:
            f.append(f"Classe 'Cumprimento de Sentença' ajustada na MIGRAÇÃO de sistema em {d:%d/%m/%Y}: "
                     "não é, por si, início do cumprimento")
        for d, m in self.impug_julgadas:
            f.append(f"JULGAMENTO DE IMPUGNAÇÃO/EMBARGOS: {d:%d/%m/%Y} — {m}")
        for d, m in self.migracoes:
            f.append(f"PROCESSO MIGRADO DE SISTEMA: {d:%d/%m/%Y} — {m}")
        return f


def _docref(ev: Evento, doc: Documento, anexos: list[Documento]) -> DocRef:
    return DocRef(ev.data, ev.polo, doc.doc_id, doc.tipo, doc.titulo,
                  [f"{a.tipo} ({a.titulo})" if a.titulo else a.tipo for a in anexos], doc.link_id,
                  " / ".join(ev.movimentos))


def _data_br(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%Y").date()


def _do_cliente(m: str) -> bool:
    """Falso quando o texto só fala de outro banco (litisconsorte: a extinção é da execução contra ele)."""
    bancos = set(re.findall(r"\bbanco ([a-z0-9]+)", m))
    return not bancos or perfil.atual().eh_cliente(m)


def _mov_limpo(m: str) -> str:
    return re.sub(r"\s*-\s*id no pje:.*$", "",
                  re.sub(r"^(\[(relacionado|incidente) [^\]]*\] )?(evento \d+ - )?(juntado\(a\) - )?", "", norm(m)))


def extrair(capa: Capa, eventos: list[Evento]) -> Fatos:
    p = perfil.atual()
    f = Fatos(juizado=capa.juizado, classe=capa.classe,
              advogado_habilitado=p.advogado_no_texto(" ".join(capa.polo_passivo)) if p.advogados else None)
    f.outros_reus = len([x for x in capa.polo_passivo if not p.eh_cliente(x) and not RE_NAO_PARTE.search(norm(x))])
    re_decorrido = re_decorrido_cliente()
    re_honor_cliente = re_honor_perito_cliente()
    vistos: set[str] = set()
    datas_migracao = {ev.data for ev in eventos
                      if any(RE_MIGRACAO.search(norm(m)) and not RE_MIGRACAO_FUTURA.search(norm(m))
                             for m in ev.movimentos)}
    for ev in eventos:  # mais recente primeiro
        # eproc prefixa "Evento N - "; as regras olham só a descrição do movimento
        # e, nos processos migrados do PJe, "Juntado(a) - <movimento original> - ID no PJe: ..."
        movs = [_mov_limpo(m) for m in ev.movimentos]
        doc_principal_id = next((d.doc_id for d in ev.documentos if not d.anexo_de), "")
        for m, original in zip(movs, ev.movimentos):
            if RE_SENTENCA_MOV.search(m):
                f.sentencas.append((ev.data, original, doc_principal_id))
            if t := RE_TRANSITO_MOV.search(m):
                f.transito_mov.append(_data_br(t.group(1)))
            elif re.match(r"transito em julgado", m):  # e-SAJ: sem data no texto
                f.transito_mov.append(ev.data)
            if RE_RECURSO_MOV.search(m):
                f.recursos.append((ev.data, original))
            if RE_CS_MOV.search(m):
                if RE_CLASSE_CS.search(m) and ev.data in datas_migracao:
                    f.classe_cs_migracao.append(ev.data)
                else:
                    f.cs_inicio.append((ev.data, original))
            if t := re_decorrido.search(m):
                f.decorridos_reu.append(_data_br(t.groups()[-1]))
            if RE_BLOQUEIO_FEITO.search(m) and "sob pena" not in m and "intime-se" not in m \
                    and not RE_ENVIO_CENTRAL_SISBAJUD.search(m):
                f.bloqueios.append((ev.data, original))
            if RE_ALVARA.search(m):
                f.alvaras.append((ev.data, original))
            if RE_EXTINCAO.search(m):
                f.extincoes.append((ev.data, original))
            if RE_EXTINCAO_EXEC.search(m) and _do_cliente(m):
                f.extincoes_execucao.append((ev.data, original))
            if RE_DESBLOQUEIO.search(m):
                f.desbloqueios.append((ev.data, original))
            if RE_PERICIA_CALCULO.search(m):
                f.pericias_calculo.append((ev.data, original))
            if RE_BAIXA_RECURSO.search(m):
                f.baixas_recurso.append((ev.data, original))
            if RE_ORDEM_BLOQUEIO.search(m):
                f.ordens_bloqueio.append((ev.data, original))
            if RE_MIGRACAO.search(m) and not RE_MIGRACAO_FUTURA.search(m):
                f.migracoes.append((ev.data, original))
            if RE_IMPUG_JULGADA.search(m):
                f.impug_julgadas.append((ev.data, original))

        principais = [d for d in ev.documentos if not d.anexo_de]
        for doc in principais:
            if doc.doc_id in vistos:
                continue
            vistos.add(doc.doc_id)
            anexos = [a for a in ev.documentos if a.anexo_de == doc.doc_id]
            ref = _docref(ev, doc, anexos)
            tipo, tudo = norm(doc.tipo), norm(f"{doc.tipo} {doc.titulo} " + " ".join(ref.anexos))
            if tipo == "sentenca":
                f.docs_sentenca.append(ref)
            elif tipo in ("acordao", "ementa", "decisao monocratica", "decisao terminativa") \
                    or ("2a instancia" in tipo and "acordao" in tudo):
                f.acordaos.append(ref)
            if "transito em julgado" in tudo or "certidao de transito" in tudo:
                f.certidoes_transito.append(ref)
            if tipo in ("decisao", "despacho", "decisao interlocutoria"):
                f.decisoes.append(ref)
            if RE_BLOQUEIO_DOC.search(tudo) and ev.polo in ("INTERNO", "SISTEMA") \
                    and not RE_ENVIO_CENTRAL_SISBAJUD.search(tudo):
                f.bloqueios.append((ev.data, f"documento #{doc.doc_id} {doc.tipo} ({doc.titulo})"))
            if "cumprimento de sentenca" in tudo and ev.polo == "ATIVO":
                f.cs_inicio.append((ev.data, f"petição do autor #{doc.doc_id} {doc.tipo} ({doc.titulo})"))
            if ev.polo == "PASSIVO":
                f.peticoes_reu.append(ref)
                anexos_txt = norm(" ".join(ref.anexos) + " " + doc.titulo)
                if RE_PAGTO_ANEXO.search(anexos_txt) or "pagamento" in tipo:
                    f.pagamentos_reu.append(ref)
                if RE_APOLICE.search(anexos_txt):
                    f.apolices_reu.append(ref)
                if RE_IMPUG.search(tudo):
                    f.impugnacoes_reu.append(ref)
            elif ev.polo == "ATIVO":
                f.peticoes_autor.append(ref)
            # anexos de qualquer petição podem ser documentos do SISBAJUD juntados pelo juízo
    # suspensão em vigor e impugnação pendente, pelos nomes dos movimentos (mais antigo primeiro)
    susp, fim_susp, imp, julg = [], [], [], []
    for ev in eventos:
        for m in ev.movimentos:
            n = _mov_limpo(m)
            if RE_SUSPENSAO.search(n) and not RE_SUSPENSAO_ACORDO.search(n):
                susp.append((ev.data, m))
            if RE_FIM_SUSPENSAO.search(n):
                fim_susp.append(ev.data)
            if RE_INTIMACAO_PAGAR.search(n) and not RE_ATO_ROTINA.search(n[:40]):
                f.sinais_intimacao_pagar.append(ev.data)
            if RE_IMPUG_JULG_MOV.search(n) or RE_IMPUG_JULGADA.search(n):
                julg.append(ev.data)
            elif RE_IMPUG_MOV.search(n) and not RE_IMPUG_POS_BLOQUEIO.search(n):
                imp.append(ev.data)
    ordens_perito = [(ev.data, m) for ev in eventos if ev.polo in ("INTERNO", "SISTEMA") for m in ev.movimentos
                     if RE_HONOR_PERITO.search(_mov_limpo(m)) and re_honor_cliente.search(_mov_limpo(m))
                     and not RE_DEPOSITO.search(_mov_limpo(m)) and not RE_HONOR_PERITO_PRECLUSAO.search(_mov_limpo(m))]
    if ordens_perito:
        d0, m0 = max(ordens_perito, key=lambda x: x[0])
        depositou = any(ev.data >= d0 and (ev.polo == "PASSIVO" or any(RE_DEPOSITO.search(_mov_limpo(m))
                                                                        for m in ev.movimentos))
                        for ev in eventos if ev.data >= d0 and not any(m == m0 for m in ev.movimentos))
        if not depositou:
            f.honorarios_perito_pendentes = (d0, m0)
    if susp and (not fim_susp or max(fim_susp) < max(d for d, _ in susp)):
        f.suspensao = max(susp, key=lambda x: x[0])
    if imp and (not julg or max(julg) < max(imp)):
        f.impugnacao_pendente_mov = max(imp)
    # acordo homologado DEPOIS de uma sentença de mérito = acordo na fase de cumprimento: a
    # sentença homologatória extingue a execução (CPC 487 III b / 924 III)
    for d, m, _ in f.sentencas:
        if RE_TRANSACAO.search(_mov_limpo(m)) and any(d2 < d and not RE_TRANSACAO.search(_mov_limpo(m2))
                                                      for d2, m2, _ in f.sentencas):
            f.extincoes_execucao.append((d, m))
            f.extincoes.append((d, m))
    # cumprimento/liquidação/execução em autos apartados (rótulo do grupo) com movimento relevante
    # depois da sentença e da última extinção da execução: o autor está executando, mesmo que o
    # checklist não tenha visto o cumprimento (ele chega como processo relacionado)
    rot_cs = re.compile(r"^\[(relacionado|incidente) [^\]]*(cumpr|execu|liquida|cumsen|cumprse|extiex)[^\]]*\]")
    rot_esaj = re.compile(r"^\[incidente [^\] ]*\]")  # incidente do e-SAJ sem rótulo: só CS/execução
    rot_recurso = re.compile(r"^\[[^\]]*(agravo|apela|recurso|2o grau|embargos de declara)[^\]]*\]")
    nao_conta = re.compile(r"certidao|publicad|disponibilizad|arquivad|baixa|decorrido|remetido ao dje|"
                           r"documento juntado: (certidao|ato ordinatorio)|redistribu|distribu|autuad|"
                           r"extint|julgo extint|transitad|transito|alvara|levantament|ato ordinatorio|"
                           r"expedicao de documento|confirmada a intimacao|ciencia|migra|conclus")
    ref_cs = max([d for d, _, _ in f.sentencas] + [d for d, _ in f.extincoes_execucao] or [date.min])
    ativo_por, arquivado_por = {}, {}  # por processo apenso: último movimento relevante / arquivamento
    for ev in eventos:
        for m in ev.movimentos:
            n = norm(m)
            if not (rot_cs.match(n) or rot_esaj.match(n)) or rot_recurso.match(n):
                continue
            chave = n[:n.index("]")]
            limpo = _mov_limpo(m)
            if re.search(r"arquivamento definitivo|arquivado definitivamente|baixa definitiva|\{arquivamento|"
                         r"encaminhado para o arquivo|remetidos? ao arquivo", limpo):
                arquivado_por[chave] = max(arquivado_por.get(chave, date.min), ev.data)
            elif ev.data > ref_cs and not nao_conta.search(limpo):
                ativo_por[chave] = max(ativo_por.get(chave, date.min), ev.data)
    vivos = [d for k, d in ativo_por.items() if d > arquivado_por.get(k, date.min)]
    if vivos:
        f.cs_apenso_ativo = max(vivos)
    if f.extincoes:
        # extinção do principal não encerra o caso se o cumprimento (relacionado/incidente)
        # continuou andando depois dela
        ult = max(d for d, _ in f.extincoes)
        relevante = re.compile(r"intim|penhora|bloqueio|sisbajud|calculo|cumprimento de sentenca|"
                               r"execucao|peticao|remanescente")
        rotina = re.compile(r"certidao|publicad|disponibilizad|arquivad|baixa|decorrido|remetido ao dje|"
                            r"documento juntado: (certidao|ato ordinatorio)")
        f.atividade_apos_extincao = any(
            e.data > ult and (
                # o autor pedindo algo depois da extinção reabre o risco; petição da ré
                # (custas, levantamento) e mera ciência/confirmação de intimação não
                (e.polo == "ATIVO" and not all(RE_ATO_ROTINA.search(norm(m)) for m in e.movimentos
                                               if not norm(m).startswith("por ")))
                or any(m.startswith(("[RELACIONADO", "[INCIDENTE")) and relevante.search(norm(m))
                       and not rotina.search(norm(m)) for m in e.movimentos))
            and not any(RE_EXTINCAO.search(norm(m)) for m in e.movimentos)
            for e in eventos)
    return f


# --- sinais no TEXTO dos documentos (aplicados ao texto integral, antes do recorte) ---
SINAIS = {
    "PASSIVO": [
        ("comprovante/depósito", r"(juntada d[eo]s? |anexo |segue |junta )?comprovante[s]? de (pagamento|deposito)|deposito judicial|guia de deposito|efetuou o (pagamento|deposito)|realizou o (pagamento|deposito)"),
        ("valor incontroverso", r"valor incontroverso|incontroverso"),
        ("apólice/seguro garantia", r"apolice|seguro[ -]garantia|garantia do juizo"),
        ("pede efeito suspensivo", r"efeito suspensivo"),
        ("apresenta impugnação/embargos", r"(apresentar|opor|interpor|oferecer)(,? tempestivamente,?)? (a presente )?"
                                          r"(impugnacao ao cumprimento|impugnacao a execucao|embargos a execucao|"
                                          r"excecao de pre-?executividade)"),
    ],
    "ATIVO": [
        ("pede penhora on-line", r"penhora (on-?line|eletronica)|sisbajud|bacenjud|bloqueio (de|dos) (valores|ativos)"),
        ("pede remanescente/saldo", r"(saldo|valor) remanescente|diferenca (devida|apurada)|complementa"),
        ("concorda/levantamento", r"concorda|anuencia|nada (mais )?a (opor|requerer)|quitacao|requer (a )?expedicao de alvara|levantamento d[oa]s? (valor|deposito|quantia)"),
        ("pede cumprimento", r"(apresentar|requer(er)?|promover|iniciar|deflagrar|instaurar)[^.]{0,40}cumprimento (definitivo |provisorio )?de sentenca|nos termos do art(igo)?\.? ?523"),
    ],
    "INTERNO": [
        ("ordem p/ ré pagar", r"intime-se (o|a) (executad[oa]|requerid[oa]|re|banco|devedor)[^.]{0,160}(pag|deposit)|para (que )?(efetue|efetuar|realize|realizar) o pagamento|sob pena de (multa|penhora|bloqueio)"),
        ("determina bloqueio", r"(defiro|determino)[^.]{0,80}(penhora|bloqueio|sisbajud|bacenjud)"),
        ("impugnação julgada", r"(acolho|rejeito|julgo (procedente|improcedente))[^.]{0,60}impugna|impugnacao[^.]{0,40}(acolhida|rejeitada)"),
        ("extinção/alvará", r"julgo extint|extingo|expeca-se (o )?alvara|satisfacao da obrigacao"),
        ("homologa cálculo", r"homologo (os )?calculo"),
    ],
}


def sinais_texto(polo: str, texto: str) -> list[str]:
    """Rótulos + trecho curto para cada padrão encontrado no documento."""
    t = norm(texto)
    achados = []
    guia_de_taxa = bool(re.search(r"dare-sp|dare sp|gnre|sefaz|portaria cat", t))
    for rotulo, rx in SINAIS.get(polo if polo in SINAIS else "INTERNO", []):
        m = re.search(rx, t)
        if m and rotulo == "comprovante/depósito" and guia_de_taxa:
            m = None  # comprovante de taxa/custas estaduais, não de pagamento da condenação
        if m and rotulo == "comprovante/depósito":
            # pagamento de custas não é pagamento da condenação
            m = next((x for x in re.finditer(rx, t)
                      if not re.search(r"custa|taxa|dare|gnre|sefaz|despesas? (processuais|postais)|preparo|porte de remessa|diligencia",
                                       t[max(0, x.start() - 150):x.end() + 150])), None)
        if m:
            achados.append(f"{rotulo}: \"…{t[max(0, m.start() - 70):m.end() + 70]}…\"")
    return achados


def aplicar_sinais(f: Fatos, d: DocRef, texto: str) -> list[str]:
    """Sinais do texto integral de um documento lido -> fatos (mesma regra na análise e no replay)."""
    anexos = norm(" ".join(d.anexos))
    if d.polo == "PASSIVO" and re.search(r"comprovante|pagamento|deposito", anexos) \
            and not re.search(r"idcusta|custa|dare|taxa", anexos):
        f.sinal_pagamento_reu.append((d.data, d.doc_id))
    sinais = sinais_texto(d.polo, texto) if texto and not texto.startswith("[falha") else []
    if d.polo == "PASSIVO" and any(x.startswith("comprovante/depósito") for x in sinais):
        f.sinal_pagamento_reu.append((d.data, d.doc_id))
    if d.polo == "PASSIVO" and any(x.startswith("apresenta impugnação") for x in sinais) \
            and all(i.doc_id != d.doc_id for i in f.impugnacoes_reu):
        f.impugnacoes_reu.append(d)
    if d.polo == "ATIVO" and any(x.startswith("pede cumprimento") for x in sinais):
        f.cs_inicio.append((d.data, f"petição do autor #{d.doc_id} pede cumprimento de sentença"))
    return sinais


# --- pagamento nos autos (sinal para o produto, não muda o grau) ----------------------
# Petição da ré com comprovante/guia de depósito juntado, ou comprovante achado no texto da peça.
# Custas, taxas e preparo não contam (não pagam a condenação).
RE_PAGTO_FORTE = re.compile(r"comprovante|deposito|pagamento|pasta\d+valor|boleto|transferencia|\bted\b|recibo|"
                            r"guia (de )?(deposito|judicial|recolhimento judicial)")
RE_NAO_E_CONDENACAO = re.compile(r"idcusta|custa|dare|gnre|taxa|preparo|porte de remessa|diligencia|emolumento")
# linhas do polo passivo que não são parte (advogado, procuradoria do próprio réu, perito...)
RE_NAO_PARTE = re.compile(r"advogad|procurador|perit|curador|ministerio publico|defensor|testemunh|terceiro interessado")
RE_CONTRA = re.compile(r"contra|em face|desfavor|movid|promovid|ajuizad")


def autoria_do_cliente(texto: str | None) -> bool | None:
    """A peça é do cliente? True: assinada por advogado do escritório (nome/OAB) ou o cliente abre a peça
    ("BANCO X ..., vem"). False: outra parte abre a peça. None: o texto não diz."""
    if not texto or texto.startswith("[falha"):
        return None
    p = perfil.atual()
    t = norm(texto)
    if p.advogados and (re.search(p.regex_advogados, t[:3000]) or re.search(p.regex_advogados, t[-3000:])):
        return True
    v = re.search(r"\bvem\b|\bvem,", t[:6000])
    if not v:
        return None
    antes = t[max(0, v.start() - 500):v.start()]
    nomes = list(re.finditer(p.regex_nomes, antes))
    if nomes and not RE_CONTRA.search(antes[max(0, nomes[-1].start() - 80):nomes[-1].start()]):
        return True
    return False


def _pagamento_do_cliente(d: DocRef | None, textos: dict[str, str] | None, outros_reus: int) -> bool:
    """Pagamento do polo passivo é do NOSSO réu? Sem corréu, sim. Com corréu, só com prova: o movimento
    cita o cliente ou o texto da peça é dele (assinatura do escritório ou abertura)."""
    if outros_reus == 0:
        return True
    if d is None:
        return False
    if d.movs and perfil.atual().eh_cliente(d.movs):
        return True
    return autoria_do_cliente((textos or {}).get(d.doc_id)) is True


CAMPOS_PAGAMENTO = ("houve_pagamento", "comprovacao_pagamento", "data_pagamento", "valor_pago")


def _pagamento_da_ia(checklist: dict | None) -> tuple[date, str, str] | None:
    """Pagamento que a IA leu nos autos com comprovante (petição + comprovante ou só o comprovante)."""
    c = checklist or {}
    if c.get("houve_pagamento") not in ("parcial", "integral") or \
            c.get("comprovacao_pagamento") not in ("peticao_com_comprovante", "so_comprovante_sem_peticao"):
        return None
    try:
        data = date.fromisoformat(str(c.get("data_pagamento") or "")[:10])
    except ValueError:
        return None
    ev = next((e for e in c.get("evidencias") or [] if e.get("campo") in CAMPOS_PAGAMENTO and e.get("doc_id")), {})
    valor = c.get("valor_pago")
    brl = f" de R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if isinstance(valor, (int, float)) else ""
    desc = f"Pagamento {'integral' if c['houve_pagamento'] == 'integral' else 'parcial'}{brl} com comprovante nos autos"
    return data, str(ev.get("doc_id") or "").lstrip("#"), desc


def pagamento_nos_autos(f: Fatos, checklist: dict | None = None, textos: dict[str, str] | None = None) -> dict | None:
    """O comprovante de pagamento mais recente juntado pela ré depois da última sentença (ou em qualquer
    data, se não há sentença): {"data", "doc_id", "descricao", "quantidade"}. Fontes: petição da ré com
    anexo de pagamento, comprovante achado no texto da peça e a leitura da IA (pagamento com comprovante).
    Só sinaliza: o grau segue as regras (pagamento parcial, sem apólice, autor que discorda continuam pesando).
    Com corréus no processo, só conta o comprovante que se prova do cliente (`_pagamento_do_cliente`)."""
    ref = f.data_ultima_sentenca
    por_id = {d.doc_id: d for d in f.peticoes_reu}
    cands: dict[str, tuple[date, str]] = {}
    for d in f.pagamentos_reu:
        tudo = norm(f"{d.tipo} {d.titulo} " + " ".join(d.anexos))
        if (ref and d.data < ref) or RE_NAO_E_CONDENACAO.search(tudo) or not RE_PAGTO_FORTE.search(tudo) \
                or not _pagamento_do_cliente(d, textos, f.outros_reus):
            continue
        anexos = [a for a in d.anexos if RE_PAGTO_FORTE.search(norm(a))][:2]
        desc = f"{d.tipo}" + (f" com {', '.join(anexos)}" if anexos else "")
        cands[d.doc_id] = (d.data, desc)
    for data, doc_id in f.sinal_pagamento_reu:
        if (ref and data < ref) or doc_id in cands or not _pagamento_do_cliente(por_id.get(doc_id), textos, f.outros_reus):
            continue
        cands[doc_id] = (data, "Petição da ré com comprovante de pagamento/depósito (lido no texto)")
    ia = _pagamento_da_ia(checklist)
    if ia and f.outros_reus and not _pagamento_do_cliente(por_id.get(ia[1]), textos, f.outros_reus):
        ia = None  # com corréu, a IA pode ter lido o pagamento de outro réu: só com a peça provada
    if ia and not (ref and ia[0] < ref) and ia[1] not in cands:
        cands[ia[1] or f"ia:{ia[0].isoformat()}"] = (ia[0], ia[2])
    if not cands:
        return None
    doc_id, (data, desc) = max(cands.items(), key=lambda x: (x[1][0], x[0]))
    return {"data": data.isoformat(), "doc_id": "" if doc_id.startswith("ia:") else doc_id,
            "descricao": desc[:240], "quantidade": len(cands)}
