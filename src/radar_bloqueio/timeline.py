"""Parser da página dos autos (listProcessoCompletoAdvogado.seam): capa + timeline."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from selectolax.lexbor import LexborHTMLParser

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
         # alguns PJe (ex.: TJES) devolvem a data em inglês
         "feb": 2, "apr": 4, "may": 5, "aug": 8, "sep": 9, "oct": 10, "dec": 12}

# Ícone à esquerda de cada item da timeline: quem lançou.
POLOS = {"PA": "ATIVO", "PP": "PASSIVO", "I": "INTERNO", "sistema": "SISTEMA"}


@dataclass
class Documento:
    doc_id: str
    tipo: str            # "Petição", "Sentença", "Documento de Comprovação"...
    titulo: str          # texto entre parênteses (nome do arquivo/descrição)
    link_id: str         # id do <a> A4J que seleciona o documento
    anexo_de: str = ""   # doc_id do documento principal, se for anexo


@dataclass
class Evento:
    seq: int             # 0 = mais recente
    data: date
    hora: str
    polo: str            # ATIVO | PASSIVO | INTERNO | SISTEMA
    movimentos: list[str] = field(default_factory=list)
    documentos: list[Documento] = field(default_factory=list)

    @property
    def quando(self) -> datetime:
        h, m = (self.hora or "00:00").split(":")
        return datetime(self.data.year, self.data.month, self.data.day, int(h), int(m))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data"] = self.data.isoformat()
        return d


@dataclass
class Capa:
    classe: str = ""
    orgao_julgador: str = ""
    jurisdicao: str = ""
    competencia: str = ""
    valor_causa: str = ""
    autuacao: str = ""
    polo_ativo: list[str] = field(default_factory=list)
    polo_passivo: list[str] = field(default_factory=list)
    associados: int = 0

    @property
    def juizado(self) -> bool:
        t = f"{self.classe} {self.jurisdicao} {self.orgao_julgador} {self.competencia}".lower()
        return "juizado" in t or "jesp" in t or "jec" in t


def _data_pt(s: str) -> date | None:
    # "09 Sep 2026", "10 out. 2026", "10 outubro 2026"
    m = re.match(r"(\d{1,2})\s+(\w{3})\w*\.?\s+(\d{4})", s.strip().lower())
    if not m or m.group(2) not in MESES:
        return None
    return date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))


def _txt(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ") if node else "").strip()


_RE_DOC = re.compile(r"^(\d{6,})\s*-\s*(.+?)(?:\s*\(([^()]*)\))?$")


def _doc(span_text: str, link_id: str, anexo_de: str = "") -> Documento | None:
    m = _RE_DOC.match(span_text)
    if not m:
        return None
    return Documento(doc_id=m.group(1), tipo=m.group(2).strip(), titulo=(m.group(3) or "").strip(),
                     link_id=link_id, anexo_de=anexo_de)


def parse_capa(html: str) -> Capa:
    t = LexborHTMLParser(html)
    capa = Capa()
    rotulos = {"Classe judicial": "classe", "Órgão julgador": "orgao_julgador",
               "Jurisdição": "jurisdicao", "Competência": "competencia",
               "Valor da causa": "valor_causa", "Autuação": "autuacao"}
    for dt in t.css("dt"):
        k = _txt(dt)
        if k in rotulos:
            dd = dt.next
            while dd is not None and dd.tag != "dd":
                dd = dd.next
            if dd is not None:
                setattr(capa, rotulos[k], _txt(dd))
    for polo, attr in (("poloAtivo", "polo_ativo"), ("poloPassivo", "polo_passivo")):
        tabela = t.css_first(f"#{polo} table, #{polo}")
        if tabela:
            itens = [_txt(li) for li in tabela.css("td, li")]
            itens = [i for i in dict.fromkeys(itens) if i]
            # células externas concatenam as internas: fica só com as "folhas"
            setattr(capa, attr, [i for i in itens if not any(o != i and o in i for o in itens)])
    m = re.search(r"Associados \((\d+)\)", html)
    capa.associados = int(m.group(1)) if m else 0
    return capa


def parse_timeline(html: str) -> list[Evento]:
    t = LexborHTMLParser(html)
    # Se as páginas seguintes foram carregadas, o fragmento "acumulada" já contém tudo.
    raiz = t.css_first("form#divTimeLine.acumulada") or t.css_first("form#divTimeLine")
    nodes = raiz.css("div.media") if raiz is not None else []
    eventos: list[Evento] = []
    data_atual: date | None = None
    for node in nodes:
        classes = node.attributes.get("class", "")
        if "data" in classes.split():
            d = _data_pt(_txt(node.css_first(".data-interna")))
            if d:
                data_atual = d
            continue
        if "interno" not in classes or data_atual is None:
            continue
        icone = node.css_first(".media-left .icon-auto")
        polo = "SISTEMA"
        if icone is not None:
            for c in icone.attributes.get("class", "").split():
                if c in POLOS:
                    polo = POLOS[c]
        hora_el = node.css_first("small.text-muted")
        ev = Evento(seq=len(eventos), data=data_atual, hora=_txt(hora_el), polo=polo)
        ev.movimentos = [_txt(s) for s in node.css(".texto-movimento") if _txt(s)]
        principal = ""
        anexos = node.css_first(".anexos")
        if anexos is not None:
            for a in anexos.css("a[id^='divTimeLine:']"):
                span = a.css_first("span")
                if span is None:
                    continue
                dentro_arvore = a.parent is not None and a.parent.tag == "li"
                doc = _doc(_txt(span), a.attributes["id"], principal if dentro_arvore else "")
                if doc is None:
                    continue
                if not dentro_arvore:
                    principal = doc.doc_id
                ev.documentos.append(doc)
        eventos.append(ev)
    return eventos


def tipo_canonico(nome: str, desc: str = "", segundo_grau: bool = False) -> str:
    """Tipo de documento no vocabulário do PJe a partir do nome do movimento (sistemas em que o
    arquivo não tem tipo próprio: Projudi, portal do TJSE)."""
    import unicodedata
    n = unicodedata.normalize("NFKD", f"{nome} {desc}").encode("ascii", "ignore").decode().lower()
    if re.match(r"\s*(juntada|peticao|protocolo)", n):
        return "Petição"
    if "acordao" in n or "ementa" in n:
        return "Acórdão"
    if re.search(r"julgamento|julgad[oa]|sentenca|procedencia|improcedencia|extin[cg]|homologa", n):
        return "Acórdão" if segundo_grau else "Sentença"
    if "decisao" in n:
        return "Decisão"
    if "despacho" in n or "ato ordinatorio" in n:
        return "Despacho"
    if "certid" in n:
        return "Certidão"
    return nome[:120]
