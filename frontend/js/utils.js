// Small shared UI helpers: toast notifications, inline text formatting and safe Markdown for answers.
// Classic script (shared global scope), loaded by index.html before app.js.

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2400);
}

function appendInlineMarkdown(parent, text) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (!part) {
      return;
    }

    if (part.startsWith("**") && part.endsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = part.slice(2, -2);
      parent.appendChild(strong);
      return;
    }

    parent.appendChild(document.createTextNode(part));
  });
}

function appendFormattedText(container, text) {
  const blocks = String(text || "")
    .replace(/\r\n/g, "\n")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean);

  blocks.forEach((block) => {
    const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
    const isList = lines.every((line) => /^[-*]\s+/.test(line));

    if (isList) {
      const list = document.createElement("ul");
      lines.forEach((line) => {
        const item = document.createElement("li");
        appendInlineMarkdown(item, line.replace(/^[-*]\s+/, ""));
        list.appendChild(item);
      });
      container.appendChild(list);
      return;
    }

    lines.forEach((line) => {
      const paragraph = document.createElement("p");
      appendInlineMarkdown(paragraph, line);
      container.appendChild(paragraph);
    });
  });
}

// ---- Safe Markdown for AI Tutor answers ----------------------------------------------------------
// Model output is untrusted. It is parsed here into DOM nodes built only with createElement and
// textContent -- never innerHTML -- so any HTML the model writes is shown as literal text. Supported:
// paragraphs (single line breaks kept), headings, **bold**, *italic*, `inline code`, fenced code
// blocks, ordered/unordered (nested) lists, > quotes, --- rules, simple | tables, http(s) links and
// "(source: ...)" citations, which are styled as chips. Anything else falls back to plain text.
const MD_FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const MD_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const MD_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const MD_QUOTE = /^\s{0,3}>\s?/;
const MD_LIST_ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const MD_TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const MD_INLINE = new RegExp([
  "(`+)([^`]|[^`][\\s\\S]*?[^`])\\1(?!`)",                       // 1-2 `code`
  "\\*\\*(?=\\S)([\\s\\S]*?\\S)\\*\\*",                            // 3 **bold**
  "(?<![\\w_])__(?=\\S)([\\s\\S]*?\\S)__(?![\\w_])",               // 4 __bold__
  "(?<![\\w*])\\*(?=[^\\s*])([^*\\n]*?[^\\s*])\\*(?![\\w*])",      // 5 *italic*
  "(?<![\\w_])_(?=[^\\s_])([^_\\n]*?[^\\s_])_(?![\\w_])",          // 6 _italic_
  "\\[([^\\]\\n]+)\\]\\(([^\\s()]+)\\)",                           // 7-8 [label](url)
  "(https?:\\/\\/[^\\s<>]*[^\\s<>.,;:!?'\")\\]])",                 // 9 bare URL
  "(\\((?:sources?|nguồn)\\s*:[^()\\n]*\\)|\\[(?:Document|Source)\\s*\\d+\\])", // 10 citation
].join("|"), "i");

function safeLinkHref(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function markdownLink(href, label) {
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer nofollow";
  appendMarkdownInline(link, label);
  return link;
}

function markdownInlineNode(match) {
  const [whole, , code, bold, boldAlt, italic, italicAlt, label, url, bareUrl, citation] = match;
  if (code !== undefined) {
    const element = document.createElement("code");
    element.textContent = /^ .* $/.test(code) ? code.slice(1, -1) : code;
    return element;
  }
  if (bold !== undefined || boldAlt !== undefined) {
    const element = document.createElement("strong");
    appendMarkdownInline(element, bold ?? boldAlt);
    return element;
  }
  if (italic !== undefined || italicAlt !== undefined) {
    const element = document.createElement("em");
    appendMarkdownInline(element, italic ?? italicAlt);
    return element;
  }
  if (label !== undefined) {
    const href = safeLinkHref(url);
    return href ? markdownLink(href, label) : document.createTextNode(whole);
  }
  if (bareUrl !== undefined) {
    const href = safeLinkHref(bareUrl);
    if (!href) return document.createTextNode(whole);
    const link = markdownLink(href, "");
    link.textContent = bareUrl;
    return link;
  }
  const chip = document.createElement("span");
  chip.className = "inline-citation";
  chip.textContent = citation;
  return chip;
}

function appendMarkdownInline(parent, text) {
  let rest = String(text);
  while (rest) {
    const match = MD_INLINE.exec(rest);
    if (!match) {
      parent.appendChild(document.createTextNode(rest));
      return;
    }
    if (match.index) parent.appendChild(document.createTextNode(rest.slice(0, match.index)));
    parent.appendChild(markdownInlineNode(match));
    rest = rest.slice(match.index + match[0].length);
  }
}

function isTableStart(lines, index) {
  return lines[index].includes("|") && index + 1 < lines.length && MD_TABLE_DIVIDER.test(lines[index + 1]);
}

function startsMarkdownBlock(lines, index) {
  const line = lines[index];
  return MD_FENCE.test(line) || MD_HEADING.test(line) || MD_RULE.test(line) || MD_QUOTE.test(line)
    || MD_LIST_ITEM.test(line) || isTableStart(lines, index);
}

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function parseMarkdownTable(lines, start) {
  const wrap = document.createElement("div");
  wrap.className = "md-table-wrap";
  const table = document.createElement("table");
  const headRow = table.createTHead().insertRow();
  tableCells(lines[start]).forEach((cell) => {
    const th = document.createElement("th");
    appendMarkdownInline(th, cell);
    headRow.appendChild(th);
  });
  const body = table.createTBody();
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
    const row = body.insertRow();
    tableCells(lines[index]).forEach((cell) => appendMarkdownInline(row.insertCell(), cell));
    index += 1;
  }
  wrap.appendChild(table);
  return { element: wrap, next: index };
}

