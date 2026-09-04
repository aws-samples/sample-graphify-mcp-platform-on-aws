/**
 * Streaming playground chat: Lambda Function URL (RESPONSE_STREAM) -> SSE.
 *
 * Same contract as the buffered POST /playground/chat (one model call per
 * request; the browser drives the tool-use loop), but the model's output
 * streams to the client as Server-Sent Events:
 *
 *   data: {"type":"delta","text":"..."}          text tokens as they generate
 *   data: {"type":"thinking","text":"..."}       reasoning deltas (thinking models)
 *   data: {"type":"tool_use","id","name","input"} complete tool call blocks
 *   data: {"type":"tool_result","tool_use_id","name","content","is_error"}
 *   data: {"type":"final", ...buffered-response-shape}
 *   data: {"type":"error","message"}             mid-stream failure
 *
 * The Function URL is AuthType AWS_IAM and is reached only through CloudFront
 * (OAC, SigV4) — it is never world-accessible. CloudFront's OAC owns the
 * Authorization header for its own signature, so the caller's Cognito ID token
 * rides in X-Graphify-Id; THIS CODE verifies it (aws-jwt-verify: signature +
 * iss + client_id + token_use) BEFORE anything else and uses `sub` for the
 * per-user budget AND for MCP access: the user must hold a grant on the
 * chosen server (the hub is open to all), and tool traffic goes to the in-VPC
 * proxy Lambda by direct invoke — no API key is involved. API Gateway streaming is not an option — HTTP API Lambda
 * integrations are buffered.
 */

import { AnthropicBedrock } from '@anthropic-ai/bedrock-sdk';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import { DynamoDBClient, GetItemCommand, UpdateItemCommand } from '@aws-sdk/client-dynamodb';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';

const REGION = process.env.AWS_REGION;
const MCP_PROXY_FN = process.env.MCP_PROXY_FN;
const REGISTRY_TABLE = process.env.REGISTRY_TABLE;
const ALLOWED_MODELS = process.env.ALLOWED_MODELS.split(',').map((s) => s.trim()).filter(Boolean);
const DEFAULT_MODEL = process.env.DEFAULT_MODEL || ALLOWED_MODELS[0];
const PLATFORM_TABLE = process.env.PLATFORM_TABLE;
const DAILY_TOKEN_BUDGET = parseInt(process.env.DAILY_TOKEN_BUDGET || '20000000', 10);
// The stream is served from its own CloudFront distribution, so browser calls
// are cross-origin to the console — reflect the allowed origins for CORS.
const ALLOWED_ORIGINS = new Set([process.env.CONSOLE_ORIGIN, 'http://localhost:8787'].filter(Boolean));

function corsHeaders(event) {
  const origin = Object.entries(event.headers || {}).find(([k]) => k.toLowerCase() === 'origin')?.[1] || '';
  const allow = ALLOWED_ORIGINS.has(origin) ? origin : (process.env.CONSOLE_ORIGIN || '');
  return {
    'Access-Control-Allow-Origin': allow,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'content-type, x-graphify-id, x-amz-content-sha256',
    'Access-Control-Max-Age': '3600',
    Vary: 'Origin',
  };
}

// One attempt per request: a Bedrock-side retry would re-bill the generation
// and the client's rollback/resend already covers transient failures.
const anthropic = new AnthropicBedrock({ awsRegion: REGION, maxRetries: 0, timeout: 240_000 });
const ddb = new DynamoDBClient({ region: REGION });
const lambda = new LambdaClient({ region: REGION, maxAttempts: 1 });
// ID token (not access): it carries the same `sub` and is what a browser
// Cognito login already holds; verified for aud=clientId.
const verifier = CognitoJwtVerifier.create({
  userPoolId: process.env.USER_POOL_ID,
  tokenUse: 'id',
  clientId: process.env.USER_POOL_CLIENT_ID,
});

const SERVER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$/;
const TOOL_NAME_RE = /^[a-zA-Z0-9_-]{1,128}$/;
const MCP_METHODS = new Set(['initialize', 'ping', 'tools/list', 'tools/call']);
const MAX_TOOLS = 48;
const MAX_MESSAGES = 60;
const MAX_TOKENS_CAP = 8192;
const MAX_SYSTEM_CHARS = 4000;
const MAX_TOOL_RESULT_CHARS = 16_000;
const MAX_BODY_CHARS = 1_000_000;

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

