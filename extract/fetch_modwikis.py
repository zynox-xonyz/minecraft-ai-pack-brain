"""Fetch mod documentation/wikis into mc-ai/web/modwikis/<site>/pages.jsonl (resumable, polite).

Site types:
  ghwiki     git clone --depth 1 of <repo>.wiki.git (markdown files)
  mediawiki  MediaWiki api.php: list all main-namespace pages, then plain-text extract per page (capped)
  crawl      same-prefix BFS crawler for static docs sites (robots.txt respected)
  readme     raw README.md of a GitHub repo

Usage: python -I fetch_modwikis.py <web_dir> [site-name ...]
No personal data is sent: generic User-Agent, >=0.8 s between requests to one host.
"""
import html, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

UA = "packbrain-local/0.1 (personal modpack knowledge base)"
web = Path(sys.argv[1]) / "modwikis"
web.mkdir(parents=True, exist_ok=True)
only = set(sys.argv[2:])

# name, mod id in the pack, type, params
SITES = [
    ("create", "create", "crawl", {"start": "https://wiki.createmod.net/", "prefix": "https://wiki.createmod.net/", "max": 250}),
    ("aether", "aether", "mediawiki", {"api": "https://aether.wiki.gg/api.php", "max": 400, "noextracts": True}),
    ("iceandfire", "iceandfire", "crawl", {"start": "https://iaf-docs.pages.dev/docs/mod/ice-and-fire-ce/", "prefix": "https://iaf-docs.pages.dev/docs/", "max": 200}),
    ("waystones", "waystones", "crawl", {"start": "https://mods.twelveiterations.com/minecraft/waystones/21.1", "prefix": "https://mods.twelveiterations.com/minecraft/waystones/21.1", "max": 60}),
    ("terralith", "terralith", "mediawiki", {"api": "https://stardustlabs.miraheze.org/w/api.php", "max": 400}),
    ("create_aeronautics", "create_aeronautics", "crawl", {"start": "https://createaeronautics.wiki/", "prefix": "https://createaeronautics.wiki", "max": 80}),
    ("farmersdelight", "farmersdelight", "ghwiki", {"repo": "vectorwing/FarmersDelight"}),
    ("sable", "sable", "ghwiki", {"repo": "ryanhcode/sable"}),
    ("tacztweaks", "tacz-tweaks", "ghwiki", {"repo": "MUKSC/TaCZTweaks"}),
    ("enderscape", "enderscape", "ghwiki", {"repo": "Penumbra-MC/Enderscape"}),
    ("yungcaves", "betterdeserttemples", "ghwiki", {"repo": "YUNG-GANG/YUNGs-Better-Caves"}),
    ("trimmed", "trimmed", "ghwiki", {"repo": "dhyces/trimmed"}),
]

# extra GitHub wikis/READMEs for every Modrinth project whose source_url is on GitHub (best effort)
mr = Path(sys.argv[1]) / "modrinth.json"
if mr.exists():
    known = {s[3].get("repo", "").lower() for s in SITES if s[2] == "ghwiki"}
    for e in json.loads(mr.read_text(encoding="utf-8")):
        m = re.match(r"https?://github\.com/([^/]+/[^/#?]+)", e.get("source_url") or "")
        if m:
            repo = m.group(1).removesuffix(".git")
            if repo.lower() not in known:
                known.add(repo.lower())
                SITES.append((f"gh_{e['slug']}", e["slug"], "ghwiki", {"repo": repo, "readme": True}))

_last = {}


def get(url, accept_json=False):
    host = urllib.parse.urlparse(url).netloc
    wait = 0.8 - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
    return raw, ctype


