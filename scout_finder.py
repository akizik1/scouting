#!/usr/bin/env python3
"""
Scout Finder
------------
Filters in, watch list out.

Give it what you are looking for (position, age, budget, leagues, nationality,
foot, minutes, contract, stat thresholds, style). It runs for free by default
(Google Gemini free tier + DuckDuckGo search, or a local Ollama model); Claude is
an optional paid provider. It will:
  1. find real players who fit, using free public sources
     (Transfermarkt, FotMob, Sofascore, Ogol, league sites, Wikipedia),
  2. read open match reports, scouting blogs, Reddit and fan forums,
  3. write 3 strengths, 3 risks and "what to watch on video", every claim with
     its sources and a confidence note (how many independent sources agree),
  4. say clearly when the data is thin, and give a signal
     (Watch now / Monitor / Skip) with the reason.
Optionally it adds the players you pick to a Notion kanban board.

The AI prepares the list. The scout watches the matches and decides.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Literal, Optional

import time

import requests
from pydantic import BaseModel, ValidationError

# Provider: "gemini" (free tier, default), "ollama" (local, free) or "claude" (paid)
PROVIDER = os.getenv("SCOUT_PROVIDER", "gemini")
MODEL = os.getenv("SCOUT_MODEL", "claude-opus-5-5")          # Claude model
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")                  # empty = pick automatically
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
NOTION_VERSION = "2022-06-28"

POSITIONS = Literal["GK", "CB", "RB", "LB", "RWB", "LWB", "DM", "CM", "AM",
                    "RW", "LW", "SS", "ST"]
SIGNALS = Literal["🟢 Watch now", "🟡 Monitor", "🔴 Skip"]
FOUND_IN = Literal["Transfermarkt", "FotMob", "Sofascore", "Ogol", "Wikipedia",
                   "League site", "Other"]


# --------------------------------------------------------------------------
# Data models (also the structured-output schemas for Claude)
# --------------------------------------------------------------------------
class SearchFilters(BaseModel):
    title: str = "Player search"
    description: str = ""                      # the scout's request in plain words
    positions: list[str] = []
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    foot: Optional[str] = None                 # Left / Right / Any
    max_value_eur: Optional[float] = None
    min_value_eur: Optional[float] = None
    leagues: list[str] = []
    nationalities: list[str] = []
    languages: list[str] = []                  # languages the player speaks
    include_lower_leagues: bool = False
    only_lower_leagues: bool = False
    min_minutes: Optional[int] = None          # this season
    min_height_cm: Optional[int] = None
    contract_ends_by: Optional[str] = None     # YYYY or YYYY-MM-DD
    criteria: str = ""                         # measurable thresholds
    must_haves: str = ""                       # style, checked on video
    custom: list[str] = []                     # the scout's own filters, free text

    def describe(self) -> str:
        p = [f"Search: {self.title}"]
        if self.description.strip():
            p.append("The scout's request, in their own words (treat every requirement in it as "
                     f"a filter): {self.description.strip()}")
        if self.positions:
            p.append("Position: " + " / ".join(self.positions))
        if self.age_min or self.age_max:
            p.append(f"Age: {self.age_min or 'any'} to {self.age_max or 'any'}")
        if self.foot and self.foot != "Any":
            p.append(f"Preferred foot: {self.foot}")
        if self.max_value_eur or self.min_value_eur:
            lo = f"€{self.min_value_eur:,.0f}" if self.min_value_eur else "€0"
            hi = f"€{self.max_value_eur:,.0f}" if self.max_value_eur else "no limit"
            p.append(f"Market value: {lo} to {hi}")
        if self.leagues:
            p.append("Leagues / countries to search: " + ", ".join(self.leagues))
        if self.only_lower_leagues:
            p.append("ONLY lower divisions: second tier and below. Skip top-flight players")
        elif self.include_lower_leagues:
            p.append("Also search lower / cheaper divisions")
        if self.nationalities:
            p.append("Nationality: " + ", ".join(self.nationalities))
        if self.languages:
            p.append("Speaks: " + ", ".join(self.languages))
        if self.min_minutes:
            p.append(f"Minimum minutes this season: {self.min_minutes}")
        if self.min_height_cm:
            p.append(f"Minimum height: {self.min_height_cm} cm")
        if self.contract_ends_by:
            p.append(f"Contract ends by: {self.contract_ends_by}")
        if self.criteria:
            p.append(f"Measurable criteria: {self.criteria}")
        if self.must_haves:
            p.append(f"Must-haves (check on video): {self.must_haves}")
        for c in self.custom:
            p.append(f"Extra filter from the scout: {c}")
        return "\n".join(p)


class Candidate(BaseModel):
    name: str
    club: str
    league: str
    why_fits: str
    profile_url: Optional[str] = None


class CandidateList(BaseModel):
    candidates: list[Candidate]
    search_notes: str


class Claim(BaseModel):
    text: str
    sources: list[str]          # URLs
    source_count: int           # independent sources that say this
    confidence: Literal["High", "Medium", "Low"]


class Fixture(BaseModel):
    match: str
    kickoff: Optional[str] = None
    competition: Optional[str] = None
    where_to_watch: Optional[str] = None
    source_url: Optional[str] = None


class CareerStep(BaseModel):
    club: str
    from_year: Optional[str] = None
    to_year: Optional[str] = None      # null = current club
    apps: Optional[int] = None
    goals: Optional[int] = None
    loan: bool = False


class ValuePoint(BaseModel):
    date: str                          # YYYY-MM or YYYY-MM-DD
    value_eur: float


class PlayerBrief(BaseModel):
    name: str
    club: str
    league: str
    position: list[POSITIONS]
    nationality: Optional[str] = None
    foot: Optional[Literal["Left", "Right", "Both"]] = None
    date_of_birth: Optional[str] = None
    height_cm: Optional[int] = None
    contract_until: Optional[str] = None
    market_value_eur: Optional[float] = None
    value_source: Optional[str] = None
    minutes_this_season: Optional[int] = None
    found_in: list[FOUND_IN]
    stats_line: str
    stats_source: Optional[str] = None
    criteria_check: list[str]
    strengths: list[Claim]
    risks: list[Claim]
    watch_on_video: list[str]
    data_quality: Literal["Good", "Thin", "Very thin"]
    data_gaps: str
    signal: SIGNALS
    signal_reason: str
    next_fixture: Optional[Fixture] = None
    all_sources: list[str]
    languages: list[str] = []
    profile_links: list[str] = []      # player pages: Transfermarkt, FotMob, Sofascore, club
    agent: Optional[str] = None        # agent / agency, as listed publicly
    agent_url: Optional[str] = None
    club_website: Optional[str] = None
    club_email: Optional[str] = None   # official club contact only
    club_phone: Optional[str] = None
    contact_source: Optional[str] = None
    career: list[CareerStep] = []      # clubs and years, youth to now
    value_history: list[ValuePoint] = []
    value_history_source: Optional[str] = None


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------
SYSTEM = """You are a football scouting research assistant. You prepare a scout's WATCH LIST.
You never make the final decision: the scout watches the matches and decides.

