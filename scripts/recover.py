#!/usr/bin/env python3
import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import html as html_module
from pathlib import Path

from bs4 import BeautifulSoup
from warcio.archiveiterator import ArchiveIterator

ROOT = Path("recovery")
ROOT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
MAX_DIRECT_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_HITS = 3
MAX_CC_INDEXES = 8


def sh(cmd, cwd=None, timeout=900):
    try:
        p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout[-20000:], "stderr": p.stderr[-20000:]}
    except Exception as e:
        return {"cmd": cmd, "error": repr(e)}


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))[:120].strip("_") or "item"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_bytes(url, headers=None, timeout=90, max_bytes=MAX_DIRECT_BYTES):
    req_headers = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        return data, {
            "requested_url": url,
            "final_url": response.geturl(),
            "status": getattr(response, "status", None),
            "headers": dict(response.headers),
            "bytes": len(data),
            "truncated": truncated,
            "sha256": sha256_bytes(data),
        }


def fetch_to_file(url, out, headers=None, timeout=90, max_bytes=MAX_DIRECT_BYTES):
    data, meta = fetch_bytes(url, headers=headers, timeout=timeout, max_bytes=max_bytes)
    out.write_bytes(data)
    meta["path"] = str(out)
    return meta


def content_type_from_headers(headers):
    raw = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-type":
            raw = value
            break
    return raw.split(";", 1)[0].strip().lower()


def extension_for(content_type, url=""):
    mapping = {
        "text/html": ".html", "text/plain": ".txt", "application/json": ".json",
        "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
        "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "application/zip": ".zip", "application/gzip": ".gz",
    }
    if content_type in mapping:
        return mapping[content_type]
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed:
        return guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    return suffix if suffix and len(suffix) <= 8 else ".bin"


def detect_mime(path):
    probe = sh(["file", "-b", "--mime-type", str(path)], timeout=60)
    if probe.get("returncode") == 0:
        return probe.get("stdout", "").strip().splitlines()[-1] if probe.get("stdout", "").strip() else ""
    return ""


def extract_html(path, out_dir, stem):
    text = path.read_bytes().decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    (out_dir / f"{stem}.text.txt").write_text(visible, encoding="utf-8")
    links = []
    for tag, attr in [("a", "href"), ("img", "src"), ("iframe", "src"), ("source", "src"), ("video", "src")]:
        for node in soup.find_all(tag):
            value = node.get(attr)
            if value:
                links.append({"tag": tag, "attr": attr, "value": value})
    metas = []
    for node in soup.find_all("meta"):
        if node.get("content"):
            metas.append({k: node.get(k) for k in ["name", "property", "http-equiv", "content"] if node.get(k)})
    json_dump(out_dir / f"{stem}.links.json", links)
    json_dump(out_dir / f"{stem}.meta.json", metas)
    return {"text_chars": len(visible), "links": len(links), "meta_tags": len(metas)}


def derive_file(path, out_dir, label=None):
    label = safe(label or path.stem)
    mime = detect_mime(path)
    result = {"path": str(path), "bytes": path.stat().st_size, "sha256": hash_file(path), "mime": mime,
              "file": sh(["file", "-b", str(path)], timeout=60)}
    lower = mime.lower()
    if lower in {"text/html", "application/xhtml+xml"}:
        try:
            result["html"] = extract_html(path, out_dir, label)
        except Exception as e:
            result["html_error"] = repr(e)
    elif lower == "application/pdf":
        result["pdfinfo"] = sh(["pdfinfo", str(path)], timeout=120)
        result["pdftotext"] = sh(["pdftotext", "-layout", str(path), str(out_dir / f"{label}.pdf.txt")], timeout=180)
    elif lower.startswith("image/"):
        result["ocr"] = sh(["tesseract", str(path), str(out_dir / f"{label}.ocr"), "-l", "eng"], timeout=300)
    elif lower.startswith("audio/") or lower.startswith("video/"):
        result["ffprobe"] = sh(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=180)
    elif any(x in lower for x in ["zip", "gzip", "x-7z", "rar", "tar"]):
        result["archive_list"] = sh(["7z", "l", "-slt", str(path)], timeout=180)
    return result


