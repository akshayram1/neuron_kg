import { type ReactNode } from "react";

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Bold before italic so `**x**` is not eaten as `*x*`.
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(?<!\*)\*(?!\*)([^*]+)\*(?!\*)/g;
  let last = 0;
  let key = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(text.slice(last, start));
    if (match[1]) {
      nodes.push(<code key={key++}>{match[1].slice(1, -1)}</code>);
    } else if (match[2]) {
      nodes.push(<strong key={key++}>{match[2].slice(2, -2)}</strong>);
    } else {
      nodes.push(<em key={key++}>{match[3]}</em>);
    }
    last = start + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/** Models often glue `### Heading The sentence` and `includes: - item` on one line. */
export function normalizeMarkdown(text: string): string {
  let out = text.replace(/\r\n/g, "\n");
  out = out.replace(/[ \t]*###[ \t]+/g, "\n\n### ");
  out = out.replace(/[ \t]*##[ \t]+(?!#)/g, "\n\n## ");
  const expanded: string[] = [];
  for (const line of out.split("\n")) {
    for (const piece of splitGluedHeading(line)) {
      expanded.push(...explodeInlineBullets(piece));
    }
  }
  return expanded.join("\n");
}

function splitGluedHeading(line: string): string[] {
  const heading = /^(#{1,3})\s+(.*)$/.exec(line);
  if (!heading) return [line];
  const hashes = heading[1];
  const rest = heading[2].trim();
  const dash = rest.match(/^(.{3,80}?)\s+-\s+(.+)$/);
  if (dash && !dash[1].includes(".")) {
    return [`${hashes} ${dash[1].trim()}`, `- ${dash[2].trim()}`];
  }
  const sentence = rest.match(/^(.{3,80}?)\s+(The |This |That |Reported )(.*)$/);
  if (sentence && !sentence[1].includes(".")) {
    return [`${hashes} ${sentence[1].trim()}`, `${sentence[2]}${sentence[3]}`];
  }
  return [line];
}

function explodeInlineBullets(line: string): string[] {
  if (/^#{1,3}\s/.test(line) || line.startsWith("```") || /^---+$/.test(line.trim())) {
    return [line];
  }
  const parts = line.split(/\s+-\s+/);
  if (parts.length < 2) return [line];
  const out: string[] = [];
  const first = parts[0].trim();
  if (first) out.push(first);
  for (const part of parts.slice(1)) {
    const item = part.trim();
    if (item) out.push(item.startsWith("- ") ? item : `- ${item}`);
  }
  return out;
}

export default function MarkdownBody({ text }: { text: string }) {
  const nodes: ReactNode[] = [];
  const lines = normalizeMarkdown(text).split("\n");
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      const Tag = heading[1].length === 1 ? "h2" : heading[1].length === 2 ? "h3" : "h4";
      nodes.push(<Tag key={key++}>{renderInline(heading[2])}</Tag>);
      i += 1;
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      nodes.push(<hr key={key++} />);
      i += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const fence: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) {
        fence.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      nodes.push(<pre key={key++}><code>{fence.join("\n")}</code></pre>);
      continue;
    }
    if (/^[-*]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i += 1;
      }
      nodes.push(
        <ul key={key++}>
          {items.map((item, index) => <li key={index}>{renderInline(item)}</li>)}
        </ul>,
      );
      continue;
    }
    if (/^\d+\.\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i += 1;
      }
      nodes.push(
        <ol key={key++}>
          {items.map((item, index) => <li key={index}>{renderInline(item)}</li>)}
        </ol>,
      );
      continue;
    }
    const paragraph: string[] = [];
    while (
      i < lines.length
      && lines[i].trim()
      && !/^(#{1,3})\s+/.test(lines[i])
      && !/^---+$/.test(lines[i].trim())
      && !lines[i].startsWith("```")
      && !/^[-*]\s/.test(lines[i])
      && !/^\d+\.\s/.test(lines[i])
    ) {
      paragraph.push(lines[i]);
      i += 1;
    }
    nodes.push(<p key={key++}>{renderInline(paragraph.join(" "))}</p>);
  }

  return <>{nodes}</>;
}