// ---------------------------------------------------------------------------
// validation (mirrors lambdas/playground/handler.py)
// ---------------------------------------------------------------------------

function parseBody(event) {
  let raw = event.body || '{}';
  if (event.isBase64Encoded) raw = Buffer.from(raw, 'base64').toString('utf8');
  if (raw.length > MAX_BODY_CHARS) throw new ApiError(413, `request body too large (> ${MAX_BODY_CHARS} chars)`);
  let parsed;
  try { parsed = JSON.parse(raw); } catch { parsed = null; }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new ApiError(400, 'request body must be a JSON object');
  }
  return parsed;
}

function sanitizeMessages(raw) {
  if (!Array.isArray(raw) || raw.length === 0) throw new ApiError(400, 'messages must be a non-empty list');
  if (raw.length > MAX_MESSAGES) throw new ApiError(400, `conversation too long (> ${MAX_MESSAGES} messages) — start a new one`);
  return raw.map((m) => {
    if (!m || typeof m !== 'object' || !['user', 'assistant'].includes(m.role)) {
      throw new ApiError(400, 'each message needs role user|assistant');
    }
    if (typeof m.content !== 'string' && !Array.isArray(m.content)) {
      throw new ApiError(400, 'message content must be a string or a block list');
    }
    return { role: m.role, content: m.content };
  });
}

function anthropicTools(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [], seen = new Set();
  for (const t of raw.slice(0, MAX_TOOLS)) {
    if (!t || typeof t !== 'object') continue;
    const name = String(t.name || '');
    if (!TOOL_NAME_RE.test(name) || seen.has(name)) continue;
    seen.add(name);
    let schema = t.inputSchema || t.input_schema || {};
    if (!schema || typeof schema !== 'object' || schema.type !== 'object') schema = { type: 'object' };
    out.push({ name, description: String(t.description || '').slice(0, 1500), input_schema: schema });
  }
  return out;
}

// ---------------------------------------------------------------------------
// per-user daily token budget (same table rows as the buffered Lambda)
// ---------------------------------------------------------------------------

const budgetKey = (sub, day) => ({ pk: { S: `USAGE#PLAYGROUND#${sub}` }, sk: { S: `D#${day}` } });

async function budgetCheck(sub) {
  const day = new Date().toISOString().slice(0, 10);
  const item = (await ddb.send(new GetItemCommand({ TableName: PLATFORM_TABLE, Key: budgetKey(sub, day) }))).Item;
  const used = parseInt(item?.tokens?.N || '0', 10);
  if (used >= DAILY_TOKEN_BUDGET) {
    throw new ApiError(429, `playground daily token budget exhausted (${DAILY_TOKEN_BUDGET} tokens/day) — resets at 00:00 UTC`);
  }
  return day;
}

async function budgetRecord(sub, day, tokens) {
  try {
    await ddb.send(new UpdateItemCommand({
      TableName: PLATFORM_TABLE,
      Key: budgetKey(sub, day),
      UpdateExpression: 'ADD tokens :n, req :one SET #ttl = :ttl',
      ExpressionAttributeNames: { '#ttl': 'ttl' },
      ExpressionAttributeValues: {
        ':n': { N: String(tokens) },
        ':one': { N: '1' },
        ':ttl': { N: String(Math.floor(Date.now() / 1000) + 90 * 86400) },
      },
    }));
  } catch (e) { console.log(`budget_record failed: ${e.name}: ${e.message}`); }
}

// ---------------------------------------------------------------------------
// MCP bridge: console identity -> grant check -> in-VPC proxy Lambda
// ---------------------------------------------------------------------------