def extract_ytdlp_info(url, d):
    """Use yt-dlp only as an extractor; do not let subtitle parsing stop recovery."""
    probe = sh(["yt-dlp", "--skip-download", "--no-playlist", "-J", url], timeout=300)
    if probe.get("returncode") != 0:
        return {"probe": probe}
    try:
        info = json.loads(probe.get("stdout") or "{}")
        json_dump(d / "extractor.info.json", info)
        return {"probe": {"returncode": 0}, "info": info}
    except Exception as e:
        return {"probe": probe, "parse_error": repr(e)}


def normalize_vtt_text(text):
    # Accept broken provider timestamps such as 00:02:21.1000 by truncating
    # fractional seconds to WebVTT's required millisecond precision.
    return re.sub(r"(\d{2}:\d{2}:\d{2}\.)(\d{3})\d+(?=\s*-->|\s*$)", r"\1\2", text, flags=re.M)


def recover_hls_subtitles(info, d, language="en"):
    subs = (info or {}).get("subtitles") or {}
    tracks = subs.get(language) or []
    if not tracks:
        return {"status": "no_track", "language": language}
    # Prefer an HLS subtitle rendition when available.
    track = next((x for x in tracks if "m3u8" in str(x.get("url", "")).lower()
                  or str(x.get("ext", "")).lower() in {"m3u8", "m3u8_native"}), tracks[0])
    playlist_url = track.get("url")
    if not playlist_url:
        return {"status": "no_url", "track": track}
    try:
        playlist_bytes, pmeta = fetch_bytes(playlist_url, timeout=120, max_bytes=4 * 1024 * 1024)
        playlist_text = playlist_bytes.decode("utf-8", errors="replace")
        (d / f"subtitle.{language}.m3u8").write_text(playlist_text, encoding="utf-8")
    except Exception as e:
        return {"status": "playlist_error", "url": playlist_url, "error": repr(e)}

    segment_urls = []
    for raw in playlist_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        segment_urls.append(urllib.parse.urljoin(playlist_url, line))

    pieces = []
    segment_meta = []
    for idx, segment_url in enumerate(segment_urls):
        try:
            data, meta = fetch_bytes(segment_url, timeout=90, max_bytes=2 * 1024 * 1024)
            text = normalize_vtt_text(data.decode("utf-8", errors="replace"))
            seg_path = d / f"subtitle.{language}.segment-{idx:04d}.vtt"
            seg_path.write_text(text, encoding="utf-8")
            pieces.append(text.replace("WEBVTT", "", 1).strip())
            segment_meta.append({"index": idx, "url": segment_url, "bytes": len(data),
                                 "sha256": sha256_bytes(data), "status": meta.get("status")})
        except Exception as e:
            segment_meta.append({"index": idx, "url": segment_url, "error": repr(e)})

    merged = "WEBVTT\n\n" + "\n\n".join(x for x in pieces if x) + "\n"
    merged_path = d / f"subtitle.{language}.full.salvaged.vtt"
    merged_path.write_text(merged, encoding="utf-8")
    json_dump(d / f"subtitle.{language}.segments.json", segment_meta)
    return {
        "status": "complete" if segment_urls and len(pieces) == len(segment_urls) else "partial",
        "language": language,
        "playlist_url": playlist_url,
        "playlist_sha256": sha256_bytes(playlist_bytes),
        "segments_expected": len(segment_urls),
        "segments_recovered": len(pieces),
        "merged_path": str(merged_path),
        "merged_bytes": merged_path.stat().st_size,
        "merged_sha256": hash_file(merged_path),
        "segment_errors": [x for x in segment_meta if x.get("error")],
    }


