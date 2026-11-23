"""Bounded HTTP adapters for the public evidence sources used by DE."""

import json
import os
import re
import ssl
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree


@dataclass(frozen=True)
class PopulationObservation:
    place: str
    variable: str
    year: str
    percent: float
    method: str
    provenance: str


@dataclass(frozen=True)
class EvidenceDocument:
    source: str
    title: str
    url: str
    text: str
    year: str = ""
    publication_type: str = ""
    observation: PopulationObservation | None = None


class SourceUnavailable(Exception):
    """The provider could not return a usable response."""


def _clean(value: str) -> str:
    value = re.sub(r"</?(?:p|div|li|br|h[1-6])\b[^>]*>", " ", unescape(value), flags=re.I)
    return " ".join(re.sub(r"<[^>]+>", "", value).split())


def _https(value: object, domains: tuple[str, ...] = ()) -> str:
    if not isinstance(value, str) or re.search(r"[\s<>]", value):
        return ""
    try:
        url = urlsplit(value)
        host = url.hostname or ""
    except ValueError:
        return ""
    if url.scheme != "https" or not host or url.username or url.password:
        return ""
    return value if not domains or any(host == d or host.endswith("." + d) for d in domains) else ""


class EvidenceSources:
    def __init__(self, *, ncbi_key: str = "", ncbi_email: str = "", web_key: str = "", population_key: str = ""):
        self.ncbi = {"tool": "personalhealthagent"}
        if ncbi_key:
            self.ncbi["api_key"] = ncbi_key
        if ncbi_email:
            self.ncbi["email"] = ncbi_email
        self.web_key = web_key
        self.population_key = population_key
        context = ssl.create_default_context()
        ca_file = Path("/etc/ssl/cert.pem")
        if ca_file.is_file():
            context.load_verify_locations(cafile=str(ca_file))
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))

    @classmethod
    def from_environment(cls):
        return cls(ncbi_key=os.getenv("NCBI_API_KEY", ""), ncbi_email=os.getenv("NCBI_EMAIL", ""),
                   web_key=os.getenv("TAVILY_API_KEY", ""),
                   population_key=os.getenv("DATA_COMMONS_API_KEY", ""))

    def _read(self, url: str, parameters: dict, *, body: dict | None = None, auth: dict | None = None) -> bytes:
        if parameters:
            url += "?" + urlencode(parameters)
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
        for key, value in (auth or {}).items():
            req.add_unredirected_header(key, value)
        try:
            with self.opener.open(req, timeout=8) as response:
                contents = response.read(768 * 1024 + 1)
        except HTTPError as error:
            raise SourceUnavailable(f"HTTP {error.code}") from error
        except (URLError, OSError, TimeoutError) as error:
            raise SourceUnavailable("connection failed") from error
        if len(contents) > 768 * 1024:
            raise SourceUnavailable("response exceeds limit")
        return contents

    def _json(self, url: str, parameters: dict, **options) -> dict:
        try:
            document = json.loads(self._read(url, parameters, **options))
        except ValueError as error:
            raise SourceUnavailable("invalid JSON response") from error
        if not isinstance(document, dict):
            raise SourceUnavailable("unexpected response shape")
        return document

    def _xml(self, url: str, parameters: dict) -> ElementTree.Element:
        try:
            return ElementTree.fromstring(self._read(url, parameters))
        except ElementTree.ParseError as error:
            raise SourceUnavailable("invalid XML response") from error

    def pubmed(self, term: str) -> tuple[EvidenceDocument, ...]:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        result = self._json(base + "esearch.fcgi", {**self.ncbi, "db": "pubmed", "retmode": "json",
                                                    "term": term, "sort": "relevance", "retmax": 5})
        ids = result.get("esearchresult", {}).get("idlist")
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i.isascii() or not i.isdecimal() for i in ids):
            raise SourceUnavailable("missing PubMed identifiers")
        if not ids:
            return ()
        root = self._xml(base + "efetch.fcgi", {**self.ncbi, "db": "pubmed", "retmode": "xml",
                                              "id": ",".join(ids[:5])})
        documents = []
        for article in root.findall("./PubmedArticle/MedlineCitation"):
            pmid = article.findtext("PMID", "")
            title = article.find("Article/ArticleTitle")
            abstract = _clean(" ".join("".join(item.itertext()) for item in article.findall("Article/Abstract/AbstractText")))
            if pmid in ids and title is not None and abstract:
                year = article.findtext("Article/Journal/JournalIssue/PubDate/Year", "")
                kinds = ", ".join(node.text or "" for node in article.findall("Article/PublicationTypeList/PublicationType"))
                documents.append(EvidenceDocument("PubMed", _clean("".join(title.itertext())),
                                                  f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", abstract[:5000], year, kinds))
        return tuple(documents)

    def medlineplus(self, term: str) -> tuple[EvidenceDocument, ...]:
        parameters = {"db": "healthTopics", "term": term, "retmax": 3, "tool": self.ncbi["tool"]}
        if "email" in self.ncbi:
            parameters["email"] = self.ncbi["email"]
        root = self._xml("https://wsearch.nlm.nih.gov/ws/query", parameters)
        documents = []
        for item in root.findall(".//document")[:3]:
            url = _https(item.get("url"), ("medlineplus.gov",))
            fields = {part.get("name", "").lower(): _clean("".join(part.itertext())) for part in item.findall("content")}
            text = fields.get("fullsummary") or fields.get("snippet") or ""
            if url and fields.get("title") and text:
                documents.append(EvidenceDocument("MedlinePlus.gov", fields["title"], url, text[:4000]))
        return tuple(documents)

    def tavily(self, term: str) -> tuple[EvidenceDocument, ...]:
        if not self.web_key:
            raise SourceUnavailable("TAVILY_API_KEY is not configured")
        domains = ("nih.gov", "cdc.gov", "who.int", "medlineplus.gov")
        result = self._json("https://api.tavily.com/search", {}, auth={"Authorization": f"Bearer {self.web_key}"},
                            body={"query": term, "search_depth": "basic", "max_results": 4,
                                  "include_domains": domains, "include_answer": False, "include_raw_content": False})
        documents = []
        for item in result.get("results", [])[:4]:
            url = _https(item.get("url"), domains)
            title, text = _clean(item.get("title") or ""), _clean(item.get("content") or "")
            if url and title and text:
                documents.append(EvidenceDocument("Tavily 网页搜索", title, url, text[:4000]))
        return tuple(documents)

    def data_commons(self, variable: str, entity: str, place: str) -> tuple[EvidenceDocument, ...]:
        if not self.population_key:
            raise SourceUnavailable("DATA_COMMONS_API_KEY is not configured")
        payload = self._json(
            "https://api.datacommons.org/v2/observation", {}, auth={"X-API-Key": self.population_key},
            body={"date": "LATEST", "variable": {"dcids": [variable]}, "entity": {"dcids": [entity]},
                  "select": ["variable", "entity", "date", "value", "facet"]},
        )
        place_data = payload.get("byVariable", {}).get(variable, {}).get("byEntity", {}).get(entity, {})
        for facet in place_data.get("orderedFacets", []):
            metadata = payload.get("facets", {}).get(facet.get("facetId"), {})
            provenance = _https(metadata.get("provenanceUrl"))
            for item in facet.get("observations", []):
                year, value = item.get("date"), item.get("value")
                if provenance and isinstance(year, str) and re.fullmatch(r"\d{4}", year) and type(value) in (int, float) and 0 <= value <= 100:
                    method = metadata.get("measurementMethod") or ""
                    stat = PopulationObservation(place, variable, year, value, method, provenance)
                    return (EvidenceDocument("Data Commons", variable, f"https://datacommons.org/browser/{variable}",
                                             "", year, observation=stat),)
        return ()
