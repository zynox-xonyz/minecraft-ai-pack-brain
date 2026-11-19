"""Fetch FULL text of Minecraft Wiki pages for every vanilla item, mob, biome and structure name in the pack DB.
(fetch_web.py only fetched intros for those; full pages hold drops, spawning, trading, usage tables ...)

Polite: one request per second, resumable cache web/wiki_full/pages.jsonl, generic User-Agent. Text is CC BY-NC-SA 3.0.
Usage: python -I fetch_wiki_full.py <db_path> <web_dir>
"""
import html, json, re, sqlite3, sys, time, urllib.parse, urllib.request
from html.parser import HTMLParser
from pathlib import Path

UA = "packbrain-local/0.1 (personal modpack knowledge base)"
API = "https://minecraft.wiki/api.php"
db = sqlite3.connect(sys.argv[1])
out = Path(sys.argv[2]) / "wiki_full"
out.mkdir(parents=True, exist_ok=True)
cache = out / "pages.jsonl"


class Text(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "aside", "noscript", "svg", "sup", "table-of-contents"}

    def __init__(self):
        super().__init__()
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "") or ""
        if tag in self.SKIP or "navbox" in cls or "mw-editsection" in cls or "toc" == cls:
            self.skip += 1
        if tag in ("p", "br", "li", "tr", "div", "pre"):
            self.parts.append("\n")
        if tag in ("h2", "h3", "h4"):
            self.parts.append("\n" + "=" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if self.skip and tag in self.SKIP:
            self.skip -= 1
        if tag in ("h2", "h3", "h4"):
            self.parts.append(" " + "=" * int(tag[1]) + "\n")
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_data(self, d):
        if not self.skip:
            self.parts.append(d)


def to_text(htm):
    p = Text()
    p.feed(htm)
    t = html.unescape("".join(p.parts)).replace("\xa0", " ")
    t = re.sub(r"\[edit\]|\[ ?edit ?\]", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


titles = []
titles += [r[0] for r in db.execute("SELECT DISTINCT name FROM items WHERE mod='minecraft' AND name IS NOT NULL AND id NOT LIKE '%smithing_template%'")]
titles += [r[0] for r in db.execute("SELECT text FROM lang WHERE key LIKE 'entity.minecraft.%' AND key NOT LIKE '%.%.%.%'")]
titles += [r[0] for r in db.execute("SELECT text FROM lang WHERE key LIKE 'biome.minecraft.%'")]
titles += ["Trial Chambers", "Ancient City", "Bastion Remnant", "Nether Fortress", "End City", "Stronghold", "Mineshaft", "Village", "Pillager Outpost",
           "Woodland Mansion", "Ocean Monument", "Ocean Ruins", "Shipwreck", "Buried Treasure", "Desert Pyramid", "Jungle Temple", "Igloo", "Swamp Hut",
           "Ruined Portal", "Trail Ruins", "Dungeon", "Fossil", "Geode", "Overworld", "The Nether", "The End", "Mob", "Spawn", "Loot table", "Experience", "Trading"]
titles = list(dict.fromkeys(t.strip() for t in titles if t and len(t) < 60))

done = set()
if cache.exists():
    for line in cache.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["req"])
        except ValueError:
            pass
todo = [t for t in titles if t not in done]
print(f"{len(titles)} titles, {len(todo)} to fetch", flush=True)

with open(cache, "a", encoding="utf-8") as f:
    for n, t in enumerate(todo):
        q = urllib.parse.urlencode({"action": "parse", "page": t, "prop": "text", "redirects": 1, "format": "json", "formatversion": 2, "disableeditsection": 1})
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA}), timeout=40) as r:
                d = json.loads(r.read().decode("utf-8"))
            if "parse" in d:
                text = to_text(d["parse"]["text"])
                f.write(json.dumps({"req": t, "title": d["parse"]["title"], "text": text}, ensure_ascii=False) + "\n")
            else:
                f.write(json.dumps({"req": t, "title": t, "text": ""}) + "\n")
        except Exception as e:
            print("  fail", t, str(e)[:80], flush=True)
        f.flush()
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}", flush=True)
        time.sleep(1.0)
print("done", flush=True)