Rules:
- Real, verifiable players only. Never invent players, numbers, clubs or quotes.
- Use free public sources: Transfermarkt, FotMob, Sofascore, Ogol, Flashscore, league and
  club sites, Wikipedia, open match reports, scouting blogs and newsletters, Reddit and fan
  forums, in any language.
- Every claim needs the URL of the page where you found it.
- Count independent sources per claim. Fan forums and Reddit are noisy and biased: a claim
  that only comes from fans is at most "Low" confidence.
- Lower leagues often have little data. Say exactly what is missing. Thin data is a fact
  to report, not something to hide or fill with guesses.
- Give per-90 numbers with sample size (minutes, matches)."""


def make_client():
    """Claude client for PROVIDER=claude, otherwise a free LLM client."""
    if PROVIDER == "claude":
        import anthropic  # optional, only for the paid provider
        return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    if PROVIDER == "ollama":
        return Ollama(OLLAMA_URL, OLLAMA_MODEL)
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("Add your free Gemini API key in Settings (aistudio.google.com/apikey).")
    return Gemini(key, GEMINI_MODEL)


def is_free(client) -> bool:
    return isinstance(client, FreeLLM)


def research(client, prompt: str, max_uses: int = 12) -> str:
    """One web-research turn: Claude searches and reads pages, returns notes with URLs."""
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_uses},
    ]
    messages: list = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(6):  # continue long server-tool turns (pause_turn)
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=SYSTEM,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    if resp is None or resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this research request.")

    texts, cited = [], []
    for block in resp.content:
        if block.type == "text":
            texts.append(block.text)
            for c in (getattr(block, "citations", None) or []):
                url = getattr(c, "url", None)
                if url and url not in cited:
                    cited.append(url)
    notes = "\n".join(texts).strip()
    if cited:
        notes += "\n\nCited pages:\n" + "\n".join(f"- {u}" for u in cited)
    return notes


def structure(client, notes: str, schema: type[BaseModel], task: str):
    """Turn research notes into validated JSON. No new facts allowed."""
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low"},
        output_format=schema,
        messages=[{
            "role": "user",
            "content": (
                f"{task}\n\nUse ONLY the research notes below. Do not add facts, numbers or "
                f"URLs that are not in the notes. Unknown -> null / 'NO DATA'.\n\n"
                f"<notes>\n{notes}\n</notes>"
            ),
        }],
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("Could not structure the research notes.")
    return resp.parsed_output


# --------------------------------------------------------------------------
# Free providers: DuckDuckGo search + page reading + Gemini free tier / Ollama
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def free_search(query: str, n: int = 6) -> list[dict]:
    """DuckDuckGo search, no key needed. Returns [{title, url, snippet}]."""
    try:
        from ddgs import DDGS
    except ImportError:  # older package name
        from duckduckgo_search import DDGS
    for attempt in range(3):
        try:
            rows = DDGS().text(query, max_results=n) or []
            return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                     "snippet": r.get("body", "")} for r in rows if (r.get("href") or r.get("url"))]
        except Exception as e:  # rate limit or network: wait and retry
            log(f"Search retry ({e.__class__.__name__}) for: {query}")
            time.sleep(3 * (attempt + 1))
    return []


def fetch_text(url: str, limit: int = 6000) -> str:
    """Download a page and keep its readable text."""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en,*;q=0.5"},
                         timeout=20)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
            t.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return text[:limit]
    except Exception:
        return ""


def gather(queries: list[str], budget: int, per_query: int = 5, per_page: int = 6000,
           max_pages: int = 10) -> tuple[str, list[str]]:
    """Search, read pages, return (numbered notes, list of URLs actually read)."""
    seen, notes, urls, used = set(), [], [], 0
    hits = []
    for q in queries:
        for h in free_search(q, per_query):
            if h["url"] not in seen:
                seen.add(h["url"])
                hits.append(h)
    for h in hits:
        if len(urls) >= max_pages or used >= budget:
            break
        body = fetch_text(h["url"], min(per_page, budget - used)) or h["snippet"]
        if not body:
            continue
        urls.append(h["url"])
        block = f"[S{len(urls)}] {h['title']}\nURL: {h['url']}\n{body}"
        notes.append(block)
        used += len(block)
    # snippets of pages not read still help (cheap)
    for h in hits:
        if h["url"] not in urls and h["snippet"] and used < budget:
            urls.append(h["url"])
            block = f"[S{len(urls)}] {h['title']} (search snippet only)\nURL: {h['url']}\n{h['snippet']}"
            notes.append(block)
            used += len(block)
    return "\n\n".join(notes), urls


class FreeLLM:
    """Common JSON helper: ask for JSON, validate with Pydantic, retry once."""
    budget = 60000  # characters of source text per call

    def complete(self, prompt: str, schema: dict) -> str:
        raise NotImplementedError

    def ask(self, prompt: str, model: type[BaseModel]):
        schema = model.model_json_schema()
        full = (f"{SYSTEM}\n\n{prompt}\n\nAnswer with ONE JSON object that matches this JSON "
                f"schema. No markdown, no comments.\n{json.dumps(schema)}")
        err = ""
        for _ in range(2):
            raw = self.complete(full + err, schema)
            raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
            try:
                return model.model_validate_json(raw)
            except ValidationError as e:
                err = (f"\n\nYour last answer was not valid: {str(e)[:1500]}\n"
                       "Fix it and answer with valid JSON only.")
        raise RuntimeError("The AI did not return a valid answer. Try again.")


class Gemini(FreeLLM):
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, key: str, model: str = ""):
        self.s = requests.Session()
        self.s.headers.update({"x-goog-api-key": key, "Content-Type": "application/json"})
        self.model = model or self.pick_model()

    def pick_model(self) -> str:
        """Pick a free-tier friendly model: Flash-Lite first, then Flash (stable before preview)."""
        try:
            r = self.s.get(f"{self.BASE}/models", params={"pageSize": 200}, timeout=30)
            r.raise_for_status()
            names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
        except Exception:
            return "gemini-2.5-flash-lite"
        bad = ("image", "tts", "audio", "live", "embedding", "thinking", "exp", "learnlm")
        names = [n for n in names if n.startswith("gemini-") and not any(b in n for b in bad)]

        def score(n):
            ver = re.findall(r"gemini-(\d+(?:\.\d+)?)", n)
            v = float(ver[0]) if ver else 0
            tier = 0 if "flash-lite" in n else 1 if "flash" in n else 3
            return (tier, "preview" in n, n.endswith("latest"), -v)
        names.sort(key=score)
        return names[0] if names else "gemini-2.5-flash-lite"

    def complete(self, prompt: str, schema: dict) -> str:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
        for attempt in range(4):
            r = self.s.post(f"{self.BASE}/models/{self.model}:generateContent", json=body, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:  # free-tier rate limit: wait
                m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                wait = min(int(m.group(1)) + 2 if m else 20 * (attempt + 1), 90)
                log(f"Gemini free tier is busy, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Gemini: {r.status_code} {r.text[:300]}")
            cand = (r.json().get("candidates") or [{}])[0]
            parts = cand.get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        raise RuntimeError("Gemini free-tier limit reached. Wait a minute (or until tomorrow) "
                           "and try again.")


class Ollama(FreeLLM):
    budget = 20000  # local models have smaller context

    def __init__(self, url: str, model: str):
        self.url, self.model = url.rstrip("/"), model

    def complete(self, prompt: str, schema: dict) -> str:
        try:
            r = requests.post(f"{self.url}/api/chat", timeout=900, json={
                "model": self.model, "stream": False, "format": schema,
                "options": {"temperature": 0.2, "num_ctx": 16384},
                "messages": [{"role": "user", "content": prompt}]})
        except requests.ConnectionError:
            raise RuntimeError("Ollama is not running. Install it from ollama.com and run "
                               f"'ollama pull {self.model}'.")
        if r.status_code >= 400:
            raise RuntimeError(f"Ollama: {r.status_code} {r.text[:300]}")
        return r.json().get("message", {}).get("content", "")


class Queries(BaseModel):
    queries: list[str]


def clean_sources(b: PlayerBrief, read: list[str], notes: str = "") -> PlayerBrief:
    """Free models may invent links: keep only URLs we really read, recount sources."""
    ok = set(read)

    def seen(v):  # a URL / email / phone is kept only if it appears in what we read
        return v if v and (v in ok or v in notes) else None
    b.profile_links = [u for u in b.profile_links if seen(u)]
    if not seen(b.value_history_source) or len(b.value_history) < 2:
        b.value_history, b.value_history_source = [], None
    for attr in ("agent_url", "club_website", "club_email", "club_phone", "contact_source"):
        setattr(b, attr, seen(getattr(b, attr)))
    for c in b.strengths + b.risks:
        c.sources = [u for u in c.sources if u in ok]
        c.source_count = min(c.source_count, len(c.sources))
        if c.source_count <= 1:
            c.confidence = "Low"
        elif c.source_count == 2 and c.confidence == "High":
            c.confidence = "Medium"
    b.all_sources = [u for u in dict.fromkeys(b.all_sources + read) if u in ok]
    for attr in ("stats_source", "value_source"):
        if getattr(b, attr) and getattr(b, attr) not in ok:
            setattr(b, attr, None)
    if b.next_fixture and b.next_fixture.source_url not in ok:
        b.next_fixture.source_url = None
    return b


def free_find_candidates(llm: FreeLLM, f: SearchFilters, n: int) -> CandidateList:
    today = dt.date.today().isoformat()
    q = llm.ask(f"""Today is {today}. Write 5 short web search queries (DuckDuckGo) that find