// The signed-in user (Cognito sub) may reach the hub ("all": merged PUBLIC
// graph) and any server they hold a grant on — owner, subscriber or member,
// i.e. exactly the rows the console's MCP Servers tab lists. No API key is
// involved; the proxy is invoked directly with a synthesized authorizer
// context (kid "playground"), like the console's source viewer does.
async function authorizeServer(sub, serverId) {
  if (!SERVER_ID_RE.test(serverId)) throw new ApiError(400, 'server_id is invalid');
  if (serverId === 'all') return;
  const reg = (await ddb.send(new GetItemCommand({ TableName: REGISTRY_TABLE, Key: { repo_id: { S: serverId } } }))).Item;
  if (!reg || reg.enabled?.S !== '1') throw new ApiError(404, `unknown MCP server '${serverId}' (not registered or disabled)`);
  const grant = (await ddb.send(new GetItemCommand({
    TableName: PLATFORM_TABLE, Key: { pk: { S: `USER#${sub}` }, sk: { S: `REPO#${serverId}` } },
  }))).Item;
  if (!grant) throw new ApiError(403, `you have no access to '${serverId}' — subscribe to it in the catalog or ask its owner to add you`);
}

const MCP_STATUS_HINTS = {
  403: 'forbidden (403) — this server is not scoped for the playground session',
  404: 'server not found (404)',
  429: 'throttled or quota exceeded (429)',
  502: 'MCP server unavailable (502) — its task may be starting',
  504: 'MCP server timed out (504)',
};

// One JSON-RPC message through the proxy Lambda. Returns
// { status, text } where status 0 = the invoke itself failed. The proxy caps
// its upstream call at ~26s (API Gateway's ceiling on the public path); the
// caller's timeout only bounds the invoke.
async function proxyInvoke(serverId, sub, payload, timeoutMs) {
  const proxyEvent = {
    pathParameters: { serverId },
    requestContext: { authorizer: { kid: 'playground', ownerSub: sub, scopeServerIds: serverId } },
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    isBase64Encoded: false,
  };
  let out;
  try {
    const res = await lambda.send(
      new InvokeCommand({ FunctionName: MCP_PROXY_FN, Payload: Buffer.from(JSON.stringify(proxyEvent)) }),
      { abortSignal: AbortSignal.timeout(timeoutMs) },
    );
    out = JSON.parse(Buffer.from(res.Payload || []).toString('utf8') || '{}');
  } catch (e) {
    return { status: 0, text: `MCP proxy call failed: ${e.name}` };
  }
  if (!out || typeof out !== 'object' || !('statusCode' in out)) return { status: 0, text: 'bad proxy response' };
  return { status: parseInt(out.statusCode, 10) || 500, text: String(out.body || '') };
}

// Raw JSON-RPC passthrough (tools/list, tools/call, ...) for the console's
// tool panel and direct-call tester — this streaming path has no API Gateway
// 30s ceiling and the SSE heartbeat keeps CloudFront's connection up.
async function mcpRaw(serverId, sub, payload, timeoutMs) {
  const { status, text } = await proxyInvoke(serverId, sub, payload, timeoutMs);
  if (status === 0) return { ok: false, status: 0, hint: text };
  if (status !== 200) {
    return { ok: false, status, hint: MCP_STATUS_HINTS[status] || `HTTP ${status}`, raw: text.slice(0, 500) };
  }
  try { return { ok: true, status: 200, body: JSON.parse(text) }; }
  catch { return { ok: true, status: 200, body: text.slice(0, 2000) }; }
}

async function mcpToolCall(serverId, sub, name, args, timeoutMs) {
  const { status, text: rawText } = await proxyInvoke(
    serverId, sub, { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args || {} } }, timeoutMs,
  );
  if (status === 0) return { text: rawText, isError: true };
  if (status !== 200) {
    const hint = MCP_STATUS_HINTS[status] || `MCP proxy returned HTTP ${status}`;
    return { text: `${hint}: ${rawText.slice(0, 500)}`, isError: true };
  }
  let parsed;
  try { parsed = JSON.parse(rawText); } catch { return { text: rawText.slice(0, 2000) || '(empty response)', isError: true }; }
  if (parsed.error) return { text: `MCP error ${parsed.error.code}: ${parsed.error.message}`, isError: true };
  const result = parsed.result || {};
  const parts = (result.content || []).filter((c) => c && c.type === 'text' && c.text).map((c) => c.text);
  let text = parts.join('\n') || JSON.stringify(result).slice(0, 4000);
  if (text.length > MAX_TOOL_RESULT_CHARS) {
    text = text.slice(0, MAX_TOOL_RESULT_CHARS) + `\n…[truncated at ${MAX_TOOL_RESULT_CHARS} chars]`;
  }
  return { text, isError: Boolean(result.isError) };
}