function listIndent(whitespace) {
  return whitespace.replace(/\t/g, "    ").length;
}

// One list starting at lines[start]; deeper-indented items become nested lists inside the last item.
function parseMarkdownList(lines, start) {
  const first = lines[start].match(MD_LIST_ITEM);
  const baseIndent = listIndent(first[1]);
  const ordered = /\d/.test(first[2]);
  const list = document.createElement(ordered ? "ol" : "ul");
  if (ordered && Number.parseInt(first[2], 10) !== 1) list.start = Number.parseInt(first[2], 10);
  let item = null;
  let index = start;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      let ahead = index + 1;
      while (ahead < lines.length && !lines[ahead].trim()) ahead += 1;
      const nextItem = ahead < lines.length && lines[ahead].match(MD_LIST_ITEM);
      if (!nextItem || listIndent(nextItem[1]) < baseIndent) break;
      index = ahead;   // a blank line between items keeps the same list
      continue;
    }
    const marker = line.match(MD_LIST_ITEM);
    if (marker) {
      const indent = listIndent(marker[1]);
      if (indent < baseIndent) break;
      if (indent >= baseIndent + 2 && item) {
        const nested = parseMarkdownList(lines, index);
        item.appendChild(nested.element);
        index = nested.next;
        continue;
      }
      if (/\d/.test(marker[2]) !== ordered) break;
      item = document.createElement("li");
      appendMarkdownInline(item, marker[3]);
      list.appendChild(item);
      index += 1;
      continue;
    }
    // An indented line continues the current item; anything else ends the list.
    if (item && listIndent(line.match(/^\s*/)[0]) > baseIndent && !startsMarkdownBlock(lines, index)) {
      item.appendChild(document.createElement("br"));
      appendMarkdownInline(item, line.trim());
      index += 1;
      continue;
    }
    break;
  }
  return { element: list, next: index };
}

function appendMarkdownBlocks(container, lines, topHeadingLevel) {
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    const fence = line.match(MD_FENCE);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !(lines[index].trim().startsWith(fence[1][0].repeat(fence[1].length)) && MD_FENCE.test(lines[index]))) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;   // closing fence (an unclosed fence runs to the end of the answer)
      const pre = document.createElement("pre");
      const element = document.createElement("code");
      if (fence[2]) pre.dataset.language = fence[2];
      element.textContent = code.join("\n");
      pre.appendChild(element);
      container.appendChild(pre);
      continue;
    }

    const heading = line.match(MD_HEADING);
    if (heading) {
      // The answer's top heading level becomes h3 (bubble-sized), deeper levels h4 and h5.
      const element = document.createElement(`h${Math.min(3 + heading[1].length - topHeadingLevel, 5)}`);
      element.className = "md-heading";
      appendMarkdownInline(element, heading[2]);
      container.appendChild(element);
      index += 1;
      continue;
    }

    if (MD_RULE.test(line)) {
      container.appendChild(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (MD_QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && MD_QUOTE.test(lines[index])) {
        quoted.push(lines[index].replace(MD_QUOTE, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      appendMarkdownBlocks(quote, quoted, topHeadingLevel);
      container.appendChild(quote);
      continue;
    }

    if (MD_LIST_ITEM.test(line)) {
      const list = parseMarkdownList(lines, index);
      container.appendChild(list.element);
      index = list.next;
      continue;
    }

    if (isTableStart(lines, index)) {
      const table = parseMarkdownTable(lines, index);
      container.appendChild(table.element);
      index = table.next;
      continue;
    }

    const paragraph = document.createElement("p");
    let firstLine = true;
    while (index < lines.length && lines[index].trim() && (firstLine || !startsMarkdownBlock(lines, index))) {
      if (!firstLine) paragraph.appendChild(document.createElement("br"));
      appendMarkdownInline(paragraph, lines[index].trim());
      firstLine = false;
      index += 1;
    }
    container.appendChild(paragraph);
  }
}

// Renders untrusted Markdown into container; on any parser failure shows the raw text instead.
function appendMarkdown(container, text) {
  const source = String(text || "").replace(/\r\n?/g, "\n");
  try {
    const lines = source.split("\n");
    let inFence = false;   // "# comment" lines inside code blocks are not headings
    const levels = lines.map((line) => {
      if (MD_FENCE.test(line)) inFence = !inFence;
      return inFence ? 0 : line.match(MD_HEADING)?.[1].length;
    }).filter(Boolean);
    appendMarkdownBlocks(container, lines, levels.length ? Math.min(...levels) : 1);
  } catch (error) {
    console.error("Markdown rendering failed; showing plain text", error);
    container.textContent = "";
    const paragraph = document.createElement("p");
    paragraph.className = "md-plain";
    paragraph.textContent = source;
    container.appendChild(paragraph);
  }
}