REAL football players who pass these filters. Mix: Transfermarkt lists, league stats pages,
"best young <position> in <league>" articles, scouting blogs; use the local language when
it helps.

{f.describe()}""", Queries).queries[:6]
    notes, _ = gather(q, llm.budget, max_pages=8)
    if not notes:
        raise RuntimeError("Web search returned nothing. DuckDuckGo may be rate-limiting: "
                           "wait a minute and try again.")
    return llm.ask(f"""Today is {today}. From the sources below, list up to {n} REAL players
who pass ALL hard filters. Use ONLY what the sources say. Drop anyone who clearly fails a
filter. profile_url must be one of the source URLs. In search_notes say what you could not
verify.

{f.describe()}

<sources>
{notes}
</sources>""", CandidateList)


def free_brief_player(llm: FreeLLM, f: SearchFilters, name: str, club: str = "") -> PlayerBrief:
    today = dt.date.today().isoformat()
    who = f"{name} {club}".strip()
    year = dt.date.today().year
    notes, read = gather([
        f"{who} transfermarkt",
        f"{who} stats {year}",
        f"{who} scouting report",
        f"{name} reddit OR forum",
        f"{who} next match",
        f"{name} player agent agency",
        f"{club or name} football club official website contact",
        f"{name} career clubs market value history",
    ], llm.budget, per_query=4, max_pages=12)
    if not notes:
        raise RuntimeError(f"No public pages found for {name}.")
    b = llm.ask(f"""Today is {today}. Build a scouting brief for {who} against these filters,
