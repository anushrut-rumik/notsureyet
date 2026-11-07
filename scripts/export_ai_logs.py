#!/usr/bin/env python3
"""Export filtered, user-visible development history from local Codex sessions.

This reads local history only. It never calls a model or changes source sessions.
"""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
EXCLUDED = re.compile(r"rc-harness|\bira\b", re.IGNORECASE)
# The request to create this archive, and work performed for that request.
EXCLUDED_SESSIONS = {"01a1b3b9-fad4-72f3-88ae-7757b88714be"}
IST = ZoneInfo("Asia/Kolkata")
CONTEXT_PREFIXES = (
    "# AGENTS.md instructions", "<environment_context>",
    "<external_codex_apps_", "<turn_aborted>",
)


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                # Fail closed instead of publishing a silently incomplete archive.
                raise ValueError(f"Invalid JSON in {path.name}, line {number}") from error


def message_text(item):
    return "\n\n".join(
        block["text"] for block in item.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def display_prompt(text):
    """Make an answer from the question UI readable without changing its wording."""
    match = re.fullmatch(
        r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*",
        text, re.DOTALL,
    )
    if not match:
        return text
    try:
        answers = json.loads(match[1])
        rendered = []
        for answer in answers:
            value = answer.get("answer", answer.get("response"))
            if value is None:
                return text
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            rendered.append(f"Question: {answer.get('question', '')}\n\nAnswer: {value}")
        return "\n\n".join(rendered)
    except (ValueError, TypeError, AttributeError):
        return text


class Redactor:
    def __init__(self, project, secrets=()):
        self.project = str(project)
        self.secrets = sorted(set(secrets), key=len, reverse=True)
        self.counts = Counter()

    def text(self, text):
        def replace(pattern, replacement, label, flags=0):
            nonlocal text
            text, count = re.subn(pattern, replacement, text, flags=flags)
            self.counts[label] += count

        for secret in self.secrets:
            if len(secret) >= 6:
                replace(re.escape(secret), "[REDACTED CREDENTIAL]", "known_credential")
        text = text.replace(self.project, "<project>")
        text = text.replace(str(Path.home()), "<home>")
        replace(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                "[REDACTED PRIVATE KEY]", "private_key", re.DOTALL)
        replace(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
                r"AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{20,})\b",
                "[REDACTED CREDENTIAL]", "credential_pattern")
        replace(r"(?i)([?&#](?:access_token|api_key|apikey|token|key|secret|signature|sig|password)=)[^\s&#\"'<>]+",
                r"\1[REDACTED]", "url_credential")
        replace(r"(?i)(\b(?:authorization\s*[:=]\s*|bearer\s+))[^\s\"'<>]+",
                r"\1[REDACTED]", "authorization")
        replace(r"(?im)(\b[A-Z_]*(?:API_KEY|PRIVATE_KEY|PASSWORD|SECRET|ACCESS_TOKEN|AUTH_TOKEN|"
                r"REVIEW_TOKEN|ENCRYPTION_KEY)[A-Z_]*\s*[=:]\s*[\"']?)[^\s\"',;}]+",
                r"\1[REDACTED]", "credential_assignment")
        # Unknown old credentials can no longer be recovered from the current .env.
        # These conservative rules also mask some public hashes and long identifiers.
        replace(r"(?<![A-Za-z0-9])(?:0x)?[a-fA-F0-9]{64}(?![A-Za-z0-9])",
                "[REDACTED 256-BIT VALUE]", "hex_value")
        replace(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={1,2}(?![A-Za-z0-9+/=])",
                "[REDACTED ENCODED VALUE]", "encoded_value")
        # Treat slashes as path separators here, so ordinary documentation URLs
        # and repository paths survive. Padded base64 is handled above.
        replace(r"(?<![A-Za-z0-9_+-])[A-Za-z0-9_+-]{48,}(?![A-Za-z0-9_+-])",
                "[REDACTED LONG VALUE]", "long_value")
        replace(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
                "[REDACTED EMAIL]", "email")
        replace(r"(?<![\w])\+\d[\d ()-]{8,}\d(?![\w])", "[REDACTED PHONE]", "phone")
        replace(r"\b\d{9,15}(?:@(?:s\.whatsapp\.net|c\.us))?\b", "[REDACTED NUMBER]", "phone_or_id")
        return text

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key): self.value(item) for key, item in value.items()}
        return value


