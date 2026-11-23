import { describe, expect, it } from "vitest";
import { MAX_FRAME_BYTES, PROTOCOL_VERSION, parseHostMessage } from "./protocol";

describe("Runtime stdio protocol", () => {
  it("parses a versioned response frame", () => {
    expect(
      parseHostMessage(
        JSON.stringify({
          type: "response",
          protocolVersion: PROTOCOL_VERSION,
          requestId: "request-1",
          result: { status: "ready" },
        }),
      ),
    ).toMatchObject({ type: "response", requestId: "request-1", result: { status: "ready" } });
  });

  it("parses monotonic event metadata without changing payloads", () => {
    expect(
      parseHostMessage(
        JSON.stringify({
          type: "event",
          protocolVersion: PROTOCOL_VERSION,
          eventId: "event-1",
          sequence: 7,
          event: "assistant_delta",
          payload: { text: "你好" },
        }),
      ),
    ).toMatchObject({ sequence: 7, event: "assistant_delta", payload: { text: "你好" } });
  });

  it("rejects malformed and unsupported-version frames", () => {
    expect(() => parseHostMessage("not-json")).toThrow();
    expect(() =>
      parseHostMessage(JSON.stringify({ type: "event", protocolVersion: 9 })),
    ).toThrow("unsupported protocol version");
    expect(() =>
      parseHostMessage(
        JSON.stringify({
          type: "event",
          protocolVersion: PROTOCOL_VERSION,
          eventId: "event-1",
          sequence: 1,
          event: "task.completed",
          payload: [],
        }),
      ),
    ).toThrow("malformed protocol frame");
  });

  it("rejects responses without exactly one valid result or error payload", () => {
    expect(() =>
      parseHostMessage(JSON.stringify({ type: "response", protocolVersion: 1, requestId: "" })),
    ).toThrow("invalid response identifier");
    expect(() =>
      parseHostMessage(
        JSON.stringify({ type: "response", protocolVersion: 1, requestId: "r-1" }),
      ),
    ).toThrow("malformed response payload");
    expect(() =>
      parseHostMessage(
        JSON.stringify({
          type: "response",
          protocolVersion: 1,
          requestId: "r-1",
          error: { code: "BROKEN", message: 2 },
        }),
      ),
    ).toThrow("invalid error payload");
  });

  it("documents the byte limit enforced by both process boundaries", () => {
    expect(MAX_FRAME_BYTES).toBe(1_048_576);
  });
});