using ONLY the numbered sources below. Do not add facts, numbers or URLs that are not in
the sources. Unknown -> null / "NO DATA".

{f.describe()}

1. Profile: position(s), nationality, foot, date of birth, height, club, league, contract end,
   market value (value_source = its URL), minutes this season.
2. stats_line: stats this season per 90 with minutes/matches. criteria_check: each filter
   and criterion -> "PASS / FAIL / NO DATA: ...".
3. Max 3 STRENGTHS and 3 RISKS from what people say (reports, blogs, forums). For each: the
   claim, the source URLs that support it, how many independent sources agree, confidence
   (High = 3+ independent sources incl. one non-fan; Medium = 2; Low = 1 or fans only).
4. watch_on_video: 3-5 concrete things to check in his next match.
5. next_fixture if a source shows it.
6. data_quality Good / Thin / Very thin, and data_gaps: exactly what is missing.
7. signal: "🟢 Watch now" (passes most filters, sample ≥ ~450 min), "🟡 Monitor" (mixed,
   small sample or key data missing), "🔴 Skip" (clearly fails a core filter). 2-3 sentence
   signal_reason.
8. all_sources: the URLs you used.
9. profile_links: the source URLs that are his player pages (Transfermarkt, FotMob, Sofascore,
   club site). languages if a source states them. agent / agent_url as a source lists them
   (e.g. Transfermarkt "Player agent"). club_website, club_email, club_phone ONLY if they
   appear in the sources (official club contact); contact_source = that URL. Never guess.
