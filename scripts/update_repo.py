#!/usr/bin/env python3
"""Keep repo/pluginmaster.json and the README plugin list in sync with releases.

For every plugin in repo/pluginmaster.json (its GitHub repo is taken from
RepoUrl), this reads the plugin's manifest from the release zip and updates:

  stable   latest GitHub release            -> AssemblyVersion, DalamudApiLevel,
                                               LastUpdate
  testing  release tagged "Testing"         -> TestingAssemblyVersion,
           (if one exists)                     TestingDalamudApiLevel,
                                               DownloadLinkTesting

Hand-written fields (Name, Punchline, Description, Tags, IconUrl, ...) are never
touched. If a plugin has no Testing release, its testing fields are removed.

Zips are only downloaded when a release asset is new or changed; what was seen
last time is remembered in repo/sync-state.json.

The plugin table in README.md (between the PLUGINS markers) is rebuilt from
pluginmaster.json on every run.
"""
import io, json, os, re, sys, zipfile, urllib.error, urllib.request
from datetime import datetime, timezone

MASTER = "repo/pluginmaster.json"
STATE = "repo/sync-state.json"
README = "README.md"

STABLE_ASSET = "latest.zip"   # preferred zip name; falls back to any .zip
TESTING_TAG = "Testing"       # tag of the pre-release that holds testing builds
API = "https://api.github.com"

TOKEN = os.environ.get("GITHUB_TOKEN")
START, END = "<!-- PLUGINS:START -->", "<!-- PLUGINS:END -->"


class NotFound(Exception):
    pass


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": "lunarrepo-updater"})
    # The token only ever goes to GitHub's API, never to download hosts.
    if TOKEN and url.startswith(API + "/"):
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise
    return data if binary else json.loads(data)


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def set_after(d, key, value, after):
    """Set d[key]; if the key is new, place it right after `after` when present."""
    if key in d or after not in d:
        d[key] = value
        return
    items = list(d.items())
    d.clear()
    for k, v in items:
        d[k] = v
        if k == after:
            d[key] = value


def pick_asset(rel):
    assets = rel.get("assets", [])
    asset = next((a for a in assets if a["name"] == STABLE_ASSET), None) \
        or next((a for a in assets if a["name"].lower().endswith(".zip")), None)
    if not asset:
        raise RuntimeError(f"release {rel.get('tag_name')} has no zip asset")
    return asset


def fingerprint(asset):
    return f"{asset['id']}:{asset['updated_at']}:{asset['size']}"


def read_manifest(zdata, internal_name):
    with zipfile.ZipFile(io.BytesIO(zdata)) as z:
        names = [n for n in z.namelist()
                 if "/" not in n and n.lower().endswith(".json") and not n.lower().endswith(".deps.json")]
        pick = next((n for n in names if n.lower() == f"{internal_name}.json".lower()), None)
        if not pick and len(names) == 1:
            pick = names[0]
        if not pick:
            raise RuntimeError("plugin manifest not found in zip")
        return json.loads(z.read(pick).decode("utf-8-sig"))


def sync_stable(e, owner, repo, st):
    rel = get(f"{API}/repos/{owner}/{repo}/releases/latest")
    asset = pick_asset(rel)
    fp = fingerprint(asset)
    if st.get("stable") == fp and e.get("AssemblyVersion") and e.get("LastUpdate"):
        return False
    man = read_manifest(get(asset["browser_download_url"], binary=True), e["InternalName"])
    if not man.get("AssemblyVersion"):
        raise RuntimeError("manifest has no AssemblyVersion")
    e["AssemblyVersion"] = man["AssemblyVersion"]
    if man.get("DalamudApiLevel") is not None:
        e["DalamudApiLevel"] = man["DalamudApiLevel"]
    stamp = max(parse_ts(rel["published_at"]), parse_ts(asset["updated_at"]))
    set_after(e, "LastUpdate", int(stamp.timestamp()), "DalamudApiLevel")
    st["stable"] = fp
    return True


