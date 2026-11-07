#!/usr/bin/env python3
"""Regenerates the profile's images from live data. Standard library only.

Run daily by .github/workflows/main.yml. Projects are discovered, so nothing
here needs touching when one changes:

  * A public repo whose homepage or README links to a Chrome Web Store listing
    is an extension. Name, description, users and rating come from the store.
    It is listed once it has MIN_USERS users, and hidden until then.
  * Any other public repo with a real description is listed too. Give a repo a
    description to show it; remove the description to hide it.

Usage: generate.py --out dist --readme README.md
Output: <out>/*.svg, <out>/data.json (last good values, used if a source is
down) and the Projects section of the README.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

USER = "harvey"
NAME = "Harvey Chandler"
SITE = "harveychandler.dev"
MIN_USERS = 100
MAX_PROJECTS = 6
SHOW_ONLY = ["Panopto-Extension"]  # repo names to list; empty lists everything found
MIN_DESCRIPTION = 20  # hides placeholder descriptions like "data analysis"
FEATURED = ["Decdata-Web"]  # listed straight after the extensions
RAW = f"https://raw.githubusercontent.com/{USER}/{USER}/output"

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Consolas,'Liberation Mono',monospace"
THEMES = {
    "dark": {"bg": "#0d1016", "panel": "#141923", "ink": "#f2f4fb", "muted": "#99a1b3", "line": "#272e3c",
             "accent": "#7c8cff", "wash": ("#7c8cff", "#2dd4bf", "#f472b6"), "wash_strength": ".5",
             "grid": ".5"},
    "light": {"bg": "#fbfbfe", "panel": "#ffffff", "ink": "#151924", "muted": "#596173", "line": "#dfe2ec",
              "accent": "#4656d6", "wash": ("#7c8cff", "#2dd4bf", "#f472b6"), "wash_strength": ".38",
              "grid": ".7"},
}
LANGUAGE_COLOURS = {"JavaScript": "#f1e05a", "TypeScript": "#3178c6", "Python": "#3572a5",
                    "Shell": "#89e051", "HTML": "#e34c26", "CSS": "#663399", "Kotlin": "#a97bff"}


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def fetch(url, accept=None, token=None, binary=False):
    """GET with two retries. Raises the last error if all three attempts fail."""
    headers = {"User-Agent": "Mozilla/5.0 (profile-card-generator)"}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as r:
                body = r.read()
            return body if binary else body.decode("utf-8", "replace")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


# --- data ----------------------------------------------------------------

STORE_LINK = re.compile(
    r"(?:chromewebstore\.google\.com/detail|chrome\.google\.com/webstore/detail)/(?:[^/\s\"')]+/)?([a-p]{32})")


def public_repos(token):
    repos = json.loads(fetch(
        f"https://api.github.com/users/{USER}/repos?per_page=100&sort=pushed&type=owner",
        "application/vnd.github+json", token))
    return [r for r in repos
            if not r["fork"] and r["name"] != USER and not r["name"].endswith(".github.io")]


def store_ids(repo, token):
    """Every store listing a repo links to, from its homepage and README."""
    text = repo.get("homepage") or ""
    try:
        text += fetch(f"https://api.github.com/repos/{USER}/{repo['name']}/readme",
                      "application/vnd.github.raw", token)
    except Exception:
        pass  # most often a repo with no README
    return list(dict.fromkeys(STORE_LINK.findall(text)))


def parse_store_page(page):
    """Listing details, or None if this is not a real listing page."""
    title = re.search(r"<title>(.*?)</title>", page)
    users = re.search(r"([\d,]+)\s+users?\b", page)
    if not title or "Chrome Web Store" not in title[1] or not users:
        return None
    description = re.search(r'og:description" content="(.*?)"', page)
    icon = re.search(r'og:image" content="(.*?)"', page)
    return {
        "name": html.unescape(title[1]).split(" - ")[0].strip(),
        "description": html.unescape(description[1]).strip() if description else "",
        "users": int(users[1].replace(",", "")),
        "icon_url": html.unescape(icon[1]) if icon else "",
    }


def shield_value(kind, ext_id):
    url = f"https://img.shields.io/chrome-web-store/{kind}/{ext_id}.json"
    return json.loads(fetch(url))["value"]


def parse_count(text):
    """'32 total' -> 32, '1.2k total' -> 1200."""
    match = re.match(r"([\d.]+)(k?)", text.strip())
    return int(float(match[1]) * (1000 if match[2] else 1)) if match else 0


def extension(ext_id, repo, cached):
    """Live details for a listing, falling back to the last good values."""
    previous = cached.get(ext_id, {})
    try:
        details = parse_store_page(fetch(f"https://chromewebstore.google.com/detail/{ext_id}"))
        if details is None:
            raise ValueError("unrecognised listing page")
    except Exception as e:
        warn(f"store listing {ext_id}: {e}")
        if not previous:
            return None
        details = dict(previous)

    try:
        match = re.match(r"([\d.]+)", shield_value("rating", ext_id))
        details["rating"] = float(match[1]) if match else 0.0
        details["ratings"] = parse_count(shield_value("rating-count", ext_id))
    except Exception as e:
        warn(f"rating {ext_id}: {e}")
        details["rating"] = previous.get("rating", 0.0)
        details["ratings"] = previous.get("ratings", 0)

    details["icon"] = previous.get("icon", "")
    if details.get("icon_url") and details["icon_url"] != previous.get("icon_url"):
        try:
            data = fetch(details["icon_url"], binary=True)
            details["icon"] = "data:image/png;base64," + base64.b64encode(data).decode()
        except Exception as e:
            warn(f"icon {ext_id}: {e}")

    details.update(id=ext_id, repo=repo["name"],
                   url=f"https://chromewebstore.google.com/detail/{ext_id}")
    return details


def collect(token, cache):
    cached_exts = {e["id"]: e for e in cache.get("extensions", [])}
    try:
        repos = public_repos(token)
    except Exception as e:
        if not cache:
            raise
        warn(f"GitHub API unavailable, reusing last good data: {e}")
        return cache

    extensions, seen = [], set()
    for repo in repos:
        for ext_id in store_ids(repo, token):
            if ext_id not in seen:
                seen.add(ext_id)
                details = extension(ext_id, repo, cached_exts)
                if details:
                    extensions.append(details)
    return {
        "extensions": extensions,
        "repos": [{k: r[k] for k in ("name", "html_url", "description", "language",
                                     "stargazers_count", "pushed_at")}
                  for r in repos],
    }


def projects(data):
    """Everything to list, as one uniform shape: extensions first, then repos."""
    out = []
    extensions = [e for e in data["extensions"] if e["users"] >= MIN_USERS]
    for e in sorted(extensions, key=lambda e: -e["users"]):
        meta = "Chrome extension"
        if e["ratings"]:
            meta += f"  ·  ★ {e['rating']:.1f}  ·  {e['ratings']} ratings"
        out.append({"key": e["repo"], "name": e["name"], "description": e["description"], "url": e["url"],
                    "icon": e.get("icon", ""), "meta": meta, "language": None,
                    "metric": f"{e['users']:,}", "metric_label": "users"})

    taken = {e["repo"] for e in data["extensions"]}
    repos = [r for r in data["repos"]
             if len(r["description"] or "") >= MIN_DESCRIPTION and r["name"] not in taken]
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)
    repos.sort(key=lambda r: r["stargazers_count"], reverse=True)
    repos.sort(key=lambda r: r["name"] not in FEATURED)
    for r in repos:
        stars = r["stargazers_count"]
        out.append({"key": r["name"], "name": r["name"], "description": r["description"].strip().rstrip("."),
                    "url": r["html_url"], "icon": "", "meta": r.get("language") or "GitHub repo",
                    "language": r.get("language"), "metric": f"★ {stars}" if stars else "",
                    "metric_label": "stars" if stars > 1 else "star" if stars else ""})
    return [p for p in out if not SHOW_ONLY or p["key"] in SHOW_ONLY][:MAX_PROJECTS]


# --- svg -----------------------------------------------------------------

def wrap(text, size, width, lines=2):
    per_line = int(width / (size * 0.5))
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > per_line:
            out.append(line)
            line = word
            if len(out) == lines:
                out[-1] = out[-1].rstrip(" ,.;:") + "…"
                return out
        else:
            line = f"{line} {word}".strip()
    return out + [line] if line else out


def svg(width, height, label, style, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label, {chr(34): "&quot;"})}">'
            f"<style>text{{font-family:{SANS}}}.m{{font-family:{MONO}}}{style}</style>{body}</svg>\n")


def hero(t):
    width, height = 840, 280
    name_width = 640  # fixed so the cursor sits right after the name in any font
    drift = [("a", 22, (150, 40), (420, 180)), ("b", 29, (720, 240), (430, 60)), ("c", 37, (420, 300), (760, 90))]
    style = "".join(
        f"@keyframes {n}{{from{{transform:translate({x0}px,{y0}px)}}to{{transform:translate({x1}px,{y1}px)}}}}"
        f".{n}{{animation:{n} {seconds}s ease-in-out infinite alternate}}"
        for n, seconds, (x0, y0), (x1, y1) in drift)
    style += ("@keyframes k{0%,49%{opacity:1}50%,100%{opacity:0}}.k{animation:k 1.1s step-end infinite}"
              "@keyframes u{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}"
              ".u{animation:u .9s ease-out both}.v{animation:u .9s .2s ease-out both}")
    gradients = "".join(
        f'<radialGradient id="g{i}"><stop offset="0" stop-color="{colour}" stop-opacity="{t["wash_strength"]}"/>'
        f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></radialGradient>'
        for i, colour in enumerate(t["wash"]))
    blobs = "".join(f'<circle class="{n}" r="{r}" fill="url(#g{i})"/>'
                    for i, (n, r) in enumerate([("a", 300), ("b", 270), ("c", 240)]))
    body = f"""