def salvage_partial_vtt(d):
    """Repair/merge partial WebVTT left by yt-dlp when a provider subtitle fragment is malformed."""
    parts = sorted(p for p in d.glob("media*.vtt*") if p.is_file() and not p.name.endswith(".ytdl"))
    if not parts:
        return None
    chunks = []
    for p in parts:
        text = p.read_text(encoding="utf-8", errors="replace")
        # Some NFL timed-text fragments contain 4-digit millisecond fields (e.g. 00:02:21.1000).
        text = re.sub(r"(\d{2}:\d{2}:\d{2}\.)(\d{3})\d+(?=\s*-->|\s*$)", r"\1\2", text, flags=re.M)
        chunks.append(text.replace("WEBVTT", "", 1).strip())
    merged = "WEBVTT\n\n" + "\n\n".join(x for x in chunks if x) + "\n"
    out = d / "media.salvaged.vtt"
    out.write_text(merged, encoding="utf-8")
    return {"path": str(out), "bytes": out.stat().st_size, "sha256": hash_file(out),
            "parts": [str(p) for p in parts]}


def direct_acquire(item, d):
    urls = ([item["url"]] if item.get("url") else []) + item.get("urls", [])
    out = []
    for idx, url in enumerate(dict.fromkeys(urls)):
        entry = {"url": url}
        try:
            raw = d / f"direct_{idx}.bin"
            meta = fetch_to_file(url, raw)
            ctype = content_type_from_headers(meta.get("headers"))
            final = d / f"direct_{idx}{extension_for(ctype, meta.get('final_url') or url)}"
            if final != raw:
                raw.replace(final)
            meta["path"] = str(final)
            entry["fetch"] = meta
            entry["derive"] = derive_file(final, d, f"direct_{idx}")
        except Exception as e:
            entry["error"] = repr(e)
        out.append(entry)
    if item.get("kind") == "video" and urls:
        extractor = extract_ytdlp_info(urls[0], d)
        hls_subtitles = recover_hls_subtitles(extractor.get("info"), d, "en") if extractor.get("info") else None
        ytdlp = sh(["yt-dlp", "--no-playlist", "--write-info-json", "--write-description", "--write-thumbnail",
                    "--write-subs", "--write-auto-subs", "--sub-langs", "all,-live_chat", "--convert-subs", "vtt",
                    "-o", str(d / "media.%(ext)s"), urls[0]], timeout=1800)
        derived = []
        for media_path in sorted(d.glob("media.*")):
            if media_path.suffix.lower() in {".json", ".vtt", ".description"}:
                continue
            try:
                derived.append(derive_file(media_path, d, "ytdlp_" + safe(media_path.suffix)))
            except Exception as e:
                derived.append({"path": str(media_path), "error": repr(e)})
        salvage = salvage_partial_vtt(d)
        out.append({"extractor": extractor, "hls_subtitles": hls_subtitles,
                    "ytdlp": ytdlp, "derived": derived, "subtitle_salvage": salvage})
    return out


def wayback_query(pattern, d, index):
    params = {"url": pattern, "output": "json", "filter": "statuscode:200", "collapse": "digest",
              "fl": "timestamp,original,statuscode,mimetype,digest,length", "limit": str(MAX_ARCHIVE_HITS)}
    url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params)
    result = {"pattern": pattern, "query_url": url, "hits": []}
    try:
        data, meta = fetch_bytes(url, timeout=120, max_bytes=8 * 1024 * 1024)
        result["query"] = meta
        rows = json.loads(data.decode("utf-8", errors="replace"))
        if rows and isinstance(rows[0], list):
            headers = rows[0]
            for row in rows[1:MAX_ARCHIVE_HITS + 1]:
                hit = dict(zip(headers, row))
                result["hits"].append(hit)
                ts, original = hit.get("timestamp"), hit.get("original")
                if ts and original:
                    try:
                        p = d / f"wayback_{index}_{len(result['hits'])}.bin"
                        smeta = fetch_to_file(f"https://web.archive.org/web/{ts}id_/{original}", p, timeout=120)
                        ctype = content_type_from_headers(smeta.get("headers")) or hit.get("mimetype", "")
                        final = p.with_suffix(extension_for(ctype, original))
                        if final != p:
                            p.replace(final)
                        hit["snapshot"] = {**smeta, "path": str(final)}
                        hit["derive"] = derive_file(final, d, f"wayback_{index}_{len(result['hits'])}")
                    except Exception as e:
                        hit["snapshot_error"] = repr(e)
    except Exception as e:
        result["error"] = repr(e)
    return result


