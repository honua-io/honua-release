/**
 * The model turn: a manual Claude tool-use loop over the Honua AI map kit's
 * MCP-shaped tool definitions. `bridge` abstracts the page: it runs kit
 * calls in the browser and streams assistant text back to the chat panel.
 */
import { AnthropicBedrock, AnthropicBedrockMantle } from "@anthropic-ai/bedrock-sdk";

const REGION = process.env.AWS_REGION ?? "us-east-1";
const MODEL = process.env.HONUA_AGENT_MODEL ?? "us.anthropic.claude-haiku-4-5-20251001-v1:0";
const CLIENT_KIND = process.env.HONUA_AGENT_CLIENT ?? "legacy";
const MAX_ROUNDS = 12;
const MAX_RESULT_CHARS = 7000;

const client = CLIENT_KIND === "mantle" ? new AnthropicBedrockMantle({ awsRegion: REGION }) : new AnthropicBedrock({ awsRegion: REGION });

const GUIDANCE = `
You are the map agent inside a Honua SDK web app showing Maui (Hawaiʻi). The user types a request; you compose the map by calling tools. The map is live in the user's browser: every action tool call is applied immediately, so act, do not ask for permission.

Practical rules for THIS runtime:
- Sources are the ids listed in the semantic map context (e.g. "maui-flood-hazard"). addLayer takes { layer: { id, sourceId, title?, limit?, bbox?, where?, popupFields?, fitBounds?, renderer?, paint?, geometry? } }. Use a bbox ([west, south, east, north]) or a small limit for big sources like maui-parcels (51k rows) or maui-buildings; limit defaults to 2000 and is the row cap.
- renderer: { kind: "unique-value", field, values: [{ value, color }], defaultColor? } or { kind: "class-breaks", field, breaks: [{ min?, max?, label? }], colors: [...], defaultColor? } or { kind: "heatmap", weightField?, radius? }. Apply later with setLayerStyle { layerId, style: { renderer } } or paint overrides { layerId, style: { paint: { polygon: {...}, line: {...}, point: {...}, polygonOutline: {...} } } } (MapLibre paint properties).
- setFilter { id, clause: { field, operator, value, appliesTo: [sourceId] } } filters mounted layers server-side (operators = != < <= > >= in between like is-null). Clear with { id } and no clause.
- setViewport { bbox } or { center, zoom, pitch, bearing }. Maui island bbox is roughly [-156.7, 20.57, -155.97, 21.03]; Kahului is near [-156.47, 20.89]; Lahaina near [-156.68, 20.88]; Kihei near [-156.45, 20.75]; Hana near [-155.99, 20.76].
- setVisibility takes { draftId: "local", generation: 0, layerId, visible } in this runtime.
- addWidget { widget: { id, kind, title? } } docks one of the SDK's web components over the map. Kinds available here: "layer-list" (a.k.a. toc / table of contents — the SDK's <honua-layer-list>, with per-layer show/hide toggles the user can click) and "legend" (<honua-legend>, driven by each layer's renderer). removeWidget { widgetId } removes it. When the user asks for a TOC, layer list, or legend, call addWidget; do not say it is unsupported.
- Prefer one inspectMap at the start of a conversation, then act. Keep replies short: say what you did and anything notable in the data (counts, overflow). Field names come from the source metadata; do not invent fields.
`;

function bound(value) {
  const text = JSON.stringify(value ?? null);
  return text.length > MAX_RESULT_CHARS ? `${text.slice(0, MAX_RESULT_CHARS)}… [truncated ${text.length - MAX_RESULT_CHARS} chars]` : text;
}

export function createAgent(bridge) {
  const history = [];
  let tools;
  let systemPrompt;

  async function turn(userText, log = console.log) {
    tools ??= (await bridge.tools()).map((tool) => ({ name: tool.name, description: tool.description, input_schema: tool.inputSchema }));
    // Refresh the semantic map context every turn: the kit derives it from live state.
    systemPrompt = `${await bridge.prompt()}\n${GUIDANCE}`;
    history.push({ role: "user", content: userText });
    for (let round = 0; round < MAX_ROUNDS; round += 1) {
      const response = await client.messages.create({
        model: MODEL,
        max_tokens: 4000,
        system: systemPrompt,
        tools,
        messages: history,
      });
      history.push({ role: "assistant", content: response.content });
      const toolUses = response.content.filter((block) => block.type === "tool_use");
      for (const block of response.content) {
        if (block.type === "text" && block.text.trim()) await bridge.say(toolUses.length ? "assistant-progress" : "assistant", block.text.trim());
      }
      if (response.stop_reason !== "tool_use" || toolUses.length === 0) {
        if (!response.content.some((block) => block.type === "text" && block.text.trim())) await bridge.say("assistant", "(done)");
        return { rounds: round + 1, usage: response.usage, stopReason: response.stop_reason };
      }
      const results = [];
      for (const use of toolUses) {
        log(`[agent] tool ${use.name} ${JSON.stringify(use.input).slice(0, 200)}`);
        let content;
        let isError = false;
        try {
          const outcome = await bridge.call({ name: use.name, args: use.input });
          content = bound(outcome);
          isError = outcome?.status === "error" || outcome?.status === "denied";
        } catch (error) {
          content = `Tool execution failed: ${error.message}`;
          isError = true;
        }
        results.push({ type: "tool_result", tool_use_id: use.id, content, ...(isError ? { is_error: true } : {}) });
      }
      history.push({ role: "user", content: results });
    }
    await bridge.say("error", `Stopped after ${MAX_ROUNDS} tool rounds without a final answer.`);
    return { rounds: MAX_ROUNDS, stopReason: "max_rounds" };
  }

  return { turn, history, model: MODEL };
}
