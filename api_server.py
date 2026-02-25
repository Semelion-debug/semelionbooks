import json
import os
import re
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOK_LINKS_PATH = os.path.join(BASE_DIR, "book_links.txt")


def normalize_text(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def extract_form(section_title):
    match = re.search(r"FORM\s*([1-6])", section_title.upper())
    if match:
        return f"Form {match.group(1)}"
    if "PAST PAPERS" in section_title.upper():
        return "Past Papers"
    return "General"


def extract_section_subject(section_title):
    title = section_title.replace("\u202f", " ").strip()
    upper = title.upper()
    if "PAST PAPERS" in upper:
        return title.replace("PAST PAPERS", "").strip().title() or "Past Papers"
    if "BOOKS" in upper:
        return "General"
    return title.title() or "General"


def derive_subject(name, section_subject):
    if section_subject and section_subject != "General":
        return section_subject
    base = re.sub(r"\s*\(.*?\)", "", name).strip()
    return base or name


def parse_book_links(text):
    section_title = "General"
    form = "General"
    subject = "General"
    books = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("###"):
            section_title = line.replace("###", "").strip()
            form = extract_form(section_title)
            subject = extract_section_subject(section_title)
            continue
        match = re.match(r"^- \*\*(.+?)\*\*\s*<(.+?)>\s*$", line)
        name = None
        link = None
        if match:
            name = match.group(1).strip().rstrip("–- ").strip()
            link = match.group(2).strip()
        else:
            match_plain = re.match(r"^- (.+?)\s*[–-]\s*<(.+?)>\s*$", line)
            if match_plain:
                name = match_plain.group(1).strip()
                link = match_plain.group(2).strip()
        if not name or not link:
            continue
        books.append(
            {
                "name": name,
                "form": form,
                "subject": derive_subject(name, subject),
                "category": section_title.replace("\u202f", " ").strip(),
                "link": link,
            }
        )
    return books


def load_books():
    if not os.path.exists(BOOK_LINKS_PATH):
        return []
    with open(BOOK_LINKS_PATH, "r", encoding="utf-8") as handle:
        text = handle.read()
    return parse_book_links(text)


def compute_score(query, item):
    normalized_query = normalize_text(query)
    normalized_name = normalize_text(item["name"])
    normalized_all = normalize_text(
        f'{item["name"]} {item["subject"]} {item["form"]} {item["category"]}'
    )
    if not normalized_query:
        return 0.0
    if normalized_query == normalized_name:
        return 1.0
    ratio_name = SequenceMatcher(None, normalized_query, normalized_name).ratio()
    ratio_all = SequenceMatcher(None, normalized_query, normalized_all).ratio()
    score = max(ratio_name, ratio_all)
    if normalized_query in normalized_name and len(normalized_query) >= 3:
        score = max(score, 0.9)
    query_tokens = set(normalized_query.split())
    name_tokens = set(normalized_name.split())
    if query_tokens:
        overlap = len(query_tokens & name_tokens) / len(query_tokens)
        if overlap > 0:
            score = max(score, 0.7 + (0.3 * overlap))
    return min(score, 1.0)


def rank_matches(query, books, limit=5):
    scored = []
    for item in books:
        score = compute_score(query, item)
        scored.append((score, item))
    scored.sort(key=lambda value: (-value[0], value[1]["name"]))
    return scored[:limit]


class BookApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health"):
            self._send_json(
                {
                    "status": "ok",
                    "endpoints": ["/books.json", "/match?q=macbeth", "/search?q=macbeth"],
                }
            )
            return
        if parsed.path == "/books.json":
            books = load_books()
            self._send_json({"count": len(books), "books": books})
            return
        if parsed.path in ("/match", "/search"):
            query_params = parse_qs(parsed.query)
            query = (
                query_params.get("q", [""])[0]
                or query_params.get("query", [""])[0]
            ).strip()
            books = load_books()
            ranked = rank_matches(query, books, limit=5)
            if not query:
                self._send_json(
                    {
                        "status": "no_query",
                        "query": query,
                        "matches": [],
                        "message": "Provide a query using ?q=",
                    },
                    status=400,
                )
                return
            if not ranked or ranked[0][0] < 0.55:
                suggestions = [
                    {**item, "confidence": round(score, 3)}
                    for score, item in ranked
                ]
                self._send_json(
                    {
                        "status": "no_match",
                        "query": query,
                        "matches": suggestions,
                    }
                )
                return
            top_score = ranked[0][0]
            close_matches = [
                {**item, "confidence": round(score, 3)}
                for score, item in ranked
                if top_score - score <= 0.03
            ]
            if len(close_matches) > 1 and top_score < 0.85:
                self._send_json(
                    {
                        "status": "multiple_matches",
                        "query": query,
                        "matches": close_matches,
                    }
                )
                return
            best = {**ranked[0][1], "confidence": round(top_score, 3)}
            self._send_json(
                {
                    "status": "match",
                    "query": query,
                    "match": best,
                }
            )
            return
        self._send_json({"status": "not_found", "path": parsed.path}, status=404)


def run_server(host="0.0.0.0", port=8080):
    server = HTTPServer((host, port), BookApiHandler)
    print(f"Book API listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
