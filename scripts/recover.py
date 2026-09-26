#!/usr/bin/env python3
import json, os, re, hashlib, subprocess, sys, urllib.request, urllib.parse
from pathlib import Path

ROOT=Path("recovery")
ROOT.mkdir(exist_ok=True)
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

def sh(cmd, cwd=None, timeout=900):
    p=subprocess.run(cmd,cwd=cwd,text=True,capture_output=True,timeout=timeout)
    return {"cmd":cmd,"returncode":p.returncode,"stdout":p.stdout[-20000:],"stderr":p.stderr[-20000:]}

def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+","_",s)[:100]

def hash_file(p):
    h=hashlib.sha256()
    with open(p,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def fetch(url,out):
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"*/*"})
    with urllib.request.urlopen(req,timeout=60) as r:
        data=r.read()
        out.write_bytes(data)
        return {"final_url":r.geturl(),"status":getattr(r,"status",None),"headers":dict(r.headers),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}

def process_url(item,d):
    url=item["url"]
    meta={}
    try:
        p=d/"direct.bin"
        meta["direct"]=fetch(url,p)
    except Exception as e:
        meta["direct_error"]=repr(e)
    if item.get("kind")=="video":
        meta["ytdlp"]=sh(["yt-dlp","--write-info-json","--write-subs","--write-auto-subs","--sub-langs","all,-live_chat","--convert-subs","vtt","--no-playlist","-o",str(d/"media.%(ext)s"),url],timeout=1200)
    return meta

def archive_probe(item,d):
    target=item["target"]
    q=urllib.parse.quote(target,safe="")
    endpoints=[
      "https://web.archive.org/cdx/search/cdx?url=*"+q+"*&output=json&filter=statuscode:200&collapse=digest&limit=100",
      "https://index.commoncrawl.org/collinfo.json"
    ]
    out=[]
    for i,u in enumerate(endpoints):
        try:
            p=d/f"archive_probe_{i}.txt"
            m=fetch(u,p); out.append({"url":u,**m})
        except Exception as e: out.append({"url":u,"error":repr(e)})
    return out

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
if __name__=="__main__": main()