// ---------------------------------------------------------------------------
// handler
// ---------------------------------------------------------------------------

export const handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
  let stream = null;  // set once the 200 + SSE headers have been committed
  let heartbeat = null;  // SSE keepalive interval; cleared before stream.end()
  const cors = corsHeaders(event);
  try {
    const method = event.requestContext?.http?.method || '';
    if (method === 'OPTIONS') {
      // Must write a body before end(): a streamifyResponse prelude with
      // no bytes doesn't flush, and the metadata (status + CORS headers) is
      // dropped — the caller then sees a bare 200 with no Access-Control-*.
      const rs = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 200,
        headers: { ...cors, 'Content-Type': 'text/plain' },
      });
      rs.write('ok');
      rs.end();
      return;
    }
    if (method !== 'POST') throw new ApiError(405, 'POST only');

    const hdr = Object.entries(event.headers || {}).find(([k]) => k.toLowerCase() === 'x-graphify-id')?.[1] || '';
    const token = hdr.replace(/^Bearer\s+/i, '').trim();
    if (!token) throw new ApiError(401, 'missing identity token (X-Graphify-Id)');
    let claims;
    try { claims = await verifier.verify(token); } catch { throw new ApiError(401, 'invalid or expired token'); }
    const sub = claims.sub;

    const body = parseBody(event);
    const serverId = String(body.server_id || '').trim();
    await authorizeServer(sub, serverId);

    // op:"mcp" — a single JSON-RPC passthrough (tools/list, tools/call, ...).
    // The console's tool panel and direct-call tester use this instead of the
    // buffered /playground/mcp so a large repo's ~30s-per-request latency is
    // not clipped by API Gateway's 30s ceiling. No model call, no budget.
    if (body.op === 'mcp') {
      const payload = body.payload;
      if (!payload || typeof payload !== 'object' || !MCP_METHODS.has(payload.method)) {
        throw new ApiError(400, `payload.method must be one of ${JSON.stringify([...MCP_METHODS])}`);
      }
      stream = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: 200,
        headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', ...cors },
      });
      heartbeat = setInterval(() => { try { stream.write(': keepalive\n\n'); } catch {} }, 10_000);
      const remainingS = context.getRemainingTimeInMillis() / 1000 - 5;
      const res = await mcpRaw(serverId, sub, payload, Math.max(2, remainingS) * 1000);
      clearInterval(heartbeat); heartbeat = null;
      stream.write(`data: ${JSON.stringify({ type: 'mcp', ...res })}\n\n`);
      stream.end();
      return;
    }

    const model = String(body.model || DEFAULT_MODEL);
    if (!ALLOWED_MODELS.includes(model)) throw new ApiError(400, `model must be one of ${JSON.stringify(ALLOWED_MODELS)}`);
    const maxTokens = Math.min(parseInt(body.max_tokens, 10) || 2048, MAX_TOKENS_CAP);
    const system = String(body.system || '').slice(0, MAX_SYSTEM_CHARS);
    const messages = sanitizeMessages(body.messages);
    const tools = anthropicTools(body.tools);

    const day = await budgetCheck(sub);

    // Everything validated — commit the SSE response and start streaming.
    stream = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', ...cors },
    });
    const send = (obj) => stream.write(`data: ${JSON.stringify(obj)}\n\n`);
    // SSE keepalive: CloudFront (the OAC origin in front of this Function URL)
    // drops the connection on a silent origin gap of ~30-60s. A big repo's
    // tool execution (e.g. query_graph on a 157k-node graph) or a long model
    // TTFB on a large transcript produces exactly such gaps, so the client
    // never gets the `final` frame and the turn rolls back. A comment line
    // every 10s keeps bytes flowing; the client's SSE parser ignores it.
    heartbeat = setInterval(() => { try { stream.write(': keepalive\n\n'); } catch {} }, 10_000);

    const kwargs = { model, max_tokens: maxTokens, messages };
    if (system) kwargs.system = system;
    if (tools.length) {
      kwargs.tools = tools;
      // final=true: the client is wrapping up (loop budget hit) — keep the
      // tool definitions so the history stays coherent, forbid new calls.
      if (body.final === true) kwargs.tool_choice = { type: 'none' };
    }

    // Bound the generation to the Lambda budget. The SDK's `timeout` clears
    // once response headers arrive, so it does NOT cap a slow/stalled SSE body
    // — without this an abandoned generation runs to the 300s wall, which
    // ships no final frame and skips metering. Abort ~12s early so the catch
    // can emit a clean frame and meter what was produced.
    const genBudgetMs = Math.max(5_000, context.getRemainingTimeInMillis() - 12_000);
    const ac = new AbortController();
    const genTimer = setTimeout(() => ac.abort(), genBudgetMs);
    let usageIn = 0, usageOut = 0;

    const msgStream = anthropic.messages.stream(kwargs, { signal: ac.signal });
    // message_start carries input tokens; message_delta carries the running
    // output count — captured so an aborted generation is still metered.
    msgStream.on('streamEvent', (e) => {
      if (e.type === 'message_start') usageIn = e.message.usage.input_tokens;
      else if (e.type === 'message_delta' && e.usage) usageOut = e.usage.output_tokens;
    });
    msgStream.on('text', (delta) => send({ type: 'delta', text: delta }));
    msgStream.on('thinking', (delta) => send({ type: 'thinking', text: delta }));

    let message;
    try {
      message = await msgStream.finalMessage();
    } catch (e) {
      clearTimeout(genTimer);
      await budgetRecord(sub, day, usageIn + usageOut);  // meter partial spend
      if (ac.signal.aborted) throw new ApiError(504, 'response time budget exhausted mid-generation — narrow the question or lower max_tokens');
      throw new ApiError(502, `Bedrock call failed: ${e.name}: ${e.message}`);
    }
    clearTimeout(genTimer);

    await budgetRecord(sub, day, message.usage.input_tokens + message.usage.output_tokens);

    const assistant = message.content.map((b) => JSON.parse(JSON.stringify(b)));
    const out = {
      type: 'final',
      model,
      stop_reason: message.stop_reason,
      assistant,
      usage: { input_tokens: message.usage.input_tokens, output_tokens: message.usage.output_tokens },
    };

    if (message.stop_reason === 'tool_use') {
      for (const b of assistant) {
        if (b.type === 'tool_use') send({ type: 'tool_use', id: b.id, name: b.name, input: b.input || {} });
      }
      out.tool_results = [];
      for (const b of assistant) {
        if (b.type !== 'tool_use') continue;
        // Leave >=5s of Lambda budget so the final frame still ships.
        const remainingS = context.getRemainingTimeInMillis() / 1000 - 5;
        let text, isError;
        if (remainingS < 2) {
          text = 'tool execution skipped: request time budget exhausted — answer from the results you already have';
          isError = true;
          out.budget_exhausted = true;
        } else {
          // The proxy bounds each call at ~26s; the SSE heartbeat holds
          // CloudFront open across the wait.
          ({ text, isError } = await mcpToolCall(serverId, sub, b.name, b.input, Math.min(90, remainingS) * 1000));
        }
        send({ type: 'tool_result', tool_use_id: b.id, name: b.name, content: text, is_error: isError });
        const block = { type: 'tool_result', tool_use_id: b.id, content: text };
        if (isError) block.is_error = true;
        out.tool_results.push(block);
      }
    }

    send(out);
    if (heartbeat) clearInterval(heartbeat);
    stream.end();
  } catch (e) {
    if (heartbeat) clearInterval(heartbeat);
    const status = e instanceof ApiError ? e.status : 500;
    const message = e instanceof ApiError ? e.message : `internal error: ${e.name}`;
    if (!(e instanceof ApiError)) console.log(`UNHANDLED: ${e.name}: ${e.message}`);
    if (stream) {
      // Headers already sent — surface the failure inside the stream.
      stream.write(`data: ${JSON.stringify({ type: 'error', message })}\n\n`);
      stream.end();
    } else {
      const rs = awslambda.HttpResponseStream.from(responseStream, {
        statusCode: status,
        headers: { 'Content-Type': 'application/json', ...cors },
      });
      rs.write(JSON.stringify({ error: message }));
      rs.end();
    }
  }
});
