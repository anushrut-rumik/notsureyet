import json
from pathlib import Path

import pytest

from scripts.export_ai_logs import EXCLUDED_SESSIONS, Redactor, export


def write_session(home, project, session_id, items, archived=False, source="vscode"):
    folder = home / ("archived_sessions" if archived else "sessions")
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "session_meta", "payload": {
        "id": session_id, "cwd": str(project), "source": source,
        "timestamp": "2026-11-06T06:00:00Z",
    }}]
    for n, (turn, item) in enumerate(items):
        rows.append({"timestamp": f"2026-11-06T06:01:{n:02d}Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": session_id, "turn_id": turn,
            "item": {"id": f"{session_id}-{n}", **item},
        }})
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def prompt(text):
    return {"type": "UserMessage", "content": [{"type": "text", "text": text}]}


def reply(text):
    return {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": text}]}


def contents(output):
    return "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())


@pytest.mark.parametrize("blocked", ["please inspect RC-HARNESS", "please copy Ira", "what is ira's behavior?"])
def test_blocked_prompt_removes_whole_turn_and_quoted_replies(tmp_path, blocked):
    project, home, output = tmp_path / "project", tmp_path / "history", tmp_path / "out"
    project.mkdir()
    write_session(home, project, "chat", [
        ("bad", prompt("Initial allowed wording")),
        ("bad", reply("RESPONSE_TO_BE_REMOVED")),
        ("bad", prompt(blocked)),  # A correction arrives during an ongoing turn.
        ("bad", {"type": "CommandExecution", "command": ["REMOVED_COMMAND"], "status": "completed"}),
        ("good", prompt("Explain expiration and spiral behavior")),
        ("good", reply("Keep this answer")),
        ("good", reply(f"Earlier you asked: {blocked}")),
    ])
    manifest = export(project, home, output)
    text = contents(output)
    assert "Initial allowed wording" not in text
    assert "RESPONSE_TO_BE_REMOVED" not in text
    assert "REMOVED_COMMAND" not in text
    assert blocked not in text
    assert "Explain expiration and spiral behavior" in text
    assert "Keep this answer" in text
    assert manifest["counts"]["included_prompts"] == 1


def test_scope_archives_internal_data_and_request_exclusion(tmp_path):
    project, home, output = tmp_path / "project", tmp_path / "history", tmp_path / "out"
    project.mkdir()
    write_session(home, project, "archived", [
        ("turn", prompt("An archived project prompt")),
        ("turn", reply("An archived reply")),
        ("turn", {"type": "Reasoning", "raw_content": ["PRIVATE_REASONING"]}),
        ("turn", {"type": "CommandExecution", "command": ["python", "check.py"],
                  "status": "completed", "exit_code": 1, "aggregated_output": "RAW_PRIVATE_RESULT"}),
        ("turn", {"type": "McpToolCall", "server": "test", "tool": "read_thread", "status": "completed",
                  "arguments": {"secret": "PRIVATE_ARGUMENTS"}, "result": {"text": "PRIVATE_TOOL_RESULT"}}),
    ], archived=True)
    write_session(home, tmp_path / "other", "unrelated", [("a", prompt("OTHER_PROJECT"))])
    write_session(home, project, next(iter(EXCLUDED_SESSIONS)), [("a", prompt("CREATION_REQUEST"))])
    write_session(home, project, "internal", [("a", prompt("INTERNAL_AGENT"))], source={"subagent": {}})
    result = export(project, home, output)
    text = contents(output)
    for excluded in ("PRIVATE_REASONING", "RAW_PRIVATE_RESULT", "PRIVATE_ARGUMENTS", "PRIVATE_TOOL_RESULT",
                     "OTHER_PROJECT", "CREATION_REQUEST", "INTERNAL_AGENT"):
        assert excluded not in text
    assert "An archived project prompt" in text
    assert result["counts"]["included_tool_records"] == 2
    activity = [json.loads(line) for line in (output / "tool-activity.jsonl").read_text().splitlines()]
    assert activity[0]["exit_code"] == 1
    assert activity[0]["status"] == "completed"


def test_redacts_credentials_contacts_and_machine_paths():
    private_key = "0x" + "a1" * 32
    token = "sk-proj-" + "abC123" * 20
    redactor = Redactor(Path("/tmp/project"), ["old-secret-example"])
    text = redactor.text(
        f"{private_key} {token} old-secret-example "
        "https://example.com/?token=shortsecret API_KEY=anothersecret "
        "person@example.com +91 98765 43210 /tmp/project/agent/main.py"
    )
    for excluded in (private_key, token, "old-secret-example", "shortsecret", "anothersecret",
                     "person@example.com", "+91 98765 43210", "/tmp/project"):
        assert excluded not in text
    assert "<project>/agent/main.py" in text


def test_preserves_documentation_urls_while_redacting_query_credentials():
    redactor = Redactor(Path("/tmp/project"))
    url = "https://docs.example.com/developer-hub/building-on-example/compute-network/overview"
    path = "/tmp/project/integrations/lending-setup/public/index.html"
    assert redactor.text(url) == url
    assert redactor.text(path) == "<project>/integrations/lending-setup/public/index.html"
    assert redactor.text(url + "?token=shortsecret") == url + "?token=[REDACTED]"


def test_refresh_removes_stale_transcript_and_keeps_non_generated_files(tmp_path):
    project, home, output = tmp_path / "project", tmp_path / "history", tmp_path / "out"
    project.mkdir()
    write_session(home, project, "keep", [("turn", prompt("Keep this prompt"))])
    write_session(home, project, "change", [("turn", prompt("Old eligible prompt"))])
    export(project, home, output)
    (output / "personal-note.txt").write_text("Do not delete")
    write_session(home, project, "change", [("turn", prompt("Ira now makes this ineligible"))])
    export(project, home, output)
    assert "Old eligible prompt" not in contents(output)
    assert (output / "personal-note.txt").read_text() == "Do not delete"
    assert len(list((output / "sessions").glob("*.md"))) == 1
