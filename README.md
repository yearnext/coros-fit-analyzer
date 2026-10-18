# COROS FIT Analyzer — hosted MCP service

This is the cloud-runtime companion for the existing private `coros-fit-analyzer` plugin. The currently uploaded package uses a local stdio process; uploading it to an account does not deploy that process. This service exposes the FIT parser over MCP Streamable HTTP so ChatGPT can call it from a conversation.

## Deploy to Render

1. Put this directory in a Git repository connected to Render, then create a Web Service from the repository using the included `Dockerfile` / `render.yaml`.
2. Use the service's public HTTPS hostname. The MCP endpoint is `https://<service-host>/mcp`; health is `https://<service-host>/healthz`.
3. Once deployed, verify `/healthz`, then call `server_health` and `tools/list` through an MCP client. After the endpoint has been tested, update both `mcp.json` and `.mcp.json` in the private plugin to use that exact endpoint with `type: streamable-http`, bump its version, and publish the plugin update.
4. Only after the endpoint passes a real parser call should the plugin update be treated as live.

## FIT parsing workflow

- Call COROS `queryActivityFitFileDownloadUrls` for the chosen activity, then pass the returned HTTPS URL to `parse_fit_file_url`.
- The server defaults to accepting only `*.coros.com` HTTPS downloads and rejects non-public DNS destinations. If COROS changes its download domain or redirects to a separate CDN, add only the observed trusted suffix to `COROS_ALLOWED_HOST_SUFFIXES` in the service environment.
- The initial parse returns the actual FIT message counts, record count, timestamp coverage, sampling gaps, field inventory, summaries, and a dataset ID. It includes a short first/last-row preview rather than tens of thousands of records in one response.
- Use `get_fit_record_chunk(dataset_id, start_index, count)` to inspect raw record rows; count is capped at 500. The server keeps parsed rows in memory for 20 minutes; after a restart/expiry, parse again.

## Why URL parsing exists

A FIT file around 580 KiB becomes roughly 775 KiB in Base64. Passing that as text through a model call is wasteful and can exceed practical request/context limits. The server fetches the signed COROS download URL itself, checks the response size and FIT CRC, then returns actual parse metadata and paged records.

## Security notes

- No FIT files are persisted to disk or uploaded to a third-party analytics API; decoded records are held in process memory temporarily.
- File downloads are HTTPS-only and domain-allowlisted by default; redirects are revalidated.
- The service is a public MCP endpoint without user-specific authentication in this draft. Before broadly sharing it or placing sensitive tools on the same host, configure host-level access control / OAuth. Keep it private and only expose the FIT parsing functions.
- The 80 MiB input limit and 500-record page limit are enforced server-side.

## Local validation

Run `python -m py_compile server.py` for syntax validation. Full runtime tests require installing packages from `requirements.txt`; the current ChatGPT analysis container may not have Internet access for those packages.