def parse_json_lines(data):
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            if line.strip():
                rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def load_cc_collections(d):
    p = d / "commoncrawl_collinfo.json"
    try:
        meta = fetch_to_file("https://index.commoncrawl.org/collinfo.json", p, timeout=120, max_bytes=8 * 1024 * 1024)
        return json.loads(p.read_text(encoding="utf-8")), meta
    except Exception as e:
        return [], {"error": repr(e)}


def select_cc_indexes(item, collections):
    ids = [x.get("id") for x in collections if x.get("id")]
    explicit = item.get("cc_indexes", [])
    if explicit:
        return explicit[:MAX_CC_INDEXES]
    first = item.get("cc_first_index")
    if not first:
        return ids[:min(3, MAX_CC_INDEXES)]
    if first not in ids:
        return [first]
    pos = ids.index(first)
    followups = max(0, int(item.get("cc_followups", 5)))
    return list(reversed(ids[max(0, pos - followups):pos + 1]))[:MAX_CC_INDEXES]


def extract_warc_payload(warc_path, out_dir, stem, hinted_mime=""):
    result = {"warc": str(warc_path)}
    try:
        with warc_path.open("rb") as stream:
            for record in ArchiveIterator(stream):
                if record.rec_type != "response":
                    continue
                payload = record.content_stream().read()
                http_headers = dict(record.http_headers.headers) if record.http_headers else {}
                ctype = next((v.split(";", 1)[0].strip().lower() for k, v in http_headers.items()
                              if k.lower() == "content-type"), "") or hinted_mime
                target = record.rec_headers.get_header("WARC-Target-URI") or ""
                payload_path = out_dir / f"{stem}.payload{extension_for(ctype, target)}"
                payload_path.write_bytes(payload)
                result.update({"target_uri": target, "warc_date": record.rec_headers.get_header("WARC-Date"),
                               "content_type": ctype, "payload_path": str(payload_path), "payload_bytes": len(payload),
                               "payload_sha256": sha256_bytes(payload), "http_headers": http_headers,
                               "derive": derive_file(payload_path, out_dir, stem + "_payload")})
                return result
        result["error"] = "no response record in fetched WARC member"
    except Exception as e:
        result["error"] = repr(e)
    return result


def commoncrawl_query(index_id, pattern, d, pattern_index):
    params = {"url": pattern, "output": "json", "filter": "status:200", "collapse": "digest",
              "limit": str(MAX_ARCHIVE_HITS)}
    url = f"https://index.commoncrawl.org/{index_id}-index?" + urllib.parse.urlencode(params)
    result = {"index": index_id, "pattern": pattern, "query_url": url, "hits": []}
    try:
        data, meta = fetch_bytes(url, timeout=120, max_bytes=8 * 1024 * 1024)
        result["query"] = meta
        for hit_index, hit in enumerate(parse_json_lines(data)[:MAX_ARCHIVE_HITS], start=1):
            saved = dict(hit)
            # Wildcard archive indexes can occasionally return a host/root record that does not
            # contain the requested distinctive token. Keep it as rejected metadata, never evidence.
            distinctive = [x for x in re.findall(r"[A-Za-z0-9_-]{8,}", pattern) if x.lower() not in {"https", "http"}]
            candidate_url = str(hit.get("url") or "")
            if distinctive and not any(tok.lower() in candidate_url.lower() for tok in distinctive):
                saved["rejected"] = "archive hit does not contain distinctive query token"
                result["hits"].append(saved)
                continue
            filename, offset, length = hit.get("filename"), hit.get("offset"), hit.get("length")
            if filename and offset is not None and length is not None:
                try:
                    start, length_i = int(offset), int(length)
                    warc_path = d / f"cc_{safe(index_id)}_{pattern_index}_{hit_index}.warc.gz"
                    wmeta = fetch_to_file("https://data.commoncrawl.org/" + filename.lstrip("/"), warc_path,
                                          headers={"Range": f"bytes={start}-{start + length_i - 1}"}, timeout=180,
                                          max_bytes=max(length_i + 1024, 2 * 1024 * 1024))
                    saved["warc_fetch"] = wmeta
                    saved["payload"] = extract_warc_payload(warc_path, d, f"cc_{safe(index_id)}_{pattern_index}_{hit_index}",
                                                            hit.get("mime-detected") or hit.get("mime") or "")
                except Exception as e:
                    saved["warc_error"] = repr(e)
            result["hits"].append(saved)
    except Exception as e:
        result["error"] = repr(e)
    return result