class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form", "button"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts, self.links, self.title, self._in_title = [], [], "", False
        self.h1 = ""
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)
        if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div", "pre"):
            self.parts.append("\n")
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self.parts.append("\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if not self.skip:
            self.parts.append(data)


def page_text(raw, base_url):
    p = TextExtractor()
    p.feed(raw.decode("utf-8", "replace"))
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    title = (p.h1 or p.title).strip().split(" | ")[0].split(" - ")[0]
    links = [urllib.parse.urljoin(base_url, l.split("#")[0]) for l in p.links]
    return title, text, links


def save(name, mod, pages):
    d = web / name
    d.mkdir(exist_ok=True)
    with open(d / "pages.jsonl", "w", encoding="utf-8") as f:
        for pg in pages:
            f.write(json.dumps({"mod": mod, **pg}, ensure_ascii=False) + "\n")


def do_crawl(name, mod, a):
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.set_url(urllib.parse.urljoin(a["start"], "/robots.txt"))
        rp.read()
    except Exception:
        rp = None
    queue, seen, pages = [a["start"]], {a["start"]}, []
    while queue and len(pages) < a["max"]:
        url = queue.pop(0)
        if rp and not rp.can_fetch(UA, url):
            continue
        try:
            raw, ctype = get(url)
        except Exception as e:
            continue
        if "html" not in ctype:
            continue
        mref = re.search(rb'http-equiv="refresh"[^>]*url=([^"\']+)', raw[:600], re.I)
        if mref and len(raw) < 600:
            nxt = urllib.parse.urljoin(url, mref.group(1).decode())
            if nxt not in seen:
                seen.add(nxt)
                queue.insert(0, nxt)
            continue
        title, text, links = page_text(raw, url)
        if len(text) > 120:
            pages.append({"url": url, "title": title or url, "text": text})
        for l in links:
            if l.startswith(a["prefix"]) and l not in seen and not re.search(r"\.(png|jpg|gif|svg|zip|jar|pdf)$", l, re.I):
                seen.add(l)
                queue.append(l)
    save(name, mod, pages)
    return len(pages)


def do_mediawiki(name, mod, a):
    titles, cont = [], {}
    while len(titles) < a["max"]:
        q = {"action": "query", "list": "allpages", "aplimit": 500, "apfilterredir": "nonredirects", "format": "json", **cont}
        raw, _ = get(a["api"] + "?" + urllib.parse.urlencode(q))
        d = json.loads(raw)
        titles += [p["title"] for p in d["query"]["allpages"]]
        if "continue" not in d:
            break
        cont = d["continue"]
    titles = titles[: a["max"]]
    pages = []
    for t in titles:
        try:
            if a.get("noextracts"):  # wiki without the TextExtracts extension: render via action=parse
                q = {"action": "parse", "page": t, "prop": "text", "format": "json", "formatversion": 2, "redirects": 1}
                raw, _ = get(a["api"] + "?" + urllib.parse.urlencode(q))
                _, text, _ = page_text(json.loads(raw)["parse"]["text"].encode("utf-8"), a["api"])
            else:
                q = {"action": "query", "prop": "extracts", "explaintext": 1, "titles": t, "format": "json", "formatversion": 2}
                raw, _ = get(a["api"] + "?" + urllib.parse.urlencode(q))
                text = (json.loads(raw)["query"]["pages"][0].get("extract") or "").strip()
            if len(text) > 120:
                base = a["api"].replace("api.php", "index.php")
                pages.append({"url": base + "?title=" + urllib.parse.quote(t.replace(" ", "_")), "title": t, "text": text})
        except Exception:
            continue
    save(name, mod, pages)
    return len(pages)


def do_ghwiki(name, mod, a):
    pages = []
    repo = a["repo"]
    # never let Git Credential Manager pop up a login window for repos without a public wiki
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "echo", "SSH_ASKPASS": "echo"}
    tmp = tempfile.mkdtemp(prefix="wikiclone_")
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", "-c", "core.askPass=echo", "clone", "--depth", "1", "-q",
                            f"https://github.com/{repo}.wiki.git", tmp + "/w"],
                           env=env, capture_output=True, timeout=60)
        if r.returncode == 0:
            for f in sorted(Path(tmp, "w").rglob("*.md")):
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                if len(text) > 120:
                    pages.append({"url": f"https://github.com/{repo}/wiki/{f.stem}", "title": f.stem.replace("-", " "), "text": text})
        if a.get("readme"):
            try:
                raw, _ = get(f"https://raw.githubusercontent.com/{repo}/HEAD/README.md")
                text = raw.decode("utf-8", "replace").strip()
                if len(text) > 200:
                    pages.append({"url": f"https://github.com/{repo}#readme", "title": f"{repo.split('/')[1]} README", "text": text})
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if pages:
        save(name, mod, pages)
    return len(pages)


for name, mod, typ, a in SITES:
    if only and name not in only:
        continue
    if (web / name / "pages.jsonl").exists():
        print(f"skip (cached) {name}")
        continue
    try:
        n = {"crawl": do_crawl, "mediawiki": do_mediawiki, "ghwiki": do_ghwiki}[typ](name, mod, a)
    except Exception as e:
        n = f"ERROR {e}"
    print(f"{name:28s} {typ:9s} -> {n} pages", flush=True)
