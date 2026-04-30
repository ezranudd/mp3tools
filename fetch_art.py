"""
Online album art fetching.

Public API
  search_art_sources(artist, album, settings, interactive=False) -> list[dict]
  search_itunes(artist, album, limit=5)                          -> list[dict]
  fetch_artwork(url)                                             -> (bytes, mime)
  resize_artwork(data, mime, max_size)                           -> (bytes, mime)
"""
import json
import re
import time
import urllib.parse
import urllib.request

CONFIDENT_MATCH_SCORE = 140
SOURCE_ORDER = ("itunes", "musicbrainz", "theaudiodb", "discogs")
AUTO_SOURCES = ("itunes", "musicbrainz", "theaudiodb")
SOURCE_LABELS = {
    "itunes": "iTunes",
    "musicbrainz": "MusicBrainz",
    "theaudiodb": "TheAudioDB",
    "discogs": "Discogs",
}

_LAST_CALL: dict[str, float] = {}
_MIN_INTERVALS = {
    "itunes": 0.5,
    "musicbrainz": 1.05,
    "coverartarchive": 0.25,
    "theaudiodb": 2.0,
    "discogs": 1.0,
}
_USER_AGENT = "mp3tools/1.0 (local library art fetcher)"
_ARTWORK_SIZE_RE = re.compile(r"/(\d+)x(\d+)bb\.(jpg|jpeg|png)(?:\?|$)", re.I)


def enabled_sources(settings: dict | None,
                    interactive: bool = False) -> list[str]:
    source_flags = settings.get("art_sources", {}) if isinstance(settings, dict) else {}
    if not isinstance(source_flags, dict):
        source_flags = {}

    order = settings.get("art_source_order", SOURCE_ORDER) if isinstance(settings, dict) else SOURCE_ORDER
    if not isinstance(order, list) or not order:
        order = list(SOURCE_ORDER)

    result: list[str] = []
    for source in order:
        if source not in SOURCE_ORDER:
            continue
        if source == "discogs" and not interactive:
            continue
        if source not in AUTO_SOURCES and source != "discogs":
            continue
        if source_flags.get(source, source == "itunes"):
            result.append(source)
    return result


def _rate_limit(provider: str) -> None:
    interval = _MIN_INTERVALS.get(provider, 0.5)
    elapsed = time.monotonic() - _LAST_CALL.get(provider, 0.0)
    if elapsed < interval:
        time.sleep(interval - elapsed)


def _request_bytes(url: str, provider: str, timeout: int = 12,
                   headers: dict | None = None) -> tuple[bytes, str]:
    _rate_limit(provider)
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            mime = resp.headers.get_content_type() or "application/octet-stream"
    except Exception as e:
        _LAST_CALL[provider] = time.monotonic()
        raise RuntimeError(f"{SOURCE_LABELS.get(provider, provider)} request failed: {e}") from e
    _LAST_CALL[provider] = time.monotonic()
    return data, mime


def _request_json(url: str, provider: str, timeout: int = 12,
                  headers: dict | None = None) -> dict:
    data, _ = _request_bytes(url, provider, timeout=timeout, headers=headers)
    try:
        return json.loads(data)
    except Exception as e:
        raise RuntimeError(f"{SOURCE_LABELS.get(provider, provider)} parse error: {e}") from e


def _full_size_artwork_url(url: str) -> tuple[str, str]:
    full_url = re.sub(
        r"/\d+x\d+bb\.(jpg|jpeg|png)(?=\?|$)",
        "/3000x3000bb.\\1",
        url,
        count=1,
        flags=re.I,
    )
    m = _ARTWORK_SIZE_RE.search(full_url)
    size = f"{m.group(1)}x{m.group(2)}" if m else ""
    return full_url, size


