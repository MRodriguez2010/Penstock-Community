"""Read a pinned workflow receipt and propose a reviewable correction."""
import copy
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit


NUMBER_ERROR = re.compile(r"^Start ([A-Za-z][A-Za-z0-9_]{0,63}): Expected a finite number\.$")


def provider_disclosures(connections):
    """Expose the configured destination and charging boundary, without credential metadata."""
    profiles = {}
    for name, entry in connections.providers.items():
        host = urlsplit(entry.get("url", "")).hostname
        if not host:
            continue
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        profiles[name] = {
            "model": entry["model"],
            "host": host,
            "local": loopback,
            "binding": hashlib.sha256(json.dumps({
                field: entry.get(field) for field in ("url", "model", "protocol", "key_env")
            }, sort_keys=True).encode()).hexdigest(),
            "cost": ("This call uses a local model endpoint. Penstock adds no separate charge."
                     if loopback else "The provider may charge for this call. Rates and remaining credits are set in your provider account."),
        }
    return profiles


def choose_node(version, run, node_id):
    nodes = version["graph"]["nodes"]
    known = {node["id"]: node for node in nodes}
    if node_id is not None:
        if not isinstance(node_id, str) or node_id not in known:
            raise ValueError("Select a step belonging to this workflow version.")
        return known[node_id]
    receipt = run["history"][-1] if run and run["history"] else None
    return known.get(run.get("cursor") if run else None) or known.get(receipt["node"] if receipt else None) or nodes[0]


def draft_for_number_error(version, field):
    draft = copy.deepcopy(version["plan"])
    start = next((step for step in draft["steps"] if step["type"] == "start"), None)
    spec = next((item for item in (start or {}).get("config", {}).get("inputFormat", [])
                 if item["name"] == field and item.get("type") == "number"), None)
    if spec is None:
        return None
    spec["type"] = "string"
    if "value" in spec:
        spec["value"] = str(spec["value"])
    return draft


def finding(version, run, node):
    history = run["history"] if run else []
    receipt = next((entry for entry in reversed(history) if entry.get("node") == node["id"]), None)
    if not run:
        return "no-receipt", "Start with a local test", [
            "Open the run inputs, supply an example and test this exact version.",
            "Use the resulting receipt to inspect the branch and the inputs to this step.",
        ], None
    if run["state"] == "waiting" and run.get("cursor") == node["id"]:
        return "waiting", "This step needs a review decision", [
            "Open the run receipt to inspect what the step is waiting for.",
            "A project reviewer can approve or decline the pending decision.",
        ], None
    if run["state"] == "interrupted":
        return "interrupted", "The run was interrupted", [
            "Review the last recorded step and check any change made by an external service.",
            "Test the current inputs before retrying a live workflow.",
        ], None
    if receipt is None:
        return "not-reached", "This run did not reach the selected step", [
            "Check the route taken in the run activity.",
            "Use an input that takes the intended route and start another test.",
        ], None
    error = str(receipt.get("error", ""))
    if not error:
        if receipt.get("port") == "error":
            return "rejected-output", "The step used its error route", [
                "Inspect the selected step result and the validation criteria.",
                "Adjust the inputs or criteria in a draft, then run a test.",
            ], None
        return "step-completed", "The selected step completed", [
            "Compare its result with the intended outcome, then inspect the next step on the recorded route.",
        ], None
    number = NUMBER_ERROR.fullmatch(error)
    if number:
        field = number[1]
        return "invalid-number", "A run input needs a number", [
            "Correct " + field + " in the run inputs and test again.",
            "If " + field + " is an identifier or label, change its type to Text in a draft.",
            "Test any downstream calculation that uses this field before publishing.",
        ], draft_for_number_error(version, field)
    if "needs a model call" in error.lower() or "model consent" in error.lower():
        return "model-consent", "This step uses a model provider", [
            "Review the step's model profile and what the run would send there.",
            "For a model test, confirm the selected provider before starting the test.",
        ], None
    if "not authorized" in error.lower() or "feature is disabled" in error.lower():
        return "permission", "The run lacks permission for this step", [
            "Check the selected source, destination, and project permissions.",
            "Ask a project operator to review the connection and approve a live run.",
            "Use an offline preview to verify the workflow with sample values.",
        ], None
    if "missing value" in error.lower() or "has no output" in error.lower():
        return "missing-value", "A required step value was unavailable", [
            "Open the preceding step's result on this same run path.",
            "Check that the selected field exists on this path, or supply a default in the draft.",
            "Test both the usual path and the path with a missing value.",
        ], None
    return "step-failed", "The selected step failed", [
        "Read the error and compare the step's settings with the recorded input.",
        "Edit the step in a draft, then test it with a small known example.",
    ], None


def diagnose(workspace, project, data, *, can_edit=False, connections=None):
    """Keep every local and model-assisted diagnosis scoped to one exact version and optional run."""
    workflow, version_number = data.get("workflow"), data.get("version")
    if not isinstance(workflow, str) or type(version_number) is not int or version_number < 1:
        raise ValueError("Choose a saved workflow version.")
    version = workspace.version(workflow, version_number)
    if version["project"] != project:
        raise PermissionError("This workflow is unavailable in your project.")
    run_id = data.get("run")
    run = None
    if run_id is not None:
        if not isinstance(run_id, str):
            raise ValueError("Choose a saved run receipt.")
        run = workspace.run(run_id)
        if (run["project"], run["workflow"], run["version"]) != (project, workflow, version_number):
            raise PermissionError("Choose a receipt for this exact workflow version.")
    node = choose_node(version, run, data.get("node"))
    code, title, guidance, repair = finding(version, run, node)
    result = {
        "title": title, "code": code, "guidance": guidance, "workflow": workflow,
        "version": version_number, "node": node["id"], "step": node.get("label") or node["type"],
        "run": run_id, "state": run["state"] if run else None, "canEdit": can_edit,
    }
    if repair is not None and can_edit:
        result["repair"] = {
            "plan": repair, "workflow": workflow, "version": version_number, "node": node["id"],
            "summary": "Accept text in the selected input. Test the complete draft before publishing.",
        }
    provider = data.get("provider")
    if provider is not None:
        if not can_edit or data.get("modelConsent") is not True:
            raise PermissionError("A project editor must confirm sending the diagnosis summary to the selected provider.")
        provider_store = connections or workspace.connections
        disclosures = provider_disclosures(provider_store)
        if not isinstance(provider, str) or provider not in disclosures:
            raise ValueError("Choose a configured diagnosis provider.")
        if data.get("providerBinding") is not None and data["providerBinding"] != disclosures[provider]["binding"]:
            raise ValueError("The selected provider changed. Reopen the consent panel before retrying.")
        context = {"finding": code, "stepType": node["type"], "runState": result["state"]}
        system = ("You give brief, cautious workflow troubleshooting suggestions. "
                  "Use only the supplied failure category, step type, and run state. "
                  "Give two concrete checks as plain text. Do not infer hidden inputs, "
                  "generate code, or suggest publishing before a test.")
        answer = provider_store.complete(provider, system, json.dumps(context))
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("The selected provider did not return diagnosis guidance.")
        result["assistant"] = {"provider": provider, "text": answer.strip()[:6000], **disclosures[provider]}
    return result