<defs>{gradients}
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="{t['line']}" stroke-opacity="{t['grid']}"/></pattern>
<clipPath id="frame"><rect width="{width}" height="{height}" rx="18"/></clipPath>
</defs>
<g clip-path="url(#frame)">
<rect width="{width}" height="{height}" fill="{t['bg']}"/>{blobs}<rect width="{width}" height="{height}" fill="url(#grid)"/>
<g class="u"><text x="44" y="158" font-size="88" font-weight="800" textLength="{name_width}" lengthAdjust="spacingAndGlyphs" fill="{t['ink']}">{escape(NAME)}</text>
<rect class="k" x="{44 + name_width + 12}" y="88" width="7" height="78" rx="2" fill="{t['accent']}"/></g>
<text class="m v" x="46" y="212" font-size="25" fill="{t['muted']}">{escape(SITE)}</text>
</g>
<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="18" fill="none" stroke="{t['line']}"/>"""
    return svg(width, height, NAME, style, body)


def card(t, p):
    width = 840
    lines = wrap(p["description"], 23, width - 56)
    height = 128 + 31 * len(lines) - 5
    if p["icon"]:
        icon = f'<image href="{p["icon"]}" x="28" y="24" width="68" height="68" clip-path="url(#icon)"/>'
    else:
        icon = (f'<rect x="28" y="24" width="68" height="68" rx="16" fill="{t["accent"]}" fill-opacity=".14" stroke="{t["accent"]}" stroke-opacity=".5"/>'
                f'<text x="62" y="71" font-size="34" font-weight="800" text-anchor="middle" fill="{t["accent"]}">{escape(p["name"][:1].upper())}</text>')
    if p["language"]:
        colour = LANGUAGE_COLOURS.get(p["language"], t["muted"])
        meta = (f'<circle cx="124" cy="82" r="7" fill="{colour}"/>'
                f'<text x="138" y="89" font-size="22" fill="{t["muted"]}">{escape(p["meta"])}</text>')
    else:
        meta = f'<text x="116" y="89" font-size="22" fill="{t["muted"]}">{escape(p["meta"])}</text>'
    metric = ""
    if p["metric"]:
        metric = (f'<text x="812" y="60" font-size="40" font-weight="800" text-anchor="end" fill="{t["accent"]}">{escape(p["metric"])}</text>'
                  f'<text x="812" y="89" font-size="22" text-anchor="end" fill="{t["muted"]}">{p["metric_label"]}</text>')
    # Long names shrink to stay clear of the metric on the right.
    name_size = min(31.0, 31 * 540 / max(1.0, len(p["name"]) * 31 * 0.56))
    body = (
        f'<defs><clipPath id="icon"><rect x="28" y="24" width="68" height="68" rx="16"/></clipPath></defs>'
        f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="18" fill="{t["panel"]}" stroke="{t["line"]}"/>'
        f'{icon}<text x="116" y="56" font-size="{name_size:.1f}" font-weight="800" fill="{t["ink"]}">{escape(p["name"])}</text>'
        f"{meta}{metric}"
        + "".join(f'<text x="28" y="{134 + i * 31}" font-size="23" fill="{t["ink"]}">{escape(line)}</text>'
                  for i, line in enumerate(lines)))
    label = f"{p['name']}. {p['description']}" + (f" {p['metric']} {p['metric_label']}." if p["metric"] else "")
    return svg(width, height, label, "", body)


# --- readme --------------------------------------------------------------

def slug(project):
    return "project-" + re.sub(r"[^a-z0-9]+", "-", project["key"].lower())


def projects_section(items):
    out = []
    for p in items:
        name, alt = slug(p), escape(p["name"], {'"': "&quot;"})
        out.append(f'<a href="{p["url"]}"><picture><source media="(prefers-color-scheme: dark)" srcset="{RAW}/{name}-dark.svg" />'
                   f'<source media="(prefers-color-scheme: light)" srcset="{RAW}/{name}-light.svg" />'
                   f'<img alt="{alt}" src="{RAW}/{name}-dark.svg" width="100%" /></picture></a>')
    return "\n\n".join(out)


def replace_section(readme, name, content):
    pattern = re.compile(rf"(<!-- {name}:start -->\n).*?(<!-- {name}:end -->)", re.S)
    if not pattern.search(readme):
        raise ValueError(f"README is missing the {name} markers")
    return pattern.sub(lambda m: m[1] + content + "\n" + m[2], readme)


# --- main ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--readme", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    cache_path = args.out / "data.json"
    cache = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
    data = collect(os.environ.get("GITHUB_TOKEN"), cache)
    items = projects(data)
    if not items:
        raise SystemExit("no projects found, keeping the current profile")

    for theme, t in THEMES.items():
        files = {"hero": hero(t)}
        files.update({slug(p): card(t, p) for p in items})
        for name, content in files.items():
            (args.out / f"{name}-{theme}.svg").write_text(content, "utf-8", newline="\n")
    cache_path.write_text(json.dumps(data, indent=1), "utf-8", newline="\n")

    if args.readme:
        readme = args.readme.read_text("utf-8")
        args.readme.write_text(replace_section(readme, "projects", projects_section(items)), "utf-8", newline="\n")
    print("\n".join(f"{p['name']}: {p['metric']} {p['metric_label']}" for p in items).encode("ascii", "replace").decode())


if __name__ == "__main__":
    main()
