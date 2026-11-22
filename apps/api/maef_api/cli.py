"""`maef` command: thin argparse front-end over MaefClient. Exit codes: 0 ok, 1 run failed/cancelled,
2 usage or API error, 3 timeout."""
import argparse
import json
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
    run = client.submit(args.agent, instruction, _budget(args))
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


def _dispatch(client: MaefClient, args) -> int:
    cmd = args.cmd
    if cmd == "agents":
        agents = client.list_agents()
        _out(args, agents, "\n".join(f"{a['id']}\t{a.get('display_name', '')}" for a in agents))
    elif cmd in ("preview", "run"):
        return _run(client, args)
    elif cmd == "decide":
        return _decide(client, args)
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
