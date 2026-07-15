export async function resolveWebSocketConstructor() {
  if (typeof globalThis.WebSocket === "function") {
    return globalThis.WebSocket;
  }

  try {
    const wsModule = await import("ws");
    return wsModule.WebSocket || wsModule.default;
  } catch (error) {
    throw new Error(
      [
        "No WebSocket implementation is available in this Node runtime.",
        "Use Node with globalThis.WebSocket or install the ws package in this workspace.",
        `Original error: ${error instanceof Error ? error.message : String(error)}`
      ].join(" ")
    );
  }
}

export async function createWebSocket(url, protocols, options) {
  const WebSocketConstructor = await resolveWebSocketConstructor();
  if (options !== undefined) {
    return new WebSocketConstructor(url, protocols, options);
  }
  if (protocols !== undefined) {
    return new WebSocketConstructor(url, protocols);
  }
  return new WebSocketConstructor(url);
}
