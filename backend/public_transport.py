"""Public HTTPS only, with DNS resolution pinned to the validated socket address."""
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from collect_mastodon import CollectionError, MastodonAdapter


class PublicAPIRedirect(HTTPRedirectHandler):
    """Follow API-preserving public HTTPS redirects, never login or website routes."""
    max_repeats = 2
    max_redirections = 4

    def __init__(self, adapter):
        self.adapter = adapter

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        try:
            valid_destination = (new.scheme == 'https' and new.hostname and
                                 re.fullmatch(r'[A-Za-z0-9.-]+', new.hostname) and
                                 '.' in new.hostname and new.port in (None, 443) and
                                 not (new.username or new.password or new.fragment))
        except ValueError:
            valid_destination = False
        if not valid_destination:
            raise CollectionError('The server redirected to an unsupported address. Only public HTTPS Mastodon APIs can be analyzed.')
        # A domain alias may change the host, but not the requested account,
        # pagination cursor, or API resource. No website/login fallback is valid.
        if (old.path.rstrip('/') != new.path.rstrip('/') or
                sorted(parse_qsl(old.query, keep_blank_values=True)) != sorted(parse_qsl(new.query, keep_blank_values=True))):
            raise CollectionError(
                f'{urlsplit(self.adapter.instance).hostname} redirected the Mastodon API request to a different resource. '
                'This server does not provide the public Mastodon API this analyzer requires at that address. '
                'A profile can be visible on Mastodon while hosted by a different ActivityPub service. '
                'No audience rating was calculated; try a profile hosted by a native Mastodon server.')
        origin = 'https://' + new.hostname.lower()
        if origin != self.adapter.instance and old.path.rstrip('/') != '/api/v1/accounts/lookup':
            raise CollectionError('The follower API moved to a different server mid-collection. No complete audience sample could be obtained.')
        if req.get_method() != 'GET' or code not in (301, 302, 303, 307, 308):
            raise CollectionError('Unsupported API redirect.')
        self.adapter.requests.append({'url': req.full_url, 'status': code, 'redirect_to': newurl})
        self.adapter.instance = origin
        # PublicHTTPSConnection checks and pins DNS separately on every hop.
        # Build a fresh request: never forward cookies or authorization headers.
        return Request(newurl, headers={'Accept': 'application/json', 'User-Agent': 'AudienceMetadataHackathon/1.0'})


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if self._tunnel_host or self.port != 443:
            raise CollectionError('Only direct public HTTPS connections are supported.')
        try:
            addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise CollectionError(
                f'Cannot resolve the profile server {self.host} (DNS lookup failed). '
                'A profile displayed on another Mastodon server may be a cached federated copy; '
                'its home server must still be reachable to collect a fresh follower sample. '
                'Check the profile address and your connection, retry later, or use a previously saved sample. '
                'No audience rating was calculated.') from None
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise CollectionError('The profile server must resolve only to public Internet addresses.')
        last_error = None
        for family, kind, protocol, _, address in addresses:
            connection = socket.socket(family, kind, protocol)
            connection.settimeout(self.timeout)
            try:
                connection.connect(address)
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except OSError as exc:
                connection.close()
                last_error = exc
        raise last_error or OSError('No reachable public address.')


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=ssl.create_default_context())


class PublicMastodonAdapter(MastodonAdapter):
    def __init__(self, instance, unused_token=None):
        # Never send an instance-specific CLI token to an arbitrary pasted hostname.
        super().__init__(instance, token=None)
        self.opener = build_opener(ProxyHandler({}), PublicAPIRedirect(self), PublicHTTPSHandler())
