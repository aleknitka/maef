"""`maef` command: thin argparse front-end over MaefClient. Exit codes: 0 ok, 1 run failed/cancelled,
2 usage or API error, 3 timeout."""
import argparse
import json
import os
import sys
from pathlib import Path

from .client import TERMINAL_STATUSES, HttpTransport, MaefClient, MaefClientError

EXIT_OK, EXIT_RUN_FAILED, EXIT_ERROR, EXIT_TIMEOUT = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="maef", description="Delegate bounded tasks to MAEF agents.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("agents", help="list available agents").add_argument("--json", action="store_true")
    for name in ("preview", "run"):
        s = sub.add_parser(name, help=f"{name} a task (INSTRUCTION '-' reads stdin)")
        s.add_argument("agent")
        s.add_argument("instruction")
        s.add_argument("--json", action="store_true")
        s.add_argument("--max-tokens", type=int)
        s.add_argument("--max-turns", type=int)
        s.add_argument("--max-duration", type=int, metavar="SECONDS")
        if name == "run":
            s.add_argument("--revision", metavar="SHA|REF", help="run the definition committed at this revision")
            s.add_argument("--wait", action="store_true", help="follow events and print the result")
            s.add_argument("--timeout", type=float, default=600, metavar="SECONDS")
    for name in ("status", "cancel", "artifacts", "result"):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.add_argument("--json", action="store_true")
    s = sub.add_parser("events", help="print run events")
    s.add_argument("run_id")
    s.add_argument("--follow", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("get", help="download an artifact by digest")
    s.add_argument("digest")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("decide", help="ask a decision model typed questions about a state")
    s.add_argument("profile")
    s.add_argument("--state", required=True, metavar="@FILE|TEXT", help="@file.json (JSON is parsed) or literal text")
    s.add_argument("--question", action="append", required=True, metavar="NAME:TYPE:INSTRUCTIONS[:OPTIONS]",
                   help="choice: opt1=desc,opt2=desc; score: level0,level1,...; noul: [true=desc,false=desc]")
    s.add_argument("--max-tokens", type=int)
    s.add_argument("--wait", action="store_true", help="follow events and print the answers")
    s.add_argument("--timeout", type=float, default=600, metavar="SECONDS")
    s.add_argument("--json", action="store_true")
    sub.add_parser("mcp", help="run the stdio MCP gateway")
    s = sub.add_parser("lint", help="validate definition files (default: this repository)")
    s.add_argument("paths", nargs="*", metavar="PATH")
    s = sub.add_parser("schema", help="JSON Schemas of the definition formats")
    schema_sub = s.add_subparsers(dest="schema_cmd", required=True)
    s = schema_sub.add_parser("export", help="write schemas/*.schema.json")
    s.add_argument("--check", action="store_true", help="exit 1 if committed files differ; write nothing")
    s.add_argument("--dir", metavar="DIR", help="target directory (default: schemas/ in this repository)")
    d = sub.add_parser("draft", help="Git-backed definition drafts (server: MAEF_AUTHORING_ENABLED=1)")
    draft_sub = d.add_subparsers(dest="draft_cmd", required=True)
    s = draft_sub.add_parser("new", help="start a draft of AGENT, or of a new agent with --new-agent")
    s.add_argument("agent", nargs="?")
    s.add_argument("--new-agent", metavar="ID")
    s.add_argument("--json", action="store_true")
    s = draft_sub.add_parser("write", help="replace a file from FILE or stdin ('-')")
    s.add_argument("draft_id")
    s.add_argument("path", help="repo-relative, e.g. agents/<agent>/SYSTEM.md")
    s.add_argument("file", nargs="?", default="-")
    s = draft_sub.add_parser("review", help="show the diff, permission changes and lint findings")
    s.add_argument("draft_id")
    s.add_argument("--json", action="store_true")
    s = draft_sub.add_parser("publish", help="commit the draft on its branch (no merge, no push)")
    s.add_argument("draft_id")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--json", action="store_true")
    s = draft_sub.add_parser("discard")
    s.add_argument("draft_id")
    draft_sub.add_parser("list").add_argument("--json", action="store_true")
    return p


def _budget(args) -> dict | None:
    b = {k: v for k, v in (("max_tokens", args.max_tokens), ("max_turns", args.max_turns),
                           ("max_duration_seconds", args.max_duration)) if v is not None}
    return b or None


def _event_line(event: dict) -> str:
    payload = json.dumps(event.get("payload") or {}, separators=(",", ":"), ensure_ascii=False)
    if len(payload) > 100:
        payload = payload[:97] + "..."
    return f"{event.get('seq', '?'):>4} {event.get('type', '?')} {'' if payload == '{}' else payload}".rstrip()


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _out(args, value, text: str) -> None:
    print(json.dumps(value, indent=2) if args.json else text)


def _print_event(args, event: dict) -> None:
    print(json.dumps(event) if args.json else _event_line(event), flush=True)


def _run(client: MaefClient, args) -> int:
    instruction = sys.stdin.read() if args.instruction == "-" else args.instruction
    if not instruction.strip():
        _err("instruction is empty")
        return EXIT_ERROR
    if args.cmd == "preview":
        preview = client.preview(args.agent, instruction, _budget(args))
        _out(args, preview, f"{preview['agent_id']}: {preview['model_profile']} / {preview['sandbox_profile']}, "
             f"budget {preview['effective_budget']}")
        return EXIT_OK
    run = client.submit(args.agent, instruction, _budget(args), args.revision)
    run_id = run["run_id"]
    if not args.wait:
        _out(args, run, f"{run_id} {run['status']}")
        return EXIT_OK
    _err(f"run {run_id}")
    final = client.wait(run_id, args.timeout, on_event=lambda e: _err(_event_line(e)))
    if final["status"] not in TERMINAL_STATUSES:
        _err(f"timed out after {args.timeout:g}s; run {run_id} is still {final['status']}")
        if args.json:
            print(json.dumps({"run_id": run_id, "status": final["status"]}))
        return EXIT_TIMEOUT
    text = client.result_text(run_id)
    if args.json:
        print(json.dumps({"run_id": run_id, "status": final["status"], "terminal_reason": final.get("terminal_reason"),
                          "tokens_used": final.get("tokens_used"), "result": text}))
    elif text is not None:
        print(text)
    if final["status"] != "succeeded":
        _err(f"run {final['status']}: {final.get('terminal_reason')}")
        return EXIT_RUN_FAILED
    return EXIT_OK


def parse_question(spec: str) -> tuple[str, dict]:
    """NAME:TYPE:INSTRUCTIONS[:OPTIONS] -> (name, question). Instructions cannot contain ':'; options
    are comma separated (choice/noul: key=description pairs, score: ordered level descriptions)."""
    parts = spec.split(":", 3)
    if len(parts) < 3 or not all(p.strip() for p in parts[:3]):
        raise ValueError(f"bad --question {spec!r}: expected NAME:TYPE:INSTRUCTIONS[:OPTIONS]")
    name, kind, instructions = (p.strip() for p in parts[:3])
    question: dict = {"type": kind, "instructions": instructions}
    options = parts[3].strip() if len(parts) == 4 else ""
    if options:
        items = [o.strip() for o in options.split(",") if o.strip()]
        if kind == "score":
            question["criteria"] = items
        else:
            pairs = [i.partition("=") for i in items]
            question["criteria"] = {k.strip(): v.strip() or k.strip() for k, _, v in pairs}
    return name, question


def _decision_state(text: str):
    if not text.startswith("@"):
        return text
    raw = Path(text[1:]).read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    return value if isinstance(value, dict | list) else raw


def _decide(client: MaefClient, args) -> int:
    try:
        questions = dict(parse_question(q) for q in args.question)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    run = client.decide(args.profile, _decision_state(args.state), questions, args.max_tokens)
    run_id = run["run_id"]
    if not args.wait:
        _out(args, run, f"{run_id} {run['status']}")
        return EXIT_OK
    _err(f"run {run_id}")
    final = client.wait(run_id, args.timeout, on_event=lambda e: _err(_event_line(e)))
    if final["status"] not in TERMINAL_STATUSES:
        _err(f"timed out after {args.timeout:g}s; run {run_id} is still {final['status']}")
        return EXIT_TIMEOUT
    answers = client.answers(run_id)
    if answers is not None:
        print(json.dumps({"run_id": run_id, "status": final["status"], "tokens_used": final.get("tokens_used"), **answers})
              if args.json else json.dumps(answers, indent=2))
    if final["status"] != "succeeded":
        _err(f"run {final['status']}: {final.get('terminal_reason')}")
        return EXIT_RUN_FAILED
    return EXIT_OK


def _lint(args) -> int:
    from .lint import lint_repo
    from .registry import ROOT
    roots = ("agents", "sandbox-profiles", "mcp-servers", "model-profiles")
    errors = 0
    for raw in args.paths or [str(ROOT)]:
        path = Path(raw).resolve()
        top = next((p for p in (path, *path.parents) if any((p / r).is_dir() for r in roots)), None)
        if top is None or not path.exists():
            _err(f"{raw}: nothing to lint")
            errors += 1
            continue
        for finding in lint_repo(top):
            if (top / finding.path).resolve().is_relative_to(path) or path == top:
                print(f"{os.path.relpath(top / finding.path)}: " + str(finding).partition(": ")[2])
                errors += finding.severity == "error"
    return EXIT_RUN_FAILED if errors else EXIT_OK


def _schema_export(args) -> int:
    from .schemas import SCHEMAS_DIR, check_schemas, write_schemas
    directory = Path(args.dir) if args.dir else SCHEMAS_DIR
    if args.check:
        drift = check_schemas(directory)
        for name in drift:
            _err(f"{directory / (name + '.schema.json')}: out of date; run `maef schema export`")
        return EXIT_RUN_FAILED if drift else EXIT_OK
    for path in write_schemas(directory):
        print(f"wrote {path}")
    return EXIT_OK


def _draft(client: MaefClient, args) -> int:
    cmd = args.draft_cmd
    if cmd == "new":
        if bool(args.agent) == bool(args.new_agent):
            _err("give AGENT or --new-agent ID")
            return EXIT_ERROR
        draft = client.draft_create(args.agent, args.new_agent)
        _out(args, draft, f"{draft['id']} {draft['branch']}")
    elif cmd == "write":
        content = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        client.draft_write(args.draft_id, args.path, content)
        _err(f"wrote {args.path}")
    elif cmd == "review":
        review = client.draft_review(args.draft_id)
        if args.json:
            print(json.dumps(review, indent=2))
        else:
            print(review["diff"] or "(no changes)")
            print(_permission_summary(review["permissions"]))
            for f in review["findings"]:
                print(f"{f['path']}: {'warning: ' if f['severity'] == 'warning' else ''}{f['message']}")
            print("publishable" if review["publishable"] else "NOT publishable: fix lint errors")
    elif cmd == "publish":
        out = client.draft_publish(args.draft_id, args.message)
        _out(args, out, out["revision"])
    elif cmd == "discard":
        client.draft_discard(args.draft_id)
    elif cmd == "list":
        drafts = client.draft_list()
        _out(args, drafts, "\n".join(f"{d['id']}\t{d['agent_id']}\t{d['branch']}\t{d.get('published') or '-'}" for d in drafts))
    return EXIT_OK


def _permission_summary(p: dict) -> str:
    lines = ["permission changes:" if _has_changes(p) else "permission changes: none"]
    if p["new_agent"]:
        lines.append("  new agent")
    for label, key in (("tools added", "tools_added"), ("tools removed", "tools_removed"),
                       ("mcp servers added", "mcp_servers_added"), ("mcp servers removed", "mcp_servers_removed"),
                       ("mcp servers with open network added", "mcp_network_open_added")):
        if p[key]:
            lines.append(f"  {label}: {', '.join(p[key])}")
    if p["network_open"]:
        lines.append(f"  permissions.network_open: {p['network_open']['before']} -> {p['network_open']['after']}")
    for c in p["budget_increases"] + p["runtime_changes"]:
        lines.append(f"  {c['field']}: {c['before']} -> {c['after']}")
    for ref, names in p["mcp_new_secrets"].items():
        lines.append(f"  new secrets for {ref}: {', '.join(names)}")
    return "\n".join(lines)


def _has_changes(p: dict) -> bool:
    return p["new_agent"] or any(p[k] for k in (
        "tools_added", "tools_removed", "mcp_servers_added", "mcp_servers_removed", "network_open",
        "budget_increases", "runtime_changes", "mcp_new_secrets", "mcp_network_open_added"))


def _dispatch(client: MaefClient, args) -> int:
    cmd = args.cmd
    if cmd == "agents":
        agents = client.list_agents()
        _out(args, agents, "\n".join(f"{a['id']}\t{a.get('display_name', '')}" for a in agents))
    elif cmd in ("preview", "run"):
        return _run(client, args)
    elif cmd == "decide":
        return _decide(client, args)
    elif cmd == "draft":
        return _draft(client, args)
    elif cmd == "status":
        run = client.get_run(args.run_id)
        run.pop("events", None)
        _out(args, run, f"{run['id']} {run['status']}" + (f" ({run['terminal_reason']})" if run.get("terminal_reason") else ""))
    elif cmd == "events":
        if args.follow:
            for event in client.events(args.run_id):
                _print_event(args, event)
        else:
            for event in client.get_run(args.run_id).get("events", []):
                _print_event(args, event)
    elif cmd == "cancel":
        out = client.cancel(args.run_id)
        _out(args, out, out["outcome"])
    elif cmd == "artifacts":
        refs = client.artifacts(args.run_id)
        _out(args, refs, "\n".join(f"{r['digest']}\t{r['size']}\t{r['name']}" for r in refs))
    elif cmd == "get":
        dest = Path(args.output)
        if dest.exists() and not args.force:
            _err(f"{dest} exists; use --force to overwrite")
            return EXIT_ERROR
        dest.write_bytes(client.download(args.digest))
        _err(f"wrote {dest}")
    elif cmd == "result":
        text = client.result_text(args.run_id)
        if text is None:
            _err("no output.md artifact for this run")
            return EXIT_ERROR
        _out(args, {"run_id": args.run_id, "result": text}, text)
    return EXIT_OK


def main(argv: list[str] | None = None, client: MaefClient | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_ERROR
    try:
        if args.cmd == "lint":
            return _lint(args)
        if args.cmd == "schema":
            return _schema_export(args)
        if args.cmd == "mcp":
            from .gateway import Gateway
            return Gateway(client or MaefClient(HttpTransport()), sys.stdin.buffer, sys.stdout.buffer).serve()
        return _dispatch(client or MaefClient(HttpTransport()), args)
    except MaefClientError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    except OSError as exc:
        _err(f"error: {exc.strerror or exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