def _norm(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _overlap_score(wanted: str, candidate: str, weight: int) -> int:
    wanted_tokens = set(wanted.split())
    candidate_tokens = set(candidate.split())
    if not wanted_tokens or not candidate_tokens:
        return 0
    return int(weight * (len(wanted_tokens & candidate_tokens) / len(wanted_tokens)))


def _match_score(wanted_artist: str, wanted_album: str,
                 found_artist: str, found_album: str) -> int:
    artist = _norm(wanted_artist)
    album  = _norm(wanted_album)
    found_artist_n = _norm(found_artist)
    found_album_n  = _norm(found_album)

    score = 0
    if artist and found_artist_n == artist:
        score += 100
    elif artist and (artist in found_artist_n or found_artist_n in artist):
        score += 60
    else:
        score += _overlap_score(artist, found_artist_n, 30)

    if album and found_album_n == album:
        score += 120
    elif album and found_album_n.startswith(album + " "):
        score += 90
    elif album and album in found_album_n:
        score += 70
    elif album and found_album_n in album:
        score += 50
    else:
        score += _overlap_score(album, found_album_n, 40)

    if album and "tribute" not in album and "tribute" in found_album_n:
        score -= 40
    if album and "karaoke" not in album and "karaoke" in found_album_n:
        score -= 40
    if album and "solo violin" not in album and "solo violin" in found_album_n:
        score -= 30

    return score


def _result(source: str, artist: str, album: str, year: str,
            url: str, score: int, size: str = "",
            detail_url: str = "") -> dict:
    return {
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "artist": artist,
        "album": album,
        "year": year,
        "size": size,
        "url": url,
        "score": score,
        "detail_url": detail_url,
    }


def search_itunes(artist: str, album: str, limit: int = 5) -> list[dict]:
    """Query iTunes Search API and return normalized art results."""
    term = f"{artist} {album}".strip()
    params = urllib.parse.urlencode({"term": term, "entity": "album", "limit": limit})
    url = f"https://itunes.apple.com/search?{params}"
    data = _request_json(url, "itunes", timeout=10)

    results = []
    for item in data.get("results", []):
        art_url = item.get("artworkUrl100", "")
        if art_url:
            art_url, size = _full_size_artwork_url(art_url)
        else:
            size = ""
        artist_name = item.get("artistName", "")
        album_name = item.get("collectionName", "")
        results.append(_result(
            "itunes",
            artist_name,
            album_name,
            str(item.get("releaseDate", ""))[:4],
            art_url,
            _match_score(artist, album, artist_name, album_name),
            size=size,
            detail_url=item.get("collectionViewUrl", ""),
        ))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def _musicbrainz_artist_credit(item: dict) -> str:
    parts = []
    for credit in item.get("artist-credit", []):
        if isinstance(credit, dict):
            name = credit.get("name")
            if name:
                parts.append(name)
    return " ".join(parts)


def search_musicbrainz(artist: str, album: str, limit: int = 5) -> list[dict]:
    """Search MusicBrainz release-groups and resolve front art from Cover Art Archive."""
    safe_artist = artist.replace('"', "")
    safe_album = album.replace('"', "")
    query = f'artist:"{safe_artist}" AND releasegroup:"{safe_album}"'
    params = urllib.parse.urlencode({
        "query": query,
        "fmt": "json",
        "limit": min(max(limit, 1), 10),
    })
    url = f"https://musicbrainz.org/ws/2/release-group/?{params}"
    data = _request_json(url, "musicbrainz", timeout=12)

    results = []
    for item in data.get("release-groups", []):
        mbid = item.get("id", "")
        if not mbid:
            continue
        title = item.get("title", "")
        found_artist = _musicbrainz_artist_credit(item)
        score = _match_score(artist, album, found_artist, title)
        art_url = ""
        size = ""
        try:
            art_data = _request_json(
                f"https://coverartarchive.org/release-group/{mbid}/",
                "coverartarchive",
                timeout=10,
            )
        except RuntimeError:
            continue
        for image in art_data.get("images", []):
            if not image.get("front"):
                continue
            thumbs = image.get("thumbnails", {})
            art_url = thumbs.get("1200") or thumbs.get("large") or image.get("image", "")
            if thumbs.get("1200"):
                size = "1200"
            elif thumbs.get("large"):
                size = "500"
            break
        if not art_url:
            continue
        results.append(_result(
            "musicbrainz",
            found_artist,
            title,
            str(item.get("first-release-date", ""))[:4],
            art_url,
            score,
            size=size,
            detail_url=f"https://musicbrainz.org/release-group/{mbid}",
        ))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def search_theaudiodb(artist: str, album: str, api_key: str = "",
                      limit: int = 5) -> list[dict]:
    """Search TheAudioDB album endpoint. Requires an API key."""
    if not api_key:
        return []
    params = urllib.parse.urlencode({"s": artist, "a": album})
    url = f"https://www.theaudiodb.com/api/v1/json/{urllib.parse.quote(api_key)}/searchalbum.php?{params}"
    data = _request_json(url, "theaudiodb", timeout=12)

    results = []
    for item in (data.get("album") or [])[:limit]:
        art_url = item.get("strAlbumThumbHQ") or item.get("strAlbumThumb") or ""
        if not art_url:
            continue
        artist_name = item.get("strArtist", "")
        album_name = item.get("strAlbum", "")
        results.append(_result(
            "theaudiodb",
            artist_name,
            album_name,
            str(item.get("intYearReleased", ""))[:4],
            art_url,
            _match_score(artist, album, artist_name, album_name),
            detail_url=f"https://www.theaudiodb.com/album/{item.get('idAlbum', '')}",
        ))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def search_discogs(artist: str, album: str, token: str = "",
                   limit: int = 5) -> list[dict]:
    """Search Discogs releases. Intended for interactive candidate selection only."""
    params = urllib.parse.urlencode({
        "artist": artist,
        "release_title": album,
        "type": "release",
        "per_page": min(max(limit, 1), 10),
    })
    url = f"https://api.discogs.com/database/search?{params}"
    headers = {}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    data = _request_json(url, "discogs", timeout=12, headers=headers)

    results = []
    for item in data.get("results", []):
        art_url = item.get("cover_image", "")
        if not art_url:
            continue
        title = item.get("title", "")
        if " - " in title:
            found_artist, album_name = title.split(" - ", 1)
        else:
            found_artist, album_name = artist, title
        results.append(_result(
            "discogs",
            found_artist,
            album_name,
            str(item.get("year", "")),
            art_url,
            _match_score(artist, album, found_artist, album_name),
            detail_url=f"https://www.discogs.com{item.get('uri', '')}",
        ))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def search_source(source: str, artist: str, album: str,
                  settings: dict | None = None, limit: int = 5) -> list[dict]:
    settings = settings or {}
    if source == "itunes":
        return search_itunes(artist, album, limit=limit)
    if source == "musicbrainz":
        return search_musicbrainz(artist, album, limit=limit)
    if source == "theaudiodb":
        return search_theaudiodb(
            artist, album,
            api_key=str(settings.get("theaudiodb_api_key", "") or ""),
            limit=limit,
        )
    if source == "discogs":
        return search_discogs(
            artist, album,
            token=str(settings.get("discogs_token", "") or ""),
            limit=limit,
        )
    return []


def search_art_sources(artist: str, album: str, settings: dict | None = None,
                       interactive: bool = False, limit: int = 5) -> list[dict]:
    """Search enabled sources in configured order and return normalized results."""
    results: list[dict] = []
    errors: list[str] = []
    for source in enabled_sources(settings, interactive=interactive):
        try:
            source_results = search_source(source, artist, album, settings, limit=limit)
        except RuntimeError as e:
            errors.append(str(e))
            continue
        results.extend(source_results)
        if not interactive and any(r.get("score", 0) >= CONFIDENT_MATCH_SCORE for r in source_results):
            break
    results.sort(key=lambda r: (r.get("score", 0), -SOURCE_ORDER.index(r["source"])), reverse=True)
    if not results and errors:
        raise RuntimeError("; ".join(errors))
    return results


def fetch_artwork(url: str) -> tuple[bytes, str]:
    """Download image from *url*. Returns (bytes, mime). Raises RuntimeError on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            mime = resp.headers.get_content_type() or "image/jpeg"
    except Exception as e:
        raise RuntimeError(f"Fetch failed: {e}") from e
    return data, mime


def resize_artwork(data: bytes, mime: str, max_size: int) -> tuple[bytes, str]:
    """Resize image to at most max_size x max_size. Falls back to original if Pillow unavailable."""
    if max_size <= 0:
        return data, mime
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        if max(img.width, img.height) > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            buf = BytesIO()
            if "jpeg" in mime or "jpg" in mime:
                img = img.convert("RGB")
                img.save(buf, "JPEG", quality=88)
            else:
                img.save(buf, "PNG")
            return buf.getvalue(), mime
    except ImportError:
        pass
    return data, mime
