# Build diagnostics in the platform console

Open **Build details / logs** on a row in **My sources**. The panel shows the
source status separately from the latest CodeBuild status, its phases and
failure messages, and the most recent log events. A successful CodeBuild can
still leave a source `FAILED` (for example, a missing published graph); the
registry error remains visible in that case.

Use **Refresh** to fetch current state and reset to the newest log page.
**Earlier logs** walks backward through the same build. Diagnostics are fetched
on demand, without another model call or background log polling. Copying the
displayed diagnostics includes only the content already fetched.

The suggested checks are keyword-based troubleshooting hints, not a model
diagnosis or proof of root cause:

| Evidence | Suggested next action |
|---|---|
| Git authentication / repository not found | Check repository URL, ref, and PAT permissions with the source owner/operator. |
| Bedrock access / unsupported model | Check model access, inference profile and the build role; adjust AI extraction settings if appropriate. |
| OCR / document conversion failure | Inspect the named document/page; replace corrupt or password-protected input, or adjust the corpus and extraction settings. |
| Throttling / time limit / memory | Review quota, build timeout/compute, or split/prune the source. These infrastructure settings still require an operator. |
| Missing input / crawl failure | Check uploads, source URL and crawl permissions. |
| Snapshot / graph publication failure | Check size limits, S3 permissions and the published graph error before retrying. |

Files, AI extraction and crawl controls open the existing source editors,
subject to the same permissions as before. Rebuild uses the existing API.
No credentials or infrastructure settings are changed automatically.

## API and access boundary

`GET /repos/{repoId}/build` requires a Cognito token and an existing source
grant, including for a public source. Catalog viewers without a grant cannot
read build diagnostics. Disabled/deleted sources return 404.

The server resolves the current build ID from the registry, reads exactly one
build through `BatchGetBuilds`, and verifies both its project and `REPO_ID`
environment variable before fetching logs. It returns a selected metadata
projection, never the build environment, secrets, source credentials or full
CodeBuild response. The CloudWatch group must be
`/aws/codebuild/<projectName>` and the stream comes only from that build.

For an older page, send both `build_id` from the first response and
`next_token` from `logs.next_token`. A changed current build returns 409;
refresh and begin a new log view. Client-supplied log groups, stream names and
historical build selection are unsupported. Each response reads at most
100 log events, limits displayed messages to 3,000 characters, and reports
truncation. Empty pages may still carry an advancing backward cursor;
CloudWatch pagination ends when the cursor no longer advances.

| `logs.state` | Meaning |
|---|---|
| `not_started` | The source has no recorded build yet. A registry error may still describe a pre-build failure. |
| `available` | The log stream was read; it may have no events yet. |
| `pending` | A running build has not exposed its stream/events yet. |
| `missing` | Build history or a terminal build's log stream is absent, deleted or not configured. |
| `unavailable` | AWS denied or failed the read. Available phase/context data is retained; retry after correcting permissions or a transient outage. |

Known credential formats (authorization headers, token/password assignments,
API keys, AWS access IDs, JWTs, URL credentials/query strings and private-key
blocks) are redacted before display/truncation. This is best-effort masking:
arbitrary secrets printed by build tools cannot all be identified. Do not
print credentials in build scripts.

## Deployment and verification

Deploy the platform Lambda, its IAM policy, management route and console
assets together through the existing CDK stack. The Lambda needs
`codebuild:BatchGetBuilds` on the **project ARN** and `logs:GetLogEvents` only
on that project's log streams. The exact diagnostics route is throttled at
5 requests/second with a burst of 10. No new permanent compute service,
database, model extraction or source rebuild is required for this feature.

Offline tests use synthetic build metadata and fake AWS clients:

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_build_diagnostics.py'
```

A deployment still needs verification with a permitted failed/running source:
failure contexts and logs visible to a grant holder; denied for an unrelated
user; file/AI settings and rebuild reachable; stale build pagination resets
without mixing attempts. Local mocked UI checks and CDK synthesis cannot
establish live IAM or CloudWatch behavior.

AWS references:
[CodeBuild authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_codebuild.html)
and [GetLogEvents pagination](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetLogEvents.html).
