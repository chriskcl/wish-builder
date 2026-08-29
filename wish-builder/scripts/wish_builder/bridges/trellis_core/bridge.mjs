#!/usr/bin/env node

import { parseStrictJsonBytes, StrictJsonError } from "./strict-json.mjs";
import {
  BridgeRequestError,
  MAX_STDIN_BYTES,
  failureResponse,
  handleBridgeRequest,
} from "./protocol.mjs";

async function main() {
  const originalWrite = process.stdout.write.bind(process.stdout);
  let action = null;
  let response;
  let exitCode = 0;
  process.stdout.write = () => {
    throw new Error("Bridge dependencies must not write to stdout");
  };
  try {
    if (process.argv.length !== 2) {
      throw new BridgeRequestError("INVALID_INVOCATION", "Bridge accepts no command-line arguments");
    }
    const bytes = await readStandardInput();
    let request;
    try {
      request = parseStrictJsonBytes(bytes);
    } catch (error) {
      if (error instanceof StrictJsonError) {
        throw new BridgeRequestError("INVALID_JSON", error.message);
      }
      throw error;
    }
    if (
      request !== null &&
      typeof request === "object" &&
      !Array.isArray(request) &&
      typeof request.action === "string"
    ) {
      action = request.action;
    }
    response = await handleBridgeRequest(request);
  } catch (error) {
    const failure = failureResponse(error, action);
    response = failure.body;
    exitCode = failure.exitCode;
  }
  try {
    originalWrite(JSON.stringify(response));
  } catch {
    exitCode = 5;
    originalWrite(
      '{"protocolVersion":1,"ok":false,"action":null,"error":{"code":"BRIDGE_FAILURE","message":"Bridge response serialization failed","details":null}}',
    );
  }
  process.exitCode = exitCode;
}

async function readStandardInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_STDIN_BYTES) {
      throw new BridgeRequestError("INPUT_TOO_LARGE", `Bridge input exceeds ${MAX_STDIN_BYTES} bytes`);
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size);
}

await main();
