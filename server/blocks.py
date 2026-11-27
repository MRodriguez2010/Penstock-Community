"""Portable workflow blocks. Configuration can reference only registered routes and local project data."""
import json
import base64
import project_resources

import agent_schema
import graph_knowledge
import graph_table_core
import graph_values
import portable_lookup
import portable_transforms
import structured_start
import workflow_files
import workers
from graph_spec import graph_id

RESOURCES = {"table","knowledge","pinned-table","file","verify-artifact","memory","callback","step-log"}
MODELS = {"translate","evaluator","router","rank","local-model","chat","thinking","decisions","vision"}
SERVICES = {"http","rss","mcp","a2a"}
CONTAINERS = {"loop-container","parallel-container"}
CHILDREN = {"invoke-workflow","for-each","parallel-each"}
# Required and optional fields. Each names an actual input to the runner.
FIELDS = {
    **{kind: ("adapter operation query limit", "") for kind in portable_lookup.SCHEMAS},
    "group-records": ("field", "rows"),
    "ordered-condition": ("branches", "value"),
    "source-read": ("source binding sample",""),
    "loop-container": ("body","count items"), "parallel-container": ("body","count items"),
    "vision": ("provider fileId question",""), "decisions": ("provider question threshold",""),
    "credential": ("operation","profile"), "deployments": ("operation workflow","version"),
    "pinned-table": ("tableId revision",""), "file": ("operation","id name text version search limit latestOnly"),
    "verify-artifact": ("expectedSha256 expectedBytes","id name version"),
    "memory": ("operation","conversation role content"), "callback": ("",""), "step-log": ("operation","label"),
    "variables": ("values",""), "response": ("value",""), "verify-value": ("","schema value expected"),
    "function": ("code","params"), "guardrails": ("validationType","regex"),
    "wait": ("seconds",""), "human-input": ("question","schema"),
    "http": ("service","preview"), "rss": ("service","preview"), "mcp": ("service operation","tool arguments preview"),
    "a2a": ("service operation","message taskId contextId historyLength data preview"),
    "translate": ("provider language",""), "evaluator": ("provider criteria",""),
    "router": ("provider choices","instructions"), "rank": ("provider criteria",""),
    "local-model": ("provider instructions","skill"), "chat": ("provider instructions","skill"),
    "thinking": ("provider instructions","skill"), "invoke-workflow": ("workflow version",""),
    "for-each": ("workflow version",""), "parallel-each": ("workflow version","")
}
OPERATIONS = {
    "credential":{"list","select"}, "deployments":{"list","publish","withdraw"},
    "file":{"list","read","save"}, "memory":{"add","read","delete","list"}, "step-log":{"read","write"},
    "mcp":{"discover","list","call"}, "a2a":{"a2a_get_agent_card","a2a_send_message","a2a_get_task","a2a_cancel_task"}
}


