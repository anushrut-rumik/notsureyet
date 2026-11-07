# Product prompts and AI logs

Available development history from 2026-11-06T11:46:34+05:30 through 2026-11-07T07:30:07+05:30.

This snapshot contains 197 user prompts, 589 AI messages and 2545 tool activity records across 37 chats.

## What is included

The transcripts contain eligible user prompts, question answers, AI progress updates and final replies. They preserve original wording except for clearly marked redactions. The source is local Codex history for this project's exact workspace, including archived chats and user-created side discussions.

- [All prompts and AI replies, in chronological JSONL](conversations.jsonl)
- [Tool activity, in chronological JSONL](tool-activity.jsonl)
- [Coverage, counts and source session IDs](manifest.json)

Tool activity records commands and their reported exit codes, edited file paths, tool names and web actions. The session and turn IDs connect those records to the conversation. An action being recorded does not prove that a deployment, transfer or user-facing outcome succeeded.

## Filtering and limits

The requested content exclusions are case-insensitive. If a user prompt matches, its entire turn is omitted, including associated responses and tool activity. Matching replies, titles and tool records are also filtered so excluded references do not return through quotations. The archive-creation chat is excluded.

Credentials, personal contact details and machine-specific home paths are redacted. Conservative credential filters also mask some public transaction hashes and long identifiers. Images are represented by attachment markers. No original source sessions were changed.

This is a filtered development record, not an unabridged export. It excludes internal model reasoning, system/developer context, internal agent sessions, raw tool result bodies, terminal stdout, file diffs and attachment contents. Those are not copied into the repository. It does not include the running product's customer chats or provider-side inference logs. Missing or deleted source history cannot be reconstructed. Historical AI claims are not newly verified by this export.

## Refreshing the snapshot

Run from the project root on a machine with its local Codex history:

```sh
python3 scripts/export_ai_logs.py
```

The same exclusions apply on every refresh. This is a snapshot through the latest available history, not a claim that product development has ended and not an automatic background recorder.

## Conversations

