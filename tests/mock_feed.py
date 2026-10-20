"""A local stand-in for Elections Alberta's results feed, used by the tests.

It serves the same JSON shape as https://results.elections.ab.ca/data/8400 (one question's
division results per request), built from a saved snapshot of real numbers. It can also
behave like the real feed after a version bump: refuse requests without the current
version and expect the client to read it from the results page.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "3.3.1"
QUESTION_IDS = {**{n: n for n in range(1, 10)}, 10: 11}


class MockFeed:
    def __init__(self, snapshot: dict, require_version: bool = False, broken: bool = False):
        self.snapshot = snapshot
        self.require_version = require_version
        self.broken = broken
        self.requests: list[str] = []
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                feed.requests.append(self.path)
                url = urlparse(self.path)
                if url.path == "/8400":
                    self._send("text/html", feed.page_html())
                elif url.path == "/data/8400":
                    self._send("application/json", json.dumps(feed.data(parse_qs(url.query))))
                else:
                    self.send_error(404)

            def _send(self, ctype, body):
                raw = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def page_html(self) -> str:
        questions = [{"questionID": qid, "questionNumber": n, "questionText": f"Question {n}"}
                     for n, qid in sorted(QUESTION_IDS.items())]
        return ("<html><body><script> $(function () { let options = { countyMun: '8400', "
                f"version: '{VERSION}', apiRefreshRates: [-1, 30, 60, 90], defaultRefresh: 60, "
                f"questions: {json.dumps(questions)}, wards: [] }}; }}); </script></body></html>")

    def data(self, query: dict) -> dict:
        version = query.get("version", [None])[0]
        if self.broken or (version and version != VERSION) or (self.require_version and not version):
            return {"forceFullRefresh": True}
        wanted = int(query["questions"][0])
        s = self.snapshot
        stats = []
        for n, qid in sorted(QUESTION_IDS.items(), key=lambda kv: kv[1]):
            votes = s["questions"][str(n)]
            yes = sum(v[0] for v in votes.values())
            no = sum(v[1] for v in votes.values())
            wards = [{"ward": f"{int(e):02d}", "yesVotes": v[0], "noVotes": v[1], "pollStats": []}
                     for e, v in sorted(votes.items(), key=lambda kv: int(kv[0]))] if qid == wanted else []
            stats.append({"questionID": qid, "yesVotes": yes, "noVotes": no, "wardStats": wards})
        return {
            "success": True, "forceFullRefresh": False, "lastUpdated": s["lastUpdated"],
            "candidateResults": None,
            "referendumResults": {
                "widgets": {"summary": {"votes": 0, "valid": 0, "pollsReporting": s["pollsReporting"],
                                        "totalPolls": s["totalPolls"]}},
                "wards": [{"ward": f"{int(e):02d}", "pollsReporting": p[0], "totalPolls": p[1]}
                          for e, p in sorted(s["polls"].items(), key=lambda kv: int(kv[0]))],
                "votes": {"questionStats": stats},
            },
        }