def validate(kind, cfg, connections):
    if kind in ("table","knowledge"):
        module = graph_table_core if kind == "table" else graph_knowledge
        params = dict(cfg)
        if kind == "knowledge":
            params.setdefault("knowledgeBaseId", {"ref":{"node":"$input"}})
        spec = {k:v if k=="operation" or isinstance(v,dict) and set(v) in ({"ref"},{"literal"}) else {"literal":v}
                for k,v in params.items()}
        module.config(spec)
        return {"success"}
    if kind not in FIELDS:
        raise ValueError(f"Unknown block {kind}.")
    must, may = (set(group.split()) for group in FIELDS[kind])
    if not must <= set(cfg) or set(cfg) - must - may:
        raise ValueError(f"Check the configuration fields for {kind}.")
    if kind in portable_lookup.KINDS:
        return portable_lookup.validate(kind,cfg)
    if kind == "group-records":
        return portable_transforms.validate_group(cfg)
    if kind == "ordered-condition":
        return portable_transforms.validate_ordered(cfg)
    if kind == "verify-artifact":
        return workflow_files.validate_verification(cfg)
    if kind=="source-read":
        import re
        from office_connections import CAPABILITIES
        from ops_events import NOTIFIERS
        if not isinstance(cfg["source"],str) or cfg["source"] not in CAPABILITIES or cfg["source"] in NOTIFIERS:
            raise ValueError("Choose a project connection.")
        if not isinstance(cfg["binding"],str) or not re.fullmatch(r"[0-9a-f]{64}",cfg["binding"]):
            raise ValueError("Add this source from Connections to bind its settings.")
        if not isinstance(cfg["sample"],list) or len(cfg["sample"])>5 or not all(isinstance(x,str) and len(x)<=240 for x in cfg["sample"]):
            raise ValueError("Supply up to five short sample items.")
        return {"success"}
    op = cfg.get("operation")
    if kind in OPERATIONS and op not in OPERATIONS[kind]:
        raise ValueError(f"Unknown {kind} operation.")
    if kind == "file":
        return workflow_files.validate_file(cfg)
    for key in ("instructions","language","criteria","question","code"):
        if key in cfg and (not isinstance(cfg[key],str) or not cfg[key].strip() or len(cfg[key])>16000):
            raise ValueError(f"Supply bounded text for {key}.")
    if kind in MODELS and cfg["provider"] not in connections.providers:
        raise ValueError("Select an installed model profile.")
    if "skill" in cfg and cfg["skill"] not in connections.skills:
        raise ValueError("Select an installed skill.")
    if kind in SERVICES:
        service = connections.services.get(cfg["service"])
        if not service or service.get("protocol","http") != kind:
            raise ValueError("Select a registered service with the matching protocol.")
        if kind=="mcp" and op=="call" and cfg.get("tool") not in service.get("tools",[]):
            raise ValueError("Select a tool allowed by the workspace owner.")
    if kind in CONTAINERS:
        if "items" in cfg and "count" in cfg:
            raise ValueError("Choose a collection or an iteration count.")
        if "count" in cfg and (type(cfg["count"]) is not int or not 0<=cfg["count"]<=100):
            raise ValueError("Choose a count from zero through 100.")
        if not isinstance(cfg["body"],dict):
            raise ValueError("Supply a workflow plan for the container body.")
    if kind in CHILDREN:
        from project_resources import ident
        ident(cfg["workflow"])
        if type(cfg["version"]) is not int or cfg["version"]<1:
            raise ValueError("Choose an exact child workflow version.")
    if kind=="wait" and (type(cfg["seconds"]) is not int or not 1<=cfg["seconds"]<=31536000):
        raise ValueError("Wait must be 1 second through 365 days.")
    if kind=="pinned-table":
        project_resources.ident(cfg["tableId"])
        if type(cfg["revision"]) is not int or cfg["revision"]<1:
            raise ValueError("Choose the table ID and a positive revision number.")
    if kind=="variables":
        if not isinstance(cfg["values"],dict) or len(cfg["values"])>64:
            raise ValueError("Supply up to 64 named variables.")
        for key in cfg["values"]:
            graph_id(key)
    if "schema" in cfg:
        agent_schema.check(cfg["schema"])
    if kind=="verify-value" and ("expected" in cfg)==("schema" in cfg):
        raise ValueError("Choose an expected JSON value or a response schema.")
    if kind=="credential" and op=="select" and cfg.get("profile") not in connections.services:
        raise ValueError("Select a registered service profile.")
    if kind=="decisions" and (type(cfg["threshold"]) not in (int,float) or not 0.5<cfg["threshold"]<=1):
        raise ValueError("Choose a decision threshold above 0.5 and at most 1.")
    if kind=="decisions":
        return {"yes","no","uncertain"}
    if kind=="deployments" and op!="list" and (type(cfg.get("version")) is not int or cfg["version"]<1):
        raise ValueError("Select an exact version.")
    if kind=="guardrails" and (cfg["validationType"] not in ("json","regex") or
                              cfg["validationType"]=="regex" and not isinstance(cfg.get("regex"),str)):
        raise ValueError("Choose JSON or provide a bounded regular expression.")
    if kind=="router":
        choices=cfg["choices"]
        if not isinstance(choices,list) or not 2<=len(choices)<=12 or len(set(choices))!=len(choices):
            raise ValueError("Supply 2 to 12 distinct route names.")
        return {graph_id(choice) for choice in choices}
    return {"success","error"} if kind in ("guardrails","verify-value") else {"success"}


def catalog():
    result={kind:{"required":required.split(),"optional":optional.split()} for kind,(required,optional) in FIELDS.items()}
    for kind, module in (("table",graph_table_core),("knowledge",graph_knowledge)):
        result[kind]={"operations":{op:{"required":sorted(required | ({"tableId"} if kind=="table" else set())),
                                        "optional":sorted(module.OPTIONAL.get(op,set()))}
                                    for op,required in module.REQUIRED.items()}}
    for kind, choices in OPERATIONS.items():
        result[kind]["operations"]=sorted(choices)
    for kind, choices in portable_lookup.SCHEMAS.items():
        result[kind]["operations"]=sorted(choices)
    return result


