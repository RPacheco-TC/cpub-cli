"""An in-memory Confluence Cloud that answers ``confluence_publish._req``.

It records every request as ``(method, path)`` so tests can assert, for
example, that a refused plan made zero POST/PUT calls. Offline: no sockets, no
files. Attachment uploads are routed through the same recorder without reading
the (non-existent) PNG files.
"""
import contextlib
import json
import urllib.error
import urllib.parse

from _support import cp

BASE = "https://example.invalid/wiki"


class FakeConfluence:
    def __init__(self, space_id="S1"):
        self.space_id = space_id
        self.pages = {}         # id -> dict(title, spaceId, parentId, status, version, body, props, atts)
        self.calls = []         # (method, path) in order
        self.fail_on = None     # (method, path_prefix) -> raise HTTP 500 once matched
        self.ancestor_page_size = None   # paginate the ancestors listing (tests _links.next)
        # /rest/api/user/current: an HTTP status to fail with, or the JSON to return
        self.user = {"type": "known", "accountId": "acc-1", "displayName": "Dev", "email": "x@example.invalid"}
        self._next = 1000

    # --- fixtures ---------------------------------------------------------
    def add(self, page_id, title, parent=None, space=None, status="current", marker=None, version=3):
        page = {"title": title, "spaceId": space or self.space_id, "parentId": parent,
                "status": status, "version": version, "body": "<p>old</p>", "props": {}, "atts": set()}
        if marker is not None:
            page["props"]["cpub-managed"] = {"id": f"prop{page_id}", "value": marker, "version": 1}
        self.pages[str(page_id)] = page
        return str(page_id)

    def ancestors(self, page_id):
        out, cur = [], self.pages[page_id]["parentId"]
        while cur is not None:
            out.append(cur)
            cur = self.pages[cur]["parentId"]
        return list(reversed(out))

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT", "DELETE")]

    # --- transport --------------------------------------------------------
    def _err(self, code, url, msg=""):
        e = urllib.error.HTTPError(url, code, msg or "error", {}, None)
        e.detail = msg
        return e

    def req(self, method, url, data=None, headers=None):
        u = urllib.parse.urlsplit(url)
        path = u.path[len("/wiki"):] if u.path.startswith("/wiki") else u.path
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, path))
        if self.fail_on and method == self.fail_on[0] and path.startswith(self.fail_on[1]):
            raise self._err(500, url, "boom")
        body = json.loads(data) if data and headers and "json" in headers.get("Content-Type", "") else None
        parts = path.strip("/").split("/")
        if path == "/rest/api/user/current" and method == "GET":
            if isinstance(self.user, int):
                raise self._err(self.user, url, "Unauthorized")
            return 200, dict(self.user)
        if parts[:2] != ["api", "v2"]:
            raise AssertionError(f"unexpected endpoint {method} {path}")
        parts = parts[2:]
        if method == "GET" and parts[0] == "spaces" and parts[2:] == ["pages"]:
            return 200, {"results": [{"id": i, "title": p["title"]} for i, p in self.pages.items()
                                     if p["spaceId"] == parts[1]], "_links": {}}
        if parts[0] != "pages":
            raise AssertionError(f"unexpected endpoint {method} {path}")
        if method == "POST" and len(parts) == 1:
            pid = str(self._next); self._next += 1
            self.pages[pid] = {"title": body["title"], "spaceId": body["spaceId"], "parentId": body["parentId"],
                               "status": "current", "version": 1, "body": body["body"]["value"],
                               "props": {}, "atts": set()}
            return 200, {"id": pid}
        pid = parts[1]
        if pid not in self.pages:
            raise self._err(404, url, "not found")
        page = self.pages[pid]
        rest = parts[2:]
        if not rest:
            if method == "GET":
                return 200, {"id": pid, "title": page["title"], "spaceId": page["spaceId"],
                             "parentId": page["parentId"], "status": page["status"],
                             "version": {"number": page["version"], "createdAt": "2026-10-01T10:00:00Z"},
                             "_links": {"webui": f"/spaces/X/pages/{pid}"}}
            if method == "PUT":
                assert "parentId" not in body, "updates must never move a page"
                if body["version"]["number"] != page["version"] + 1:
                    raise self._err(409, url, "Version must be incremented")
                page.update(title=body["title"], body=body["body"]["value"], version=page["version"] + 1)
                return 200, {"id": pid}
        if rest == ["ancestors"] and method == "GET":
            anc = [{"id": a, "type": "page"} for a in self.ancestors(pid)]
            if not self.ancestor_page_size:
                return 200, {"results": anc, "_links": {}}
            start, size = int(q.get("cursor", 0)), self.ancestor_page_size
            nxt = (f"/wiki/api/v2/pages/{pid}/ancestors?limit={size}&cursor={start + size}"
                   if start + size < len(anc) else None)
            return 200, {"results": anc[start:start + size], "_links": {"next": nxt} if nxt else {}}
        if rest == ["attachments"] and method == "GET":
            return 200, {"results": [{"title": t} for t in sorted(page["atts"])]}
        if rest[0] == "properties":
            if method == "GET":
                p = page["props"].get(q.get("key"))
                return 200, {"results": [] if p is None else
                             [{"id": p["id"], "key": q["key"], "value": p["value"],
                               "version": {"number": p["version"]}}]}
            if method == "POST":
                assert body["key"] not in page["props"]
                page["props"][body["key"]] = {"id": f"prop{pid}", "value": body["value"], "version": 1}
                return 200, {}
            if method == "PUT":
                p = page["props"][body["key"]]
                assert rest[1] == p["id"] and body["version"]["number"] == p["version"] + 1
                p.update(value=body["value"], version=p["version"] + 1)
                return 200, {}
        raise AssertionError(f"unexpected endpoint {method} {path}")

    def upload(self, page_id, filepath, filename):
        self.calls.append(("POST", f"/rest/api/content/{page_id}/child/attachment"))
        self.pages[page_id]["atts"].add(filename)
        return {}


@contextlib.contextmanager
def installed(fake):
    """Route the publisher's REST layer to ``fake`` for the duration of the block."""
    saved = {n: getattr(cp, n) for n in ("_req", "_cfg", "upload_attachment")}
    cp._req = fake.req
    cp._cfg = lambda: (BASE, "x@example.invalid", "token")
    cp.upload_attachment = fake.upload
    try:
        yield fake
    finally:
        for n, f in saved.items():
            setattr(cp, n, f)
