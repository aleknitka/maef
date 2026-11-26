"""`maef` command: thin argparse front-end over MaefClient. Exit codes: 0 ok, 1 run failed/cancelled,
2 usage or API error, 3 timeout."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import evals, otel
from .client import TERMINAL_STATUSES, HttpTransport, MaefClient, MaefClientError
from .registry import ROOT

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
    s.add_argument(
        "--question",
        action="append",
        required=True,
        metavar="NAME:TYPE:INSTRUCTIONS[:OPTIONS]",
        help="choice: opt1=desc,opt2=desc; score: level0,level1,...; noul: [true=desc,false=desc]",
    )
    s.add_argument("--max-tokens", type=int)
    s.add_argument("--wait", action="store_true", help="follow events and print the answers")
    s.add_argument("--timeout", type=float, default=600, metavar="SECONDS")
    s.add_argument("--json", action="store_true")
    sub.add_parser("mcp", help="run the stdio MCP gateway")
    o = sub.add_parser("otel", help="OpenTelemetry export of finished runs").add_subparsers(
        dest="otel_cmd", required=True
    )
    s = o.add_parser("export", help="send finished runs to an OTLP/HTTP collector as traces (idempotent)")
    s.add_argument(
        "--endpoint",
        metavar="URL",
        default=os.environ.get("MAEF_OTLP_ENDPOINT"),
        help="collector base URL (default: MAEF_OTLP_ENDPOINT); posts to URL/v1/traces",
    )
    s.add_argument(
        "--headers-ref",
        metavar="env:NAME",
        default=os.environ.get("MAEF_OTLP_HEADERS_REF"),
        help="credential ref whose value is Name=value,Name2=value2",
    )
    s.add_argument("--allow-private", action="store_true", help="allow a private-network endpoint")
    s.add_argument("--since", metavar="ISO", help="only runs created at or after this timestamp")
    s.add_argument("--run", metavar="ID", help="export only this run")
    s.add_argument("--json", action="store_true")
    e = sub.add_parser("eval", help="golden-task evals").add_subparsers(dest="eval_cmd", required=True)
    s = e.add_parser("run", help="run a suite (evals/<name>.yaml or a path), optionally across revisions")
    s.add_argument("suite")
    s.add_argument("--revision", action="append", metavar="SHA|REF", help="repeat to compare revisions")
    s.add_argument("--timeout", type=float, default=600, metavar="SECONDS", help="per task")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out", metavar="FILE", help="write the markdown report here")
    _bench_parser(sub)
    s = sub.add_parser(
        "init", help="set up a fully local install: local-default model profile, artifacts dir, SQLite ledger"
    )
    s.add_argument("--backend", choices=("ollama", "lmstudio", "openrouter"), default="ollama")
    s.add_argument("--model-id", help="model name as the local server knows it (default from the example)")
    s.add_argument(
        "--base-url",
        help="OpenAI-compatible URL (default localhost); a non-loopback host enables allow_private_network",
    )
    s.add_argument("--force", action="store_true", help="overwrite an existing model-profiles/local-default.yaml")
    s.add_argument("--json", action="store_true")
    a = sub.add_parser("admin", help="bootstrap tokens and worker credentials (local, uses MAEF_DB directly)")
    admin_sub = a.add_subparsers(dest="admin_cmd", required=True)
    t = admin_sub.add_parser("token", help="API tokens").add_subparsers(dest="action", required=True)
    s = t.add_parser("create", help="print a new API token once")
    s.add_argument("--name", required=True)
    s.add_argument(
        "--scopes",
        required=True,
        metavar="a,b",
        help="comma list of: read, runs:write, decisions:write, authoring, admin (admin implies all)",
    )
    t.add_parser("list")
    t.add_parser("revoke").add_argument("id")
    w = admin_sub.add_parser("worker", help="worker credentials").add_subparsers(dest="action", required=True)
    w.add_parser("create", help="print a new worker credential once").add_argument("--id", required=True)
    w.add_parser("list")
    w.add_parser("revoke").add_argument("id")
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


def _bench_parser(sub) -> None:
    b = sub.add_parser("bench", help="benchmark task families (docs/BENCHMARKS.md)").add_subparsers(
        dest="bench_cmd", required=True
    )
    for name, helptext in (
        ("variant", "print the task for one seed and noise level"),
        ("run", "run one variant against an agent, then grade and report"),
        ("report", "grade and report an existing run"),
    ):
        s = b.add_parser(name, help=helptext)
        if name == "report":
            s.add_argument("run_id")
        s.add_argument("--family", default="bowling")
        s.add_argument("--seed", type=int, default=0)
        s.add_argument("--noise", type=int, default=0, help="0 = the canonical task; higher perturbs more")
        if name == "variant":
            s.add_argument("--json", action="store_true", help="print the variant and hidden cases")
        else:
            s.add_argument("--out", default="bench-results")
        if name == "run":
            s.add_argument("--agent", default="tdd-engineer")
            s.add_argument("--timeout", type=float, default=1800, metavar="SECONDS")


def _bench(client_factory, args) -> int:
    from .bench.families import FAMILIES

    family = FAMILIES.get(args.family)
    if family is None:
        _err(f"unknown family {args.family!r}; known: {', '.join(FAMILIES)}")
        return EXIT_ERROR
    if args.bench_cmd == "variant":
        v = family.sample(args.seed, args.noise)
        if args.json:
            print(json.dumps({"variant": v.model_dump(), "cases": family.hidden_cases(v)}, indent=2))
        else:
            print(family.render_task(v))
        return EXIT_OK
    from .bench import runner

    client = client_factory()
    if args.bench_cmd == "run":
        rep = runner.run_bench(
            client,
            family,
            args.seed,
            args.noise,
            args.agent,
            Path(args.out),
            args.timeout,
            on_event=lambda e: _err(_event_line(e)),
        )
    else:
        rep = runner.report_existing(client, args.run_id, family, args.seed, args.noise, Path(args.out))
    print((Path(args.out) / rep["run_id"] / "report.md").read_text(encoding="utf-8"))
    return EXIT_OK if rep["status"] == "succeeded" else EXIT_RUN_FAILED


def _budget(args) -> dict | None:
    b = {
        k: v
        for k, v in (
            ("max_tokens", args.max_tokens),
            ("max_turns", args.max_turns),
            ("max_duration_seconds", args.max_duration),
        )
        if v is not None
    }
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
        _out(
            args,
            preview,
            f"{preview['agent_id']}: {preview['model_profile']} / {preview['sandbox_profile']}, "
            f"budget {preview['effective_budget']}",
        )
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
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": final["status"],
                    "terminal_reason": final.get("terminal_reason"),
                    "tokens_used": final.get("tokens_used"),
                    "result": text,
                }
            )
        )
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
        print(
            json.dumps(
                {"run_id": run_id, "status": final["status"], "tokens_used": final.get("tokens_used"), **answers}
            )
            if args.json
            else json.dumps(answers, indent=2)
        )
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
        _out(
            args,
            drafts,
            "\n".join(f"{d['id']}\t{d['agent_id']}\t{d['branch']}\t{d.get('published') or '-'}" for d in drafts),
        )
    return EXIT_OK


def _permission_summary(p: dict) -> str:
    lines = ["permission changes:" if _has_changes(p) else "permission changes: none"]
    if p["new_agent"]:
        lines.append("  new agent")
    for label, key in (
        ("tools added", "tools_added"),
        ("tools removed", "tools_removed"),
        ("mcp servers added", "mcp_servers_added"),
        ("mcp servers removed", "mcp_servers_removed"),
        ("mcp servers with open network added", "mcp_network_open_added"),
    ):
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
    return p["new_agent"] or any(
        p[k]
        for k in (
            "tools_added",
            "tools_removed",
            "mcp_servers_added",
            "mcp_servers_removed",
            "network_open",
            "budget_increases",
            "runtime_changes",
            "mcp_new_secrets",
            "mcp_network_open_added",
        )
    )


def _init(args) -> int:
    """Local only: never contacts a server or cloud account."""
    from .localsetup import InitError, init_local

    try:
        report = init_local(args.backend, args.model_id, args.base_url, args.force)
    except (InitError, ValueError) as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_OK
    prof = report["profile"]
    print(f"model profile  {prof['path']} ({prof['action']})")
    print(f"artifacts      {report['artifacts']}")
    print(f"ledger         {report['ledger']}")
    print(f"oci runtime    {report['oci_runtime'] or 'none'}")
    for w in report["warnings"]:
        _err(f"warning: {w}")
    print(
        "next:\n  export MAEF_API_TOKEN=$(maef admin token create --name me --scopes read,runs:write)\n"
        "  export MAEF_WORKER_CREDENTIAL=$(maef admin worker create --id worker-1)\n"
        "  make api   # terminal 1\n  make worker   # terminal 2\n"
        "  maef run research-analyst 'your task'"
    )
    return EXIT_OK


def _admin(args) -> int:
    """Talks to the ledger DB directly (not over HTTP): this is the root of trust. Prints a token only at creation."""
    from .auth import (
        AuthError,
        issue_api_token,
        issue_worker_credential,
        local_ledger,
        parse_scopes,
    )

    ledger = local_ledger()
    who, kind, action = "local-cli", args.admin_cmd, args.action
    try:
        if kind == "token" and action == "create":
            scopes = parse_scopes(args.scopes)
            token_id, token = issue_api_token(ledger, args.name, scopes)
            ledger.audit(who, "token.create", f"token:{token_id}", "ok", "local")
            _err(f"created token {token_id} ({args.name}; scopes {','.join(scopes)}); it is shown once")
            print(token)
        elif kind == "token" and action == "list":
            for t in ledger.list_api_tokens():
                print(
                    f"{t['id']}\t{t['name']}\t{','.join(t['scopes'])}\t{t['created_at']}\t"
                    f"{'revoked ' + t['revoked_at'] if t['revoked_at'] else 'active'}"
                )
        elif kind == "token":
            ok = ledger.revoke_api_token(args.id)
            ledger.audit(who, "token.revoke", f"token:{args.id}", "ok" if ok else "not_found", "local")
            if not ok:
                _err(f"no active token {args.id}")
                return EXIT_ERROR
            _err(f"revoked {args.id}")
        elif action == "create":
            token = issue_worker_credential(ledger, args.id)
            ledger.audit(who, "worker.create", f"worker:{args.id}", "ok", "local")
            _err(f"created credential for worker {args.id}; it is shown once")
            print(token)
        elif action == "list":
            for c in ledger.list_worker_credentials():
                print(
                    f"{c['worker_id']}\t{c['created_at']}\t{'revoked ' + c['revoked_at'] if c['revoked_at'] else 'active'}"
                )
        else:
            count = ledger.revoke_worker(args.id)
            ledger.audit(who, "worker.revoke", f"worker:{args.id}", "ok" if count else "not_found", "local")
            if not count:
                _err(f"no active credential for worker {args.id}")
                return EXIT_ERROR
            _err(f"revoked {count} credential(s) of {args.id}")
    except AuthError as exc:
        _err(f"error: {exc}")
        return EXIT_ERROR
    return EXIT_OK


def _otel(client: MaefClient, args) -> int:
    from .models import RunDetail

    if not args.endpoint:
        _err("no endpoint: pass --endpoint or set MAEF_OTLP_ENDPOINT")
        return EXIT_ERROR
    try:
        exporter = otel.OtlpHttpExporter(args.endpoint, args.headers_ref, allow_private=args.allow_private)
        if args.run:
            run_ids = [args.run]
        else:
            run_ids = [
                r["id"]
                for r in client.list_runs()
                if r["status"] in TERMINAL_STATUSES and (not args.since or r["created_at"] >= args.since)
            ]
        exported = 0
        for run_id in run_ids:
            run = RunDetail.model_validate(client.get_run(run_id))
            if run.status not in TERMINAL_STATUSES:
                _err(f"skipping {run_id}: still {run.status}")
                continue
            exporter.export(otel.run_to_spans(run))
            exported += 1
    except otel.OtlpExportError as exc:
        _err(f"error: export failed: {exc}")
        return EXIT_ERROR
    _out(args, {"exported": exported}, f"exported {exported} run(s)")
    return EXIT_OK


def _eval(client: MaefClient, args) -> int:
    path = Path(args.suite)
    if not path.is_file():
        path = ROOT / "evals" / f"{args.suite}.yaml"
    try:
        suite = evals.load_suite(path)
    except (OSError, ValueError) as exc:
        _err(f"error: cannot load suite {args.suite}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
        return EXIT_ERROR
    results = []
    for revision in args.revision or [None]:
        _err(f"suite {suite.id} @ {revision or 'working tree'}")
        results.append(
            evals.run_suite(
                client,
                suite,
                revision=revision,
                timeout_seconds=args.timeout,
                on_task=lambda t: _err(
                    f"  {t.task_id}: {'pass' if t.passed else 'FAIL'} ({t.status}, {t.tokens_used} tokens)"
                ),
            )
        )
    comparison = evals.compare(results)
    report = comparison.to_markdown()
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        _err(f"wrote {args.out}")
    if args.json:
        print(
            json.dumps(
                {
                    "comparison": comparison.model_dump(mode="json"),
                    "results": [{**r.model_dump(mode="json"), "pass_rate": r.pass_rate} for r in results],
                }
            )
        )
    else:
        print(report)
    return EXIT_RUN_FAILED if results[-1].failed_tasks else EXIT_OK


def _dispatch(client: MaefClient, args) -> int:
    cmd = args.cmd
    if cmd == "agents":
        agents = client.list_agents()
        _out(args, agents, "\n".join(f"{a['id']}\t{a.get('display_name', '')}" for a in agents))
    elif cmd in ("preview", "run"):
        return _run(client, args)
    elif cmd == "decide":
        return _decide(client, args)
    elif cmd == "otel":
        return _otel(client, args)
    elif cmd == "eval":
        return _eval(client, args)
    elif cmd == "draft":
        return _draft(client, args)
    elif cmd == "status":
        run = client.get_run(args.run_id)
        run.pop("events", None)
        _out(
            args,
            run,
            f"{run['id']} {run['status']}" + (f" ({run['terminal_reason']})" if run.get("terminal_reason") else ""),
        )
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
        if args.cmd == "admin":
            return _admin(args)
        if args.cmd == "init":
            return _init(args)
        if args.cmd == "bench":
            return _bench(lambda: client or MaefClient(HttpTransport()), args)
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
