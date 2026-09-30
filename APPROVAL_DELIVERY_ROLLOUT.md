# Slack interactive approval delivery — rollout gate

This patch is **not** a production deployment. It changes only approval/clarify delivery and gateway lifecycle command classification. The Engineering-Lead pilot, profiles, privileges, and `approvals.mode` are out of scope.

## Evidence and limits

- Session record for both incidents targeted channel `C0B83D9QH71`, root thread `1789630724.603929`. The first `clarify` call at 2026-09-29 21:17:28 UTC returned a timeout result at 21:48:36. The second terminal request at 22:03:11 was blocked at 22:04:12. The command did not execute. Neither card has a recorded Slack message `ts` in inspected `gateway.log` or `state.db`; neither actual post nor human visibility can be established from those records. Ordinary final-response ledger entries are not card ACKs. The interval for the first stored call/result is shorter than the result's “60m” string; no explanation was found.
- A separate later approval card was apparently visible promptly; Denny clicked it ten minutes later because he was away from the laptop. That is an expired human response, not evidence of a delayed Slack post.

## Decision semantics

- Slack `ok:false`: definitive failure; no successful interactive send. `ok` not false but no `ts`, or a transport timeout/connection loss without a confirmed response: **ambiguous** (possibly posted), not a successful ACK. Never auto-repost it. A nonempty `ts` with no explicit rejection: platform ACK, **not** proof the human saw a card.
- On definitive native-card failure, the existing text fallback may run once unless a connector egress guard declined. If fallback also fails definitively, notify raises and the central approval queue entry is removed; the command stays blocked. Any ambiguous send keeps its existing bounded decision waiter and avoids duplicates. A declined destination never gets a text fallback.
- The help-only lifecycle command fixture is classification-only: it never executes a restart. An actual lifecycle invocation remains approval-gated.

## Production gate (requires separate explicit Denny approval)

1. Review PR and CI, select exact commit and deployment window. Capture currently deployed commit/image digest and config hash, active gateway PID, health and Slack connection. Back up the current deploy artifact. Do not modify the Engineering-Lead pilot or any profile/authorization setting.
2. Deploy only the reviewed commit through the supported worker-01/Coolify release procedure. An explicitly approved gateway restart/recreate may be necessary to load Python code; do not do it in this work item without that gate. Preserve data volumes. Do not delete state, sessions, logs, or `.env`.
3. Check one healthy gateway PID, expected commit, connected Slack adapter, unchanged profile/routes/approval settings and normal message delivery. Fail closed and roll back if any check fails.
4. With Denny present, he posts a nonce-bearing request **in thread `C0B83D9QH71/1789630724.603929`** for one harmless two-option `clarify` card. Confirm a new adapter log line with channel, thread root, and returned message `ts`, then have Denny explicitly confirm he sees it and click promptly. Check the resulting tool response and no duplicate card. Only this human confirmation establishes visibility; ACK alone does not. Do not trigger an actual dangerous command or restart for the test.
5. If no card appears, if `ts` is absent, or if the response times out, stop the test and collect sanitized diagnostics. Do not resend automatically. Roll back the deployed image/commit to the captured artifact via the supported release mechanism, recheck PID/health/routes, and report a blocker. The rollback itself requires the approved production window and must not touch persistent volumes.
