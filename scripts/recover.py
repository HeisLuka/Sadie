#!/usr/bin/env python3
import json, re, hashlib, subprocess, urllib.request, urllib.parse
from pathlib import Path

ROOT=Path("recovery")
ROOT.mkdir(exist_ok=True)
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

def sh(cmd, timeout=900):
    p=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
    return {"cmd":cmd,"returncode":p.returncode,"stdout":p.stdout[-30000:],"stderr":p.stderr[-30000:]}

def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",s)[:100]

def fetch(url,out,timeout=60):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"*/*"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        data=r.read()
        out.write_bytes(data)
        return {"final_url":r.geturl(),"status":getattr(r,"status",None),"headers":dict(r.headers),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}

def wayback_exact(url,d,prefix):
    api="https://web.archive.org/cdx/search/cdx?"+urllib.parse.urlencode({
      "url":url,"output":"json","filter":"statuscode:200","filter":"mimetype:.*","collapse":"digest","fl":"timestamp,original,statuscode,mimetype,digest,length","limit":"100"
    })
    meta={"cdx_url":api}
    try:
        cdx=d/(prefix+"_cdx.json")
        meta["cdx"]=fetch(api,cdx,timeout=120)
        rows=json.loads(cdx.read_text(encoding="utf-8",errors="replace"))
        if len(rows)>1:
            ts=rows[-1][0]
            replay=f"https://web.archive.org/web/{ts}id_/{url}"
            out=d/(prefix+"_wayback.bin")
            meta["capture_timestamp"]=ts
            meta["replay"]=fetch(replay,out,timeout=180)
    except Exception as e:
        meta["error"]=repr(e)
    return meta

def cc_exact(url,d,prefix,indexes):
    results=[]
    for index in indexes:
        api=f"https://index.commoncrawl.org/{index}-index?"+urllib.parse.urlencode({"url":url,"output":"json","filter":"status:200"})
        ent={"index":index,"query_url":api}
        try:
            p=d/f"{prefix}_{index}.jsonl"
            ent["query"]=fetch(api,p,timeout=120)
            lines=[x for x in p.read_text(encoding="utf-8",errors="replace").splitlines() if x.strip()]
            ent["hits"]=len(lines)
            if lines:
                hit=json.loads(lines[0])
                ent["first_hit"]=hit
        except Exception as e:
            ent["error"]=repr(e)
        results.append(ent)
    return results

def archive_fallback(url,d):
    parsed=urllib.parse.urlparse(url)
    year_hint="2015" if any(x in url for x in ["waywire","theplatform","broadwayworld"]) else "2013"
    indexes=["CC-MAIN-2013-48"] if year_hint=="2013" else ["CC-MAIN-2015-18","CC-MAIN-2015-14"]
    return {
      "wayback":wayback_exact(url,d,"exact"),
      "commoncrawl":cc_exact(url,d,"exact_cc",indexes)
    }


def candidate_variants(url):
    out=[url]
    if url.startswith("http://"): out.append("https://"+url[len("http://"):])
    elif url.startswith("https://"): out.append("http://"+url[len("https://"):])
    if "?" in url: out.append(url.split("?",1)[0])
    if "traffic.libsyn.com" in url:
        base=url.split("?",1)[0]
        out += [
          base.replace("traffic.libsyn.com","hwcdn.libsyn.com"),
          base.replace("traffic.libsyn.com","media.libsyn.com"),
          base.replace("traffic.libsyn.com","traffic.libsyn.com/secure"),
        ]
    return list(dict.fromkeys(out))

def probe_variants(url,d):
    results=[]
    for i,u in enumerate(candidate_variants(url),1):
        ent={"url":u}
        try:
            p=d/f"variant_{i:02d}.bin"
            ent["live"]=fetch(u,p,timeout=60)
        except Exception as e:
            ent["live_error"]=repr(e)
        ent["wayback"]=wayback_exact(u,d,f"variant_{i:02d}")
        results.append(ent)
    return results

def mine_text_urls(path):
    try:
        txt=path.read_text(encoding="utf-8",errors="replace")
    except Exception:
        return []
    urls=re.findall(r'https?://[^"\'<>\s]+',txt)
    return sorted(set(urls))
