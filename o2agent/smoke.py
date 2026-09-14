"""Smoke test: exercise the read-only tool surface against the real env.

Read-only only. Prints a compact status line per tool. Run:
    python -m o2agent.smoke
"""
from __future__ import annotations

from .client import ReadOnlyClient
from .config import Settings
from .router import route
from .tools import GetSchemaInput, ListStreamsInput, OpenObserveTools, SearchInput


def _fmt(r) -> str:
    if not r.ok:
        return f"FAIL  {r.error}"
    d = r.data
    if isinstance(d, dict) and "list" in d:
        n = len(d["list"])
        extra = f", total={d.get('total')}" if "total" in d else ""
        return f"ok    items={n}{extra} ({r.duration_ms}ms)"
    if isinstance(d, dict) and "hits" in d:
        return f"ok    hits={len(d['hits'])} scan_records={r.scan_records} ({r.duration_ms}ms)"
    if isinstance(d, list):
        return f"ok    items={len(d)} ({r.duration_ms}ms)"
    return f"ok    ({r.duration_ms}ms)"


def main() -> None:
    s = Settings.load()
    print(f"# base={s.base_url} org={s.org}\n")

    with ReadOnlyClient(s) as c:
        t = OpenObserveTools(c)

        print("[read-only tools]")
        print("  list_organizations :", _fmt(t.list_organizations()))
        streams = t.list_streams()
        print("  list_streams       :", _fmt(streams))
        print("  list_alerts (v2)   :", _fmt(t.list_alerts()))
        print("  alert_history (v2) :", _fmt(t.alert_history()))
        print("  list_dashboards    :", _fmt(t.list_dashboards()))
        print("  list_functions     :", _fmt(t.list_functions()))
        print("  list_pipelines     :", _fmt(t.list_pipelines()))
        print("  list_reports       :", _fmt(t.list_reports()))
        print("  list_saved_views   :", _fmt(t.list_saved_views()))
        print("  list_enrichment    :", _fmt(t.list_enrichment_tables()))
        print("  summary            :", _fmt(t.summary()))

        # schema + bounded search against a known stream
        first = None
        if streams.ok and streams.data.get("list"):
            first = streams.data["list"][0]["name"]
        if first:
            sc = t.get_schema(GetSchemaInput(stream_name=first))
            fields = [f["name"] for f in sc.data.get("schema", [])] if sc.ok else []
            print(f"  get_schema({first}) :", _fmt(sc), "fields=", fields)
            sr = t.search(SearchInput(
                sql=f'SELECT * FROM "{first}"',
                start_time=1780900000000000, end_time=1783700000000000, size=2,
            ))
            print("  search             :", _fmt(sr))

    print("\n[intent router]")
    for q in [
        "Write SQL to count errors",
        "Why was my last alert fired?",
        "Create an alert for 5% error rate",
        "Delete this stream",
        "How does OpenObserve retention work?",
    ]:
        print(f"  {route(q).intent.value:22} <- {q!r}")


if __name__ == "__main__":
    main()