- 2026-11-06 · [Integrate 0G Compute](sessions/2026-11-06-01a1af80-5c7c-7d13-bab2-479328a9f11b.md) (1 prompts, 10 AI messages)
- 2026-11-06 · [Add Arkiv-backed Web3 agent features](sessions/2026-11-06-01a1afbb-bf7e-7473-8a5b-0c6cb984282e.md) (11 prompts, 35 AI messages)
- 2026-11-06 · [Add Arkiv-backed Web3 agent features](sessions/2026-11-06-01a1afd8-bc34-7b20-9115-fe2cde6bf6a9.md) (2 prompts, 1 AI messages)
- 2026-11-06 · [so currently the agnet is responding to / commands and what…](sessions/2026-11-06-01a1b001-aeb4-7470-b225-9b3df1c3f73d.md) (2 prompts, 11 AI messages)
- 2026-11-06 · [Locate Arkiv usage and storage](sessions/2026-11-06-01a1b01d-36dc-7a91-a252-19117b202942.md) (18 prompts, 46 AI messages)
- 2026-11-06 · [Implement CCIP cross-chain transfers](sessions/2026-11-06-01a1b045-dc1c-7862-bd65-08be11ab32b6.md) (16 prompts, 51 AI messages)
- 2026-11-06 · [Implement CCIP cross-chain transfers](sessions/2026-11-06-01a1b05d-17cc-7382-849c-3cf086daac4d.md) (3 prompts, 4 AI messages)
- 2026-11-06 · [Switch inference to GPT-6 Sol](sessions/2026-11-06-01a1b06e-970a-7f61-996b-44e6e31b777e.md) (3 prompts, 8 AI messages)
- 2026-11-06 · [Commit repository changes](sessions/2026-11-06-01a1b071-46d4-7660-9bf3-30faad8f1294.md) (1 prompts, 4 AI messages)
- 2026-11-06 · [Explain CCIP cross-chain flow](sessions/2026-11-06-01a1b07a-6249-75f2-8c16-867089e659e0.md) (2 prompts, 4 AI messages)
- 2026-11-06 · [Audit Arkiv transaction usage](sessions/2026-11-06-01a1b0b7-865f-75b3-987c-e9d6d54a3b21.md) (1 prompts, 4 AI messages)
- 2026-11-06 · [Show Arkiv usage to sponsors](sessions/2026-11-06-01a1b0c2-0ddb-7760-8253-8b0dc2233ddc.md) (3 prompts, 4 AI messages)
- 2026-11-06 · [Commit and push changes](sessions/2026-11-06-01a1b0c7-5859-7990-83d6-c838028e4975.md) (1 prompts, 5 AI messages)
- 2026-11-06 · [Commit the changes](sessions/2026-11-06-01a1b0e7-6c21-7c50-ac0c-8de1dd437eb2.md) (1 prompts, 3 AI messages)
- 2026-11-06 · [Remove .env.example](sessions/2026-11-06-01a1b106-47dc-77c3-874e-69865bdf33d8.md) (2 prompts, 4 AI messages)
- 2026-11-06 · [Add contextual emoji reactions](sessions/2026-11-06-01a1b109-9dff-74b3-a45f-e5e4c93dd98c.md) (1 prompts, 2 AI messages)
- 2026-11-06 · [Write warm WhatsApp messages](sessions/2026-11-06-01a1b130-eee7-7213-82ca-8c7c9b185be3.md) (2 prompts, 4 AI messages)
- 2026-11-06 · [Improve WhatsApp agent messages](sessions/2026-11-06-01a1b135-312f-78a1-9446-9831492e41d4.md) (2 prompts, 7 AI messages)
- 2026-11-06 · [Implement shared decision logic](sessions/2026-11-06-01a1b177-be07-7c42-a99a-09226a995b5b.md) (2 prompts, 8 AI messages)
- 2026-11-06 · [Implement Part 2](sessions/2026-11-06-01a1b18b-2f57-7d31-9ad5-bdf8ff49ea62.md) (18 prompts, 46 AI messages)
- 2026-11-06 · [Fix issues in part 2](sessions/2026-11-06-01a1b1f0-ecce-75a3-8b94-7dae6399fc1a.md) (7 prompts, 17 AI messages)
- 2026-11-06 · [Implement CRE-CCIP rebalance](sessions/2026-11-06-01a1b1fc-8bea-79a2-a43f-c7efa8007c27.md) (6 prompts, 16 AI messages)
- 2026-11-06 · [Implement CRE-CCIP rebalance](sessions/2026-11-06-01a1b214-a265-7a41-b311-64fe2f65e835.md) (4 prompts, 7 AI messages)
- 2026-11-07 · [Implement Part 4](sessions/2026-11-07-01a1b221-b042-75a3-9b36-30ad2308f5bf.md) (25 prompts, 89 AI messages)
- 2026-11-07 · [Implement Part 4](sessions/2026-11-07-01a1b231-a065-72c0-b4b4-e1b5787ff840.md) (1 prompts, 1 AI messages)
- 2026-11-07 · [Development session](sessions/2026-11-07-01a1b2cb-c2ac-7262-9d7b-a5ab4ccdf2bb.md) (12 prompts, 38 AI messages)
- 2026-11-07 · [Fix repeated strategy responses](sessions/2026-11-07-01a1b2f3-39af-7d12-bea6-1fcbec7ae103.md) (8 prompts, 31 AI messages)
- 2026-11-07 · [Locate Arkiv liquidity pool usage](sessions/2026-11-07-01a1b309-f209-7da2-bd7d-8192bacbfe44.md) (5 prompts, 11 AI messages)
- 2026-11-07 · [Development session](sessions/2026-11-07-01a1b32e-1370-7f32-acc1-51892aa837a1.md) (7 prompts, 24 AI messages)
- 2026-11-07 · [Explain the deployment plan](sessions/2026-11-07-01a1b348-1208-7840-b119-9dc54a17d8d3.md) (3 prompts, 5 AI messages)
- 2026-11-07 · [Development session](sessions/2026-11-07-01a1b34c-9a76-7fb0-b8aa-d1054c93b608.md) (4 prompts, 6 AI messages)
- 2026-11-07 · [Resume CRE pool fix](sessions/2026-11-07-01a1b364-f5c5-7483-a93c-e49b1f7d1059.md) (8 prompts, 40 AI messages)
- 2026-11-07 · [Resume CRE pool fix](sessions/2026-11-07-01a1b36a-8f10-7861-a84e-974314ce7aab.md) (6 prompts, 11 AI messages)
- 2026-11-07 · [Integrate Arkiv with the CRE pool](sessions/2026-11-07-01a1b37b-b20b-7280-8140-75b14bc8c302.md) (5 prompts, 21 AI messages)
- 2026-11-07 · [Check CRE 24-hour cooldown](sessions/2026-11-07-01a1b39c-b152-78e3-91e3-fe03450cf992.md) (1 prompts, 3 AI messages)
- 2026-11-07 · [Assess CRE deployment impact](sessions/2026-11-07-01a1b39e-078b-7131-8c98-3b7fe5cef996.md) (2 prompts, 5 AI messages)
- 2026-11-07 · [Write clear project descriptions](sessions/2026-11-07-01a1b3ba-f33e-7a50-bce1-bc33c13ca0ae.md) (1 prompts, 3 AI messages)