10. career: each club with from_year / to_year (null if current), apps, goals, loan, oldest first,
    only as the sources list it. value_history: dated market values (oldest first) ONLY if a
    source lists them, with value_history_source = that URL. Otherwise leave both empty.

<sources>
{notes}
</sources>""", PlayerBrief)
    return clean_sources(b, read, notes)


def find_candidates(client, f: SearchFilters, n: int) -> CandidateList:
    if is_free(client):
        return free_find_candidates(client, f, n)
    today = dt.date.today().isoformat()
    notes = research(client, f"""Today is {today}.
Find up to {n} REAL players who pass ALL of these filters. Prefer players who are not
already famous or expensive: the value is in finding them early.

{f.describe()}

Use the free databases (Transfermarkt, FotMob, Sofascore, Ogol, league sites) to check
position, age, foot, market value, nationality, minutes and contract for the CURRENT season.
Drop anyone who fails a hard filter. For each player write: name, club, league, one line
on why he fits, and his profile URL. Also note how you searched and what you could not
verify.""", max_uses=15)
    return structure(client, notes, CandidateList,
                     f"Extract up to {n} candidates that really pass the filters.")


def brief_player(client, f: SearchFilters, name: str, club: str = "") -> PlayerBrief:
    if is_free(client):
        return free_brief_player(client, f, name, club)
    today = dt.date.today().isoformat()
    who = f"{name} ({club})" if club else name
    notes = research(client, f"""Today is {today}.
Build a scouting brief for {who} against these filters:

{f.describe()}

1. Profile: position(s), nationality, foot, date of birth, height, club, league, contract
   end, market value (with the source page), minutes this season.
2. Stats this season and last season from public databases, per 90 with minutes/matches.
   Check every filter and measurable criterion: value vs threshold -> PASS / FAIL / NO DATA.
3. Read what people say about him: open match reports, scouting blogs, Reddit, fan
   forums (any language). Synthesize the 3 strongest STRENGTHS and 3 most important RISKS.
   For each: the claim, the URLs that support it, how many independent sources agree,
   confidence (High = 3+ independent sources incl. at least one non-fan source;
   Medium = 2; Low = 1 or fans only).
4. "What to watch on video": 3-5 concrete things to check in his next match, linked to
   the risks and the must-haves.
5. Next official fixture (date, opponent, competition, where it can be watched), if found.
5b. Links and contact: his profile pages (Transfermarkt, FotMob, Sofascore, club page), languages
   he speaks if stated, his agent / agency as publicly listed (e.g. Transfermarkt "Player agent")
   with its page, and the club's official website and public contact email / phone.
   Only official public contact details; never guess an email.
5c. Career: every club (youth, senior, loans) with years, apps and goals if listed.
   Market value history: dated values (e.g. Transfermarkt market value history) with the page.
6. Data quality: Good / Thin / Very thin, and exactly what is missing.
7. Signal: 🟢 Watch now (passes most filters, reasonable sample ≥ ~450 min),
   🟡 Monitor (mixed, small sample or key data missing), 🔴 Skip (clearly fails a core
   filter: position / age / budget / key output). 2-3 sentence reason.
