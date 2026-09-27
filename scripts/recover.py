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
    # Keep raw subtitle fragments; do not ask yt-dlp/ffmpeg to normalize malformed timestamps.
    r=sh(["yt-dlp","--skip-download","--write-info-json","--write-subs","--write-auto-subs",
          "--sub-langs","en.*,en","--sub-format","vtt","--no-playlist",
          "-o",str(d/"media.%(ext)s"),url],timeout=600)
    manifest=[]
    for p in sorted(d.glob("*.vtt")):
        txt=p.read_text(encoding="utf-8",errors="replace")
        cleaned=[]
        for line in txt.splitlines():
            line=re.sub(r"(\d\d:\d\d:\d\d)\.(\d{3})\d+",r"\1.\2",line)
            cleaned.append(line)
        cp=p.with_name(p.stem+".clean.vtt")
        cp.write_text("\n".join(cleaned)+"\n",encoding="utf-8")
        manifest.append({"file":p.name,"bytes":p.stat().st_size,"clean_file":cp.name})
    return {"ytdlp":r,"vtt_files":manifest}

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
