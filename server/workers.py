"""Run bounded data helpers and mediate SDK traffic to installed service addresses."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib import request, error

from connections import NoRedirect

HERE=Path(__file__).resolve().parent


def command(name):
    binary=shutil.which("node")
    if not binary:
        raise ValueError("This block needs Node.js 22 or newer on the server. The container includes Node.")
    return [binary,"--max-old-space-size=128",str(HERE.parent/"runtime"/(name+".mjs"))]


def environment():
    return {k:os.environ[k] for k in ("SystemRoot","SYSTEMROOT","WINDIR","TEMP","TMP") if k in os.environ}


def helper(name, payload, timeout=5):
    data=payload if isinstance(payload,bytes) else json.dumps(payload,allow_nan=False).encode()
    try:
        run=subprocess.run(command(name),input=data,capture_output=True,env=environment(),timeout=timeout)
        result=json.loads(run.stdout)
    except (subprocess.TimeoutExpired,ValueError,OSError):
        raise ValueError("The bounded worker did not confirm a result.") from None
    if run.returncode or result.get("ok") is False or "failure" in result:
        raise ValueError(str(result.get("error") or result.get("failure") or "Worker failed.")[:1000])
    return result


def http(url, method, headers, body, limit=524288, timeout=20):
    client=request.build_opener(request.ProxyHandler({}),NoRedirect())
    req=request.Request(url,data=body,headers=headers,method=method)
    try:
        try:
            response=client.open(req,timeout=timeout)
        except error.HTTPError as e:
            response=e
        with response:
            raw=response.read(limit+1)
            if len(raw)>limit:
                raise ValueError("Endpoint response exceeds the size limit.")
            return response.status,dict(response.headers),raw
    except OSError:
        raise ValueError("Endpoint did not confirm the request.") from None


def headers_for(connections, entry):
    token=connections.secret(entry)
    return {"Authorization":"Bearer "+token} if token else {}


def endpoint_json(connections, name, value):
    entry=connections.services[name]
    method=entry.get("method","POST")
    headers={"Content-Type":"application/json",**headers_for(connections,entry)}
    body=None if method=="GET" else json.dumps(value,allow_nan=False).encode()
    status,_,raw=http(entry["url"],method,headers,body)
    if not 200<=status<300:
        raise ValueError(f"Service returned HTTP {status}.")
    return json.loads(raw) if raw.strip() else None


def feed(connections,name):
    entry=connections.services[name]
    status,_,raw=http(entry["url"],"GET",headers_for(connections,entry),None)
    if status!=200:raise ValueError(f"Feed returned HTTP {status}.")
    return helper("rss-parser",raw)


def sdk(connections,kind,cfg,value,invocation):
    name=cfg["service"];entry=connections.services[name]
    registered=entry["url"]
    allowed={registered}
    if kind=="a2a":
        base=registered.rstrip("/")
        allowed.update(base+"/"+suffix for suffix in (".well-known/agent-card.json",".well-known/agent.json"))
        allowed.update(entry.get("endpoint_urls",[]))
        payload={**cfg,"agentUrl":registered,"messageId":invocation,"message":cfg.get("message",value)}
    else:
        payload={**cfg,"server":name,"url":registered,"arguments":cfg.get("arguments",value)}
        if cfg["operation"]=="call" and cfg["tool"] not in entry.get("tools",[]):
            raise ValueError("Select an MCP tool allowed in the service configuration.")
    with subprocess.Popen(command(kind+"-client"),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,env=environment()) as proc:
        timer=threading.Timer(90,proc.kill)
        timer.start()
        def send(item):
            proc.stdin.write((json.dumps(item)+"\n").encode());proc.stdin.flush()
        try:
            send(payload)
            for _ in range(55):
                line=proc.stdout.readline(2**20+1)
                if not line or len(line)>2**20:raise ValueError("SDK did not return a bounded message.")
                msg=json.loads(line)
                if msg.get("kind")=="result":
                    if not msg.get("ok") or msg.get("failed"):raise ValueError(str(msg.get("error","Remote operation failed."))[:500])
                    return msg["values"]
                if msg.get("kind")!="http" or msg.get("url") not in allowed or msg.get("method") not in ("GET","POST","DELETE"):
                    raise ValueError("SDK requested an address outside the configured service endpoints.")
                headers={k:v for k,v in msg.get("headers",{}).items() if k.lower() in
                         ("accept","content-type","mcp-protocol-version","mcp-session-id","a2a-version","a2a-extensions")}
                headers.update(headers_for(connections,entry))
                body=msg.get("body")
                code,h,raw=http(msg["url"],msg["method"],headers,body.encode() if body is not None else None)
                safe={k:v for k,v in h.items() if k.lower() in ("content-type","mcp-session-id","mcp-protocol-version")}
                send({"status":code,"headers":safe,"body":base64.b64encode(raw).decode()})
            raise ValueError("SDK request limit reached.")
        finally:
            timer.cancel()
            if proc.poll() is None:proc.kill()
            proc.wait()
