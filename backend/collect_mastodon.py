#!/usr/bin/env python3
"""Collect public Mastodon follower metadata; Python 3.10+, no dependencies."""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CollectionError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectionError("Server redirected the request; use the canonical instance URL.")


class BioText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append(" ")


def plain_bio(value):
    if not isinstance(value, str):
        return None
    parser = BioText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def parse_target(value):
    value = value.strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        match = re.fullmatch(r"/(?:@|users/)([A-Za-z0-9_.]+)/*", parsed.path)
        if not match or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CollectionError("Use a canonical https://server/@username profile URL.")
        host, username = parsed.netloc, match[1]
    else:
        match = re.fullmatch(r"@?([A-Za-z0-9_.]+)@([A-Za-z0-9.-]+)", value)
        if not match:
            raise CollectionError("Supply @username@server or https://server/@username.")
        username, host = match.groups()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or "." not in host:
        raise CollectionError("Expected a public Mastodon server hostname.")
    return "https://" + host.lower(), username


def nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def normalize(account, instance, collected_at):
    bio = plain_bio(account.get("note"))
    acct = account.get("acct") or account.get("username")
    full_handle = acct if not acct or "@" in acct else acct + "@" + urlsplit(instance).hostname
    avatar = account.get("avatar")
    # The API has no definitive custom-avatar boolean. Preserve uncertainty.
    picture = None
    if isinstance(avatar, str) and avatar:
        if re.search(r"/avatars/(?:original/)?missing\.", urlsplit(avatar).path):
            picture = False
        elif "/accounts/avatars/" in urlsplit(avatar).path:
            picture = True
    row = {key: account.get(key) for key in (
        "id", "acct", "display_name", "created_at", "last_status_at", "note",
        "avatar", "avatar_static", "url", "uri", "fields", "bot", "hide_collections"
    )}
    row.update({
        "username": full_handle,
        "local_username": account.get("username"),
        "followers_count": nonnegative_int(account.get("followers_count")),
        "following_count": nonnegative_int(account.get("following_count")),
        "posts_count": nonnegative_int(account.get("statuses_count")),
        "statuses_count": nonnegative_int(account.get("statuses_count")),
        "is_bot": account.get("bot") if type(account.get("bot")) is bool else None,
        "bio": bio,
        "bio_length": len(bio) if bio is not None else None,
        "has_profile_picture": picture,
        "profile_picture_method": "URL heuristic; null means unknown",
        "source_instance": instance,
        "collected_at": collected_at,
    })
    return row


def next_page(header, current_url):
    for link, parameters in re.findall(r"<([^>]+)>([^,]*)", header or ""):
        relation = re.search(r'\brel\s*=\s*(?:"([^"]+)"|([^;\s]+))', parameters)
        if relation and "next" in (relation[1] or relation[2]).split():
            return urljoin(current_url, link)
    return None


