/**
 * The server's `app` on a tool result survives every path into the kit: the
 * AG-UI inverse, the reducer (new row and upsert) and the REST conversion.
 */
import { describe, expect, it } from "vitest";

import { fromAgui } from "../agui";
import type { SessionState } from "../protocol";
import { restToKitMessage } from "../restMessage";
import { sessionReducer } from "../sessionReducer";

const APP = { tool_call_id: "toolu_1", site: "connect-labs", tool: "workflow_run_action",
              resource_uri: "ui://labs/x", path: "direct" };

describe("MCP Apps wiring", () => {
  it("AG-UI TOOL_CALL_RESULT metadata.canopy.app becomes frame.data.app", () => {
    const [frame] = fromAgui({ type: "TOOL_CALL_RESULT", toolCallId: "m1", messageId: "m1",
                               content: "x", metadata: { canopy: { turn_index: 3, app: APP,
                               block: { tool_use_id: "toolu_1" } } } } as never);
    expect(frame).toMatchObject({ event: "chat.tool_result", data: { app: APP } });
  });

  it("the reducer keeps app on a new row and on an upsert", () => {
    const empty = { messages: [] } as unknown as SessionState;
    const frame = { event: "chat.tool_result" as const,
                    data: { parent_message_id: null, tool_message_id: "m1", turn_index: 3,
                            block: { tool_use_id: "toolu_1" }, app: APP } };
    const once = sessionReducer(empty, frame);
    expect(once.messages[0].app).toEqual(APP);
    const twice = sessionReducer(once, { ...frame, data: { ...frame.data, tool_message_id: "m2" } });
    expect(twice.messages).toHaveLength(1);
    expect(twice.messages[0].app).toEqual(APP);
  });

  it("restToKitMessage carries app", () => {
    expect(restToKitMessage({ turn_index: 1, role: "tool_result", content: {}, plaintext: "",
                              created_at: "", app: APP }).app).toEqual(APP);
  });
});
