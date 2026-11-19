"""Shared helpers for the Pack-Brain extractors (stdlib only)."""
import io, json, re, zipfile
from pathlib import Path


def strip_json_comments(txt: str) -> str:
    """Remove // and /* */ comments and trailing commas, respecting string literals."""
    out, i, n = [], 0, len(txt)
    in_str = False
    while i < n:
        c = txt[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(txt[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and i + 1 < n and txt[i + 1] == "/":
            while i < n and txt[i] != "\n":
                i += 1
            continue
        elif c == "/" and i + 1 < n and txt[i + 1] == "*":
            i += 2
            while i + 1 < n and not (txt[i] == "*" and txt[i + 1] == "/"):
                i += 1
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json(raw: bytes):
    try:
        return json.loads(raw)
    except Exception:
        try:
            return json.loads(strip_json_comments(raw.decode("utf-8-sig", "replace")))
        except Exception:
            return None


def iter_archives(path: Path):
    """Yield (label, ZipFile) for a jar and its jar-in-jar archives."""
    z = zipfile.ZipFile(path)
    yield path.name, z
    for n in z.namelist():
        if n.startswith("META-INF/jarjar/") and n.endswith(".jar"):
            try:
                yield f"{path.name}!{n.split('/')[-1]}", zipfile.ZipFile(io.BytesIO(z.read(n)))
            except Exception:
                pass


def all_archives(pack: Path, vanilla: Path, extra=()):
    for jar in sorted((pack / "mods").glob("*.jar")):
        try:
            yield from iter_archives(jar)
        except Exception:
            pass
    yield "1.21.1.jar", zipfile.ZipFile(vanilla)
    for e in extra:
        yield Path(e).name, zipfile.ZipFile(e)


_PATCHOULI_CODE = re.compile(r"\$\((?:br|br2|p|/p|l:[^)]*|/l|[a-z0-9#]*)\)")


def clean_patchouli(text: str) -> str:
    text = text.replace("$(br2)", "\n\n").replace("$(br)", "\n").replace("$(p)", "\n\n")
    text = re.sub(r"\$\([^)]*\)", "", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def strip_md(text: str):
    """Return (title, body) for a GuideME-style markdown page with YAML frontmatter."""
    title = None
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            m = re.search(r"^\s*title:\s*(.+)$", fm, re.M)
            if m:
                title = m.group(1).strip().strip("\"'")
            text = text[end + 4:]
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else None
    # GuideME components: keep link targets as text, drop images/scenes/layout tags
    text = re.sub(r'<(?:ItemLink|BlockLink|Recipe\w*)\b[^>]*?\bid="([^"]+)"[^>]*/?>', r"{\1}", text)
    text = re.sub(r"<GameScene\b.*?</GameScene>", "", text, flags=re.S)
    text = re.sub(r"<(?:ItemImage|BlockImage|Row|Column|\w*Grid|Color)\b[^>]*/>", "", text)
    text = re.sub(r"</?(?:Row|Column|Color|ItemGrid|CategoryIndex|SubPages)\b[^>]*>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()