def archive_queries(item):
    values = item.get("archive_queries", []) + item.get("urls", [])
    if item.get("url"):
        values.append(item["url"])
    if item.get("target") and not values:
        values.append(item["target"])
    return list(dict.fromkeys(v for v in values if v))


def archive_probe(item, d):
    queries = archive_queries(item)
    result = {"queries": queries, "wayback": [], "commoncrawl": [], "commoncrawl_collection_meta": {}}
    for i, pattern in enumerate(queries):
        result["wayback"].append(wayback_query(pattern, d, i))
    collections, collection_meta = load_cc_collections(d)
    result["commoncrawl_collection_meta"] = collection_meta
    indexes = select_cc_indexes(item, collections)
    result["commoncrawl_indexes"] = indexes
    for i, pattern in enumerate(queries):
        for index_id in indexes:
            probe = commoncrawl_query(index_id, pattern, d, i)
            result["commoncrawl"].append(probe)
            usable = any(not hit.get("rejected") and not hit.get("payload", {}).get("error")
                         and hit.get("payload", {}).get("payload_bytes", 0) > 0
                         for hit in probe.get("hits", []))
            if usable:
                break
    return result


def collect_payload_files(d):
    payloads = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(("result.json", ".links.json", ".meta.json", ".txt", ".vtt", ".description", ".info.json")):
            continue
        if name.startswith("commoncrawl_collinfo") or name.endswith(".warc.gz"):
            continue
        payloads.append({"path": str(p), "bytes": p.stat().st_size, "sha256": hash_file(p), "mime": detect_mime(p)})
    return payloads


def outcome_for(result, d):
    payloads = collect_payload_files(d)
    result["payload_files"] = payloads
    if any(p["bytes"] > 0 for p in payloads):
        return "recovered_payload"
    archive = result.get("archive", {})
    cc_hits = sum(len(x.get("hits", [])) for x in archive.get("commoncrawl", []))
    wb_hits = sum(len(x.get("hits", [])) for x in archive.get("wayback", []))
    if cc_hits or wb_hits:
        return "metadata_only"
    errors = [row["error"] for row in result.get("direct", []) if row.get("error")]
    return "blocked_or_error" if any("HTTP Error 404" not in e for e in errors) else "not_found"


