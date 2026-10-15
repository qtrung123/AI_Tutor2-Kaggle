// AI Tutor answer streaming: reads the chat API's Server-Sent Events (Accept: text/event-stream) and
// batches the growing answer into a few renders per second. Classic script (shared global scope),
// loaded before tutor.js; DOM-free, so it also runs under Node in the tests.
//
// Events, one JSON object per `data:` frame: {type: "start"} · {type: "delta", text} (the answer, in
// order) · {type: "done", ...the complete JSON answer payload} · {type: "error", message}.

const TUTOR_STREAM_MEDIA_TYPE = "text/event-stream";
const TUTOR_STREAM_RENDER_MS = 80;   // at most ~12 Markdown renders a second while text arrives

// Split complete SSE frames off `buffer`: {events, rest}. A frame ends at a blank line; only its
// `data:` lines matter (comments and other fields are ignored). `rest` is an unfinished frame.
function parseTutorStreamFrames(buffer) {
  const frames = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = frames.pop();
  const events = [];
  frames.forEach((frame) => {
    const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).replace(/^ /, ""));
    if (data.length) events.push(JSON.parse(data.join("\n")));
  });
  return { events, rest };
}

class TutorStreamError extends Error {}

// Read a streamed answer to its end. Calls onDelta(text) per piece and resolves with the "done"
// payload. Rejects with a TutorStreamError for an "error" event or a stream that ends without
// "done" (connection lost); an aborted fetch rejects with the browser's AbortError.
async function readTutorAnswerStream(response, onDelta) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      // An unfinished last frame (connection cut mid-frame) is never parsed: it ends as "closed early".
      const parsed = parseTutorStreamFrames(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) {
        if (event.type === "delta" && event.text) onDelta(event.text);
        else if (event.type === "error") throw new TutorStreamError(event.message || "The tutor could not finish this answer.");
        else if (event.type === "done") return event;
      }
      if (done) throw new TutorStreamError("The connection closed before the answer finished.");
    }
  } finally {
    reader.releaseLock?.();
  }
}

// Coalesce many update requests into one render per `intervalMs`. flush() renders now (if anything
// is pending), cancel() drops a pending render.
function createTutorStreamRenderer(render, intervalMs = TUTOR_STREAM_RENDER_MS, schedule = setTimeout, unschedule = clearTimeout) {
  let timer = null;
  let pending = false;
  const run = () => {
    timer = null;
    if (!pending) return;
    pending = false;
    render();
  };
  return {
    request() {
      pending = true;
      if (timer === null) timer = schedule(run, intervalMs);
    },
    flush() {
      if (timer !== null) unschedule(timer);
      run();
    },
    cancel() {
      if (timer !== null) unschedule(timer);
      timer = null;
      pending = false;
    },
  };
}

if (typeof module !== "undefined") {
  module.exports = { parseTutorStreamFrames, readTutorAnswerStream, createTutorStreamRenderer, TutorStreamError };
}