def execute(kind, cfg, value, connections, ctx):
    if kind == "group-records":
        return portable_transforms.group(cfg, value)
    if kind == "ordered-condition":
        return portable_transforms.ordered(cfg, value)
    if kind=="variables":
        ctx.setdefault("variables",{}).update(cfg["values"])
        graph_values.bound(ctx["variables"])
        return "success", value
    if kind=="response":
        return "success", cfg["value"]
    if kind=="verify-value":
        result=cfg.get("value",value)
        if "expected" in cfg:
            if agent_schema.canonical(result)==agent_schema.canonical(cfg["expected"]):
                return "success",result
            return "error",{"message":"The value differs from the expected JSON.","value":result}
        try:
            agent_schema.validate(cfg["schema"], result)
        except ValueError as exc:
            return "error", {"message":str(exc),"value":result}
        return "success",result
    if kind=="function":
        result=workers.helper("function-sandbox",{"language":"javascript","code":cfg["code"],"params":cfg.get("params",value),
                              "memoryMiB":16,"timeoutMs":1000})
        return "success",result["result"]
    if kind=="guardrails":
        source=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        result=workers.helper("guardrails-check",{**cfg,"input":source})
        return ("success",value) if result["passed"] else ("error",result)
    if kind=="credential":
        profiles={name:{"profile":name,"protocol":entry.get("protocol","http"),"hasCredential":"key_env" in entry}
                  for name,entry in connections.services.items()}
        return "success",profiles[cfg["profile"]] if cfg["operation"]=="select" else list(profiles.values())
    prompt=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    system=cfg.get("instructions","")
    if kind=="translate":
        system="Translate the supplied value into "+cfg["language"]+". Return the translation."
    if kind=="decisions":
        system=cfg["question"]+' Return JSON with a numeric probability of yes from 0 to 1 and a short reason.'
    if kind=="evaluator":
        system="Assess the input using these criteria: "+cfg["criteria"]+'. Return JSON {"score":0..1,"reason":"text"}.'
    if kind=="router":
        system+="\nSelect a route from "+json.dumps(cfg["choices"])+". Return just its name."
    if kind=="rank":
        if not isinstance(value,list) or len(value)>100:
            raise ValueError("Rank expects an array of up to 100 items.")
        system="Rank by "+cfg["criteria"]+". Return a JSON array of all zero-based item indices in rank order."
    if "skill" in cfg:
        system+="\n\n"+connections.skills[cfg["skill"]]
    answer=connections.complete(cfg["provider"],system,prompt)
    if kind=="router":
        route=answer.strip()
        if route not in cfg["choices"]:
            raise ValueError("The model returned an undeclared route.")
        return route,value
    if kind=="decisions":
        answer=json.loads(answer)
        score=answer.get("probability") if isinstance(answer,dict) else None
        if type(score) not in (int,float) or not 0<=score<=1:
            raise ValueError("The decision needs a probability between zero and one.")
        route="yes" if score>=cfg["threshold"] else "no" if score<=1-cfg["threshold"] else "uncertain"
        return route,answer
    if kind=="evaluator":
        answer=json.loads(answer)
        if not isinstance(answer,dict) or type(answer.get("score")) not in (int,float) or not 0<=answer["score"]<=1 or not isinstance(answer.get("reason"),str):
            raise ValueError("Expected a score between zero and one and a reason.")
    if kind=="rank":
        indices=json.loads(answer)
        if not isinstance(indices,list) or not all(type(i) is int for i in indices) or sorted(indices)!=list(range(len(value))):
            raise ValueError("The model must return every index exactly once.")
        answer=[value[i] for i in indices]
    return "success",answer


def deliver(kind, connections, cfg, value, invocation):
    if kind=="http":
        return workers.endpoint_json(connections,cfg["service"],value)
    if kind=="rss":
        return workers.feed(connections,cfg["service"])
    return workers.sdk(connections,kind,cfg,value,invocation)


def external_read(kind, cfg, connections):
    if kind == "http":
        return connections.services[cfg["service"]].get("method", "POST") == "GET"
    return (kind == "rss" or kind == "mcp" and cfg["operation"] in {"discover", "list"} or
            kind == "a2a" and cfg["operation"] in {"a2a_get_agent_card", "a2a_get_task"})


def vision(data, record, cfg, connections):
    with data.db(record["project"],record["preview"]) as c:
        file,raw=project_resources.file_data(c,record["project"],project_resources.ident(cfg["fileId"]))
    mime=project_resources.media_type(file["name"])
    if mime not in ("image/png","image/jpeg","image/webp") or len(raw)>700000:
        raise ValueError("Choose a project PNG, JPEG or WebP file of up to 700,000 bytes.")
    text=connections.complete(cfg["provider"],"Describe the visible evidence relevant to the question.",cfg["question"],
                              image=(mime,base64.b64encode(raw).decode("ascii")))
    return {"answer":text,"file":file["id"],"model":connections.providers[cfg["provider"]]["model"]}
