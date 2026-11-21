"""Freeze the live UI into static HTML (real data from the running server + state/events.jsonl) and screenshot it.

  python tools/snapshot_live.py [--port 8765]   -> docs/screenshots/live_*.html / .png

The live page is the same file; only EventSource and fetch are replaced by the recorded data, so the
screenshots show exactly what the live UI shows at the end of the run.
"""
import argparse
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    graph = get(a.port, "/api/graph")
    incs = get(a.port, "/api/incidents")
    series = {}
    for i in incs:
        if i.get("first_cause"):
            q = urllib.parse.urlencode({"metric": i["first_cause"], "segment": i.get("segment", "")})
            series[f"{i['first_cause']}|{i.get('segment', '')}"] = get(a.port, f"/api/series?{q}")
    events = [json.loads(l) for l in (ROOT / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines() if l]
    page = (ROOT / "sentinel" / "outputs" / "live.html").read_text(encoding="utf-8")
    shim = """<script>
const __DATA = %s;
window.fetch = async (u) => { const url = new URL(u, "http://x");
  let body = url.pathname === "/api/graph" ? __DATA.graph : url.pathname === "/api/incidents" ? __DATA.incs
    : (__DATA.series[url.searchParams.get("metric") + "|" + (url.searchParams.get("segment") || "")] || []);
  return { json: async () => body }; };
window.EventSource = class { constructor(){ setTimeout(() => { this.onopen && this.onopen();
  for (const e of __DATA.events) this.onmessage({data: JSON.stringify(e)});
  setTimeout(() => { document.querySelector("#status").textContent = "snapshot of a finished run";
    const want = new URLSearchParams(location.search).get("inc"); const f = new URLSearchParams(location.search).get("filter");
    if (f) document.querySelector(`.filters button[data-f="${f}"]`).click();
    if (want) { const c = document.querySelector(`.inc-card[data-id="${want}"]`); c && c.click(); } }, 600); }, 50); }
  addEventListener(){} };
</script>""" % json.dumps({"graph": graph, "incs": incs, "series": series, "events": events})
    page = page.replace("<script>\nconst $=", shim + "\n<script>\nconst $=", 1)
    OUT.mkdir(parents=True, exist_ok=True)
    html = OUT / "live_snapshot.html"
    html.write_text(page, encoding="utf-8")
    alerted = [i["id"] for i in incs if i.get("label") == "supported_cause" and i["state"] != "resolved"] + \
              [i["id"] for i in incs if i.get("label") == "supported_cause"]
    shots = {"live_overview": "", "live_payment_incident": f"?inc={alerted[0] if alerted else ''}"}
    pay = next((i["id"] for i in incs if i.get("first_cause") == "payment_success_rate" and i.get("label") == "supported_cause"), None)
    fy = next((i["id"] for i in incs if "for_you" in (i.get("segment") or "") and i["state"] != "resolved"), None)
    shots = {"live_overview": "?filter=agent", "live_payment_incident": f"?inc={pay}", "live_for_you_incident": f"?inc={fy}"}
    for name, q in shots.items():
        png = OUT / f"{name}.png"
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1700,1050",
                        "--virtual-time-budget=8000", f"--screenshot={png}", html.as_uri() + q],
                       check=True, capture_output=True, timeout=180)
        print("wrote", png)


if __name__ == "__main__":
    main()