def sync_testing(e, owner, repo, st):
    try:
        rel = get(f"{API}/repos/{owner}/{repo}/releases/tags/{TESTING_TAG}")
    except NotFound:
        rel = None

    if rel is None:
        changed = False
        for k in ("TestingAssemblyVersion", "TestingDalamudApiLevel"):
            changed |= e.pop(k, None) is not None
        if e.get("DownloadLinkInstall") and e.get("DownloadLinkTesting") != e["DownloadLinkInstall"]:
            e["DownloadLinkTesting"] = e["DownloadLinkInstall"]
            changed = True
        changed |= st.pop("testing", None) is not None
        return changed

    asset = pick_asset(rel)
    fp = fingerprint(asset)
    if st.get("testing") == fp and e.get("TestingAssemblyVersion"):
        return False
    man = read_manifest(get(asset["browser_download_url"], binary=True), e["InternalName"])
    if not man.get("AssemblyVersion"):
        raise RuntimeError("testing manifest has no AssemblyVersion")
    set_after(e, "TestingAssemblyVersion", man["AssemblyVersion"], "AssemblyVersion")
    level = man.get("DalamudApiLevel", e.get("DalamudApiLevel"))
    if level is not None:
        set_after(e, "TestingDalamudApiLevel", level, "TestingAssemblyVersion")
    e["DownloadLinkTesting"] = asset["browser_download_url"]
    st["testing"] = fp
    return True


def sync_plugin(e, state):
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", e.get("RepoUrl", ""))
    if not m:
        raise RuntimeError("RepoUrl is not a GitHub repository")
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    st = state.setdefault(e["InternalName"], {})
    a = sync_stable(e, owner, repo, st)
    b = sync_testing(e, owner, repo, st)
    return a or b


# --- README ------------------------------------------------------------------

def _cell(s):
    return " ".join(str(s or "").split()).replace("|", "\\|")


def _date(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def plugin_table(entries):
    rows = ["| Plugin | Description | Version | Testing | Updated |",
            "|---|---|---|---|---|"]
    for e in sorted(entries, key=lambda e: e.get("Name", "").lower()):
        if str(e.get("IsHide")).lower() == "true":
            continue
        name = _cell(e.get("Name"))
        if e.get("RepoUrl"):
            name = f"[{name}]({e['RepoUrl']})"
        desc = _cell(e.get("Description"))
        if e.get("Punchline"):
            desc = f"**{_cell(e['Punchline'])}**<br>{desc}" if desc else f"**{_cell(e['Punchline'])}**"
        rows.append(
            f"| {name} | {desc} | {_cell(e.get('AssemblyVersion')) or '-'} "
            f"| {_cell(e.get('TestingAssemblyVersion')) or '-'} | {_date(e.get('LastUpdate'))} |"
        )
    return "\n".join(rows)


STAMP_LINE = re.compile(r"^_Last updated: .*_\s*$", re.M)
STAMP_TIME = re.compile(r"^_Last updated: (.*) UTC_\s*$", re.M)


def install_block(repo_url):
    return f"""## Install

1. In game, type `/xlsettings` and open the **Experimental** tab.
2. Under **Custom Plugin Repositories**, paste this link, click **+**, tick its
   checkbox, then click the save icon:

```
{repo_url}
```

3. Type `/xlplugins` and look under **Available Plugins**.

Use the `raw.githubusercontent.com` link above, not the `github.com` page link.

## Plugins
"""


def write_readme(entries, path=README, now=None):
    """Rebuild the plugin list. 'Last updated' only moves when the table changes."""
    repo = os.environ.get("GITHUB_REPOSITORY", "<you>/<repo>")
    repo_url = f"https://raw.githubusercontent.com/{repo}/main/repo/pluginmaster.json"
    now = now or datetime.now(timezone.utc)
    table = plugin_table(entries)
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""

    stamp = now.strftime("%Y-%m-%d %H:%M")
    if START in text and END in text:
        head, rest = text.split(START, 1)
        mid, tail = rest.split(END, 1)
        prior = STAMP_TIME.search(mid)
        if prior and STAMP_LINE.sub("", mid).strip() == table.strip():
            stamp = prior.group(1)
        prefix, suffix = head, tail
    else:
        prefix = text.rstrip("\n") + ("\n\n" if text.strip() else "") + install_block(repo_url) + "\n"
        suffix = "\n"

    new = f"{prefix}{START}\n\n_Last updated: {stamp} UTC_\n\n{table}\n\n{END}{suffix}"
    if new != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)


# --- main ----------------------------------------------------------------------

def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data, **kw):
    text = json.dumps(data, indent=2, ensure_ascii=False, **kw) + "\n"
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    if text != old:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def main():
    master = load(MASTER, [])
    state = load(STATE, {})
    failures = 0

    for e in master:
        name = e.get("InternalName", "?")
        try:
            print(f"{'updated' if sync_plugin(e, state) else 'current'}  {name}")
        except Exception as ex:
            failures += 1
            print(f"::warning::{name} failed: {ex}")

    save(MASTER, master)
    save(STATE, state, sort_keys=True)
    write_readme(master)
    sys.exit(1 if master and failures == len(master) else 0)


if __name__ == "__main__":
    main()