class MastodonAdapter:
    def __init__(self, instance, token=None):
        self.instance = instance.rstrip("/")
        if urlsplit(self.instance).hostname == "bsky.brid.gy":
            raise CollectionError(
                "This is a bridged Bluesky account. This collector supports Mastodon server APIs, "
                "not the bsky.brid.gy bridge. Use a native Mastodon handle (for example "
                "@bagder@mastodon.social). Collecting the original Bluesky audience requires a Bluesky adapter."
            )
        self.token = token
        self.opener = build_opener(NoRedirect())
        self.requests = []

    def get(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != urlsplit(self.instance).netloc:
            raise CollectionError("Refusing a cross-instance pagination URL.")
        headers = {"Accept": "application/json", "User-Agent": "AudienceMetadataHackathon/1.0"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with self.opener.open(Request(url, headers=headers), timeout=30) as response:
                payload = json.load(response)
                response_headers = response.headers
                self.requests.append({"url": url, "status": response.status,
                                      "rate_limit_remaining": response_headers.get("X-RateLimit-Remaining"),
                                      "rate_limit_reset": response_headers.get("X-RateLimit-Reset")})
                return payload, response_headers
        except HTTPError as exc:
            self.requests.append({"url": url, "status": exc.code})
            if exc.code in (401, 403):
                raise CollectionError("Access denied. The server may require a token from this instance; set MASTODON_ACCESS_TOKEN locally. Hidden networks remain inaccessible.") from exc
            if exc.code == 429:
                reset = exc.headers.get("X-RateLimit-Reset", "not supplied")
                raise CollectionError("Rate limited; partial data saved. Retry after reset: " + reset) from exc
            raise CollectionError(f"API returned HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise CollectionError(f"Could not read API response: {exc}") from exc

    def get_account(self, username):
        account, _ = self.get(self.instance + "/api/v1/accounts/lookup?" + urlencode({"acct": username}))
        if not isinstance(account, dict) or not account.get("id"):
            raise CollectionError("Lookup did not return an account ID.")
        return account

    def collect(self, username, limit):
        account = self.get_account(username)
        followers, seen, visited = [], set(), set()
        stop_reason, error = "limit_reached", None
        if account.get("hide_collections") is True:
            return account, followers, "hidden_network", None
        path = "/api/v1/accounts/" + quote(str(account["id"]), safe="") + "/followers"
        url = self.instance + path + "?" + urlencode({"limit": min(limit, 80)})
        try:
            while url and len(followers) < limit:
                if url in visited:
                    raise CollectionError("Pagination repeated a page URL.")
                visited.add(url)
                page, headers = self.get(url)
                if not isinstance(page, list):
                    raise CollectionError("Followers response is not a JSON array.")
                before = len(followers)
                for follower in page:
                    if not isinstance(follower, dict) or not follower.get("id"):
                        raise CollectionError("Follower record is missing an account ID.")
                    key = str(follower["id"])
                    if key not in seen:
                        seen.add(key)
                        followers.append(follower)
                    if len(followers) >= limit:
                        break
                url = next_page(headers.get("Link"), url)
                if not page or not url:
                    stop_reason = "available_pages_exhausted"
                    break
                if len(followers) == before:
                    raise CollectionError("Page contained no new follower records.")
                if len(followers) < limit:
                    time.sleep(0.5)
        except CollectionError as exc:
            stop_reason, error = "partial_error", str(exc)
        return account, followers, stop_reason, error


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export(out, account, raw, metadata):
    out.mkdir(parents=True, exist_ok=False)
    rows = [normalize(a, metadata["instance"], metadata["collected_at"]) for a in raw]
    save_json(out / "target_account.json", account)
    save_json(out / "followers_raw.json", raw)
    save_json(out / "followers_normalized.json", rows)
    metadata["missing_values"] = {k: sum(r.get(k) is None for r in rows) for k in
        ("followers_count", "following_count", "posts_count", "created_at", "last_status_at", "is_bot", "has_profile_picture", "bio_length")}
    save_json(out / "collection_metadata.json", metadata)
    fields = list(normalize({}, metadata["instance"], metadata["collected_at"]))
    with (out / "demo_followers.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict, bool)) else value)
                             for key, value in row.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", help="@username@server or canonical HTTPS profile URL")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--out", type=Path, help="New directory for collected files")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        instance, username = parse_target(args.account)
        now = datetime.now(timezone.utc)
        out = args.out or Path(__file__).resolve().parent / ("mastodon_data_" + now.strftime("%Y%m%dT%H%M%S%fZ"))
        if out.exists():
            raise CollectionError("Output directory already exists; choose a new --out to preserve previous data.")
        adapter = MastodonAdapter(instance, os.getenv("MASTODON_ACCESS_TOKEN"))
        account, raw, reason, error = adapter.collect(username, args.limit)
        metadata = {"target_input": args.account, "instance": instance,
                    "collected_at": now.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                    "requested_sample_size": args.limit, "sample_size": len(raw),
                    "reported_followers_count": account.get("followers_count"),
                    "stop_reason": reason, "error": error, "requests": adapter.requests,
                    "sampling_method": "First unique accessible followers in API order; not random.",
                    "limitations": ["Server-visible records may be cached or incomplete for remote accounts.",
                                    "A short or empty response does not establish that the account has no followers.",
                                    "Bot flag is declared automation, not evidence of fake followers.",
                                    "Custom-avatar presence is a URL heuristic with unknown values preserved."]}
        export(out, account, raw, metadata)
        print(json.dumps({"output_directory": str(out.resolve()), "followers_collected": len(raw), "stop_reason": reason, "error": error}, indent=2))
        return 2 if error or reason == "hidden_network" else 0
    except (CollectionError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