List all source URLs at the end.""")
    return structure(client, notes, PlayerBrief,
                     "Fill the scouting brief. Keep max 3 strengths and 3 risks.")


def run_search(client, f: SearchFilters, top: int = 5, players: Optional[list[str]] = None,
               on_step=None) -> list[PlayerBrief]:
    """Full pipeline. on_step(text) is called before each stage."""
    step = on_step or (lambda t: log(t))
    if players:
        targets = [(p, "") for p in players]
    else:
        step("Searching the web (Transfermarkt, FotMob, Sofascore, league sites) for players who pass the filters")
        cl = find_candidates(client, f, min(max(top + 5, 8), 20))
        targets = [(c.name, c.club) for c in cl.candidates[:top]]
        if not targets:
            raise RuntimeError("No players passed the filters. Try wider filters.")
    briefs: list[PlayerBrief] = []
    for i, (name, club) in enumerate(targets, 1):
        step(f"Reading match reports, blogs and forums · {name} ({i}/{len(targets)})")
        try:
            briefs.append(brief_player(client, f, name, club))
        except Exception as e:  # keep going with the others
            log(f"Skipped {name}: {e}")
    briefs.sort(key=lambda b: SIGNAL_ORDER.get(b.signal, 9))
    return briefs


# --------------------------------------------------------------------------
# Notion watchlist (optional)
# --------------------------------------------------------------------------
def page_id_from(url_or_id: str) -> str:
    m = re.findall(r"[0-9a-fA-F]{32}", url_or_id.replace("-", ""))
    if not m:
        raise ValueError(f"No Notion id in: {url_or_id}")
    return m[-1]


def rt(text: str, url: Optional[str] = None) -> list[dict]:
    text = text or ""
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)][:50] or [""]
    out = []
    for c in chunks:
        item = {"type": "text", "text": {"content": c}}
        if url:
            item["text"]["link"] = {"url": url}
        out.append(item)
    return out


def valid_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        if len(s) <= 10:
            return dt.date.fromisoformat(s[:10]).isoformat()
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def valid_url(s: Optional[str]) -> Optional[str]:
    return s if s and re.match(r"^https?://", s) else None


class Notion:
    """Adds players to a Notion database called "Watchlist" (a kanban board by Status).
    Set NOTION_WATCHLIST_DB to use another database. Only properties that exist in your
    database are written, so you can add or remove columns freely."""

    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._db: Optional[str] = None
        self._schema: Optional[dict] = None

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        r = self.s.request(method, f"https://api.notion.com/v1/{path}", json=body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Notion {method} {path}: {r.status_code} {r.text[:400]}")
        return r.json()

    @property
    def db(self) -> str:
        if self._db:
            return self._db
        env = os.getenv("NOTION_WATCHLIST_DB")
        if env:
            self._db = page_id_from(env)
            return self._db
        res = self._call("POST", "search", {
            "query": "Watchlist", "filter": {"property": "object", "value": "database"}})
        for d in res.get("results", []):
            if "".join(t.get("plain_text", "") for t in d.get("title", [])).strip().lower() == "watchlist":
                self._db = d["id"]
                return self._db
        raise RuntimeError('No "Watchlist" database found. In Notion, open the Watchlist page → '
                           "••• → Connections → add your integration.")

    @property
    def schema(self) -> dict:
        if self._schema is None:
            self._schema = self._call("GET", f"databases/{self.db}")["properties"]
        return self._schema

    def fit(self, props: dict) -> dict:
        return {k: v for k, v in props.items()
                if k in self.schema and next(iter(v)) == self.schema[k]["type"]}

    def title_prop(self) -> str:
        return next(k for k, v in self.schema.items() if v["type"] == "title")

    def find(self, name: str) -> Optional[dict]:
        res = self._call("POST", f"databases/{self.db}/query", {
            "filter": {"property": self.title_prop(), "title": {"equals": name}}, "page_size": 1})
        return res["results"][0] if res.get("results") else None

    def add(self, b: "PlayerBrief") -> str:
        """Add (or refresh) one player card. Returns the Notion page URL."""
        props = self.fit(player_props(b))
        existing = self.find(b.name)
        if existing:
            props.pop("Status", None)  # never move a card you already moved
            self._call("PATCH", f"pages/{existing['id']}", {"properties": props})
            blocks = brief_blocks(b)
            for i in range(0, len(blocks), 100):
                self._call("PATCH", f"blocks/{existing['id']}/children", {"children": blocks[i:i + 100]})
            return existing["url"]
        props[self.title_prop()] = {"title": rt(b.name)}
        page = self._call("POST", "pages", {"parent": {"database_id": self.db},
                                            "properties": props, "children": brief_blocks(b)[:100]})
        return page["url"]


def brief_blocks(b: PlayerBrief) -> list[dict]:
    def h(text):
        return {"type": "heading_3", "heading_3": {"rich_text": rt(text)}}

    def claim_item(c: Claim):
        rich = rt(f"{c.text}  ")
        rich += rt(f"[{c.confidence} · {c.source_count} source{'s' if c.source_count != 1 else ''}]")
        rich[-1]["annotations"] = {"color": "gray", "italic": True}
        for i, u in enumerate([u for u in c.sources if valid_url(u)][:4], 1):
            rich += rt(" ") + rt(f"↗{i}", u)
        return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}}

    today = dt.date.today().isoformat()
    blocks = [
        {"type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🤖"}, "color": "gray_background",
            "rich_text": rt(f"AI brief · {today} · {b.signal} · Data: {b.data_quality}. "
                            f"{b.signal_reason}")}},
        h("✅ Strengths"), *[claim_item(c) for c in b.strengths[:3]],
        h("⚠️ Risks"), *[claim_item(c) for c in b.risks[:3]],
        h("🎥 What to watch on video"),
        *[{"type": "to_do", "to_do": {"rich_text": rt(w), "checked": False}}
          for w in b.watch_on_video[:6]],
        h("📏 Filter check"),
        *[{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt(c)}}
          for c in b.criteria_check],
        h("🕳️ Data gaps"),
        {"type": "paragraph", "paragraph": {"rich_text": rt(b.data_gaps or "—")}},
    ]
    if b.career:
        blocks.append(h("🏟️ Career"))
        blocks += [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt(career_text(c))}}
                   for c in b.career[:20]]
    contact = contact_lines(b)
    if contact:
        blocks.append(h("📇 Links & contact"))
        blocks += [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt(t, u)}}
                   for t, u in contact]
    srcs = [u for u in b.all_sources if valid_url(u)][:40]
    if srcs:
        blocks.append({"type": "toggle", "toggle": {
            "rich_text": rt(f"Sources ({len(srcs)})"),
            "children": [{"type": "bulleted_list_item",
                          "bulleted_list_item": {"rich_text": rt(u, u)}} for u in srcs]}})
    return blocks


def career_text(c: CareerStep) -> str:
    yrs = f"{c.from_year or '?'}–{c.to_year or 'now'}"
    extra = ", ".join(x for x in [f"{c.apps} apps" if c.apps is not None else "",
                                  f"{c.goals} goals" if c.goals is not None else "",
                                  "loan" if c.loan else ""] if x)
    return f"{yrs} · {c.club}" + (f" ({extra})" if extra else "")


def contact_lines(b: PlayerBrief) -> list[tuple[str, Optional[str]]]:
    """(text, link) rows for profile pages, agent and club contact."""
    out = [(f"Profile: {u}", u) for u in b.profile_links if valid_url(u)][:4]
    if b.agent:
        out.append((f"Agent: {b.agent}", valid_url(b.agent_url)))
    if b.club_website:
        out.append((f"Club website: {b.club_website}", valid_url(b.club_website)))
    if b.club_email:
        out.append((f"Club email: {b.club_email}", None))
    if b.club_phone:
        out.append((f"Club phone: {b.club_phone}", None))
    return out


def next_step_text(b: PlayerBrief) -> str:
    f = b.next_fixture
    if b.signal == "🔴 Skip":
        return "AI suggests skip. Quick check only if you disagree."
    if f and f.match:
        when = f" · {f.kickoff[:10]}" if f.kickoff else ""
        where = f" · {f.where_to_watch}" if f.where_to_watch else ""
        return f"{f.match}{when}{where}"
    return "No fixture found"


def player_props(b: PlayerBrief) -> dict:
    p = {
        "Status": {"status": {"name": "To watch"}},
        "Signal": {"select": {"name": b.signal}},
        "Position": {"multi_select": [{"name": x} for x in b.position]},
        "Club": {"rich_text": rt(b.club)},
        "League": {"rich_text": rt(b.league)},
        "Nationality": {"rich_text": rt(b.nationality or "")},
        "Data": {"select": {"name": b.data_quality}},
        "Next match": {"rich_text": rt(next_step_text(b))},
        "Stats": {"rich_text": rt(b.stats_line)},
    }
    if b.foot:
        p["Foot"] = {"select": {"name": b.foot}}
    if valid_date(b.date_of_birth):
        p["Date of birth"] = {"date": {"start": valid_date(b.date_of_birth)}}
    if b.market_value_eur is not None:
        p["Market value (€)"] = {"number": b.market_value_eur}
    if valid_url(b.stats_source) or valid_url(b.value_source):
        p["Profile"] = {"url": valid_url(b.stats_source) or valid_url(b.value_source)}
    return p


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
SIGNAL_ORDER = {"🟢 Watch now": 0, "🟡 Monitor": 1, "🔴 Skip": 2}
LOG_HOOK = None  # the desktop app sets this


def log(msg: str) -> None:
    if LOG_HOOK:
        LOG_HOOK(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


def to_markdown(f: SearchFilters, briefs: list[PlayerBrief]) -> str:
    out = [f"# Watch list: {f.title}", "", "```", f.describe(), "```", ""]
    for b in briefs:
        out += [f"## {b.signal} {b.name} ({b.club}, {b.league})",
                f"*{b.signal_reason}* Data quality: **{b.data_quality}**", "",
                f"**Stats:** {b.stats_line}", "", "**Strengths**"]
        out += [f"- {c.text} _[{c.confidence}, {c.source_count} src]_ "
                + " ".join(f"[↗]({u})" for u in c.sources[:3]) for c in b.strengths]
        out += ["", "**Risks**"]
        out += [f"- {c.text} _[{c.confidence}, {c.source_count} src]_ "
                + " ".join(f"[↗]({u})" for u in c.sources[:3]) for c in b.risks]
        out += ["", "**What to watch on video**"] + [f"- [ ] {w}" for w in b.watch_on_video]
        out += ["", "**Career**"] + [f"- {career_text(c)}" for c in b.career]
        out += ["", "**Links & contact**"] + [f"- {t}" for t, _ in contact_lines(b)]
        out += ["", f"**Next step:** {next_step_text(b)}", f"**Data gaps:** {b.data_gaps}", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _csv(s: Optional[str]) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Scout Finder: filters in, watch list out")
    g = ap.add_argument_group("filters")
    g.add_argument("--title")
    g.add_argument("--position", help="comma separated: LW,RW")
    g.add_argument("--age-min", type=int)
    g.add_argument("--age-max", type=int)
    g.add_argument("--foot", choices=["Left", "Right", "Any"])
    g.add_argument("--max-value", type=float, help="max market value in EUR")
    g.add_argument("--min-value", type=float)
    g.add_argument("--leagues", help='comma separated: "Brazil Série B, Ghana"')
    g.add_argument("--lower", action="store_true", help="also search lower divisions")
    g.add_argument("--nationality", help="comma separated")
    g.add_argument("--min-minutes", type=int)
    g.add_argument("--min-height", type=int, help="cm")
    g.add_argument("--contract-by", help="YYYY or YYYY-MM-DD")
    g.add_argument("--criteria", help='e.g. "G+A/90 >= 0.4; dribble success >= 50%%"')
    g.add_argument("--must-haves", help='e.g. "pace in transition, presses"')
    ap.add_argument("--player", help="skip discovery: brief these players (comma separated)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--provider", choices=["gemini", "ollama", "claude"],
                    help="AI provider (default gemini free tier; env SCOUT_PROVIDER)")
    ap.add_argument("--notion", action="store_true", help="add the players to your Notion Watchlist")
    ap.add_argument("--save", default="watchlist.json")
    ap.add_argument("--from-json", help="load a saved result instead of searching")
    a = ap.parse_args()
    global PROVIDER
    PROVIDER = a.provider or PROVIDER

    notion = None
    if a.notion:
        notion = Notion(os.getenv("NOTION_TOKEN") or sys.exit("NOTION_TOKEN is not set."))

    if a.from_json:
        data = json.load(open(a.from_json, encoding="utf-8"))
        f = SearchFilters(**data["filters"])
        briefs = [PlayerBrief(**b) for b in data["briefs"]]
    else:
        f = SearchFilters(
            title=a.title or "Player search", positions=_csv(a.position),
            age_min=a.age_min, age_max=a.age_max, foot=a.foot,
            max_value_eur=a.max_value, min_value_eur=a.min_value,
            leagues=_csv(a.leagues), include_lower_leagues=a.lower,
            nationalities=_csv(a.nationality), min_minutes=a.min_minutes,
            min_height_cm=a.min_height, contract_ends_by=a.contract_by,
            criteria=a.criteria or "", must_haves=a.must_haves or "")
        log("\n" + f.describe() + "\n")
        briefs = run_search(make_client(), f, a.top, _csv(a.player))
        with open(a.save, "w", encoding="utf-8") as fh:
            json.dump({"filters": f.model_dump(), "briefs": [b.model_dump() for b in briefs]},
                      fh, ensure_ascii=False, indent=2)
        md = os.path.splitext(a.save)[0] + ".md"
        with open(md, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(f, briefs))
        log(f"Saved {a.save} and {md}")

    if notion:
        for b in briefs:
            log(f"Notion: {b.name} -> {notion.add(b)}")

    log("\nWatch list:")
    for b in briefs:
        log(f"  {b.signal}  {b.name} ({b.club}) · data {b.data_quality} · {next_step_text(b)}")


if __name__ == "__main__":
    main()