def project_secrets(project):
    """Read project credentials solely for exact-match redaction; never print them."""
    values = []
    for path in project.glob(".env*"):
        if not path.is_file() or path.name.endswith(".example"):
            continue
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and re.search(r"key|token|secret|password|credential", key, re.I):
                value = value.strip().strip("\"'")
                if len(value) >= 6:
                    values.append(value)
    return values


def tool_record(item):
    """Export activity metadata, without raw result bodies or copied file contents."""
    kind = item["type"]
    record = {"kind": kind, "status": item.get("status", "recorded")}
    if kind == "CommandExecution":
        record.update(command=item.get("command"), exit_code=item.get("exit_code"))
    elif kind == "FileChange":
        record["files"] = [
            {"path": path, "change": change.get("type")}
            for path, change in item.get("changes", {}).items()
        ]
    elif kind == "McpToolCall":
        record.update(server=item.get("server"), tool=item.get("tool"))
    elif kind == "Extension":
        record.update(tool=item.get("kind"), query=item.get("query"), action=item.get("action"))
    elif kind == "SubAgentActivity":
        record.update(action=item.get("kind"), agent=item.get("agent_path"))
    else:
        return None
    return record


def collect_session(path, meta, redactor, stats):
    rows = list(read_jsonl(path))
    visible = []
    models = set()
    for row in rows:
        payload = row.get("payload", {})
        if row.get("type") == "turn_context" and payload.get("model"):
            models.add(payload["model"])
        if row.get("type") == "event_msg" and payload.get("type") == "item_completed":
            if payload.get("thread_id") == meta["id"]:
                visible.append((row, payload, payload["item"]))

    blocked_turns = set()
    prompt_turns = set()
    for _, payload, item in visible:
        if item["type"] == "UserMessage":
            prompt = message_text(item)
            if prompt.startswith(CONTEXT_PREFIXES):
                continue
            prompt_turns.add(payload["turn_id"])
            stats["source_prompts"] += 1
            if EXCLUDED.search(prompt):
                blocked_turns.add(payload["turn_id"])
                stats["matching_prompts_excluded"] += 1
    stats["excluded_turns"] += len(blocked_turns)

    messages, activity = [], []
    seen = set()
    for row, payload, item in visible:
        turn = payload["turn_id"]
        if turn in blocked_turns or turn not in prompt_turns or item["id"] in seen:
            continue
        seen.add(item["id"])
        base = {
            "session_id": meta["id"], "turn_id": turn,
            "item_id": item["id"], "timestamp": row["timestamp"],
        }
        if item["type"] in {"UserMessage", "AgentMessage"}:
            text = message_text(item)
            if text.startswith(CONTEXT_PREFIXES):
                continue
            # Do not reintroduce excluded references through replies or quotations.
            if EXCLUDED.search(text):
                stats["additional_messages_excluded"] += 1
                continue
            is_user = item["type"] == "UserMessage"
            if is_user:
                text = display_prompt(text)
            attachments = sum(block.get("type") not in {"text", "Text"} for block in item.get("content", []))
            if attachments:
                text += f"\n\n[{attachments} attachment(s) omitted from this text archive.]"
            if not text.strip():
                continue
            messages.append({**base, "role": "user" if is_user else "assistant",
                             "phase": item.get("phase"), "text": redactor.text(text)})
        else:
            record = tool_record(item)
            if record is None:
                continue
            if EXCLUDED.search(json.dumps(record, ensure_ascii=False)):
                stats["tool_records_excluded"] += 1
                continue
            activity.append({**base, **redactor.value(record)})
    return messages, activity, sorted(models)


