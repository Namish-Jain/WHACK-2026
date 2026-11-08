export function normalizeProfile(value) {
  const text = value.trim();
  if (/^@?[\w.]+@[a-z0-9.-]+\.[a-z0-9.-]+$/i.test(text)) return text.startsWith('@') ? text : '@' + text;
  let url;
  try { url = new URL(text); } catch { throw new Error('Paste a Mastodon profile URL or @username@server handle.'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.search || url.hash)
    throw new Error('Use a canonical HTTPS Mastodon profile URL without query parameters.');
  const path = url.pathname.match(/^\/(?:@|users\/)([\w.]+)(?:@([a-z0-9.-]+))?\/?$/i);
  if (!path) throw new Error('Open a profile, then copy its URL (for example https://mastodon.social/@username).');
  const host = path[2] || url.hostname;
  if (!/^[a-z0-9.-]+\.[a-z0-9.-]+$/i.test(host)) throw new Error('Use a public Mastodon server hostname.');
  return '@' + path[1] + '@' + host.toLowerCase();
}

export function scoreText(value) { return Number.isFinite(value) ? value.toFixed(1) + ' / 100' : 'Unavailable'; }
export function markerPosition(value) { return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) + '%' : null; }