def compact_archive_evidence(result):
    evidence = []
    archive = result.get("archive", {})
    for probe in archive.get("commoncrawl", []):
        for hit in probe.get("hits", []):
            if hit.get("rejected"):
                continue
            payload = hit.get("payload", {})
            evidence.append({
                "archive": "commoncrawl",
                "index": probe.get("index"),
                "pattern": probe.get("pattern"),
                "url": hit.get("url"),
                "timestamp": hit.get("timestamp"),
                "status": hit.get("status"),
                "mime": hit.get("mime") or hit.get("mime-detected"),
                "digest": hit.get("digest"),
                "filename": hit.get("filename"),
                "offset": hit.get("offset"),
                "length": hit.get("length"),
                "payload_target_uri": payload.get("target_uri"),
                "payload_content_type": payload.get("content_type"),
                "payload_bytes": payload.get("payload_bytes"),
                "payload_sha256": payload.get("payload_sha256"),
            })
    for probe in archive.get("wayback", []):
        for hit in probe.get("hits", []):
            snapshot = hit.get("snapshot", {})
            evidence.append({
                "archive": "wayback",
                "pattern": probe.get("pattern"),
                "url": hit.get("original"),
                "timestamp": hit.get("timestamp"),
                "status": hit.get("statuscode"),
                "mime": hit.get("mimetype"),
                "digest": hit.get("digest"),
                "length": hit.get("length"),
                "snapshot_url": snapshot.get("final_url"),
                "payload_bytes": snapshot.get("bytes"),
                "payload_sha256": snapshot.get("sha256"),
            })
    return evidence[:40]


def write_report(rows):
    totals = {}
    for row in rows:
        totals[row["outcome"]] = totals.get(row["outcome"], 0) + 1
    json_dump(ROOT / "summary.json", {"totals": totals, "items": rows})
    handoff_items = []
    for row in rows:
        result_path = ROOT / safe(row["id"]) / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        handoff_items.append({
            "id": row["id"], "priority": row.get("priority"), "outcome": row["outcome"],
            "notion": row.get("notion"), "archive_hits": row.get("archive_hits", 0),
            "recovered_files": row.get("key_files", []), "errors": row.get("errors", []),
            "archive_evidence": compact_archive_evidence(result),
            "payload_files": result.get("payload_files", [])[:20],
        })
    json_dump(ROOT / "notion_handoff.json", {
        "generated_by": "Sadie Source Recovery",
        "items": handoff_items,
    })
    lines = ["# Sadie Source Recovery report", "", "| Item | Priority | Outcome | Payload files | Archive hits |",
             "|---|---:|---|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['id']} | {row.get('priority','')} | {row['outcome']} | {row.get('payload_count',0)} | {row.get('archive_hits',0)} |")
    lines += ["", "## Detail", ""]
    for row in rows:
        lines.append(f"### {row['id']} — {row['outcome']}")
        if row.get("notion"):
            lines.append(f"Notion: {row['notion']}")
        if row.get("key_files"):
            lines.append("Recovered files:")
            lines += [f"- `{p}`" for p in row["key_files"][:10]]
        if row.get("errors"):
            lines.append("Errors/boundaries:")
            lines += [f"- `{e[:300]}`" for e in row["errors"][:5]]
        lines.append("")
    (ROOT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    queue = json.load(open("queue/recovery_queue.json", encoding="utf-8"))
    rows = []
    for item in queue["items"]:
        d = ROOT / safe(item["id"])
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        result = {"item": item}
        try:
            if item.get("url") or item.get("urls"):
                result["direct"] = direct_acquire(item, d)
            if item.get("archive_queries") or item.get("cc_first_index") or item.get("cc_indexes") or item.get("kind", "").startswith("archive"):
                result["archive"] = archive_probe(item, d)
        except Exception as e:
            result["fatal_error"] = repr(e)
        outcome = outcome_for(result, d)
        result["outcome"] = outcome
        json_dump(d / "result.json", result)
        archive = result.get("archive", {})
        archive_hits = sum(len(x.get("hits", [])) for x in archive.get("commoncrawl", [])) + sum(len(x.get("hits", [])) for x in archive.get("wayback", []))
        errors = [x["error"] for x in result.get("direct", []) if x.get("error")]
        if result.get("fatal_error"):
            errors.append(result["fatal_error"])
        payloads = result.get("payload_files", [])
        rows.append({"id": item["id"], "priority": item.get("priority"), "outcome": outcome,
                     "payload_count": len(payloads), "archive_hits": archive_hits,
                     "key_files": [p["path"] for p in payloads], "notion": item.get("notion"), "errors": errors})
    write_report(rows)


if __name__ == "__main__":
    main()