def local_time(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(IST).isoformat(timespec="seconds")


def fence(text):
    longest = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}text\n{text}\n{marker}\n"


def export(project, codex_home, output):
    stats = Counter()
    redactor = Redactor(project, project_secrets(project))
    titles = {}
    index = codex_home / "session_index.jsonl"
    if index.exists():
        for row in read_jsonl(index):
            titles[row["id"]] = row.get("thread_name", "Development session")
    sessions = {}
    for folder in ("sessions", "archived_sessions"):
        for path in sorted((codex_home / folder).rglob("*.jsonl")):
            first = next(read_jsonl(path), {})
            meta = first.get("payload", {})
            if first.get("type") != "session_meta" or meta.get("cwd") != str(project):
                continue
            if meta["id"] in EXCLUDED_SESSIONS:
                stats["request_sessions_excluded"] += 1
                continue
            if isinstance(meta.get("source"), dict):
                stats["internal_agent_sessions_excluded"] += 1
                continue
            if meta["id"] in sessions:
                raise ValueError("Duplicate source session; reconcile source copies before export")
            stats["source_sessions"] += 1
            messages, activity, models = collect_session(path, meta, redactor, stats)
            if not messages:
                stats["sessions_without_eligible_messages"] += 1
                continue
            title = titles.get(meta["id"], "Development session")
            if EXCLUDED.search(title):
                title = "Development session"
            sessions[meta["id"]] = {
                "id": meta["id"], "title": redactor.text(title), "started_at": meta["timestamp"],
                "archived_source": folder == "archived_sessions", "models": models,
                "messages": messages, "activity": activity,
            }
    sessions = sorted(sessions.values(), key=lambda session: session["started_at"])
    if not sessions:
        raise ValueError("No eligible project history found; existing export left untouched")

    all_messages = sorted([m for s in sessions for m in s["messages"]], key=lambda m: m["timestamp"])
    all_activity = sorted([a for s in sessions for a in s["activity"]], key=lambda a: a["timestamp"])
    stats.update(included_sessions=len(sessions), included_prompts=sum(m["role"] == "user" for m in all_messages),
                 included_ai_messages=sum(m["role"] == "assistant" for m in all_messages),
                 included_tool_records=len(all_activity))
    generated_at = datetime.now(UTC).isoformat()
    coverage = {"from": all_messages[0]["timestamp"], "through": all_messages[-1]["timestamp"]}
    files = {}
    session_index = []
    for session in sessions:
        filename = f"sessions/{local_time(session['started_at'])[:10]}-{session['id']}.md"
        title = session["title"].replace("\n", " ")
        lines = [f"# {title}\n", f"Session: `{session['id']}`\n",
                 "Historical transcript. Statements describe what was reported at the time.\n"]
        for message in session["messages"]:
            label = "User prompt" if message["role"] == "user" else "AI response"
            if message.get("phase") == "commentary":
                label = "AI progress update"
            lines.extend([f"## {local_time(message['timestamp'])} — {label}\n", fence(message["text"])])
        files[filename] = "\n".join(lines)
        session_index.append({k: v for k, v in session.items() if k not in {"messages", "activity"}} | {
            "file": filename, "prompts": sum(m["role"] == "user" for m in session["messages"]),
            "ai_messages": sum(m["role"] == "assistant" for m in session["messages"]),
            "tool_records": len(session["activity"]),
        })
    for filename, records in (("conversations.jsonl", all_messages), ("tool-activity.jsonl", all_activity)):
        files[filename] = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    lines = [
        "# Product prompts and AI logs\n",
        f"Available development history from {local_time(coverage['from'])} "
        f"through {local_time(coverage['through'])}.\n",
        f"This snapshot contains {stats['included_prompts']} user prompts, {stats['included_ai_messages']} AI messages "
        f"and {stats['included_tool_records']} tool activity records across {len(sessions)} chats.\n",
        "## What is included\n",
        "The transcripts contain eligible user prompts, question answers, AI progress updates and final replies. "
        "They preserve original wording except for clearly marked redactions. The source is local Codex history "
        "for this project's exact workspace, including archived chats and user-created side discussions.\n",
        "- [All prompts and AI replies, in chronological JSONL](conversations.jsonl)\n"
        "- [Tool activity, in chronological JSONL](tool-activity.jsonl)\n"
        "- [Coverage, counts and source session IDs](manifest.json)\n",
        "Tool activity records commands and their reported exit codes, edited file paths, tool names and web actions. "
        "The session and turn IDs connect those records to the conversation. An action being recorded does not "
        "prove that a deployment, transfer or user-facing outcome succeeded.\n",
        "## Filtering and limits\n",
        "The requested content exclusions are case-insensitive. If a user prompt matches, its entire turn is omitted, "
        "including associated responses and tool activity. Matching replies, titles and tool records are also filtered "
        "so excluded references do not return through quotations. The archive-creation chat is excluded.\n",
        "Credentials, personal contact details and machine-specific home paths are redacted. Conservative credential "
        "filters also mask some public transaction hashes and long identifiers. Images are represented by attachment "
        "markers. No original source sessions were changed.\n",
        "This is a filtered development record, not an unabridged export. It excludes internal model reasoning, "
        "system/developer context, internal agent sessions, raw tool result bodies, terminal stdout, file diffs and "
        "attachment contents. Those are not copied into the repository. It does not include the running product's "
        "customer chats or provider-side inference logs. Missing or deleted source history cannot be reconstructed. "
        "Historical AI claims are not newly verified by this export.\n",
        "## Refreshing the snapshot\n",
        "Run from the project root on a machine with its local Codex history:\n\n"
        "```sh\npython3 scripts/export_ai_logs.py\n```\n",
        "The same exclusions apply on every refresh. This is a snapshot through the latest available history, "
        "not a claim that product development has ended and not an automatic background recorder.\n",
        "## Conversations\n",
    ]
    for session in session_index:
        title = session["title"].replace("\n", " ").replace("[", "(").replace("]", ")")
        lines.append(f"- {local_time(session['started_at'])[:10]} · [{title}]({session['file']}) "
                     f"({session['prompts']} prompts, {session['ai_messages']} AI messages)")
    files["README.md"] = "\n".join(lines) + "\n"
    manifest = {"schema_version": 1, "generated_at": generated_at, "timezone": "Asia/Kolkata",
                "coverage": coverage, "counts": dict(stats), "redactions": dict(redactor.counts),
                "sessions": session_index, "generated_files": sorted([*files, "manifest.json"])}
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    for filename, content in files.items():
        if EXCLUDED.search(content):
            raise ValueError(f"Exclusion verification failed for {filename}; nothing written")
        if any(secret in content for secret in redactor.secrets if len(secret) >= 6):
            raise ValueError(f"Credential verification failed for {filename}; nothing written")
    output.mkdir(parents=True, exist_ok=True)
    previous_manifest = output / "manifest.json"
    stale = []
    if previous_manifest.exists():
        previous = json.loads(previous_manifest.read_text())
        for filename in previous.get("generated_files", []):
            path = (output / filename).resolve()
            if not path.is_relative_to(output.resolve()):
                raise ValueError("Unsafe output path in previous manifest")
            if filename not in files:
                stale.append(path)
    for filename, content in files.items():
        target = output / filename
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("Unsafe output path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for path in stale:
        path.unlink(missing_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--output", type=Path, default=PROJECT / "docs" / "ai-logs")
    args = parser.parse_args()
    manifest = export(PROJECT, args.codex_home, args.output)
    print(json.dumps({"output": str(args.output), **manifest["counts"]}, indent=2))


if __name__ == "__main__":
    main()