def youtube_transcript(url,d):
    m=re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",url)
    if not m: return {"skipped":"not youtube"}
    vid=m.group(1)
    code=("from youtube_transcript_api import YouTubeTranscriptApi\n"
          f"vid={vid!r}\n"
          "api=YouTubeTranscriptApi()\n"
          "items=api.fetch(vid)\n"
          "import json\n"
          "print(json.dumps([{'text':x.text,'start':x.start,'duration':x.duration} for x in items],ensure_ascii=False))\n")
    r=sh(["python","-c",code],timeout=180)
    if r["returncode"]==0:
        (d/"youtube_transcript.json").write_text(r["stdout"],encoding="utf-8")
    return r

def extract_vtt_fragments(url,d):
    out={"metadata":None,"playlist":None,"fragments":[]}
    meta_run=sh(["yt-dlp","--skip-download","--no-playlist","--write-info-json",
                 "-o",str(d/"media.%(ext)s"),url],timeout=300)
    out["metadata"]=meta_run
    info_files=sorted(d.glob("media*.info.json"))
    if meta_run["returncode"]!=0 or not info_files:
        return out
    try:
        info=json.loads(info_files[0].read_text(encoding="utf-8",errors="replace"))
    except Exception as e:
        out["parse_error"]=repr(e); return out
    subs=info.get("subtitles") or {}
    tracks=subs.get("en") or []
    track=next((x for x in tracks if x.get("ext")=="vtt" and x.get("url")), None)
    if not track:
        out["available_subtitle_keys"]=list(subs.keys())
        return out
    sub_url=track["url"]
    out["playlist_url"]=sub_url
    try:
        playlist_path=d/"subtitle_playlist.m3u8"
        out["playlist"]=fetch(sub_url,playlist_path,timeout=120)
        playlist=playlist_path.read_text(encoding="utf-8",errors="replace")
    except Exception as e:
        out["playlist_error"]=repr(e); return out
    frag_urls=[urllib.parse.urljoin(sub_url,line.strip()) for line in playlist.splitlines() if line.strip() and not line.startswith("#")]
    blocks=[]
    for i,frag_url in enumerate(frag_urls,1):
        try:
            p=d/f"subtitle_frag_{i:03d}.vtt"
            m=fetch(frag_url,p,timeout=60)
            txt=p.read_text(encoding="utf-8",errors="replace")
            txt=re.sub(r"(\d\d:\d\d:\d\d)\.(\d{3})\d+",r"\1.\2",txt)
            blocks.append(txt)
            out["fragments"].append({"index":i,"url":frag_url,**m})
        except Exception as e:
            out["fragments"].append({"index":i,"url":frag_url,"error":repr(e)})
    if blocks:
        merged=["WEBVTT",""]
        for txt in blocks:
            lines=txt.splitlines()
            if lines and lines[0].strip()=="WEBVTT":
                lines=lines[1:]
            merged.extend(lines)
        merged_path=d/"subtitle_merged.clean.vtt"
        merged_path.write_text("\n".join(merged)+"\n",encoding="utf-8")
        out["merged_file"]=merged_path.name
        out["merged_bytes"]=merged_path.stat().st_size
    return out

def process_url(item,d):
    url=item["url"]; meta={}
    try:
        p=d/"direct.bin"
        meta["direct"]=fetch(url,p)
    except Exception as e:
        meta["direct_error"]=repr(e)
        meta["archive_fallback"]=archive_fallback(url,d)
    if item.get("kind")=="video":
        meta["subtitles"]=extract_vtt_fragments(url,d)
        meta["youtube_transcript_api"]=youtube_transcript(url,d)
    return meta

def derive(d):
    logs={}
    for p in list(d.iterdir()):
        if not p.is_file() or p.suffix in {".json",".txt",".vtt",".jsonl"}: continue
        logs[p.name+"_file"]=sh(["file","-b",str(p)])
        logs[p.name+"_ffprobe"]=sh(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(p)],timeout=120)
        logs[p.name+"_pdftotext"]=sh(["pdftotext",str(p),str(p)+".txt"],timeout=120)
        logs[p.name+"_ocr"]=sh(["tesseract",str(p),str(p)+".ocr"],timeout=180)
        logs[p.name+"_7zlist"]=sh(["7z","l","-slt",str(p)],timeout=120)
    return logs

def main():
    queue=json.load(open("queue/recovery_queue.json",encoding="utf-8"))
    summary=[]
    for item in queue["items"]:
        d=ROOT/safe(item["id"]); d.mkdir(parents=True,exist_ok=True)
        result={"item":item,"acquisition":process_url(item,d)}
        result["derivation"]=derive(d)
        (d/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        summary.append({"id":item["id"],"dir":str(d)})
    (ROOT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":
    main()
