#!/usr/bin/env python3
import json, re, hashlib, subprocess, urllib.request, urllib.parse
from pathlib import Path

ROOT=Path("recovery")
ROOT.mkdir(exist_ok=True)
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

def sh(cmd, timeout=900):
    p=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
    return {"cmd":cmd,"returncode":p.returncode,"stdout":p.stdout[-20000:],"stderr":p.stderr[-20000:]}

def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",s)[:100]

def fetch(url,out,timeout=60):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"*/*"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        data=r.read()
        out.write_bytes(data)
        return {"final_url":r.geturl(),"status":getattr(r,"status",None),"headers":dict(r.headers),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}

def youtube_transcript(url,d):
    m=re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",url)
    if not m:
        return {"skipped":"not youtube"}
    vid=m.group(1)
    code=(
      "from youtube_transcript_api import YouTubeTranscriptApi\n"
      f"vid={vid!r}\n"
      "api=YouTubeTranscriptApi()\n"
      "items=api.fetch(vid)\n"
      "import json\n"
      "print(json.dumps([{'text':x.text,'start':x.start,'duration':x.duration} for x in items],ensure_ascii=False))\n"
    )
    r=sh(["python","-c",code],timeout=180)
    if r["returncode"]==0:
        (d/"youtube_transcript.json").write_text(r["stdout"],encoding="utf-8")
    return r

def process_url(item,d):
    url=item["url"]
    meta={}
    try:
        p=d/"direct.bin"
        meta["direct"]=fetch(url,p)
    except Exception as e:
        meta["direct_error"]=repr(e)
    if item.get("kind")=="video":
        meta["ytdlp_metadata_subs"]=sh([
          "yt-dlp","--skip-download","--write-info-json","--write-subs","--write-auto-subs",
          "--sub-langs","all,-live_chat","--convert-subs","vtt","--no-playlist",
          "-o",str(d/"media.%(ext)s"),url
        ],timeout=600)
        meta["youtube_transcript_api"]=youtube_transcript(url,d)
    return meta

def wayback_query(query,d,name):
    u="https://web.archive.org/cdx/search/cdx?"+urllib.parse.urlencode({
      "url":query,"output":"json","filter":"statuscode:200","collapse":"digest","limit":"100"
    })
    p=d/name
    try:
        return {"url":u,**fetch(u,p,timeout=120)}
    except Exception as e:
        return {"url":u,"error":repr(e)}

def commoncrawl_query(query,d,name,index="CC-MAIN-2026-38"):
    u=f"https://index.commoncrawl.org/{index}-index?"+urllib.parse.urlencode({
      "url":query,"output":"json","filter":"status:200"
    })
    p=d/name
    try:
        return {"url":u,**fetch(u,p,timeout=120)}
    except Exception as e:
        return {"url":u,"error":repr(e)}

def archive_probe(item,d):
    target=item["target"]
    probes=[]
    if target.endswith((".mov",".mp4",".mp3",".m3u")):
        probes.append(wayback_query("*"+target+"*",d,"wayback_filename.txt"))
        probes.append(commoncrawl_query("*"+target+"*",d,"cc_filename.txt","CC-MAIN-2013-48"))
    elif target=="12GSRS094YW79MDL":
        probes.append(wayback_query("*12GSRS094YW79MDL*",d,"wayback_waywire.txt"))
        probes.append(commoncrawl_query("*12GSRS094YW79MDL*",d,"cc_waywire.txt","CC-MAIN-2015-18"))
    elif target=="2867948":
        probes.append(wayback_query("*2867948*",d,"wayback_nbc.txt"))
        probes.append(commoncrawl_query("*2867948*",d,"cc_nbc.txt","CC-MAIN-2015-14"))
    elif "NewYork.com" in target:
        probes.append(wayback_query("newyork.com/articles/broadway/a-day-in-the-life-of-annie-star-sadie-sink*",d,"wayback_newyork.txt"))
        probes.append(commoncrawl_query("newyork.com/*56345*",d,"cc_newyork.txt","CC-MAIN-2013-48"))
    else:
        probes.append(wayback_query("*"+target+"*",d,"wayback_generic.txt"))
    return probes

def derive(d):
    logs={}
    for p in list(d.iterdir()):
        if not p.is_file() or p.suffix in {".json",".txt",".vtt"}: continue
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
        result={"item":item}
        if item.get("url"): result["acquisition"]=process_url(item,d)
        else: result["archive_probe"]=archive_probe(item,d)
        result["derivation"]=derive(d)
        (d/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        summary.append({"id":item["id"],"dir":str(d)})
    (ROOT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":
    main()
